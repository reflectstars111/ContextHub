"""Bounded, inert code snapshots carried with portable conversations.

Snapshot hashes attest to exact file contents and recorded ancestry, not who
authored the code. A ``captured`` binding records a save-time association; it
does not claim that a historic message was generated against those bytes.
No function in this module executes code or writes extracted archive paths.
"""
from __future__ import annotations

import base64
import binascii
import copy
import datetime
import difflib
import hashlib
import re

from .package import PackageError, _json_bytes, _text
from .safe_fs import _windows_component


MAX_CODE_SNAPSHOTS = 128
MAX_CODE_FILES = 10_000
MAX_CODE_BLOB_BYTES = 16 * 1024 * 1024
MAX_CODE_BYTES = 64 * 1024 * 1024
MAX_CODE_INDEX_BYTES = 8 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_FIELDS = {'schema_version', 'head', 'snapshots', 'blobs', 'bindings'}
_SNAPSHOT_FIELDS = {'id', 'parents', 'created_at', 'git_commit', 'branch', 'files'}
_FILE_FIELDS = {'path', 'sha256', 'size', 'mode'}
_BINDING_FIELDS = {'conversation_id', 'message_id', 'snapshot_id', 'kind'}


def _digest(value, label):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise PackageError(f'{label} must be a lowercase SHA-256 digest')
    return value


def snapshot_id(snapshot):
    """Hash immutable snapshot identity, excluding its capture timestamp."""
    if not isinstance(snapshot, dict) or not {'parents', 'git_commit', 'branch', 'files'} <= snapshot.keys():
        raise PackageError('Code snapshot identity fields are missing')
    return hashlib.sha256(_json_bytes({key: snapshot[key] for key in
                                      ('parents', 'git_commit', 'branch', 'files')})).hexdigest()


def validate_code_path(path):
    """Validate one portable relative regular-file path, without touching disk."""
    _text(path, 'code file path', 4096, nonempty=True)
    parts = path.split('/')
    if len(parts) > 64 or '\\' in path or any(ord(char) == 127 for char in path):
        raise PackageError('Code file path contains unsafe components')
    for component in parts:
        if component.casefold() in {'.git', '.contexthub'} or len(component.encode('utf-8')) > 255:
            raise PackageError('Code file path contains a protected or oversized component')
        try:
            _windows_component(component)
        except OSError as exc:
            raise PackageError('Code file path is not a safe portable relative path') from exc
    return path


def _timestamp(value):
    _text(value, 'code snapshot created_at', 64)
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value):
            raise ValueError('not UTC ISO')
        datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise PackageError('Code snapshot created_at must be an ISO UTC timestamp ending in Z') from exc


def blob_bytes(encoded):
    """Decode one canonical, bounded base64 blob."""
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_CODE_BLOB_BYTES + 2) // 3):
        raise PackageError('Code blob exceeds its encoded size limit')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PackageError('Code blobs must contain canonical base64') from exc
    if len(data) > MAX_CODE_BLOB_BYTES or base64.b64encode(data).decode('ascii') != encoded:
        raise PackageError('Code blob is oversized or uses noncanonical base64')
    return data


