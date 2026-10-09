#!/usr/bin/env python3
"""Browser acceptance using temporary storage and synthetic Codex adapter fixtures.

Requires optional Playwright; never opens the real user's Codex session store.
"""
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from contexthub.package import build_package, read_package
from contexthub.server import create_server
from playwright.sync_api import sync_playwright, expect

CONTENT = '合成浏览器消息 <script>window.contextHubInjected=true</script>\n```python\nprint("你好")\n```'
CONVERSATION = {'id': 'web-synthetic', 'title': '浏览器合成对话', 'source': {'method': 'synthetic'}, 'messages': [
    {'id': 'u1', 'role': 'user', 'content': CONTENT},
    {'id': 'a1', 'role': 'assistant', 'content': '浏览器合成回复，仅用于验收。'},
]}


@contextlib.contextmanager
def service(path, mode='hub'):
    server = create_server(path, port=0, mode=mode, token='browser-fixture-token')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def register(page, name):
    page.locator('#account-button').click()
    page.locator('#register-tab').click()
    page.locator('#username').fill(name)
    page.locator('#password').fill('synthetic-password-123')
    page.locator('#auth-submit').click()
    expect(page.locator('#auth-dialog')).not_to_be_visible()
    expect(page.locator('#account-name')).to_contain_text(name)


