import http.client
import json
import tempfile
import threading
import unittest
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from contexthub.package import build_package
from contexthub.server import create_server


def sample_pack():
    return build_package([{'id': 'sample', 'title': '中文对话', 'messages': [
        {'id': 'm1', 'role': 'user', 'content': '如何保存上下文？'},
        {'id': 'm2', 'role': 'assistant', 'content': '保存消息与来源。\n```python\nprint(1)\n```'},
    ]}], '测试对话库')


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = create_server(Path(self.tmp.name), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ''
        self.csrf = ''

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, data=None, auth=True, headers=None):
        body = json.dumps(data).encode() if isinstance(data, dict) else data
        h = {'Content-Type': 'application/json' if isinstance(data, dict) else 'application/octet-stream'}
        if auth:
            h.update({'Cookie': self.cookie, 'X-CSRF-Token': self.csrf})
        h.update(headers or {})
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        conn.request(method, quote(path, safe='/?=&'), body, h)
        response = conn.getresponse()
        raw = response.read()
        out = json.loads(raw) if 'application/json' in response.getheader('Content-Type', '') else raw
        cookie = response.getheader('Set-Cookie')
        if cookie and auth:
            self.cookie = cookie.split(';')[0]
        conn.close()
        return response.status, out

    def register(self, name='alice'):
        status, result = self.request('POST', '/api/register', {'username': name, 'password': 'a-test-password-123'})
        self.assertEqual(status, 201, result)
        self.csrf = result['csrf_token']
        return result

    def test_private_package_cannot_be_read_listed_or_downloaded_anonymously(self):
        self.register()
        status, item = self.request('POST', '/api/packages?visibility=private', sample_pack())
        self.assertEqual(status, 201, item)
        ident = item['id']
        self.assertEqual(self.request('GET', '/api/packages', auth=False)[1]['packages'], [])
        for suffix in ['', '/download']:
            self.assertEqual(self.request('GET', f'/api/packages/{ident}{suffix}', auth=False)[0], 404)
        self.assertEqual(self.request('GET', f'/api/packages/{ident}/download')[1], sample_pack())
        self.register('bob')
        self.assertEqual(self.request('GET', '/api/packages')[1]['packages'], [])
        for suffix in ['', '/download']:
            self.assertEqual(self.request('GET', f'/api/packages/{ident}{suffix}')[0], 404)
        self.assertEqual(self.request('DELETE', f'/api/packages/{ident}')[0], 404)

    def test_public_package_round_trip_git_snapshot_and_other_user_acl(self):
        self.register()
        pack = sample_pack()
        status, item = self.request('POST', '/api/packages?visibility=public', pack)
        self.assertEqual(status, 201, item)
        self.assertTrue(item['git_commit'])
        self.assertEqual(self.request('GET', f"/api/packages/{item['id']}/download", auth=False)[1], pack)
        self.assertEqual(len(self.request('GET', '/api/packages?q=中文', auth=False)[1]['packages']), 1)
        self.register('bob')
        self.assertEqual(self.request('DELETE', f"/api/packages/{item['id']}")[0], 404)

    def test_preview_is_not_published_and_duplicate_upload_is_idempotent(self):
        self.register()
        status, preview = self.request('POST', '/api/inspect', sample_pack())
        self.assertEqual(status, 200)
        self.assertEqual(preview['conversations'][0]['title'], '中文对话')
        self.assertEqual(self.request('GET', '/api/packages')[1]['packages'], [])
        first = self.request('POST', '/api/packages', sample_pack())[1]
        second = self.request('POST', '/api/packages', sample_pack())[1]
        self.assertEqual(first['id'], second['id'])

    def test_csrf_origin_and_invalid_upload_rejected(self):
        self.register()
        self.assertEqual(self.request('POST', '/api/packages', sample_pack(), headers={'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('POST', '/api/packages', sample_pack(), headers={'Origin': 'https://evil.invalid'})[0], 403)
        self.assertEqual(self.request('POST', '/api/packages', b'not a zip')[0], 422)
        self.assertEqual(self.request('POST', '/api/packages?visibility=anything', sample_pack())[0], 422)

    def test_logout_revokes_session_and_private_metadata_survives_restart(self):
        self.register()
        item = self.request('POST', '/api/packages', sample_pack())[1]
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = create_server(Path(self.tmp.name), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.request('GET', f"/api/packages/{item['id']}")[0], 200)
        old_cookie, old_csrf = self.cookie, self.csrf
        self.assertEqual(self.request('POST', '/api/logout', {})[0], 200)
        self.assertIsNone(self.request('GET', '/api/session', headers={'Cookie': old_cookie, 'X-CSRF-Token': old_csrf})[1]['user'])

    def test_hub_cannot_access_local_codex(self):
        self.assertEqual(self.request('GET', '/api/local/sessions')[0], 404)

    def test_cli_upload_download_with_password_stdin(self):
        self.register()
        source = Path(self.tmp.name) / 'source.contextpack'
        dest = Path(self.tmp.name) / 'downloaded.contextpack'
        source.write_bytes(sample_pack())
        url = f'http://127.0.0.1:{self.server.server_port}'
        result = subprocess.run([sys.executable, '-m', 'contexthub', 'upload', str(source),
                                 '--url', url, '--username', 'alice', '--password-stdin'],
                                input='a-test-password-123\n', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout)
        self.assertEqual(item['visibility'], 'private')
        download = f"{url}/api/packages/{item['id']}/download"
        denied = subprocess.run([sys.executable, '-m', 'contexthub', 'download', download, '-o', str(dest)], capture_output=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse(dest.exists())
        result = subprocess.run([sys.executable, '-m', 'contexthub', 'download', download,
                                 '-o', str(dest), '--username', 'alice', '--password-stdin'],
                                input='a-test-password-123\n', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dest.read_bytes(), source.read_bytes())

    def test_connector_requires_token(self):
        other = create_server(Path(self.tmp.name) / 'connector', port=0, mode='connector', token='test-connector-token')
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(*other.server_address)
            conn.request('POST', '/api/local/inspect', sample_pack(), {'X-ContextHub-Token': 'bad'})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.close()
            conn = http.client.HTTPConnection(*other.server_address)
            conn.request('POST', '/api/local/inspect', sample_pack(), {'X-ContextHub-Token': 'test-connector-token'})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn('conversations', json.loads(response.read()))
            conn.close()
        finally:
            other.shutdown()
            other.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
