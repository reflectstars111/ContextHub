import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from contexthub import windows_launcher as launcher


def stub(machine=0x8664, magic=0x20B):
    data = bytearray(1024)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3C, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<HH', data, 132, machine, 1)
    struct.pack_into('<HH', data, 148, 240, 0x2)
    struct.pack_into('<H', data, 152, magic)
    struct.pack_into('<H', data, 220, 3)
    return bytes(data)


class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = {'purelib': str(self.root / 'site-packages'),
                      'platlib': str(self.root / 'site-packages'),
                      'stdlib': str(self.root / 'Lib')}
        self.platform_patch = patch.object(launcher.sysconfig, 'get_platform', return_value='win-amd64')
        self.paths_patch = patch.object(launcher.sysconfig, 'get_paths', return_value=self.paths)
        self.platform_patch.start()
        self.paths_patch.start()

    def tearDown(self):
        self.paths_patch.stop()
        self.platform_patch.stop()
        self.temporary.cleanup()

    def installed(self, name='t64.exe', data=None):
        path = Path(self.paths['purelib']) / 'pip' / '_vendor' / 'distlib' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(stub() if data is None else data)
        return path

    def test_abi_selection_follows_selected_python_instead_of_host_cpu(self):
        for platform, (name, machine, magic) in launcher._ARCHITECTURES.items():
            with self.subTest(platform=platform):
                data = stub(machine, magic)
                self.installed(name, data)
                with patch.object(launcher.sysconfig, 'get_platform', return_value=platform):
                    target = self.root / (platform + '.exe')
                    self.assertEqual(launcher.build_launcher(target), target)
                self.assertTrue(target.read_bytes().startswith(data))

    def test_build_has_quoted_utf8_interpreter_and_one_entry(self):
        data = stub()
        self.installed(data=data)
        executable = str(self.root / 'Python 用户 & spaces' / 'python.exe')
        target = self.root / 'ctx.exe'
        with patch.object(launcher.sys, 'executable', executable):
            launcher.build_launcher(target)
        payload = target.read_bytes()
        self.assertTrue(payload.startswith(data + ('#!"' + executable + '"\n').encode('utf-8')))
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.namelist(), ['__main__.py'])
            self.assertEqual(archive.read('__main__.py'), launcher._MAIN)

    def test_installed_stub_is_only_read_and_no_pip_module_runs(self):
        self.installed()
        init = Path(self.paths['purelib']) / 'pip' / '__init__.py'
        init.write_text('raise AssertionError("must not import pip")\n', encoding='utf-8')
        launcher.build_launcher(self.root / 'ctx.exe')

    def test_ensurepip_bundled_wheel_fallback_reads_bounded_member(self):
        directory = Path(self.paths['stdlib']) / 'ensurepip' / '_bundled'
        directory.mkdir(parents=True)
        with zipfile.ZipFile(directory / 'pip-25.0.1-py3-none-any.whl', 'w') as archive:
            archive.writestr('pip/__init__.py', 'raise AssertionError("never imported")')
            archive.writestr('pip/_vendor/distlib/t64.exe', stub())
        target = launcher.build_launcher(self.root / 'ctx.exe')
        self.assertTrue(target.read_bytes().startswith(stub()))

    def test_bad_or_wrong_architecture_stub_is_rejected_without_output(self):
        variations = [b'MZ', b'not executable' * 100, stub(0x14C, 0x10B)]
        for offset, format, value in [(0x3C, '<I', 0xFFFFFFFF),
                                      (148, '<H', 65535), (150, '<H', 0x2002),
                                      (152, '<H', 0x10B), (220, '<H', 2)]:
            data = bytearray(stub())
            struct.pack_into(format, data, offset, value)
            variations.append(bytes(data))
        for data in variations:
            with self.subTest(size=len(data)):
                self.installed(data=data)
                target = self.root / 'ctx.exe'
                with self.assertRaises(launcher.LauncherError):
                    launcher.build_launcher(target)
                self.assertFalse(target.exists())

    def test_missing_resources_explains_full_python_and_direct_pyz_fallback(self):
        with self.assertRaisesRegex(launcher.LauncherError, 'pip 或 ensurepip.*py ctx.pyz'):
            launcher.build_launcher(self.root / 'ctx.exe')
        self.assertFalse((self.root / 'ctx.exe').exists())

    def test_existing_output_is_never_overwritten(self):
        self.installed()
        target = self.root / 'ctx.exe'
        target.write_bytes(b'previous launcher')
        with self.assertRaises(FileExistsError):
            launcher.build_launcher(target)
        self.assertEqual(target.read_bytes(), b'previous launcher')

    def test_invalid_interpreter_cannot_inject_another_shebang_argument(self):
        self.installed()
        for suffix in ('" --injected', '\nmalicious', '\rmalicious', '\0'):
            executable = str(self.root / 'python.exe') + suffix
            with self.subTest(suffix=repr(suffix)), patch.object(launcher.sys, 'executable', executable):
                with self.assertRaises(launcher.LauncherError):
                    launcher.build_launcher(self.root / 'ctx.exe')
        self.assertFalse((self.root / 'ctx.exe').exists())

    def test_zip_entry_runs_adjacent_pyz_preserving_arguments_and_exit_status(self):
        self.installed()
        directory = self.root / '用户 & launch % ! spaces'
        directory.mkdir()
        archive_path = directory / 'ctx.pyz'
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('__main__.py',
                             'import json, sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=True))\n'
                             'raise SystemExit(37)\n')
        target = launcher.build_launcher(directory / 'ctx.exe')
        arguments = ['中文 参数', '', 'a&b', '%PATH%', '!value!', 'a"b', 'ends\\', '; $(echo danger)']
        # Python can read the appended ZIP directly on all platforms. Native
        # Windows CI additionally exercises the actual distlib PE process stub.
        result = subprocess.run([sys.executable, '-X', 'utf8', str(target), *arguments],
                                cwd=self.root, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)


@unittest.skipUnless(os.name == 'nt', 'requires a Windows interpreter and its installed pip/ensurepip')
class NativeWindowsLauncherTests(unittest.TestCase):
    def test_native_console_launcher_preserves_unicode_metacharacters_and_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / '中文 ctx & % ! space'
            directory.mkdir()
            with zipfile.ZipFile(directory / 'ctx.pyz', 'w') as archive:
                archive.writestr('__main__.py',
                                 'import json, sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=True))\n'
                                 'raise SystemExit(29)\n')
            target = launcher.build_launcher(directory / 'ctx.exe')
            arguments = ['中文 空格', 'a&b', '%PATH%', '!x!', 'a"b', '', 'tail\\', 'x|y']
            result = subprocess.run([str(target), *arguments], cwd=temporary, capture_output=True,
                                    text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 29, result.stderr)
            self.assertEqual(json.loads(result.stdout), arguments)


if __name__ == '__main__':
    unittest.main()