def validate_code(code, conversations=None):
    """Verify every content hash, path, parent edge and message reference."""
    if not isinstance(code, dict) or set(code) != _FIELDS:
        raise PackageError('Code repository must contain exactly the supported fields')
    if type(code['schema_version']) is not int or code['schema_version'] != 1:
        raise PackageError('Unsupported code repository schema_version')
    _digest(code['head'], 'code head')
    snapshots, blobs, bindings = code['snapshots'], code['blobs'], code['bindings']
    if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= MAX_CODE_SNAPSHOTS:
        raise PackageError('Code repository requires 1–128 snapshots')
    if not isinstance(blobs, dict) or len(blobs) > MAX_CODE_FILES:
        raise PackageError('Code repository permits at most 10,000 blobs')
    if not isinstance(bindings, list) or len(bindings) > 400_000:
        raise PackageError('Code message bindings exceed their count limit')
    index = {key: value for key, value in code.items() if key != 'blobs'}
    if len(_json_bytes(index)) > MAX_CODE_INDEX_BYTES:
        raise PackageError('Code index exceeds 8 MiB')
    lengths, total = {}, 0
    for digest, encoded in blobs.items():
        _digest(digest, 'code blob key')
        data = blob_bytes(encoded)
        total += len(data)
        if total > MAX_CODE_BYTES:
            raise PackageError('Code blobs exceed 64 MiB in total')
        if hashlib.sha256(data).hexdigest() != digest:
            raise PackageError('Code blob SHA-256 mismatch')
        lengths[digest] = len(data)
    by_id, referenced = {}, set()
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
            raise PackageError('Code snapshot must contain exactly the supported fields')
        ident = _digest(snapshot['id'], 'code snapshot id')
        if ident in by_id or ident != snapshot_id(snapshot):
            raise PackageError('Duplicate code snapshot or snapshot identity mismatch')
        by_id[ident] = snapshot
        parents = snapshot['parents']
        if not isinstance(parents, list) or len(parents) > 2:
            raise PackageError('Code snapshot permits at most two parents')
        for parent in parents:
            _digest(parent, 'code snapshot parent')
        if len(set(parents)) != len(parents) or ident in parents:
            raise PackageError('Code snapshot parents must be distinct and not self-referencing')
        _timestamp(snapshot['created_at'])
        commit = snapshot['git_commit']
        if not isinstance(commit, str) or (commit and not _OID.fullmatch(commit)):
            raise PackageError('Code source Git commit must be empty or a complete object ID')
        branch = _text(snapshot['branch'], 'code branch', 1024)
        if any(ord(char) < 32 or ord(char) == 127 for char in branch):
            raise PackageError('Code branch contains control characters')
        files = snapshot['files']
        if not isinstance(files, list) or len(files) > MAX_CODE_FILES:
            raise PackageError('Code snapshot permits at most 10,000 files')
        names, directories, spellings, previous = set(), set(), {}, None
        expanded_bytes = 0
        for item in files:
            if not isinstance(item, dict) or set(item) != _FILE_FIELDS:
                raise PackageError('Code file must contain exactly path, sha256, size and mode')
            path = validate_code_path(item['path'])
            if previous is not None and path <= previous:
                raise PackageError('Code file paths must be unique and sorted')
            previous = path
            pieces = path.split('/')
            for number in range(1, len(pieces) + 1):
                prefix = '/'.join(pieces[:number])
                folded = prefix.casefold()
                if folded in spellings and spellings[folded] != prefix:
                    raise PackageError('Code paths contain a case-insensitive collision')
                spellings[folded] = prefix
                if number < len(pieces):
                    if folded in names:
                        raise PackageError('Code path is both a file and directory')
                    directories.add(folded)
            folded = path.casefold()
            if folded in names or folded in directories:
                raise PackageError('Code path is both a file and directory or duplicated')
            names.add(folded)
            digest = _digest(item['sha256'], 'code file sha256')
            if type(item['size']) is not int or digest not in lengths or item['size'] != lengths[digest]:
                raise PackageError('Code file size or blob reference is inconsistent')
            # Multiple paths may share one blob, but restoring them still
            # consumes their combined size on disk. Bound both representations.
            expanded_bytes += item['size']
            if expanded_bytes > MAX_CODE_BYTES:
                raise PackageError('Expanded code snapshot exceeds 64 MiB')
            if item['mode'] not in ('100644', '100755'):
                raise PackageError('Code snapshots support only regular files, not links or submodules')
            referenced.add(digest)
    if referenced != set(blobs):
        raise PackageError('Every code blob must be referenced by a snapshot')
    if code['head'] not in by_id:
        raise PackageError('Code head references a missing snapshot')
    visiting, visited = set(), set()

    def visit(ident):
        if ident in visiting or ident not in by_id:
            raise PackageError('Code snapshot ancestry is cyclic or references a missing parent')
        if ident in visited:
            return
        visiting.add(ident)
        for parent in by_id[ident]['parents']:
            visit(parent)
        visiting.remove(ident)
        visited.add(ident)

    for ident in by_id:
        visit(ident)
    known = None if conversations is None else {
        (conversation['id'], message['id']) for conversation in conversations
        for message in conversation['messages']}
    seen = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != _BINDING_FIELDS:
            raise PackageError('Code message binding has unsupported fields')
        for key in ('conversation_id', 'message_id'):
            _text(binding[key], 'code binding ' + key, 512, nonempty=True)
        _digest(binding['snapshot_id'], 'code binding snapshot_id')
        key = binding['conversation_id'], binding['message_id']
        if key in seen or (known is not None and key not in known):
            raise PackageError('Code message binding is duplicated or references a missing message')
        if binding['kind'] != 'captured' or binding['snapshot_id'] not in by_id:
            raise PackageError('Code message binding has unsupported kind or missing snapshot')
        seen.add(key)
    return copy.deepcopy(code)


