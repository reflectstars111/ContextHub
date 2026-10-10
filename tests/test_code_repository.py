"""Portable code snapshots retain exact bytes and honest message associations."""
import base64
import copy
import hashlib
import io
import unittest
from unittest.mock import patch
import zipfile

from contexthub.package import PackageError, build_package, read_package
from contexthub.code_repository import (code_diff, code_summary, prune_bindings,
                                        snapshot_id, validate_code)
from contexthub.merge import merge_packages
from contexthub.revisions import build_revision, compare_packages
from contexthub.checkpoints import attach_checkpoint


CONVERSATIONS = [{'id': 'c1', 'messages': [
    {'id': 'm1', 'role': 'user', 'content': 'Implement this'},
    {'id': 'm2', 'role': 'assistant', 'content': 'Done'}]}]


def repository(files=None, previous=None, bindings=None, time='2026-10-10T08:00:00Z'):
    files = {'app.py': b'print("hello")\n'} if files is None else files
    result = copy.deepcopy(previous) if previous else {
        'schema_version': 1, 'snapshots': [], 'blobs': {}, 'bindings': []}
    entries = []
    for path, data in sorted(files.items()):
        digest = hashlib.sha256(data).hexdigest()
        result['blobs'][digest] = base64.b64encode(data).decode('ascii')
        entries.append({'path': path, 'sha256': digest, 'size': len(data), 'mode': '100644'})
    snap = {'parents': [result['head']] if previous else [], 'created_at': time,
            'git_commit': '', 'branch': 'main', 'files': entries}
    snap['id'] = snapshot_id(snap)
    result['snapshots'].append(snap)
    result['head'] = snap['id']
    if bindings is not None:
        result['bindings'] = [dict(item, snapshot_id=result['head']) for item in bindings]
    return result


def package(code=None):
    return read_package(build_package(CONVERSATIONS, 'Code and conversations', code=code))


