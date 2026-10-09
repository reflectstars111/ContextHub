import importlib.util
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from contexthub.package import build_package
from contexthub.server import create_server


def client_module():
    assert importlib.util.find_spec('contexthub.hub_client') is not None, 'Safe Hub client is not implemented'
    from contexthub import hub_client
    return hub_client


class Fixture:
    """A real HTTP peer for redirect, wire format, and response-limit tests."""
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.handle_request()

            do_POST = do_GET

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                fixture.requests.append((self.command, self.path, dict(self.headers), body))
                status, headers, value = fixture.routes.get(urlsplit(self.path).path, (200, {}, {'packages': []}))
                raw = json.dumps(value).encode() if isinstance(value, dict) else value
                self.send_response(status)
                self.send_header('Content-Type', 'application/json' if isinstance(value, dict) else 'application/octet-stream')
                self.send_header('Content-Length', str(len(raw)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class HubClientTests(unittest.TestCase):
    def setUp(self):
        self.client = client_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.credentials = self.client.Credentials(Path(self.tmp.name) / 'config' / 'credentials.json')

    def peer(self, routes=None):
        fixture = Fixture(routes)
        self.addCleanup(fixture.close)
        return fixture

    def real_hub(self):
        server = create_server(Path(self.tmp.name) / 'data', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(close)
        server.store.register('alice', 'synthetic-password-123')
        return server, self.client.HubClient(f'http://127.0.0.1:{server.server_port}', self.credentials)

    def record(self, **updates):
        return {'cookie_name': 'ch_session', 'token': 's' * 43, 'csrf_token': 'c' * 43,
                'user': {'id': 'uid', 'username': 'alice'}, 'expires_at': time.time() + 3600, **updates}

    def test_origin_rejects_plaintext_remote_and_ambiguous_urls(self):
        for bad in ['http://example.com', 'http://localhost:8080', 'http://127.1',
                    'https://alice:pw@example.com', 'https://example.com/path',
                    'https://example.com?key=1', 'https://example.com#x',
                    'https://example.com\\@evil.test', 'https://example.com\n',
                    'https://example.com:0', 'https://[::1%25eth0]', '//example.com']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.client.normalize_hub(bad)
        self.assertEqual(self.client.normalize_hub('HTTPS://EXAMPLE.COM:443/'), 'https://example.com')
        self.assertEqual(self.client.normalize_hub('http://[::1]:8765/'), 'http://[::1]:8765')

    def test_login_persists_only_private_session_and_logout_revokes_it(self):
        server, hub = self.real_hub()
        result = hub.login('alice', 'synthetic-password-123')
        self.assertEqual(result['user']['username'], 'alice')
        self.assertNotIn('csrf_token', result)
        path = self.credentials.path
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertNotIn(b'synthetic-password', path.read_bytes())
        record = self.credentials.get(hub.origin)
        self.assertEqual(server.store.session(record['token'])['user']['username'], 'alice')
        resumed = self.client.HubClient(hub.origin, self.client.Credentials(path))
        self.assertEqual(resumed.session()['user']['username'], 'alice')
        self.assertNotIn('csrf_token', resumed.session())
        resumed.logout()
        self.assertIsNone(self.credentials.get(hub.origin))
        self.assertIsNone(server.store.session(record['token'])['user'])

    def test_anon_reads_and_authenticated_exact_download(self):
        _, hub = self.real_hub()
        hub.login('alice', 'synthetic-password-123')
        blob = build_package([{'id': 'c', 'title': 'Synthetic', 'messages': [
            {'id': 'm', 'role': 'user', 'content': 'sample'}]}], 'Client test')
        item = hub.upload(blob, visibility='public')
        self.assertEqual(hub.download(item['id'], item['head_version_id']), blob)
        anonymous = self.client.HubClient(hub.origin, self.client.Credentials(Path(self.tmp.name) / 'anon' / 'credentials.json'))
        self.assertEqual(anonymous.detail(item['id'])['id'], item['id'])
        self.assertEqual(len(anonymous.list_packages()['packages']), 1)
        with self.assertRaises(self.client.HubClientError) as exc:
            anonymous.download(item['id'])
        self.assertEqual(exc.exception.status, 401)
        self.assertIn('ctx auth login', str(exc.exception))

    def test_expired_server_session_is_removed_and_requires_login(self):
        server, hub = self.real_hub()
        hub.login('alice', 'synthetic-password-123')
        server.store.logout(self.credentials.get(hub.origin)['token'])
        with self.assertRaises(self.client.HubClientError) as exc:
            hub.download('nonexistent')
        self.assertEqual(exc.exception.status, 401)
        self.assertIsNone(self.credentials.get(hub.origin))
        self.assertIsNone(hub.session()['user'])

    def test_credentials_are_exact_origin_scoped(self):
        source = self.peer()
        target = self.peer()
        self.credentials.set(source.origin, self.record())
        self.client.HubClient(target.origin, self.credentials).list_packages()
        self.assertNotIn('Cookie', target.requests[0][2])
        self.assertNotIn('X-Csrf-Token', target.requests[0][2])
        with self.assertRaises(self.client.HubClientError):
            self.client.HubClient(target.origin, self.credentials).upload(b'secret')
        self.assertEqual(len(target.requests), 1)

    def test_redirect_never_forwards_cookie_csrf_or_upload(self):
        target = self.peer()
        source = self.peer({'/api/packages': (307, {'Location': target.origin + '/api/packages'}, b'')})
        self.credentials.set(source.origin, self.record())
        with self.assertRaises(self.client.HubClientError) as exc:
            self.client.HubClient(source.origin, self.credentials).upload(b'private conversation')
        self.assertEqual(exc.exception.status, 307)
        self.assertEqual(len(target.requests), 0)
        self.assertEqual(source.requests[0][3], b'private conversation')
        source.routes['/api/packages'] = (302, {'Location': source.origin + '/another-path'}, b'')
        with self.assertRaises(self.client.HubClientError):
            self.client.HubClient(source.origin, self.credentials).list_packages()
        self.assertEqual(len(source.requests), 2)

    def test_login_redirect_does_not_forward_password(self):
        target = self.peer()
        source = self.peer({'/api/login': (307, {'Location': target.origin + '/api/login'}, b'')})
        with self.assertRaises(self.client.HubClientError):
            self.client.HubClient(source.origin, self.credentials).login('alice', 'secret-password')
        self.assertEqual(target.requests, [])
        self.assertIsNone(self.credentials.get(source.origin))

    def test_push_cas_wire_data_and_conflict_message(self):
        peer = self.peer({'/api/packages/pkg/push': (409, {}, {'error': '远端已更新'})})
        self.credentials.set(peer.origin, self.record())
        with self.assertRaises(self.client.HubClientError) as exc:
            self.client.HubClient(peer.origin, self.credentials).push('pkg', b'pack', 'base-1', '保存 & 中文')
        self.assertEqual(exc.exception.status, 409)
        self.assertIn('远端已更新', str(exc.exception))
        method, url, headers, data = peer.requests[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(parse_qs(urlsplit(url).query), {'base_version': ['base-1'], 'message': ['保存 & 中文']})
        self.assertEqual(headers['Cookie'], 'ch_session=' + 's' * 43)
        self.assertEqual(headers['X-Csrf-Token'], 'c' * 43)
        self.assertEqual(headers['Origin'], peer.origin)
        self.assertEqual(data, b'pack')

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions; native Windows path checks have separate tests')
    def test_credentials_reject_symlinks_and_world_readable_files(self):
        peer = self.peer()
        self.credentials.set(peer.origin, self.record())
        self.credentials.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.credentials.get(peer.origin)
        self.credentials.path.chmod(0o600)
        link = self.credentials.path.parent / 'linked.json'
        link.symlink_to(self.credentials.path)
        with self.assertRaises(ValueError):
            self.client.Credentials(link).get(peer.origin)
        alias = Path(self.tmp.name) / 'alias'
        alias.symlink_to(self.credentials.path.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.client.Credentials(alias / 'credentials.json').get(peer.origin)

    def test_credentials_path_environment_and_local_expiry(self):
        custom = Path(self.tmp.name) / 'custom'
        with patch.dict(os.environ, {'CONTEXTHUB_CONFIG_DIR': str(custom)}):
            self.assertEqual(self.client.Credentials().path, custom / (
                'credentials.dpapi' if os.name == 'nt' else 'credentials.json'))
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(custom), 'LOCALAPPDATA': str(custom)}):
            with patch.dict(os.environ, {'CONTEXTHUB_CONFIG_DIR': ''}):
                expected = custom / 'ContextHub' / 'credentials.dpapi' if os.name == 'nt' else custom / 'contexthub' / 'credentials.json'
                self.assertEqual(self.client.Credentials().path, expected)
        origin = 'https://example.com'
        self.credentials.set(origin, self.record(expires_at=time.time() - 1))
        self.assertIsNone(self.credentials.get(origin))

    def test_response_size_and_non_json_errors_do_not_dump_body(self):
        peer = self.peer({'/api/packages': (200, {}, {'packages': ['x' * 100]})})
        hub = self.client.HubClient(peer.origin, self.credentials, max_json_bytes=64)
        with self.assertRaises(self.client.HubClientError):
            hub.list_packages()
        peer.routes['/api/packages'] = (502, {}, b'<html>proxy-secret</html>')
        with self.assertRaises(self.client.HubClientError) as exc:
            hub.list_packages()
        self.assertNotIn('proxy-secret', str(exc.exception))

    def test_rejects_cookie_scope_and_header_injection(self):
        body = {'user': {'id': 'uid', 'username': 'alice'}, 'csrf_token': 'c' * 43}
        peer = self.peer({'/api/login': (200, {'Set-Cookie': 'ch_session=' + 's' * 43 + '; Domain=evil.test; Path=/; Max-Age=3600'}, body)})
        with self.assertRaises(self.client.HubClientError):
            self.client.HubClient(peer.origin, self.credentials).login('alice', 'password')
        peer.routes['/api/login'] = (200, {'Set-Cookie': 'ch_session=x; invalid@=a'}, body)
        with self.assertRaises(self.client.HubClientError):
            self.client.HubClient(peer.origin, self.credentials).login('alice', 'password')
        with self.assertRaises(ValueError):
            self.credentials.set(peer.origin, self.record(token='x\r\nAuthorization: leak'))

    def test_logout_removes_expired_credentials_without_a_request(self):
        peer = self.peer()
        self.credentials.set(peer.origin, self.record(expires_at=time.time() - 10))
        self.client.HubClient(peer.origin, self.credentials).logout()
        with self.credentials._locked() as directory:
            self.assertNotIn(peer.origin, self.credentials._load(directory)['hubs'])
        self.assertEqual(peer.requests, [])

    def test_server_error_does_not_echo_login_password_or_terminal_escapes(self):
        peer = self.peer({'/api/login': (403, {}, {'error': 'bad password secret-password\x1b[31m'})})
        with self.assertRaises(self.client.HubClientError) as exc:
            self.client.HubClient(peer.origin, self.credentials).login('alice', 'secret-password')
        self.assertNotIn('secret-password', str(exc.exception))
        self.assertNotIn('\x1b', str(exc.exception))
        peer.routes['/api/login'] = (403, {}, {'error': 'x' * 490 + 'secret-password'})
        with self.assertRaises(self.client.HubClientError) as exc:
            self.client.HubClient(peer.origin, self.credentials).login('alice', 'secret-password')
        self.assertNotIn('secret', str(exc.exception))

    def test_user_fields_cannot_inject_terminal_output(self):
        with self.assertRaises(ValueError):
            self.credentials.set('https://example.com', self.record(user={'id': 'u', 'username': 'alice\x1b[0m'}))


if __name__ == '__main__':
    unittest.main()
