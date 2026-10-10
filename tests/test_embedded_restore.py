"""A downloaded pack restores code and conversations without the source Git."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from contexthub.code_sync import CodeSyncError, NativeRestoreError, restore_embedded
from contexthub.package import build_package
from contexthub.workspace import Workspace, WorkspaceError
from tests.test_code_repository import CONVERSATIONS, repository


class EmbeddedRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = repository({'app.py': b'old working bytes\n'}, bindings=[
            {'conversation_id': 'c1', 'message_id': 'm1', 'kind': 'captured'}])
        self.code = repository({'app.py': b'new uncommitted bytes\n', 'src/new.py': b'new\n'}, previous=self.old)
        self.code['bindings'].append({'conversation_id': 'c1', 'message_id': 'm2',
                                     'snapshot_id': self.code['head'], 'kind': 'captured'})
        self.blob = build_package(CONVERSATIONS, 'Portable project', code=self.code)
        self.ws = Workspace.init(self.root / 'workspace', 'Portable project')
        self.ws.add(self.blob)
        self.ws.commit('save code and conversations')
        self.target = self.root / 'restored'

    def tearDown(self):
        self.temp.cleanup()

    def test_restore_head_without_source_or_git_and_preserve_complete_pack(self):
        with patch('shutil.which', return_value=None):
            result = restore_embedded(self.ws, 'HEAD', self.target)
        self.assertEqual(result['code_snapshot'], self.code['head'])
        self.assertEqual(result['pair_status'], 'complete')
        self.assertEqual((self.target / 'code/app.py').read_bytes(), b'new uncommitted bytes\n')
        self.assertEqual((self.target / 'code/src/new.py').read_bytes(), b'new\n')
        self.assertEqual(Workspace(self.target / 'conversations').package_bytes(), self.blob)
        self.assertFalse((self.target / 'code/.git').exists())
        self.assertTrue(restore_embedded(self.ws, 'HEAD', self.target, resume=True)['resumed'])

    def test_select_older_snapshot_or_message_keeps_full_conversation_package(self):
        result = restore_embedded(self.ws, 'HEAD', self.target, message='m1')
        self.assertEqual(result['code_snapshot'], self.old['head'])
        self.assertEqual(result['conversation_id'], 'c1')
        self.assertEqual((self.target / 'code/app.py').read_bytes(), b'old working bytes\n')
        self.assertFalse((self.target / 'code/src').exists())
        self.assertEqual(Workspace(self.target / 'conversations').package_bytes(), self.blob)
        target = self.root / 'by-snapshot'
        result = restore_embedded(self.ws, 'HEAD', target, snapshot=self.old['head'])
        self.assertEqual(result['code_snapshot'], self.old['head'])
        with self.assertRaises(CodeSyncError):
            restore_embedded(self.ws, 'HEAD', self.target, resume=True)

    def test_missing_binding_code_ambiguous_message_and_bad_selections_fail_before_write(self):
        for options in ({'message': 'unknown'}, {'snapshot': 'a' * 64},
                        {'message': 'm1', 'snapshot': self.old['head']},
                        {'conversation': 'unknown'}, {'mode': 'unsupported'}):
            with self.subTest(options=options), self.assertRaises(CodeSyncError):
                restore_embedded(self.ws, 'HEAD', self.target, **options)
            self.assertFalse(self.target.exists())
        plain = Workspace.init(self.root / 'plain', 'No code')
        plain.add(build_package(CONVERSATIONS, 'No code'))
        plain.commit('plain')
        with self.assertRaises(CodeSyncError):
            restore_embedded(plain, 'HEAD', self.target)
        conversations = copy.deepcopy(CONVERSATIONS)
        conversations.append(dict(copy.deepcopy(CONVERSATIONS[0]), id='c2'))
        code = copy.deepcopy(self.code)
        code['bindings'].append({'conversation_id': 'c2', 'message_id': 'm1',
                                 'snapshot_id': code['head'], 'kind': 'captured'})
        other = Workspace.init(self.root / 'ambiguous', 'Multiple')
        other.add(build_package(conversations, 'Multiple', code=code))
        other.commit('multiple')
        with self.assertRaises(CodeSyncError):
            restore_embedded(other, 'HEAD', self.target, message='m1')
        result = restore_embedded(other, 'HEAD', self.target, message='m1', conversation='c2')
        self.assertEqual(result['code_snapshot'], code['head'])

    def test_existing_destination_is_never_overwritten_and_resume_checks_code(self):
        self.target.mkdir()
        marker = self.target / 'personal'
        marker.write_text('keep')
        with self.assertRaises(CodeSyncError):
            restore_embedded(self.ws, 'HEAD', self.target)
        self.assertEqual(marker.read_text(), 'keep')
        target = self.root / 'actual'
        restore_embedded(self.ws, 'HEAD', target)
        (target / 'code/unexpected').write_text('extra')
        with self.assertRaises(CodeSyncError):
            restore_embedded(self.ws, 'HEAD', target, resume=True)
        (target / 'code/unexpected').unlink()
        (target / 'code/app.py').write_bytes(b'changed')
        with self.assertRaises(CodeSyncError):
            restore_embedded(self.ws, 'HEAD', target, resume=True)
        with self.assertRaises(CodeSyncError):
            restore_embedded(self.ws, 'HEAD', self.root / 'missing', resume=True)

    def test_resume_checks_conversation_content_and_receipt(self):
        restore_embedded(self.ws, 'HEAD', self.target)
        conversation = next((self.target / 'conversations/conversations').glob('*.json'))
        original = conversation.read_bytes()
        value = json.loads(original)
        value['title'] = 'modified'
        conversation.write_text(json.dumps(value))
        with self.assertRaises(WorkspaceError):
            restore_embedded(self.ws, 'HEAD', self.target, resume=True)
        conversation.write_bytes(original)
        path = self.target / 'receipt.json'
        receipt = json.loads(path.read_text())
        receipt['pair_status'] = 'preparing'
        path.write_text(json.dumps(receipt))
        with self.assertRaises(CodeSyncError):
            restore_embedded(self.ws, 'HEAD', self.target, resume=True)

    def test_native_message_import_is_prefix_only_and_is_not_repeated(self):
        with patch('contexthub.codex.CodexClient'), patch('contexthub.codex.import_conversation') as importer:
            importer.return_value = {'mode': 'timeline', 'verified': True, 'thread_id': 'new'}
            result = restore_embedded(self.ws, 'HEAD', self.target, codex=True, message='m1')
            imported = importer.call_args.args[1]
            self.assertEqual([item['id'] for item in imported['messages']], ['m1'])
            self.assertEqual(importer.call_args.args[2], self.target / 'code')
            self.assertEqual(result['native_status'], 'complete')
            self.assertEqual(result['native_scope'], 'through_message')
            self.assertEqual(Workspace(self.target / 'conversations').package_bytes(), self.blob)
            resumed = restore_embedded(self.ws, 'HEAD', self.target, codex=True, message='m1', resume=True)
            self.assertTrue(resumed['resumed'])
            self.assertEqual(importer.call_count, 1)

    def test_native_snapshot_selection_keeps_full_dialogue_and_failure_never_retries(self):
        with patch('contexthub.codex.CodexClient'), patch('contexthub.codex.import_conversation') as importer:
            importer.return_value = {'mode': 'failed', 'verified': False}
            with self.assertRaises(NativeRestoreError) as caught:
                restore_embedded(self.ws, 'HEAD', self.target, codex=True, snapshot=self.old['head'])
            self.assertEqual(len(importer.call_args.args[1]['messages']), 2)
            self.assertEqual(caught.exception.receipt['pair_status'], 'complete')
            self.assertEqual(caught.exception.receipt['native_status'], 'uncertain')
            with self.assertRaises(NativeRestoreError):
                restore_embedded(self.ws, 'HEAD', self.target, codex=True, snapshot=self.old['head'], resume=True)
            self.assertEqual(importer.call_count, 1)

    def test_failed_pair_publication_cleans_staging(self):
        with patch('contexthub.code_sync._workspace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                restore_embedded(self.ws, 'HEAD', self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob('.ctx-git-*')), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX link fixture')
    def test_resume_rejects_symlink_or_executable_mode_changes(self):
        restore_embedded(self.ws, 'HEAD', self.target)
        path = self.target / 'code/app.py'
        path.chmod(0o755)
        with self.assertRaises(CodeSyncError):
            restore_embedded(self.ws, 'HEAD', self.target, resume=True)
        path.unlink()
        path.symlink_to(self.root / 'workspace/library.json')
        with self.assertRaises(WorkspaceError):
            restore_embedded(self.ws, 'HEAD', self.target, resume=True)

    @unittest.skipUnless(shutil.which('git'), 'Git is required for capture')
    def test_real_uncommitted_capture_remains_restorable_after_source_removed(self):
        from contexthub.code_capture import capture_code
        source = self.root / 'source'
        source.mkdir()
        subprocess.run(['git', 'init', '--template=', str(source)], check=True, capture_output=True)
        (source / 'new.py').write_bytes(b'untracked working bytes\n')
        code = capture_code(source, conversations=CONVERSATIONS)
        self.assertEqual(code['snapshots'][0]['git_commit'], '')
        ws = Workspace.init(self.root / 'uncommitted', 'Uncommitted')
        ws.add(build_package(CONVERSATIONS, 'Uncommitted', code=code))
        ws.commit('save without git commit')
        shutil.rmtree(source)
        result = restore_embedded(ws, 'HEAD', self.target)
        self.assertEqual(result['code_snapshot'], code['head'])
        self.assertEqual((self.target / 'code/new.py').read_bytes(), b'untracked working bytes\n')


if __name__ == '__main__':
    unittest.main()
