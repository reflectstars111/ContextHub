#!/usr/bin/env python3
"""Record reproducible platform evidence; never promote an untested platform."""
import json
import os
from pathlib import Path
import platform
import sys
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from contexthub import __version__


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    suite = unittest.defaultTestLoader.discover(str(PROJECT / 'tests'), top_level_dir=str(PROJECT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    evidence = {
        'version': __version__, 'python': platform.python_version(),
        'platform': platform.platform(), 'machine': platform.machine(),
        'native_windows': os.name == 'nt',
        'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': [{'test': str(test), 'reason': reason} for test, reason in result.skipped],
        'passed': result.wasSuccessful(),
    }
    (artifacts / 'release-platform-verification.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
