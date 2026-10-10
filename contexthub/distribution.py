"""Reproducible, source-only CLI distribution; no data directories are read."""
from functools import lru_cache
import hashlib
import io
from pathlib import Path
import sys
import zipfile

MODULES = ('__init__', '__main__', 'cli_runtime', 'codex', 'ctx', 'hub_client', 'workspace',
           'importers', 'package', 'platforms', 'revisions', 'safe_fs', 'windows_credentials',
           'windows_launcher', 'merge', 'git_bridge', 'codex_timeline', 'checkpoints', 'code_sync', 'capture', 'code_repository', 'code_capture')
ENTRY_POINT = '''import sys
if sys.version_info < (3, 12) or sys.platform not in ('linux', 'win32'):
    sys.exit('ctx requires Windows or Linux with Python 3.12 or newer.')
from contexthub.ctx import main
sys.exit(main())
'''


@lru_cache(maxsize=1)
def cli_archive():
    root = Path(__file__).parent
    files = {'__main__.py': ENTRY_POINT.encode(), 'LICENSE': (root / 'CLI_LICENSE.txt').read_bytes()}
    for name in MODULES:
        files[f'contexthub/{name}.py'] = (root / f'{name}.py').read_bytes()
    output = io.BytesIO()
    output.write(b'#!/usr/bin/env python3\n')
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


def cli_checksum():
    return hashlib.sha256(cli_archive()).hexdigest() + '  ctx.pyz\n'


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Build the self-contained ContextHub CLI')
    parser.add_argument('-o', '--output', required=True)
    args = parser.parse_args()
    target = Path(args.output)
    with target.open('xb') as file:
        file.write(cli_archive())
    if sys.platform != 'win32':
        target.chmod(0o755)
    print(cli_checksum(), end='')