def code_summary(code):
    """Return metadata only; no encoded or decoded source-file contents."""
    return None if code is None else copy.deepcopy({key: value for key, value in code.items() if key != 'blobs'})


def prune_bindings(code, conversations):
    """Drop associations to removed messages while retaining code history."""
    if code is None:
        return None
    result = validate_code(code)
    known = {(conversation['id'], message['id']) for conversation in conversations
             for message in conversation['messages']}
    result['bindings'] = [binding for binding in result['bindings']
                          if (binding['conversation_id'], binding['message_id']) in known]
    return result


def _snapshot(code, ident=None):
    if code is None:
        return None
    ident = ident or code['head']
    for snapshot in code['snapshots']:
        if snapshot['id'] == ident:
            return snapshot
    raise PackageError('Unknown code snapshot')


def code_diff(before_code, after_code, snapshot=None):
    """Compare exact files, with bounded text patches and explicit truncation.

The caller supplies validated repositories. Selecting ``snapshot`` with no
before repository compares that snapshot to its first parent in after_code.
Large/binary files receive hashes and sizes instead of text expansion.
"""
    old = _snapshot(before_code)
    new = _snapshot(after_code, snapshot)
    if snapshot is not None and before_code is None and new is not None and new['parents']:
        before_code = after_code
        old = _snapshot(after_code, new['parents'][0])
    old_files = {item['path']: item for item in old['files']} if old else {}
    new_files = {item['path']: item for item in new['files']} if new else {}
    result = {'changed': (old != new if snapshot is not None else before_code != after_code),
              'before_snapshot': old['id'] if old else None,
              'after_snapshot': new['id'] if new else None,
              'summary': {'files_added': 0, 'files_removed': 0, 'files_modified': 0,
                          'additions': 0, 'deletions': 0}, 'files': [], 'truncated': False}
    budget = 128 * 1024
    for path in sorted(old_files.keys() | new_files.keys()):
        before, after = old_files.get(path), new_files.get(path)
        if before == after:
            continue
        status = 'added' if before is None else 'removed' if after is None else 'modified'
        result['summary']['files_' + status] += 1
        if len(result['files']) >= 200:
            result['truncated'] = True
            continue
        entry = {'path': path, 'status': status,
                 'before_sha256': before['sha256'] if before else None,
                 'after_sha256': after['sha256'] if after else None,
                 'before_size': before['size'] if before else 0,
                 'after_size': after['size'] if after else 0,
                 'before_mode': before['mode'] if before else None,
                 'after_mode': after['mode'] if after else None,
                 'binary': False, 'patch': '', 'additions': 0, 'deletions': 0, 'truncated': False}
        raw_old = blob_bytes(before_code['blobs'][before['sha256']]) if before else b''
        raw_new = blob_bytes(after_code['blobs'][after['sha256']]) if after else b''
        try:
            if b'\0' in raw_old or b'\0' in raw_new:
                raise UnicodeError('binary content')
            text_old, text_new = raw_old.decode('utf-8'), raw_new.decode('utf-8')
        except UnicodeError:
            entry['binary'] = True
        else:
            if max(len(raw_old), len(raw_new)) > 256 * 1024 or text_old.count('\n') + text_new.count('\n') > 8000:
                entry['truncated'] = True
            else:
                lines_old, lines_new = text_old.splitlines(keepends=True), text_new.splitlines(keepends=True)
                for tag, start_old, end_old, start_new, end_new in difflib.SequenceMatcher(
                        None, lines_old, lines_new).get_opcodes():
                    if tag in ('delete', 'replace'):
                        entry['deletions'] += end_old - start_old
                    if tag in ('insert', 'replace'):
                        entry['additions'] += end_new - start_new
                patch = ''.join(difflib.unified_diff(lines_old, lines_new,
                                                   fromfile='a/' + path, tofile='b/' + path))
                limit = min(4000, budget)
                entry['patch'] = patch[:limit]
                entry['truncated'] = len(patch) > limit
                budget -= len(entry['patch'])
        result['truncated'] |= entry['truncated']
        result['summary']['additions'] += entry['additions']
        result['summary']['deletions'] += entry['deletions']
        result['files'].append(entry)
    return result


