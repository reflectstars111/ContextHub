#!/usr/bin/env python3
"""Check the CLI guide and download using a temporary Hub, never live accounts."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from playwright.sync_api import expect, sync_playwright
from contexthub import __version__
from contexthub.distribution import cli_archive
from scripts.verify_web import service


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        static = root / 'static'
        shutil.copytree(PROJECT / 'contexthub/static', static)
        staging = os.environ.get('CONTEXTHUB_STAGED_INDEX')
        if staging:
            shutil.copyfile(staging, static / 'index.html')
        staged_static = os.environ.get('CONTEXTHUB_STAGED_STATIC')
        if staged_static:
            for file in Path(staged_static).iterdir():
                if file.is_file() and file.suffix in ('.html', '.css', '.ps1'):
                    shutil.copyfile(file, static / file.name)
        with patch('contexthub.server.STATIC', static), service(root / 'hub') as (_, url), sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page(accept_downloads=True, viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            expect(page.locator('#cli-guide-link')).to_be_visible()
            page.locator('#cli-guide-link').click()
            expect(page.locator('h1')).to_contain_text('让对话持续演化')
            expect(page.locator('#windows')).to_contain_text('PowerShell')
            installer = page.request.get(url + '/install-ctx.ps1')
            assert installer.ok and 'Get-HubOrigin' in installer.text()
            assert page.locator('pre').count() >= 5
            with page.expect_download() as info:
                page.get_by_role('link', name=f'下载 ctx · {__version__}').click()
            path = root / 'ctx.pyz'
            info.value.save_as(path)
            assert path.read_bytes() == cli_archive()
            checksum = page.request.get(url + '/ctx.pyz.sha256')
            assert checksum.ok
            assert checksum.text().split()[0] == hashlib.sha256(path.read_bytes()).hexdigest()
            result = subprocess.run([sys.executable, str(path), '--version'], cwd=root,
                                    capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            assert __version__ in result.stdout
            page.screenshot(path=artifacts / 'ctx-guide-desktop.png', full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(url + '/cli')
            expect(page.get_by_role('link', name=f'下载 ctx · {__version__}')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=artifacts / 'ctx-guide-mobile.png', full_page=True)
            browser.close()
    assert not errors, errors
    report = {'passed': True, 'version': __version__, 'desktop_mobile': True,
              'download_checksum': True, 'download_executable': True, 'browser_errors': errors}
    (artifacts / 'ctx-web-verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
