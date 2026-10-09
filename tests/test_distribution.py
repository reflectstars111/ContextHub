"""Installable CLI archive contains only its explicit, public source modules."""
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from contexthub import __version__
from contexthub.distribution import MODULES, cli_archive, cli_checksum
from tests import test_server as helpers


class DistributionTests(unittest.TestCase):
    def test_archive_is_deterministic_allowlisted_source_and_runs_outside_project(self):
        blob = cli_archive()
        cli_archive.cache_clear()
        self.assertEqual(blob, cli_archive())
        self.assertEqual(cli_checksum(), hashlib.sha256(blob).hexdigest() + '  ctx.pyz\n')
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            self.assertEqual(set(archive.namelist()), {'__main__.py', 'LICENSE'} |
                             {f'contexthub/{name}.py' for name in MODULES})
            self.assertIn(b'MIT License', archive.read('LICENSE'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ctx.pyz'
            path.write_bytes(blob)
            if sys.platform != 'win32':
                path.chmod(0o755)
            command = [sys.executable, str(path)] if sys.platform == 'win32' else [str(path)]
            result = subprocess.run([*command, '--version'], cwd=directory, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(__version__, result.stdout)
            pack = Path(directory) / 'sample.contextpack'
            pack.write_bytes(helpers.sample_pack())
            result = subprocess.run([sys.executable, str(path), 'codex', 'import', str(pack), '--dry-run'],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, str(path), 'init', 'local', '--title', 'Zip app test'],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(directory) / 'local' / '.contexthub' / 'state.json').is_file())


class DistributionHTTPTests(unittest.TestCase):
    setUp = helpers.ServerTests.setUp
    tearDown = helpers.ServerTests.tearDown
    request = helpers.ServerTests.request

    def test_public_install_guide_archive_and_checksum(self):
        for path in ('/cli', '/cli.css', '/install-ctx.ps1'):
            status, body = self.request('GET', path, auth=False)
            self.assertEqual(status, 200)
            self.assertTrue(body)
        self.assertEqual(self.request('GET', '/ctx.pyz', auth=False), (200, cli_archive()))
        self.assertEqual(self.request('GET', '/ctx.pyz.sha256', auth=False), (200, cli_checksum().encode()))
        self.assertEqual(self.request('GET', '/contexthub/hub_client.py', auth=False)[0], 404)
        self.assertEqual(self.request('GET', '/.contexthub/state.json', auth=False)[0], 404)


if __name__ == '__main__':
    unittest.main()