def merge_code(base, target, source, conflicts):
    """Merge immutable objects and atomic files; report conflicts to the caller."""
    if target == source:
        return copy.deepcopy(target)
    if target == base:
        return copy.deepcopy(source)
    if source == base:
        return copy.deepcopy(target)
    if target is None or source is None:
        conflicts.add('/code', 'delete/edit', code_summary(base), code_summary(target), code_summary(source))
        return copy.deepcopy(target)
    snapshots, blobs = {}, {}
    for code in (base, target, source):
        if code is None:
            continue
        blobs.update(code['blobs'])
        for snapshot in code['snapshots']:
            previous = snapshots.get(snapshot['id'])
            # The timestamp is deliberately outside snapshot identity. Choose
            # the earliest claim when independently captured bytes coincide.
            if previous is None or snapshot['created_at'] < previous['created_at']:
                snapshots[snapshot['id']] = copy.deepcopy(snapshot)

    def atomic(old, ours, theirs, path):
        if ours == theirs or theirs == old:
            return copy.deepcopy(ours)
        if ours == old:
            return copy.deepcopy(theirs)
        conflicts.add(path, 'file' if path.startswith('/code/files/') else 'binding', old, ours, theirs)
        return copy.deepcopy(ours)

    trees = [{item['path']: item for item in _snapshot(code)['files']} if code else {}
             for code in (base, target, source)]
    merged_files = []
    for path in sorted(set().union(*(tree.keys() for tree in trees))):
        item = atomic(*(tree.get(path) for tree in trees), '/code/files/' + path.replace('~', '~0').replace('/', '~1'))
        if item is not None:
            merged_files.append(item)
    bindings = [{(item['conversation_id'], item['message_id']): item for item in code['bindings']} if code else {}
                for code in (base, target, source)]
    merged_bindings = []
    for key in sorted(set().union(*(values.keys() for values in bindings))):
        path = '/code/bindings/' + '/'.join(part.replace('~', '~0').replace('/', '~1') for part in key)
        item = atomic(*(values.get(key) for values in bindings), path)
        if item is not None:
            merged_bindings.append(item)
    old_head = base['head'] if base else None
    ours, theirs = target['head'], source['head']
    if ours == theirs or theirs == old_head:
        head = ours
    elif ours == old_head:
        head = theirs
    else:
        snap = {'parents': [ours, theirs],
                'created_at': max(snapshots[ours]['created_at'], snapshots[theirs]['created_at']),
                'git_commit': '', 'branch': snapshots[ours]['branch'], 'files': merged_files}
        snap['id'] = snapshot_id(snap)
        snapshots[snap['id']] = snap
        head = snap['id']
    return {'schema_version': 1, 'head': head, 'snapshots': [snapshots[key] for key in sorted(snapshots)],
            'blobs': blobs, 'bindings': merged_bindings}
