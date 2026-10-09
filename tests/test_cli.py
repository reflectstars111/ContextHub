import json
import subprocess
import sys
import tempfile
import unittest
import contextlib
import io
from unittest.mock import patch
from pathlib import Path


class CLITests(unittest.TestCase):
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
