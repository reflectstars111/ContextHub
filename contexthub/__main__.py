"""Run ``python -m contexthub --help`` for the hub and transfer commands."""

import argparse
import getpass
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .package import build_package, read_package, redact_conversations


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def save(path, data, force=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb' if force else 'xb') as file:
        file.write(data)


def inspect_package(package):
    return {'title': package['library'].get('title'), 'conversation_count': len(package['conversations']),
            'conversations': [{'id': c['id'], 'title': c['title'], 'message_count': len(c['messages'])} for c in package['conversations']],
            'compatibility': package['compatibility']}


def http_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('请提供不含账号密码的 http(s) 地址')
    return value


def transfer_opener(*handlers):
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [('User-Agent', f'ContextHub/{__version__}')]
    return opener


def login_opener(url, username, password_stdin=False):
    jar = http.cookiejar.CookieJar()
    opener = transfer_opener(urllib.request.HTTPCookieProcessor(jar))
    password = sys.stdin.readline().rstrip('\n') if password_stdin else getpass.getpass('ContextHub 密码: ')
    request = urllib.request.Request(url.rstrip('/') + '/api/login', json.dumps({'username': username, 'password': password}).encode(), {'Content-Type': 'application/json'})
    with opener.open(request, timeout=30) as response:
        session = json.load(response)
    return opener, session['csrf_token']


def demo():
    return build_package([
        {'id': 'demo-research', 'title': '示例：理解一篇论文', 'context': '仅用于演示导入导出；这些是合成对话。', 'source': {'method': 'synthetic'}, 'messages': [
            {'id': 'r1', 'role': 'user', 'content': '阅读一篇论文时，如何快速找到核心贡献？'},
            {'id': 'r2', 'role': 'assistant', 'content': '先读摘要，记录研究问题、方法和结论。然后检查实验能否支持核心结论，最后整理尚未解答的问题。'},
            {'id': 'r3', 'role': 'user', 'content': '请给我一份可以复用的阅读清单。'},
            {'id': 'r4', 'role': 'assistant', 'content': '1. 研究解决了什么问题？\n2. 与已有工作相比改变了什么？\n3. 哪个实验支持主要结论？\n4. 哪些条件下可能失败？'}]},
        {'id': 'demo-code', 'title': '示例：保留代码与上下文', 'source': {'method': 'synthetic'}, 'messages': [
            {'id': 'c1', 'role': 'user', 'content': '请用 Python 统计列表中的单词。'},
            {'id': 'c2', 'role': 'assistant', 'content': '```python\nfrom collections import Counter\ncounts = Counter(["对话", "上下文", "对话"])\nprint(counts)\n```\n代码块、中文和消息顺序都会进入迁移包。'}]},
    ], 'ContextHub 演示对话库', '两段合成对话，用于体验批量上传、阅读与下载。', source={'type': 'synthetic-demo'})


def parser():
    root = argparse.ArgumentParser(prog='contexthub', description='Codex 对话导出、分享与导入工具')
    sub = root.add_subparsers(dest='action', required=True)
    for name in ('serve', 'connect'):
        p = sub.add_parser(name, help='启动分享站点' if name == 'serve' else '启动本地 Codex 连接器')
        p.add_argument('--host', default='127.0.0.1')
        p.add_argument('--port', type=int, default=8765 if name == 'serve' else 8766)
        p.add_argument('--data', default='.context-data')
        p.add_argument('--open', action='store_true', help='在浏览器中打开')
        p.add_argument('--max-connections', type=int, default=16, help='最大并发连接数（1–128，默认 16）')
        p.add_argument('--request-deadline', type=float, default=120, help='请求连接总期限秒数（默认 120，限制网络传输，不中断计算）')
        p.add_argument('--max-body-mib', type=int, default=101, help='单次请求大小上限（1–101 MiB，默认 101）')
        if name == 'serve':
            p.add_argument('--public-url', help='反向代理后的唯一 HTTPS 来源，例如 https://contexthub.example.com')
            p.add_argument('--trust-cloudflare', action='store_true', help='仅在 loopback Cloudflare 隧道后信任 CF-Connecting-IP 限流')
            p.add_argument('--max-storage-mib', type=int, default=0, help='数据目录写入预算（MiB，0 不启用预算）')
            p.add_argument('--min-free-mib', type=int, default=0, help='磁盘剩余空间保留量（MiB，0 不启用保留）')
    p = sub.add_parser('list', help='列出本机 Codex 对话（只读）')
    p.add_argument('--archived', action='store_true')
    p.add_argument('--subagents', action='store_true')
    p.add_argument('--search', default='')
    p = sub.add_parser('export', help='按 ID 批量导出 Codex 对话')
    p.add_argument('--thread', action='append', required=True)
    p.add_argument('--title', default='我的 Codex 对话库')
    p.add_argument('--description', default='')
    p.add_argument('-o', '--output', required=True)
    p.add_argument('--no-redact', action='store_true', help='关闭常见敏感信息辅助脱敏；仍需自行检查')
    p.add_argument('--force', action='store_true')
    p = sub.add_parser('inspect', help='检查迁移包摘要与兼容报告')
    p.add_argument('file')
    p = sub.add_parser('import', help='导入迁移包为新的 Codex 上下文会话')
    p.add_argument('file')
    p.add_argument('--cwd', default=str(Path.cwd()))
    p.add_argument('--dry-run', action='store_true', help='只检查迁移包，不写入 Codex')
    p = sub.add_parser('upload', help='上传迁移包到 ContextHub')
    p.add_argument('file')
    p.add_argument('--url', required=True)
    p.add_argument('--username', required=True)
    p.add_argument('--password-stdin', action='store_true')
    p.add_argument('--visibility', choices=('private', 'public'), default='private')
    p = sub.add_parser('download', help='下载指定对话库迁移包')
    p.add_argument('url', help='完整的 /api/packages/ID/download 地址')
    p.add_argument('-o', '--output', required=True)
    p.add_argument('--username')
    p.add_argument('--password-stdin', action='store_true')
    p.add_argument('--force', action='store_true')
    p = sub.add_parser('demo', help='生成不含私人数据的演示迁移包')
    p.add_argument('-o', '--output', default='demo.contextpack')
    p.add_argument('--force', action='store_true')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action in ('serve', 'connect'):
            from .server import create_server
            server = create_server(args.data, args.host, args.port, mode='hub' if args.action == 'serve' else 'connector',
                                   public_url=getattr(args, 'public_url', None), max_connections=args.max_connections,
                                   max_body_mib=args.max_body_mib, trust_cloudflare=getattr(args, 'trust_cloudflare', False),
                                   max_storage_mib=getattr(args, 'max_storage_mib', 0), min_free_mib=getattr(args, 'min_free_mib', 0),
                                   request_deadline=args.request_deadline)
            url = server.public_origin + '/' if server.public_origin else f'http://{args.host}:{server.server_port}/'
            if args.action == 'connect':
                url += '#token=' + server.connector_token
            print(f'ContextHub {args.action}: {url}', flush=True)
            if args.open:
                import webbrowser
                webbrowser.open(url)
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        if args.action == 'demo':
            save(args.output, demo(), args.force)
            emit({'output': str(Path(args.output).resolve()), 'synthetic': True})
        elif args.action == 'inspect':
            emit(inspect_package(read_package(Path(args.file).read_bytes())))
        elif args.action == 'upload':
            url = http_url(args.url)
            blob = Path(args.file).read_bytes()
            read_package(blob)
            opener, csrf = login_opener(url, args.username, args.password_stdin)
            request = urllib.request.Request(url.rstrip('/') + '/api/packages?visibility=' + args.visibility, blob, {'Content-Type': 'application/zip', 'X-CSRF-Token': csrf})
            with opener.open(request, timeout=90) as response:
                item = json.load(response)
            emit({k: item[k] for k in ('id', 'title', 'visibility', 'conversation_count', 'sha256')})
        elif args.action == 'download':
            url = http_url(args.url)
            opener = transfer_opener()
            if args.username:
                parts = urllib.parse.urlsplit(url)
                opener, _ = login_opener(f'{parts.scheme}://{parts.netloc}', args.username, args.password_stdin)
            with opener.open(url, timeout=90) as response:
                blob = response.read(101 * 1024 * 1024 + 1)
            package = read_package(blob)
            save(args.output, blob, args.force)
            emit({'output': str(Path(args.output).resolve()), **inspect_package(package)})
        elif args.action == 'import':
            package = read_package(Path(args.file).read_bytes())
            if args.dry_run:
                emit({'dry_run': True, **inspect_package(package)})
                return 0
            cwd = Path(args.cwd).resolve()
            if not cwd.is_dir():
                raise ValueError('目标项目目录不存在')
            from .codex import CodexClient, import_conversation
            results = []
            with CodexClient() as client:
                for conversation in package['conversations']:
                    try:
                        result = import_conversation(client, conversation, str(cwd))
                    except Exception as exc:
                        result = {'title': conversation['title'], 'mode': 'failed', 'verified': False,
                                  'error': f'导入未完成：{type(exc).__name__}，请核查部分创建的会话'}
                    results.append(result)
            emit({'results': results, 'note': '导入只创建上下文，不自动调用模型；请查看逐条兼容报告。'})
            return 1 if any(r.get('error') or r.get('mode') == 'failed' or r.get('injected') is False for r in results) else 0
        else:
            from .codex import CodexClient, export_conversation, list_sessions
            with CodexClient() as client:
                if args.action == 'list':
                    sessions = list_sessions(client, args.archived, args.subagents)
                    emit([s for s in sessions if args.search.casefold() in json.dumps(s, ensure_ascii=False).casefold()])
                elif args.action == 'export':
                    conversations = [export_conversation(client, ident) for ident in dict.fromkeys(args.thread)]
                    findings = []
                    if not args.no_redact:
                        conversations, findings = redact_conversations(conversations)
                    blob = build_package(conversations, args.title, args.description, source={'adapter': 'codex'})
                    save(args.output, blob, args.force)
                    emit({'output': str(Path(args.output).resolve()), 'redactions': findings, **inspect_package(read_package(blob))})
        return 0
    except KeyboardInterrupt:
        return 130
    except urllib.error.HTTPError as exc:
        print(f'HTTP {exc.code}: {exc.read(2000).decode(errors="replace")}', file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
