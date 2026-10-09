"""Branch and merge behavior using synthetic conversations only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from contexthub.package import build_package, read_package
from contexthub.workspace import Workspace, WorkspaceError, MergeConflict


def pack():
    return build_package([{'id': 'conversation', 'title': 'Example', 'messages': [
        {'id': 'one', 'role': 'user', 'content': 'one'},
        {'id': 'two', 'role': 'assistant', 'content': 'two'}]}], 'Synthetic')


class BranchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'workspace'
        self.ws = Workspace.init(self.root, 'Synthetic')
        self.ws.add(pack())
        self.base = self.ws.commit('base')

    def tearDown(self):
        self.temp.cleanup()

    def edit(self, index, value):
        path = next((self.root / 'conversations').glob('*.json'))
        document = json.loads(path.read_text())
        document['messages'][index]['content'] = value
        path.write_text(json.dumps(document))
        self.ws.stage()
        return self.ws.commit(value)

    def remote(self, record, version='v1'):
        return {'url': 'https://example.test', 'package_id': 'package', 'version_id': version,
                'local_commit': record['id'], 'visibility': 'private'}

    def test_branches_isolate_heads_and_remote_and_dirty_switch(self):
        self.ws.mark_pushed(self.remote(self.base), self.base['id'])
        self.ws.switch('feature', create=True)
        self.assertIsNone(self.ws.remote)
        feature = self.edit(0, 'feature')
        self.ws.switch('main')
        self.assertEqual(self.ws.head, self.base['id'])
        self.assertEqual(self.ws.remote['version_id'], 'v1')
        self.assertEqual(read_package(self.ws.package_bytes())['conversations'][0]['messages'][0]['content'], 'one')
        self.ws.switch('feature')
        self.assertEqual(self.ws.head, feature['id'])
        path = next((self.root / 'conversations').glob('*.json'))
        data = json.loads(path.read_text()); data['title'] = 'dirty'; path.write_text(json.dumps(data))
        before = path.read_bytes()
        with self.assertRaises(WorkspaceError):
            self.ws.switch('main')
        self.assertEqual(path.read_bytes(), before)
        for name in ('../escape', 'a//b', 'HEAD', '-bad', 'x.lock', 'a/'):
            with self.subTest(name=name), self.assertRaises(WorkspaceError):
                self.ws.branch(name)

    def test_three_way_merge_preserves_independent_changes_and_two_parents(self):
        self.ws.branch('feature')
        ours = self.edit(0, 'our question')
        self.ws.switch('feature')
        theirs = self.edit(1, 'their answer')
        self.ws.switch('main')
        result = self.ws.merge('feature')
        self.assertFalse(result['fast_forward'])
        self.assertEqual(result['commit']['parent'], ours['id'])
        self.assertEqual(result['commit']['merge_parent'], theirs['id'])
        self.assertEqual([m['content'] for m in read_package(self.ws.package_bytes())['conversations'][0]['messages']],
                         ['our question', 'their answer'])
        self.assertEqual({r['id'] for r in self.ws.history()}, {self.base['id'], ours['id'], theirs['id'], self.ws.head})
        self.assertFalse(self.ws.status()['modified'])
        self.assertFalse(self.ws.merge('feature')['merged'])

    def test_conflict_keeps_worktree_index_heads_and_can_be_explicitly_resolved(self):
        self.ws.branch('feature')
        ours = self.edit(0, 'ours')
        self.ws.switch('feature'); theirs = self.edit(0, 'theirs'); self.ws.switch('main')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p.name != 'lock'}
        with self.assertRaises(MergeConflict) as failure:
            self.ws.merge('feature')
        self.assertTrue(failure.exception.details['conflicts'])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(self.ws.package_bytes('feature'), self.ws.package_bytes(theirs['id']))
        path = next((self.root / 'conversations').glob('*.json'))
        document = json.loads(path.read_text()); document['messages'][0]['content'] = 'resolved'
        path.write_text(json.dumps(document)); self.ws.stage()
        result = self.ws.merge('feature', resolved=True)
        self.assertEqual(result['commit']['parent'], ours['id'])
        self.assertEqual(result['commit']['merge_parent'], theirs['id'])
        self.assertEqual(read_package(self.ws.package_bytes())['conversations'][0]['messages'][0]['content'], 'resolved')

    def test_fast_forward_and_legacy_state_migration(self):
        statepath = self.root / '.contexthub/state.json'
        state = json.loads(statepath.read_text()); state.pop('branch'); state.pop('branches'); state.pop('upload_keys'); state['schema_version'] = 1
        statepath.write_text(json.dumps(state))
        self.assertEqual(self.ws.status()['branch'], 'main')
        self.ws.switch('feature', create=True); commit = self.edit(1, 'change'); self.ws.switch('main')
        self.assertTrue(self.ws.merge('feature')['fast_forward'])
        self.assertEqual(self.ws.head, commit['id'])
        self.assertEqual(json.loads(statepath.read_text())['schema_version'], 2)

    def test_pull_divergence_merges_and_queues_only_merge_result(self):
        self.ws.mark_pushed(self.remote(self.base), self.base['id'])
        self.ws.branch('remote-edit'); local = self.edit(0, 'local')
        self.ws.switch('remote-edit'); remote_commit = self.edit(1, 'remote'); remote_blob = self.ws.package_bytes()
        self.ws.switch('main')
        result = self.ws.integrate_remote(remote_blob, self.remote(remote_commit, 'v2'), expected_head=local['id'])
        self.assertEqual(result['ahead'], 1)
        self.assertEqual(self.ws.remote['version_id'], 'v2')
        self.assertEqual(len(self.ws.pending_commits()), 1)
        self.assertEqual([m['content'] for m in read_package(self.ws.package_bytes())['conversations'][0]['messages']], ['local', 'remote'])
        self.ws.mark_pushed(self.remote(self.ws.log()[0], 'v3'), self.ws.head)
        self.assertEqual(self.ws.status()['ahead'], 0)

    def test_pull_conflict_retains_remote_on_incoming_branch_without_updating_checkpoint(self):
        self.ws.mark_pushed(self.remote(self.base), self.base['id'])
        self.ws.branch('remote-edit'); local = self.edit(0, 'local')
        self.ws.switch('remote-edit'); remote_commit = self.edit(0, 'remote'); remote_blob = self.ws.package_bytes()
        self.ws.switch('main'); before = self.ws.package_bytes(); checkpoint = self.ws.remote
        with self.assertRaises(MergeConflict) as failure:
            self.ws.integrate_remote(remote_blob, self.remote(remote_commit, 'v2'), expected_head=local['id'])
        self.assertEqual(self.ws.head, local['id']); self.assertEqual(self.ws.remote, checkpoint)
        self.assertEqual(self.ws.package_bytes(), before)
        incoming = failure.exception.details['branch']
        self.assertEqual(self.ws.package_bytes(incoming), remote_blob)

    def test_old_conflict_resolution_then_new_remote_does_not_stall_checkpoint(self):
        self.ws.mark_pushed(self.remote(self.base), self.base['id'])
        self.ws.branch('remote-edit'); self.edit(0, 'local')
        self.ws.switch('remote-edit'); first = self.edit(0, 'remote1'); blob1 = self.ws.package_bytes()
        second = self.edit(0, 'remote2'); blob2 = self.ws.package_bytes(); self.ws.switch('main')
        incoming = []
        for blob, record, version in ((blob1, first, 'v2'), (blob2, second, 'v3')):
            with self.assertRaises(MergeConflict) as failure:
                self.ws.integrate_remote(blob, self.remote(record, version))
            incoming.append(failure.exception.details['branch'])
        self.ws.merge(incoming[0], resolved=True)
        self.assertEqual(self.ws.remote['version_id'], 'v2')
        with self.assertRaises(MergeConflict) as failure:
            self.ws.integrate_remote(blob2, self.remote(second, 'v3'))
        fresh_incoming = failure.exception.details['branch']
        self.assertNotEqual(fresh_incoming, incoming[1])
        self.ws.merge(fresh_incoming, resolved=True)
        self.assertEqual(self.ws.remote['version_id'], 'v3')
        self.assertEqual(self.ws.status()['ahead'], 1)
        self.assertEqual(self.ws.package_bytes(incoming[1]), blob2)

    def test_switch_failure_rolls_back_files_and_active_branch(self):
        self.ws.switch('feature', create=True); self.edit(1, 'changed'); self.ws.switch('main')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p.name != 'lock'}
        with mock.patch.object(self.ws, '_write_state', side_effect=RuntimeError('simulated state failure')):
            with self.assertRaises(RuntimeError):
                self.ws.switch('feature')
        self.assertEqual(self.ws.status()['branch'], 'main')
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse((self.root / '.contexthub/journal.json').exists())

    def test_upload_key_is_persisted_per_branch_and_legacy_migration(self):
        original = self.ws.upload_key()
        self.assertEqual(Workspace(self.root).upload_key(), original)
        self.ws.switch('feature', create=True)
        other = self.ws.upload_key()
        self.assertNotEqual(original, other)
        self.ws.switch('main'); self.assertEqual(self.ws.upload_key(), original)
        path = self.root / '.contexthub/state.json'
        state = json.loads(path.read_text()); state.pop('upload_keys'); path.write_text(json.dumps(state))
        migrated = self.ws.upload_key()
        self.assertEqual(Workspace(self.root).upload_key(), migrated)
        self.assertEqual(json.loads(path.read_text())['upload_keys']['main'], migrated)
