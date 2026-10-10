"""Pair immutable conversation snapshots with local Git objects.

Git is an object reader here: source config, hooks, filters and checkout are never
executed. Restores publish a new, independent shallow repository atomically.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit, urlunsplit

from . import safe_fs as fs
from .checkpoints import checkpoint_status
from .git_bridge import _Git, _destination, _read_file, GitBridgeError
from .package import _json_bytes, read_package
from .workspace import Workspace, WorkspaceError, _atomic, _directory, _read

_OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_MAX_FILES = 50000
_MAX_BYTES = 256 * 1024 * 1024
_MAX_FILE = 32 * 1024 * 1024


class CodeSyncError(WorkspaceError):
    pass


class NativeRestoreError(CodeSyncError):
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt = receipt


class _Objects(_Git):
    def oid(self, *args, **kwargs):
        value = self.run(*args, limit=128, **kwargs).decode('ascii').strip()
        if not _OID.fullmatch(value):
            raise CodeSyncError('Git 返回了无效的完整对象标识')
        return value

    def object(self, oid, kind, limit):
        if not _OID.fullmatch(oid) or kind not in ('commit', 'tree', 'blob'):
            raise CodeSyncError('无效的 Git 对象请求')
        size = self.run('cat-file', '-s', oid, limit=30)
        if not size.strip().isdigit() or int(size) > limit:
            raise CodeSyncError('Git 对象超过文件或总量上限')
        data = self.run('cat-file', kind, oid, limit=limit)
        digest = hashlib.sha256() if len(oid) == 64 else hashlib.sha1()
        digest.update(kind.encode('ascii') + b' ' + str(len(data)).encode('ascii') + b'\0')
        digest.update(data)
        if digest.hexdigest() != oid:
            raise CodeSyncError('Git 对象内容与对象标识不匹配，拒绝关联或恢复损坏的对象库')
        return data

    def blob(self, oid, limit):
        return self.object(oid, 'blob', limit)


def _safe_directory(path):
    anchor = _directory(path)
    fs.close(anchor)


def _paths(repository):
    root = Path(os.path.abspath(os.fspath(repository)))
    _safe_directory(root)
    gitdir = root / '.git'
    if gitdir.is_file() and not gitdir.is_symlink():
        raw = _read_file(gitdir, 4096).decode('utf-8').strip()
        if not raw.startswith('gitdir: '):
            raise CodeSyncError('Git 工作区指针无效')
        gitdir = Path(os.path.abspath(root / raw[8:]))
    _safe_directory(gitdir)
    common = gitdir
    if (gitdir / 'commondir').exists():
        value = _read_file(gitdir / 'commondir', 4096).decode('utf-8').strip()
        common = Path(os.path.abspath(gitdir / value))
        _safe_directory(common)
    objects = common / 'objects'
    _safe_directory(objects)
    if os.path.lexists(objects / 'info/alternates'):
        raise CodeSyncError('代码关联要求独立 Git 对象库，不支持 alternates')
    return root, gitdir, common, objects


def _head(gitdir, common):
    value = _read_file(gitdir / 'HEAD', 4096).decode('ascii').strip()
    if _OID.fullmatch(value):
        return value, ''
    if not value.startswith('ref: refs/heads/'):
        raise CodeSyncError('代码仓库必须有已提交的 HEAD')
    ref = value[5:]
    if any(part in ('', '.', '..') for part in ref.split('/')) or '\\' in ref or any(ord(c) < 32 for c in ref):
        raise CodeSyncError('无效的 Git 分支引用')
    path = common / ref
    if os.path.lexists(path):
        commit = _read_file(path, 256).decode('ascii').strip()
    else:
        commit = ''
        packed = common / 'packed-refs'
        if os.path.lexists(packed):
            for line in _read_file(packed, 4 * 1024 * 1024).decode('ascii').splitlines():
                if not line.startswith(('#', '^')) and line.endswith(' ' + ref):
                    commit = line.split(' ', 1)[0]
    if not _OID.fullmatch(commit):
        raise CodeSyncError('代码仓库必须有已提交的有效分支')
    return commit, ref[11:]


def _init(directory, length=40):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    git = _Objects(directory)
    with tempfile.TemporaryDirectory(prefix='ctx-empty-template-') as template:
        git.run('init', '--quiet', '--bare', '--object-format=' + ('sha256' if length == 64 else 'sha1'),
                '--template=' + template, str(directory))
    return git


@contextmanager
def _source(repository, commit=None):
    root, gitdir, common, objects = _paths(repository)
    head, branch = _head(gitdir, common)
    with tempfile.TemporaryDirectory(prefix='ctx-code-objects-') as temporary:
        isolated = Path(temporary)
        _init(isolated, len(commit or head))
        git = _Objects(isolated, objects)
        # Source shallow boundaries are data, not configuration.
        if os.path.lexists(common / 'shallow'):
            shallow = _read_file(common / 'shallow', 1024 * 1024)
            if any(not _OID.fullmatch(line) for line in shallow.decode('ascii').splitlines()):
                raise CodeSyncError('无效的 Git shallow 边界')
            (isolated / 'shallow').write_bytes(shallow)
        yield git, root, gitdir, common, head, branch


def _tree(git, commit):
    # cat-file/ls-tree normally trust loose-object filenames. Hash raw bytes
    # before using them and parse the verified tree bytes ourselves, so neither
    # corruption nor a source mutation between commands can change the listing
    # while retaining the original commit claim.
    raw_commit = git.object(commit, 'commit', _MAX_FILE)
    first = raw_commit.split(b'\n', 1)[0]
    if not first.startswith(b'tree '):
        raise CodeSyncError('Git commit 缺少代码树')
    tree = first[5:].decode('ascii')
    if not _OID.fullmatch(tree) or len(tree) != len(commit):
        raise CodeSyncError('Git commit 的代码树标识无效')
    objects, files, folded = {commit, tree}, [], set()
    pending, tree_bytes, cache = [(tree, '')], 0, {}
    while pending:
        parent, prefix = pending.pop()
        if parent not in cache:
            cache[parent] = git.object(parent, 'tree', min(16 * 1024 * 1024, _MAX_BYTES - tree_bytes))
            tree_bytes += len(cache[parent])
        raw = cache[parent]
        position = 0
        while position < len(raw):
            try:
                space = raw.index(b' ', position)
                end = raw.index(b'\0', space)
                mode = raw[position:space].decode('ascii')
                name = raw[space + 1:end].decode('utf-8')
            except (ValueError, UnicodeError) as exc:
                raise CodeSyncError('Git 代码树条目格式无效') from exc
            oid_end = end + 1 + len(commit) // 2
            if oid_end > len(raw):
                raise CodeSyncError('Git 代码树对象标识不完整')
            oid = raw[end + 1:oid_end].hex()
            position = oid_end
            path = prefix + name
            if (not name or name in ('.', '..') or name.lower() == '.git' or '/' in name or '\\' in name
                    or any(ord(c) < 32 or ord(c) == 127 for c in name) or path.count('/') >= 64):
                raise CodeSyncError('代码树含不安全路径')
            try:
                fs._windows_component(name)
            except OSError as exc:
                raise CodeSyncError('代码树含不支持的跨平台文件名称') from exc
            if path.casefold() in folded:
                raise CodeSyncError('代码树含大小写冲突路径')
            folded.add(path.casefold())
            if len(folded) > _MAX_FILES:
                raise CodeSyncError('代码树最多支持 50,000 个文件和目录')
            objects.add(oid)
            if mode in ('40000', '040000'):
                pending.append((oid, path + '/'))
            elif mode in ('100644', '100755'):
                files.append((path, mode, oid))
            else:
                raise CodeSyncError('同步恢复暂不支持子模块或符号链接；请使用普通文件代码仓库')
    return tree, files, objects


def _contents(git, files):
    total = 0
    for path, mode, oid in files:
        data = git.blob(oid, min(_MAX_FILE, _MAX_BYTES - total))
        total += len(data)
        if data.startswith((b'version https://git-lfs.github.com/spec/v1\n',
                            b'version https://git-lfs.github.com/spec/v1\r\n')):
            raise CodeSyncError('同步恢复暂不支持 Git LFS 指针；需要完整的普通 Git 文件')
        yield path, mode, data


def _clean(git, root, gitdir, commit, files):
    index = _read_file(gitdir / 'index', 32 * 1024 * 1024)
    (git.repository / 'index').write_bytes(index)
    # Never refresh the source index or invoke diff drivers/filter programs.
    diff = git.run('diff-index', '--cached', '--raw', '--no-ext-diff', '--no-textconv', commit, '--', limit=1024 * 1024)
    if diff:
        raise CodeSyncError('代码暂存区有未提交修改；请先使用 Git 提交')
    for path, mode, data in _contents(git, files):
        target = root / path
        try:
            current = _read_file(target, _MAX_FILE)
        except OSError as exc:
            raise CodeSyncError('代码工作区缺少文件或包含链接；请先检查 Git 状态') from exc
        # Verify exact bytes without executing clean/smudge filters. Reject
        # transformed checkouts (including CRLF conversion) rather than claim a
        # local_clean snapshot that silently differs from the working files.
        if current != data or (os.name != 'nt' and bool(target.stat().st_mode & 0o111) != (mode == '100755')):
            raise CodeSyncError('代码工作区与提交内容不同；请先使用 Git 提交。自动换行或过滤器转换的检出暂不支持，请使用与 Git 对象字节一致的独立检出')
    git.environment['GIT_WORK_TREE'] = str(root)
    if git.run('-C', str(root), 'ls-files', '--others', '--exclude-standard', '-z', '--', limit=2 * 1024 * 1024):
        raise CodeSyncError('代码工作区有未跟踪文件；请先提交或通过 .gitignore 排除')


def _remote(git, common):
    config = common / 'config'
    if not os.path.lexists(config):
        return ''
    (git.repository / 'source-config').write_bytes(_read_file(config, 1024 * 1024))
    try:
        value = git.run('config', '--no-includes', '--file', str(git.repository / 'source-config'),
                        '--get', 'remote.origin.url', limit=8192).decode('utf-8').strip()
    except GitBridgeError:
        return ''
    if value.startswith('git@') and ':' in value:
        host, path = value[4:].split(':', 1)
        value = 'https://' + host + '/' + path
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'ssh') or not parsed.hostname or any(c.isspace() for c in value):
            return ''
        # Drop credentials, queries and fragments. Local paths never leave the connector.
        host = parsed.hostname
        if ':' in host:
            host = '[' + host + ']'
        if parsed.port and parsed.scheme == 'https':
            host += ':' + str(parsed.port)
        return urlunsplit(('https', host, parsed.path, '', ''))
    except ValueError:
        return ''


def capture_checkpoint(workspace, repository, message):
    with workspace.locked():
        workspace._clean_snapshot()
        workspace.package_bytes()
        with _source(repository) as (git, root, gitdir, common, commit, branch):
            tree, files, _ = _tree(git, commit)
            _clean(git, root, gitdir, commit, files)
            info = {'commit': commit, 'tree': tree, 'branch': branch, 'repository_url': _remote(git, common),
                    'captured_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'verification': 'local_clean'}
            if _head(gitdir, common) != (commit, branch):
                raise CodeSyncError('代码分支在关联期间发生变化；请重试')
            record = workspace.record_code_checkpoint(info, message)
            return {'conversation_commit': record['id'], 'code_checkpoint': read_package(workspace.package_bytes())['library']['code_checkpoint']}


def _write_receipt(directory, receipt):
    anchor = _directory(directory)
    try:
        _atomic(anchor, 'receipt.json', _json_bytes(receipt))
    finally:
        fs.close(anchor)


def _workspace(directory, blob):
    package = read_package(blob)
    ws = Workspace.init(directory, package['library']['title'], package['library']['description'])
    with ws.locked():
        expected = ws._clean_snapshot()
        state = ws._state()
        record = ws._save_commit(blob, 'Restore paired conversation snapshot', None)
        state.update(head=record['id'], index=record['sha256'])
        ws._transition(state, ws._files(package), expected=expected)
    return ws


def _verify_pair(destination, expected):
    _safe_directory(destination)
    try:
        receipt = json.loads(_read_file(destination / 'receipt.json', 1024 * 1024))
    except (ValueError, RecursionError) as exc:
        raise CodeSyncError('恢复目录回执不是有效 JSON') from exc
    if not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int:
        raise CodeSyncError('恢复目录回执格式无效')
    for key in ('schema_version', 'conversation_commit', 'snapshot_sha256', 'code_commit', 'code_tree', 'native_requested', 'conversation_id', 'mode'):
        if receipt.get(key) != expected[key]:
            raise CodeSyncError('恢复目录回执与本次请求不同，不能继续')
    if receipt.get('pair_status') != 'complete':
        raise CodeSyncError('恢复目录未完成；请保留现场并使用新的目标目录')
    restored = Workspace(destination / 'conversations')
    with restored.locked():
        restored._clean_snapshot()
        if hashlib.sha256(restored.package_bytes()).hexdigest() != expected['snapshot_sha256']:
            raise CodeSyncError('已恢复的对话已改变，不能复用该目录')
    with _source(destination / 'code') as (git, root, gitdir, common, commit, _):
        if commit != expected['code_commit']:
            raise CodeSyncError('已恢复的代码提交已改变')
        tree, files, _ = _tree(git, commit)
        if tree != expected['code_tree']:
            raise CodeSyncError('已恢复的代码树不匹配')
        _clean(git, root, gitdir, commit, files)
    return receipt


def restore_pair(workspace, ref, repository, destination, *, codex=False, conversation=None, mode='timeline', resume=False):
    if mode not in ('timeline', 'context'):
        raise CodeSyncError('不支持的 Codex 恢复模式')
    if conversation and not codex:
        raise CodeSyncError('--conversation 需要 --codex')
    with workspace.locked():
        commit = workspace._resolve(ref)
        blob = workspace.package_bytes(commit)
    package = read_package(blob)
    if checkpoint_status(package) != 'matched':
        raise CodeSyncError('所选对话版本缺少匹配的代码关联；请先 ctx checkpoint')
    checkpoint = package['library']['code_checkpoint']
    selected = None
    if codex:
        choices = package['conversations']
        if conversation:
            selected = next((item for item in choices if item['id'] == conversation), None)
        elif len(choices) == 1:
            selected = choices[0]
        if selected is None:
            raise CodeSyncError('请用 --conversation 指定要恢复的对话 ID')
        if selected.get('source', {}).get('kind') == 'source_archive':
            raise CodeSyncError('原文存档尚未解析为对话，不能恢复为 Codex 会话；请先转换为角色消息')
    destination = Path(os.path.abspath(os.fspath(destination)))
    receipt = {'schema_version': 1, 'conversation_commit': commit, 'snapshot_sha256': hashlib.sha256(blob).hexdigest(),
               'code_commit': checkpoint['commit'], 'code_tree': checkpoint['tree'], 'pair_status': 'preparing',
               'native_requested': codex, 'conversation_id': selected['id'] if selected else None,
               'mode': mode if codex else None, 'native_status': 'pending' if codex else 'not_requested'}
    if os.path.lexists(destination):
        if not resume:
            raise CodeSyncError('恢复目标必须不存在；核验已完成的恢复时添加 --resume')
        receipt = _verify_pair(destination, receipt)
        if codex and receipt.get('native_status') != 'complete':
            raise NativeRestoreError('对话与代码已恢复，但原生导入状态未完成或不确定；为避免重复会话，不自动重试，请检查 receipt.json', receipt)
        return dict(receipt, directory=str(destination), resumed=True)
    if resume:
        raise CodeSyncError('--resume 仅用于核验已有完整恢复目录')
    with _source(repository, checkpoint['commit']) as (git, _, _, _, _, _):
        tree, files, objects = _tree(git, checkpoint['commit'])
        if tree != checkpoint['tree']:
            raise CodeSyncError('代码对象与关联记录的树不同')
        # Bound both selected contents and the compressed object transfer.
        with _destination(destination) as (staged, _):
            _write_receipt(staged, receipt)
            code = staged / 'code'
            code.mkdir(mode=0o700)
            target = _init(code / '.git', len(checkpoint['commit']))
            pack = git.run('pack-objects', '--stdout', data=('\n'.join(sorted(objects)) + '\n').encode(), limit=_MAX_BYTES)
            target.run('index-pack', '--stdin', data=pack, limit=1024)
            # Recheck the independent copy, then read only that copy. The
            # source repository may change during packing or afterwards.
            copied_tree, files, _ = _tree(target, checkpoint['commit'])
            if copied_tree != checkpoint['tree']:
                raise CodeSyncError('复制后的代码树与关联记录不同')
            (code / '.git' / 'shallow').write_text(checkpoint['commit'] + '\n', encoding='ascii')
            target.run('config', 'core.bare', 'false')
            target.run('symbolic-ref', 'HEAD', 'refs/heads/restored')
            target.run('update-ref', 'refs/heads/restored', checkpoint['commit'])
            target.run('read-tree', checkpoint['commit'])
            for name, file_mode, data in _contents(target, files):
                path = code / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o755 if file_mode == '100755' else 0o644)
            _workspace(staged / 'conversations', blob)
            receipt['pair_status'] = 'complete'
            _write_receipt(staged, receipt)
    if codex:
        # Journal before touching native storage. Any interruption thereafter is
        # uncertain; --resume verifies the pair but never blindly creates twice.
        receipt['native_status'] = 'in_progress'
        _write_receipt(destination, receipt)
        try:
            from .codex import CodexClient, import_conversation
            with CodexClient() as client:
                result = import_conversation(client, selected, destination / 'code', mode=mode)
            receipt['native_receipt'] = result
            success = bool(result.get('verified')) if mode == 'timeline' else bool(result.get('injected'))
            if not success or result.get('mode') == 'failed':
                raise CodeSyncError('Codex 未确认所请求的导入成功')
            receipt['native_status'] = 'complete'
            _write_receipt(destination, receipt)
        except Exception as exc:
            receipt['native_status'] = 'uncertain'
            receipt['native_error'] = type(exc).__name__
            _write_receipt(destination, receipt)
            raise NativeRestoreError('代码与对话已恢复；Codex 导入未完成或状态不确定。请检查目标 receipt.json，不会自动重复导入', receipt) from exc
    return dict(receipt, directory=str(destination), resumed=False)


def _verify_embedded_code(directory, snapshot):
    """Inspect exactly the restored tree through anchored, non-link handles."""
    expected = {}
    for item in snapshot['files']:
        parts = item['path'].split('/')
        parent = expected
        for part in parts[:-1]:
            parent = parent.setdefault(part, {})
        parent[parts[-1]] = ('file', item)

    def check(anchor, nodes):
        # POSIX lists the directory descriptor; Windows ancestors remain held
        # open without delete sharing while their guarded path is enumerated.
        if set(os.listdir(getattr(anchor, 'path', anchor))) != set(nodes):
            raise CodeSyncError('已恢复的代码目录包含新增、移除或重命名的文件，不能复用')
        for name, node in nodes.items():
            information = fs.stat_entry(anchor, name)
            if isinstance(node, dict):
                if not stat.S_ISDIR(information.st_mode):
                    raise CodeSyncError('已恢复的代码目录类型发生变化，不能复用')
                child = fs.child_dir(anchor, name)
                try:
                    check(child, node)
                finally:
                    fs.close(child)
            else:
                item = node[1]
                data = _read(anchor, name, item['size'])
                if (len(data) != item['size'] or hashlib.sha256(data).hexdigest() != item['sha256']
                        or (os.name != 'nt' and bool(information.st_mode & 0o111) != (item['mode'] == '100755'))):
                    raise CodeSyncError('已恢复的代码内容或执行权限发生变化，不能复用')
        if set(os.listdir(getattr(anchor, 'path', anchor))) != set(nodes):
            raise CodeSyncError('核验期间代码目录发生变化，请重新检查')

    anchor = _directory(directory)
    try:
        check(anchor, expected)
    finally:
        fs.close(anchor)


def _verify_embedded_pair(destination, expected, snapshot):
    _safe_directory(destination)
    try:
        receipt = json.loads(_read_file(destination / 'receipt.json', 1024 * 1024))
    except (ValueError, RecursionError) as exc:
        raise CodeSyncError('恢复目录回执不是有效 JSON') from exc
    if not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int:
        raise CodeSyncError('恢复目录回执格式无效')
    for key in ('schema_version', 'restore_kind', 'conversation_commit', 'snapshot_sha256',
                'code_snapshot', 'selection', 'message_id', 'conversation_id',
                'association_semantics', 'native_requested', 'native_scope', 'mode'):
        if receipt.get(key) != expected[key]:
            raise CodeSyncError('恢复目录回执与本次请求不同，不能继续')
    if receipt.get('pair_status') != 'complete':
        raise CodeSyncError('恢复目录未完成；请保留现场并使用新的目标目录')
    restored = Workspace(destination / 'conversations')
    with restored.locked():
        restored._clean_snapshot()
        if hashlib.sha256(restored.package_bytes()).hexdigest() != expected['snapshot_sha256']:
            raise CodeSyncError('已恢复的对话包已改变，不能复用该目录')
    _verify_embedded_code(destination / 'code', snapshot)
    return receipt


def restore_embedded(workspace, ref, destination, *, snapshot=None, message=None,
                     conversation=None, codex=False, mode='timeline', resume=False):
    """Restore a self-contained code/conversation pair into a new directory.

