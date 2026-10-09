"""HTTP hub and separately launched, token-protected local Codex connector."""

import hmac
import json
import mimetypes
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .package import build_package, read_package, redact_conversations
from .store import Store, StoreConflict
from .revisions import build_revision, compare_packages
from . import __version__

MAX_BODY = 101 * 1024 * 1024
STATIC = Path(__file__).parent / 'static'


class HTTPError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def create_server(data_dir, host='127.0.0.1', port=8765, mode='hub', token=None, codex_command=None):
    if mode not in ('hub', 'connector'):
        raise ValueError('invalid server mode')
    if mode == 'connector' and host not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('本地连接器只能绑定 loopback 地址')
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.mode = mode
    server.store = Store(data_dir) if mode == 'hub' else None
    server.connector_token = token or secrets.token_urlsafe(32)
    server.codex_command = codex_command
    server.codex_lock = threading.Lock()
    server.auth_attempts = {}
    server.auth_lock = threading.Lock()
    return server


class Handler(BaseHTTPRequestHandler):
    server_version = 'ContextHub/0.1'

    def log_message(self, fmt, *args):
        # Keep credentials, URL parameters and conversation bodies out of logs.
        pass

    def _send(self, status, body, content_type='application/json; charset=utf-8', headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _body(self, limit=MAX_BODY):
        if self.headers.get('Transfer-Encoding'):
            raise HTTPError(400, '不支持 chunked 请求，请提供 Content-Length')
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError as exc:
            raise HTTPError(400, '无效的 Content-Length') from exc
        if size < 0 or size > limit:
            raise HTTPError(413, '文件超过大小限制，请分批传输')
        self.connection.settimeout(30)
        data = self.rfile.read(size)
        if len(data) != size:
            raise HTTPError(400, '请求未完整上传')
        return data

    def _json(self, limit=MAX_BODY):
        try:
            value = json.loads(self._body(limit))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPError(422, '无效的 JSON') from exc
        if not isinstance(value, dict):
            raise HTTPError(422, '请求应为 JSON 对象')
        return value

    def _origin(self):
        origin = self.headers.get('Origin')
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ('http', 'https') or parsed.netloc != self.headers.get('Host'):
                raise HTTPError(403, '拒绝跨来源请求')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise HTTPError(403, '拒绝跨站请求')

    def _token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return ''
        return cookie['ch_session'].value if 'ch_session' in cookie else ''

    def _session(self, required=False, mutate=False):
        session = self.server.store.session(self._token())
        if required and not session['user']:
            raise HTTPError(401, '请先登录')
        if mutate:
            self._origin()
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf_token'] or '') or not session['csrf_token']:
                raise HTTPError(403, '会话校验失败，请刷新后重试')
        return session

    def _auth_rate_limit(self):
        now = time.monotonic()
        address = self.client_address[0]
        with self.server.auth_lock:
            attempts = [t for t in self.server.auth_attempts.get(address, []) if now - t < 60]
            if len(attempts) >= 20:
                raise HTTPError(429, '登录尝试过多，请稍后重试')
            attempts.append(now)
            self.server.auth_attempts[address] = attempts

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_DELETE(self):
        self._handle('DELETE')

    def _handle(self, method):
        try:
            path = unquote(urlsplit(self.path).path)
            query = parse_qs(urlsplit(self.path).query)
            if path == '/api/config' and method == 'GET':
                return self._send(200, {'mode': self.server.mode, 'version': __version__, 'max_package_bytes': MAX_BODY})
            if path.startswith('/api/local/'):
                return self._local(method, path, query)
            if path.startswith('/api/'):
                if self.server.mode != 'hub':
                    raise HTTPError(404, '此接口不属于本地连接器')
                return self._hub(method, path, query)
            if method == 'GET':
                return self._static(path)
            raise HTTPError(404, '未找到此资源')
        except HTTPError as exc:
            self._send(exc.status, {'error': str(exc)})
        except FileNotFoundError as exc:
            self._send(404, {'error': str(exc)})
        except PermissionError as exc:
            self._send(403, {'error': str(exc)})
        except StoreConflict as exc:
            self._send(409, {'error': str(exc)})
        except (ValueError, TypeError, KeyError) as exc:
            self._send(422, {'error': str(exc)[:500]})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except TimeoutError:
            self._send(504, {'error': '操作超时，请检查本地 Codex 是否可用'})
        except Exception as exc:
            # Never echo upstream provider credentials or raw process output.
            self._send(500, {'error': f'操作未完成（{type(exc).__name__}），请查看终端诊断或稍后重试'})

    def _static(self, path):
        allowed = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js', '/versions.js': 'versions.js', '/style.css': 'style.css'}
        if path not in allowed:
            raise HTTPError(404, '未找到页面')
        file = STATIC / allowed[path]
        return self._send(200, file.read_bytes(), (mimetypes.guess_type(str(file))[0] or 'text/plain') + '; charset=utf-8')

    def _hub(self, method, path, query):
        store = self.server.store
        if path == '/api/session' and method == 'GET':
            return self._send(200, self._session())
        if path in ('/api/register', '/api/login') and method == 'POST':
            self._origin()
            self._auth_rate_limit()
            data = self._json(4096)
            if path.endswith('register'):
                result, token = store.register(data.get('username'), data.get('password'))
                status = 201
            else:
                result, token = store.login(data.get('username'), data.get('password'))
                status = 200
            return self._send(status, result, headers={'Set-Cookie': f'ch_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800'})
        if path == '/api/logout' and method == 'POST':
            self._session(required=True, mutate=True)
            store.logout(self._token())
            return self._send(200, {'user': None, 'csrf_token': None}, headers={'Set-Cookie': 'ch_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'})
        if path == '/api/inspect' and method == 'POST':
            self._session(required=True, mutate=True)
            package = read_package(self._body())
            return self._send(200, {k: package[k] for k in ('library', 'conversations', 'compatibility')})
        if path == '/api/packages':
            if method == 'GET':
                session = self._session()
                return self._send(200, {'packages': store.list_packages((session['user'] or {}).get('id'), query.get('q', [''])[0])})
            if method == 'POST':
                session = self._session(required=True, mutate=True)
                result = store.add_package(session['user'], self._body(), query.get('visibility', ['private'])[0])
                return self._send(201, result)
        parts = path.strip('/').split('/')
        if len(parts) in (3, 4) and parts[:2] == ['api', 'packages']:
            ident = parts[2]
            mutating = method in ('POST', 'DELETE')
            session = self._session(required=mutating, mutate=mutating)
            user_id = (session['user'] or {}).get('id')
            if len(parts) == 4 and parts[3] == 'download' and method == 'GET':
                return self._send(200, store.download(ident, user_id, version_id=query.get('version', [None])[0]), 'application/zip', {'Content-Disposition': f'attachment; filename="{ident}.contextpack"'})
            if len(parts) == 3 and method == 'GET':
                return self._send(200, store.detail(ident, user_id, version_id=query.get('version', [None])[0]))
            if len(parts) == 3 and method == 'DELETE':
                store.delete(ident, user_id)
                return self._send(200, {'deleted': True})
            if len(parts) == 4:
                action = parts[3]
                if method == 'GET' and action == 'versions':
                    return self._send(200, {'versions': store.versions(ident, user_id)})
                if method == 'GET' and action == 'compare':
                    # Authorize each operand separately, even if IDs are guessed.
                    store.get(ident, user_id)
                    old_id = query.get('from', [''])[0]
                    new_id = query.get('to', [''])[0]
                    if not old_id or not new_id:
                        raise ValueError('请选择两个明确的版本')
                    before = read_package(store.download(ident, user_id, version_id=old_id))
                    after = read_package(store.download(ident, user_id, version_id=new_id))
                    return self._send(200, compare_packages(before, after))
                if method == 'POST' and action == 'fork':
                    store.get(ident, user_id)
                    data = self._json(4096)
                    version_id = data.get('version_id')
                    if not isinstance(version_id, str) or not version_id:
                        raise ValueError('请选择要 Fork 的固定版本')
                    return self._send(201, store.fork_package(ident, session['user'], version_id, data.get('visibility', 'private')))
                if method == 'POST' and action in ('versions', 'preview'):
                    current = store.get(ident, user_id)
                    if current['owner_id'] != user_id:
                        raise FileNotFoundError('对话库不存在或无权修改')
                    data = self._json()
                    base_id, message = data.get('base_version'), data.get('message')
                    if not isinstance(base_id, str) or not base_id:
                        raise ValueError('缺少编辑的基准版本，请重新打开对话库')
                    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 2000:
                        raise ValueError('保存说明需为 1–2,000 个字符')
                    if action == 'preview' and base_id != current['head_version_id']:
                        raise StoreConflict('对话库已有新版本，请保留草稿并重新检查最新版本')
                    base = read_package(store.download(ident, user_id, version_id=base_id))
                    blob = build_revision(base, data)
                    if action == 'preview':
                        candidate = read_package(blob)
                        return self._send(200, {'comparison': compare_packages(base, candidate), 'compatibility': candidate['compatibility']})
                    return self._send(201, store.add_version(ident, session['user'], blob, base_id, message.strip()))
        raise HTTPError(404, '未找到此接口')

    def _local(self, method, path, query):
        if self.server.mode != 'connector':
            raise HTTPError(404, 'Hub 不访问本机 Codex 会话，请启动本地连接器')
        if not hmac.compare_digest(self.headers.get('X-ContextHub-Token', ''), self.server.connector_token):
            raise HTTPError(403, '连接器令牌无效，请使用终端提供的完整链接')
        self._origin()
        from .codex import CodexClient, export_conversation, import_conversation, list_sessions
        if method == 'GET' and path == '/api/local/sessions':
            with self.server.codex_lock, CodexClient(command=self.server.codex_command) as client:
                sessions = list_sessions(client, include_archived=query.get('archived') == ['1'], include_subagents=query.get('subagents') == ['1'])
            return self._send(200, {'sessions': sessions})
        if method == 'POST' and path == '/api/local/preview':
            data = self._json(16384)
            ids = data.get('thread_ids')
            if not isinstance(ids, list) or not 1 <= len(ids) <= 200 or not all(isinstance(i, str) for i in ids):
                raise ValueError('请选择 1–200 段对话')
            with self.server.codex_lock, CodexClient(command=self.server.codex_command) as client:
                conversations = [export_conversation(client, ident) for ident in dict.fromkeys(ids)]
            warnings = [w for c in conversations for w in c.get('source', {}).get('warnings', [])]
            return self._send(200, {'conversations': conversations, 'warnings': warnings})
        if method == 'POST' and path == '/api/local/export':
            data = self._json()
            conversations = data.get('conversations', [])
            if data.get('redact', True):
                conversations, _ = redact_conversations(conversations)
            blob = build_package(conversations, data.get('title', 'Codex 对话库'), data.get('description', ''), source={'adapter': 'codex'})
            return self._send(200, blob, 'application/zip', {'Content-Disposition': 'attachment; filename="codex-conversations.contextpack"'})
        if method == 'POST' and path in ('/api/local/inspect', '/api/local/import'):
            package = read_package(self._body())
            if path.endswith('inspect'):
                return self._send(200, {k: package[k] for k in ('library', 'conversations', 'compatibility')})
            cwd = query.get('cwd', [''])[0]
            if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                raise ValueError('请选择存在的本地项目绝对路径')
            results = []
            with self.server.codex_lock, CodexClient(command=self.server.codex_command) as client:
                for conversation in package['conversations']:
                    try:
                        result = import_conversation(client, conversation, cwd)
                    except Exception as exc:
                        result = {'title': conversation['title'], 'verified': False, 'error': f'导入未完成：{type(exc).__name__}，请核查部分创建的会话'}
                    results.append(result)
            return self._send(200, {'results': results})
        raise HTTPError(404, '未找到本地连接器接口')
