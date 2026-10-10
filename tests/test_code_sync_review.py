"""Independent regressions for checkpoint integrity and restore boundaries."""
import json
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch
import zlib

from contexthub import code_sync
from tests import test_code_sync as helpers


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class CodeSyncReviewTests(unittest.TestCase):
    setUp = helpers.CodeSyncTests.setUp
    tearDown = helpers.CodeSyncTests.tearDown
    git = helpers.CodeSyncTests.git
    capture = helpers.CodeSyncTests.capture

    def test_crlf_only_dirty_change_cannot_claim_clean_checkpoint(self):
        self.git('config', 'core.autocrlf', 'false')
        (self.repo / '.gitattributes').write_bytes(b'app.py -text\n')
        self.git('add', '.gitattributes')
        self.git('commit', '-m', 'Keep literal source bytes')
        (self.repo / 'app.py').write_bytes(b'print("one")\r\n')
        self.assertIn('app.py', self.git('status', '--porcelain'))
        previous = self.ws.head
        with self.assertRaises(code_sync.CodeSyncError):
            self.capture()
        self.assertEqual(self.ws.head, previous)

    def test_empty_directories_count_towards_tree_entry_bound(self):
        def tree(data):
            result = subprocess.run(['git', '-C', str(self.repo), 'mktree', '-z'],
                                    input=data, capture_output=True, check=True, timeout=20)
            return result.stdout.decode().strip()

        empty = tree(b'')
        root = tree(b''.join(f'040000 tree {empty}\t{name}\0'.encode() for name in ('a', 'b', 'c')))
        commit = self.git('commit-tree', root, '-m', 'Empty directory fixture')
        with code_sync._source(self.repo, commit) as (git, *_):
            with patch.object(code_sync, '_MAX_FILES', 2):
                with self.assertRaises(code_sync.CodeSyncError):
                    code_sync._tree(git, commit)

    def test_interrupt_during_native_import_preserves_pair_without_retry(self):
        self.capture()
        with patch('contexthub.codex.CodexClient'), patch('contexthub.codex.import_conversation') as importer:
            importer.side_effect = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                code_sync.restore_pair(self.ws, 'HEAD', self.repo, self.target, codex=True)
            receipt = json.loads((self.target / 'receipt.json').read_text())
            self.assertEqual(receipt['pair_status'], 'complete')
            self.assertEqual(receipt['native_status'], 'in_progress')
            with self.assertRaises(code_sync.NativeRestoreError):
                code_sync.restore_pair(self.ws, 'HEAD', self.repo, self.target, codex=True, resume=True)
            self.assertEqual(importer.call_count, 1)

    def test_object_copy_failure_never_publishes_partial_pair(self):
        self.capture()
        original = code_sync._Objects.run

        def fail_copy(git, *args, **kwargs):
            if args[0] == 'index-pack':
                raise code_sync.CodeSyncError('simulated object transfer failure')
            return original(git, *args, **kwargs)

        with patch.object(code_sync._Objects, 'run', fail_copy):
            with self.assertRaises(code_sync.CodeSyncError):
                code_sync.restore_pair(self.ws, 'HEAD', self.repo, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob('.ctx-git-*')))

    def test_corrupt_source_object_cannot_publish_under_the_recorded_id(self):
        self.capture()
        oid = self.git('rev-parse', 'HEAD:app.py')
        replacement = b'print("different object bytes")\n'
        object_path = self.repo / '.git' / 'objects' / oid[:2] / oid[2:]
        object_path.chmod(0o600)
        object_path.write_bytes(zlib.compress(b'blob ' + str(len(replacement)).encode() + b'\0' + replacement))
        with self.assertRaises(ValueError):
            code_sync.restore_pair(self.ws, 'HEAD', self.repo, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob('.ctx-git-*')))

    def test_capture_rejects_corrupt_blob_even_when_worktree_matches_it(self):
        oid = self.git('rev-parse', 'HEAD:app.py')
        replacement = b'print("corrupted object also matches working copy")\n'
        object_path = self.repo / '.git' / 'objects' / oid[:2] / oid[2:]
        object_path.chmod(0o600)
        object_path.write_bytes(zlib.compress(b'blob ' + str(len(replacement)).encode() + b'\0' + replacement))
        (self.repo / 'app.py').write_bytes(replacement)
        before = self.ws.head
        with self.assertRaises(ValueError):
            self.capture()
        self.assertEqual(self.ws.head, before)

    def test_corrupt_commit_cannot_capture_or_restore_under_old_id(self):
        self.capture()
        before = self.ws.head
        oid = self.git('rev-parse', 'HEAD')
        replacement = (self.git('cat-file', 'commit', oid) + '\nforged message\n').encode()
        object_path = self.repo / '.git' / 'objects' / oid[:2] / oid[2:]
        object_path.chmod(0o600)
        object_path.write_bytes(zlib.compress(b'commit ' + str(len(replacement)).encode() + b'\0' + replacement))
        with self.assertRaises(ValueError):
            self.capture()
        with self.assertRaises(ValueError):
            code_sync.restore_pair(self.ws, 'HEAD', self.repo, self.target)
        self.assertEqual(self.ws.head, before)
        self.assertFalse(self.target.exists())

    def test_corrupt_tree_cannot_capture_or_restore_under_old_id(self):
        self.capture()
        before = self.ws.head
        oid = self.git('rev-parse', 'HEAD^{tree}')
        blob = self.git('rev-parse', 'HEAD:app.py')
        replacement = b'100644 renamed.py\0' + bytes.fromhex(blob)
        object_path = self.repo / '.git' / 'objects' / oid[:2] / oid[2:]
        object_path.chmod(0o600)
        object_path.write_bytes(zlib.compress(b'tree ' + str(len(replacement)).encode() + b'\0' + replacement))
        with self.assertRaises(ValueError):
            self.capture()
        with self.assertRaises(ValueError):
            code_sync.restore_pair(self.ws, 'HEAD', self.repo, self.target)
        self.assertEqual(self.ws.head, before)
        self.assertFalse(self.target.exists())

    def test_source_blob_mutation_after_pack_does_not_change_restored_copy(self):
        self.capture()
        oid = self.git('rev-parse', 'HEAD:app.py')
        original = code_sync._Objects.run

        def pack_then_corrupt(git, *args, **kwargs):
            result = original(git, *args, **kwargs)
            if args[0] == 'pack-objects':
                replacement = b'print("source changed after packing")\n'
                object_path = self.repo / '.git' / 'objects' / oid[:2] / oid[2:]
                object_path.chmod(0o600)
                object_path.write_bytes(zlib.compress(b'blob ' + str(len(replacement)).encode() + b'\0' + replacement))
            return result

        with patch.object(code_sync._Objects, 'run', pack_then_corrupt):
            result = code_sync.restore_pair(self.ws, 'HEAD', self.repo, self.target)
        self.assertEqual(result['pair_status'], 'complete')
        self.assertEqual((self.target / 'code' / 'app.py').read_text(), 'print("one")\n')
        self.assertEqual(self.git('status', '--porcelain', repository=self.target / 'code'), '')

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink fixture')
    def test_parent_symlink_cannot_redirect_pair_publication(self):
        self.capture()
        outside = self.root / 'outside'
        outside.mkdir()
        redirect = self.root / 'redirect'
        redirect.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            code_sync.restore_pair(self.ws, 'HEAD', self.repo, redirect / 'new')
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
