"""Real Git pairing, exact-object recovery and native retry boundaries."""
from contextlib import chdir
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from contexthub.code_sync import capture_checkpoint, restore_pair, CodeSyncError, NativeRestoreError
from contexthub.package import build_package, read_package
from contexthub.workspace import Workspace, WorkspaceError
from tests.test_workspace_branches import pack


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class CodeSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'code'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'core.autocrlf', 'false')
        (self.repo / 'app.py').write_bytes(b'print("one")\n')
        self.git('add', '.'); self.git('commit', '-m', 'initial')
        self.ws = Workspace.init(self.root / 'conversations', 'Synthetic')
        self.ws.add(pack()); self.ws.commit('initial conversation')
        self.target = self.root / 'restored'

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, repository=None):
        result = subprocess.run(['git', '-c', 'user.name=Synthetic', '-c', 'user.email=test@example.invalid',
                                 '-c', 'commit.gpgSign=false', '-C', str(repository or self.repo), *args],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()

    def capture(self):
        return capture_checkpoint(self.ws, self.repo, 'paired')

    def test_raw_archive_pair_is_preserved_but_not_imported_as_native_dialogue(self):
        archive = Workspace.init(self.root / 'archive', 'Unparsed source')
        archive.add(build_package([{'id': 'archive', 'source': {'kind': 'source_archive'},
                                   'messages': [{'role': 'user', 'content': 'Unparsed raw log'}]}], 'Unparsed source'))
        archive.commit('save raw source')
        capture_checkpoint(archive, self.repo, 'pair raw archive')
        with patch('contexthub.codex.CodexClient') as client:
            with self.assertRaises(CodeSyncError):
                restore_pair(archive, 'HEAD', self.repo, self.target, codex=True)
            client.assert_not_called()
        self.assertFalse(self.target.exists())
        self.assertEqual(restore_pair(archive, 'HEAD', self.repo, self.target)['pair_status'], 'complete')

    def test_exact_pair_after_branch_moves_keeps_source_untouched(self):
        self.git('remote', 'add', 'origin', 'https://user:secret@github.com/example/project?token=secret')
        result = self.capture()
        checkpoint = result['code_checkpoint']
        self.assertEqual(checkpoint['repository_url'], 'https://github.com/example/project')
        snapshot = self.ws.package_bytes()
        (self.repo / 'app.py').write_text('print("two")\n')
        self.git('add', '.'); self.git('commit', '-m', 'newer')
        (self.repo / 'app.py').write_text('dirty source stays\n')
        index = (self.repo / '.git/index').read_bytes()
        head = self.git('rev-parse', 'HEAD')
        outcome = restore_pair(self.ws, 'HEAD', self.repo, self.target)
        self.assertEqual(outcome['pair_status'], 'complete')
        self.assertEqual(self.git('rev-parse', 'HEAD', repository=self.target / 'code'), checkpoint['commit'])
        self.assertEqual((self.target / 'code/app.py').read_text(), 'print("one")\n')
        self.assertEqual(self.git('status', '--porcelain', repository=self.target / 'code'), '')
        self.assertEqual(Workspace(self.target / 'conversations').package_bytes(), snapshot)
        self.assertEqual((self.repo / 'app.py').read_text(), 'dirty source stays\n')
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertFalse((self.target / 'code/.git/objects/info/alternates').exists())
        resumed = restore_pair(self.ws, 'HEAD', self.repo, self.target, resume=True)
        self.assertTrue(resumed['resumed'])
        (self.target / 'code/app.py').write_text('changed')
        with self.assertRaises(CodeSyncError):
            restore_pair(self.ws, 'HEAD', self.repo, self.target, resume=True)

    def test_capture_rejects_dirty_code_index_untracked_and_conversations(self):
        original = self.ws.head
        (self.repo / 'app.py').write_text('dirty')
        with self.assertRaises(CodeSyncError): self.capture()
        self.git('add', '.')
        with self.assertRaises(CodeSyncError): self.capture()
        self.git('reset', '--hard', 'HEAD')
        (self.repo / 'extra').write_text('untracked')
        with self.assertRaises(CodeSyncError): self.capture()
        (self.repo / 'extra').unlink()
        conversation = next((self.ws.root / 'conversations').glob('*.json'))
        value = json.loads(conversation.read_text()); value['title'] = 'edited'
        conversation.write_text(json.dumps(value))
        with self.assertRaises(WorkspaceError): self.capture()
        self.assertEqual(self.ws.head, original)

    def test_capture_from_subdirectory_rejects_untracked_root_file(self):
        nested = self.repo / 'nested'
        nested.mkdir()
        (self.repo / 'untracked-root.txt').write_text('must not be omitted')
        original = self.ws.head
        with chdir(nested), self.assertRaises(CodeSyncError):
            self.capture()
        self.assertEqual(self.ws.head, original)

    def test_stale_missing_commit_and_existing_destination_fail_before_publish(self):
        self.capture()
        conversation = next((self.ws.root / 'conversations').glob('*.json'))
        value = json.loads(conversation.read_text()); value['title'] = 'edited'
        conversation.write_text(json.dumps(value)); self.ws.stage(); self.ws.commit('edit')
        with self.assertRaises(CodeSyncError): restore_pair(self.ws, 'HEAD', self.repo, self.target)
        self.assertFalse(self.target.exists())
        self.capture()
        self.target.mkdir(); (self.target / 'personal').write_text('preserve')
        with self.assertRaises(CodeSyncError): restore_pair(self.ws, 'HEAD', self.repo, self.target)
        self.assertEqual((self.target / 'personal').read_text(), 'preserve')
        wrong = self.root / 'wrong'; wrong.mkdir()
        self.git('init', '-b', 'main', repository=wrong)
        (wrong / 'unrelated').write_text('different')
        self.git('add', '.', repository=wrong); self.git('commit', '-m', 'different', repository=wrong)
        with self.assertRaises(ValueError): restore_pair(self.ws, 'HEAD', wrong, self.root / 'missing')
        self.assertFalse((self.root / 'missing').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture')
    def test_hooks_config_filters_never_execute(self):
        payload = self.root / 'attack'
        marker = self.root / 'executed'
        payload.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        payload.chmod(0o700)
        self.git('config', 'core.fsmonitor', str(payload))
        self.git('config', 'core.hooksPath', str(self.root))
        self.git('config', 'filter.evil.clean', str(payload))
        self.git('config', 'filter.evil.smudge', str(payload))
        self.capture()
        restore_pair(self.ws, 'HEAD', self.repo, self.target)
        self.assertFalse(marker.exists())

    def test_linked_worktree_capture_and_restore(self):
        worktree = self.root / 'linked'
        self.git('worktree', 'add', '-b', 'topic', str(worktree))
        result = capture_checkpoint(self.ws, worktree, 'topic')
        self.assertEqual(result['code_checkpoint']['branch'], 'topic')
        restore_pair(self.ws, 'HEAD', worktree, self.target)
        self.assertEqual((self.target / 'code/app.py').read_text(), 'print("one")\n')

    def test_lfs_is_not_claimed_as_complete(self):
        for newline in (b'\n', b'\r\n'):
            with self.subTest(newline=newline):
                (self.repo / 'large').write_bytes(newline.join([
                    b'version https://git-lfs.github.com/spec/v1',
                    b'oid sha256:' + b'a' * 64, b'size 100', b'']))
                self.git('add', '.'); self.git('commit', '-m', 'lfs')
                with self.assertRaises(CodeSyncError): self.capture()

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink fixture')
    def test_symlink_code_rejected(self):
        (self.repo / 'link').symlink_to('app.py')
        self.git('add', '.'); self.git('commit', '-m', 'link')
        with self.assertRaises(CodeSyncError): self.capture()
        self.assertFalse(self.target.exists())

    def test_native_failure_preserves_pair_and_never_retries(self):
        self.capture()
        with patch('contexthub.codex.CodexClient') as client, patch('contexthub.codex.import_conversation') as importer:
            importer.return_value = {'mode': 'failed', 'thread_id': 'partial', 'verified': False}
            with self.assertRaises(NativeRestoreError) as caught:
                restore_pair(self.ws, 'HEAD', self.repo, self.target, codex=True)
            self.assertEqual(caught.exception.receipt['pair_status'], 'complete')
            receipt = json.loads((self.target / 'receipt.json').read_text())
            self.assertEqual(receipt['native_status'], 'uncertain')
            with self.assertRaises(NativeRestoreError):
                restore_pair(self.ws, 'HEAD', self.repo, self.target, codex=True, resume=True)
            self.assertEqual(importer.call_count, 1)

    def test_native_success_resumes_without_second_import(self):
        self.capture()
        with patch('contexthub.codex.CodexClient'), patch('contexthub.codex.import_conversation') as importer:
            importer.return_value = {'mode': 'timeline', 'thread_id': 'new-thread', 'verified': True}
            result = restore_pair(self.ws, 'HEAD', self.repo, self.target, codex=True)
            self.assertEqual(result['native_status'], 'complete')
            self.assertTrue(restore_pair(self.ws, 'HEAD', self.repo, self.target, codex=True, resume=True)['resumed'])
            self.assertEqual(importer.call_count, 1)

    def test_sha256_git_objects_and_cli_zipapp(self):
        from contexthub.distribution import cli_archive
        other = self.root / 'sha256'
        other.mkdir()
        initialized = subprocess.run(['git', '-C', str(other), 'init', '--object-format=sha256', '-b', 'main'], capture_output=True)
        if initialized.returncode:
            self.skipTest('Git does not support SHA-256')
        self.git('config', 'core.autocrlf', 'false', repository=other)
        (other / 'app.py').write_text('sha256 code\n')
        self.git('add', '.', repository=other); self.git('commit', '-m', 'sha256', repository=other)
        executable = self.root / 'ctx.pyz'; executable.write_bytes(cli_archive())
        first = subprocess.run([sys.executable, str(executable), 'checkpoint', '--code', str(other), '-m', 'sha256 pair', '--json'],
                               cwd=self.ws.root, capture_output=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        checkpoint = json.loads(first.stdout)['code_checkpoint']
        self.assertEqual(len(checkpoint['commit']), 64)
        restored = subprocess.run([sys.executable, str(executable), 'restore', 'HEAD', '--code', str(other), '--to', str(self.target), '--json'],
                                  cwd=self.ws.root, capture_output=True, timeout=30)
        self.assertEqual(restored.returncode, 0, restored.stderr.decode())
        self.assertEqual(self.git('rev-parse', 'HEAD', repository=self.target / 'code'), checkpoint['commit'])
        self.assertEqual((self.target / 'code/app.py').read_text(), 'sha256 code\n')

    def test_failed_publish_cleans_staging_and_source_is_unchanged(self):
        self.capture()
        head = self.git('rev-parse', 'HEAD')
        with patch('contexthub.code_sync._workspace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): restore_pair(self.ws, 'HEAD', self.repo, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob('.ctx-git-*')), [])
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)


if __name__ == '__main__': unittest.main()
