"""A branched, explicit-file conversation workspace with conservative merges.

Only library.json and the conversation IDs it names are staged. Immutable
packages and commit records live below .contexthub; one atomic state file is
the publication point. Worktree replacements have a durable rollback journal.
No Git hooks, project files, model calls, or native session state are involved.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from .package import (MAX_ARCHIVE_BYTES, MAX_CONVERSATION_BYTES,
                      MAX_CONVERSATIONS, MAX_METADATA_BYTES, MAX_TOTAL_BYTES,
                      PackageError, _json_bytes, _load_json, build_package,
                      read_package)
from .revisions import build_revision, compare_packages
from .merge import merge_packages
from . import safe_fs as fs


class WorkspaceError(ValueError):
    """The workspace cannot safely complete the requested operation."""


class MergeConflict(WorkspaceError):
    def __init__(self, details):
        self.details = details
        super().__init__('合并有冲突；本地文件和双方提交均已保留。检查冲突后编辑文件、ctx add .，再 ctx merge 分支 --resolved')


class _WorktreeChanged(WorkspaceError):
    pass


_HASH = re.compile(r'[0-9a-f]{64}\Z')
_FILE = re.compile(r'conversations/[0-9a-f]{64}\.json\Z')
_LIMIT = MAX_TOTAL_BYTES + 4 * MAX_METADATA_BYTES
_UNCHECKED = object()
_MAX_BRANCHES = 1000


def _branch_name(name):
    if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}', name)
            or name in ('HEAD', 'INDEX', 'WORKTREE') or '..' in name or '//' in name
            or any(part.startswith('.') or part.endswith(('.', '.lock')) for part in name.split('/'))
            or name.endswith('/')):
        raise WorkspaceError('分支名需为 1–100 个字母、数字、点、横线、下划线或斜线，且不能含路径跳转')
    return name


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _source_identity(source):
    if (not isinstance(source, dict)
            or set(source) != {'provider', 'thread_id', 'conversation_id'}
            or source['provider'] != 'codex'
            or not isinstance(source['thread_id'], str)
            or not source['thread_id'].strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in source['thread_id'])):
        raise WorkspaceError('本地来源记录需要 Codex 原生会话 ID 和对应的规范对话 ID')
    try:
        raw = source['thread_id'].encode('utf-8')
    except UnicodeEncodeError as exc:
        raise WorkspaceError('Codex 原生会话 ID 包含无效字符') from exc
    if len(raw) > 512 or source['conversation_id'] != 'sha256:' + _hash(raw):
        raise WorkspaceError('Codex 原生会话 ID 过长或与规范对话 ID 不匹配')
    return source['conversation_id']


def _capture_digest(conversation):
    value = copy.deepcopy(conversation)
    value['source'].pop('exported_at', None)
    return _hash(_json_bytes(value))


def _filename(identifier):
    return 'conversations/' + _hash(identifier.encode('utf-8')) + '.json'


def _directory(path):
    """Anchor every path component without following links, including parents."""
    try:
        return fs.directory(path)
    except OSError as exc:
        raise WorkspaceError('工作目录必须存在，且路径不得包含符号链接、junction 或特殊路径') from exc


def _regular(fd, name, *, missing=False):
    try:
        info = fs.stat_entry(fd, name)
    except FileNotFoundError:
        if missing:
            return None
        raise WorkspaceError(f'缺少工作区文件：{name}') from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WorkspaceError(f'拒绝符号链接、硬链接或特殊文件：{name}')
    return info


def _read(fd, name, limit, *, missing=False):
    if _regular(fd, name, missing=missing) is None:
        return None
    try:
        handle = fs.open_file(fd, name, os.O_RDONLY)
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise WorkspaceError(f'文件类型或大小不受支持：{name}')
            with os.fdopen(handle, 'rb', closefd=False) as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise WorkspaceError(f'文件过大：{name}')
            return data
        finally:
            os.close(handle)
    except OSError as exc:
        raise WorkspaceError(f'无法安全读取文件：{name}') from exc


def _atomic(fd, name, data, *, expected=_UNCHECKED):
    _regular(fd, name, missing=True)
    temporary = '.tmp-' + uuid.uuid4().hex
    handle = fs.open_file(fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(handle, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(handle)
        # Windows refuses to rename an open file. Close the flushed temporary
        # handle before the replace; the anchored parent directory stays held.
        os.close(handle)
        handle = None
        # rename replaces the directory entry, never the target of a link.
        _regular(fd, name, missing=True)
        if expected is not _UNCHECKED:
            _check_snapshot(fd, name, expected)
        fs.replace(fd, temporary, name)
        fs.sync_dir(fd)
    finally:
        if handle is not None:
            os.close(handle)
        try:
            fs.unlink(fd, temporary)
        except FileNotFoundError:
            pass


def _json(fd, name, limit=MAX_METADATA_BYTES):
    try:
        return _load_json(_read(fd, name, limit), name)
    except PackageError as exc:
        raise WorkspaceError(f'工作区 JSON 无效：{name}: {exc}') from exc


def _file_limit(name):
    return MAX_TOTAL_BYTES if name == "code.json" else MAX_CONVERSATION_BYTES


def _check_snapshot(fd, name, expected):
    data = _read(fd, name, _file_limit(name), missing=True)
    if (_hash(data) if data is not None else None) != expected:
        raise _WorktreeChanged(f'文件在操作期间被其他程序修改，已取消并保留编辑：{name}')


def _empty_package(library):
    return {'library': library, 'conversations': []}


def _revision_data(base_data, payload):
    base = read_package(base_data)
    result = build_revision(base, payload)
    parsed = read_package(result)
    # Valid downloaded packs may use different ZIP/JSON serialization. Keep
    # their exact bytes for an unchanged snapshot rather than staging a false
    # change just because our serializer chooses canonical whitespace.
    if all(parsed.get(key) == base.get(key) for key in ('library', 'conversations', 'compatibility', 'code')):
        return base_data
    return result


class Workspace:
    def __init__(self, path):
        self.root = Path(os.path.abspath(os.fspath(path)))
        self._mutex = threading.RLock()
        self._depth = 0
        self._fds = None

    @classmethod
    def init(cls, path, title, description=''):
        # Reuse the format validator without inventing a portable empty pack.
        example = read_package(build_package([{'id': 'validation', 'messages': []}],
                                             title, description))
        library = example['library']
        library['conversation_ids'] = []
        target = Path(os.path.abspath(os.fspath(path)))
        parent = _directory(target.parent)
        created = False
        try:
            try:
                fs.mkdir(parent, target.name)
                created = True
            except FileExistsError:
                pass
            root = _directory(target)
            try:
                for name in ('.contexthub', 'library.json', 'conversations', 'code.json'):
                    try:
                        fs.stat_entry(root, name)
                    except FileNotFoundError:
                        continue
                    raise WorkspaceError(f'初始化不会覆盖已有路径：{name}')
                fs.mkdir(root, '.contexthub')
                meta = fs.child_dir(root, '.contexthub')
                try:
                    for name in ('objects', 'commits', 'backups'):
                        fs.mkdir(meta, name)
                    state = {'schema_version': 2, 'branch': 'main', 'upload_keys': {'main': uuid.uuid4().hex},
                             'branches': {'main': {'head': None, 'remote': None}},
                             'head': None, 'index': None,
                             'remote': None, 'managed': ['library.json'], 'transaction': None}
                    _atomic(meta, 'state.json', _json_bytes(state))
                    fs.mkdir(root, 'conversations')
                    _atomic(root, 'library.json', _json_bytes(library))
                    fs.sync_dir(root)
                finally:
                    fs.close(meta)
            finally:
                fs.close(root)
        except Exception:
            # A partial initialization is left intact for inspection; existing
            # project files are never removed during cleanup.
            if created:
                try:
                    fs.rmdir(parent, target.name)
                except OSError:
                    pass
            raise
        finally:
            fs.close(parent)
        return cls(target)

    @classmethod
    def find(cls, path='.'):
        current = Path(os.path.abspath(os.fspath(path)))
        probe = _directory(current)
        fs.close(probe)
        for candidate in (current, *current.parents):
            marker = candidate / '.contexthub'
            if marker.exists() or marker.is_symlink():
                workspace = cls(candidate)
                with workspace.locked():
                    workspace._state()
                return workspace
        raise WorkspaceError('未找到 ContextHub 工作区，请先运行 ctx init 或 ctx clone')

    @contextmanager
    def locked(self):
        """Reentrant, nonblocking process/thread lock for local and sync work."""
        if not self._mutex.acquire(blocking=False):
            raise WorkspaceError('工作区正被另一个操作使用，请稍后重试')
        outer = self._depth == 0
        descriptors = []
        try:
            if outer:
                root = _directory(self.root)
                descriptors.append(root)
                for name in ('.contexthub', 'conversations'):
                    fd = fs.child_dir(root, name)
                    descriptors.append(fd)
                meta, conversations = descriptors[1:]
                lock = fs.open_file(meta, 'lock', os.O_RDWR | os.O_CREAT)
                descriptors.append(lock)
                info = os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkspaceError('工作区锁文件不是独立的常规文件')
                try:
                    fs.lock(lock)
                except BlockingIOError as exc:
                    raise WorkspaceError('工作区正被另一个操作使用，请稍后重试') from exc
                self._fds = (root, meta, conversations)
                self._recover()
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
        except OSError as exc:
            raise WorkspaceError(f'工作区文件操作失败：{exc.strerror or str(exc)}') from exc
        finally:
            if outer:
                self._fds = None
                for fd in reversed(descriptors):
                    fs.close(fd)
            self._mutex.release()

    def _state(self):
        value = _json(self._fds[1], 'state.json')
        required = {'schema_version', 'head', 'index', 'remote', 'managed', 'transaction'}
        if not isinstance(value, dict):
            raise WorkspaceError('工作区 state.json 无效')
        optional = {'sources'} if 'sources' in value else set()
        if value.get('schema_version') == 1 and set(value) == required | optional:
            # Read old workspaces without touching their files. The next
            # successful mutation atomically publishes the migrated state.
            value.update(schema_version=2, branch='main',
                         branches={'main': {'head': value['head'], 'remote': copy.deepcopy(value['remote'])}})
        if value.get('schema_version') == 2 and set(value) == required | {'branch', 'branches'} | optional:
            value['upload_keys'] = {}
        value.setdefault('sources', {})
        required |= {'branch', 'branches', 'upload_keys', 'sources'}
        if (set(value) != required or type(value['schema_version']) is not int or value['schema_version'] != 2
                or not isinstance(value['managed'], list)
                or len(value['managed']) > MAX_CONVERSATIONS + 2
                or any(not self._valid_path(path) for path in value['managed'])
                or len(set(value['managed'])) != len(value['managed'])
                or 'library.json' not in value['managed']):
            raise WorkspaceError('工作区 state.json 无效或版本不受支持')
        for key in ('head', 'index'):
            if value[key] is not None and (not isinstance(value[key], str) or not _HASH.fullmatch(value[key])):
                raise WorkspaceError(f'工作区 {key} 无效')
        branches = value['branches']
        if not isinstance(branches, dict) or not 1 <= len(branches) <= _MAX_BRANCHES:
            raise WorkspaceError('工作区分支记录无效')
        keys = value['upload_keys']
        if (not isinstance(keys, dict) or set(keys) - set(branches)
                or any(not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{32}', key) for key in keys.values())):
            raise WorkspaceError('工作区分支上传标识无效')
        for name, branch in branches.items():
            _branch_name(name)
            if (not isinstance(branch, dict) or set(branch) != {'head', 'remote'}
                    or (branch['head'] is not None and (
                        not isinstance(branch['head'], str) or not _HASH.fullmatch(branch['head'])))):
                raise WorkspaceError('工作区分支指针无效')
            if branch['remote'] is not None:
                self._validate_remote(branch['remote'])
        if (not isinstance(value['branch'], str) or value['branch'] not in branches
                or branches[value['branch']] != {'head': value['head'], 'remote': value['remote']}):
            raise WorkspaceError('当前分支与工作区指针不一致')
        sources = value['sources']
        if not isinstance(sources, dict) or set(sources) - set(branches):
            raise WorkspaceError('本地来源跟踪的分支记录无效')
        for bindings in sources.values():
            if not isinstance(bindings, dict) or len(bindings) > MAX_CONVERSATIONS:
                raise WorkspaceError('本地来源跟踪记录无效或数量超过限制')
            for identifier, binding in bindings.items():
                if (not isinstance(binding, dict)
                        or set(binding) != {'provider', 'thread_id', 'capture_digest', 'workspace_digest'}):
                    raise WorkspaceError('本地来源跟踪记录字段无效')
                _source_identity({'provider': binding['provider'], 'thread_id': binding['thread_id'],
                                  'conversation_id': identifier})
                for key in ('capture_digest', 'workspace_digest'):
                    if not isinstance(binding[key], str) or not _HASH.fullmatch(binding[key]):
                        raise WorkspaceError('本地来源跟踪摘要无效')
        if value['transaction'] is not None and (
                not isinstance(value['transaction'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', value['transaction'])):
            raise WorkspaceError('工作区事务标识无效')
        return value

    @staticmethod
    def _valid_path(path):
        return isinstance(path, str) and (path in ('library.json', 'code.json') or _FILE.fullmatch(path))

    def _path(self, path):
        if not self._valid_path(path):
            raise WorkspaceError('不安全的工作树路径')
        return (self._fds[0], path) if path in ('library.json', 'code.json') else (self._fds[2], path.split('/')[1])

    @contextmanager
    def _subdir(self, name):
        fd = fs.child_dir(self._fds[1], name)
        try:
            yield fd
        finally:
            fs.close(fd)

    def _object(self, digest):
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise WorkspaceError('无效的对象 ID')
        with self._subdir('objects') as fd:
            data = _read(fd, digest + '.contextpack', MAX_ARCHIVE_BYTES)
        if _hash(data) != digest:
            raise WorkspaceError('工作区对象校验失败')
        read_package(data)
        return data

    def _save_object(self, data):
        read_package(data)
        digest = _hash(data)
        with self._subdir('objects') as fd:
            existing = _read(fd, digest + '.contextpack', MAX_ARCHIVE_BYTES, missing=True)
            if existing is None:
                _atomic(fd, digest + '.contextpack', data)
            elif existing != data:
                raise WorkspaceError('不可变对象发生冲突')
        return digest

    def _commit_record(self, identifier):
        if not isinstance(identifier, str) or not _HASH.fullmatch(identifier):
            raise WorkspaceError('无效的提交 ID')
        with self._subdir('commits') as fd:
            record = _json(fd, identifier + '.json')
        if not isinstance(record, dict) or record.get('id') != identifier:
            raise WorkspaceError('提交 ID 不一致')
        contents = {key: value for key, value in record.items() if key != 'id'}
        if (set(contents) not in ({'parent', 'sha256', 'message', 'created_at'},
                                  {'parent', 'merge_parent', 'sha256', 'message', 'created_at'})
                or _hash(_json_bytes(contents)) != identifier
                or not isinstance(contents['sha256'], str)
                or not _HASH.fullmatch(contents['sha256'])
                or (contents['parent'] is not None and (
                    not isinstance(contents['parent'], str) or not _HASH.fullmatch(contents['parent'])))):
            raise WorkspaceError('提交记录校验失败')
        if 'merge_parent' in contents and (not isinstance(contents['merge_parent'], str)
                or not _HASH.fullmatch(contents['merge_parent']) or contents['merge_parent'] == contents['parent']):
            raise WorkspaceError('合并提交的父记录无效')
        if (not isinstance(contents['message'], str) or not 1 <= len(contents['message']) <= 2000
                or not isinstance(contents['created_at'], str) or len(contents['created_at']) > 100):
            raise WorkspaceError('提交元数据无效')
        return record

    def _save_commit(self, data, message, parent, merge_parent=None, created_at=None):
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise WorkspaceError('提交说明需要 1–2000 个字符')
        contents = {'parent': parent, 'sha256': self._save_object(data),
                    'message': message.strip(), 'created_at': created_at or datetime.now(timezone.utc).isoformat()}
        if merge_parent is not None:
            contents['merge_parent'] = merge_parent
        record = dict(contents, id=_hash(_json_bytes(contents)))
        with self._subdir('commits') as fd:
            _atomic(fd, record['id'] + '.json', _json_bytes(record))
        return record

    def _write_state(self, state):
        state['branches'][state['branch']] = {'head': state['head'], 'remote': copy.deepcopy(state['remote'])}
        data = _json_bytes(state)
        if len(data) > MAX_METADATA_BYTES:
            raise WorkspaceError('工作区元数据超过大小限制')
        _atomic(self._fds[1], 'state.json', data)

    def _cleanup_journal(self, journal):
        fs.unlink(self._fds[1], 'journal.json')
        fs.sync_dir(self._fds[1])
        with self._subdir('backups') as fd:
            for entry in journal['files']:
                if entry['backup']:
                    try:
                        fs.unlink(fd, entry['backup'])
                    except FileNotFoundError:
                        pass
            fs.sync_dir(fd)

    def _recover(self, *, preserve_external=False):
        raw = _read(self._fds[1], 'journal.json', MAX_METADATA_BYTES, missing=True)
        if raw is None:
            return
        journal = _load_json(raw, 'journal.json')
        if (set(journal) != {'id', 'files'} or not isinstance(journal['id'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', journal['id'])
                or not isinstance(journal['files'], list)
                or len(journal['files']) > 2 * MAX_CONVERSATIONS + 2):
            raise WorkspaceError('恢复日志无效，请保留 .contexthub 并检查')
        seen = set()
        for entry in journal['files']:
            if (not isinstance(entry, dict) or set(entry) != {'path', 'backup', 'after'}
                    or not self._valid_path(entry['path']) or entry['path'] in seen
                    or (entry['after'] is not None and (
                        not isinstance(entry['after'], str) or not _HASH.fullmatch(entry['after'])))
                    or (entry['backup'] is not None and (
                        not isinstance(entry['backup'], str)
                        or not re.fullmatch(journal['id'] + r'-[0-9]{3}\.bak', entry['backup'])))):
                raise WorkspaceError('恢复日志包含不安全的路径')
            seen.add(entry['path'])
        if self._state()['transaction'] != journal['id']:
            with self._subdir('backups') as backup_fd:
                # Validate every rollback target and backup before modifying any.
                restored = []
                for entry in journal['files']:
                    fd, name = self._path(entry['path'])
                    current = _read(fd, name, _file_limit(name), missing=True)
                    data = (_read(backup_fd, entry['backup'], _file_limit(name))
                            if entry['backup'] else None)
                    current_hash = _hash(current) if current is not None else None
                    before_hash = _hash(data) if data is not None else None
                    if current_hash not in (before_hash, entry['after']):
                        if preserve_external:
                            continue
                        raise WorkspaceError(
                            f'中断后文件又被修改，自动恢复已停止；请保留文件和 .contexthub/backups：{entry["path"]}')
                    restored.append((fd, name, data, current_hash))
                for fd, name, data, expected in restored:
                    if data is None:
                        _check_snapshot(fd, name, expected)
                        try:
                            fs.unlink(fd, name)
                        except FileNotFoundError:
                            pass
                        fs.sync_dir(fd)
                    else:
                        _atomic(fd, name, data, expected=expected)
        self._cleanup_journal(journal)

    def _transition(self, state, files, *, expected, permit_existing=False):
        previous = self._state()
        paths = sorted(set(previous['managed']) | set(files))
        transaction = uuid.uuid4().hex
        journal = {'id': transaction, 'files': []}
        backups = []
        total = 0
        # Complete preflight and save original bytes before any working writes.
        for index, path in enumerate(paths):
            fd, name = self._path(path)
            data = _read(fd, name, _file_limit(name), missing=True)
            if (_hash(data) if data is not None else None) != expected.get(path):
                raise _WorktreeChanged(f'文件在操作期间被其他程序修改，已取消并保留编辑：{path}')
            if data is not None and path not in previous['managed'] and not permit_existing:
                raise WorkspaceError(f'未跟踪文件会被覆盖，操作已取消：{path}')
            total += len(data) if data else 0
            if total > _LIMIT:
                raise WorkspaceError('工作树备份超过大小限制')
            backup = f'{transaction}-{index:03d}.bak' if data is not None else None
            journal['files'].append({'path': path, 'backup': backup,
                                     'after': _hash(files[path]) if path in files else None})
            if backup:
                backups.append((backup, data))
        with self._subdir('backups') as fd:
            for name, data in backups:
                _atomic(fd, name, data)
        _atomic(self._fds[1], 'journal.json', _json_bytes(journal))
        try:
            for path in paths:
                fd, name = self._path(path)
                _check_snapshot(fd, name, expected.get(path))
                if path in files:
                    _atomic(fd, name, files[path], expected=expected.get(path))
                else:
                    _regular(fd, name, missing=True)
                    try:
                        fs.unlink(fd, name)
                    except FileNotFoundError:
                        pass
                    fs.sync_dir(fd)
            state['managed'] = sorted(files)
            state['transaction'] = transaction
            self._write_state(state)
        except Exception:
            # If rollback itself fails (e.g. ENOSPC), the durable journal stays
            # and the next command retries recovery before accepting new work.
            self._recover(preserve_external=True)
            raise
        self._cleanup_journal(journal)

    @staticmethod
    def _files(package):
        result = {'library.json': _json_bytes(package['library'])}
        result.update({_filename(item['id']): _json_bytes(item) for item in package['conversations']})
        if package.get('code') is not None:
            result['code.json'] = _json_bytes(package['code'])
        return result

    def _work(self, *, snapshot=None, original=None):
        state = self._state()
        raw_library = _read(self._fds[0], 'library.json', MAX_METADATA_BYTES)
        library = _load_json(raw_library, 'library.json')
        if snapshot is not None:
            snapshot['library.json'] = _hash(raw_library)
        if (not isinstance(library, dict)
                or set(library) - {'code_checkpoint'} != {'schema_version', 'title', 'description', 'source', 'conversation_ids'}
                or library['schema_version'] != '0.1'
                or not isinstance(library['conversation_ids'], list)
                or len(library['conversation_ids']) > MAX_CONVERSATIONS
                or any(not isinstance(item, str) for item in library['conversation_ids'])
                or len(set(library['conversation_ids'])) != len(library['conversation_ids'])):
            raise WorkspaceError('library.json 的字段或 conversation_ids 无效')
        if 'code_checkpoint' in library:
            from .checkpoints import validate_checkpoint
            validate_checkpoint(library['code_checkpoint'])
        conversations = []
        total = 0
        for identifier in library['conversation_ids']:
            name = _filename(identifier).split('/')[1]
            data = _read(self._fds[2], name, MAX_CONVERSATION_BYTES)
            if snapshot is not None:
                snapshot['conversations/' + name] = _hash(data)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise WorkspaceError('工作树超过大小限制')
            item = _load_json(data, name)
            if not isinstance(item, dict) or item.get('id') != identifier:
                raise WorkspaceError(f'对话 ID 与 library.json 不一致：{name}')
            conversations.append(item)
        raw_code = _read(self._fds[0], 'code.json', MAX_TOTAL_BYTES, missing=True)
        code = _load_json(raw_code, 'code.json') if raw_code is not None else None
        if snapshot is not None:
            snapshot['code.json'] = _hash(raw_code) if raw_code is not None else None
        if original is not None:
            original.update(library=library, conversations=conversations, code=code)
        if snapshot is not None:
            # A library edit can remove an ID. Capture those previously managed
            # files too, so an editor save cannot be erased by a later deletion.
            for path in state['managed']:
                if path not in snapshot:
                    fd, name = self._path(path)
                    data = _read(fd, name, _file_limit(name), missing=True)
                    snapshot[path] = _hash(data) if data is not None else None
        if not conversations:
            # Validate empty workspace metadata using the same bounded format.
            build_package([{'id': 'validation', 'messages': []}], library['title'],
                          library['description'], source=library['source'],
                          code_checkpoint=library.get('code_checkpoint'))
            return _empty_package(library), None
        if state['head']:
            base_data = self._object(self._commit_record(state['head'])['sha256'])
            base = read_package(base_data)
            if library['source'] != base['library']['source']:
                raise WorkspaceError('已有对话库的来源记录不可改写；可修改标题、说明和对话正文')
            data = _revision_data(base_data, {'title': library['title'], 'description': library['description'],
                                              'conversations': conversations,
                                              'code_checkpoint': library.get('code_checkpoint'), 'code': code})
        else:
            data = build_package(conversations, library['title'], library['description'], source=library['source'],
                                 code_checkpoint=library.get('code_checkpoint'), code=code)
        return read_package(data), data

    @property
    def head(self):
        with self.locked():
            return self._state()['head']

    @property
    def remote(self):
        with self.locked():
            return copy.deepcopy(self._state()['remote'])

    def package_bytes(self, ref='HEAD'):
        with self.locked():
            state = self._state()
            if ref == 'WORKTREE':
                data = self._work()[1]
                if data is None:
                    raise WorkspaceError('工作区尚无对话')
                return data
            if ref == 'INDEX':
                digest = state['index']
            else:
                identifier = self._resolve(ref, state)
                if not identifier:
                    raise WorkspaceError('尚无提交，请先 ctx add 和 ctx commit')
                digest = self._commit_record(identifier)['sha256']
            if digest is None:
                raise WorkspaceError('暂存区尚无对话')
            return self._object(digest)

    @staticmethod
    def _source_list(state):
        return [{'provider': binding['provider'], 'thread_id': binding['thread_id'],
                 'conversation_id': identifier}
                for identifier, binding in sorted(state['sources'].get(state['branch'], {}).items())]

    def sources(self):
        """Return this branch's local bindings, never the internal guard hashes."""
        with self.locked():
            return self._source_list(self._state())

    def capture(self, data, sources, replace=False, *, code=_UNCHECKED):
        """Atomically stage captured conversations and their local refresh guards."""
        if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_CONVERSATIONS:
            raise WorkspaceError(f'采集需要 1–{MAX_CONVERSATIONS} 条本地来源记录')
        bindings = {}
        for source in sources:
            identifier = _source_identity(source)
            if identifier in bindings:
                raise WorkspaceError('一次采集不能重复登记同一来源')
            bindings[identifier] = copy.deepcopy(source)
        return self._add(data, replace=replace, sources=bindings, code=code)

    def add(self, data, replace=False, *, code=_UNCHECKED):
        return self._add(data, replace=replace, code=code)

    def _add(self, data, replace=False, *, sources=None, code=_UNCHECKED):
        incoming = read_package(data)
        if sources is not None:
            if set(sources) != {item['id'] for item in incoming['conversations']}:
                raise WorkspaceError('每条本地来源必须对应采集包中的唯一对话，且不能遗漏对话')
            for item in incoming['conversations']:
                if item['source'].get('provider') != 'codex' or item['source'].get('thread_id') != item['id']:
                    raise WorkspaceError('采集包中的 Codex 来源与规范对话 ID 不匹配')
        with self.locked():
            state = self._state()
            expected = {}
            original = {}
            current, _ = self._work(snapshot=expected, original=original)
            conversations = {item['id']: item for item in current['conversations']}
            original_conversations = {item['id']: item for item in original['conversations']}
            bindings = state['sources'].get(state['branch'], {})
            captured = {}
            for item in incoming['conversations']:
                identifier = item['id']
                previous = conversations.get(identifier)
                binding = bindings.get(identifier) if sources is not None else None
                if binding is not None:
                    original_item = original_conversations.get(identifier)
                    changed = (original_item is None
                               or _hash(_json_bytes(original_item)) != binding['workspace_digest'])
                    if changed and not replace:
                        raise WorkspaceError(
                            f'已跟踪的对话被修改或删除：{identifier}；先用 ctx add . --no-refresh 暂存人工修改，'
                            '确认使用 Codex 内容覆盖后再添加 --replace')
                    # Preserve the staged/audited representation when the source
                    # has not changed; an export timestamp is not a new revision.
                    unchanged = not changed and _capture_digest(item) == binding['capture_digest']
                else:
                    unchanged = False
                    if previous is not None and previous != item and not replace:
                        raise WorkspaceError(f'对话 ID 已存在且内容不同：{identifier}；检查后使用 --replace')
                if sources is not None:
                    captured[identifier] = _capture_digest(item)
                if not unchanged:
                    conversations[identifier] = item
            library = current['library']
            checkpoint = library.get('code_checkpoint')
            incoming_checkpoint = incoming['library'].get('code_checkpoint')
            if incoming_checkpoint is not None:
                if checkpoint is not None and checkpoint != incoming_checkpoint:
                    raise WorkspaceError('导入内容关联了不同的代码检查点；请在新工作区导入，避免覆盖现有代码关联')
                checkpoint = incoming_checkpoint
            code = incoming.get('code', current.get('code')) if code is _UNCHECKED else code
            if code and current.get('code'):
                old_head = current['code']['head']
                if old_head not in {snap['id'] for snap in code['snapshots']} and not replace:
                    raise WorkspaceError('导入代码与现有快照历史不相连；请新建工作区，或确认后使用 --replace')
            if code is not None:
                from .code_repository import prune_bindings
                code = prune_bindings(code, list(conversations.values()))
            if state['head']:
                data = _revision_data(self.package_bytes(), {
                    'title': library['title'], 'description': library['description'],
                    'conversations': list(conversations.values()), 'code_checkpoint': checkpoint, 'code': code})
            else:
                data = build_package(list(conversations.values()), library['title'],
                                     library['description'], source=library['source'], code_checkpoint=checkpoint, code=code)
            package = read_package(data)
            if sources is not None:
                final = {item['id']: item for item in package['conversations']}
                bindings = state['sources'].setdefault(state['branch'], {})
                for identifier, source in sources.items():
                    bindings[identifier] = {'provider': source['provider'], 'thread_id': source['thread_id'],
                                            'capture_digest': captured[identifier],
                                            'workspace_digest': _hash(_json_bytes(final[identifier]))}
                if len(bindings) > MAX_CONVERSATIONS:
                    raise WorkspaceError('本地来源跟踪数量超过限制')
            state['index'] = self._save_object(data)
            self._transition(state, self._files(package), expected=expected)
            return self.status()

    def stage(self, *, code=None):
        with self.locked():
            state = self._state()
            expected = {}
            package, data = self._work(snapshot=expected)
            if data is None:
                raise WorkspaceError('不能暂存空对话库；至少保留一段对话')
            if code is not None:
                data = _revision_data(data, {'conversations': package['conversations'], 'code': code})
                package = read_package(data)
            state['index'] = self._save_object(data)
            self._transition(state, self._files(package), expected=expected, permit_existing=True)
            return self.status()

    def commit(self, message):
        with self.locked():
            state = self._state()
            if state['index'] is None:
                raise WorkspaceError('暂无暂存内容，请先 ctx add')
            if state['head'] and state['index'] == self._commit_record(state['head'])['sha256']:
                raise WorkspaceError('没有已暂存的变化，无需提交')
            record = self._save_commit(self._object(state['index']), message, state['head'])
            state['head'] = record['id']
            self._write_state(state)
            return record

    def record_code_checkpoint(self, checkpoint, message):
        """Record a code link as a new immutable conversation commit.

        The caller verifies a clean code checkout; this layer binds its claim to
        the exact committed conversation snapshot and refuses pending edits.
        """
        from .checkpoints import attach_checkpoint
        with self.locked():
            expected = self._clean_snapshot()
            state = self._state()
            data = attach_checkpoint(self.package_bytes(), checkpoint)
            package = read_package(data)
            record = self._save_commit(data, message, state['head'])
            state.update(head=record['id'], index=record['sha256'])
            self._transition(state, self._files(package), expected=expected)
            return record

    def _resolve(self, ref, state=None):
        state = state or self._state()
        if ref == 'HEAD':
            return state['head']
        if ref in state['branches']:
            return state['branches'][ref]['head']
        self._commit_record(ref)
        return ref

    def branches(self):
        with self.locked():
            state = self._state()
            return [dict(copy.deepcopy(value), name=name, current=name == state['branch'])
                    for name, value in sorted(state['branches'].items())]

    def branch(self, name, ref='HEAD'):
        _branch_name(name)
        with self.locked():
            state = self._state()
            if name in state['branches'] or len(state['branches']) >= _MAX_BRANCHES:
                raise WorkspaceError('分支已存在或超过分支数量限制')
            head = self._resolve(ref, state)
            if head is None:
                raise WorkspaceError('请先创建首次提交，再新建分支')
            # A new branch must not accidentally publish to its parent's Hub.
            state['branches'][name] = {'head': head, 'remote': None}
            state['upload_keys'][name] = uuid.uuid4().hex
            if state['sources'].get(state['branch']):
                state['sources'][name] = copy.deepcopy(state['sources'][state['branch']])
            self._write_state(state)
            return self.branches()

    def upload_key(self):
        """Persist a branch-scoped creation key before attempting any upload."""
        with self.locked():
            state = self._state()
            if state['branch'] not in state['upload_keys']:
                state['upload_keys'][state['branch']] = uuid.uuid4().hex
                self._write_state(state)
            return state['upload_keys'][state['branch']]

    def _clean_snapshot(self, *, staged=False):
        expected = {}
        status = self.status(_snapshot=expected)
        if status['modified'] or (status['staged'] and not staged):
            raise WorkspaceError('工作区有未提交修改；请先检查、暂存并提交，文件保持不变')
        return expected

    def switch(self, name, create=False):
        _branch_name(name)
        with self.locked():
            expected = self._clean_snapshot()
            state = self._state()
            if create:
                self.branch(name)
                state = self._state()
            if name not in state['branches']:
                raise WorkspaceError('分支不存在；可使用 ctx switch -c 分支名 新建')
            target = state['branches'][name]
            if not target['head']:
                raise WorkspaceError('目标分支尚无提交')
            record = self._commit_record(target['head'])
            state.update(branch=name, head=target['head'], index=record['sha256'],
                         remote=copy.deepcopy(target['remote']))
            self._transition(state, self._files(read_package(self._object(record['sha256']))), expected=expected)
            return self.status()

    def history(self, ref='HEAD'):
        """All reachable commits, children before parents, rejecting cycles."""
        with self.locked():
            head = self._resolve(ref)
            records, active, done, ordered = {}, set(), set(), []
            stack = [(head, False)] if head else []
            while stack:
                identifier, visited = stack.pop()
                if visited:
                    active.remove(identifier)
                    done.add(identifier)
                    ordered.append(records[identifier])
                    continue
                if identifier in done:
                    continue
                if identifier in active or len(records) >= 100000:
                    raise WorkspaceError('提交历史循环或超过读取限制')
                record = records.setdefault(identifier, self._commit_record(identifier))
                active.add(identifier)
                stack.append((identifier, True))
                for parent in (record.get('merge_parent'), record['parent']):
                    if parent:
                        stack.append((parent, False))
            return list(reversed(ordered))

    def log(self):
        return self.history()

    def _ancestor_ids(self, head):
        return {record['id'] for record in self.history(head)} if head else set()

    def _merge_base(self, ours, theirs):
        left = self._ancestor_ids(ours)
        right = self._ancestor_ids(theirs)
        common = left & right
        if not common:
            raise WorkspaceError('双方没有共同祖先，无法自动合并；请显式导入所需对话')
        # The newest common ancestors must form a single base. Refuse ambiguous
        # criss-cross merges rather than silently selecting a lossy base.
        older = set()
        for identifier in common:
            record = self._commit_record(identifier)
            for key in ('parent', 'merge_parent'):
                if record.get(key):
                    older.add(record[key])
        candidates = common - older
        if len(candidates) != 1:
            raise WorkspaceError('存在多个共同合并基准，请人工整理后再合并')
        return candidates.pop()

    def merge(self, ref, message=None, resolved=False):
        with self.locked():
            state = self._state()
            ours, theirs = state['head'], self._resolve(ref, state)
            if not ours or not theirs:
                raise WorkspaceError('合并双方必须已有提交')
            expected = self._clean_snapshot(staged=resolved)
            if theirs in self._ancestor_ids(ours):
                return {'merged': False, 'fast_forward': False, 'status': self.status()}
            target_remote = state['branches'].get(ref, {}).get('remote')
            adopt_remote = bool(target_remote and state['remote'] and
                                (target_remote['url'], target_remote['package_id']) ==
                                (state['remote']['url'], state['remote']['package_id']) and
                                target_remote['local_commit'] != state['remote']['local_commit'] and
                                state['remote']['local_commit'] in self._ancestor_ids(target_remote['local_commit']))
            if ours in self._ancestor_ids(theirs) and not resolved:
                record = self._commit_record(theirs)
                data = self._object(record['sha256'])
                fast_forward = True
            else:
                base = self._merge_base(ours, theirs)
                if resolved:
                    data = self._object(state['index'])
                else:
                    result = merge_packages(read_package(self.package_bytes(base)),
                                            read_package(self.package_bytes()),
                                            read_package(self.package_bytes(theirs)))
                    if result['conflicts']:
                        raise MergeConflict(dict(result, package=None, branch=ref, base=base,
                                                 ours=ours, theirs=theirs))
                    data = result['package']
                # Existing Hub pushes validate revisions against their latest
                # checkpoint. Preserve both original audit histories in the DAG,
                # while recording this new snapshot against the applicable base.
                revision_base = self.package_bytes(target_remote['local_commit'] if adopt_remote else ours)
                package = read_package(data)
                data = _revision_data(revision_base, dict(package['library'], conversations=package['conversations'], code=package.get('code')))
                record = self._save_commit(data, message or f'合并 {ref}', ours, merge_parent=theirs)
                fast_forward = False
            state.update(head=record['id'], index=record['sha256'])
            if adopt_remote:
                state['remote'] = copy.deepcopy(target_remote)
            self._transition(state, self._files(read_package(data)), expected=expected)
            return {'merged': True, 'fast_forward': fast_forward, 'commit': record, 'status': self.status()}

    def pending_commits(self):
        with self.locked():
            state = self._state()
            checkpoint = state['remote']['local_commit'] if state['remote'] else None
            pending, identifier, seen = [], state['head'], set()
            while identifier and identifier != checkpoint:
                if identifier in seen or len(seen) >= 100000:
                    raise WorkspaceError('提交历史循环或超过读取限制')
                seen.add(identifier)
                record = self._commit_record(identifier)
                pending.append(record)
                # After pulling a divergence the Hub checkpoint is on the
                # second parent. Only the merge result and newer commits need
                # uploading; replaying the other side would undo remote edits.
                if checkpoint and checkpoint in self._ancestor_ids(record.get('merge_parent')):
                    return list(reversed(pending))
                identifier = record['parent']
            if checkpoint and identifier != checkpoint:
                raise WorkspaceError('远端跟踪提交不在本地历史中')
            return list(reversed(pending))

    def integrate_remote(self, data, remote, expected_head=None):
        """Pull a remote divergence without discarding either side on conflict."""
        read_package(data)
        with self.locked():
            state = self._state()
            if expected_head is not None and state['head'] != expected_head:
                raise WorkspaceError('本地 HEAD 已变化，拉取已取消')
            self._clean_snapshot()
            if not self.pending_commits():
                return self.checkout_remote(data, remote, expected_head)
            previous = state['remote']
            if not previous or (previous['url'], previous['package_id']) != (remote['url'], remote['package_id']):
                raise WorkspaceError('拉取不能切换到另一个远端库')
            incoming = 'incoming/' + _hash((remote['package_id'] + '/' + remote['version_id'] + '/' +
                                             previous['local_commit']).encode('utf-8'))[:16]
            stem, suffix = incoming, 0
            existing = state['branches'].get(incoming)
            # Preserve an incoming branch if the user has since committed on it.
            while existing and (not existing['remote'] or existing['head'] != existing['remote']['local_commit']):
                suffix += 1
                incoming = stem + '-' + str(suffix)
                existing = state['branches'].get(incoming)
            if existing and existing['remote'] and existing['remote']['version_id'] == remote['version_id']:
                record = self._commit_record(existing['head'])
                if self._object(record['sha256']) != data:
                    raise WorkspaceError('同一远端版本返回了不同内容，已停止合并')
            else:
                if existing:
                    raise WorkspaceError('远端临时分支名已存在，请保留并检查现有分支')
                record = self._save_commit(data, f'从 Hub 拉取版本 {remote["version_id"]}', previous['local_commit'])
            remote = copy.deepcopy(remote)
            remote['local_commit'] = record['id']
            self._validate_remote(remote)
            if len(state['branches']) >= _MAX_BRANCHES:
                raise WorkspaceError('分支数量超过限制')
            state['branches'][incoming] = {'head': record['id'], 'remote': remote}
            self._write_state(state)
            result = self.merge(incoming, message=f'合并 Hub 版本 {remote["version_id"]}')
            return result['status']

    def status(self, *, _snapshot=None):
        with self.locked():
            state = self._state()
            base = self._commit_record(state['head'])['sha256'] if state['head'] else None
            result = {'branch': state['branch'], 'head': state['head'], 'staged': state['index'] != base,
                      'modified': False, 'remote': copy.deepcopy(state['remote']),
                      'ahead': len(self.pending_commits()), 'conversations': 0,
                      'sources': self._source_list(state)}
            try:
                original = {}
                package, data = self._work(snapshot=_snapshot, original=original)
                if state['index']:
                    indexed = read_package(self._object(state['index']))
                    result['modified'] = any(original.get(key) != indexed.get(key)
                                             for key in ('library', 'conversations', 'code'))
                else:
                    result['modified'] = bool(original['conversations'])
                result['conversations'] = len(package['conversations'])
                if package.get('code'):
                    head = next(s for s in package['code']['snapshots'] if s['id'] == package['code']['head'])
                    result['code'] = {'head': head['id'], 'files': len(head['files']),
                                      'snapshots': len(package['code']['snapshots'])}
            except (WorkspaceError, PackageError) as exc:
                result.update(modified=True, error=str(exc))
            return result

    def diff(self, staged=False):
        with self.locked():
            state = self._state()
            empty = _empty_package({'title': '', 'description': '', 'source': {}})
            if staged:
                before = read_package(self.package_bytes()) if state['head'] else empty
                after = read_package(self.package_bytes('INDEX')) if state['index'] else empty
            else:
                before = read_package(self.package_bytes('INDEX')) if state['index'] else empty
                after = self._work()[0]
            return compare_packages(before, after)

    @staticmethod
    def _validate_remote(remote):
        keys = {'url', 'package_id', 'version_id', 'local_commit', 'visibility'}
        if not isinstance(remote, dict) or set(remote) != keys:
            raise WorkspaceError('远端跟踪信息字段不完整')
        if any(not isinstance(remote[key], str) or not remote[key] or len(remote[key]) > 4096
               for key in ('url', 'package_id', 'version_id')):
            raise WorkspaceError('远端跟踪信息无效')
        if remote['visibility'] not in ('public', 'private'):
            raise WorkspaceError('远端可见性无效')
        if remote['local_commit'] is not None and (
                not isinstance(remote['local_commit'], str) or not _HASH.fullmatch(remote['local_commit'])):
            raise WorkspaceError('远端本地提交指针无效')

    def checkout_remote(self, data, remote, expected_head=None):
        package = read_package(data)
        remote = copy.deepcopy(remote)
        remote.setdefault('local_commit', None)
        self._validate_remote(remote)
        with self.locked():
            state = self._state()
            if expected_head is not None and state['head'] != expected_head:
                raise WorkspaceError('本地 HEAD 已变化，拉取已取消')
            expected = {}
            status = self.status(_snapshot=expected)
            if status['staged'] or status['modified'] or status['ahead']:
                raise WorkspaceError('拉取需要干净工作区且无未推送提交；当前内容保持不变')
            if state['remote'] and (state['remote']['url'], state['remote']['package_id']) != (
                    remote['url'], remote['package_id']):
                raise WorkspaceError('拉取不能将工作区切换到另一个远端库，请另行 clone')
            record = self._save_commit(data, f'从 Hub 拉取版本 {remote["version_id"]}', state['head'])
            remote['local_commit'] = record['id']
            state.update(head=record['id'], index=record['sha256'], remote=remote)
            self._transition(state, self._files(package), expected=expected)
            return self.status()

    def mark_pushed(self, remote, expected_head):
        remote = copy.deepcopy(remote)
        remote.setdefault('local_commit', expected_head)
        self._validate_remote(remote)
        with self.locked():
            state = self._state()
            if state['head'] != expected_head:
                raise WorkspaceError('本地 HEAD 已变化，远端跟踪记录未更新')
            ancestry = [record['id'] for record in self.log()]
            if remote['local_commit'] not in ancestry:
                raise WorkspaceError('已推送提交不在本地历史中')
            previous = state['remote']
            if previous:
                if (previous['url'], previous['package_id']) != (remote['url'], remote['package_id']):
                    raise WorkspaceError('不能将已有工作区静默切换到另一个远端库')
                if previous['local_commit'] not in self._ancestor_ids(remote['local_commit']):
                    raise WorkspaceError('远端跟踪提交不能后退')
            state['remote'] = remote
            self._write_state(state)
            return self.status()
