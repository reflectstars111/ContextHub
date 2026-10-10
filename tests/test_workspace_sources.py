"""Local source bindings must not compromise Git-style staging or portability."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from contexthub.package import MAX_CONVERSATIONS, build_package, read_package
from contexthub.workspace import Workspace, WorkspaceError, _filename


def conversation(thread='synthetic-thread', texts=('first',), exported_at='2026-10-10T00:00:00Z'):
    return {'id': thread, 'title': 'Synthetic Codex conversation',
            'source': {'provider': 'codex', 'thread_id': thread, 'exported_at': exported_at},
            'messages': [{'id': f'm{index}', 'role': 'user', 'content': text,
                          'provenance': {'provider': 'codex', 'thread_id': thread}}
                         for index, text in enumerate(texts)]}


def source(thread='synthetic-thread'):
    return {'provider': 'codex', 'thread_id': thread,
            'conversation_id': 'sha256:' + hashlib.sha256(thread.encode()).hexdigest()}


def pack(*conversations):
    return build_package(list(conversations) or [conversation()], 'Captured conversation')


class WorkspaceSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = Workspace.init(self.root / 'workspace', 'Project conversations')

    def tearDown(self):
        self.temporary.cleanup()

    def capture(self, texts=('first',), *, timestamp='2026-10-10T00:00:00Z', replace=False):
        return self.workspace.capture(pack(conversation(texts=texts, exported_at=timestamp)),
                                      [source()], replace=replace)

    def file(self, thread='synthetic-thread'):
        return self.workspace.root / _filename(source(thread)['conversation_id'])

    def edit(self, text):
        item = json.loads(self.file().read_text())
        item['messages'][0]['content'] = text
        self.file().write_text(json.dumps(item))

    def state_bytes(self):
        return (self.workspace.root / '.contexthub' / 'state.json').read_bytes()

    def test_capture_persists_binding_and_stages_without_creating_commit(self):
        result = self.capture()
        self.assertTrue(result['staged'])
        self.assertIsNone(self.workspace.head)
        self.assertEqual(result['sources'], [source()])
        reopened = Workspace.find(self.workspace.root)
        self.assertEqual(reopened.sources(), [source()])
        returned = reopened.sources()
        returned[0]['thread_id'] = 'changed outside'
        self.assertEqual(reopened.sources(), [source()])

    def test_timestamp_only_refresh_has_no_diff_before_and_after_commit(self):
        self.capture()
        first = self.workspace.package_bytes('INDEX')
        self.capture(timestamp='2026-10-11T00:00:00Z')
        self.assertEqual(self.workspace.package_bytes('INDEX'), first)
        self.workspace.commit('Initial capture')
        self.capture(timestamp='2026-10-12T00:00:00Z')
        self.assertFalse(self.workspace.status()['staged'])
        self.assertFalse(self.workspace.status()['modified'])
        self.assertEqual(self.workspace.package_bytes('INDEX'), first)

    def test_refresh_tracks_final_revision_representation_and_appends_only_once(self):
        self.capture()
        self.workspace.commit('Initial capture')
        self.capture(('first', 'second'))
        self.workspace.commit('Second capture')
        self.capture(('first', 'second'), timestamp='2026-10-11T00:00:00Z')
        self.assertFalse(self.workspace.status()['staged'])
        self.capture(('first', 'second', 'third'))
        result = read_package(self.workspace.package_bytes('INDEX'))['conversations'][0]
        self.assertEqual([item['content'] for item in result['messages']], ['first', 'second', 'third'])
        self.assertIn('source_claimed', result['messages'][1]['transformations'])

    def test_refresh_keeps_other_conversations_and_library_edits(self):
        self.capture()
        self.workspace.add(build_package([{'id': 'manual', 'messages': [
            {'role': 'assistant', 'content': 'Keep this'}]}], 'Imported'))
        library = self.workspace.root / 'library.json'
        data = json.loads(library.read_text())
        data['title'] = 'User chosen title'
        library.write_text(json.dumps(data))
        self.capture(('first', 'second'))
        result = read_package(self.workspace.package_bytes('INDEX'))
        self.assertEqual(result['library']['title'], 'User chosen title')
        self.assertEqual([item['id'] for item in result['conversations']],
                         [source()['conversation_id'], 'manual'])

    def test_manual_edits_block_refresh_even_if_source_unchanged_or_edit_committed(self):
        self.capture()
        self.workspace.commit('Initial capture')
        self.edit('Manually rewritten')
        before = self.state_bytes()
        for texts in (('first',), ('first', 'second')):
            with self.assertRaisesRegex(WorkspaceError, '--no-refresh'):
                self.capture(texts)
            self.assertEqual(self.state_bytes(), before)
            self.assertEqual(json.loads(self.file().read_text())['messages'][0]['content'], 'Manually rewritten')
        self.workspace.stage()
        self.workspace.commit('Keep manual change')
        with self.assertRaisesRegex(WorkspaceError, '--no-refresh'):
            self.capture()
        self.capture(replace=True)
        self.assertEqual(json.loads(self.file().read_text())['messages'][0]['content'], 'first')

    def test_manual_source_metadata_edit_is_not_hidden_by_revision_normalization(self):
        self.capture()
        self.workspace.commit('Initial capture')
        item = json.loads(self.file().read_text())
        item['source']['exported_at'] = 'Manually changed claim'
        self.file().write_text(json.dumps(item))
        with self.assertRaisesRegex(WorkspaceError, '--no-refresh'):
            self.capture()
        self.assertEqual(json.loads(self.file().read_text())['source']['exported_at'], 'Manually changed claim')

    def test_deleted_binding_does_not_silently_reappear(self):
        self.capture()
        self.workspace.add(pack(conversation('other-thread')))
        self.workspace.commit('Two conversations')
        library = self.workspace.root / 'library.json'
        data = json.loads(library.read_text())
        data['conversation_ids'].remove(source()['conversation_id'])
        library.write_text(json.dumps(data))
        self.workspace.stage()
        with self.assertRaisesRegex(WorkspaceError, '--no-refresh'):
            self.capture()
        self.assertFalse(self.file().exists())
        self.capture(replace=True)
        self.assertTrue(self.file().exists())

    def test_new_binding_conflict_requires_explicit_replacement(self):
        self.workspace.add(pack(conversation(texts=('Existing imported text',))))
        original = self.workspace.package_bytes('INDEX')
        with self.assertRaisesRegex(WorkspaceError, '--replace'):
            self.capture()
        self.assertEqual(self.workspace.sources(), [])
        self.assertEqual(self.workspace.package_bytes('INDEX'), original)
        self.capture(replace=True)
        self.assertEqual(self.workspace.sources(), [source()])

    def test_source_identifiers_must_match_the_entire_incoming_package(self):
        candidates = [[], [dict(source(), provider='other')], [dict(source(), thread_id='wrong')],
                      [dict(source(), thread_id='x' * 513)], [dict(source(), thread_id='bad\nthread')],
                      [dict(source(), extra='unrecognized')], [source(), source()],
                      [source('other-thread')], [source()] * (MAX_CONVERSATIONS + 1)]
        original = self.state_bytes()
        for bindings in candidates:
            with self.subTest(bindings=bindings[:2]), self.assertRaises(WorkspaceError):
                self.workspace.capture(pack(), bindings)
            self.assertEqual(self.state_bytes(), original)
        with self.assertRaises(WorkspaceError):
            self.workspace.capture(pack(conversation(), conversation('other-thread')), [source()])
        mismatched = conversation()
        mismatched['source']['provider'] = 'claude'
        with self.assertRaises(WorkspaceError):
            self.workspace.capture(pack(mismatched), [source()])

    def test_binding_changes_are_published_with_worktree_or_rolled_back_together(self):
        self.capture()
        self.workspace.commit('Initial capture')
        before = self.state_bytes()
        before_file = self.file().read_bytes()
        incoming = pack(conversation(texts=('first', 'second')), conversation('other-thread'))
        with patch.object(self.workspace, '_write_state', side_effect=OSError('simulated publication failure')):
            with self.assertRaises(WorkspaceError):
                self.workspace.capture(incoming, [source(), source('other-thread')])
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.file().read_bytes(), before_file)
        self.assertFalse(self.file('other-thread').exists())
        self.assertEqual(self.workspace.sources(), [source()])
        self.assertFalse((self.workspace.root / '.contexthub' / 'journal.json').exists())

    def test_concurrent_editor_save_is_preserved_without_updating_binding(self):
        self.capture()
        before = self.state_bytes()
        save_object = self.workspace._save_object
        def save_then_edit(data):
            result = save_object(data)
            self.edit('Concurrent editor text')
            return result
        with patch.object(self.workspace, '_save_object', side_effect=save_then_edit):
            with self.assertRaisesRegex(WorkspaceError, '操作期间'):
                self.capture(('first', 'second'))
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(json.loads(self.file().read_text())['messages'][0]['content'], 'Concurrent editor text')

    def test_branch_bindings_are_independent_and_copy_when_branching(self):
        self.capture()
        self.workspace.commit('Initial capture')
        self.workspace.switch('experiment', create=True)
        self.assertEqual(self.workspace.sources(), [source()])
        self.capture(('first', 'experiment'))
        self.workspace.commit('Experiment capture')
        self.workspace.capture(pack(conversation('other-thread')), [source('other-thread')])
        self.workspace.commit('Track another source')
        self.workspace.switch('main')
        self.assertEqual(self.workspace.sources(), [source()])
        self.capture(timestamp='2026-10-11T00:00:00Z')
        self.assertFalse(self.workspace.status()['staged'])
        self.workspace.switch('experiment')
        self.capture(('first', 'experiment'), timestamp='2026-10-11T00:00:00Z')
        self.assertFalse(self.workspace.status()['staged'])
        self.assertEqual(len(self.workspace.sources()), 2)

    def test_exported_package_and_fresh_clone_do_not_include_local_bindings(self):
        self.capture()
        self.workspace.commit('Initial capture')
        blob = self.workspace.package_bytes()
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for name in archive.namelist():
                value = archive.read(name)
                self.assertNotIn(b'synthetic-thread', value)
                self.assertNotIn(b'workspace_digest', value)
                self.assertNotIn(b'capture_digest', value)
        clone = Workspace.init(self.root / 'clone', 'Clone')
        clone.checkout_remote(blob, {'url': 'https://example.test', 'package_id': 'synthetic',
                                    'version_id': 'v1', 'local_commit': None, 'visibility': 'private'})
        self.assertEqual(clone.sources(), [])
        self.assertEqual(clone.package_bytes(), blob)

    def test_legacy_workspace_without_sources_is_read_without_a_write(self):
        path = self.workspace.root / '.contexthub' / 'state.json'
        value = json.loads(path.read_text())
        value.pop('sources', None)
        path.write_text(json.dumps(value))
        before = path.read_bytes()
        self.assertEqual(self.workspace.sources(), [])
        self.assertEqual(path.read_bytes(), before)
        self.capture()
        self.assertEqual(self.workspace.sources(), [source()])

    def test_invalid_persisted_binding_is_rejected_before_workspace_mutation(self):
        self.capture()
        path = self.workspace.root / '.contexthub' / 'state.json'
        original = json.loads(path.read_text())
        corruptions = [None, {'missing-branch': {}}, {'main': []}, {'main': {'bogus': {}}}]
        for sources in corruptions:
            value = copy.deepcopy(original)
            value['sources'] = sources
            path.write_text(json.dumps(value))
            before = path.read_bytes()
            with self.subTest(sources=sources), self.assertRaises(WorkspaceError):
                self.workspace.sources()
            self.assertEqual(path.read_bytes(), before)
        path.write_text(json.dumps(original))


if __name__ == '__main__':
    unittest.main()
