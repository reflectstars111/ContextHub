"""Small anchored filesystem adapter for local ContextHub client state.

POSIX uses dir_fd + O_NOFOLLOW. Windows holds every ancestor directory open
without FILE_SHARE_WRITE or FILE_SHARE_DELETE, preventing directory/junction replacement while an
operation is in progress; each entry is opened with OPEN_REPARSE_POINT and
checked by handle before use. Windows supports local drive paths only.
Directory tokens must outlive their child tokens. File descriptors returned by
open_file are ordinary Python/CRT descriptors and must be closed before replace.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
import stat
from types import SimpleNamespace


class UnsafePathError(OSError):
    """A path cannot be used without following links or special files."""


def validate_windows_path(path):
    """Validate a fully qualified ordinary drive path (also testable on POSIX)."""
    path = os.fspath(path)
    if not isinstance(path, str) or '\x00' in path:
        raise UnsafePathError('需要普通本地磁盘路径')
    path = path.replace('/', '\\')
    drive, tail = ntpath.splitdrive(path)
    if (len(drive) != 2 or drive[1] != ':' or not drive[0].isascii()
            or not drive[0].isalpha() or not tail.startswith('\\')):
        raise UnsafePathError('Windows 工作目录仅支持本地盘符路径，不支持 UNC、设备或相对盘符路径')
    for component in tail.split('\\'):
        if component:
            _windows_component(component)
    return drive.upper() + tail


def _windows_component(name):
    stem = name.split('.')[0].upper()
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$'}
    reserved.update(prefix + number for prefix in ('COM', 'LPT')
                    for number in '123456789¹²³')
    if (not name or name in ('.', '..') or name.endswith((' ', '.'))
            or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in name)
            or stem in reserved):
        raise UnsafePathError('Windows 路径包含保留名称、设备名或不安全字符')


def _name(name):
    if not isinstance(name, str) or not name or name in ('.', '..') or any(c in name for c in '/\\\x00'):
        raise UnsafePathError('文件名称必须是单个普通路径组件')
    if os.name == 'nt':
        _windows_component(name)
    return name


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _HANDLE = wintypes.HANDLE
    _DWORD = wintypes.DWORD
    _INVALID = ctypes.c_void_p(-1).value
    _READ = 0x80000000
    _WRITE = 0x40000000
    _ATTRIBUTES = 0x80
    _DELETE = 0x10000
    _SHARE_READ = 1
    _SHARE_WRITE = 2
    _OPEN_EXISTING = 3
    _CREATE_NEW = 1
    _OPEN_ALWAYS = 4
    _DIRECTORY_ATTRIBUTE = 0x10
    _REPARSE_ATTRIBUTE = 0x400
    _OPEN_REPARSE_POINT = 0x00200000
    _BACKUP_SEMANTICS = 0x02000000
    _NORMAL_ATTRIBUTE = 0x80

    class _FileInfo(ctypes.Structure):
        _fields_ = [('attributes', _DWORD), ('creation', wintypes.FILETIME),
                    ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                    ('volume', _DWORD), ('size_high', _DWORD), ('size_low', _DWORD),
                    ('links', _DWORD), ('index_high', _DWORD), ('index_low', _DWORD)]

    class _Overlapped(ctypes.Structure):
        _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                    ('offset', _DWORD), ('offset_high', _DWORD), ('event', _HANDLE)]

    class _RenameInfo(ctypes.Structure):
        _fields_ = [('replace', _DWORD), ('root', _HANDLE),
                    ('length', _DWORD), ('name', wintypes.WCHAR * 1)]

    class _IOStatus(ctypes.Structure):
        _fields_ = [('status', ctypes.c_void_p), ('information', ctypes.c_size_t)]

    def _api(name, result, *arguments):
        function = getattr(_kernel, name)
        function.restype = result
        function.argtypes = list(arguments)
        return function

    _create_file = _api('CreateFileW', _HANDLE, wintypes.LPCWSTR, _DWORD, _DWORD,
                        wintypes.LPVOID, _DWORD, _DWORD, _HANDLE)
    _close_handle = _api('CloseHandle', wintypes.BOOL, _HANDLE)
    _file_info = _api('GetFileInformationByHandle', wintypes.BOOL, _HANDLE, ctypes.POINTER(_FileInfo))
    _create_directory = _api('CreateDirectoryW', wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPVOID)
    _remove_directory = _api('RemoveDirectoryW', wintypes.BOOL, wintypes.LPCWSTR)
    _delete_file = _api('DeleteFileW', wintypes.BOOL, wintypes.LPCWSTR)
    _lock_file = _api('LockFileEx', wintypes.BOOL, _HANDLE, _DWORD, _DWORD, _DWORD,
                      _DWORD, ctypes.POINTER(_Overlapped))
    _drive_type = _api('GetDriveTypeW', wintypes.UINT, wintypes.LPCWSTR)

    _ntdll = ctypes.WinDLL('ntdll', use_last_error=True)
    _rename_file = _ntdll.NtSetInformationFile
    _rename_file.restype = wintypes.LONG
    _rename_file.argtypes = [_HANDLE, ctypes.POINTER(_IOStatus), wintypes.LPVOID,
                            _DWORD, ctypes.c_int]
    _dos_error = _ntdll.RtlNtStatusToDosError
    _dos_error.restype = _DWORD
    _dos_error.argtypes = [wintypes.LONG]

    def _error(path=None):
        number = ctypes.get_last_error()
        if number in (2, 3):
            return FileNotFoundError(2, ctypes.FormatError(number), path)
        if number in (80, 183):
            return FileExistsError(17, ctypes.FormatError(number), path)
        if number in (32, 33):
            return PermissionError(13, ctypes.FormatError(number), path)
        return ctypes.WinError(number)

    def _info(handle):
        information = _FileInfo()
        if not _file_info(handle, ctypes.byref(information)):
            raise _error()
        if information.attributes & _REPARSE_ATTRIBUTE:
            raise UnsafePathError('拒绝 Windows reparse point、junction 或符号链接')
        return information

    def _open_handle(path, access, sharing, disposition, *, directory=False):
        # BACKUP_SEMANTICS lets us inspect a directory and reject it as a file.
        handle = _create_file(path, access, sharing, None, disposition,
                              _OPEN_REPARSE_POINT | _BACKUP_SEMANTICS, None)
        if handle == _INVALID:
            raise _error(path)
        try:
            information = _info(handle)
            is_directory = bool(information.attributes & _DIRECTORY_ATTRIBUTE)
            if is_directory != directory:
                raise UnsafePathError('路径类型错误，需要目录' if directory else '拒绝特殊文件或目录')
            if not directory and information.links != 1:
                raise UnsafePathError('拒绝硬链接文件')
            return handle, information
        except BaseException:
            _close_handle(handle)
            raise

    class _Directory:
        def __init__(self, path, handles, parent=None):
            self.path = path
            self.handles = handles
            self.parent = parent

        def entry(self, name):
            if not self.handles:
                raise OSError('目录句柄已关闭')
            return ntpath.join(self.path, _name(name))

        def close(self):
            for handle in reversed(self.handles):
                _close_handle(handle)
            self.handles = []

    def directory(path, *, create=False):
        # Validate the supplied absolute spelling before abspath can normalize
        # away an unsafe trailing-dot or device component.
        original = os.fspath(path)
        if isinstance(original, str) and (original.startswith(('\\\\', '//'))
                or ntpath.splitdrive(original)[0]):
            validate_windows_path(original)
        absolute = validate_windows_path(os.path.abspath(original))
        drive, tail = ntpath.splitdrive(absolute)
        drive_root = drive + '\\'
        if _drive_type(drive_root) not in (2, 3):
            raise UnsafePathError('Windows 工作区仅支持本地磁盘，不支持网络映射盘')
        handles = []
        current = drive_root
        try:
            # Request data/list access, not just attributes: attribute-only
            # handles do not participate in the Windows sharing reservation.
            handle, _ = _open_handle(current, _READ, _SHARE_READ,
                                     _OPEN_EXISTING, directory=True)
            handles.append(handle)
            for component in tail.strip('\\').split('\\') if tail.strip('\\') else []:
                current = ntpath.join(current, component)
                try:
                    handle, _ = _open_handle(current, _READ, _SHARE_READ,
                                             _OPEN_EXISTING, directory=True)
                except FileNotFoundError:
                    if not create:
                        raise
                    if not _create_directory(current, None):
                        error = _error(current)
                        if not isinstance(error, FileExistsError):
                            raise error
                    handle, _ = _open_handle(current, _READ, _SHARE_READ,
                                             _OPEN_EXISTING, directory=True)
                handles.append(handle)
            return _Directory(absolute, handles)
        except BaseException:
            for handle in reversed(handles):
                _close_handle(handle)
            raise

    def child_dir(parent, name):
        path = parent.entry(name)
        handle, _ = _open_handle(path, _READ, _SHARE_READ,
                                 _OPEN_EXISTING, directory=True)
        return _Directory(path, [handle], parent)

    def stat_entry(parent, name):
        path = parent.entry(name)
        handle = _create_file(path, _ATTRIBUTES, _SHARE_READ | _SHARE_WRITE,
                              None, _OPEN_EXISTING, _OPEN_REPARSE_POINT | _BACKUP_SEMANTICS, None)
        if handle == _INVALID:
            raise _error(path)
        try:
            information = _info(handle)
            mode = stat.S_IFDIR if information.attributes & _DIRECTORY_ATTRIBUTE else stat.S_IFREG
            return SimpleNamespace(st_mode=mode, st_nlink=information.links,
                                   st_size=(information.size_high << 32) | information.size_low)
        finally:
            _close_handle(handle)

    def open_file(parent, name, flags, mode=0o600):
        path = parent.entry(name)
        access_mode = flags & (os.O_WRONLY | os.O_RDWR)
        access = _READ if access_mode == os.O_RDONLY else (_WRITE if access_mode == os.O_WRONLY else _READ | _WRITE)
        creation = _CREATE_NEW if flags & os.O_EXCL else (_OPEN_ALWAYS if flags & os.O_CREAT else _OPEN_EXISTING)
        sharing = _SHARE_READ if access_mode == os.O_RDONLY else _SHARE_READ | _SHARE_WRITE
        handle, _ = _open_handle(path, access, sharing, creation)
        try:
            # Creation never truncates: inspect the handle/link count first.
            descriptor = msvcrt.open_osfhandle(handle, access_mode | os.O_BINARY)
        except BaseException:
            _close_handle(handle)
            raise
        try:
            if flags & os.O_TRUNC:
                os.ftruncate(descriptor, 0)
            if flags & os.O_APPEND:
                os.lseek(descriptor, 0, os.SEEK_END)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def mkdir(parent, name, mode=0o700):
        path = parent.entry(name)
        if not _create_directory(path, None):
            raise _error(path)

    def rmdir(parent, name):
        path = parent.entry(name)
        information = stat_entry(parent, name)
        if not stat.S_ISDIR(information.st_mode):
            raise UnsafePathError('仅可移除普通目录')
        if not _remove_directory(path):
            raise _error(path)

    def unlink(parent, name):
        path = parent.entry(name)
        information = stat_entry(parent, name)
        if not stat.S_ISREG(information.st_mode) or information.st_nlink != 1:
            raise UnsafePathError('仅可移除独立常规文件')
        if not _delete_file(path):
            raise _error(path)

    def replace(parent, source, target):
        source_path = parent.entry(source)
        target_path = parent.entry(target)
        for name in (source, target):
            try:
                information = stat_entry(parent, name)
            except FileNotFoundError:
                if name == target:
                    continue
                raise
            if not stat.S_ISREG(information.st_mode) or information.st_nlink != 1:
                raise UnsafePathError('原子替换只允许独立常规文件')
        # A full-path MoveFileEx/Win32 rename reopens the parent for writing,
        # conflicting with our directory guards. Native FileRenameInformation
        # with a simple name and NULL RootDirectory renames in the source's
        # existing directory without reopening it. Keep every ancestor guarded.
        # https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information
        handle, _ = _open_handle(source_path, _DELETE, _SHARE_READ | _SHARE_WRITE,
                                 _OPEN_EXISTING)
        try:
            encoded = _name(target).encode('utf-16-le')
            buffer = ctypes.create_string_buffer(ctypes.sizeof(_RenameInfo) + len(encoded))
            information = _RenameInfo.from_buffer(buffer)
            information.replace = 1
            information.root = None
            information.length = len(encoded)
            ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.name.offset, encoded, len(encoded))
            status = _IOStatus()
            result = _rename_file(handle, ctypes.byref(status), buffer, len(buffer), 10)
            if result < 0:
                ctypes.set_last_error(_dos_error(result))
                raise _error(target_path)
        finally:
            _close_handle(handle)

    def rename_dir(parent, source, target):
        """Publish a sibling directory atomically, never replacing a target."""
        source_path = parent.entry(source)
        target_path = parent.entry(target)
        handle, _ = _open_handle(source_path, _DELETE, _SHARE_READ | _SHARE_WRITE,
                                 _OPEN_EXISTING, directory=True)
        try:
            encoded = _name(target).encode('utf-16-le')
            buffer = ctypes.create_string_buffer(ctypes.sizeof(_RenameInfo) + len(encoded))
            information = _RenameInfo.from_buffer(buffer)
            information.replace = 0
            information.root = None
            information.length = len(encoded)
            ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.name.offset, encoded, len(encoded))
            status = _IOStatus()
            result = _rename_file(handle, ctypes.byref(status), buffer, len(buffer), 10)
            if result < 0:
                ctypes.set_last_error(_dos_error(result))
                raise _error(target_path)
        finally:
            _close_handle(handle)

    def sync_dir(parent):
        # Windows has no supported directory fsync equivalent. All payloads
        # are FlushFileBuffers'd via os.fsync before an atomic rename.
        pass

    def lock(descriptor, *, blocking=False):
        overlapped = _Overlapped()
        flags = 0x2 | (0 if blocking else 0x1)
        if not _lock_file(msvcrt.get_osfhandle(descriptor), flags, 0, 1, 0, ctypes.byref(overlapped)):
            number = ctypes.get_last_error()
            if number in (32, 33, 158):
                raise BlockingIOError(11, '文件锁正在使用')
            raise _error()

    def close(descriptor):
        if isinstance(descriptor, _Directory):
            descriptor.close()
        else:
            os.close(descriptor)

else:
    def directory(path, *, create=False):
        absolute = os.path.abspath(os.fspath(path))
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in Path(absolute).parts[1:]:
                try:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def child_dir(parent, name):
        return os.open(_name(name), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)

    def stat_entry(parent, name):
        return os.stat(_name(name), dir_fd=parent, follow_symlinks=False)

    def open_file(parent, name, flags, mode=0o600):
        return os.open(_name(name), flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=parent)

    def mkdir(parent, name, mode=0o700):
        os.mkdir(_name(name), mode, dir_fd=parent)

    def rmdir(parent, name):
        os.rmdir(_name(name), dir_fd=parent)

    def unlink(parent, name):
        os.unlink(_name(name), dir_fd=parent)

    def replace(parent, source, target):
        os.replace(_name(source), _name(target), src_dir_fd=parent, dst_dir_fd=parent)

    def rename_dir(parent, source, target):
        """Linux renameat2 NOREPLACE closes the target-existence race."""
        import ctypes
        source, target = _name(source), _name(target)
        information = stat_entry(parent, source)
        if not stat.S_ISDIR(information.st_mode):
            raise UnsafePathError('仅可重命名普通目录')
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renameat2', None)
        if rename is None:
            raise UnsafePathError('系统缺少安全目录重命名支持（需要 Linux renameat2）')
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(parent, os.fsencode(source), parent, os.fsencode(target), 1):
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number), target)
        sync_dir(parent)

    def sync_dir(parent):
        os.fsync(parent)

    def lock(descriptor, *, blocking=False):
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))

    def close(descriptor):
        os.close(descriptor)
