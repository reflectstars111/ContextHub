"""A single-branch, explicit-file conversation workspace.

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
from . import safe_fs as fs


class WorkspaceError(ValueError):
    """The workspace cannot safely complete the requested operation."""


class _WorktreeChanged(WorkspaceError):
    pass


_HASH = re.compile(r'[0-9a-f]{64}\Z')
_FILE = re.compile(r'conversations/[0-9a-f]{64}\.json\Z')
_LIMIT = MAX_TOTAL_BYTES + 4 * MAX_METADATA_BYTES
_UNCHECKED = object()


def _hash(data):
    return hashlib.sha256(data).hexdigest()


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


def _check_snapshot(fd, name, expected):
    data = _read(fd, name, MAX_CONVERSATION_BYTES, missing=True)
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
    if all(parsed[key] == base[key] for key in ('library', 'conversations', 'compatibility')):
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
                for name in ('.contexthub', 'library.json', 'conversations'):
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
                    state = {'schema_version': 1, 'head': None, 'index': None,
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
        if (set(value) != required or type(value['schema_version']) is not int or value['schema_version'] != 1
                or not isinstance(value['managed'], list)
                or len(value['managed']) > MAX_CONVERSATIONS + 1
                or any(not self._valid_path(path) for path in value['managed'])
                or len(set(value['managed'])) != len(value['managed'])
                or 'library.json' not in value['managed']):
            raise WorkspaceError('工作区 state.json 无效或版本不受支持')
        for key in ('head', 'index'):
            if value[key] is not None and (not isinstance(value[key], str) or not _HASH.fullmatch(value[key])):
                raise WorkspaceError(f'工作区 {key} 无效')
        if value['remote'] is not None:
            self._validate_remote(value['remote'])
        if value['transaction'] is not None and (
                not isinstance(value['transaction'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', value['transaction'])):
            raise WorkspaceError('工作区事务标识无效')
        return value

    @staticmethod
    def _valid_path(path):
        return isinstance(path, str) and (path == 'library.json' or _FILE.fullmatch(path))

    def _path(self, path):
        if not self._valid_path(path):
            raise WorkspaceError('不安全的工作树路径')
        return (self._fds[0], path) if path == 'library.json' else (self._fds[2], path.split('/')[1])

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
        if record.get('id') != identifier:
            raise WorkspaceError('提交 ID 不一致')
        contents = {key: value for key, value in record.items() if key != 'id'}
        if (set(contents) != {'parent', 'sha256', 'message', 'created_at'}
                or _hash(_json_bytes(contents)) != identifier
                or not isinstance(contents['sha256'], str)
                or not _HASH.fullmatch(contents['sha256'])
                or (contents['parent'] is not None and (
                    not isinstance(contents['parent'], str) or not _HASH.fullmatch(contents['parent'])))):
            raise WorkspaceError('提交记录校验失败')
        return record

    def _save_commit(self, data, message, parent):
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise WorkspaceError('提交说明需要 1–2000 个字符')
        contents = {'parent': parent, 'sha256': self._save_object(data),
                    'message': message.strip(), 'created_at': datetime.now(timezone.utc).isoformat()}
        record = dict(contents, id=_hash(_json_bytes(contents)))
        with self._subdir('commits') as fd:
            _atomic(fd, record['id'] + '.json', _json_bytes(record))
        return record

    def _write_state(self, state):
        _atomic(self._fds[1], 'state.json', _json_bytes(state))

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
                or len(journal['files']) > 2 * MAX_CONVERSATIONS + 1):
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
                    current = _read(fd, name, MAX_CONVERSATION_BYTES, missing=True)
                    data = (_read(backup_fd, entry['backup'], MAX_CONVERSATION_BYTES)
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
            data = _read(fd, name, MAX_CONVERSATION_BYTES, missing=True)
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
        return result

    def _work(self, *, snapshot=None, original=None):
        state = self._state()
        raw_library = _read(self._fds[0], 'library.json', MAX_METADATA_BYTES)
        library = _load_json(raw_library, 'library.json')
        if snapshot is not None:
            snapshot['library.json'] = _hash(raw_library)
        if (set(library) != {'schema_version', 'title', 'description', 'source', 'conversation_ids'}
                or library['schema_version'] != '0.1'
                or not isinstance(library['conversation_ids'], list)
                or len(library['conversation_ids']) > MAX_CONVERSATIONS
                or any(not isinstance(item, str) for item in library['conversation_ids'])
                or len(set(library['conversation_ids'])) != len(library['conversation_ids'])):
            raise WorkspaceError('library.json 的字段或 conversation_ids 无效')
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
            if item.get('id') != identifier:
                raise WorkspaceError(f'对话 ID 与 library.json 不一致：{name}')
            conversations.append(item)
        if original is not None:
            original.update(library=library, conversations=conversations)
        if snapshot is not None:
            # A library edit can remove an ID. Capture those previously managed
            # files too, so an editor save cannot be erased by a later deletion.
            for path in state['managed']:
                if path not in snapshot:
                    fd, name = self._path(path)
                    data = _read(fd, name, MAX_CONVERSATION_BYTES, missing=True)
                    snapshot[path] = _hash(data) if data is not None else None
        if not conversations:
            # Validate empty workspace metadata using the same bounded format.
            build_package([{'id': 'validation', 'messages': []}], library['title'],
                          library['description'], source=library['source'])
            return _empty_package(library), None
        if state['head']:
            base_data = self._object(self._commit_record(state['head'])['sha256'])
            base = read_package(base_data)
            if library['source'] != base['library']['source']:
                raise WorkspaceError('已有对话库的来源记录不可改写；可修改标题、说明和对话正文')
            data = _revision_data(base_data, {'title': library['title'], 'description': library['description'],
                                              'conversations': conversations})
        else:
            data = build_package(conversations, library['title'], library['description'], source=library['source'])
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
                identifier = state['head'] if ref == 'HEAD' else ref
                if not identifier:
                    raise WorkspaceError('尚无提交，请先 ctx add 和 ctx commit')
                digest = self._commit_record(identifier)['sha256']
            if digest is None:
                raise WorkspaceError('暂存区尚无对话')
            return self._object(digest)

    def add(self, data, replace=False):
        incoming = read_package(data)
        with self.locked():
            state = self._state()
            expected = {}
            current, _ = self._work(snapshot=expected)
            conversations = {item['id']: item for item in current['conversations']}
            for item in incoming['conversations']:
                previous = conversations.get(item['id'])
                if previous is not None and previous != item and not replace:
                    raise WorkspaceError(f'对话 ID 已存在且内容不同：{item["id"]}；检查后使用 --replace')
                conversations[item['id']] = item
            library = current['library']
            if state['head']:
                data = _revision_data(self.package_bytes(), {
                    'title': library['title'], 'description': library['description'],
                    'conversations': list(conversations.values())})
            else:
                data = build_package(list(conversations.values()), library['title'],
                                     library['description'], source=library['source'])
            package = read_package(data)
            state['index'] = self._save_object(data)
            self._transition(state, self._files(package), expected=expected)
            return self.status()

    def stage(self):
        with self.locked():
            state = self._state()
            expected = {}
            package, data = self._work(snapshot=expected)
            if data is None:
                raise WorkspaceError('不能暂存空对话库；至少保留一段对话')
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

    def log(self):
        with self.locked():
            identifier = self._state()['head']
            result, seen = [], set()
            while identifier:
                if identifier in seen or len(seen) >= 100000:
                    raise WorkspaceError('提交历史循环或超过读取限制')
                seen.add(identifier)
                record = self._commit_record(identifier)
                result.append(record)
                identifier = record['parent']
            return result

    def pending_commits(self):
        with self.locked():
            remote = self._state()['remote']
            checkpoint = remote['local_commit'] if remote else None
            pending = []
            for record in self.log():
                if record['id'] == checkpoint:
                    return list(reversed(pending))
                pending.append(record)
            if checkpoint:
                raise WorkspaceError('远端跟踪提交不在本地历史中')
            return list(reversed(pending))

    def status(self, *, _snapshot=None):
        with self.locked():
            state = self._state()
            base = self._commit_record(state['head'])['sha256'] if state['head'] else None
            result = {'head': state['head'], 'staged': state['index'] != base,
                      'modified': False, 'remote': copy.deepcopy(state['remote']),
                      'ahead': len(self.pending_commits()), 'conversations': 0}
            try:
                original = {}
                package, data = self._work(snapshot=_snapshot, original=original)
                if state['index']:
                    indexed = read_package(self._object(state['index']))
                    result['modified'] = any(original[key] != indexed[key]
                                             for key in ('library', 'conversations'))
                else:
                    result['modified'] = bool(original['conversations'])
                result['conversations'] = len(package['conversations'])
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
                if ancestry.index(remote['local_commit']) > ancestry.index(previous['local_commit']):
                    raise WorkspaceError('远端跟踪提交不能后退')
            state['remote'] = remote
            self._write_state(state)
            return self.status()
