"""Current-user DPAPI storage for the native Windows command-line client.

Plaintext credentials exist only in memory. Filesystem operations use anchored
directory handles, and DPAPI is never replaced by a plaintext fallback.
"""

import contextlib
import ctypes
import os
import stat
import uuid


MAX_PLAINTEXT_BYTES = 1024 * 1024
MAX_STORED_BYTES = MAX_PLAINTEXT_BYTES + 65536
MAGIC = b'ContextHub-DPAPI-v1\x00'
_ENTROPY = b'ContextHub current-user session credentials v1'
_CRYPTPROTECT_UI_FORBIDDEN = 1


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _windows_api():
    if os.name != 'nt':
        raise ValueError('DPAPI 登录凭据只能由原 Windows 用户在 Windows 上读取')
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    blob_pointer = ctypes.POINTER(_DataBlob)
    crypt32.CryptProtectData.argtypes = [blob_pointer, ctypes.c_wchar_p, blob_pointer,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                        blob_pointer]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [blob_pointer, ctypes.c_void_p, blob_pointer,
                                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                          blob_pointer]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _dpapi(data, *, decrypt=False):
    limit = MAX_STORED_BYTES if decrypt else MAX_PLAINTEXT_BYTES
    if not isinstance(data, bytes) or not data or len(data) > limit:
        raise ValueError('Windows 登录凭据大小无效')
    crypt32, kernel32 = _windows_api()
    input_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(_ENTROPY)
    source = _DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = _DataBlob(len(_ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _DataBlob()
    output_limit = MAX_PLAINTEXT_BYTES if decrypt else MAX_STORED_BYTES - len(MAGIC)
    try:
        operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        # In particular, do not set CRYPTPROTECT_LOCAL_MACHINE: credentials belong
        # to the current Windows account, not every account on this computer.
        success = operation(ctypes.byref(source), None, ctypes.byref(entropy), None,
                            None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result))
        if not success or not result.pbData or not 0 < result.cbData <= output_limit:
            action = '解密' if decrypt else '加密'
            raise ValueError(f'Windows DPAPI {action}失败；凭据未以明文保存，请使用原 Windows 账户或重新登录')
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.memset(input_buffer, 0, len(input_buffer))
        ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
        if result.pbData:
            if 0 < result.cbData <= output_limit:
                ctypes.memset(result.pbData, 0, result.cbData)
            kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def protect(data):
    return MAGIC + _dpapi(data)


def unprotect(data):
    if (not isinstance(data, bytes) or len(data) > MAX_STORED_BYTES
            or not data.startswith(MAGIC) or len(data) == len(MAGIC)):
        raise ValueError('Windows 登录凭据必须是当前用户的 DPAPI 加密文件，请重新登录')
    return _dpapi(data[len(MAGIC):], decrypt=True)


class WindowsCredentialStorage:
    """Encrypted, atomic storage; every operation requires a directory token."""

    def __init__(self, path):
        self.path = path

    @staticmethod
    def _regular(descriptor):
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Windows 登录凭据与锁文件必须是没有硬链接的常规文件')

    @contextlib.contextmanager
    def locked(self, create=False):
        from . import safe_fs

        directory = None
        descriptor = None
        try:
            try:
                directory = safe_fs.directory(self.path.parent, create=create)
            except FileNotFoundError:
                if create:
                    raise
                yield None
                return
            descriptor = safe_fs.open_file(directory, '.credentials.lock', os.O_RDWR | os.O_CREAT)
            self._regular(descriptor)
            try:
                safe_fs.lock(descriptor)
            except BlockingIOError as exc:
                raise ValueError('另一个 ctx 命令正在更新登录凭据，请稍后重试') from exc
            yield directory
        except OSError as exc:
            raise ValueError(f'无法安全访问 Windows 登录凭据：{exc}；请检查文件占用与路径权限，路径不能包含链接、junction 或重解析点') from exc
        finally:
            if descriptor is not None:
                safe_fs.close(descriptor)
            if directory is not None:
                safe_fs.close(directory)

    def read(self, directory):
        from . import safe_fs

        try:
            descriptor = safe_fs.open_file(directory, self.path.name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, 'rb') as stream:
            self._regular(stream.fileno())
            raw = stream.read(MAX_STORED_BYTES + 1)
        return unprotect(raw)

    def write(self, directory, plaintext):
        from . import safe_fs

        # Encryption must succeed before a temporary file is even opened.
        encrypted = protect(plaintext)
        temporary_name = f'.credentials-{uuid.uuid4().hex}.tmp'
        created = False
        try:
            descriptor = safe_fs.open_file(directory, temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            created = True
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            safe_fs.replace(directory, temporary_name, self.path.name)
            created = False
            safe_fs.sync_dir(directory)
        finally:
            if created:
                safe_fs.unlink(directory, temporary_name)