class CodeRepositoryTests(unittest.TestCase):
    def test_package_roundtrip_preserves_exact_binary_bytes_and_old_shape(self):
        self.assertEqual(set(package()), {'manifest', 'library', 'conversations', 'compatibility'})
        code = repository({'app.py': b'print("hello")\n', 'image.bin': bytes(range(256))})
        blob = build_package(CONVERSATIONS, 'Code and conversations', code=code)
        self.assertEqual(read_package(blob)['code'], code)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            self.assertIn('code/index.json', archive.namelist())
            for digest, data in code['blobs'].items():
                self.assertEqual(archive.read('code/blobs/' + digest), base64.b64decode(data))
        self.assertNotIn('blobs', code_summary(code))

    def test_snapshot_hash_is_stable_across_capture_time(self):
        code = repository()
        snap = code['snapshots'][0]
        self.assertEqual(snapshot_id(snap), snapshot_id(dict(snap, created_at='2026-10-10T09:00:00Z')))

    def test_checkpoint_attachment_preserves_portable_code(self):
        code = repository()
        blob = build_package(CONVERSATIONS, 'Code and conversations', code=code)
        linked = attach_checkpoint(blob, {'repository_url': '', 'commit': 'a' * 40,
            'tree': 'b' * 40, 'branch': 'main', 'captured_at': '2026-10-10T08:00:00Z',
            'verification': 'local_clean'})
        self.assertEqual(read_package(linked)['code'], code)

    def test_revision_keeps_code_prunes_deleted_message_binding_and_compares_code(self):
        code = repository(bindings=[{'conversation_id': 'c1', 'message_id': 'm2', 'kind': 'captured'}])
        base = package(code)
        cs = copy.deepcopy(base['conversations'])
        cs[0]['messages'].pop()
        changed = read_package(build_revision(base, {'conversations': cs}))
        self.assertEqual(changed['code']['bindings'], [])
        self.assertEqual(changed['code']['head'], code['head'])
        new_code = repository({'app.py': b'print("goodbye")\n'}, previous=code)
        after = read_package(build_revision(base, {'conversations': base['conversations'], 'code': new_code}))
        diff = compare_packages(base, after)['code']
        self.assertEqual(diff['summary']['files_modified'], 1)
        self.assertIn('-print("hello")', diff['files'][0]['patch'])
        self.assertIn('+print("goodbye")', diff['files'][0]['patch'])
        self.assertNotIn('code', read_package(build_revision(base, {'conversations': base['conversations'], 'code': None})))

    def test_rejects_corrupt_blob_missing_parent_and_unknown_message(self):
        code = repository()
        bad = copy.deepcopy(code)
        bad['blobs'][next(iter(bad['blobs']))] = base64.b64encode(b'changed').decode('ascii')
        with self.assertRaises(PackageError):
            validate_code(bad)
        bad = copy.deepcopy(code)
        bad['snapshots'][0]['parents'] = ['a' * 64]
        bad['snapshots'][0]['id'] = bad['head'] = snapshot_id(bad['snapshots'][0])
        with self.assertRaises(PackageError):
            validate_code(bad)
        bad = repository(bindings=[{'conversation_id': 'c1', 'message_id': 'missing', 'kind': 'captured'}])
        with self.assertRaises(PackageError):
            build_package(CONVERSATIONS, 'x', code=bad)

    def test_rejects_forged_identity_size_links_or_noncanonical_blob(self):
        for modify in (
                lambda code: code['snapshots'][0].update(id='b' * 64),
                lambda code: code['snapshots'][0]['files'][0].update(size=True),
                lambda code: code['snapshots'][0]['files'][0].update(mode='120000'),
                lambda code: code.update(schema_version=True),
                lambda code: code.update(head='c' * 64),
                lambda code: code['snapshots'][0].update(created_at='2026-99-10T00:00:00Z'),
                lambda code: code['snapshots'][0].update(branch='main\nsecret'),
                lambda code: code['blobs'].update({next(iter(code['blobs'])): '%%%'})):
            code = repository()
            modify(code)
            # Recompute identity when the case is a forged content claim rather
            # than an explicitly forged ID, so validation tests the claim too.
            if code['snapshots'][0]['id'] == code['head']:
                code['snapshots'][0]['id'] = code['head'] = snapshot_id(code['snapshots'][0])
            with self.subTest(code=code), self.assertRaises(PackageError):
                validate_code(code)

    def test_limits_are_checked_on_raw_data_and_index(self):
        code = repository({'binary': b'12345'})
        for name, limit in (('MAX_CODE_BLOB_BYTES', 4), ('MAX_CODE_BYTES', 4),
                            ('MAX_CODE_INDEX_BYTES', 20), ('MAX_CODE_SNAPSHOTS', 0),
                            ('MAX_CODE_FILES', 0)):
            with self.subTest(name=name), patch('contexthub.code_repository.' + name, limit), self.assertRaises(PackageError):
                validate_code(code)

    def test_repeated_blob_references_cannot_exceed_snapshot_expansion_limit(self):
        code = repository({'first.bin': b'12345', 'second.bin': b'12345'})
        self.assertEqual(len(code['blobs']), 1)
        with patch('contexthub.code_repository.MAX_CODE_BYTES', 9), self.assertRaises(PackageError):
            validate_code(code)
        with patch('contexthub.code_repository.MAX_CODE_BYTES', 10):
            self.assertEqual(validate_code(code), code)

    def test_unreferenced_blobs_duplicate_bindings_and_wrong_snapshots_rejected(self):
        code = repository(bindings=[{'conversation_id': 'c1', 'message_id': 'm1', 'kind': 'captured'}])
        bad = copy.deepcopy(code)
        bad['bindings'].append(copy.deepcopy(bad['bindings'][0]))
        with self.assertRaises(PackageError):
            validate_code(bad, CONVERSATIONS)
        bad = copy.deepcopy(code)
        bad['bindings'][0]['snapshot_id'] = 'a' * 64
        with self.assertRaises(PackageError):
            validate_code(bad, CONVERSATIONS)
        bad = copy.deepcopy(code)
        bad['blobs'][hashlib.sha256(b'orphan').hexdigest()] = base64.b64encode(b'orphan').decode('ascii')
        with self.assertRaises(PackageError):
            validate_code(bad, CONVERSATIONS)
        self.assertEqual(prune_bindings(code, [])['bindings'], [])
        self.assertEqual(len(code['bindings']), 1)

    def test_rejects_unsafe_and_colliding_paths(self):
        for path in ('../secret', '/tmp/file', '.git/config', 'a/.contexthub/state.json',
                     'C:/file', 'dir\\file', 'CON', 'file.', 'a//b', 'a/./b'):
            with self.subTest(path=path), self.assertRaises(PackageError):
                validate_code(repository({path: b'x'}))
        for paths in (('A.py', 'a.py'), ('a', 'a/b'), ('Dir/a', 'dir/b')):
            with self.subTest(paths=paths), self.assertRaises(PackageError):
                validate_code(repository({path: b'x' for path in paths}))

    def test_file_merge_keeps_independent_edits_and_conflicts_same_file(self):
        code = repository({'a': b'old\n', 'b': b'old\n'})
        base = package(code)
        left = package(repository({'a': b'left\n', 'b': b'old\n'}, previous=code))
        right = package(repository({'a': b'old\n', 'b': b'right\n'}, previous=code))
        result = merge_packages(base, left, right)
        self.assertEqual(result['conflicts'], [])
        merged = read_package(result['package'])['code']
        head = next(s for s in merged['snapshots'] if s['id'] == merged['head'])
        self.assertEqual(head['parents'], [left['code']['head'], right['code']['head']])
        self.assertEqual({f['path']: base64.b64decode(merged['blobs'][f['sha256']]) for f in head['files']},
                         {'a': b'left\n', 'b': b'right\n'})
        right = package(repository({'a': b'right\n', 'b': b'old\n'}, previous=code))
        conflict = merge_packages(base, left, right)
        self.assertIsNone(conflict['package'])
        self.assertTrue(any(item['path'] == '/code/files/a' for item in conflict['conflicts']))

    def test_code_diff_binary_mode_deletion_and_selected_snapshot(self):
        initial = repository({'a': b'old\n', 'binary': b'\0old', 'removed': b'gone\n'})
        after = repository({'a': b'new\n', 'binary': b'\0new', 'added': b'hello\n'}, previous=initial)
        result = code_diff(None, after, snapshot=after['head'])
        self.assertEqual(result['before_snapshot'], initial['head'])
        self.assertEqual(result['summary'], {'files_added': 1, 'files_removed': 1,
            'files_modified': 2, 'additions': 2, 'deletions': 2})
        binary = next(item for item in result['files'] if item['path'] == 'binary')
        self.assertTrue(binary['binary'])
        self.assertEqual(binary['patch'], '')
        changed = copy.deepcopy(initial)
        changed['snapshots'][0]['files'][0]['mode'] = '100755'
        changed['snapshots'][0]['id'] = changed['head'] = snapshot_id(changed['snapshots'][0])
        mode_diff = code_diff(initial, changed)['files'][0]
        self.assertEqual(mode_diff['before_mode'], '100644')
        self.assertEqual(mode_diff['after_mode'], '100755')

    def test_code_diff_bounds_details_and_reports_all_changed_files(self):
        initial = repository({'large': b'old\n'})
        after = repository(dict({'large': b'line\n' * 9000},
                                **{f'file-{number:04d}': b'new\n' for number in range(220)}), previous=initial)
        result = code_diff(initial, after)
        self.assertEqual(result['summary']['files_added'], 220)
        self.assertEqual(result['summary']['files_modified'], 1)
        self.assertEqual(len(result['files']), 200)
        self.assertTrue(result['truncated'])
        large_only = repository({'large': b'line\n' * 9000}, previous=initial)
        self.assertTrue(code_diff(initial, large_only)['files'][0]['truncated'])

    def test_code_merge_detects_delete_edit_and_competing_message_bindings(self):
        initial = repository({'a': b'old\n'})
        base = package(initial)
        left = package(repository({'a': b'left\n'}, previous=initial))
        self.assertIsNone(merge_packages(base, left, package())['package'])
        binding = [{'conversation_id': 'c1', 'message_id': 'm1', 'kind': 'captured'}]
        left = package(repository({'a': b'left\n'}, previous=initial, bindings=binding))
        right = package(repository({'a': b'right\n'}, previous=initial, bindings=binding))
        result = merge_packages(base, left, right)
        self.assertTrue(any(item['kind'] == 'binding' for item in result['conflicts']))

    def test_archive_manifest_cannot_hide_a_tampered_code_index(self):
        import json
        from contexthub.package import _json_bytes
        code = repository()
        blob = build_package(CONVERSATIONS, 'x', code=code)
        with zipfile.ZipFile(io.BytesIO(blob)) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        index = json.loads(entries['code/index.json'])
        index['snapshots'][0]['files'][0]['size'] += 1
        index['snapshots'][0]['id'] = index['head'] = snapshot_id(index['snapshots'][0])
        entries['code/index.json'] = _json_bytes(index)
        manifest = json.loads(entries['manifest.json'])
        for record in manifest['files']:
            if record['path'] == 'code/index.json':
                record.update(size=len(entries['code/index.json']),
                              sha256=hashlib.sha256(entries['code/index.json']).hexdigest())
        entries['manifest.json'] = _json_bytes(manifest)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as target:
            for name, data in entries.items():
                target.writestr(name, data)
        with self.assertRaises(PackageError):
            read_package(output.getvalue())


if __name__ == '__main__':
    unittest.main()
