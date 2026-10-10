import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from contexthub.checkpoints import (attach_checkpoint, checkpoint_status,
                                   conversation_digest, validate_checkpoint)
from contexthub.merge import merge_packages
from contexthub.package import PackageError, _json_bytes, build_package, read_package
from contexthub.revisions import build_revision, compare_packages
from contexthub.workspace import Workspace, WorkspaceError, _filename


def original():
    return build_package([{'id': 'c1', 'title': '实现登录', 'messages': [
        {'id': 'm1', 'role': 'user', 'content': '保留会话'},
        {'id': 'm2', 'role': 'assistant', 'content': '已经实现'}]}], '项目对话')


def info(commit='a' * 40):
    return {'repository_url': 'https://github.com/example/project.git',
            'commit': commit, 'tree': 'b' * len(commit), 'branch': 'main',
            'captured_at': '2026-10-10T08:00:00Z', 'verification': 'local_clean'}


def rebuild(package, **changes):
    library = dict(package['library'], **changes)
    return build_package(package['conversations'], library['title'], library['description'],
                         source=library['source'], code_checkpoint=library.get('code_checkpoint'))


class CheckpointTests(unittest.TestCase):
    def test_binding_roundtrip_stable_and_metadata_only_revision(self):
        blob = original()
        base = read_package(blob)
        linked_blob = attach_checkpoint(blob, info())
        linked = read_package(linked_blob)
        self.assertEqual(checkpoint_status(base), 'none')
        self.assertEqual(checkpoint_status(linked), 'matched')
        self.assertEqual(conversation_digest(base), conversation_digest(linked))
        self.assertEqual(rebuild(linked), linked_blob)
        self.assertEqual(base['conversations'], linked['conversations'])
        self.assertEqual(attach_checkpoint(blob, info()), linked_blob)
        payload = {'conversations': base['conversations'],
                   'code_checkpoint': linked['library']['code_checkpoint']}
        self.assertEqual(build_revision(base, payload), linked_blob)
        differences = compare_packages(base, linked)
        self.assertEqual(differences['library_changes'][0]['field'], 'code_checkpoint')
        self.assertFalse(any(differences['summary'].values()))
        # The older format without a checkpoint retains its exact serialization.
        self.assertEqual(rebuild(base), blob)

    def test_edits_keep_historical_link_but_binding_becomes_stale(self):
        linked = read_package(attach_checkpoint(original(), info()))
        payload = {'conversations': copy.deepcopy(linked['conversations'])}
        payload['conversations'][0]['messages'][1]['content'] = '新增 OAuth'
        edited = read_package(build_revision(linked, payload))
        self.assertEqual(edited['library']['code_checkpoint'], linked['library']['code_checkpoint'])
        self.assertEqual(checkpoint_status(edited), 'stale')
        self.assertEqual(checkpoint_status(read_package(rebuild(linked, title='新名称'))), 'stale')
        self.assertEqual(checkpoint_status(read_package(attach_checkpoint(rebuild(edited), info('c' * 40)))), 'matched')
        self.assertEqual(checkpoint_status(read_package(build_revision(linked, {
            'conversations': linked['conversations'], 'code_checkpoint': None}))), 'none')

    def test_invalid_checkpoint_fields_and_credentials_are_rejected(self):
        checkpoint = read_package(attach_checkpoint(original(), info()))['library']['code_checkpoint']
        cases = [dict(checkpoint, schema_version=True), dict(checkpoint, schema_version=2),
                 dict(checkpoint, secret='token'), dict(checkpoint, commit='main'),
                 dict(checkpoint, tree='b' * 64), dict(checkpoint, commit='A' * 40),
                 dict(checkpoint, conversation_digest='x' * 64),
                 dict(checkpoint, repository_url='https://token@github.com/a/b'),
                 dict(checkpoint, repository_url='https://github.com/a/b?token=secret'),
                 dict(checkpoint, repository_url='file:///private/repository'),
                 dict(checkpoint, repository_url='https://github.com/a/b#secret'),
                 dict(checkpoint, repository_url='https://github.com\\@evil.test/a'),
                 dict(checkpoint, repository_url='https://github.com:999999/a'),
                 dict(checkpoint, captured_at='2026-99-10T08:00:00Z'),
                 dict(checkpoint, captured_at='2026-10-10T08:00:00+08:00'),
                 dict(checkpoint, branch='main\npassword'),
                 dict(checkpoint, verification='trusted')]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(PackageError):
                build_package(read_package(original())['conversations'], '项目对话', code_checkpoint=case)
        self.assertEqual(validate_checkpoint(dict(checkpoint, repository_url=''))['repository_url'], '')
        self.assertEqual(checkpoint_status(read_package(attach_checkpoint(original(), info('a' * 64)))), 'matched')

    def test_archive_reader_validates_checkpoint_even_with_matching_file_hash(self):
        blob = attach_checkpoint(original(), info())
        with zipfile.ZipFile(io.BytesIO(blob)) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        library = json.loads(entries['library.json'])
        library['code_checkpoint']['commit'] = 'not-an-oid'
        entries['library.json'] = _json_bytes(library)
        manifest = json.loads(entries['manifest.json'])
        for record in manifest['files']:
            if record['path'] == 'library.json':
                record.update(size=len(entries['library.json']), sha256=hashlib.sha256(entries['library.json']).hexdigest())
        entries['manifest.json'] = _json_bytes(manifest)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as target:
            for name, data in entries.items():
                target.writestr(name, data)
        with self.assertRaises(PackageError):
            read_package(output.getvalue())

    def test_merge_retains_binding_and_conflicts_on_different_code_checkpoints(self):
        base = read_package(original())
        target = read_package(attach_checkpoint(original(), info()))
        merged = read_package(merge_packages(base, target, base)['package'])
        self.assertEqual(checkpoint_status(merged), 'matched')
        source = copy.deepcopy(base)
        source['conversations'][0]['messages'][1]['content'] = '进一步修改'
        source = read_package(rebuild(source))
        merged = read_package(merge_packages(base, target, source)['package'])
        self.assertEqual(merged['library']['code_checkpoint'], target['library']['code_checkpoint'])
        self.assertEqual(checkpoint_status(merged), 'stale')
        other = read_package(attach_checkpoint(original(), info('c' * 40)))
        conflict = merge_packages(base, target, other)
        self.assertIsNone(conflict['package'])
        self.assertTrue(any(item['path'] == '/library/code_checkpoint' for item in conflict['conflicts']))

    def test_merge_never_synthesizes_a_checkpoint_from_independent_fields(self):
        base = read_package(attach_checkpoint(original(), info()))
        checkpoint = base['library']['code_checkpoint']
        ours = read_package(rebuild(base, code_checkpoint=dict(checkpoint, commit='c' * 40)))
        theirs = read_package(rebuild(base, code_checkpoint=dict(checkpoint, tree='d' * 40)))
        result = merge_packages(base, ours, theirs)
        self.assertIsNone(result['package'])
        self.assertEqual(result['conflicts'][0]['path'], '/library/code_checkpoint')
        # Removal is also atomic, and must conflict with a simultaneous relink.
        cleared = read_package(rebuild(base, code_checkpoint=None))
        self.assertIsNone(merge_packages(base, cleared, theirs)['package'])
        self.assertEqual(checkpoint_status(read_package(merge_packages(base, cleared, base)['package'])), 'none')

    def test_unknown_source_payload_cannot_replace_or_rebind_checkpoint(self):
        linked = read_package(attach_checkpoint(original(), info()))
        payload = {'conversations': linked['conversations'],
                   'source': {'code_checkpoint': {'commit': 'c' * 40}, 'secret': 'token'},
                   'library': {'code_checkpoint': None}}
        self.assertEqual(read_package(build_revision(linked, payload)), linked)
        with self.assertRaises(PackageError):
            attach_checkpoint(original(), dict(info(), source={'secret': 'token'}))


class WorkspaceCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = Workspace.init(self.root / 'workspace', '项目对话')
        self.workspace.add(original())
        self.first = self.workspace.commit('导入对话')

    def tearDown(self):
        self.temp.cleanup()

    def test_record_immutable_history_branch_roundtrip_and_edit_staleness(self):
        ws = self.workspace
        previous = ws.package_bytes()
        recorded = ws.record_code_checkpoint(info(), '关联代码')
        self.assertNotEqual(recorded['id'], self.first['id'])
        self.assertEqual(ws.package_bytes(self.first['id']), previous)
        self.assertFalse(ws.status()['modified'])
        self.assertFalse(ws.status()['staged'])
        self.assertEqual(next(branch['head'] for branch in ws.branches() if branch['current']), recorded['id'])
        self.assertEqual(checkpoint_status(read_package(ws.package_bytes())), 'matched')
        ws.branch('before', self.first['id'])
        ws.switch('before')
        self.assertEqual(checkpoint_status(read_package(ws.package_bytes())), 'none')
        ws.switch('main')
        path = ws.root / _filename('c1')
        conversation = json.loads(path.read_text(encoding='utf-8'))
        conversation['messages'][1]['content'] = '继续修改'
        path.write_text(json.dumps(conversation), encoding='utf-8')
        with self.assertRaises(WorkspaceError):
            ws.record_code_checkpoint(info(), '拒绝覆盖修改')
        ws.stage()
        with self.assertRaises(WorkspaceError):
            ws.record_code_checkpoint(info(), '拒绝覆盖暂存')
        ws.commit('继续对话')
        self.assertEqual(checkpoint_status(read_package(ws.package_bytes())), 'stale')
        ws.record_code_checkpoint(info('c' * 40), '重新关联代码')
        self.assertEqual(checkpoint_status(read_package(ws.package_bytes())), 'matched')

    def test_add_keeps_incoming_checkpoint_and_rejects_silent_replacement(self):
        ws = self.workspace
        linked_blob = attach_checkpoint(original(), info())
        ws.add(linked_blob)
        ws.commit('导入代码关联')
        self.assertEqual(checkpoint_status(read_package(ws.package_bytes())), 'matched')
        before = ws.package_bytes()
        with self.assertRaises(WorkspaceError):
            ws.add(attach_checkpoint(original(), info('c' * 40)), replace=True)
        self.assertEqual(before, ws.package_bytes())
        fresh = Workspace.init(self.root / 'fresh', '项目对话')
        fresh.add(linked_blob)
        fresh.commit('导入检查点')
        self.assertEqual(fresh.package_bytes(), linked_blob)
        self.assertFalse(fresh.status()['modified'])

    def test_replace_changes_contents_without_falsely_rebinding_code(self):
        ws = self.workspace
        ws.record_code_checkpoint(info(), '关联代码')
        linked = read_package(ws.package_bytes())
        modified = copy.deepcopy(linked)
        modified['conversations'][0]['messages'][0]['content'] = '改变需求'
        ws.add(rebuild(modified, code_checkpoint=None), replace=True)
        ws.commit('导入新需求')
        updated = read_package(ws.package_bytes())
        self.assertEqual(updated['library']['code_checkpoint'], linked['library']['code_checkpoint'])
        self.assertEqual(checkpoint_status(updated), 'stale')
        other = Workspace.init(self.root / 'different', '另一个项目')
        other.add(attach_checkpoint(original(), info()))
        other.commit('导入对话')
        self.assertEqual(checkpoint_status(read_package(other.package_bytes())), 'stale')

    def test_worktree_clear_is_explicit_and_null_or_malformed_link_is_rejected(self):
        ws = self.workspace
        ws.record_code_checkpoint(info(), '关联代码')
        path = ws.root / 'library.json'
        library = json.loads(path.read_text(encoding='utf-8'))
        for invalid in (None, {}, {'source': 'token'}):
            path.write_bytes(_json_bytes(dict(library, code_checkpoint=invalid)))
            with self.subTest(invalid=invalid), self.assertRaises(PackageError):
                ws.stage()
        library.pop('code_checkpoint')
        path.write_bytes(_json_bytes(library))
        ws.stage()
        ws.commit('移除关联')
        self.assertEqual(checkpoint_status(read_package(ws.package_bytes())), 'none')
        self.assertFalse(ws.status()['modified'])

    def test_failed_checkpoint_publication_rolls_back_worktree_and_head(self):
        ws = self.workspace
        before = ws.package_bytes()
        files = {name: (ws.root / name).read_bytes() for name in Workspace._files(read_package(before))}
        with patch.object(ws, '_write_state', side_effect=OSError('simulated write failure')):
            with self.assertRaises(WorkspaceError):
                ws.record_code_checkpoint(info(), '关联失败')
        self.assertEqual(ws.head, self.first['id'])
        self.assertEqual(ws.package_bytes(), before)
        self.assertEqual(files, {name: (ws.root / name).read_bytes() for name in files})
        self.assertFalse((ws.root / '.contexthub' / 'journal.json').exists())
        self.assertFalse(ws.status()['modified'])
        self.assertFalse(ws.status()['staged'])

    def test_editor_race_refuses_checkpoint_without_overwriting_edit(self):
        ws = self.workspace
        save = ws._save_commit
        path = ws.root / 'library.json'

        def save_then_edit(*args, **kwargs):
            record = save(*args, **kwargs)
            library = json.loads(path.read_text(encoding='utf-8'))
            library['title'] = '正在编辑的标题'
            path.write_bytes(_json_bytes(library))
            return record

        with patch.object(ws, '_save_commit', side_effect=save_then_edit):
            with self.assertRaises(WorkspaceError):
                ws.record_code_checkpoint(info(), '关联失败')
        self.assertEqual(ws.head, self.first['id'])
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['title'], '正在编辑的标题')
        self.assertEqual(checkpoint_status(read_package(ws.package_bytes())), 'none')


if __name__ == '__main__':
    unittest.main()
