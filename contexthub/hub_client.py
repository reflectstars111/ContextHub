"""Origin-scoped Hub sessions and a bounded, non-redirecting HTTP client."""

import contextlib
import ipaddress
import json
import math
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

from . import __version__
from .package import MAX_ARCHIVE_BYTES


DEFAULT_HUB = 'https://contexthub.reflectstars.dev'
MAX_CREDENTIAL_BYTES = 1024 * 1024
_TOKEN = re.compile(r'[A-Za-z0-9_-]{20,512}\Z')
_IS_WINDOWS = os.name == 'nt'


class HubClientError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def normalize_hub(value):
    """Require an HTTPS origin, or HTTP with a literal loopback IP address."""
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value) or '\\' in value:
        raise ValueError('Hub 地址必须是完整的 HTTPS 来源地址')
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if (parsed.scheme not in ('https', 'http') or not host or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or '?' in value or '#' in value
                or '%' in host or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if parsed.scheme == 'http' and (address is None or not address.is_loopback):
            raise ValueError
        if address is not None:
            host = f'[{address}]' if address.version == 6 else str(address)
        else:
            host = host.encode('idna').decode('ascii').lower()
            if (len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                       for label in host.split('.'))):
                raise ValueError
        suffix = '' if port is None or port == (443 if parsed.scheme == 'https' else 80) else f':{port}'
        return f'{parsed.scheme}://{host}{suffix}'
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Hub 仅接受 HTTPS 来源地址；本机 HTTP 必须使用 127.0.0.1 或 ::1 等 loopback IP') from exc


def _record(value):
    if not isinstance(value, dict) or value.get('cookie_name') not in ('ch_session', '__Host-ch_session'):
        raise ValueError('登录凭据格式无效，请重新登录')
    if any(not isinstance(value.get(key), str) or not _TOKEN.fullmatch(value[key]) for key in ('token', 'csrf_token')):
        raise ValueError('登录凭据格式无效，请重新登录')
    user = value.get('user')
    expiry = value.get('expires_at')
    if (not isinstance(user, dict) or any(not isinstance(user.get(key), str) or not 1 <= len(user[key]) <= 256
                                          for key in ('id', 'username'))
            or isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry)):
        raise ValueError('登录凭据格式无效，请重新登录')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', user['id']) or not re.fullmatch(r'[A-Za-z0-9_-]{3,32}', user['username']):
        raise ValueError('登录用户信息格式无效，请重新登录')
    return {key: value[key] for key in ('cookie_name', 'token', 'csrf_token', 'expires_at')} | {
        'user': {key: user[key] for key in ('id', 'username')}}


