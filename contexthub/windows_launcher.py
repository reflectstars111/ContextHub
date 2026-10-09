"""Build a native console launcher using this Python installation's pip stub.

No package is installed, imported, or downloaded. The executable layout is the
standard distlib console PE + interpreter shebang + __main__.py ZIP layout.
The installed Python remains required; the launcher never invokes a shell.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import stat
import struct
import sys
import sysconfig
import zipfile


class LauncherError(ValueError):
    """This Python installation cannot safely supply a Windows launcher."""


_ARCHITECTURES = {
    'win32': ('t32.exe', 0x014C, 0x010B),
    'win-amd64': ('t64.exe', 0x8664, 0x020B),
    'win-arm64': ('t64-arm.exe', 0xAA64, 0x020B),
}
_MAX_STUB = 1024 * 1024
_MAX_WHEEL = 32 * 1024 * 1024
_MAIN = b'''import os
import runpy
import sys

archive = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "ctx.pyz")
sys.argv[0] = archive
runpy.run_path(archive, run_name="__main__")
'''


def _architecture():
    platform = sysconfig.get_platform().lower()
    try:
        return _ARCHITECTURES[platform]
    except KeyError:
        raise LauncherError('ctx.exe 需要 Windows x86、x64 或 ARM64 Python；当前 Python 平台不受支持') from None


def _validate_stub(data, architecture):
    _, machine, optional_magic = architecture
    if not 1024 <= len(data) <= _MAX_STUB or data[:2] != b'MZ':
        raise LauncherError('Python 中的 Windows launcher 资源不是有效的 PE 文件')
    offset = struct.unpack_from('<I', data, 0x3C)[0]
    if offset < 64 or offset + 24 > len(data) or data[offset:offset + 4] != b'PE\0\0':
        raise LauncherError('Windows launcher PE 头无效')
    actual_machine, sections = struct.unpack_from('<HH', data, offset + 4)
    optional_size, characteristics = struct.unpack_from('<HH', data, offset + 20)
    if (actual_machine != machine or not 1 <= sections <= 32
            or not 72 <= optional_size <= 1024
            or offset + 24 + optional_size + sections * 40 > len(data)
            or not characteristics & 0x2 or characteristics & 0x2000):
        raise LauncherError('Windows launcher 架构与当前 Python 不匹配，或 PE 头无效')
    if (struct.unpack_from('<H', data, offset + 24)[0] != optional_magic
            or struct.unpack_from('<H', data, offset + 24 + 68)[0] != 3):
        raise LauncherError('需要与当前 Python 匹配的 Windows 控制台 launcher')
    return data


def _bounded_file(path, limit):
    with path.open('rb') as stream:
        information = os.fstat(stream.fileno())
        if not stat.S_ISREG(information.st_mode) or information.st_size > limit:
            raise LauncherError('Python launcher 资源文件类型或大小无效')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise LauncherError('Python launcher 资源超过大小限制')
    return data


def _find_stub(architecture):
    filename = architecture[0]
    paths = sysconfig.get_paths()
    # Deliberately ignore cwd, PYTHONPATH and arbitrary sys.path entries: merely
    # having a project named pip must not turn its executable into our launcher.
    roots = []
    for key in ('purelib', 'platlib'):
        value = paths.get(key)
        if value and value not in roots:
            roots.append(value)
    for root in roots:
        candidate = Path(root) / 'pip' / '_vendor' / 'distlib' / filename
        try:
            data = _bounded_file(candidate, _MAX_STUB)
        except FileNotFoundError:
            continue
        return _validate_stub(data, architecture)
    standard_library = paths.get('stdlib')
    if standard_library:
        bundled = Path(standard_library) / 'ensurepip' / '_bundled'
        for wheel in sorted(bundled.glob('pip-*.whl'), reverse=True):
            data = _bounded_file(wheel, _MAX_WHEEL)
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    member = 'pip/_vendor/distlib/' + filename
                    matches = [entry for entry in archive.infolist() if entry.filename == member]
                    if not matches:
                        continue
                    if (len(matches) != 1 or matches[0].is_dir()
                            or matches[0].file_size > _MAX_STUB or matches[0].flag_bits & 1):
                        raise LauncherError('ensurepip wheel 的 launcher 资源无效')
                    with archive.open(matches[0]) as stream:
                        stub = stream.read(_MAX_STUB + 1)
                    return _validate_stub(stub, architecture)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                raise LauncherError('ensurepip wheel 无法安全读取') from exc
    raise LauncherError(
        '当前 Python 缺少 Windows launcher 资源。请使用包含 pip 或 ensurepip 的完整 Python 3.12+ 安装；'
        '也可继续用 py ctx.pyz 直接运行，无需 ctx.exe。')


def build_launcher(output):
    """Create output exclusively; it will run the ctx.pyz beside the EXE.

    The selected interpreter ABI, rather than the host CPU/OS architecture,
    determines the launcher (for example x64 Python on Windows ARM64).
    """
    architecture = _architecture()
    stub = _find_stub(architecture)
    executable = sys.executable
    if (not executable or not os.path.isabs(executable)
            or any(character in executable for character in ('"', '\r', '\n', '\0'))):
        raise LauncherError('当前 Python 可执行文件路径不能用于 Windows launcher')
    shebang = ('#!"' + executable + '"\n').encode('utf-8')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        info = zipfile.ZipInfo('__main__.py', date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(info, _MAIN)
    payload = stub + shebang + stream.getvalue()
    target = Path(output)
    # Creation is exclusive: an existing executable or symlink is never
    # overwritten. The caller stages this artifact before switching installs.
    with target.open('xb') as destination:
        destination.write(payload)
        destination.flush()
        os.fsync(destination.fileno())
    return target
