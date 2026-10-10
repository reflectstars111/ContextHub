"""Run the actual PowerShell installer and native launcher on Windows.

All packages are synthetic local zipapps. These tests never download executable
content, touch Codex histories, or require an administrator account.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'contexthub' / 'static' / 'install-ctx.ps1'
POWERSHELL = shutil.which('powershell.exe')


@unittest.skipUnless(os.name == 'nt' and POWERSHELL, 'requires native Windows PowerShell 5.1')
class WindowsInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ctx-installer-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / '\u4e2d\u6587 space' / 'ContextHub' / 'bin'
        self.archive = self.root / 'fixture.pyz'
        self.make_archive('0.6.0')

    def make_archive(self, version, valid=True):
        code = (
            'import json, sys\n'
            'if sys.argv[1:] == ["--version"]:\n'
            f'    print({("ContextHub " + version) if valid else "invalid package"!r})\n'
            'else:\n'
            '    sys.stdout.buffer.write(json.dumps(sys.argv[1:], ensure_ascii=False).encode("utf-8"))\n'
        )
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr('__main__.py', code)
            archive.writestr('contexthub/__init__.py', '')
            archive.writestr('contexthub/windows_launcher.py',
                             (SCRIPT.parents[1] / 'windows_launcher.py').read_bytes())
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def install(self, *, digest=None, hub=None, expected=0, directory=None, python=None):
        arguments = [POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive',
                     '-File', str(SCRIPT), '-ArchivePath', str(self.archive),
                     '-Sha256', digest or self.digest, '-InstallDir', str(directory or self.directory),
                     '-PythonExecutable', str(python or sys.executable), '-NoPathUpdate']
        if hub is not None:
            arguments += ['-HubUrl', hub]
        result = subprocess.run(arguments, capture_output=True, timeout=60)
        detail = (result.stdout + result.stderr).decode('utf-8', errors='replace')
        if expected == 0:
            self.assertEqual(result.returncode, 0, detail)
        else:
            self.assertNotEqual(result.returncode, 0, detail)
        return result

    def launcher(self, *args):
        result = subprocess.run([str(self.directory / 'ctx.exe'), *args],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        return result.stdout.decode('utf-8').strip()

    def test_install_without_get_file_hash_function(self):
        driver = self.root / 'without-file-hash.ps1'
        driver.write_text('''$ErrorActionPreference = 'Stop'
Import-Module -Assembly ([Reflection.Assembly]::LoadWithPartialName('Microsoft.PowerShell.Commands.Utility'))
Import-Module Microsoft.PowerShell.Management
$PSModuleAutoLoadingPreference = 'None'
if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) { throw 'Fixture unexpectedly provides Get-FileHash' }
& $env:CTX_TEST_INSTALLER -ArchivePath $env:CTX_TEST_ARCHIVE -Sha256 $env:CTX_TEST_SHA -InstallDir $env:CTX_TEST_DIR -PythonExecutable $env:CTX_TEST_PYTHON -NoPathUpdate
''', encoding='ascii')
        result = subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(driver)],
                                env=self._environment(), capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode('utf-8', errors='replace'))
        self.assertEqual(self.launcher('--version'), 'ContextHub 0.6.0')

    def test_install_and_update_in_unicode_space_directory_preserve_arguments(self):
        self.install()
        self.assertEqual(self.launcher('--version'), 'ContextHub 0.6.0')
        values = ['init', '\u4f60\u597d path', '--title', '\u672c\u5730\u5bf9\u8bdd & safe',
                  'A&B', '%PATH%', '', 'trailing\\', 'quote"here', 'bang!x']
        self.assertEqual(json.loads(self.launcher(*values)), values)
        self.make_archive('0.6.1')
        self.install()
        self.assertEqual(self.launcher('--version'), 'ContextHub 0.6.1')
        self.assertEqual(hashlib.sha256((self.directory / 'ctx.pyz').read_bytes()).hexdigest(), self.digest)

    def test_checksum_or_version_failure_keeps_existing_installation(self):
        self.install()
        previous_archive = (self.directory / 'ctx.pyz').read_bytes()
        previous_launcher = (self.directory / 'ctx.exe').read_bytes()
        self.make_archive('0.6.1')
        self.install(digest='0' * 64, expected=1)
        self.assertEqual((self.directory / 'ctx.pyz').read_bytes(), previous_archive)
        self.assertEqual((self.directory / 'ctx.exe').read_bytes(), previous_launcher)
        self.make_archive('0.6.1', valid=False)
        self.install(expected=1)
        self.assertEqual((self.directory / 'ctx.pyz').read_bytes(), previous_archive)
        self.assertEqual((self.directory / 'ctx.exe').read_bytes(), previous_launcher)
        self.assertEqual(self.launcher('--version'), 'ContextHub 0.6.0')

    def test_unicode_space_python_path_is_not_corrupted_by_legacy_encoding(self):
        environment = self.root / '\u4e2d\u6587 Python environment'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)],
                       capture_output=True, timeout=60, check=True)
        self.install(python=environment / 'Scripts' / 'python.exe')
        self.assertEqual(self.launcher('--version'), 'ContextHub 0.6.0')

    def test_rejects_non_origins_and_insecure_hubs_before_installing(self):
        for hub in ['http://example.test', 'https://user:pw@example.test', 'https://@example.test',
                    'https://example.test/path', 'https://example.test?token=x',
                    'https://example.test#fragment', 'https://example.test\\@evil.test',
                    'https://example.test:0', 'https://example.test\n']:
            with self.subTest(hub=hub):
                self.install(hub=hub, expected=1)
                self.assertFalse(self.directory.exists())

    def test_junction_target_is_rejected_without_writing_through_it(self):
        outside = self.root / 'outside'
        outside.mkdir()
        junction = self.root / 'junction'
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J',
                                 str(junction), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.addCleanup(junction.rmdir)
        self.install(directory=junction / 'bin', expected=1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_locked_existing_launcher_rolls_back_archive(self):
        self.install()
        previous_archive = (self.directory / 'ctx.pyz').read_bytes()
        previous_launcher = (self.directory / 'ctx.exe').read_bytes()
        self.make_archive('0.6.1')
        driver = self.root / 'lock-and-install.ps1'
        driver.write_text('''$ErrorActionPreference = 'Stop'
$handle = [IO.File]::Open($env:CTX_TEST_LAUNCHER, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
try {
  & $env:CTX_TEST_INSTALLER -ArchivePath $env:CTX_TEST_ARCHIVE -Sha256 $env:CTX_TEST_SHA -InstallDir $env:CTX_TEST_DIR -PythonExecutable $env:CTX_TEST_PYTHON -NoPathUpdate
} finally { $handle.Dispose() }
''', encoding='ascii')
        result = subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(driver)],
                                env=self._environment(), capture_output=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.launcher('--version'), 'ContextHub 0.6.0')
        self.assertEqual((self.directory / 'ctx.pyz').read_bytes(), previous_archive)
        self.assertEqual((self.directory / 'ctx.exe').read_bytes(), previous_launcher)
        self.assertEqual(list(self.directory.glob('.ctx-install-*')), [])

    def test_user_path_is_incremental_idempotent_and_current_shell_is_updated(self):
        driver = self.root / 'path-install.ps1'
        driver.write_text('''$ErrorActionPreference = 'Stop'
$before = [Environment]::GetEnvironmentVariable('Path', 'User')
try {
  & $env:CTX_TEST_INSTALLER -ArchivePath $env:CTX_TEST_ARCHIVE -Sha256 $env:CTX_TEST_SHA -InstallDir $env:CTX_TEST_DIR -PythonExecutable $env:CTX_TEST_PYTHON
  & $env:CTX_TEST_INSTALLER -ArchivePath $env:CTX_TEST_ARCHIVE -Sha256 $env:CTX_TEST_SHA -InstallDir $env:CTX_TEST_DIR -PythonExecutable $env:CTX_TEST_PYTHON
  $after = [Environment]::GetEnvironmentVariable('Path', 'User')
  $parts = @($after.Split(';') | Where-Object { $_.TrimEnd('\\') -ieq $env:CTX_TEST_DIR.TrimEnd('\\') })
  if ($parts.Count -ne 1) { throw 'PATH contains duplicate or missing installation directory' }
  if ($before -and !$after.StartsWith($before)) { throw 'Existing User PATH was modified' }
  if (!($env:Path.Split(';') -contains $env:CTX_TEST_DIR)) { throw 'Current shell PATH was not updated' }
  $version = & ctx --version
  if ($LASTEXITCODE -ne 0 -or $version -ne 'ContextHub 0.6.0') { throw 'ctx launcher was not found in current shell PATH' }
} finally { [Environment]::SetEnvironmentVariable('Path', $before, 'User') }
''', encoding='ascii')
        # Interpreter matrix runs can overlap on a developer's machine. The
        # registry PATH is shared even when installation directories differ.
        from contexthub import safe_fs
        with (Path(tempfile.gettempdir()) / 'contexthub-test-user-path.lock').open('a+b') as handle:
            safe_fs.lock(handle.fileno(), blocking=True)
            result = subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(driver)],
                                    env=self._environment(), capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode('utf-8', errors='replace'))

    def _environment(self):
        return {**os.environ, 'CTX_TEST_INSTALLER': str(SCRIPT), 'CTX_TEST_ARCHIVE': str(self.archive),
                'CTX_TEST_SHA': self.digest, 'CTX_TEST_DIR': str(self.directory),
                'CTX_TEST_PYTHON': sys.executable, 'CTX_TEST_LAUNCHER': str(self.directory / 'ctx.exe')}


if __name__ == '__main__':
    unittest.main()
