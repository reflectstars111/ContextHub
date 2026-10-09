"""Shared terminal behavior for the workspace and transfer command entry points."""

import shlex
import sys


def configure_output():
    """Use the same UTF-8 contract for console, pipes, zipapp and EXE commands."""
    if sys.platform == 'win32':
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8', errors='backslashreplace')


def quote_argument(value):
    """Quote an example argument for PowerShell on Windows, sh elsewhere."""
    value = str(value)
    if sys.platform == 'win32':
        return "'" + value.replace("'", "''") + "'"
    return shlex.quote(value)
