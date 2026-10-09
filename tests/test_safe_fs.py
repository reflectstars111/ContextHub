import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from contexthub import safe_fs as fs
from contexthub.package import build_package, read_package
from contexthub.workspace import Workspace, WorkspaceError, _filename


class WindowsPathValidationTests(unittest.TestCase):
    def test_local_unicode_space_paths_are_valid(self):
        self.assertEqual(fs.validate_windows_path('c:/Users/用户/My conversations'),
                         'C:\\Users\\用户\\My conversations')
        self.assertEqual(fs.validate_windows_path('D:\\'), 'D:\\')

    def test_devices_shares_ads_and_ambiguous_names_are_rejected(self):
        invalid = [r'\\server\share\repo', r'\\?\C:\repo', r'\\.\C:\repo',
                   r'C:relative', r'\relative', r'C:\repo:stream', r'C:\NUL',
                   r'C:\con.json', r'C:\COM1\repo', r'C:\LPT¹.txt',
                   r'C:\CONOUT$', 'C:\\trailing.\\repo', 'C:\\trailing \\repo',
                   r'C:\..\repo', r'C:\repo\*', 'C:\\repo\x00x']
        for value in invalid:
            with self.subTest(path=value), self.assertRaises(fs.UnsafePathError):
                fs.validate_windows_path(value)


