"""Explicit offline interchange with ordinary Git repositories.

Only ContextHub's marked JSON snapshot format is imported. Git plumbing runs
against a fresh configuration, never the source repository's hooks, filters,
configuration or working files. Users perform network clone/push with Git.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

from .package import (MAX_ARCHIVE_BYTES, MAX_CONVERSATION_BYTES, MAX_METADATA_BYTES,
                      MAX_TOTAL_BYTES, _json_bytes, _load_json, build_package, read_package)
from .workspace import (Workspace, WorkspaceError, _branch_name, _directory,
                        _filename, _hash, _revision_data)
from . import safe_fs as fs

_MARKER = '.contexthub-git.json'
_SNAPSHOT = 'snapshot.contextpack'
_FORMAT = 'contexthub-git'
_OID = re.compile(r'[0-9a-f]{40}\Z')
_MAX_HISTORY = 10000
_MAX_TRANSFER = 512 * 1024 * 1024
_MAX_TREE_BYTES = 2 * 1024 * 1024


class GitBridgeError(WorkspaceError):
    pass


class _Git:
    def __init__(self, repository, object_directory=None):
        self.repository = Path(repository)
        executable = shutil.which('git')
        if not executable or Path(executable).suffix.lower() in ('.bat', '.cmd'):
            raise GitBridgeError('需要安装原生 Git，并将 git/git.exe 加入 PATH')
        self.executable = executable
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.upper().startswith('GIT_')}
        self.environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                                GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT='0',
                                GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1',
                                GIT_OPTIONAL_LOCKS='0', LC_ALL='C',
                                GIT_AUTHOR_NAME='ContextHub', GIT_AUTHOR_EMAIL='contexthub@localhost',
                                GIT_COMMITTER_NAME='ContextHub', GIT_COMMITTER_EMAIL='contexthub@localhost')
        if object_directory:
            self.environment['GIT_OBJECT_DIRECTORY'] = str(object_directory)

    def run(self, *args, data=None, limit=MAX_ARCHIVE_BYTES, date=None):
        environment = self.environment.copy()
        if date:
            environment.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        command = [self.executable, '--no-pager', '-c', 'core.hooksPath=' + os.devnull,
                   '-c', 'core.fsmonitor=false', '-c', 'protocol.allow=never',
                   '-c', 'commit.gpgSign=false', '-c', 'gc.auto=0',
                   '--git-dir=' + str(self.repository), *args]
        # Stream a bounded amount, then kill the producer immediately. A large
        # hostile tree must not fill RAM or a temporary filesystem first.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as input_file:
            if data:
                input_file.write(data)
                input_file.seek(0)
            try:
                process = subprocess.Popen(command, stdin=input_file, stdout=subprocess.PIPE,
                                           stderr=errors, env=environment)
            except OSError as exc:
                raise GitBridgeError('无法启动 Git') from exc
            exceeded = []
            def consume():
                with process.stdout:
                    total = 0
                    while chunk := process.stdout.read(65536):
                        total += len(chunk)
                        if total > limit:
                            exceeded.append(True)
                            process.kill()
                            return
                        output.write(chunk)
            reader = threading.Thread(target=consume, daemon=True)
            reader.start()
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise GitBridgeError('Git 超过 60 秒限制') from exc
            finally:
                reader.join(timeout=5)
            if exceeded or reader.is_alive():
                raise GitBridgeError('Git 输出超过导入大小限制')
            if process.returncode:
                errors.seek(0)
                raise GitBridgeError('Git 操作失败：' + errors.read(2000).decode('utf-8', 'replace'))
            output.seek(0)
            return output.read(limit + 1)

    def oid(self, *args, **kwargs):
        value = self.run(*args, limit=100, **kwargs).decode('ascii').strip()
        if not _OID.fullmatch(value):
            raise GitBridgeError('Git 返回了无效的对象标识；当前支持 SHA-1 格式普通仓库')
        return value

    def blob(self, oid, limit):
        if not _OID.fullmatch(oid):
            raise GitBridgeError('无效的 Git 对象标识')
        size = self.run('cat-file', '-s', oid, limit=30)
        if not size.strip().isdigit() or int(size) > limit:
            raise GitBridgeError('Git 文件超过大小限制')
        return self.run('cat-file', 'blob', oid, limit=limit)

    def tree(self, files):
        nodes = {}
        for path, data in files.items():
            parts = path.split('/')
            if len(parts) == 2:
                nodes.setdefault(parts[0], {})[parts[1]] = data
            else:
                nodes[path] = data
        records = []
        for name, value in sorted(nodes.items()):
            if isinstance(value, dict):
                oid = self.tree(value)
                kind = '040000 tree'
            else:
                oid = self.oid('hash-object', '-w', '--stdin', data=value)
                kind = '100644 blob'
            records.append(f'{kind} {oid}\t{name}'.encode('utf-8') + b'\0')
        return self.oid('mktree', '-z', data=b''.join(records))


def _init_git(directory, bare=False):
    directory = Path(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    gitdir = directory if bare else directory / '.git'
    git = _Git(gitdir)
    # A blank explicit template prevents global/system template hooks.
    with tempfile.TemporaryDirectory(prefix='ctx-git-template-') as template:
        args = ['init', '--quiet', '--object-format=sha1', '--initial-branch=main', '--template=' + template]
        if bare:
            args.append('--bare')
        args.append(str(directory))
        git.run(*args)
    return git


@contextmanager
def _destination(destination):
    path = Path(os.path.abspath(os.fspath(destination)))
    parent = _directory(path.parent)
    try:
        if path.exists() or path.is_symlink():
            raise GitBridgeError('目标目录必须不存在；请使用新的目录以保留已有内容')
        staged = Path(tempfile.mkdtemp(prefix='.ctx-git-', dir=path.parent))
        published = False
        try:
            yield staged, path
            # Keep both directories under the same guarded parent. Windows
            # native relative rename avoids reopening guarded ancestors; both
            # platforms atomically fail if another process created the target.
            fs.rename_dir(parent, staged.name, path.name)
            published = True
        finally:
            if not published:
                shutil.rmtree(staged)
    finally:
        fs.close(parent)


def _git_date(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        raise GitBridgeError('提交时间格式无效') from None


def export_git(workspace, directory):
    """Export every branch and reachable commit into a new standard Git repo."""
    with workspace.locked(), _destination(directory) as (staged, destination):
        workspace._clean_snapshot()
        branches = workspace.branches()
        if not workspace.head:
            raise GitBridgeError('请先保存首次 ctx 提交再导出')
        git = _init_git(staged)
        mapped, total = {}, 0
        for branch in branches:
            if branch['head'] is None:
                continue
            for record in reversed(workspace.history(branch['head'])):
                if record['id'] in mapped:
                    continue
                if len(mapped) >= _MAX_HISTORY:
                    raise GitBridgeError('Git 交换最多支持 10,000 个提交')
                blob = workspace.package_bytes(record['id'])
                total += len(blob)
                if total > _MAX_TRANSFER:
                    raise GitBridgeError('Git 交换快照总量超过 512 MiB，请拆分对话库')
                files = workspace._files(read_package(blob))
                files[_SNAPSHOT] = blob
                files[_MARKER] = _json_bytes({'format': _FORMAT, 'version': 1, 'commit': record})
                tree = git.tree(files)
                parents = []
                for key in ('parent', 'merge_parent'):
                    if record.get(key):
                        parents.extend(('-p', mapped[record[key]]))
                mapped[record['id']] = git.oid('commit-tree', tree, *parents,
                                               data=record['message'].encode('utf-8'), date=_git_date(record['created_at']))
            git.run('update-ref', 'refs/heads/' + branch['name'], mapped[branch['head']])
        current = next(branch['name'] for branch in branches if branch['current'])
        git.run('symbolic-ref', 'HEAD', 'refs/heads/' + current)
        git.run('read-tree', mapped[workspace.head])
        # Write only the already validated managed files, never git checkout.
        files = workspace._files(read_package(workspace.package_bytes()))
        files[_SNAPSHOT] = workspace.package_bytes()
        files[_MARKER] = _json_bytes({'format': _FORMAT, 'version': 1, 'commit': workspace._commit_record(workspace.head)})
        for name, data in files.items():
            path = staged / name
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.write_bytes(data)
        return {'directory': str(destination), 'commits': len(mapped), 'branches': len(branches),
                'format': _FORMAT, 'current_branch': current}


def _read_file(path, limit):
    """Read source refs as data without allowing links or special files."""
    from .workspace import _read
    parent = _directory(path.parent)
    try:
        return _read(parent, path.name, limit)
    finally:
        fs.close(parent)


def _source_refs(directory):
    directory = Path(os.path.abspath(os.fspath(directory)))
    gitdir = directory / '.git' if (directory / '.git').exists() else directory
    anchor = _directory(gitdir)
    objects = gitdir / 'objects'
    object_anchor = _directory(objects)
    fs.close(anchor)
    fs.close(object_anchor)
    if (objects / 'info/alternates').exists() or (objects / 'info/alternates').is_symlink():
        raise GitBridgeError('不导入带 objects/info/alternates 的仓库；请先创建独立普通克隆')
    refs = {}
    packed = gitdir / 'packed-refs'
    if packed.exists() or packed.is_symlink():
        for line in _read_file(packed, MAX_METADATA_BYTES).decode('ascii').splitlines():
            if line.startswith(('#', '^')) or not line:
                continue
            parts = line.split(' ')
            if len(parts) != 2 or not _OID.fullmatch(parts[0]):
                raise GitBridgeError('Git packed-refs 无效')
            if parts[1].startswith('refs/heads/'):
                refs[_branch_name(parts[1][11:])] = parts[0]
    heads = gitdir / 'refs/heads'
    ref_entries = 0
    if heads.exists() or heads.is_symlink():
        head_anchor = _directory(heads)
        fs.close(head_anchor)
        for parent, dirs, files in os.walk(heads, followlinks=False):
            ref_entries += len(dirs) + len(files)
            if ref_entries > 10000:
                raise GitBridgeError('Git 分支目录项目数超过限制')
            for name in dirs:
                handle = _directory(Path(parent) / name)
                fs.close(handle)
            for name in files:
                path = Path(parent) / name
                branch = _branch_name(path.relative_to(heads).as_posix())
                value = _read_file(path, 100).decode('ascii').strip()
                if not _OID.fullmatch(value):
                    raise GitBridgeError('不支持符号分支或无效的 Git 分支对象')
                refs[branch] = value
                if len(refs) > 1000:
                    raise GitBridgeError('Git 分支超过 1,000 个限制')
    if not refs or len(refs) > 1000:
        raise GitBridgeError('Git 仓库没有可导入的分支，或超过 1,000 个限制')
    head = _read_file(gitdir / 'HEAD', 256).decode('ascii').strip()
    current = head[16:] if head.startswith('ref: refs/heads/') else None
    if current not in refs:
        raise GitBridgeError('导入前请用普通 Git 切换到一个本地分支，不支持 detached HEAD')
    return objects, refs, current


def _snapshot(git, oid):
    listing = git.run('ls-tree', '-r', '-z', oid, limit=_MAX_TREE_BYTES)
    files = {}
    for entry in listing.split(b'\0'):
        if not entry:
            continue
        try:
            metadata, name = entry.split(b'\t', 1)
            mode, kind, object_id = metadata.decode('ascii').split(' ')
            path = name.decode('utf-8')
        except (UnicodeError, ValueError):
            raise GitBridgeError('Git tree 包含不支持的文件名') from None
        if path in (_MARKER, _SNAPSHOT, 'library.json') or path.startswith('conversations/'):
            if (path in files or mode != '100644' or kind != 'blob' or not _OID.fullmatch(object_id)
                    or (path.startswith('conversations/') and not Workspace._valid_path(path))):
                raise GitBridgeError('Git 受管路径包含链接、重复文件、可执行文件或不安全路径')
            files[path] = object_id
    if _MARKER not in files or 'library.json' not in files:
        raise GitBridgeError('Git 提交缺少 ContextHub 格式标记或 library.json')
    marker = _load_json(git.blob(files[_MARKER], MAX_METADATA_BYTES), _MARKER)
    if not isinstance(marker, dict) or set(marker) != {'format', 'version', 'commit'} or marker['format'] != _FORMAT or type(marker['version']) is not int or marker['version'] != 1:
        raise GitBridgeError('不支持的 ContextHub Git 格式')
    library = _load_json(git.blob(files['library.json'], MAX_METADATA_BYTES), 'library.json')
    if not isinstance(library, dict) or set(library) - {'code_checkpoint'} != {'schema_version', 'title', 'description', 'source', 'conversation_ids'}:
        raise GitBridgeError('Git library.json 格式无效')
    identifiers = library['conversation_ids']
    if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 200 or any(not isinstance(i, str) for i in identifiers):
        raise GitBridgeError('Git 对话 ID 列表无效')
    expected = {_filename(identifier) for identifier in identifiers}
    if expected != {path for path in files if path.startswith('conversations/')}:
        raise GitBridgeError('Git 对话文件与 library.json 不一致')
    conversations, total = [], 0
    for identifier in identifiers:
        data = git.blob(files[_filename(identifier)], MAX_CONVERSATION_BYTES)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise GitBridgeError('Git 对话文件总量超过限制')
        conversation = _load_json(data, identifier)
        if not isinstance(conversation, dict) or conversation.get('id') != identifier:
            raise GitBridgeError('Git 对话身份与文件名不一致')
        conversations.append(conversation)
    canonical = build_package(conversations, library['title'], library['description'], source=library['source'],
                              code_checkpoint=library.get('code_checkpoint'))
    checked = read_package(canonical)
    if checked['library'] != library or checked['conversations'] != conversations:
        raise GitBridgeError('Git JSON 必须使用导出的规范 ContextHub 格式')
    original = None
    if _SNAPSHOT in files:
        original = git.blob(files[_SNAPSHOT], MAX_ARCHIVE_BYTES)
        package = read_package(original)
        if package['library'] != library or package['conversations'] != conversations:
            original = None  # Ordinary Git edits can leave the old ZIP untouched.
    return original or canonical, marker['commit'], original is not None


def import_git(directory, destination):
    """Import committed branch history; ignore source working tree and config."""
    objects, refs, current = _source_refs(directory)
    with tempfile.TemporaryDirectory(prefix='ctx-git-reader-') as reader, _destination(destination) as (staged, target):
        _init_git(reader, bare=True)
        git = _Git(reader, object_directory=objects)
        rows = git.run('rev-list', '--topo-order', '--reverse', '--parents',
                       '--max-count=' + str(_MAX_HISTORY + 1), *sorted(set(refs.values())),
                       limit=_MAX_TREE_BYTES).decode('ascii').splitlines()
        if not rows or len(rows) > _MAX_HISTORY:
            raise GitBridgeError('Git 交换需要 1–10,000 个提交')
        mapped, total, ws = {}, 0, None
        for row in rows:
            parts = row.split(' ')
            if any(not _OID.fullmatch(part) for part in parts) or len(parts) > 3:
                raise GitBridgeError('仅支持零、一或两个父提交的 Git 历史')
            oid, parents = parts[0], parts[1:]
            if any(parent not in mapped for parent in parents):
                raise GitBridgeError('Git 历史不完整或不按父关系排序')
            blob, record, exact = _snapshot(git, oid)
            total += len(blob)
            if total > _MAX_TRANSFER:
                raise GitBridgeError('Git 交换快照总量超过 512 MiB')
            package = read_package(blob)
            if ws is None:
                ws = Workspace.init(staged, package['library']['title'], package['library']['description'])
            with ws.locked():
                parent_ids = [mapped[parent] for parent in parents]
                expected_parents = [record.get(key) for key in ('parent', 'merge_parent') if record.get(key)] if isinstance(record, dict) else None
                contents = {key: value for key, value in record.items() if key != 'id'} if isinstance(record, dict) else {}
                # Preserve exact ctx IDs only when the data and ancestry verify.
                expected_fields = {'id', 'parent', 'sha256', 'message', 'created_at'}
                valid_record = (isinstance(record, dict) and set(record) in (expected_fields, expected_fields | {'merge_parent'})
                                and isinstance(record.get('message'), str) and 1 <= len(record['message']) <= 2000
                                and isinstance(record.get('created_at'), str) and len(record['created_at']) <= 100)
                if (exact and valid_record and _hash(_json_bytes(contents)) == record.get('id')
                        and record.get('sha256') == _hash(blob) and expected_parents == parent_ids):
                    _git_date(record['created_at'])
                    saved = ws._save_commit(blob, record['message'], record['parent'],
                                            record.get('merge_parent'), record['created_at'])
                    if saved['id'] != record['id']:
                        raise GitBridgeError('Git 中保存的 ctx 提交记录无效')
                    ws._commit_record(saved['id'])
                else:
                    if parent_ids:
                        blob = _revision_data(ws.package_bytes(parent_ids[0]), dict(package['library'], conversations=package['conversations']))
                    raw_commit = git.run('cat-file', 'commit', oid, limit=MAX_METADATA_BYTES)
                    headers, _, body = raw_commit.partition(b'\n\n')
                    body = body.decode('utf-8', 'replace').strip()
                    created_at = None
                    for line in headers.splitlines():
                        if line.startswith(b'committer '):
                            try:
                                timestamp = int(line.rsplit(b' ', 2)[1])
                                created_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                            except (ValueError, OverflowError, OSError):
                                raise GitBridgeError('Git 提交时间无效') from None
                    if created_at is None:
                        raise GitBridgeError('Git 提交缺少提交时间')
                    saved = ws._save_commit(blob, (body or '从 Git 导入提交')[:2000],
                                            parent_ids[0] if parent_ids else None,
                                            parent_ids[1] if len(parent_ids) == 2 else None, created_at=created_at)
                mapped[oid] = saved['id']
        with ws.locked():
            state = ws._state()
            expected = {}; ws._work(snapshot=expected)
            state['branches'] = {name: {'head': mapped[oid], 'remote': None} for name, oid in refs.items()}
            state['upload_keys'] = {}
            state.update(branch=current, head=mapped[refs[current]], remote=None)
            head = ws._commit_record(state['head'])
            state['index'] = head['sha256']
            ws._transition(state, ws._files(read_package(ws._object(head['sha256']))), expected=expected)
        return {'directory': str(target), 'commits': len(mapped), 'branches': len(refs),
                'format': _FORMAT, 'current_branch': current}
