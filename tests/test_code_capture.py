"""Working-tree code snapshots preserve actual bytes without changing Git."""
import base64
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from contexthub.code_capture import capture_code, restore_code, CodeCaptureError


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class CodeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'project'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'core.autocrlf', 'false')
        (self.repo / 'app.py').write_text('initial\n')
        (self.repo / '.gitignore').write_text('ignored/\n')
        self.git('add', '.')
        self.git('commit', '-m', 'initial')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *arguments):
        result = subprocess.run(['git', '-c', 'user.name=Synthetic',
                                 '-c', 'user.email=test@example.invalid',
                                 '-c', 'commit.gpgSign=false', '-C', str(self.repo),
                                 *arguments], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()

    def files(self, code):
        snapshot = next(row for row in code['snapshots'] if row['id'] == code['head'])
        return {row['path']: base64.b64decode(code['blobs'][row['sha256']])
                for row in snapshot['files']}

    def test_dirty_untracked_and_deleted_files_roundtrip_without_source_mutation(self):
        (self.repo / 'app.py').write_text('staged\n')
        self.git('add', 'app.py')
        (self.repo / 'app.py').write_bytes(b'actual working bytes\n')
        (self.repo / 'new').mkdir()
        (self.repo / 'new/untracked.py').write_bytes(b'print("new")\n')
        (self.repo / 'ignored').mkdir()
        (self.repo / 'ignored/private').write_text('ignored')
        (self.repo / '.gitignore').unlink()
        # The absent tracked ignore file no longer applies; info/exclude does.
        (self.repo / '.git/info/exclude').write_text('ignored/\n')
        before = (self.repo / '.git/index').read_bytes(), self.git('rev-parse', 'HEAD')
        code = capture_code(self.repo)
        self.assertEqual(self.files(code), {'app.py': b'actual working bytes\n',
                                           'new/untracked.py': b'print("new")\n'})
        target = self.root / 'restored'
        receipt = restore_code(code, target)
        self.assertEqual(receipt['snapshot_id'], code['head'])
        self.assertEqual((target / 'app.py').read_bytes(), b'actual working bytes\n')
        self.assertEqual((target / 'new/untracked.py').read_bytes(), b'print("new")\n')
        self.assertEqual(before, ((self.repo / '.git/index').read_bytes(), self.git('rev-parse', 'HEAD')))

    def test_ignored_credentials_and_context_state_are_excluded_with_report(self):
        for name in ('ignored', '.contexthub', 'conversations', '.context-data'):
            (self.repo / name).mkdir()
            (self.repo / name / 'secret').write_text('sensitive')
        for name in ('.env', '.env.production', '.netrc', 'id_ed25519', 'credentials.json',
                     'library.json', 'code.json'):
            (self.repo / name).write_text('sensitive')
        (self.repo / '.env.example').write_text('TOKEN=\n')
        self.git('add', '-f', '.env', 'credentials.json')
        report = {}
        files = self.files(capture_code(self.repo, report=report))
        self.assertEqual(set(files), {'app.py', '.gitignore', '.env.example'})
        self.assertTrue({'.env', 'credentials.json', 'code.json'} <=
                        {entry['path'] for entry in report['excluded']})
        self.assertEqual(report['captured_files'], 3)

    def test_crlf_bytes_are_preserved_and_lfs_pointers_are_rejected(self):
        data = b'actual Windows bytes\r\n'
        (self.repo / 'app.py').write_bytes(data)
        code = capture_code(self.repo)
        self.assertEqual(self.files(code)['app.py'], data)
        restore_code(code, self.root / 'crlf-restored')
        self.assertEqual((self.root / 'crlf-restored/app.py').read_bytes(), data)
        for newline in (b'\n', b'\r\n'):
            with self.subTest(newline=newline):
                (self.repo / 'app.py').write_bytes(newline.join([
                    b'version https://git-lfs.github.com/spec/v1',
                    b'oid sha256:' + b'a' * 64, b'size 100', b'']))
                with self.assertRaises(CodeCaptureError):
                    capture_code(self.repo)

    def test_unchanged_reuses_snapshot_changed_preserves_history_and_explicit_bindings(self):
        conversations = [{'id': 'conversation', 'messages': [{'id': 'old'}, {'id': 'latest'}]}]
        first = capture_code(self.repo, conversations=conversations,
                             bind_messages=[{'conversation_id': 'conversation', 'message_id': 'latest'}])
        original = copy.deepcopy(first)
        same = capture_code(self.repo, first, conversations=conversations)
        self.assertEqual(same, first)
        (self.repo / 'app.py').write_text('changed\n')
        second = capture_code(self.repo, first, conversations=conversations)
        self.assertNotEqual(second['head'], first['head'])
        self.assertEqual(second['snapshots'][-1]['parents'], [first['head']])
        self.assertEqual(second['bindings'], first['bindings'])
        self.assertEqual(first, original)
        self.assertEqual(first['bindings'][0]['message_id'], 'latest')
        target = self.root / 'old'
        restore_code(second, target, snapshot=first['head'])
        self.assertEqual((target / 'app.py').read_text(), 'initial\n')

    def test_unborn_repository_and_existing_restore_target(self):
        self.repo = self.root / 'newrepo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        (self.repo / 'README.md').write_text('new project')
        code = capture_code(self.repo)
        self.assertEqual(code['snapshots'][0]['git_commit'], '')
        self.assertEqual(code['snapshots'][0]['branch'], 'main')
        with self.assertRaises(CodeCaptureError):
            restore_code(code, self.repo)
        self.assertEqual((self.repo / 'README.md').read_text(), 'new project')

    @unittest.skipIf(os.name == 'nt', 'POSIX mode, links and executable fixture')
    def test_symlink_rejected_executable_preserved_and_git_commands_are_inert(self):
        marker = self.root / 'executed'
        payload = self.root / 'attack'
        payload.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        payload.chmod(0o700)
        self.git('config', 'core.fsmonitor', str(payload))
        self.git('config', 'filter.evil.clean', str(payload))
        self.git('config', 'filter.evil.smudge', str(payload))
        (self.repo / '.gitattributes').write_text('*.py filter=evil\n')
        (self.repo / 'app.py').chmod(0o755)
        code = capture_code(self.repo)
        restore_code(code, self.root / 'executable')
        self.assertTrue((self.root / 'executable/app.py').stat().st_mode & 0o111)
        self.assertFalse(marker.exists())
        (self.repo / 'link').symlink_to(self.repo / 'app.py')
        with self.assertRaises(CodeCaptureError):
            capture_code(self.repo)

    def test_corrupt_or_unsafe_snapshot_never_publishes(self):
        code = capture_code(self.repo)
        digest = next(iter(code['blobs']))
        code['blobs'][digest] = base64.b64encode(b'altered').decode('ascii')
        target = self.root / 'bad'
        with self.assertRaises(ValueError):
            restore_code(code, target)
        self.assertFalse(target.exists())

    def test_deleted_directory_and_linked_worktree(self):
        (self.repo / 'directory').mkdir()
        (self.repo / 'directory/removed.py').write_text('removed')
        self.git('add', '.')
        self.git('commit', '-m', 'directory')
        shutil.rmtree(self.repo / 'directory')
        self.assertNotIn('directory/removed.py', self.files(capture_code(self.repo)))
        worktree = self.root / 'linked'
        self.git('worktree', 'add', '-b', 'topic', str(worktree))
        (worktree / 'app.py').write_bytes(b'linked dirty\n')
        code = capture_code(worktree)
        self.assertEqual(code['snapshots'][0]['branch'], 'topic')
        self.assertEqual(self.files(code)['app.py'], b'linked dirty\n')

    def test_capture_from_nested_cwd_still_enumerates_entire_repository(self):
        nested = self.repo / 'nested'
        nested.mkdir()
        (nested / 'local.py').write_text('nested file\n')
        expected = self.files(capture_code(self.repo))
        index_before = (self.repo / '.git/index').read_bytes()
        head_before = self.git('rev-parse', 'HEAD')
        previous_cwd = Path.cwd()
        try:
            os.chdir(nested)
            actual = self.files(capture_code(self.repo))
        finally:
            os.chdir(previous_cwd)
        self.assertEqual(actual, expected)
        self.assertIn('app.py', actual)
        self.assertIn('nested/local.py', actual)
        self.assertEqual((self.repo / '.git/index').read_bytes(), index_before)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head_before)

    def test_concurrent_edits_fail_instead_of_publishing_mixed_bytes(self):
        import contexthub.code_capture as module
        original = module._read_work_file
        reads = 0
        def mutate(root, path, mode=None):
            nonlocal reads
            value = original(root, path, mode)
            if path == 'app.py':
                reads += 1
                if reads == 1:
                    (self.repo / 'app.py').write_text('concurrent change\n')
            return value
        with patch.object(module, '_read_work_file', side_effect=mutate):
            with self.assertRaises(CodeCaptureError):
                capture_code(self.repo)

    @unittest.skipIf(os.name == 'nt', 'POSIX hardlink fixture')
    def test_hardlink_and_lfs_pointer_rejected(self):
        os.link(self.repo / 'app.py', self.repo / 'hardlink')
        with self.assertRaises(CodeCaptureError):
            capture_code(self.repo)
        (self.repo / 'hardlink').unlink()
        (self.repo / 'app.py').write_text('version https://git-lfs.github.com/spec/v1\n'
                                        'oid sha256:' + 'a' * 64 + '\nsize 100\n')
        with self.assertRaises(CodeCaptureError):
            capture_code(self.repo)


if __name__ == '__main__':
    unittest.main()
