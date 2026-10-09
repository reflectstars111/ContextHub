"""Exercise portable installer operations in PowerShell on non-Windows hosts.

This is supplementary validation, not a replacement for test_windows_installer:
setting OS here only passes the installer's platform guard. It does not provide
Windows PE launchers, native registry PATH updates, locks, or Windows paths.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


POWERSHELL = os.environ.get('CONTEXTHUB_TEST_POWERSHELL') or shutil.which('pwsh')
SCRIPT = Path(__file__).resolve().parents[1] / 'contexthub' / 'static' / 'install-ctx.ps1'


@unittest.skipUnless(os.name != 'nt' and POWERSHELL, 'requires optional non-Windows PowerShell runtime')
class InstallerPortableCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ctx-installer-core-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / 'fixture.pyz'
        self.directory = self.root / '\u4e2d\u6587 space' / 'bin'
        self.make_archive('ContextHub 0.6.0')

    def make_archive(self, output, *, launcher_mode='normal'):
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr('__main__.py', f'print({output!r})')
            archive.writestr('contexthub/__init__.py', '')
            # Only the native helper is replaced: actual PowerShell orchestration,
            # checksums, version checks and atomic replacement still run here.
            helper = 'MODE = ' + repr(launcher_mode) + '\n' + '''from pathlib import Path
import shlex
import sys

def build_launcher(output):
    if MODE == 'raise':
        raise RuntimeError('No matching local launcher stub is available')
    target = Path(output)
    script = '#!/bin/sh\\nexec ' + shlex.quote(sys.executable) + ' "$(dirname "$0")/ctx.pyz" "$@"\\n'
    if MODE == 'wrong_version':
        script = "#!/bin/sh\\necho 'ContextHub 99.0.0'\\n"
    target.write_text(script, encoding='utf-8')
    target.chmod(0o755)
    return target
'''
            if launcher_mode != 'missing':
                archive.writestr('contexthub/windows_launcher.py', helper)
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def install(self, *, digest=None, hub='https://example.test', directory=None):
        return subprocess.run(
            [POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(SCRIPT),
             '-ArchivePath', str(self.archive), '-Sha256', digest or self.digest,
             '-InstallDir', str(directory or self.directory), '-PythonExecutable', sys.executable,
             '-NoPathUpdate', '-HubUrl', hub],
            env={**os.environ, 'OS': 'Windows_NT'}, capture_output=True, text=True, timeout=60)

    def test_install_and_update_execute_verified_archive(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.directory / 'ctx.pyz').read_bytes(), self.archive.read_bytes())
        self.assertTrue((self.directory / 'ctx.exe').is_file())
        self.make_archive('ContextHub 0.6.1')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(subprocess.check_output([sys.executable, str(self.directory / 'ctx.pyz'), '--version'],
                                                text=True).strip(), 'ContextHub 0.6.1')
        self.assertEqual(subprocess.check_output([str(self.directory / 'ctx.exe'), '--version'],
                                                text=True).strip(), 'ContextHub 0.6.1')
        self.assertFalse(list(self.directory.glob('.ctx-install-*')))

    def test_hash_or_version_failure_does_not_replace_existing_files(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        previous = {name: (self.directory / name).read_bytes() for name in ('ctx.pyz', 'ctx.exe')}
        self.make_archive('ContextHub 0.6.1')
        self.assertNotEqual(self.install(digest='0' * 64).returncode, 0)
        for name, data in previous.items():
            self.assertEqual((self.directory / name).read_bytes(), data)
        self.make_archive('not a ContextHub version')
        self.assertNotEqual(self.install().returncode, 0)
        for name, data in previous.items():
            self.assertEqual((self.directory / name).read_bytes(), data)

    def test_invalid_origins_are_rejected_before_installation(self):
        for hub in ['http://example.test', 'https://@example.test', 'https://user:pw@example.test',
                    'https://example.test/path', 'https://example.test?key=x',
                    'https://example.test#part', 'https://example.test\\@evil.test',
                    'https://example.test:0', 'https://example.test\n']:
            with self.subTest(hub=hub):
                self.assertNotEqual(self.install(hub=hub).returncode, 0)
                self.assertFalse(self.directory.exists())

    def test_launcher_build_or_execution_failure_preserves_installed_files(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        previous = {name: (self.directory / name).read_bytes() for name in ('ctx.pyz', 'ctx.exe')}
        for mode in ('missing', 'raise', 'wrong_version'):
            with self.subTest(launcher_failure=mode):
                self.make_archive('ContextHub 0.6.1', launcher_mode=mode)
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                for name, data in previous.items():
                    self.assertEqual((self.directory / name).read_bytes(), data)
                self.assertEqual(subprocess.check_output([str(self.directory / 'ctx.exe'), '--version'],
                                                        text=True).strip(), 'ContextHub 0.6.0')

    def test_symlink_install_directory_cannot_redirect_file_writes(self):
        outside = self.root / 'outside'
        outside.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.install(directory=alias / 'bin').returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_relative_paths_follow_powershell_location(self):
        driver = self.root / 'relative.ps1'
        driver.write_text('''$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $env:CTX_TEST_ROOT
& $env:CTX_TEST_SCRIPT -ArchivePath ./fixture.pyz -Sha256 $env:CTX_TEST_SHA -InstallDir ./relative-bin -PythonExecutable $env:CTX_TEST_PYTHON -NoPathUpdate
''', encoding='ascii')
        result = subprocess.run(
            [POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(driver)],
            cwd=self.root.parent, env={**os.environ, 'OS': 'Windows_NT', 'CTX_TEST_ROOT': str(self.root),
                                      'CTX_TEST_SCRIPT': str(SCRIPT), 'CTX_TEST_SHA': self.digest,
                                      'CTX_TEST_PYTHON': sys.executable},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'relative-bin' / 'ctx.pyz').read_bytes(), self.archive.read_bytes())

    def test_legacy_native_arguments_preserve_python_probe_and_unicode_paths(self):
        interpreter = self.root / '\u4e2d\u6587 Python space' / 'python3'
        interpreter.parent.mkdir()
        interpreter.symlink_to(sys.executable)
        driver = self.root / 'legacy-arguments.ps1'
        driver.write_text('''$ErrorActionPreference = 'Stop'
$PSNativeCommandArgumentPassing = 'Legacy'
& $env:CTX_TEST_SCRIPT -ArchivePath $env:CTX_TEST_ARCHIVE -Sha256 $env:CTX_TEST_SHA -InstallDir $env:CTX_TEST_DIR -PythonExecutable $env:CTX_TEST_PYTHON -NoPathUpdate
''', encoding='ascii')
        result = subprocess.run(
            [POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(driver)],
            env={**os.environ, 'OS': 'Windows_NT', 'CTX_TEST_SCRIPT': str(SCRIPT),
                 'CTX_TEST_ARCHIVE': str(self.archive), 'CTX_TEST_SHA': self.digest,
                 'CTX_TEST_DIR': str(self.directory), 'CTX_TEST_PYTHON': str(interpreter)},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.directory / 'ctx.pyz').read_bytes(), self.archive.read_bytes())
        self.assertEqual(subprocess.check_output([str(interpreter), str(self.directory / 'ctx.pyz'), '--version'],
                                                text=True).strip(), 'ContextHub 0.6.0')


if __name__ == '__main__':
    unittest.main()
