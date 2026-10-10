"""Git-style local conversation workspaces and authenticated Hub transfer.

Selected conversations and the Git working tree are staged together by default.
Native capture reads selected Codex threads through its local public API.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

from . import __version__
from .cli_runtime import configure_output, quote_argument
from .hub_client import HubClient, HubClientError, normalize_hub
from .importers import import_upload
from .package import MAX_ARCHIVE_BYTES, MAX_CONVERSATIONS, MAX_TOTAL_BYTES, build_package, read_package
from .workspace import Workspace, MergeConflict


DEFAULT_HUB = 'https://contexthub.reflectstars.dev'
_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


def parser():
    root = argparse.ArgumentParser(prog='ctx', description='在终端管理、提交和同步 AI 对话库')
    root.add_argument('--version', action='version', version=f'ContextHub {__version__}')
    root.add_argument('--json', action='store_true', help='输出供脚本使用的 JSON（也可放在子命令之后）')
    sub = root.add_subparsers(dest='action', required=True)
    auth = sub.add_parser('auth', help='保存或清除 Hub 登录会话').add_subparsers(dest='auth_action', required=True)
    for action, help_text in (('login', '登录并保存会话'), ('status', '检查当前账号'), ('logout', '退出并清除本机会话')):
        command = auth.add_parser(action, help=help_text)
        command.add_argument('--hub', help='Hub 地址，默认 CONTEXTHUB_HUB 或官方站点')
        if action == 'login':
            command.add_argument('--username', required=True)
            command.add_argument('--password-stdin', action='store_true', help='从标准输入读取一行密码')
    command = sub.add_parser('init', help='初始化本地对话库')
    command.add_argument('directory', nargs='?', default='.')
    command.add_argument('--title', default='我的对话库')
    command.add_argument('--description', default='')
    command = sub.add_parser('inspect', help='预览指定导出文件的对话摘要和导入提示，不保存或上传')
    command.add_argument('file')
    command.add_argument('--platform', choices=('auto', 'codex', 'chatgpt', 'claude', 'deepseek', 'other'), default='auto')
    command.add_argument('--mode', choices=('normalize', 'archive'), default='normalize')
    command = sub.add_parser('add', help='采集当前对话或导入文件；add . 刷新已跟踪对话并暂存')
    command.add_argument('files', nargs='*')
    code_options = command.add_mutually_exclusive_group()
    code_options.add_argument('--code', help='要同时采集的 Git 工作区；默认当前项目')
    code_options.add_argument('--no-code', action='store_true', help='此次只更新对话，保留已保存的代码快照')
    command.add_argument('--codex', action='store_true', help='直接采集并跟踪本机 Codex 会话，无需导出文件')
    command.add_argument('--thread', help='明确选择 Codex 会话；省略时识别当前会话或目录内唯一会话')
    command.add_argument('--before-turn', help='仅采集指定轮次之前的历史；当前会话默认排除执行中的最后一轮')
    command.add_argument('--no-refresh', action='store_true', help='与 add . 一起使用：只暂存本地 JSON，不读取 Codex')
    command.add_argument('--platform', choices=('auto', 'codex', 'chatgpt', 'claude', 'deepseek', 'other'), default='auto')
    command.add_argument('--mode', choices=('normalize', 'archive'), default='normalize')
    command.add_argument('--confirm-import', action='store_true', help='接受导入报告中的遗漏或原文存档提示')
    command.add_argument('--replace', action='store_true', help='允许替换相同 ID 的对话')
    sub.add_parser('status', help='查看暂存、未暂存修改和待推送提交')
    command = sub.add_parser('diff', help='查看对话内容差异')
    command.add_argument('--staged', action='store_true', help='比较暂存区与上次提交')
    command = sub.add_parser('commit', help='保存暂存区为不可变的本地版本')
    command.add_argument('-m', '--message', required=True)
    sub.add_parser('log', help='查看包括合并父关系的本地提交历史')
    command = sub.add_parser('branch', help='列出分支，或创建独立的本地分支')
    command.add_argument('name', nargs='?')
    command.add_argument('ref', nargs='?', default='HEAD')
    command = sub.add_parser('switch', help='切换分支；工作区必须没有未提交修改')
    command.add_argument('name')
    command.add_argument('-c', '--create', action='store_true')
    command = sub.add_parser('merge', help='三方合并；冲突时保留双方和原工作文件')
    command.add_argument('ref')
    command.add_argument('-m', '--message')
    command.add_argument('--resolved', action='store_true', help='将已检查、暂存的文件作为人工解决结果')
    command = sub.add_parser('checkpoint', help='将干净的代码提交关联到当前对话版本')
    command.add_argument('--code', required=True, help='本地 Git 代码仓库路径')
    command.add_argument('-m', '--message', required=True)
    command = sub.add_parser('restore', help='在新目录同步恢复指定版本的对话与关联代码')
    command.add_argument('ref')
    command.add_argument('--code', help='旧式检查点所需的原 Git 仓库；内嵌代码快照无需此参数')
    selection = command.add_mutually_exclusive_group()
    selection.add_argument('--snapshot', help='选择包内的代码快照 ID')
    selection.add_argument('--message', help='恢复指定消息关联的采集时代码；可用 --conversation 消歧')
    command.add_argument('--to', required=True, help='不存在的恢复目标目录')
    command.add_argument('--codex', action='store_true', help='恢复完成后导入本机 Codex')
    command.add_argument('--conversation', help='要导入 Codex 的对话 ID')
    command.add_argument('--mode', choices=('timeline', 'context'), default='timeline')
    command.add_argument('--resume', action='store_true', help='核验并复用已完成恢复；不重复原生导入')
    git = sub.add_parser('git', help='与本地普通 Git 仓库双向交换受管对话和历史').add_subparsers(dest='git_action', required=True)
    command = git.add_parser('export', help='导出分支和提交到新的普通 Git 仓库')
    command.add_argument('directory')
    command = git.add_parser('import', help='导入普通 Git 仓库的 ContextHub 格式历史到新工作区')
    command.add_argument('repository')
    command.add_argument('directory')
    command = sub.add_parser('clone', help='下载仓库或网页分享链接为本地对话库（需要登录）')
    command.add_argument('repository')
    command.add_argument('directory', nargs='?')
    command.add_argument('--hub')
    command.add_argument('--version', help='克隆指定的 Hub 历史版本')
    command = sub.add_parser('fork', help='Fork 为自己的仓库并下载到新目录')
    command.add_argument('repository')
    command.add_argument('directory', nargs='?')
    command.add_argument('--hub')
    command.add_argument('--version', help='Fork 指定的 Hub 历史版本')
    command.add_argument('--visibility', choices=('private', 'public'), default='private')
    command = sub.add_parser('push', help='按顺序推送本地提交；首次默认创建私有库')
    command.add_argument('--hub')
    command.add_argument('--visibility', choices=('private', 'public'))
    sub.add_parser('pull', help='拉取 Hub 最新版本；自动三方合并已提交的独立修改')
    sub.add_parser('remote', help='显示当前绑定的 Hub 仓库')
    command = sub.add_parser('export', help='导出已提交版本，可供 contexthub import 使用')
    command.add_argument('output')
    command = sub.add_parser('codex', help='使用本机 Codex 对话桥接：list、export、import')
    command.add_argument('codex_args', nargs=argparse.REMAINDER, help='list / export / import 及其原有参数')
    return root


def _hub(explicit=None):
    return normalize_hub(explicit or os.environ.get('CONTEXTHUB_HUB') or DEFAULT_HUB)


def _identifier(value, label):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f'{label} 必须是有效的 Hub ID')
    return value


def clone_reference(value, hub=None, version=None):
    """Parse only exact Hub share URLs, never treating URL secrets as auth."""
    if '://' not in value:
        return _hub(hub), _identifier(value, '仓库'), _identifier(version, '版本') if version else None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('分享链接不能包含控制字符')
    parsed = urllib.parse.urlsplit(value)
    if parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('请使用不含账号、密码或令牌的 Hub 网页分享链接（/?package=ID）')
    origin = normalize_hub(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '', '', '')))
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) - {'package', 'version'} or len(query.get('package', [])) != 1 or any(len(items) != 1 for items in query.values()):
        raise ValueError('分享链接只支持 package 和可选的 version 参数')
    ident = _identifier(query['package'][0], '仓库')
    linked_version = _identifier(query['version'][0], '版本') if 'version' in query else None
    if hub and _hub(hub) != origin:
        raise ValueError('--hub 与分享链接的站点不同，请明确选择同一个 Hub')
    if version and linked_version and version != linked_version:
        raise ValueError('--version 与分享链接指定的版本不同')
    return origin, ident, _identifier(version, '版本') if version else linked_version


def _require_user(client):
    session = client.session()
    if not session.get('user'):
        raise HubClientError(401, f'请先登录：ctx auth login --hub {client.origin} --username 用户名')
    return session


def _clean(status, *, pull=False):
    if status.get('modified') or status.get('staged'):
        raise ValueError('工作目录有未提交修改；请先检查 ctx diff、ctx add . 和 ctx commit -m "说明"')


def _remote_result(origin, detail, blob, local_commit=None):
    try:
        version = detail['version']
        ident = _identifier(detail['id'], '仓库')
        version_id = _identifier(version['id'], '版本')
        visibility = detail['visibility']
        digest = version['sha256']
    except (KeyError, TypeError) as exc:
        raise ValueError('Hub 返回了不完整的版本信息；本地同步记录未更新') from exc
    if visibility not in ('private', 'public') or hashlib.sha256(blob).hexdigest() != digest:
        raise ValueError('Hub 版本与传输内容不一致；本地同步记录未更新')
    result = {'url': origin, 'package_id': ident, 'version_id': version_id, 'visibility': visibility}
    if local_commit is not None:
        result['local_commit'] = local_commit
    return result


def _selected_version(detail):
    if not isinstance(detail, dict) or not isinstance(detail.get('version'), dict):
        raise ValueError('Hub 返回了不完整的版本信息')
    return _identifier(detail['version'].get('id'), '版本')


def _import_files(args):
    if len(args.files) > MAX_CONVERSATIONS:
        raise ValueError(f'一次最多选择 {MAX_CONVERSATIONS} 个文件')
    conversations, reports = {}, []
    contains_code = False
    total_read = total_normalized = 0
    single_blob = None
    for name in args.files:
        path = Path(name)
        if not path.is_file():
            raise ValueError(f'请选择具体的普通文件：{name}；ctx add . 只暂存受管对话，不会扫描项目')
        with path.open('rb') as stream:
            raw = stream.read(MAX_ARCHIVE_BYTES + 1)
        total_read += len(raw)
        if total_read > MAX_ARCHIVE_BYTES:
            raise ValueError('所选文件总大小超过迁移包上限，请分批导入')
        blob, package, report = import_upload(raw, filename=path.name, platform=args.platform, mode=args.mode)
        if report['requires_confirmation'] and not args.confirm_import:
            warning = '; '.join(report['warnings'])[:2000]
            preview = f'ctx inspect {quote_argument(path)} --platform {args.platform} --mode {args.mode}'
            raise ValueError(f'{path.name} 需要确认：{warning}\n请先使用 {preview} 检查，接受提示后添加 --confirm-import')
        contains_code = contains_code or bool(package.get('code'))
        single_blob = blob
        reports.append({'file': str(path), 'import_report': report})
        for conversation in package['conversations']:
            total_normalized += len(json.dumps(conversation, ensure_ascii=False).encode('utf-8'))
            if total_normalized > MAX_TOTAL_BYTES:
                raise ValueError('导入内容总大小超过迁移包上限，请分批导入')
            ident = conversation['id']
            if ident in conversations and conversations[ident] != conversation and not args.replace:
                raise ValueError(f'所选文件包含不同内容的相同对话 ID：{ident}；确认替换后添加 --replace')
            conversations[ident] = conversation
    if len(args.files) == 1:
        return single_blob, reports
    if contains_code:
        raise ValueError('含代码的对话包请逐个导入，避免合并来源不明的代码历史')
    return build_package(list(conversations.values()), '导入的对话'), reports


def _working_code(ws, args, conversations, previous):
    """Capture once per add, associating only the latest captured turn."""
    if getattr(args, 'no_code', False):
        return previous, {'skipped': '已选择仅更新对话'}
    selected = getattr(args, 'code', None)
    repository = Path(selected).absolute() if selected else ws.root
    if not selected and not os.path.lexists(repository / '.git'):
        return previous, {'skipped': '当前目录没有 Git 代码仓库；可用 --code 指定代码目录'}
    from .code_capture import capture_code
    bindings = []
    for conversation in conversations:
        messages = conversation['messages']
        if not messages:
            continue
        latest = messages[-1]
        provenance = latest.get('provenance')
        turn = provenance.get('turn_id') if isinstance(provenance, dict) else None
        selected_messages = ([m for m in messages if isinstance(m.get('provenance'), dict) and m['provenance'].get('turn_id') == turn]
                             if turn else [latest])
        bindings.extend({'conversation_id': conversation['id'], 'message_id': m['id']} for m in selected_messages)
    report = {}
    code = capture_code(repository, previous, bind_messages=bindings, report=report)
    return code, report


def _stage_working(ws, args):
    with ws.locked():
        package, _ = ws._work()
        code, report = _working_code(ws, args, package['conversations'], package.get('code'))
        ws.stage(code=code)
        return {'status': ws.status(), 'imports': [], 'captures': [], 'code_capture': report}


def _add(ws, args):
    selected = args.codex or args.thread is not None
    if args.no_refresh:
        if args.files != ['.'] or selected or args.before_turn or args.replace:
            raise ValueError('--no-refresh 请仅与 ctx add . 一起使用，不与采集或替换选项混用')
        if args.platform != 'auto' or args.mode != 'normalize':
            raise ValueError('ctx add . --no-refresh 不需要文件格式选项')
        return _stage_working(ws, args)
    if selected and args.files:
        raise ValueError('直接采集 Codex 与导入文件请分别执行：ctx add --codex 或 ctx add 文件路径')
    if '.' in args.files and args.files != ['.']:
        raise ValueError('ctx add . 请单独运行；导入新文件时逐个指定文件路径')
    if args.files and args.files != ['.']:
        if args.before_turn:
            raise ValueError('--before-turn 只用于 Codex 采集，不能用于导入文件')
        blob, reports = _import_files(args)
        with ws.locked():
            package = read_package(blob)
            current, _ = ws._work()
            if package.get('code') and not (args.no_code or args.code):
                code, report = package['code'], {'imported': True}
            else:
                code, report = _working_code(ws, args, package['conversations'], current.get('code'))
            ws.add(blob, replace=args.replace, code=code)
        return {'status': ws.status(), 'imports': reports, 'captures': [], 'code_capture': report}
    if args.platform != 'auto' or args.mode != 'normalize' or args.confirm_import:
        raise ValueError('直接采集或 ctx add . 不需要文件格式与 --confirm-import 选项')
    # Holding the existing workspace lock spans capture and the one atomic
    # publication of all selected sources. No partial batch is staged.
    with ws.locked():
        sources = None if selected else ws.sources()
        if args.files == ['.'] and not sources:
            if args.before_turn:
                raise ValueError('尚未跟踪会话，请先 ctx add --codex；--before-turn 只用于采集')
            return _stage_working(ws, args)
        from .capture import collect_codex
        captured = collect_codex(ws.root, thread_id=args.thread, before_turn=args.before_turn,
                                 sources=sources or None)
        package = read_package(captured['blob'])
        current, _ = ws._work()
        code, report = _working_code(ws, args, package['conversations'], current.get('code'))
        status = ws.capture(captured['blob'], captured['sources'], replace=args.replace, code=code)
        return {'status': status, 'imports': [], 'captures': captured['captures'], 'code_capture': report}


def _clone(args):
    origin, ident, version = clone_reference(args.repository, args.hub, args.version)
    destination = Path(args.directory or f"{'fork' if args.action == 'fork' else 'hub'}-{ident[:12]}")
    if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        raise ValueError('clone 目标必须是不存在的路径或空目录；现有文件不会被覆盖')
    client = HubClient(origin)
    _require_user(client)
    detail = client.detail(ident, version)
    selected = _selected_version(detail)
    if detail.get('id') != ident or (version and selected != version):
        raise ValueError('Hub 返回了不同的仓库或版本；未创建本地对话库')
    if args.action == 'fork':
        detail = client.fork(ident, selected, visibility=args.visibility)
        ident = _identifier(detail.get('id'), '仓库')
        selected = _selected_version(detail)
        version = selected
    blob = client.download(ident, selected)
    package = read_package(blob)
    remote = _remote_result(origin, detail, blob)
    if remote['package_id'] != ident or (version and remote['version_id'] != version):
        raise ValueError('Hub 返回了不同的仓库或版本；未创建本地对话库')
    ws = Workspace.init(destination, package['library']['title'], package['library'].get('description', ''))
    ws.checkout_remote(blob, remote, expected_head=None)
    return {'directory': str(ws.root), 'remote': ws.remote, 'status': ws.status()}


def _push(ws, args):
    with ws.locked():
        status = ws.status()
        _clean(status)
        original_head = ws.head
        if original_head is None:
            raise ValueError('还没有提交；请先 ctx add 文件，再 ctx commit -m "说明"')
        remote = ws.remote
        origin = normalize_hub(remote['url']) if remote else _hub(args.hub)
        if remote and args.hub and _hub(args.hub) != origin:
            raise ValueError('当前目录已绑定其他 Hub；不能用 --hub 更换同步目标')
        if remote and args.visibility and args.visibility != remote['visibility']:
            raise ValueError('当前版本的 ctx push 不能更改已绑定仓库的公开性')
        pending = ws.pending_commits()
        if not pending:
            return {'pushed': 0, 'remote': remote}
        client = HubClient(origin)
        _require_user(client)
        pushed = 0
        for commit in pending:
            blob = ws.package_bytes(commit['id'])
            if remote is not None and blob == ws.package_bytes(remote['local_commit']):
                # A topology-only merge has no new Hub snapshot to upload.
                ws.mark_pushed(dict(remote, local_commit=commit['id']), expected_head=original_head)
                remote = ws.remote
                continue
            if remote is None:
                detail = client.upload(blob, visibility=args.visibility or 'private', message=commit['message'],
                                       new_library_key=ws.upload_key())
            else:
                try:
                    detail = client.push(remote['package_id'], blob, base_version=remote['version_id'],
                                         message=commit['message'], expected_visibility=remote['visibility'])
                except HubClientError as exc:
                    if exc.status == 404:
                        raise HubClientError(404, str(exc) + '；只有仓库所有者可以推送。请确认登录账号，或用 ctx fork 将对话库复制到自己名下。') from exc
                    raise
            updated = _remote_result(origin, detail, blob, commit['id'])
            if remote and updated['package_id'] != remote['package_id']:
                raise ValueError('Hub 返回了不同的仓库；本地同步记录未更新')
            if remote and updated['visibility'] != remote['visibility']:
                raise ValueError('对话库可见性在推送期间发生变化；请核对网页设置，再 ctx pull 确认，本地同步记录未更新')
            ws.mark_pushed(updated, expected_head=original_head)
            remote = ws.remote
            pushed += 1
        return {'pushed': pushed, 'remote': remote}


def _pull(ws):
    with ws.locked():
        status = ws.status()
        _clean(status, pull=True)
        remote = ws.remote
        if remote is None:
            raise ValueError('尚未绑定远程仓库；请先 ctx push，或用 ctx clone 下载仓库')
        client = HubClient(remote['url'])
        _require_user(client)
        detail = client.detail(remote['package_id'])
        selected = _selected_version(detail)
        if selected == remote['version_id']:
            if detail.get('id') != remote['package_id'] or detail.get('visibility') not in ('private', 'public'):
                raise ValueError('Hub 返回了无效的仓库可见性信息；本地同步记录未更新')
            changed = detail['visibility'] != remote['visibility']
            if changed:
                # Pull is the explicit metadata-refresh boundary. Push always
                # uses the visibility recorded before the attempted upload.
                ws.mark_pushed(dict(remote, visibility=detail['visibility']), expected_head=ws.head)
            return {'updated': False, 'visibility_updated': changed, 'remote': ws.remote}
        head = ws.head
        blob = client.download(remote['package_id'], selected)
        read_package(blob)
        updated = _remote_result(client.origin, detail, blob)
        if updated['package_id'] != remote['package_id']:
            raise ValueError('Hub 返回了不同的仓库；本地内容未更新')
        ws.integrate_remote(blob, updated, expected_head=head)
        return {'updated': True, 'remote': ws.remote, 'status': ws.status()}


def run(args):
    if args.action == 'auth':
        client = HubClient(_hub(args.hub))
        if args.auth_action == 'login':
            password = sys.stdin.readline().rstrip('\r\n') if args.password_stdin else getpass.getpass('ContextHub 密码: ')
            if not password:
                raise ValueError('密码不能为空')
            return client.login(args.username, password)
        return client.logout() if args.auth_action == 'logout' else client.session()
    if args.action == 'inspect':
        path = Path(args.file)
        if not path.is_file():
            raise ValueError('请选择一个普通文件进行检查')
        with path.open('rb') as stream:
            raw = stream.read(MAX_ARCHIVE_BYTES + 1)
        _, package, report = import_upload(raw, filename=path.name, platform=args.platform, mode=args.mode)
        return {'file': str(path), 'title': package['library']['title'],
                'description': package['library'].get('description', ''),
                'conversation_count': len(package['conversations']), 'platforms': report['platforms'],
                'conversations': [{'id': conversation['id'], 'title': conversation['title'],
                                   'message_count': len(conversation['messages'])}
                                  for conversation in package['conversations']],
                'compatibility': package['compatibility'], 'import_report': report,
                'code': ({'head': package['code']['head'], 'snapshots': len(package['code']['snapshots']),
                          'files': len(next(s for s in package['code']['snapshots'] if s['id'] == package['code']['head'])['files'])}
                         if package.get('code') else None)}
    if args.action == 'init':
        ws = Workspace.init(Path(args.directory), args.title, args.description)
        return {'directory': str(ws.root), 'status': ws.status()}
    if args.action in ('clone', 'fork'):
        return _clone(args)
    if args.action == 'git' and args.git_action == 'import':
        from .git_bridge import import_git
        return import_git(args.repository, args.directory)
    ws = Workspace.find(Path.cwd())
    if args.action == 'checkpoint':
        from .code_sync import capture_checkpoint
        return capture_checkpoint(ws, args.code, args.message)
    if args.action == 'restore':
        if not args.code:
            from .code_sync import restore_embedded
            return restore_embedded(ws, args.ref, args.to, snapshot=args.snapshot, message=args.message,
                                    conversation=args.conversation, codex=args.codex, mode=args.mode, resume=args.resume)
        if args.snapshot or args.message:
            raise ValueError('--snapshot / --message 用于内嵌代码恢复，不与 --code 混用')
        from .code_sync import restore_pair
        return restore_pair(ws, args.ref, args.code, args.to, codex=args.codex,
                            conversation=args.conversation, mode=args.mode, resume=args.resume)
    if args.action == 'git':
        from .git_bridge import export_git
        return export_git(ws, args.directory)
    if args.action == 'branch':
        return {'branches': ws.branch(args.name, args.ref) if args.name else ws.branches()}
    if args.action == 'switch':
        return {'status': ws.switch(args.name, args.create)}
    if args.action == 'merge':
        return ws.merge(args.ref, args.message, args.resolved)
    if args.action == 'add':
        return _add(ws, args)
    if args.action == 'status':
        return ws.status()
    if args.action == 'diff':
        return ws.diff(staged=args.staged)
    if args.action == 'commit':
        return ws.commit(args.message)
    if args.action == 'log':
        return {'commits': ws.log()}
    if args.action == 'remote':
        return {'remote': ws.remote}
    if args.action == 'push':
        return _push(ws, args)
    if args.action == 'pull':
        return _pull(ws)
    if args.action == 'export':
        blob = ws.package_bytes()
        path = Path(args.output)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(blob)
        return {'output': str(path.resolve()), 'commit': ws.head, 'bytes': len(blob)}
    raise ValueError('未知命令')


def _safe(value):
    return ''.join(char if (ord(char) >= 32 and ord(char) != 127) or char in '\n\t' else f'\\x{ord(char):02x}' for char in str(value))


def _print_status(value):
    print(_safe(f"分支：{value.get('branch', 'main')}"))
    print(f"提交：{(value.get('head') or '尚无提交')[:16]}；对话：{value['conversations']}；待推送：{value['ahead']}")
    print(f"暂存修改：{'有' if value['staged'] else '无'}；未暂存修改：{'有' if value['modified'] else '无'}")
    if value.get('code'):
        print(f"代码：{value['code']['files']} 个文件；{value['code']['snapshots']} 个快照；{value['code']['head'][:12]}")
    if value.get('sources'):
        print(f"已跟踪 Codex 会话：{len(value['sources'])} 段；ctx add . 采集最新内容")
    if value.get('error'):
        print(_safe(value['error']))
    remote = value.get('remote')
    print(_safe(f"远程：{remote['url']}/?package={remote['package_id']}" if remote else '远程：未绑定（首次 push 默认创建私有库）'))


def _display(args, value):
    if args.json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.action == 'auth':
        user = value.get('user')
        print(_safe(f"{value.get('hub', _hub(args.hub))}：已登录 {user['username']}" if user else '已退出登录' if args.auth_action == 'logout' else '尚未登录；使用 ctx auth login --username 用户名'))
    elif args.action == 'inspect':
        print(_safe(f"{value['title']}：{value['conversation_count']} 段对话；来源：{', '.join(value['platforms'])}"))
        for conversation in value['conversations']:
            print(_safe(f"  {conversation['title']}（{conversation['message_count']} 条消息）"))
        if value.get('code'):
            print(f"包含代码：{value['code']['files']} 个文件，{value['code']['snapshots']} 个快照")
        warnings = list(dict.fromkeys(value['import_report'].get('warnings', []) + value['compatibility'].get('warnings', [])))
        for warning in warnings:
            print(_safe(f'提示：{warning}'))
        if value['import_report']['requires_confirmation']:
            print('接受这些提示后，可在 ctx add 时添加 --confirm-import。')
    elif args.action in ('status', 'init', 'add', 'clone', 'fork', 'switch'):
        if 'directory' in value:
            print(_safe(f"本地对话库：{value['directory']}"))
        for item in value.get('imports', []):
            print(_safe(f"已导入：{item['file']}"))
            for warning in item['import_report'].get('warnings', []):
                print(_safe(f'导入提示：{warning}'))
        for capture in value.get('captures', []):
            print(_safe(f"已采集：{capture['title']}（{capture['message_count']} 条消息）"))
            if capture.get('current_turn_excluded'):
                print('已排除当前执行中的轮次；下次 add 时再收录已结束的内容。')
            elif capture.get('before_turn'):
                print('已按指定轮次截断，不包含该轮及之后的内容。')
            if capture.get('redactions'):
                print(f"已辅助脱敏 {len(capture['redactions'])} 处；推送前可用 ctx diff --staged 检查。")
        if value.get('captures'):
            print('采集用户与助手可见正文；中断轮次可能不完整，附件与工具状态不在快照内。')
        report = value.get('code_capture', {})
        if 'captured_files' in report:
            print(f"已保存代码快照：{report['captured_files']} 个文件（包括未提交修改）")
        if report.get('excluded'):
            print(f"已排除 {len(report['excluded'])} 个本地状态或凭据路径；--json 可查看排除清单。")
        if report.get('skipped'):
            print(_safe(report['skipped']))
        _print_status(value.get('status', value))
    elif args.action == 'branch':
        for branch in value['branches']:
            print(_safe(f"{'*' if branch['current'] else ' '} {branch['name']}  {(branch['head'] or '')[:12]}"))
    elif args.action == 'merge':
        print('已合并' if value['merged'] else '已包含目标分支')
        _print_status(value['status'])
    elif args.action in ('git', 'checkpoint', 'restore'):
        print(_safe(json.dumps(value, ensure_ascii=False, indent=2)))
    elif args.action == 'commit':
        print(_safe(f"已提交 {value['id'][:12]}  {value['message']}"))
    elif args.action == 'log':
        if not value['commits']:
            print('尚无提交')
        for commit in value['commits']:
            print(_safe(f"{commit['id'][:12]}  {commit['created_at']}  {commit['message']}"))
    elif args.action == 'diff':
        summary = value['summary']
        print(f"对话：新增 {summary['conversations_added']}，删除 {summary['conversations_removed']}，修改 {summary['conversations_modified']}")
        print(f"消息：新增 {summary['messages_added']}，删除 {summary['messages_removed']}，修改 {summary['messages_modified']}，移动 {summary['messages_reordered']}")
        for change in value.get('library_changes', []):
            print(_safe(f"库信息 {change['field']}：{change['before']} → {change['after']}"))
        for change in value.get('changes', []):
            print(_safe(f"\n{change['kind']}  {change['title']} ({change['conversation_id']})"))
            for metadata in change.get('metadata', []):
                print(_safe(f"  {metadata['field']}：{metadata['before']} → {metadata['after']}"))
            for message in change.get('messages', []):
                for marker, key in (('-', 'before'), ('+', 'after')):
                    if message.get(key):
                        print(_safe(f"{marker} [{message[key]['role']}] " + message[key]['content'].replace('\n', '\n' + marker + ' ')))
        if value.get('code'):
            code = value['code']
            counts = code['summary']
            print(f"代码：新增 {counts['files_added']}，删除 {counts['files_removed']}，修改 {counts['files_modified']} 个文件")
            for file in code['files']:
                print(_safe(f"{file['status']}  {file['path']}"))
                print(_safe(file['patch']) if not file['binary'] else '二进制文件内容已变更')
            if code.get('truncated'):
                print('代码差异已截断；完整文件保存在代码快照中。')
        if value.get('truncated'):
            print('差异内容较长，显示已截断；完整内容保存在受管 JSON 文件中。')
    elif args.action == 'export':
        print(_safe(f"已导出提交 {value['commit'][:12]}：{value['output']}"))
    else:
        if args.action == 'push':
            print(f"已推送 {value['pushed']} 个提交" if value['pushed'] else '没有待推送提交')
        elif args.action == 'pull':
            print('已拉取最新版本' if value['updated'] else
                  '已更新远程可见性记录，请检查后再推送' if value.get('visibility_updated') else '已是最新版本')
        remote = value.get('remote')
        print(_safe(f"{remote['url']}/?package={remote['package_id']}\n远程版本：{remote['version_id']}（{remote['visibility']}）" if remote else '尚未绑定远程仓库'))


def main(argv=None):
    configure_output()
    argv = list(sys.argv[1:] if argv is None else argv)
    # A truly global output switch is convenient for shell aliases and scripts.
    as_json = '--json' in argv
    args = parser().parse_args([item for item in argv if item != '--json'])
    args.json = as_json
    try:
        if args.action == 'codex':
            if not args.codex_args or args.codex_args[0] not in ('list', 'export', 'import'):
                raise ValueError('ctx codex 支持 list、export、import；例如 ctx codex export --help')
            from .__main__ import main as transfer_main
            return transfer_main(args.codex_args)
        result = run(args)
        _display(args, result)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, HubClientError) as exc:
        message = str(exc)
        status = getattr(exc, 'status', None)
        if status == 401 and 'ctx auth login' not in message:
            message += '\n登录已失效或尚未登录，请使用 ctx auth login --hub 目标站点 --username 用户名'
        if status == 409:
            message += ('\n请先在网页核对公开/私有设置，再 ctx pull 更新记录，确认后重新 push。'
                        if '可见性' in message else
                        '\n远程版本已更新；本地内容保留，请使用 ctx pull 合并后重新 push。')
        if as_json:
            print(json.dumps({'error': message, **({'status': status} if status else {}),
                              **({'merge': exc.details} if isinstance(exc, MergeConflict) else {}),
                              **({'receipt': exc.receipt} if hasattr(exc, 'receipt') else {})}, ensure_ascii=False), file=sys.stderr)
        else:
            print('错误：' + _safe(message), file=sys.stderr)
            if isinstance(exc, MergeConflict):
                print(_safe(json.dumps(exc.details, ensure_ascii=False, indent=2)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