Code selection follows a saved snapshot or a message's explicit capture-time
association. The complete original package is always retained in conversations.
Only native Codex import is truncated through a selected message; selecting a
snapshot alone does not invent a matching historic conversation prefix.
"""
    if mode not in ('timeline', 'context'):
        raise CodeSyncError('不支持的 Codex 恢复模式')
    if snapshot is not None and message is not None:
        raise CodeSyncError('--snapshot 与 --message 只能选择一个代码恢复依据')
    with workspace.locked():
        commit = workspace._resolve(ref)
        blob = workspace.package_bytes(commit)
    package = read_package(blob)
    code = package.get('code')
    if code is None:
        raise CodeSyncError('所选版本没有携带代码；请先 ctx add . 保存代码和对话，旧 Git 关联可使用 --code')
    choices = package['conversations']
    selected = None
    if conversation is not None:
        selected = next((item for item in choices if item['id'] == conversation), None)
        if selected is None:
            raise CodeSyncError('找不到指定的对话 ID')
    selection = 'snapshot' if snapshot is not None else 'head'
    if message is not None:
        matches = [item for item in code['bindings'] if item['message_id'] == message
                   and (conversation is None or item['conversation_id'] == conversation)]
        if not matches:
            raise CodeSyncError('这条消息没有已记录的代码关联；无法推断历史代码，请选择已保存快照')
        if len(matches) > 1:
            raise CodeSyncError('多个对话含有相同消息 ID，请用 --conversation 明确选择')
        binding = matches[0]
        snapshot = binding['snapshot_id']
        selected = next(item for item in choices if item['id'] == binding['conversation_id'])
        selection = 'message'
    ident = code['head'] if snapshot is None else snapshot
    code_snapshot = next((item for item in code['snapshots'] if item['id'] == ident), None)
    if code_snapshot is None:
        raise CodeSyncError('找不到指定的代码快照')
    native = None
    if codex:
        if selected is None and len(choices) == 1:
            selected = choices[0]
        if selected is None:
            raise CodeSyncError('请用 --conversation 指定要导入 Codex 的对话 ID')
        if selected.get('source', {}).get('kind') == 'source_archive':
            raise CodeSyncError('原文存档尚未解析为对话，不能恢复为 Codex 会话；请先转换为角色消息')
        native = copy.deepcopy(selected)
        if message is not None:
            cutoff = next(index for index, item in enumerate(native['messages']) if item['id'] == message)
            native['messages'] = native['messages'][:cutoff + 1]
            native['source'].setdefault('warnings', []).append(
                '本次原生导入截断至所选消息；代码来自其保存时关联快照，不代表当时的原生执行状态。完整对话包另行保留。')
    destination = Path(os.path.abspath(os.fspath(destination)))
    receipt = {
        'schema_version': 1, 'restore_kind': 'embedded', 'conversation_commit': commit,
        'snapshot_sha256': hashlib.sha256(blob).hexdigest(), 'code_snapshot': ident,
        'selection': selection, 'message_id': message,
        'conversation_id': selected['id'] if selected else None,
        'association_semantics': 'save_time_snapshot', 'pair_status': 'preparing',
        'native_requested': codex, 'native_scope': ('through_message' if message is not None else 'full_conversation') if codex else None,
        'mode': mode if codex else None, 'native_status': 'pending' if codex else 'not_requested',
        'warnings': ['代码关联表示保存时采集的文件，不证明消息生成时的原生执行状态；完整原始对话包保留在 conversations。'],
    }
    if os.path.lexists(destination):
        if not resume:
            raise CodeSyncError('恢复目标必须不存在；核验已完成的恢复时添加 --resume')
        receipt = _verify_embedded_pair(destination, receipt, code_snapshot)
        if codex and receipt.get('native_status') != 'complete':
            raise NativeRestoreError('对话与代码已恢复，但原生导入状态未完成或不确定；为避免重复会话，不自动重试，请检查 receipt.json', receipt)
        return dict(receipt, directory=str(destination), resumed=True)
    if resume:
        raise CodeSyncError('--resume 仅用于核验已有完整恢复目录')
    from .code_capture import restore_code
    with _destination(destination) as (staged, _):
        _write_receipt(staged, receipt)
        restore_code(code, staged / 'code', snapshot=ident)
        _workspace(staged / 'conversations', blob)
        _verify_embedded_code(staged / 'code', code_snapshot)
        receipt['pair_status'] = 'complete'
        _write_receipt(staged, receipt)
    if codex:
        # Persist the retry boundary before creating native session state.
        # A failed or interrupted import never causes a second import on resume.
        receipt['native_status'] = 'in_progress'
        _write_receipt(destination, receipt)
        try:
            from .codex import CodexClient, import_conversation
            with CodexClient() as client:
                result = import_conversation(client, native, destination / 'code', mode=mode)
            receipt['native_receipt'] = result
            success = bool(result.get('verified')) if mode == 'timeline' else bool(result.get('injected'))
            if not success or result.get('mode') == 'failed':
                raise CodeSyncError('Codex 未确认所请求的导入成功')
            receipt['native_status'] = 'complete'
            _write_receipt(destination, receipt)
        except Exception as exc:
            receipt['native_status'] = 'uncertain'
            receipt['native_error'] = type(exc).__name__
            _write_receipt(destination, receipt)
            raise NativeRestoreError('代码与对话已恢复；Codex 导入未完成或状态不确定。请检查目标 receipt.json，不会自动重复导入', receipt) from exc
    return dict(receipt, directory=str(destination), resumed=False)