def upload(page, path, visibility):
    page.locator('#open-upload').click()
    page.locator('#upload-file').set_input_files(str(path))
    page.locator(f'input[name="visibility"][value="{visibility}"]').check()
    # Upload selection is inspected before the explicit publish action.
    expect(page.locator('#upload-submit')).to_be_enabled(timeout=10000)
    page.locator('#upload-submit').click()
    expect(page.locator('#reader-view')).to_be_visible(timeout=10000)
    expect(page.locator('#messages')).to_contain_text(CONTENT)


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory(prefix='contexthub-web-') as temp, sync_playwright() as playwright:
        root = Path(temp)
        blob = build_package([CONVERSATION], '浏览器公开验收', '合成内容，不包含私人对话。')
        pack = root / 'browser.contextpack'
        pack.write_bytes(blob)
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        with service(root / 'hub') as (hub, url):
            alice = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            page = alice.new_page()
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.goto(url)
            expect(page.locator('#library-view')).to_be_visible()
            register(page, 'browseralice')
            upload(page, pack, 'public')
            assert page.evaluate('window.contextHubInjected === undefined'), 'Untrusted message executed'
            with page.expect_download() as download_info:
                page.locator('#download-package').click()
            download = download_info.value
            downloaded = root / 'downloaded.contextpack'
            download.save_as(downloaded)
            assert downloaded.read_bytes() == blob, 'Downloaded package bytes changed'
            page.screenshot(path=str(artifacts / 'reader-desktop.png'), full_page=True)
            page.locator('#reader-back').click()
            private = root / 'private.contextpack'
            private.write_bytes(build_package([CONVERSATION], '只能自己看到的私有验收'))
            upload(page, private, 'private')
            page.locator('#reader-back').click()
            expect(page.locator('#package-grid')).to_contain_text('只能自己看到')
            page.locator('#search-input').fill('公开验收')
            page.locator('#search-form').evaluate('(form) => form.requestSubmit()')
            expect(page.locator('#package-grid')).not_to_contain_text('只能自己看到')
            page.reload()
            expect(page.locator('#account-name')).to_contain_text('browseralice')
            page.screenshot(path=str(artifacts / 'hub-desktop.png'), full_page=True)
            anonymous = browser.new_context(viewport={'width': 390, 'height': 844})
            mobile = anonymous.new_page()
            mobile.on('pageerror', lambda exc: errors.append(str(exc)))
            mobile.goto(url)
            expect(mobile.locator('#package-grid')).to_contain_text('浏览器公开验收')
            expect(mobile.locator('#package-grid')).not_to_contain_text('只能自己看到')
            assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile horizontal overflow'
            mobile.screenshot(path=str(artifacts / 'hub-mobile.png'), full_page=True)
            register(mobile, 'browserbob')
            expect(mobile.locator('#package-grid')).not_to_contain_text('只能自己看到')
            alice.close()
            anonymous.close()

        # HTTP, package validation and UI are real; only the Codex adapter is synthetic.
        session = {'id': 'web-synthetic', 'name': '浏览器合成对话', 'preview': '合成预览', 'updatedAt': 1791500000, 'source': 'cli'}
        receipt = {'thread_id': 'fixture-new-thread', 'source_id': 'web-synthetic', 'mode': 'context-injection',
                   'injected': True, 'verified': False, 'api_readback_verified': False,
                   'warnings': ['Synthetic fixture: native UI and model continuation not verified']}
        with patch('contexthub.codex.CodexClient'), patch('contexthub.codex.list_sessions', return_value=[session]), \
             patch('contexthub.codex.export_conversation', return_value=CONVERSATION), \
             patch('contexthub.codex.import_conversation', return_value=receipt) as importer, \
             service(root / 'connector', mode='connector') as (connector, url):
            local = browser.new_context(accept_downloads=True)
            page = local.new_page()
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.goto(url + '/#token=browser-fixture-token')
            expect(page.locator('#session-rows')).to_contain_text('浏览器合成对话')
            page.locator('#select-all').check()
            page.locator('#preview-selected').click()
            expect(page.locator('#messages')).to_contain_text(CONTENT)
            page.locator('#messages .message').first.get_by_text('编辑', exact=True).click()
            edited_content = CONTENT + '\n已人工检查。'
            page.locator('#messages textarea').fill(edited_content)
            page.get_by_text('保存修改', exact=True).click()
            page.once('dialog', lambda dialog: dialog.accept())
            page.locator('#messages .message').nth(1).get_by_text('移除', exact=True).click()
            page.locator('#export-title').fill('连接器浏览器验收')
            with page.expect_download() as download_info:
                page.locator('#download-package').click()
            export_path = root / 'connector.contextpack'
            download_info.value.save_as(export_path)
            parsed = read_package(export_path.read_bytes())
            exported = parsed['conversations'][0]
            assert len(exported['messages']) == 1
            assert exported['messages'][0]['content'] == edited_content
            assert 'human_edited' in exported['messages'][0]['transformations']
            assert any('message_removed' in note.get('text', '') for note in exported['annotations'])
            # Use a navigation button, avoiding assumptions about DOM routing.
            page.locator('#nav-list').get_by_text('导入对话包', exact=True).click()
            page.locator('#import-file').set_input_files(str(downloaded))
            page.locator('#inspect-package').click()
            expect(page.locator('#inspect-result')).to_be_visible()
            page.locator('#import-cwd').fill(str(root))
            page.locator('#import-confirm').click()
            expect(page.locator('#import-receipt')).to_contain_text('fixture-new-thread')
            assert importer.call_count == 1
            assert not page.evaluate('window.contextHubInjected'), 'Connector rendered unsafe HTML'
            page.screenshot(path=str(artifacts / 'connector-import.png'), full_page=True)
            importer.return_value = {'thread_id': 'fixture-partial', 'mode': 'failed', 'injected': False,
                                     'warnings': ['Synthetic injection failure'], 'verified': False}
            page.locator('#inspect-package').click()
            expect(page.locator('#import-confirm')).to_be_enabled()
            page.locator('#import-confirm').click()
            expect(page.locator('#import-receipt')).to_contain_text('内容导入未完成')
            assert importer.call_count == 2
            local.close()
        browser.close()
    assert not errors, errors
    result = {'hub_upload_download': True, 'exact_bytes': True, 'private_acl_two_accounts': True,
              'search_and_login_refresh': True, 'mobile_no_overflow': True, 'untrusted_text_inert': True,
              'connector_fixture_export_import': True, 'javascript_errors': errors,
              'edited_and_removed_messages_recorded': True, 'failed_import_labeled': True,
              'real_codex_data_access': False, 'screenshots': str(artifacts)}
    (artifacts / 'web-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
