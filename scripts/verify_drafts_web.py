#!/usr/bin/env python3
"""Synthetic browser acceptance: private autosave, conflicts and lifecycle."""
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from playwright.sync_api import expect, sync_playwright
from contexthub.package import build_package
from scripts.verify_web import service, register, upload, CONVERSATION
from scripts.verify_versions_web import api, edit_message, preview, save


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory() as temporary, sync_playwright() as p:
        root = Path(temporary)
        fixture = root / 'fixture.contextpack'
        fixture.write_bytes(build_package([CONVERSATION], '私有草稿验收'))
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        with service(root / 'hub') as (_, url):
            owner_context = browser.new_context(accept_downloads=True)
            page = owner_context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            register(page, 'draftowner')
            upload(page, fixture, 'public')
            ident = parse_qs(urlsplit(page.url).query)['package'][0]
            link = page.url
            page.locator('#edit-package').click()
            expect(page.locator('#draft-panel')).to_be_visible()
            page.locator('#messages .message').first.get_by_text('编辑', exact=True).click()
            page.locator('#messages textarea').fill('尚未点击保存修改，也要自动保存的中文草稿')
            expect(page.locator('#draft-autosave-status')).to_contain_text('草稿已保存到服务器')
            stored = api(page, url, f'/api/packages/{ident}/draft')['draft']
            assert stored['conversations'][0]['messages'][0]['content'] == '尚未点击保存修改，也要自动保存的中文草稿'
            assert api(page, url, f'/api/packages/{ident}')['version']['number'] == 1
            page.reload()
            expect(page.locator('#draft-recovery')).to_be_visible()
            page.locator('#restore-draft').click()
            expect(page.locator('#messages')).to_contain_text('尚未点击保存修改，也要自动保存的中文草稿')

            outsider = browser.new_context()
            bob = outsider.new_page(); bob.goto(link)
            assert bob.request.get(url + f'/api/packages/{ident}/draft').status == 401
            register(bob, 'draftvisitor')
            assert bob.request.get(url + f'/api/packages/{ident}/draft').status == 404
            assert '尚未点击' not in bob.locator('#messages').inner_text()

            other = owner_context.new_page(); other.goto(link)
            other.on('pageerror', lambda error: errors.append(str(error)))
            other.locator('#edit-package').click()
            expect(other.locator('#draft-panel')).to_be_visible()
            edit_message(page, '主页面保存的草稿')
            page.locator('#save-private-draft').click()
            expect(page.locator('#draft-autosave-status')).to_contain_text('草稿已保存到服务器')
            edit_message(other, '另一页的冲突修改不能丢失')
            other.locator('#save-private-draft').click()
            expect(other.locator('#draft-autosave-status')).to_contain_text('自动保存已暂停')
            assert api(page, url, f'/api/packages/{ident}/draft')['draft']['conversations'][0]['messages'][0]['content'] == '主页面保存的草稿'
            with other.expect_download() as download:
                other.locator('#download-draft-backup').click()
            backup = root / 'draft.json'; download.value.save_as(backup)
            assert json.loads(backup.read_text())['conversations'][0]['messages'][0]['content'] == '另一页的冲突修改不能丢失'

            preview(page, '将草稿发布为正式版本'); save(page, 2)
            assert api(page, url, f'/api/packages/{ident}/draft')['draft'] is None
            page.locator('#library-settings').evaluate('(element) => { element.open = true; }')
            page.once('dialog', lambda dialog: dialog.accept())
            page.locator('#change-visibility').click()
            expect(page.locator('#reader-metadata')).to_contain_text('仅自己')
            assert bob.request.get(url + f'/api/packages/{ident}').status == 404
            # Private editing must not be silently published after another tab changes visibility.
            page.locator('#edit-package').click()
            expect(page.locator('#draft-panel')).to_be_visible()
            edit_message(page, '原本按私有编辑的内容')
            preview(page, '隐私竞态验收')
            page.locator('#save-private-draft').click()
            expect(page.locator('#draft-autosave-status')).to_contain_text('草稿已保存到服务器')
            current = api(page, url, f'/api/packages/{ident}')
            api(page, url, f'/api/packages/{ident}/visibility', {'visibility': 'public', 'base_version': current['head_version_id']})
            page.locator('#save-version').click()
            expect(page.locator('#draft-status')).to_contain_text('草稿已保留')
            assert api(page, url, f'/api/packages/{ident}')['version']['number'] == 2
            assert '原本按私有' not in api(bob, url, f'/api/packages/{ident}')['conversations'][0]['messages'][0]['content']
            page.reload()
            expect(page.locator('#draft-recovery')).to_be_visible()
            page.locator('#restore-draft').click()
            expect(page.locator('#save-version')).to_have_text('发布新版本')
            preview(page, '明确确认按新的公开设置发布'); save(page, 3)
            expect(page.locator('#reader-metadata')).to_contain_text('公开')
            detail = api(page, url, f'/api/packages/{ident}')
            fork = api(bob, url, f'/api/packages/{ident}/fork', {'version_id': detail['head_version_id'], 'visibility': 'private'})
            page.locator('#library-settings').evaluate('(element) => { element.open = true; }')
            page.once('dialog', lambda dialog: dialog.accept(ident))
            page.locator('#purge-package').click()
            expect(page.locator('#library-view')).to_be_visible()
            expect(page.locator('#status')).to_contain_text('永久清理')
            assert page.request.get(url + f'/api/packages/{ident}').status == 404
            assert bob.request.get(url + f'/api/packages/{fork["id"]}/download').status == 200
            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(url)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            browser.close()
    assert not errors, errors
    result = {'passed': True, 'keystroke_autosave_reload': True, 'owner_only_drafts': True,
              'multi_tab_conflict_backup': True, 'publish_consumes_matching_draft': True,
              'visibility_permissions': True, 'private_publish_race_blocked': True, 'purge_preserves_fork': True, 'browser_errors': errors}
    (artifacts / 'drafts-web-verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
