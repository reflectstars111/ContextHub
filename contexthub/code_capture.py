"""Capture working bytes without committing to or executing source Git state.

The capture records a save-time snapshot, including unstaged and untracked
files. It never claims that the files equal the recorded source Git commit.
Excluded paths are returned through the optional local ``report`` argument;
reports are deliberately not embedded in the portable code repository.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from . import safe_fs as fs
from .code_repository import (MAX_CODE_BLOB_BYTES, MAX_CODE_BYTES, MAX_CODE_FILES,
                              blob_bytes, prune_bindings, snapshot_id, validate_code,
                              validate_code_path)
from .code_sync import _paths, _init, _Objects
from .git_bridge import _destination, _read_file
from .workspace import WorkspaceError, _directory


class CodeCaptureError(WorkspaceError):
    pass


_OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_CTX_DIRECTORIES = {'.git', '.contexthub', '.context-data', '.local-data'}
_SECRET_DIRECTORIES = {'.ssh', '.aws', '.azure', '.gnupg'}
_SECRET_FILES = {'.env', '.netrc', '_netrc', '.npmrc', '.pypirc', '.git-credentials',
                 'credentials.json', 'credentials', 'secrets.json', 'secrets.yaml',
                 'secrets.yml', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519'}
_ENV_TEMPLATES = {'.env.example', '.env.sample', '.env.template', '.env.dist'}


def _excluded(path):
    parts = path.casefold().split('/')
    if any(part in _CTX_DIRECTORIES for part in parts):
        return 'ContextHub/Git 本地状态'
    if parts[0] in {'conversations', 'library.json', 'code.json'}:
        return 'ContextHub 管理的对话和代码元数据'
    if any(part in _SECRET_DIRECTORIES for part in parts):
        return '凭据目录'
    name = parts[-1]
    if (name in _SECRET_FILES or (name.startswith('.env.') and name not in _ENV_TEMPLATES)
            or name.endswith(('.pem', '.key', '.p12', '.pfx'))
            or name.startswith(('service-account', 'service_account')) and name.endswith('.json')):
        return '凭据或私钥文件'
    return None


def _head(gitdir, common):
    """Read HEAD as data, allowing a newly initialized unborn branch."""
    value = _read_file(gitdir / 'HEAD', 4096).decode('ascii').strip()
    if _OID.fullmatch(value):
        return value, ''
    if not value.startswith('ref: refs/heads/'):
        raise CodeCaptureError('代码仓库 HEAD 无效')
    ref = value[5:]
    if (any(part in ('', '.', '..') for part in ref.split('/'))
            or '\\' in ref or any(ord(char) < 32 or ord(char) == 127 for char in ref)):
        raise CodeCaptureError('代码仓库分支引用无效')
    path = common / ref
    if os.path.lexists(path):
        commit = _read_file(path, 256).decode('ascii').strip()
    else:
        commit = ''
        if os.path.lexists(common / 'packed-refs'):
            for line in _read_file(common / 'packed-refs', 4 * 1024 * 1024).decode('ascii').splitlines():
                if not line.startswith(('#', '^')) and line.endswith(' ' + ref):
                    commit = line.split(' ', 1)[0]
    if commit and not _OID.fullmatch(commit):
        raise CodeCaptureError('代码仓库分支提交标识无效')
    return commit, ref[11:]


def _index(gitdir):
    return _read_file(gitdir / 'index', 32 * 1024 * 1024) if os.path.lexists(gitdir / 'index') else None


def _listing(git):
    tracked = {}
    # --full-name only controls output spelling; Git still restricts ls-files
    # to the process cwd prefix unless execution is explicitly rooted here.
    root_options = ('-C', git.environment['GIT_WORK_TREE'])
    raw = git.run(*root_options, 'ls-files', '--cached', '--stage', '--full-name', '-z', '--',
                  limit=8 * 1024 * 1024)
    try:
        for record in raw.split(b'\0'):
            if not record:
                continue
            metadata, name = record.split(b'\t', 1)
            mode, oid, stage = metadata.decode('ascii').split(' ')
            path = name.decode('utf-8')
            if stage != '0':
                raise CodeCaptureError('代码暂存区存在未解决的 Git 合并冲突，请先处理后再采集')
            if mode not in ('100644', '100755'):
                raise CodeCaptureError('代码快照不支持符号链接或子模块：' + path)
            tracked[path] = mode
        others = git.run(*root_options, 'ls-files', '--others', '--exclude-standard', '--full-name', '-z', '--',
                         limit=8 * 1024 * 1024)
        paths = set(tracked)
        paths.update(name.decode('utf-8') for name in others.split(b'\0') if name)
    except (ValueError, UnicodeError) as exc:
        raise CodeCaptureError('Git 文件列表不是受支持的 UTF-8 普通文件列表') from exc
    if len(paths) > MAX_CODE_FILES:
        raise CodeCaptureError('代码快照最多支持 10,000 个文件')
    return sorted(paths), tracked


def _read_work_file(root, path, tracked_mode=None):
    validate_code_path(path)
    target = root / path
    # Keep FileNotFoundError distinct: a removed tracked directory represents
    # a deletion, while unsafe ancestors still fail closed.
    parent = fs.directory(target.parent)
    handle = None
    try:
        info = fs.stat_entry(parent, target.name)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CodeCaptureError('代码快照拒绝符号链接、硬链接或特殊文件：' + path)
        handle = fs.open_file(parent, target.name, os.O_RDONLY)
        before = os.fstat(handle)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > MAX_CODE_BLOB_BYTES):
            raise CodeCaptureError('代码文件类型不受支持或超过 16 MiB：' + path)
        with os.fdopen(handle, 'rb', closefd=False) as stream:
            data = stream.read(MAX_CODE_BLOB_BYTES + 1)
        after = os.fstat(handle)
        if (len(data) > MAX_CODE_BLOB_BYTES
                or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                   (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise CodeCaptureError('采集期间代码文件发生变化，请重试：' + path)
        if data.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
            raise CodeCaptureError('代码快照需要 Git LFS 的实际文件内容，不能保存指针：' + path)
        mode = (tracked_mode or '100644') if os.name == 'nt' else (
            '100755' if before.st_mode & 0o111 else '100644')
        return data, mode
    finally:
        if handle is not None:
            os.close(handle)
        fs.close(parent)


def capture_code(repository, previous=None, *, conversations=None, bind_messages=None, report=None):
    """Return a validated independent snapshot, optionally binding explicit messages.

    ``report`` is a mutable local dictionary populated only after success with
    excluded path/reason pairs, captured file count, and snapshot reuse status.
    Old bindings are preserved (or pruned when conversations were removed).
    Passing explicit message IDs replaces only those save-time associations.
    """
    code = validate_code(previous) if previous is not None else {
        'schema_version': 1, 'head': '', 'snapshots': [], 'blobs': {}, 'bindings': []}
    if previous is not None and conversations is not None:
        code = prune_bindings(code, conversations)
    excluded, files, added, deleted = [], [], {}, []
    try:
        root, gitdir, common, objects = _paths(repository)
        commit, branch = _head(gitdir, common)
        index = _index(gitdir)
        with tempfile.TemporaryDirectory(prefix='ctx-code-capture-') as temporary:
            isolated = Path(temporary)
            _init(isolated, len(commit) if commit else 40)
            git = _Objects(isolated, objects)
            git.environment['GIT_WORK_TREE'] = str(root)
            if index is not None:
                (isolated / 'index').write_bytes(index)
            source_exclude = common / 'info/exclude'
            if os.path.lexists(source_exclude):
                (isolated / 'info').mkdir(mode=0o700, exist_ok=True)
                (isolated / 'info/exclude').write_bytes(_read_file(source_exclude, 1024 * 1024))
            paths, tracked = _listing(git)
            total = 0
            for path in paths:
                reason = _excluded(path)
                if reason:
                    excluded.append({'path': path, 'reason': reason})
                    continue
                try:
                    data, mode = _read_work_file(root, path, tracked.get(path))
                except FileNotFoundError:
                    if path in tracked:
                        deleted.append(path)
                        continue  # A tracked deletion is part of the working snapshot.
                    raise CodeCaptureError('采集期间未跟踪代码文件消失，请重试：' + path) from None
                total += len(data)
                if total > MAX_CODE_BYTES:
                    raise CodeCaptureError('代码快照文件总量超过 64 MiB')
                digest = hashlib.sha256(data).hexdigest()
                added[digest] = base64.b64encode(data).decode('ascii')
                files.append({'path': path, 'sha256': digest, 'size': len(data), 'mode': mode})
            # Catch ordinary concurrent edits instead of silently packaging a
            # list from one scan with file bytes from a different scan.
            if _listing(git) != (paths, tracked) or _index(gitdir) != index or _head(gitdir, common) != (commit, branch):
                raise CodeCaptureError('采集期间 Git 文件列表、暂存区或 HEAD 发生变化，请重试')
            for item in files:
                data, mode = _read_work_file(root, item['path'], tracked.get(item['path']))
                if hashlib.sha256(data).hexdigest() != item['sha256'] or mode != item['mode']:
                    raise CodeCaptureError('采集期间代码文件发生变化，请重试：' + item['path'])
            for path in deleted:
                try:
                    _read_work_file(root, path, tracked.get(path))
                except FileNotFoundError:
                    continue
                raise CodeCaptureError('采集期间已删除的代码文件重新出现，请重试：' + path)
    except (OSError, WorkspaceError, UnicodeError) as exc:
        if isinstance(exc, CodeCaptureError):
            raise
        raise CodeCaptureError('无法安全采集 Git 工作区代码：' + str(exc)) from exc
    head = next((row for row in code['snapshots'] if row['id'] == code['head']), None)
    reused = bool(head and all(head[key] == value for key, value in
                              (('git_commit', commit), ('branch', branch), ('files', files))))
    if not reused:
        snapshot = {'parents': [code['head']] if code['head'] else [],
                    'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'git_commit': commit, 'branch': branch, 'files': files}
        snapshot['id'] = snapshot_id(snapshot)
        code['snapshots'].append(snapshot)
        code['head'] = snapshot['id']
        code['blobs'].update(added)
    bindings = {(row['conversation_id'], row['message_id']): row for row in code['bindings']}
    for binding in bind_messages or []:
        if not isinstance(binding, dict) or set(binding) != {'conversation_id', 'message_id'}:
            raise CodeCaptureError('代码消息关联需要 conversation_id 和 message_id')
        key = binding['conversation_id'], binding['message_id']
        if any(not isinstance(value, str) for value in key):
            raise CodeCaptureError('代码消息关联标识必须是字符串')
        bindings[key] = dict(binding, snapshot_id=code['head'], kind='captured')
    code['bindings'] = [bindings[key] for key in sorted(bindings)]
    code = validate_code(code, conversations)
    if report is not None:
        report.update(excluded=excluded, captured_files=len(files), snapshot_reused=reused)
    return code


def restore_code(code, destination, snapshot=None):
    """Atomically restore one verified code tree to a new directory only."""
    checked = validate_code(code)
    ident = snapshot or checked['head']
    selected = next((row for row in checked['snapshots'] if row['id'] == ident), None)
    if selected is None:
        raise CodeCaptureError('找不到指定的代码快照')
    try:
        with _destination(destination) as (staged, target):
            total = 0
            anchor = _directory(staged)
            try:
                for item in selected['files']:
                    parts = item['path'].split('/')
                    data = blob_bytes(checked['blobs'][item['sha256']])
                    parent, parents, handle = anchor, [], None
                    try:
                        for component in parts[:-1]:
                            try:
                                fs.mkdir(parent, component, 0o700)
                            except FileExistsError:
                                pass
                            parent = fs.child_dir(parent, component)
                            parents.append(parent)
                        handle = fs.open_file(parent, parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                              0o755 if item['mode'] == '100755' else 0o644)
                        with os.fdopen(handle, 'wb', closefd=False) as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(handle)
                        if os.name != 'nt':
                            os.fchmod(handle, 0o755 if item['mode'] == '100755' else 0o644)
                    finally:
                        if handle is not None:
                            os.close(handle)
                        for opened in reversed(parents):
                            fs.close(opened)
                    total += len(data)
            finally:
                fs.close(anchor)
        return {'directory': str(target), 'snapshot_id': ident, 'git_commit': selected['git_commit'],
                'branch': selected['branch'], 'files': len(selected['files']), 'bytes': total}
    except (OSError, WorkspaceError) as exc:
        if isinstance(exc, CodeCaptureError):
            raise
        raise CodeCaptureError('无法安全恢复代码快照：' + str(exc)) from exc
