"""Platform-independent fixtures for Windows native Codex discovery."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contexthub.codex import CodexError, _default_command


class WindowsCodexDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'CONTEXTHUB_CODEX_EXE': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_native_exe_and_explicit_unicode_path_are_argument_vectors(self):
        with patch('contexthub.codex.shutil.which', return_value='C:/Codex/codex.exe'):
            self.assertEqual(_default_command(windows=True), ['C:/Codex/codex.exe', 'app-server', '--listen', 'stdio://'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '中文 space & name.exe'
            path.write_bytes(b'fixture-not-executed')
            with patch.dict(os.environ, {'CONTEXTHUB_CODEX_EXE': str(path)}):
                self.assertEqual(_default_command(windows=True)[0], str(path.resolve()))
            path = path.with_suffix('.cmd')
            path.write_text('must not execute')
            with patch.dict(os.environ, {'CONTEXTHUB_CODEX_EXE': str(path)}), self.assertRaises(CodexError):
                _default_command(windows=True)

    def test_npm_shim_resolves_native_binary_and_does_not_execute_batch(self):
        for architecture, triple, variant in [('AMD64', 'x86_64', 'x64'), ('ARM64', 'aarch64', 'arm64')]:
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shim = root / 'codex.cmd'
                shim.write_text('never execute shell')
                exe = root / 'node_modules' / '@openai' / ('codex-win32-' + variant) / 'vendor' / (triple + '-pc-windows-msvc') / 'bin' / 'codex.exe'
                exe.parent.mkdir(parents=True)
                exe.write_bytes(b'fixture-not-executed')
                with patch('contexthub.codex.shutil.which', side_effect=lambda name: str(shim) if name == 'codex.cmd' else None):
                    self.assertEqual(_default_command(windows=True, machine=architecture)[0], str(exe.resolve()))

    def test_missing_windows_binary_fails_with_configuration_hint(self):
        with patch('contexthub.codex.shutil.which', return_value=None), self.assertRaisesRegex(CodexError, 'CONTEXTHUB_CODEX_EXE'):
            _default_command(windows=True)
        self.assertEqual(_default_command(windows=False)[0], 'codex')