class Credentials:
    """Store session secrets outside workspaces, with private atomic updates."""
    def __init__(self, path=None):
        if path is None:
            config = os.environ.get('CONTEXTHUB_CONFIG_DIR')
            if _IS_WINDOWS:
                directory = Path(config) if config else Path(
                    os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'ContextHub'
                path = directory / 'credentials.dpapi'
            else:
                directory = Path(config) if config else Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'contexthub'
                path = directory / 'credentials.json'
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        self._windows_storage = None
        if _IS_WINDOWS:
            from .windows_credentials import WindowsCredentialStorage
            self._windows_storage = WindowsCredentialStorage(self.path)

    @staticmethod
    def _private(info, directory=False):
        expected = 0o700 if directory else 0o600
        if ((directory and not stat.S_ISDIR(info.st_mode)) or (not directory and not stat.S_ISREG(info.st_mode))
                or stat.S_IMODE(info.st_mode) != expected
                or (hasattr(os, 'getuid') and info.st_uid != os.getuid())
                or (not directory and info.st_nlink != 1)):
            raise ValueError(f'凭据路径必须由当前用户拥有，目录权限 700、文件权限 600；请检查 {expected:o} 权限')

    @contextlib.contextmanager
    def _locked(self, create=False):
        if self._windows_storage is not None:
            with self._windows_storage.locked(create=create) as directory:
                yield directory
            return
        directory = self.path.parent
        if create:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            info = directory.lstat()
        except FileNotFoundError:
            yield False
            return
        self._private(info, directory=True)
        try:
            import fcntl
        except ImportError as exc:
            raise ValueError('会话凭据目前需要 Linux/macOS 文件锁支持') from exc
        try:
            descriptor = os.open(directory / '.credentials.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise ValueError('无法安全打开登录凭据锁文件') from exc
        try:
            self._private(os.fstat(descriptor))
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield True
        finally:
            os.close(descriptor)

    def _load(self, directory=None):
        if self._windows_storage is not None:
            raw = self._windows_storage.read(directory)
            if raw is None:
                return {'version': 1, 'hubs': {}}
        else:
            try:
                descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                return {'version': 1, 'hubs': {}}
            except OSError as exc:
                raise ValueError('无法安全读取登录凭据；不允许符号链接') from exc
            with os.fdopen(descriptor, 'rb') as stream:
                self._private(os.fstat(stream.fileno()))
                raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
        try:
            if len(raw) > MAX_CREDENTIAL_BYTES:
                raise ValueError
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get('version') != 1
                    or not isinstance(value.get('hubs'), dict) or len(value['hubs']) > 128):
                raise ValueError
            for origin, record in value['hubs'].items():
                if origin != normalize_hub(origin):
                    raise ValueError
                _record(record)
            return value
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError('登录凭据文件无效，请检查配置或重新登录') from exc

    def _save(self, value, directory=None):
        if self._windows_storage is not None:
            raw = (json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8')
            self._windows_storage.write(directory, raw)
            return
        descriptor, name = tempfile.mkstemp(prefix='.credentials-', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream, ensure_ascii=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def get(self, origin):
        origin = normalize_hub(origin)
        with self._locked() as exists:
            value = self._load(exists)['hubs'].get(origin) if exists else None
            if value is None or value['expires_at'] <= time.time():
                return None
            return _record(value)

    def set(self, origin, record):
        origin, record = normalize_hub(origin), _record(record)
        if record['cookie_name'].startswith('__Host-') and not origin.startswith('https://'):
            raise ValueError('安全会话只能用于 HTTPS')
        with self._locked(create=True) as directory:
            value = self._load(directory)
            value['hubs'][origin] = record
            if len(value['hubs']) > 128:
                raise ValueError('保存的 Hub 登录记录过多，请先退出不再使用的 Hub')
            self._save(value, directory)

    def remove(self, origin):
        origin = normalize_hub(origin)
        with self._locked() as exists:
            if exists:
                value = self._load(exists)
                if value['hubs'].pop(origin, None) is not None:
                    self._save(value, exists)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None


class HubClient:
    def __init__(self, origin=DEFAULT_HUB, credentials=None, *, timeout=30, max_json_bytes=MAX_ARCHIVE_BYTES):
        self.origin = normalize_hub(origin)
        self.credentials = credentials if credentials is not None else Credentials()
        self.timeout = timeout
        self.max_json_bytes = max_json_bytes
        handlers = [_NoRedirect()]
        # A loopback session must not travel through an environment HTTP proxy.
        if self.origin.startswith('http://'):
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [('User-Agent', f'ContextHub-CLI/{__version__}')]

    def _login_required(self):
        return HubClientError(401, f'登录已过期或尚未登录，请运行 ctx auth login --hub {self.origin}')

    @staticmethod
    def _package_path(ident, suffix=''):
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ident):
            raise ValueError('对话库 ID 格式无效')
        return f'/api/packages/{ident}{suffix}'

    def _request(self, method, path, *, query=None, body=None, authenticated=False,
                 json_response=True, send_credentials=True, return_headers=False):
        if not path.startswith('/api/') or '?' in path or '#' in path or '..' in path:
            raise ValueError('请求必须使用 Hub API 路径')
        record = self.credentials.get(self.origin) if send_credentials else None
        if authenticated and record is None:
            raise self._login_required()
        headers = {'Accept': 'application/json' if json_response else 'application/zip'}
        secrets = [record['token'], record['csrf_token']] if record else []
        if isinstance(body, dict):
            if isinstance(body.get('password'), str) and body['password']:
                secrets.append(body['password'])
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        elif body is not None:
            headers['Content-Type'] = 'application/octet-stream'
        if method not in ('GET', 'HEAD'):
            headers['Origin'] = self.origin
        if record:
            headers['Cookie'] = record['cookie_name'] + '=' + record['token']
            if method not in ('GET', 'HEAD'):
                headers['X-CSRF-Token'] = record['csrf_token']
        url = self.origin + path
        if query:
            url += '?' + urllib.parse.urlencode({key: value for key, value in query.items() if value is not None})
        request = urllib.request.Request(url, body, headers, method=method)
        limit = self.max_json_bytes if json_response else MAX_ARCHIVE_BYTES
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(limit + 1)
                response_headers = response.headers
                if len(raw) > limit:
                    raise HubClientError(response.status, 'Hub 响应超过大小限制')
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and send_credentials:
                self.credentials.remove(self.origin)
                exc.close()
                raise self._login_required() from None
            message = f'Hub 请求失败（HTTP {exc.code}）'
            if 300 <= exc.code < 400:
                message = 'Hub 返回重定向，已拒绝转发登录凭据或对话内容；请直接使用正确的 Hub 地址'
            else:
                try:
                    payload = json.loads(exc.read(8192))
                    if isinstance(payload, dict) and isinstance(payload.get('error'), str):
                        message = payload['error']
                        for secret in secrets:
                            message = message.replace(secret, '[redacted]')
                        message = ''.join(char for char in message if char.isprintable() or char == '\n')[:500]
                except (ValueError, UnicodeError):
                    pass
            exc.close()
            raise HubClientError(exc.code, message) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HubClientError(0, f'无法连接 Hub {self.origin}，请检查地址和网络（{type(exc).__name__}）') from None
        if json_response:
            try:
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
            except (ValueError, UnicodeError) as exc:
                raise HubClientError(0, 'Hub 返回了无效的 JSON 响应') from exc
        else:
            result = raw
        return (result, response_headers) if return_headers else result

    def _status(self, user):
        return {'hub': self.origin, 'user': user, 'authenticated': user is not None}

    def login(self, username, password):
        data, headers = self._request('POST', '/api/login', body={'username': username, 'password': password},
                                      send_credentials=False, return_headers=True)
        try:
            cookies = SimpleCookie()
            values = headers.get_all('Set-Cookie', [])
            if len(values) != 1:
                raise ValueError
            cookies.load(values[0])
            choices = [cookies[name] for name in ('ch_session', '__Host-ch_session') if name in cookies]
            if len(choices) != 1:
                raise ValueError
            cookie = choices[0]
            if cookie['domain'] or cookie['path'] != '/' or (cookie.key == '__Host-ch_session' and not cookie['secure']):
                raise ValueError
            if cookie['secure'] and not self.origin.startswith('https://'):
                raise ValueError
            seconds = int(cookie['max-age'])
            if not 0 < seconds <= 604800:
                raise ValueError
            record = _record({'cookie_name': cookie.key, 'token': cookie.value, 'csrf_token': data.get('csrf_token'),
                              'user': data.get('user'), 'expires_at': time.time() + seconds})
        except (ValueError, TypeError, CookieError) as exc:
            raise HubClientError(0, 'Hub 未返回有效且仅属于本站的登录会话') from exc
        self.credentials.set(self.origin, record)
        return self._status(record['user'])

    def session(self):
        record = self.credentials.get(self.origin)
        if record is None:
            return self._status(None)
        data = self._request('GET', '/api/session')
        if data.get('user') is None:
            self.credentials.remove(self.origin)
            return self._status(None)
        try:
            updated = _record({**record, 'user': data['user'], 'csrf_token': data['csrf_token']})
        except (ValueError, KeyError) as exc:
            raise HubClientError(0, 'Hub 返回无效的会话状态') from exc
        self.credentials.set(self.origin, updated)
        return self._status(updated['user'])

    def logout(self):
        try:
            if self.credentials.get(self.origin) is not None:
                self._request('POST', '/api/logout', authenticated=True)
        except HubClientError as exc:
            if exc.status != 401:
                raise HubClientError(exc.status, '本机登录已清除，但远端会话撤销未确认：' + str(exc)) from None
        finally:
            self.credentials.remove(self.origin)
        return self._status(None)

    def config(self):
        return self._request('GET', '/api/config')

    def list_packages(self, q='', platform=None):
        return self._request('GET', '/api/packages', query={'q': q, 'platform': platform})

    def detail(self, ident, version=None):
        return self._request('GET', self._package_path(ident), query={'version': version})

    def versions(self, ident):
        return self._request('GET', self._package_path(ident, '/versions'))

    def download(self, ident, version=None):
        return self._request('GET', self._package_path(ident, '/download'), query={'version': version},
                             authenticated=True, json_response=False)

    def upload(self, blob, visibility='private', message=None, new_library_key=None):
        if new_library_key is not None:
            # Older Hubs silently ignore unknown query parameters and dedupe by
            # content, which could bind a new branch to its original repository.
            # Negotiate before sending any conversation bytes or creating a lib.
            try:
                configuration = self.config()
            except HubClientError as exc:
                if exc.status not in (404, 405):
                    raise
                configuration = {}
            capabilities = configuration.get('capabilities') if isinstance(configuration, dict) else None
            if not isinstance(capabilities, dict) or capabilities.get('new_library_key') is not True:
                raise HubClientError(None, 'Hub 未声明支持独立分支建库（new_library_key）；请先升级 Hub 至 0.7 或兼容版本。对话尚未上传。')
        return self._request('POST', '/api/packages', body=blob,
                             query={'visibility': visibility, 'message': message,
                                    'new_library_key': new_library_key}, authenticated=True)

    def push(self, ident, blob, base_version, message, expected_visibility=None):
        return self._request('POST', self._package_path(ident, '/push'), body=blob,
                             query={'base_version': base_version, 'message': message,
                                    'expected_visibility': expected_visibility}, authenticated=True)

    def fork(self, ident, version, visibility='private'):
        return self._request('POST', self._package_path(ident, '/fork'),
                             body={'version_id': version, 'visibility': visibility}, authenticated=True)
