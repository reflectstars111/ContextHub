import json
import subprocess
import sys
import tempfile
import unittest
import contextlib
import io
import threading
from unittest.mock import patch
from pathlib import Path


class CLITests(unittest.TestCase):
    def test_transfer_requests_identify_contexthub_and_preserve_cookie_csrf(self):
        from contexthub import __version__
        from contexthub.__main__ import demo
        from contexthub.server import Handler, create_server

        expected_agent = f'ContextHub/{__version__}'
        received = []

        class ProductAgentHandler(Handler):
            def _handle(self, method):
                received.append((method, self.path, self.headers.get('User-Agent'),
                                 self.headers.get('Cookie'), self.headers.get('X-CSRF-Token')))
                if self.headers.get('User-Agent') != expected_agent:
                    return self._send(403, {'error': 'Please identify the product client'})
                return super()._handle(method)

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source.contextpack'
            source.write_bytes(demo())
            server = create_server(Path(temp) / 'hub', port=0)
            server.RequestHandlerClass = ProductAgentHandler
            server.store.register('cliuser', 'test-password-123')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f'http://127.0.0.1:{server.server_port}'
            try:
                upload = subprocess.run([sys.executable, '-m', 'contexthub', 'upload', str(source),
                                         '--url', url, '--username', 'cliuser', '--password-stdin'],
                                        input='test-password-123\n', capture_output=True, text=True, timeout=15)
                self.assertEqual(upload.returncode, 0, upload.stderr)
                item = json.loads(upload.stdout)
                self.assertEqual(item['visibility'], 'private')
                download_url = f"{url}/api/packages/{item['id']}/download"
                target = Path(temp) / 'download.contextpack'
                download = subprocess.run([sys.executable, '-m', 'contexthub', 'download', download_url,
                                           '--username', 'cliuser', '--password-stdin', '-o', str(target)],
                                          input='test-password-123\n', capture_output=True, text=True, timeout=15)
                self.assertEqual(download.returncode, 0, download.stderr)
                self.assertEqual(target.read_bytes(), source.read_bytes())
                anonymous_target = Path(temp) / 'anonymous.contextpack'
                anonymous = subprocess.run([sys.executable, '-m', 'contexthub', 'download', download_url,
                                            '-o', str(anonymous_target)], capture_output=True, text=True, timeout=15)
                self.assertNotEqual(anonymous.returncode, 0)
                self.assertIn('HTTP 401:', anonymous.stderr)
                self.assertFalse(anonymous_target.exists())
                self.assertEqual([(method, path.split('?')[0]) for method, path, *_ in received], [
                    ('POST', '/api/login'), ('POST', '/api/packages'), ('POST', '/api/login'),
                    ('GET', f"/api/packages/{item['id']}/download"),
                    ('GET', f"/api/packages/{item['id']}/download")])
                self.assertTrue(all(record[2] == expected_agent for record in received))
                self.assertIn('ch_session=', received[1][3])
                self.assertTrue(received[1][4])  # Real Hub rejects upload without matching CSRF.
                self.assertIn('ch_session=', received[3][3])
                self.assertIsNone(received[4][3])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_failed_import_returns_nonzero_and_keeps_each_receipt(self):
        from contexthub.__main__ import demo, main
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sample.contextpack'
            path.write_bytes(demo())
            output = io.StringIO()
            with patch('contexthub.codex.CodexClient'), patch('contexthub.codex.import_conversation', side_effect=[
                {'thread_id': 'partial', 'mode': 'failed', 'injected': False, 'warnings': ['injection failed']},
                RuntimeError('upstream unavailable'),
            ]), contextlib.redirect_stdout(output):
                code = main(['import', str(path), '--cwd', temp])
            self.assertEqual(code, 1)
            results = json.loads(output.getvalue())['results']
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]['mode'], 'failed')
            self.assertIn('error', results[1])

    def test_demo_inspect_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sample.contextpack'
            result = subprocess.run([sys.executable, '-m', 'contexthub', 'demo', '--output', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, '-m', 'contexthub', 'inspect', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            inspected = json.loads(result.stdout)
            self.assertEqual(inspected['conversation_count'], 2)
            self.assertIn('演示', inspected['title'])
            original = path.read_bytes()
            result = subprocess.run([sys.executable, '-m', 'contexthub', 'demo', '--output', str(path)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_bytes(), original)

    def test_import_dry_run_does_not_need_codex_or_change_package(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sample.contextpack'
            subprocess.run([sys.executable, '-m', 'contexthub', 'demo', '-o', str(path)], check=True, capture_output=True)
            original = path.read_bytes()
            result = subprocess.run([sys.executable, '-m', 'contexthub', 'import', str(path), '--dry-run'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['dry_run'])
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