class SafeFilesystemTests(unittest.TestCase):
    def test_replace_existing_file_preserves_anchor_and_rejects_open_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            (parent / 'target').write_bytes(b'old')
            (parent / 'incoming').write_bytes(b'new')
            anchor = fs.directory(parent)
            try:
                if os.name == 'nt':
                    reader = fs.open_file(anchor, 'target', os.O_RDONLY)
                    try:
                        with self.assertRaises(OSError):
                            fs.replace(anchor, 'incoming', 'target')
                        self.assertEqual((parent / 'target').read_bytes(), b'old')
                        self.assertEqual((parent / 'incoming').read_bytes(), b'new')
                    finally:
                        fs.close(reader)
                fs.replace(anchor, 'incoming', 'target')
                self.assertEqual((parent / 'target').read_bytes(), b'new')
                self.assertFalse((parent / 'incoming').exists())
                fs.replace(anchor, 'target', '中文 & !.json')
                self.assertEqual((parent / '中文 & !.json').read_bytes(), b'new')
            finally:
                fs.close(anchor)

    def test_nested_creation_and_atomic_replace_in_unicode_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / '对话目录 with spaces' / 'private state'
            directory = fs.directory(target, create=True)
            try:
                fd = fs.open_file(directory, 'first.tmp', os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    os.write(fd, '内容'.encode('utf-8'))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                fs.replace(directory, 'first.tmp', 'state.json')
                fd = fs.open_file(directory, 'state.json', os.O_RDONLY)
                try:
                    self.assertEqual(os.read(fd, 100), '内容'.encode('utf-8'))
                finally:
                    os.close(fd)
                with self.assertRaises(OSError):
                    fs.open_file(directory, '../outside', os.O_RDONLY)
            finally:
                fs.close(directory)
            self.assertFalse((target / 'first.tmp').exists())

    def test_unicode_workspace_commit_and_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace.init(Path(temporary) / '中文对话 worktree', '中文对话库')
            data = build_package([{'id': '中文-ID', 'title': '对话', 'messages': [
                {'id': 'message', 'role': 'user', 'content': '第一条内容'}]}], '导入库')
            workspace.add(data)
            first = workspace.commit('第一次提交')
            path = workspace.root / _filename('中文-ID')
            conversation = json.loads(path.read_text(encoding='utf-8'))
            conversation['messages'][0]['content'] = '新编辑的内容'
            path.write_text(json.dumps(conversation, ensure_ascii=False), encoding='utf-8')
            workspace.stage()
            second = workspace.commit('更新内容')
            self.assertEqual(second['parent'], first['id'])
            parsed = read_package(workspace.package_bytes())
            self.assertEqual(parsed['conversations'][0]['messages'][0]['content'], '新编辑的内容')
            self.assertFalse(workspace.status()['modified'])

    def test_workspace_rejects_hardlinked_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace.init(Path(temporary) / 'worktree', 'title')
            source = workspace.root / '.contexthub' / 'state.json'
            alias = Path(temporary) / 'state-alias.json'
            os.link(source, alias)
            with self.assertRaises(WorkspaceError):
                workspace.status()
            self.assertEqual(source.read_bytes(), alias.read_bytes())


@unittest.skipUnless(os.name == 'nt', 'native Windows filesystem tests')
class NativeWindowsFilesystemTests(unittest.TestCase):
    def junction(self, path, target):
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(path), str(target)],
                                capture_output=True)
        if result.returncode:
            self.fail('Creating an unprivileged local directory junction failed: ' + repr(result.stderr))
        if not os.path.lexists(path):
            import ctypes
            if hasattr(ctypes.WinDLL('ntdll'), 'wine_get_version'):
                self.skipTest('Wine mklink reported success without creating a junction; native Windows CI covers this')
            self.fail('mklink reported success without creating the junction')
        information = os.lstat(path)
        self.assertTrue(information.st_file_attributes & 0x400,
                        'mklink must create a real reparse point for this safety test')

    def test_junction_in_ancestors_and_child_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            external = parent / 'external'
            external.mkdir()
            (external / 'protected.txt').write_bytes(b'protected')
            junction = parent / 'alias'
            self.junction(junction, external)
            try:
                with self.assertRaises(OSError):
                    fs.directory(junction)
                with self.assertRaises(OSError):
                    fs.directory(junction / 'should-not-create', create=True)
                anchor = fs.directory(parent)
                try:
                    with self.assertRaises(OSError):
                        fs.child_dir(anchor, 'alias')
                    with self.assertRaises(OSError):
                        fs.stat_entry(anchor, 'alias')
                finally:
                    fs.close(anchor)
                self.assertEqual((external / 'protected.txt').read_bytes(), b'protected')
                self.assertFalse((external / 'should-not-create').exists())
            finally:
                os.rmdir(junction)

    def test_held_ancestors_cannot_be_renamed(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / 'parent'
            leaf = parent / 'child'
            leaf.mkdir(parents=True)
            anchor = fs.directory(leaf)
            try:
                with self.assertRaises(OSError):
                    parent.rename(parent.with_name('moved'))
                with self.assertRaises(OSError):
                    leaf.rename(parent / 'moved-child')
            finally:
                fs.close(anchor)
            parent.rename(parent.with_name('moved'))

    def test_held_directory_cannot_be_opened_for_reparse_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / 'directory'
            parent.mkdir()
            anchor = fs.directory(parent)
            try:
                with self.assertRaises(OSError):
                    fs._open_handle(str(parent), fs._WRITE, fs._SHARE_READ | fs._SHARE_WRITE | 4,
                                    fs._OPEN_EXISTING, directory=True)
            finally:
                fs.close(anchor)
            handle, _ = fs._open_handle(str(parent), fs._WRITE, fs._SHARE_READ | fs._SHARE_WRITE | 4,
                                        fs._OPEN_EXISTING, directory=True)
            fs._close_handle(handle)

    def test_replace_rejects_hardlink_target_without_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            (parent / 'target').write_bytes(b'protected')
            os.link(parent / 'target', parent / 'external-alias')
            (parent / 'temp').write_bytes(b'replacement')
            anchor = fs.directory(parent)
            try:
                with self.assertRaises(OSError):
                    fs.replace(anchor, 'temp', 'target')
                with self.assertRaises(OSError):
                    fs.open_file(anchor, 'target', os.O_WRONLY | os.O_TRUNC)
            finally:
                fs.close(anchor)
            self.assertEqual((parent / 'target').read_bytes(), b'protected')
            self.assertEqual((parent / 'external-alias').read_bytes(), b'protected')
            self.assertEqual((parent / 'temp').read_bytes(), b'replacement')

    def test_junction_workspace_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            external = Workspace.init(parent / 'original', 'title')
            worktree = parent / 'worktree'
            worktree.mkdir()
            self.junction(worktree / '.contexthub', external.root / '.contexthub')
            try:
                with self.assertRaises(WorkspaceError):
                    Workspace.find(worktree)
            finally:
                os.rmdir(worktree / '.contexthub')


if __name__ == '__main__':
    unittest.main()
