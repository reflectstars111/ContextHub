import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from contexthub.package import build_package, read_package
from contexthub.workspace import Workspace, WorkspaceError, _filename


def pack(text='first', identifier='c1', title='Imported library'):
    return build_package([{'id': identifier, 'title': 'Discussion',
                           'source': {'platform': 'claude'}, 'messages': [
                               {'id': 'm1', 'role': 'user', 'content': text}]}], title)


def remote(version='v1', checkpoint=None):
    return {'url': 'https://example.test', 'package_id': 'package1',
            'version_id': version, 'local_commit': checkpoint, 'visibility': 'private'}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name)
        self.workspace = Workspace.init(self.parent / 'project', 'Local library')
        self.root = self.workspace.root

    def tearDown(self):
        self.temp.cleanup()

    def file(self, identifier='c1'):
        return self.root / _filename(identifier)

    def edit(self, text, identifier='c1'):
        path = self.file(identifier)
        value = json.loads(path.read_text())
        value['messages'][0]['content'] = text
        path.write_text(json.dumps(value))

    def first(self):
        self.workspace.add(pack())
        return self.workspace.commit('First import')

    def test_init_find_preserves_project_files_and_never_scans_them(self):
        existing = self.parent / 'existing'
        existing.mkdir()
        (existing / 'credentials.txt').write_text('do-not-upload')
        workspace = Workspace.init(existing, 'Title')
        nested = existing / 'subfolder'
        nested.mkdir()
        self.assertEqual(Workspace.find(nested).root, existing)
        workspace.add(pack())
        workspace.commit('Add selected conversation')
        self.assertNotIn(b'do-not-upload', workspace.package_bytes())
        self.assertEqual((existing / 'credentials.txt').read_text(), 'do-not-upload')
        self.assertEqual(workspace.status()['conversations'], 1)
        with self.assertRaises(WorkspaceError):
            Workspace.init(existing, 'Must not overwrite')

    def test_empty_and_staged_commit_status_and_noop(self):
        self.assertEqual(self.workspace.status()['ahead'], 0)
        self.assertFalse(self.workspace.status()['modified'])
        with self.assertRaises(WorkspaceError):
            self.workspace.commit('empty')
        self.workspace.add(pack())
        self.assertTrue(self.workspace.status()['staged'])
        self.assertFalse(self.workspace.status()['modified'])
        first = self.workspace.commit('First import')
        self.assertEqual(self.workspace.head, first['id'])
        self.assertFalse(self.workspace.status()['staged'])
        self.assertFalse(self.workspace.status()['modified'])
        self.workspace.stage()
        self.assertFalse(self.workspace.status()['staged'])
        self.assertFalse(self.workspace.status()['modified'])
        with self.assertRaises(WorkspaceError):
            self.workspace.commit('no changes')

    def test_commit_only_staged_data_and_log_is_immutable(self):
        self.workspace.add(pack())
        staged = self.workspace.package_bytes('INDEX')
        self.edit('unstaged work')
        first = self.workspace.commit('Initial staged text')
        self.assertEqual(self.workspace.package_bytes(), staged)
        self.assertTrue(self.workspace.status()['modified'])
        self.assertEqual(self.workspace.diff()['summary']['messages_modified'], 1)
        self.workspace.stage()
        self.assertEqual(self.workspace.diff(staged=True)['summary']['messages_modified'], 1)
        self.assertFalse(self.workspace.status()['modified'])
        second = self.workspace.commit('Second text')
        self.assertEqual(second['parent'], first['id'])
        self.assertEqual(self.workspace.package_bytes(first['id']), staged)
        self.assertEqual([item['id'] for item in self.workspace.log()], [second['id'], first['id']])
        self.assertEqual([item['id'] for item in self.workspace.pending_commits()], [first['id'], second['id']])
        self.assertFalse(self.workspace.status()['modified'])

    def test_replacement_requires_explicit_choice_and_preserves_audit(self):
        first = self.first()
        before = self.workspace.package_bytes()
        with self.assertRaises(WorkspaceError):
            self.workspace.add(pack('changed'))
        self.assertEqual(self.workspace.package_bytes(), before)
        self.workspace.add(pack())
        self.assertFalse(self.workspace.status()['staged'])
        self.workspace.add(pack('changed'), replace=True)
        package = read_package(self.workspace.package_bytes('INDEX'))
        self.assertIn('revision_conversation_edited', [item['kind'] for item in package['conversations'][0]['annotations']])
        second = self.workspace.commit('Change text')
        self.assertNotEqual(first['id'], second['id'])
        self.assertFalse(self.workspace.status()['modified'])
        self.workspace.stage()
        self.assertFalse(self.workspace.status()['staged'])

    def test_add_merges_conversations_and_never_uploads_extra_json(self):
        self.first()
        (self.root / 'conversations' / 'extra.json').write_text('{"secret":true}')
        self.workspace.add(pack('second conversation', 'c2'))
        package = read_package(self.workspace.package_bytes('INDEX'))
        self.assertEqual([item['id'] for item in package['conversations']], ['c1', 'c2'])
        self.assertEqual(package['library']['title'], 'Local library')
        self.assertTrue((self.root / 'conversations' / 'extra.json').exists())
        self.assertFalse(self.workspace.status()['modified'])

    def test_invalid_working_json_reports_dirty_without_losing_index(self):
        self.first()
        old = self.workspace.package_bytes()
        self.file().write_text('{ broken')
        state = self.workspace.status()
        self.assertTrue(state['modified'])
        self.assertIn('error', state)
        with self.assertRaises(ValueError):
            self.workspace.stage()
        self.assertEqual(self.workspace.package_bytes(), old)

    def test_deleting_managed_conversation_requires_explicit_library_change(self):
        self.workspace.add(pack())
        self.workspace.add(pack('second', 'c2'))
        self.workspace.commit('two conversations')
        library_path = self.root / 'library.json'
        library = json.loads(library_path.read_text())
        library['conversation_ids'] = ['c2']
        library_path.write_text(json.dumps(library))
        self.workspace.stage()
        self.assertFalse(self.file('c1').exists())
        self.assertEqual(self.workspace.status()['conversations'], 1)
        package = read_package(self.workspace.package_bytes('INDEX'))
        kinds = [item['kind'] for item in package['conversations'][0]['annotations']]
        self.assertIn('revision_conversation_removed', kinds)

    def test_clone_tracks_remote_and_pull_preserves_untracked_files(self):
        self.workspace.checkout_remote(pack(), remote())
        first = self.workspace.head
        self.assertEqual(self.workspace.remote['local_commit'], first)
        self.assertFalse(self.workspace.status()['modified'])
        self.assertEqual(self.workspace.pending_commits(), [])
        (self.root / 'notes.txt').write_text('untracked notes')
        self.workspace.checkout_remote(pack('new remote'), remote('v2'), expected_head=first)
        self.assertEqual(self.workspace.log()[0]['parent'], first)
        self.assertEqual((self.root / 'notes.txt').read_text(), 'untracked notes')
        self.assertEqual(self.workspace.status()['ahead'], 0)
        self.assertEqual(self.workspace.remote['version_id'], 'v2')

    def test_pull_rejects_dirty_staged_and_unpushed_content(self):
        self.workspace.checkout_remote(pack(), remote())
        original = self.workspace.head
        self.edit('dirty')
        with self.assertRaises(WorkspaceError):
            self.workspace.checkout_remote(pack('remote'), remote('v2'))
        self.workspace.stage()
        with self.assertRaises(WorkspaceError):
            self.workspace.checkout_remote(pack('remote'), remote('v2'))
        self.workspace.commit('local commit')
        with self.assertRaises(WorkspaceError):
            self.workspace.checkout_remote(pack('remote'), remote('v2'))
        self.assertNotEqual(self.workspace.head, original)
        self.assertEqual(read_package(self.workspace.package_bytes())['conversations'][0]['messages'][0]['content'], 'dirty')

    def test_push_checkpoints_advance_one_commit_at_a_time(self):
        first = self.first()
        self.edit('second')
        self.workspace.stage()
        second = self.workspace.commit('second')
        self.workspace.mark_pushed(remote('v1', first['id']), expected_head=second['id'])
        self.assertEqual(self.workspace.status()['ahead'], 1)
        self.workspace.mark_pushed(remote('v2', second['id']), expected_head=second['id'])
        self.assertEqual(self.workspace.status()['ahead'], 0)
        with self.assertRaises(WorkspaceError):
            self.workspace.mark_pushed(remote('v1', first['id']), expected_head=second['id'])
        with self.assertRaises(WorkspaceError):
            self.workspace.mark_pushed(remote('v3', second['id']), expected_head=first['id'])
        value = self.workspace.remote
        value['url'] = 'https://changed.test'
        self.assertEqual(self.workspace.remote['url'], 'https://example.test')

    def test_checkout_cannot_overwrite_untracked_name_collision(self):
        self.file().write_text('precious untracked file')
        with self.assertRaises(WorkspaceError):
            self.workspace.checkout_remote(pack(), remote())
        self.assertEqual(self.file().read_text(), 'precious untracked file')
        self.assertIsNone(self.workspace.head)

    def test_symlink_and_hardlink_working_files_are_rejected(self):
        self.first()
        outside = self.parent / 'outside.txt'
        outside.write_text('protected')
        self.file().unlink()
        self.file().symlink_to(outside)
        with self.assertRaises(WorkspaceError):
            self.workspace.stage()
        self.assertEqual(outside.read_text(), 'protected')
        self.file().unlink()
        os.link(outside, self.file())
        with self.assertRaises(WorkspaceError):
            self.workspace.stage()
        self.assertEqual(outside.read_text(), 'protected')

    def test_symlink_directories_and_metadata_are_rejected(self):
        alias = self.parent / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            Workspace.find(alias)
        external = self.parent / 'external'
        external.mkdir()
        (self.root / 'conversations').rmdir()
        (self.root / 'conversations').symlink_to(external, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            self.workspace.add(pack())
        self.assertEqual(list(external.iterdir()), [])

    def test_metadata_symlink_cannot_overwrite_external_file(self):
        external = self.parent / 'external.json'
        external.write_text('protected')
        state = self.root / '.contexthub' / 'state.json'
        state.unlink()
        state.symlink_to(external)
        with self.assertRaises(WorkspaceError):
            self.workspace.status()
        self.assertEqual(external.read_text(), 'protected')

    def test_object_tampering_is_detected(self):
        first = self.first()
        blob = self.root / '.contexthub' / 'objects' / (first['sha256'] + '.contextpack')
        blob.write_bytes(pack('tampered'))
        with self.assertRaises(WorkspaceError):
            self.workspace.package_bytes()

    def test_lock_is_reentrant_but_other_instances_and_processes_fail(self):
        with self.workspace.locked():
            with self.workspace.locked():
                self.assertIsNone(self.workspace.head)
            with self.assertRaises(WorkspaceError):
                Workspace(self.root).status()
            result = subprocess.run([sys.executable, '-c',
                                     'from contexthub.workspace import Workspace; '
                                     'import sys; Workspace(sys.argv[1]).status()', str(self.root)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('工作区正被另一个操作使用', result.stderr)
        self.assertIsNone(Workspace(self.root).head)

    def test_write_failure_rolls_back_exact_original_bytes_and_state(self):
        self.first()
        self.edit('user edited with custom formatting')
        before = {path.relative_to(self.root).as_posix(): path.read_bytes()
                  for path in [self.root / 'library.json', self.file(), self.root / '.contexthub' / 'state.json']}
        from contexthub import workspace as module
        real_atomic = module._atomic
        failed = False
        def failing(fd, name, data, **kwargs):
            nonlocal failed
            if name == 'library.json' and not failed:
                failed = True
                raise OSError(28, 'disk full')
            return real_atomic(fd, name, data, **kwargs)
        with patch.object(module, '_atomic', side_effect=failing):
            with self.assertRaises(WorkspaceError):
                self.workspace.stage()
        for name, data in before.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertFalse((self.root / '.contexthub' / 'journal.json').exists())
        self.assertTrue(self.workspace.status()['modified'])

    def test_interrupted_worktree_transaction_recovers_on_next_open(self):
        self.first()
        before = self.file().read_bytes()
        old_head = self.workspace.head
        from contexthub import workspace as module
        real_atomic = module._atomic
        def interrupt(fd, name, data, **kwargs):
            if name == 'library.json':
                raise KeyboardInterrupt('simulated process interruption')
            return real_atomic(fd, name, data, **kwargs)
        with patch.object(module, '_atomic', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.workspace.add(pack('replacement'), replace=True)
        self.assertTrue((self.root / '.contexthub' / 'journal.json').exists())
        reopened = Workspace.find(self.root)
        self.assertEqual(reopened.head, old_head)
        self.assertEqual(self.file().read_bytes(), before)
        self.assertFalse(reopened.status()['modified'])

    def test_completed_transaction_cleanup_interruption_does_not_undo_commit(self):
        with patch.object(self.workspace, '_cleanup_journal', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.workspace.checkout_remote(pack(), remote())
        reopened = Workspace.find(self.root)
        self.assertIsNotNone(reopened.head)
        self.assertEqual(reopened.remote['version_id'], 'v1')
        self.assertFalse(reopened.status()['modified'])
        self.assertFalse((self.root / '.contexthub' / 'journal.json').exists())

    def test_recovery_refuses_to_erase_edits_made_after_interruption(self):
        self.first()
        from contexthub import workspace as module
        real_atomic = module._atomic
        def interrupt(fd, name, data, **kwargs):
            if name == 'library.json':
                raise KeyboardInterrupt('simulated interruption')
            return real_atomic(fd, name, data, **kwargs)
        with patch.object(module, '_atomic', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.workspace.add(pack('replacement'), replace=True)
        self.edit('precious edit after interruption')
        data = self.file().read_bytes()
        with self.assertRaisesRegex(WorkspaceError, '中断后文件又被修改'):
            Workspace.find(self.root)
        self.assertEqual(self.file().read_bytes(), data)
        self.assertTrue((self.root / '.contexthub' / 'journal.json').exists())

    def test_invalid_state_is_a_bounded_error_and_remote_switch_is_rejected(self):
        self.workspace.checkout_remote(pack(), remote())
        switched = remote('v2')
        switched['url'] = 'https://other.test'
        with self.assertRaises(WorkspaceError):
            self.workspace.checkout_remote(pack('other'), switched)
        state_path = self.root / '.contexthub' / 'state.json'
        state = json.loads(state_path.read_text())
        state['managed'] = [{}]
        state_path.write_text(json.dumps(state))
        with self.assertRaises(WorkspaceError):
            self.workspace.status()

    def test_valid_noncanonical_zip_is_clean_after_clone_and_noop_stage(self):
        source = io.BytesIO(pack())
        output = io.BytesIO()
        with zipfile.ZipFile(source) as old, zipfile.ZipFile(output, 'w') as new:
            for name in reversed(old.namelist()):
                new.writestr(name, old.read(name))
        data = output.getvalue()
        self.assertNotEqual(data, pack())
        read_package(data)
        self.workspace.checkout_remote(data, remote())
        self.assertEqual(self.workspace.package_bytes(), data)
        self.assertFalse(self.workspace.status()['modified'])
        self.workspace.stage()
        self.assertFalse(self.workspace.status()['staged'])
        self.assertFalse(self.workspace.status()['modified'])
        self.workspace.add(pack())
        self.assertFalse(self.workspace.status()['staged'])

    def test_protected_metadata_edits_are_dirty_until_explicit_staging(self):
        self.workspace.checkout_remote(pack(), remote())
        original = json.loads(self.file().read_text())
        mutations = [
            lambda item: item['source'].update(platform='deepseek'),
            lambda item: item['messages'][0].update(provenance='human_authored'),
            lambda item: item['messages'][0].update(transformations=['forged']),
            lambda item: item.update(annotations=[{'kind': 'revision_forged', 'text': 'forged'}]),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                item = json.loads(json.dumps(original))
                mutate(item)
                self.file().write_text(json.dumps(item))
                self.assertTrue(self.workspace.status()['modified'])
                with self.assertRaises(WorkspaceError):
                    self.workspace.checkout_remote(pack('new remote'), remote('v2'))
                self.assertEqual(json.loads(self.file().read_text()), item)
                self.workspace.stage()
                self.assertFalse(self.workspace.status()['modified'])
                self.assertFalse(self.workspace.status()['staged'])
        self.file().write_text(json.dumps(original, indent=4, ensure_ascii=True))
        self.assertFalse(self.workspace.status()['modified'])

    def test_deleted_audit_annotations_block_pull(self):
        self.first()
        self.edit('new text')
        self.workspace.stage()
        commit = self.workspace.commit('Audited edit')
        self.workspace.mark_pushed(remote('v2', commit['id']), commit['id'])
        item = json.loads(self.file().read_text())
        self.assertTrue(item['annotations'])
        item.pop('annotations')
        self.file().write_text(json.dumps(item))
        self.assertTrue(self.workspace.status()['modified'])
        with self.assertRaises(WorkspaceError):
            self.workspace.checkout_remote(pack('new remote'), remote('v3'))
        self.assertNotIn('annotations', json.loads(self.file().read_text()))

    def test_concurrent_editor_save_before_stage_or_add_is_preserved(self):
        self.first()
        for operation in ('stage', 'add'):
            with self.subTest(operation=operation):
                self.edit('selected snapshot')
                prior_index = self.workspace.package_bytes('INDEX')
                save_object = self.workspace._save_object
                def save_then_edit(data):
                    digest = save_object(data)
                    self.edit('new external edit')
                    return digest
                with patch.object(self.workspace, '_save_object', side_effect=save_then_edit):
                    with self.assertRaisesRegex(WorkspaceError, '操作期间被其他程序修改'):
                        if operation == 'stage':
                            self.workspace.stage()
                        else:
                            self.workspace.add(pack('other conversation', 'c2'))
                self.assertEqual(json.loads(self.file().read_text())['messages'][0]['content'], 'new external edit')
                self.assertEqual(self.workspace.package_bytes('INDEX'), prior_index)
                self.assertFalse(self.file('c2').exists())

    def test_concurrent_editor_save_mid_transition_preserves_it_and_rolls_back_other_files(self):
        self.first()
        self.edit('staging edit')
        previous_conversation = self.file().read_bytes()
        previous_index = self.workspace.package_bytes('INDEX')
        library_path = self.root / 'library.json'
        from contexthub import workspace as module
        real_atomic = module._atomic
        injected = False
        def edit_during_replace(fd, name, data, **kwargs):
            nonlocal injected
            if name == 'library.json' and not injected:
                injected = True
                value = json.loads(library_path.read_text())
                value['title'] = 'Editor saved concurrently'
                library_path.write_text(json.dumps(value))
            return real_atomic(fd, name, data, **kwargs)
        with patch.object(module, '_atomic', side_effect=edit_during_replace):
            with self.assertRaisesRegex(WorkspaceError, '操作期间被其他程序修改'):
                self.workspace.stage()
        self.assertEqual(json.loads(library_path.read_text())['title'], 'Editor saved concurrently')
        self.assertEqual(self.file().read_bytes(), previous_conversation)
        self.assertEqual(self.workspace.package_bytes('INDEX'), previous_index)
        self.assertFalse((self.root / '.contexthub' / 'journal.json').exists())


if __name__ == '__main__':
    unittest.main()
