"""Native Windows tests are explicitly separated from mocked DPAPI contract tests."""

import ctypes
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from contexthub import hub_client
from contexthub import windows_credentials as windows


def record():
    return {'cookie_name': '__Host-ch_session', 'token': 's' * 43, 'csrf_token': 'c' * 43,
            'user': {'id': 'uid', 'username': 'alice'}, 'expires_at': time.time() + 3600}


class FakeDPAPI:
    """Contract fixture, not an implementation or security test of Windows DPAPI."""

    def __init__(self, *, success=True, result=b'encrypted-session', result_length=None):
        self.success, self.result, self.result_length = success, result, result_length
        self.calls, self.freed = [], []
        self.buffers = []

    def operation(self, source, description, entropy, reserved, prompt, flags, target):
        source = ctypes.cast(source, ctypes.POINTER(windows._DataBlob)).contents
        entropy = ctypes.cast(entropy, ctypes.POINTER(windows._DataBlob)).contents
        self.calls.append({'source': ctypes.string_at(source.pbData, source.cbData),
                           'entropy': ctypes.string_at(entropy.pbData, entropy.cbData),
                           'description': description, 'reserved': reserved, 'prompt': prompt,
                           'flags': flags, 'input_pointer': source.pbData, 'input_length': source.cbData})
        if self.result is not None:
            buffer = ctypes.create_string_buffer(self.result)
            self.buffers.append(buffer)
            target = ctypes.cast(target, ctypes.POINTER(windows._DataBlob)).contents
            target.cbData = self.result_length if self.result_length is not None else len(self.result)
            target.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        return self.success

    def free(self, pointer):
        self.freed.append(ctypes.string_at(pointer, len(self.result)))
        self.input_after_use = ctypes.string_at(self.calls[-1]['input_pointer'], self.calls[-1]['input_length'])

    def api(self):
        return (SimpleNamespace(CryptProtectData=self.operation, CryptUnprotectData=self.operation),
                SimpleNamespace(LocalFree=self.free))


