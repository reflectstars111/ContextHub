"""HTTP boundary checks for the explicitly configured HTTPS tunnel deployment."""

import contextlib
import http.client
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

from contexthub.__main__ import main, parser
from contexthub.server import create_server, public_origin
from contexthub.store import StoreCapacityError
from tests.test_server import sample_pack


ORIGIN = 'https://contexthub.example.com'
HOST = 'contexthub.example.com'


class PublicOriginTests(unittest.TestCase):
    def make_server(self, **options):
        temp = tempfile.TemporaryDirectory()
        server = create_server(Path(temp.name), port=0, **options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join()
            temp.cleanup()

        self.addCleanup(cleanup)
        return server

    def setUp(self):
        self.server = self.make_server(public_url=ORIGIN, max_body_mib=1)

    def request(self, method='GET', path='/api/config', data=None, headers=None, server=None):
        server = server or self.server
        request_headers = {'Host': HOST}
        if server.trust_cloudflare:
            request_headers['X-Forwarded-Proto'] = 'https'
        request_headers.update(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            request_headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request(method, path, data, request_headers)
        response = connection.getresponse()
        content = response.read()
        result_headers = dict(response.getheaders())
        status = response.status
        connection.close()
        if content and 'application/json' in result_headers.get('Content-Type', ''):
            content = json.loads(content)
        return status, content, result_headers

    def raw(self, request, server=None):
        with socket.create_connection((server or self.server).server_address, timeout=5) as connection:
            connection.sendall(request)
            response = b''
            while block := connection.recv(65536):
                response += block
            return response

    def register(self):
        status, data, headers = self.request('POST', '/api/register',
                                             {'username': 'alice', 'password': 'test-password-123'})
        self.assertEqual(status, 201, data)
        return data, headers['Set-Cookie']

    def wait_for_slots(self, server, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if server._request_slots._value == count:
                return
            time.sleep(0.01)
        self.fail(f'worker slots did not reach {count}')

    def test_public_origin_requires_unambiguous_https_origin(self):
        for value in ('http://example.com', 'https://user:pass@example.com', 'https://example.com/a',
                      'https://example.com?x=1', 'https://example.com#token', '//example.com',
                      'https://example.com:0', 'https://example.com:99999', 'https://bad_name.com',
                      'https://example.com\\evil', 'https://example.com\n', 'https://-bad.com',
                      '\x00https://example.com', 'https://example.com:', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                public_origin(value)
        self.assertEqual(public_origin('https://Example.com:443/'), 'https://example.com')
        self.assertEqual(public_origin('https://example.com:8443'), 'https://example.com:8443')
        with self.assertRaises(ValueError):
            create_server('/unused', port=0, mode='connector', public_url=ORIGIN)

    def test_host_is_pinned_and_forwarded_headers_cannot_override_it(self):
        for host in ('127.0.0.1', 'evil.example', f'{HOST}.', f'{HOST}:80'):
            with self.subTest(host=host):
                self.assertEqual(self.request(headers={'Host': host, 'X-Forwarded-Host': HOST,
                                                       'X-Forwarded-Proto': 'https'})[0], 421)
        status, _, headers = self.request(headers={'Host': HOST.upper(), 'X-Forwarded-Host': 'evil.example',
                                                  'X-Forwarded-Proto': 'http'})
        self.assertEqual(status, 200)
        self.assertEqual(headers['Strict-Transport-Security'], 'max-age=31536000')
        self.assertTrue(self.raw(b'GET /api/config HTTP/1.0\r\n\r\n').startswith(b'HTTP/1.0 421'))

    def test_origin_rejects_scheme_mismatch_and_cross_origin_even_on_get(self):
        for origin in ('http://' + HOST, ORIGIN + '/', ORIGIN + ':443', 'null', 'https://evil.example'):
            with self.subTest(origin=origin):
                self.assertEqual(self.request(headers={'Origin': origin})[0], 403)
        self.assertEqual(self.request(headers={'Origin': ORIGIN})[0], 200)
        self.assertEqual(self.request()[0], 200)  # Non-browser clients have no Origin.
        self.assertEqual(self.request('POST', '/api/register', {},
                                      {'Origin': ORIGIN, 'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_secure_host_cookie_login_logout_and_revocation(self):
        session, header = self.register()
        cookie = SimpleCookie(header)
        value = cookie['__Host-ch_session']
        self.assertTrue(value['secure'])
        self.assertTrue(value['httponly'])
        self.assertEqual(value['path'], '/')
        self.assertEqual(value['samesite'], 'Strict')
        self.assertEqual(value['domain'], '')
        auth = {'Cookie': header.split(';')[0], 'X-CSRF-Token': session['csrf_token']}
        self.assertEqual(self.request(path='/api/session', headers=auth)[1]['user']['username'], 'alice')
        insecure = 'ch_session=' + value.value
        self.assertIsNone(self.request(path='/api/session', headers={'Cookie': insecure})[1]['user'])
        status, _, cleared = self.request('POST', '/api/logout', {}, auth)
        self.assertEqual(status, 200)
        clear_cookie = SimpleCookie(cleared['Set-Cookie'])['__Host-ch_session']
        self.assertTrue(clear_cookie['secure'])
        self.assertEqual(clear_cookie['max-age'], '0')
        self.assertIsNone(self.request(path='/api/session', headers=auth)[1]['user'])
        status, _, logged_in = self.request('POST', '/api/login',
                                             {'username': 'alice', 'password': 'test-password-123'})
        self.assertEqual(status, 200)
        self.assertTrue(SimpleCookie(logged_in['Set-Cookie'])['__Host-ch_session']['secure'])

    def test_public_reads_and_private_owner_access_work_with_secure_cookie(self):
        session, header = self.register()
        auth = {'Cookie': header.split(';')[0], 'X-CSRF-Token': session['csrf_token'], 'Origin': ORIGIN}
        private = self.request('POST', '/api/packages?visibility=private', sample_pack(), auth)[1]
        public = self.request('POST', '/api/packages?visibility=public', sample_pack(), auth)[1]
        self.assertEqual(self.request(path=f"/api/packages/{private['id']}/download")[0], 401)
        self.assertEqual(self.request(path=f"/api/packages/{private['id']}/download", headers=auth)[1], sample_pack())
        self.assertEqual(self.request(path=f"/api/packages/{public['id']}/download")[0], 401)
        self.assertEqual(self.request(path=f"/api/packages/{public['id']}/download", headers=auth)[1], sample_pack())
        self.assertEqual([p['id'] for p in self.request(path='/api/packages')[1]['packages']], [public['id']])
        self.assertEqual(self.request(path=f"/api/packages/{public['id']}")[0], 200)

    def test_fixed_versions_and_merge_candidates_also_require_login_to_download(self):
        session, header = self.register()
        auth = {'Cookie': header.split(';')[0], 'X-CSRF-Token': session['csrf_token']}
        package = self.request('POST', '/api/packages?visibility=public', sample_pack(), auth)[1]
        detail = self.request(path=f"/api/packages/{package['id']}")[1]
        path = f"/api/packages/{package['id']}/download?version={detail['version']['id']}"
        self.assertEqual(self.request(path=path)[0], 401)
        self.assertEqual(self.request(path=path, headers=auth)[1], sample_pack())
        with patch.object(self.server.contributions, 'candidate', return_value=sample_pack()) as candidate:
            self.assertEqual(self.request(path='/api/contributions/proposal/candidate?check_id=fixed')[0], 401)
            candidate.assert_not_called()
            self.assertEqual(self.request(path='/api/contributions/proposal/candidate?check_id=fixed', headers=auth)[1], sample_pack())
            candidate.assert_called_once_with('proposal', session['user']['id'], 'fixed')

    def test_all_hub_mutations_and_head_downloads_require_login(self):
        paths = ['/api/logout', '/api/inspect', '/api/packages', '/api/packages/library/fork',
                 '/api/packages/library/preview', '/api/packages/library/versions',
                 '/api/packages/library/contributions']
        paths += [f'/api/contributions/proposal/{action}' for action in
                  ('comments', 'status', 'source', 'check', 'merge')]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.request('POST', path, {})[0], 401)
        self.assertEqual(self.request('DELETE', '/api/packages/library')[0], 401)
        for path in ('/api/packages/library/download', '/api/contributions/proposal/candidate'):
            status, body, _ = self.request('HEAD', path)
            self.assertEqual(status, 401)
            self.assertEqual(body, b'')

    def test_ambiguous_framing_and_duplicate_security_headers_are_rejected(self):
        for extra in (f'Host: {HOST}\r\n', 'Content-Length: 0\r\nContent-Length: 0\r\n',
                      'Content-Length: 0,0\r\n', 'Transfer-Encoding: chunked\r\n',
                      'Content-Length: -1\r\n', 'Transfer-Encoding: identity\r\nContent-Length: 0\r\n'):
            with self.subTest(extra=extra):
                response = self.raw(f'GET /api/config HTTP/1.1\r\nHost: {HOST}\r\n{extra}\r\n'.encode())
                self.assertTrue(response.startswith(b'HTTP/1.0 400'), response)
        response = self.raw(f'GET /api/config HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\nOrigin: {ORIGIN}\r\n\r\n'.encode())
        self.assertTrue(response.startswith(b'HTTP/1.0 403'), response)
        self.assertEqual(self.request(path=f'{ORIGIN}/api/config')[0], 400)

    def test_configured_body_limit_is_reported_and_enforced_before_read(self):
        self.assertEqual(self.request()[1]['max_package_bytes'], 1024 * 1024)
        status, _, _ = self.request('POST', '/api/login', headers={'Content-Length': str(1024 * 1024 + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(self.request('POST', '/api/login', b'x' * 4097)[0], 413)

    def test_cloudflare_trust_requires_https_and_loopback_bind(self):
        for options in ({'trust_cloudflare': True}, {'trust_cloudflare': True, 'public_url': ORIGIN, 'host': '0.0.0.0'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                create_server('/unused', port=0, **options)
        for options in ({'max_connections': 0}, {'max_connections': 129}, {'request_timeout': float('inf')},
                        {'max_body_mib': 0}, {'max_body_mib': 102}, {'max_storage_mib': -1}, {'min_free_mib': -1},
                        {'request_deadline': 0}, {'request_deadline': float('inf')}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                create_server('/unused', port=0, **options)

    def test_cloudflare_requires_single_valid_ip_and_limits_each_visitor_separately(self):
        server = self.make_server(public_url=ORIGIN, trust_cloudflare=True)
        for headers in ({}, {'CF-Connecting-IP': '1.2.3.4, 5.6.7.8'}, {'CF-Connecting-IP': 'invalid'},
                        {'CF-Connecting-IP': 'fe80::1%eth0'}):
            self.assertEqual(self.request(server=server, headers=headers)[0], 403)
        duplicate = f'GET /api/config HTTP/1.1\r\nHost: {HOST}\r\nCF-Connecting-IP: 1.2.3.4\r\nCF-Connecting-IP: 1.2.3.4\r\n\r\n'
        self.assertTrue(self.raw(duplicate.encode(), server).startswith(b'HTTP/1.0 403'))
        for index in range(20):
            status, _, _ = self.request('POST', '/api/login', {},
                                          {'CF-Connecting-IP': '2001:db8::1', 'X-Forwarded-For': f'192.0.2.{index}'}, server)
            self.assertEqual(status, 403)
        self.assertEqual(self.request('POST', '/api/login', {}, {'CF-Connecting-IP': '2001:0db8::1'}, server)[0], 429)
        self.assertEqual(self.request('POST', '/api/login', {}, {'CF-Connecting-IP': '192.0.2.1'}, server)[0], 403)

    def test_untrusted_forwarded_ip_headers_do_not_bypass_auth_limit(self):
        for index in range(20):
            self.assertEqual(self.request('POST', '/api/login', {},
                                          {'CF-Connecting-IP': f'192.0.2.{index}', 'X-Forwarded-For': f'192.0.2.{index}'})[0], 403)
        self.assertEqual(self.request('POST', '/api/login', {}, {'CF-Connecting-IP': '203.0.113.10'})[0], 429)
        self.assertEqual(set(self.server.auth_attempts), {'127.0.0.1'})

    def test_trusted_http_redirects_reads_but_rejects_writes_and_ambiguous_scheme(self):
        server = self.make_server(public_url=ORIGIN, trust_cloudflare=True)
        headers = {'CF-Connecting-IP': '192.0.2.1', 'X-Forwarded-Proto': 'http'}
        status, _, response = self.request(path='/api/packages?q=hello', headers=headers, server=server)
        self.assertEqual(status, 308)
        self.assertEqual(response['Location'], ORIGIN + '/api/packages?q=hello')
        status, body, response = self.request('HEAD', headers=headers, server=server)
        self.assertEqual(status, 308)
        self.assertEqual(body, b'')
        self.assertEqual(response['Location'], ORIGIN + '/api/config')
        self.assertEqual(self.request('POST', '/api/login', {}, headers, server)[0], 426)
        self.assertEqual(server.auth_attempts, {})
        for scheme in ('ftp', 'https,http', 'HTTPS', ''):
            self.assertEqual(self.request(headers={**headers, 'X-Forwarded-Proto': scheme}, server=server)[0], 403)
        missing = f'GET /api/config HTTP/1.1\r\nHost: {HOST}\r\nCF-Connecting-IP: 192.0.2.1\r\n\r\n'
        self.assertTrue(self.raw(missing.encode(), server).startswith(b'HTTP/1.0 403'))
        duplicate = missing.replace('\r\n\r\n', '\r\nX-Forwarded-Proto: https\r\nX-Forwarded-Proto: https\r\n\r\n')
        self.assertTrue(self.raw(duplicate.encode(), server).startswith(b'HTTP/1.0 403'))
        status, body, response = self.request('HEAD', headers={'CF-Connecting-IP': '192.0.2.1'}, server=server)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'')
        self.assertGreater(int(response['Content-Length']), 0)

    def test_auth_limiter_memory_is_bounded_and_expired_entries_are_reclaimed(self):
        server = self.make_server(public_url=ORIGIN, trust_cloudflare=True)
        now = time.monotonic()
        server.auth_attempts = {f'entry-{i}': [now] for i in range(4096)}
        self.assertEqual(self.request('POST', '/api/login', {}, {'CF-Connecting-IP': '192.0.2.1'}, server)[0], 429)
        self.assertEqual(len(server.auth_attempts), 4096)
        server.auth_attempts = {key: [now - 61] for key in server.auth_attempts}
        self.assertEqual(self.request('POST', '/api/login', {}, {'CF-Connecting-IP': '192.0.2.1'}, server)[0], 403)
        self.assertEqual(set(server.auth_attempts), {'192.0.2.1'})

    def test_public_writes_stop_at_sixty_but_reads_and_authenticated_downloads_continue(self):
        session, cookie = self.register()
        auth = {'Cookie': cookie.split(';')[0], 'X-CSRF-Token': session['csrf_token']}
        status, package, _ = self.request('POST', '/api/packages?visibility=public', sample_pack(), auth)
        self.assertEqual(status, 201)
        for _ in range(59):
            self.assertEqual(self.request('POST', '/api/inspect', sample_pack(), auth)[0], 200)
        status, _, headers = self.request('POST', '/api/inspect', sample_pack(), auth)
        self.assertEqual(status, 429)
        self.assertGreaterEqual(int(headers['Retry-After']), 1)
        self.assertLessEqual(int(headers['Retry-After']), 60)
        self.assertEqual(self.request(path='/api/packages')[0], 200)
        self.assertEqual(self.request(path=f"/api/packages/{package['id']}")[0], 200)
        self.assertEqual(self.request(path=f"/api/packages/{package['id']}/download", headers=auth)[1], sample_pack())
        self.assertEqual(len(self.server.write_attempts[session['user']['id']]), 60)

    def test_write_limits_follow_account_across_ips_and_do_not_block_other_accounts(self):
        server = self.make_server(public_url=ORIGIN, trust_cloudflare=True)
        sessions = []
        for name in ('alice', 'bob'):
            status, session, headers = self.request('POST', '/api/register',
                {'username': name, 'password': 'test-password-123'}, {'CF-Connecting-IP': '192.0.2.1'}, server)
            self.assertEqual(status, 201)
            sessions.append((session, {'Cookie': headers['Set-Cookie'].split(';')[0],
                                       'X-CSRF-Token': session['csrf_token'], 'CF-Connecting-IP': '192.0.2.1'}))
        server.write_attempts[sessions[0][0]['user']['id']] = [time.monotonic()] * 60
        for address in ('192.0.2.1', '2001:db8::2'):
            auth = {**sessions[0][1], 'CF-Connecting-IP': address}
            self.assertEqual(self.request('POST', '/api/inspect', sample_pack(), auth, server)[0], 429)
        self.assertEqual(self.request('POST', '/api/inspect', sample_pack(), sessions[1][1], server)[0], 200)
        local = self.make_server()
        status, session, headers = self.request('POST', '/api/register',
            {'username': 'local', 'password': 'test-password-123'}, server=local)
        self.assertEqual(status, 201)
        local.write_attempts[session['user']['id']] = [time.monotonic()] * 60
        auth = {'Cookie': headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': session['csrf_token']}
        self.assertEqual(self.request('POST', '/api/inspect', sample_pack(), auth, local)[0], 200)

    def test_write_limiter_memory_is_bounded_and_expired_windows_recover(self):
        session, cookie = self.register()
        auth = {'Cookie': cookie.split(';')[0], 'X-CSRF-Token': session['csrf_token']}
        now = time.monotonic()
        self.server.write_attempts = {f'user-{i}': [now] for i in range(4096)}
        status, _, headers = self.request('POST', '/api/inspect', sample_pack(), auth)
        self.assertEqual(status, 429)
        self.assertEqual(headers['Retry-After'], '60')
        self.assertEqual(len(self.server.write_attempts), 4096)
        self.server.write_attempts = {key: [now - 61] for key in self.server.write_attempts}
        self.assertEqual(self.request('POST', '/api/inspect', sample_pack(), auth)[0], 200)
        self.assertEqual(set(self.server.write_attempts), {session['user']['id']})
        self.server.write_attempts[session['user']['id']] = [now - 61] * 60
        self.assertEqual(self.request('POST', '/api/inspect', sample_pack(), auth)[0], 200)
        self.assertEqual(len(self.server.write_attempts[session['user']['id']]), 1)

    def test_invalid_session_or_csrf_does_not_consume_write_capacity(self):
        session, cookie = self.register()
        auth = {'Cookie': cookie.split(';')[0], 'X-CSRF-Token': session['csrf_token']}
        self.assertEqual(self.request('POST', '/api/inspect', sample_pack())[0], 401)
        for _ in range(65):
            self.assertEqual(self.request('POST', '/api/inspect', sample_pack(),
                                          {**auth, 'X-CSRF-Token': 'invalid'})[0], 403)
        self.assertEqual(self.server.write_attempts, {})
        self.assertEqual(self.request('POST', '/api/inspect', sample_pack(), auth)[0], 200)
        self.assertEqual(len(self.server.write_attempts[session['user']['id']]), 1)

    def test_concurrent_connection_limit_fails_fast_and_releases_capacity(self):
        server = self.make_server(public_url=ORIGIN, max_connections=1)
        with socket.create_connection(server.server_address, timeout=5) as pending:
            pending.sendall(f'GET /api/config HTTP/1.1\r\nHost: {HOST}\r\n'.encode())
            self.wait_for_slots(server, 0)
            status, _, headers = self.request(server=server)
            self.assertEqual(status, 503)
            self.assertEqual(headers['Retry-After'], '1')
        self.wait_for_slots(server, 1)
        self.assertEqual(self.request(server=server)[0], 200)

    def test_idle_headers_and_incomplete_body_timeout_without_leaking_slots(self):
        server = self.make_server(public_url=ORIGIN, max_connections=1, request_timeout=0.15)
        with socket.create_connection(server.server_address, timeout=5) as pending:
            pending.sendall(b'GET /api/config HTTP/1.1\r\n')
            self.assertEqual(pending.recv(4096), b'')
        self.wait_for_slots(server, 1)
        response = self.raw(f'POST /api/login HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 10\r\n\r\n{{'.encode(), server)
        self.assertTrue(response.startswith(b'HTTP/1.0 504'), response)
        self.wait_for_slots(server, 1)
        self.assertEqual(self.request(server=server)[0], 200)

    def assert_slow_request_deadline(self, prefix):
        server = self.make_server(public_url=ORIGIN, max_connections=1,
                                  request_timeout=0.15, request_deadline=0.35)
        stop = threading.Event()
        with socket.create_connection(server.server_address, timeout=3) as pending:
            started = time.monotonic()
            pending.sendall(prefix)

            def drip():
                while not stop.wait(0.05):
                    try:
                        pending.sendall(b'x')
                    except OSError:
                        return

            sender = threading.Thread(target=drip, daemon=True)
            with patch.object(server, 'handle_error') as errors:
                sender.start()
                try:
                    # Bytes arrive faster than the idle timeout, so only the
                    # independent deadline can terminate this connection.
                    while pending.recv(4096):
                        pass
                finally:
                    stop.set()
                    sender.join()
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 0.25)
                self.assertLess(elapsed, 1.5)
                self.wait_for_slots(server, 1)
                errors.assert_not_called()
        self.assertEqual(self.request(server=server)[0], 200)

    def test_total_deadline_terminates_continuously_dripping_headers(self):
        self.assert_slow_request_deadline(f'GET /api/config HTTP/1.1\r\nHost: {HOST}\r\nX-Slow: '.encode())

    def test_total_deadline_terminates_continuously_dripping_body(self):
        self.assert_slow_request_deadline(f'POST /api/login HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 1000\r\n\r\n{{'.encode())

    def test_local_mode_keeps_existing_cookie_and_has_no_hsts(self):
        server = self.make_server()
        status, _, headers = self.request('POST', '/api/register',
                                           {'username': 'local', 'password': 'test-password-123'}, server=server)
        self.assertEqual(status, 201)
        cookie = SimpleCookie(headers['Set-Cookie'])
        self.assertFalse(cookie['ch_session']['secure'])
        self.assertNotIn('Strict-Transport-Security', headers)

    def test_storage_budget_parameters_and_capacity_errors_map_to_507(self):
        server = self.make_server(public_url=ORIGIN, max_storage_mib=8, min_free_mib=1)
        self.assertEqual(server.store.budget.max_storage_bytes, 8 * 1024 * 1024)
        self.assertEqual(server.store.budget.min_free_bytes, 1024 * 1024)
        with patch.object(server.store, 'register', side_effect=StoreCapacityError('存储预算已满')):
            status, body, _ = self.request('POST', '/api/register',
                                           {'username': 'newuser', 'password': 'test-password-123'}, server=server)
            self.assertEqual(status, 507)
            self.assertEqual(body['error'], '存储预算已满')
        self.assertEqual(self.request(path='/api/packages', server=server)[0], 200)

    def test_cli_passes_explicit_deployment_limits_and_uses_public_browser_url(self):
        args = parser().parse_args(['serve', '--public-url', ORIGIN, '--max-connections', '4',
                                    '--max-body-mib', '16', '--trust-cloudflare', '--max-storage-mib', '5120', '--min-free-mib', '5120',
                                    '--request-deadline', '90'])
        self.assertEqual(args.max_connections, 4)
        with patch('contexthub.server.create_server') as factory, contextlib.redirect_stdout(io.StringIO()) as output:
            factory.return_value.public_origin = ORIGIN
            code = main(['serve', '--public-url', ORIGIN, '--max-connections', '4',
                         '--max-body-mib', '16', '--trust-cloudflare', '--max-storage-mib', '5120', '--min-free-mib', '5120',
                         '--request-deadline', '90'])
            self.assertEqual(code, 0)
            self.assertEqual(factory.call_args.kwargs['public_url'], ORIGIN)
            self.assertEqual(factory.call_args.kwargs['max_connections'], 4)
            self.assertEqual(factory.call_args.kwargs['max_body_mib'], 16)
            self.assertTrue(factory.call_args.kwargs['trust_cloudflare'])
            self.assertEqual(factory.call_args.kwargs['max_storage_mib'], 5120)
            self.assertEqual(factory.call_args.kwargs['min_free_mib'], 5120)
            self.assertEqual(factory.call_args.kwargs['request_deadline'], 90)
            self.assertIn(ORIGIN, output.getvalue())


if __name__ == '__main__':
    unittest.main()
