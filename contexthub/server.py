"""HTTP hub and separately launched, token-protected local Codex connector."""

import hmac
import ipaddress
import json
import mimetypes
import math
import re
import secrets
import socket
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .package import build_package, read_package, redact_conversations
from .store import Store, StoreConflict, StoreCapacityError
from .revisions import build_revision, compare_packages
from .contributions import Contributions
from . import __version__

MAX_BODY = 101 * 1024 * 1024
STATIC = Path(__file__).parent / 'static'


def public_origin(value):
    """Use one configured HTTPS origin, never a client-supplied proxy header."""
    if value is None:
        return None
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value:
        raise ValueError('公网地址必须是独立的 HTTPS 来源，例如 https://contexthub.example.com')
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('公网地址端口无效') from exc
    hostname = parsed.hostname or ''
    labels = hostname.split('.')
    if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or not hostname or len(hostname) > 253
            or any(not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label) for label in labels)
            or port == 0 or parsed.netloc.endswith(':')):
        raise ValueError('公网地址必须是独立的 HTTPS 来源，例如 https://contexthub.example.com')
    authority = hostname.lower() + (f':{port}' if port not in (None, 443) else '')
    return f'https://{authority}'


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound accepted request workers; an idle client must eventually release one."""
    daemon_threads = True

    def __init__(self, address, handler, max_connections, request_timeout, request_deadline):
        self._request_slots = threading.BoundedSemaphore(max_connections)
        self.request_timeout = request_timeout
        self.request_deadline = request_deadline
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, address

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            try:
                request.settimeout(min(self.request_timeout, 1))
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n'
                                b'Connection: close\r\nRetry-After: 1\r\n\r\n')
                # Closing a socket with unread request bytes resets the TCP
                # connection on Windows, discarding the 503 for HTTP clients.
                # Half-close the response and drain a bounded amount/time;
                # rejected clients cannot occupy a worker or stall accept().
                request.shutdown(socket.SHUT_WR)
                deadline = time.monotonic() + min(self.request_timeout, 0.05)
                remaining = 64 * 1024
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break
                    request.settimeout(timeout)
                    data = request.recv(min(remaining, 4096))
                    if not data:
                        break
                    remaining -= len(data)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        expired = threading.Event()
        timer = threading.Timer(self.request_deadline, self._expire_request, (request, expired))
        timer.daemon = True
        try:
            timer.start()
            self.finish_request(request, client_address)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass
        except OSError:
            if not expired.is_set():
                self.handle_error(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            timer.cancel()
            if timer.ident is not None:
                timer.join()
            self.shutdown_request(request)
            self._request_slots.release()

    @staticmethod
    def _expire_request(request, expired):
        # A socket deadline interrupts reads/writes, including byte-at-a-time
        # uploads. It cannot interrupt work already running in Python or Git.
        expired.set()
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class HTTPError(Exception):
    def __init__(self, status, message, headers=None):
        self.status = status
        self.headers = headers
        super().__init__(message)


def create_server(data_dir, host='127.0.0.1', port=8765, mode='hub', token=None, codex_command=None,
                  public_url=None, max_connections=16, request_timeout=30, max_body_mib=101,
                  trust_cloudflare=False, max_storage_mib=0, min_free_mib=0, request_deadline=120):
    if mode not in ('hub', 'connector'):
        raise ValueError('invalid server mode')
    if mode == 'connector' and host not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('本地连接器只能绑定 loopback 地址')
    origin = public_origin(public_url)
    if origin and mode != 'hub':
        raise ValueError('本地连接器不能配置公网地址')
    if trust_cloudflare and (not origin or host not in ('127.0.0.1', 'localhost', '::1')):
        raise ValueError('信任 Cloudflare 仅可用于已配置公网 HTTPS 地址且绑定 loopback 的 Hub')
    if not isinstance(max_connections, int) or isinstance(max_connections, bool) or not 1 <= max_connections <= 128:
        raise ValueError('最大并发连接数必须在 1–128 之间')
    if not isinstance(request_timeout, (int, float)) or not math.isfinite(request_timeout) or request_timeout <= 0:
        raise ValueError('连接超时必须为有限的正数')
    if (not isinstance(request_deadline, (int, float)) or isinstance(request_deadline, bool)
            or not math.isfinite(request_deadline) or request_deadline <= 0):
        raise ValueError('请求连接总期限必须为有限的正数')
    if not isinstance(max_body_mib, int) or isinstance(max_body_mib, bool) or not 1 <= max_body_mib <= 101:
        raise ValueError('请求大小上限必须在 1–101 MiB 之间')
    for value in (max_storage_mib, min_free_mib):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError('存储预算与磁盘保留空间必须是非负 MiB 整数')
    server = BoundedHTTPServer((host, port), Handler, max_connections, request_timeout, request_deadline)
    server.mode = mode
    server.public_origin = origin
    server.public_host = urlsplit(origin).netloc if origin else None
    server.trust_cloudflare = trust_cloudflare
    server.max_body_bytes = max_body_mib * 1024 * 1024
    server.store = Store(data_dir, max_storage_bytes=max_storage_mib * 1024 * 1024 or None,
                         min_free_bytes=min_free_mib * 1024 * 1024) if mode == 'hub' else None
    server.contributions = Contributions(server.store) if server.store else None
    server.connector_token = token or secrets.token_urlsafe(32)
    server.codex_command = codex_command
    server.codex_lock = threading.Lock()
    server.auth_attempts = {}
    server.auth_lock = threading.Lock()
    server.auth_pruned_at = time.monotonic()
    server.write_attempts = {}
    server.write_lock = threading.Lock()
    server.write_pruned_at = time.monotonic()
    return server


class Handler(BaseHTTPRequestHandler):
    server_version = f'ContextHub/{__version__}'

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
        if self.server.public_origin:
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _body(self, limit=MAX_BODY):
        limit = min(limit, self.server.max_body_bytes)
        if self.headers.get('Transfer-Encoding'):
            raise HTTPError(400, '不支持 chunked 请求，请提供 Content-Length')
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError as exc:
            raise HTTPError(400, '无效的 Content-Length') from exc
        if size < 0 or size > limit:
            raise HTTPError(413, '文件超过大小限制，请分批传输')
        self.connection.settimeout(self.server.request_timeout)
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
        origins = self.headers.get_all('Origin', [])
        if len(origins) > 1:
            raise HTTPError(403, '拒绝重复的来源标头')
        origin = origins[0] if origins else None
        if self.server.public_origin:
            if origin is not None and origin != self.server.public_origin:
                raise HTTPError(403, '拒绝跨来源请求')
        elif origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ('http', 'https') or parsed.netloc != self.headers.get('Host'):
                raise HTTPError(403, '拒绝跨来源请求')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise HTTPError(403, '拒绝跨站请求')

    def _request_headers(self):
        # Even though the origin closes each HTTP/1.0 response, reject ambiguous
        # framing before a proxy and the application can interpret it differently.
        hosts = self.headers.get_all('Host', [])
        sizes = self.headers.get_all('Content-Length', [])
        if len(hosts) > 1 or len(sizes) > 1 or self.headers.get_all('Transfer-Encoding', []):
            raise HTTPError(400, '拒绝重复或含糊的请求标头')
        if sizes and not re.fullmatch(r'[0-9]+', sizes[0]):
            raise HTTPError(400, '无效的 Content-Length')
        if sizes and (len(sizes[0]) > 12 or int(sizes[0]) > self.server.max_body_bytes):
            raise HTTPError(413, '文件超过大小限制，请分批传输')
        if self.server.public_origin:
            if len(hosts) != 1 or hosts[0].lower() != self.server.public_host:
                raise HTTPError(421, '请求域名与配置的公网地址不符')
            if not self.path.startswith('/') or urlsplit(self.path).netloc:
                raise HTTPError(400, '请求地址必须使用本站路径')
            origins = self.headers.get_all('Origin', [])
            if len(origins) > 1 or (origins and origins[0] != self.server.public_origin):
                raise HTTPError(403, '拒绝跨来源请求')
        if self.server.trust_cloudflare:
            self._client_ip()
            schemes = self.headers.get_all('X-Forwarded-Proto', [])
            if len(schemes) != 1 or schemes[0] not in ('http', 'https'):
                raise HTTPError(403, '缺少可信的 Cloudflare HTTPS 标头')
            if schemes[0] == 'http':
                if self.command in ('GET', 'HEAD'):
                    raise HTTPError(308, '请通过 HTTPS 访问', {'Location': self.server.public_origin + self.path})
                raise HTTPError(426, '此服务仅支持 HTTPS，请使用配置的公网地址')

    def _client_ip(self):
        if not self.server.trust_cloudflare:
            return self.client_address[0]
        values = self.headers.get_all('CF-Connecting-IP', [])
        try:
            if (not ipaddress.ip_address(self.client_address[0]).is_loopback
                    or len(values) != 1 or '%' in values[0]):
                raise ValueError('untrusted proxy')
            return str(ipaddress.ip_address(values[0]))
        except ValueError as exc:
            raise HTTPError(403, '缺少可信的 Cloudflare 访客地址') from exc

    def _session_cookie(self, token, max_age):
        name = '__Host-ch_session' if self.server.public_origin else 'ch_session'
        secure = '; Secure' if self.server.public_origin else ''
        return f'{name}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}'

    def _token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return ''
        name = '__Host-ch_session' if self.server.public_origin else 'ch_session'
        return cookie[name].value if name in cookie else ''

    def _session(self, required=False, mutate=False):
        session = self.server.store.session(self._token())
        if required and not session['user']:
            raise HTTPError(401, '请先登录')
        if mutate:
            self._origin()
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf_token'] or '') or not session['csrf_token']:
                raise HTTPError(403, '会话校验失败，请刷新后重试')
            if self.server.public_origin and self.server.mode == 'hub':
                self._write_rate_limit(session['user']['id'])
        return session

    def _write_rate_limit(self, user_id):
        """Limit costly authenticated work independently of a visitor's IP."""
        now = time.monotonic()
        with self.server.write_lock:
            if now - self.server.write_pruned_at >= 60 or len(self.server.write_attempts) >= 4096:
                self.server.write_attempts = {ident: items for ident, items in self.server.write_attempts.items()
                                              if items and now - items[-1] < 60}
                self.server.write_pruned_at = now
            if user_id not in self.server.write_attempts and len(self.server.write_attempts) >= 4096:
                raise HTTPError(429, '写入请求繁忙，请稍后重试', {'Retry-After': '60'})
            attempts = [t for t in self.server.write_attempts.get(user_id, []) if now - t < 60]
            if len(attempts) >= 60:
                retry = max(1, math.ceil(60 - (now - attempts[0])))
                raise HTTPError(429, '写入操作过于频繁，请稍后重试', {'Retry-After': str(retry)})
            attempts.append(now)
            self.server.write_attempts[user_id] = attempts

    def _auth_rate_limit(self):
        now = time.monotonic()
        address = self._client_ip()
        with self.server.auth_lock:
            if now - self.server.auth_pruned_at >= 60 or len(self.server.auth_attempts) >= 4096:
                self.server.auth_attempts = {ip: items for ip, items in self.server.auth_attempts.items()
                                             if items and now - items[-1] < 60}
                self.server.auth_pruned_at = now
            if address not in self.server.auth_attempts and len(self.server.auth_attempts) >= 4096:
                raise HTTPError(429, '登录请求繁忙，请稍后重试')
            attempts = [t for t in self.server.auth_attempts.get(address, []) if now - t < 60]
            if len(attempts) >= 20:
                raise HTTPError(429, '登录尝试过多，请稍后重试')
            attempts.append(now)
            self.server.auth_attempts[address] = attempts

    def do_GET(self):
        self._handle('GET')

    def do_HEAD(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_DELETE(self):
        self._handle('DELETE')

    def _handle(self, method):
        try:
            self._request_headers()
            path = unquote(urlsplit(self.path).path)
            query = parse_qs(urlsplit(self.path).query)
            if path == '/api/config' and method == 'GET':
                return self._send(200, {'mode': self.server.mode, 'version': __version__,
                                        'max_package_bytes': self.server.max_body_bytes,
                                        'capabilities': ({'new_library_key': True, 'expected_visibility': True}
                                                         if self.server.mode == 'hub' else {})})
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
            self._send(exc.status, {'error': str(exc)}, headers=exc.headers)
        except FileNotFoundError as exc:
            self._send(404, {'error': str(exc)})
        except PermissionError as exc:
            self._send(403, {'error': str(exc)})
        except StoreConflict as exc:
            self._send(409, {'error': str(exc)})
        except StoreCapacityError as exc:
            self._send(507, {'error': str(exc)})
        except (ValueError, TypeError, KeyError) as exc:
            self._send(422, {'error': str(exc)[:500]})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except TimeoutError:
            self._send(504, {'error': '操作超时，请稍后重试'})
        except Exception as exc:
            # Never echo upstream provider credentials or raw process output.
            self._send(500, {'error': f'操作未完成（{type(exc).__name__}），请查看终端诊断或稍后重试'})

    def _static(self, path):
        if path == '/install-ctx.ps1':
            return self._send(200, (STATIC / 'install-ctx.ps1').read_bytes(),
                              'text/plain; charset=utf-8',
                              {'Content-Disposition': 'attachment; filename="install-ctx.ps1"'})
        if path in ('/ctx.pyz', '/ctx.pyz.sha256'):
            from .distribution import cli_archive, cli_checksum
            if path.endswith('.sha256'):
                return self._send(200, cli_checksum().encode(), 'text/plain; charset=utf-8')
            return self._send(200, cli_archive(), 'application/octet-stream',
                              {'Content-Disposition': 'attachment; filename="ctx.pyz"'})
        allowed = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js', '/versions.js': 'versions.js', '/contributions.js': 'contributions.js', '/drafts.js': 'drafts.js', '/lifecycle.js': 'lifecycle.js', '/style.css': 'style.css', '/cli': 'cli.html', '/cli.css': 'cli.css'}
        allowed.update({'/repository.css': 'repository.css', '/repository.js': 'repository.js'})
        if path not in allowed:
            raise HTTPError(404, '未找到页面')
        file = STATIC / allowed[path]
        return self._send(200, file.read_bytes(), (mimetypes.guess_type(str(file))[0] or 'text/plain') + '; charset=utf-8')

    def _import_upload(self, query):
        from .importers import import_upload
        options = {}
        for key, default in (('filename', ''), ('platform', 'auto'), ('mode', 'normalize')):
            values = query.get(key, [default])
            if len(values) != 1:
                raise ValueError(f'导入参数 {key} 不能重复')
            options[key] = values[0]
        if len(options['filename']) > 512:
            raise ValueError('文件名过长')
        return import_upload(self._body(), **options)

    def _hub(self, method, path, query):
        store = self.server.store
        if path.startswith('/api/contributions/'):
            return self._contributions(method, path, query)
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
            return self._send(status, result, headers={'Set-Cookie': self._session_cookie(token, 604800)})
        if path == '/api/logout' and method == 'POST':
            self._session(required=True, mutate=True)
            store.logout(self._token())
            return self._send(200, {'user': None, 'csrf_token': None}, headers={'Set-Cookie': self._session_cookie('', 0)})
        if path == '/api/inspect' and method == 'POST':
            self._session(required=True, mutate=True)
            _, package, report = self._import_upload(query)
            return self._send(200, {**{k: package[k] for k in ('library', 'conversations', 'compatibility')},
                                    'platforms': report['platforms'], 'import_report': report})
        if path == '/api/packages':
            if method == 'GET':
                session = self._session()
                from .platforms import PLATFORMS
                platforms = query.get('platform', [None])
                if len(platforms) != 1 or (platforms[0] is not None and platforms[0] not in PLATFORMS):
                    raise ValueError('请选择有效的平台分类')
                return self._send(200, {'packages': store.list_packages((session['user'] or {}).get('id'),
                                    query.get('q', [''])[0], platforms[0])})
            if method == 'POST':
                session = self._session(required=True, mutate=True)
                blob, _, report = self._import_upload(query)
                if report['requires_confirmation'] and query.get('confirm_import') != ['1']:
                    raise ValueError('请先预览并确认导入提示；接受遗漏或原文存档后再上传（confirm_import=1）')
                messages = query.get('message', [None])
                if len(messages) != 1:
                    raise ValueError('message 参数不能重复')
                creation_keys = query.get('new_library_key', [None])
                if len(creation_keys) != 1:
                    raise ValueError('new_library_key 参数不能重复')
                result = store.add_package(session['user'], blob, query.get('visibility', ['private'])[0],
                                           messages[0], creation_keys[0])
                return self._send(201, {**result, 'import_report': report})
        parts = path.strip('/').split('/')
        if len(parts) in (3, 4) and parts[:2] == ['api', 'packages']:
            ident = parts[2]
            mutating = method in ('POST', 'DELETE')
            downloading = method == 'GET' and len(parts) == 4 and parts[3] == 'download'
            private_draft = len(parts) == 4 and parts[3] == 'draft'
            session = self._session(required=mutating or downloading or private_draft, mutate=mutating)
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
                if action == 'draft':
                    if method == 'GET':
                        return self._send(200, store.get_draft(ident, user_id))
                    if method == 'POST':
                        return self._send(200, store.save_draft(ident, user_id, self._json()))
                    if method == 'DELETE':
                        return self._send(200, store.delete_draft(ident, user_id, self._json(4096).get('expected_revision')))
                if method == 'POST' and action == 'visibility':
                    data = self._json(4096)
                    return self._send(200, store.set_visibility(ident, user_id, data.get('visibility'), data.get('base_version')))
                if method == 'POST' and action == 'purge':
                    return self._send(200, store.purge(ident, user_id, self._json(4096).get('confirmation')))
                if action == 'contributions':
                    if method == 'GET':
                        return self._send(200, {'contributions': self.server.contributions.list(ident, user_id)})
                    if method == 'POST':
                        data = self._json(65536)
                        return self._send(201, self.server.contributions.create(
                            ident, session['user'], data.get('source_version_id'),
                            data.get('title'), data.get('description', '')))
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
                if method == 'POST' and action == 'push':
                    current = store.get(ident, user_id)
                    if current['owner_id'] != user_id:
                        raise FileNotFoundError('对话库不存在或无权修改')
                    for key in ('base_version', 'message'):
                        if len(query.get(key, [])) != 1 or not query[key][0].strip():
                            raise ValueError(f'需要唯一且非空的 {key} 参数')
                    base_id, message = query['base_version'][0], query['message'][0].strip()
                    visibilities = query.get('expected_visibility', [None])
                    if len(visibilities) != 1:
                        raise ValueError('expected_visibility 参数不能重复')
                    if len(message) > 2000:
                        raise ValueError('保存说明需为 1–2,000 个字符')
                    base = read_package(store.download(ident, user_id, version_id=base_id))
                    submitted = self._body()
                    candidate = read_package(submitted)
                    canonical = build_revision(base, {
                        'title': candidate['library']['title'],
                        'description': candidate['library']['description'],
                        'code_checkpoint': candidate['library'].get('code_checkpoint'),
                        'conversations': candidate['conversations']})
                    checked = read_package(canonical)
                    fields = ('library', 'conversations', 'compatibility')
                    # ZIP compression/entry order can vary across machines.
                    # Validate every semantic field, then retain the original
                    # validated bytes so client object hashes remain portable.
                    if any(checked[field] != candidate[field] for field in fields):
                        raise ValueError('本地快照的编辑记录与基准版本不一致，请使用最新 ctx 工具重新提交')
                    if all(base[field] == candidate[field] for field in fields):
                        raise ValueError('内容没有变化，无需创建新版本')
                    return self._send(201, store.add_version(ident, session['user'], submitted, base_id, message,
                                                           expected_visibility=visibilities[0]))
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
                    return self._send(201, store.add_version(ident, session['user'], blob, base_id, message.strip(),
                                                           data.get('draft_revision'), data.get('expected_visibility')))
        raise HTTPError(404, '未找到此接口')

    def _contributions(self, method, path, query):
        parts = path.strip('/').split('/')
        if len(parts) not in (3, 4) or parts[:2] != ['api', 'contributions']:
            raise HTTPError(404, '未找到贡献请求接口')
        mutating = method == 'POST'
        downloading = method == 'GET' and len(parts) == 4 and parts[3] == 'candidate'
        session = self._session(required=mutating or downloading, mutate=mutating)
        user = session['user']
        user_id = (user or {}).get('id')
        service = self.server.contributions
        ident = parts[2]
        if len(parts) == 3 and method == 'GET':
            return self._send(200, service.detail(ident, user_id))
        if len(parts) == 4:
            action = parts[3]
            if method == 'GET' and action == 'candidate':
                blob = service.candidate(ident, user_id, query.get('check_id', [None])[0])
                return self._send(200, blob, 'application/zip', {'Content-Disposition': 'attachment; filename="merge-candidate.contextpack"'})
            if method == 'POST':
                data = self._json(65536)
                if action == 'comments':
                    result = service.comment(ident, user, data.get('body'))
                elif action == 'status':
                    result = service.set_status(ident, user, data.get('status'))
                elif action == 'source':
                    result = service.update_source(ident, user, data.get('source_version_id'))
                elif action == 'check':
                    result = service.check(ident, user, data.get('expected_target_version_id'))
                elif action == 'merge':
                    result = service.merge(ident, user, data.get('check_id'), data.get('candidate_oid'))
                else:
                    raise HTTPError(404, '未找到贡献请求接口')
                return self._send(200, result)
        raise HTTPError(404, '未找到贡献请求接口')

    def _local(self, method, path, query):
        if self.server.mode != 'connector':
            raise HTTPError(404, 'Hub 不访问本机 Codex 会话，请启动本地连接器')
        if not hmac.compare_digest(self.headers.get('X-ContextHub-Token', ''), self.server.connector_token):
            raise HTTPError(403, '连接器令牌无效，请使用终端提供的完整链接')
        self._origin()
        from .codex import CodexClient, TimelineError, export_conversation, import_conversation, list_sessions
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
            if any(c['source'].get('kind') == 'source_archive' for c in package['conversations']):
                raise ValueError('原文存档尚未解析为对话，不能作为会话恢复；请先将原文件转换为角色消息')
            cwd = query.get('cwd', [''])[0]
            if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                raise ValueError('请选择存在的本地项目绝对路径')
            modes = query.get('mode', ['timeline'])
            if len(modes) != 1 or modes[0] not in ('timeline', 'context'):
                raise ValueError('导入模式必须为 timeline 或 context')
            results = []
            with self.server.codex_lock, CodexClient(command=self.server.codex_command) as client:
                for conversation in package['conversations']:
                    try:
                        result = import_conversation(client, conversation, cwd, mode=modes[0])
                    except TimelineError as exc:
                        result = {'title': conversation['title'], 'verified': False,
                                  'error': str(exc)[:500], 'mode': 'failed'}
                    except Exception as exc:
                        result = {'title': conversation['title'], 'verified': False, 'error': f'导入未完成：{type(exc).__name__}，请核查部分创建的会话'}
                    results.append(result)
            return self._send(200, {'results': results})
        raise HTTPError(404, '未找到本地连接器接口')