class DPAPIContractTests(unittest.TestCase):
    def test_protect_uses_current_user_no_ui_and_zeroes_native_buffers(self):
        fake = FakeDPAPI()
        with patch.object(windows, '_windows_api', return_value=fake.api()):
            encrypted = windows.protect(b'{"token":"synthetic-secret"}')
        self.assertEqual(encrypted, windows.MAGIC + b'encrypted-session')
        call = fake.calls[0]
        self.assertEqual(call['flags'], 1)  # UI_FORBIDDEN, never LOCAL_MACHINE (4).
        self.assertEqual(call['entropy'], windows._ENTROPY)
        self.assertIsNone(call['description'])
        self.assertIsNone(call['reserved'])
        self.assertIsNone(call['prompt'])
        self.assertEqual(fake.freed, [b'\x00' * len(fake.result)])
        self.assertEqual(fake.input_after_use, b'\x00' * call['input_length'])

    def test_unprotect_bounds_input_and_passes_only_ciphertext_to_dpapi(self):
        fake = FakeDPAPI(result=b'{"version":1,"hubs":{}}')
        with patch.object(windows, '_windows_api', return_value=fake.api()):
            decoded = windows.unprotect(windows.MAGIC + b'ciphertext')
        self.assertEqual(decoded, fake.result)
        self.assertEqual(fake.calls[0]['source'], b'ciphertext')
        self.assertEqual(fake.calls[0]['flags'], 1)
        with patch.object(windows, '_windows_api') as api:
            for invalid in (b'{"token":"plaintext"}', b'', windows.MAGIC,
                            windows.MAGIC + b'x' * windows.MAX_STORED_BYTES):
                with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                    windows.unprotect(invalid)
            api.assert_not_called()

    def test_native_failure_or_oversize_result_fails_without_plaintext_fallback(self):
        for fake in (FakeDPAPI(success=False), FakeDPAPI(result=None),
                     FakeDPAPI(result_length=windows.MAX_STORED_BYTES + 1)):
            with self.subTest(fake=fake), patch.object(windows, '_windows_api', return_value=fake.api()):
                with self.assertRaisesRegex(ValueError, 'DPAPI'):
                    windows.protect(b'synthetic-secret')
            if fake.result is not None:
                self.assertEqual(len(fake.freed), 1)
        with patch.object(windows, '_windows_api') as api:
            for invalid in (b'', 'plaintext', b'x' * (windows.MAX_PLAINTEXT_BYTES + 1)):
                with self.assertRaises(ValueError):
                    windows.protect(invalid)
            api.assert_not_called()

    def test_windows_default_and_override_locations_are_separate_from_posix(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch.object(hub_client, '_IS_WINDOWS', True), patch.dict(os.environ, {
                    'LOCALAPPDATA': str(directory), 'CONTEXTHUB_CONFIG_DIR': '',
                    'XDG_CONFIG_HOME': str(directory / 'ignored')}):
                self.assertEqual(hub_client.Credentials().path, directory / 'ContextHub' / 'credentials.dpapi')
                with patch.dict(os.environ, {'CONTEXTHUB_CONFIG_DIR': str(directory / 'custom')}):
                    self.assertEqual(hub_client.Credentials().path, directory / 'custom' / 'credentials.dpapi')


class WindowsStorageContractTests(unittest.TestCase):
    """Exercise the Windows storage route with mocked encryption on either OS."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config' / 'credentials.dpapi'
        self.enterContext(patch.object(hub_client, '_IS_WINDOWS', True))
        # Reversible bytes merely stand in for the native provider in these
        # tests. The separate native suite verifies real DPAPI round trips.
        self.enterContext(patch.object(windows, '_dpapi', side_effect=lambda raw, **kwargs: bytes(b ^ 0xA5 for b in raw)))
        self.credentials = hub_client.Credentials(self.path)

    def test_round_trip_retains_origin_isolation_and_never_writes_plaintext(self):
        self.credentials.set('https://example.com', record())
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(windows.MAGIC))
        self.assertNotIn(record()['token'].encode(), raw)
        self.assertNotIn(b'"hubs"', raw)
        self.assertEqual(self.credentials.get('https://example.com')['user']['username'], 'alice')
        self.assertIsNone(self.credentials.get('https://other.example.com'))
        self.credentials.remove('https://example.com')
        self.assertIsNone(self.credentials.get('https://example.com'))
        self.assertEqual(list(self.path.parent.glob('.credentials-*.tmp')), [])

    def test_encryption_failure_never_creates_file_or_replaces_previous_ciphertext(self):
        with patch.object(windows, 'protect', side_effect=ValueError('DPAPI failed')):
            with self.assertRaises(ValueError):
                self.credentials.set('https://example.com', record())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob('.credentials-*.tmp')), [])
        self.credentials.set('https://example.com', record())
        previous = self.path.read_bytes()
        with patch.object(windows, 'protect', side_effect=ValueError('DPAPI failed')):
            with self.assertRaises(ValueError):
                self.credentials.set('https://other.example.com', record())
        self.assertEqual(self.path.read_bytes(), previous)

    def test_plaintext_file_is_rejected_without_automatic_migration(self):
        self.path.parent.mkdir(mode=0o700)
        self.path.write_text(json.dumps({'version': 1, 'hubs': {'https://example.com': record()}}))
        with self.assertRaisesRegex(ValueError, 'DPAPI'):
            self.credentials.get('https://example.com')

    def test_hardlink_file_is_rejected(self):
        self.credentials.set('https://example.com', record())
        os.link(self.path, self.path.parent / 'alias')
        with self.assertRaises(ValueError):
            self.credentials.get('https://example.com')

    def test_failed_decryption_never_overwrites_saved_file(self):
        self.credentials.set('https://example.com', record())
        previous = self.path.read_bytes()
        with patch.object(windows, 'unprotect', side_effect=ValueError('DPAPI failed')):
            with self.assertRaises(ValueError):
                self.credentials.set('https://other.example.com', record())
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob('.credentials-*.tmp')), [])


@unittest.skipUnless(os.name == 'nt', 'Requires native Windows DPAPI and filesystem handles')
class NativeWindowsCredentialTests(unittest.TestCase):
    def test_current_windows_user_round_trip_and_tamper_rejection(self):
        plaintext = b'{"token":"synthetic-session-secret","version":1}'
        encrypted = windows.protect(plaintext)
        self.assertNotIn(b'synthetic-session-secret', encrypted)
        self.assertEqual(windows.unprotect(encrypted), plaintext)
        tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
        with self.assertRaises(ValueError):
            windows.unprotect(tampered)

    def test_real_storage_session_restore_and_nonblocking_lock(self):
        from contexthub import safe_fs

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config' / 'credentials.dpapi'
            credentials = hub_client.Credentials(path)
            credentials.set('https://example.com', record())
            self.assertNotIn(record()['token'].encode(), path.read_bytes())
            self.assertEqual(hub_client.Credentials(path).get('https://example.com')['user']['username'], 'alice')
            directory = safe_fs.directory(path.parent)
            descriptor = safe_fs.open_file(directory, '.credentials.lock', os.O_RDWR)
            try:
                safe_fs.lock(descriptor)
                started = time.monotonic()
                with self.assertRaisesRegex(ValueError, '稍后重试'):
                    credentials.get('https://example.com')
                self.assertLess(time.monotonic() - started, 2)
            finally:
                safe_fs.close(descriptor)
                safe_fs.close(directory)

    def test_junction_ancestor_cannot_receive_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, alias = root / 'actual', root / 'alias'
            target.mkdir()
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(target)],
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            if not os.path.lexists(alias):
                if hasattr(ctypes.WinDLL('ntdll'), 'wine_get_version'):
                    self.skipTest('Wine mklink reported success without creating a junction; native Windows CI covers this')
                self.fail('mklink reported success without creating the junction')
            self.assertTrue(os.lstat(alias).st_file_attributes & 0x400,
                            'mklink must create a real reparse point for this safety test')
            try:
                credentials = hub_client.Credentials(alias / 'config' / 'credentials.dpapi')
                with self.assertRaises(ValueError):
                    credentials.set('https://example.com', record())
                self.assertEqual(list(target.iterdir()), [])
            finally:
                alias.rmdir()


if __name__ == '__main__':
    unittest.main()
