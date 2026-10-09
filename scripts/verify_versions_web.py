#!/usr/bin/env python3
"""Real-browser version/Fork acceptance with two accounts and synthetic data."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from contexthub.package import build_package, read_package
from scripts.verify_web import service, register, upload, CONVERSATION, CONTENT
from playwright.sync_api import sync_playwright, expect


def version_ids(page):
    params = parse_qs(urlsplit(page.url).query)
    return params['package'][0], params['version'][0]


def api(page, url, path, payload=None):
    if payload is None:
        response = page.request.get(url + path)
    else:
        csrf = page.request.get(url + '/api/session').json()['csrf_token']
        response = page.request.post(url + path, data=payload, headers={'X-CSRF-Token': csrf})
    assert response.ok, (response.status, response.text())
    return response.json()


def edit_message(page, text, index=0):
    page.locator('#messages .message').nth(index).get_by_text('编辑', exact=True).click()
    page.locator('#messages textarea').fill(text)
    page.get_by_text('保存修改', exact=True).click()


def preview(page, message):
    page.locator('#version-message').fill(message)
    page.locator('#preview-version').click()
    expect(page.locator('#draft-comparison')).to_be_visible()
    expect(page.locator('#save-version')).to_be_enabled()


def save(page, number):
    page.locator('#save-version').click()
    expect(page.locator('#draft-panel')).not_to_be_visible()
    expect(page.locator('#version-description')).to_contain_text(f'v{number}')
    expect(page.locator('#version-select')).to_have_count(1)


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory(prefix='contexthub-versions-web-') as temp, sync_playwright() as playwright:
        root = Path(temp)
        initial = build_package([CONVERSATION], '合成上游对话库', '版本与 Fork 浏览器验收')
        path = root / 'source.contextpack'
        path.write_bytes(initial)
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        with service(root / 'hub') as (hub, url):
            alice_context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            alice = alice_context.new_page()
            alice.on('pageerror', lambda error: errors.append(str(error)))
            alice.goto(url)
            register(alice, 'versionsalice')
            upload(alice, path, 'public')
            source_id, source_version = version_ids(alice)
            source_link = alice.url

            bob_context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            bob = bob_context.new_page()
            bob.on('pageerror', lambda error: errors.append(str(error)))
            bob.goto(source_link)
            expect(bob.locator('#reader-view')).to_be_visible()
            register(bob, 'versionsbob')
            expect(bob.locator('#edit-package')).not_to_be_visible()
            bob.locator('#fork-package').click()
            expect(bob.locator('input[name="fork-visibility"][value="private"]')).to_be_checked()
            bob.locator('input[name="fork-visibility"][value="public"]').check()
            bob.locator('#fork-submit').click()
            expect(bob.locator('#fork-dialog')).not_to_be_visible()
            expect(bob.locator('#upstream-info')).to_contain_text('versionsalice')
            fork_id, fork_v1 = version_ids(bob)
            assert fork_id != source_id
            fork_link = bob.url

            # Regression: an old source request must not replace a new draft.
            pending = []
            pattern = f'**/api/packages/{source_id}?version=*'
            bob.route(pattern, lambda route: pending.append(route))
            bob.locator('#upstream-info button').click()
            expect(bob.locator('#edit-package')).to_be_visible()
            bob.locator('#edit-package').click()
            bob.locator('#draft-title').fill('Bob 的持续整理')
            bob.wait_for_function('isDraft()')
            assert pending, 'Expected delayed upstream request'
            with bob.expect_response(lambda response: f'/api/packages/{source_id}?' in response.url):
                pending.pop().continue_()
            bob.wait_for_timeout(150)
            expect(bob.locator('#draft-panel')).to_be_visible()
            expect(bob.locator('#draft-title')).to_have_value('Bob 的持续整理')
            bob.unroute(pattern)
            edited = CONTENT + '\nBob 添加的上下文。'
            edit_message(bob, edited)
            bob.locator('#draft-context').fill('这份副本用于扩展讨论。')
            bob.get_by_text('＋ 助手消息', exact=True).click()
            bob.locator('#messages textarea').fill('新增的人工整理消息。')
            bob.get_by_text('保存修改', exact=True).click()
            preview(bob, '补充上下文与后续讨论')
            expect(bob.locator('#draft-comparison')).to_contain_text('新增消息 1')
            expect(bob.locator('#draft-comparison')).to_contain_text('修改消息 1')
            save(bob, 2)
            _, fork_v2 = version_ids(bob)
            assert fork_v2 != fork_v1
            unchanged = api(alice, url, f'/api/packages/{source_id}')
            assert unchanged['version']['id'] == source_version
            assert unchanged['conversations'][0]['messages'][0]['content'] == CONTENT
            bob.locator('#compare-versions').click()
            expect(bob.locator('#version-comparison')).to_contain_text('Bob 添加的上下文。')
            bob.screenshot(path=str(artifacts / 'versions-diff.png'), full_page=True)
            bob.locator('#version-select').select_option(fork_v1)
            expect(bob.locator('#version-position')).to_have_text('历史版本')
            expect(bob.locator('#messages')).not_to_contain_text('Bob 添加的上下文。')
            expect(bob.locator('#edit-package')).not_to_be_visible()
            old_link = bob.url
            with bob.expect_download() as info:
                bob.locator('#download-package').click()
            historical = root / 'v1.contextpack'
            info.value.save_as(historical)
            assert historical.read_bytes() == initial
            bob.reload()
            expect(bob.locator('#reader-view')).to_be_visible()
            assert bob.url == old_link
            expect(bob.locator('#version-position')).to_have_text('历史版本')
            bob.locator('#version-select').select_option(fork_v2)
            expect(bob.locator('#edit-package')).to_be_visible()

            # Updating an existing library by importing a complete new package.
            bob.locator('#edit-package').click()
            replacement = copy.deepcopy(CONVERSATION)
            replacement['messages'][0]['content'] = '替换包中的新内容，来源仍需核对。'
            candidate = root / 'candidate.contextpack'
            candidate.write_bytes(build_package([replacement], '导入包更新的对话库'))
            bob.locator('#version-file').set_input_files(str(candidate))
            expect(bob.locator('#candidate-actions')).to_be_visible()
            expect(bob.locator('#candidate-preview')).to_contain_text('替换包中的新内容')
            bob.locator('#apply-candidate').click()
            expect(bob.locator('#messages')).to_contain_text('替换包中的新内容')
            preview(bob, '导入后续对话包')
            save(bob, 3)
            _, fork_v3 = version_ids(bob)
            assert api(bob, url, f'/api/packages/{fork_id}?version={fork_v2}')['conversations'][0]['messages'][0]['content'] == edited

            # Two tabs both preview v3, one publishes; the other's draft remains.
            other = bob_context.new_page()
            other.on('pageerror', lambda error: errors.append(str(error)))
            other.goto(bob.url)
            expect(other.locator('#edit-package')).to_be_visible()
            other.locator('#edit-package').click()
            edit_message(other, '第二标签页需要保留的草稿')
            preview(other, '第二标签页修改')
            bob.locator('#edit-package').click()
            edit_message(bob, '第一标签页已保存的内容')
            preview(bob, '第一标签页修改')
            save(bob, 4)
            other.locator('#save-version').click()
            expect(other.locator('#draft-status')).to_contain_text('草稿已保留')
            expect(other.locator('#draft-panel')).to_be_visible()
            expect(other.locator('#messages')).to_contain_text('第二标签页需要保留的草稿')
            expect(other.locator('#save-version')).to_be_disabled()
            head = api(bob, url, f'/api/packages/{fork_id}')
            assert head['version']['number'] == 4
            assert head['conversations'][0]['messages'][0]['content'] == '第一标签页已保存的内容'
            bob.screenshot(path=str(artifacts / 'versions-reader.png'), full_page=True)

            mobile_context = browser.new_context(viewport={'width': 390, 'height': 844})
            mobile = mobile_context.new_page()
            mobile.on('pageerror', lambda error: errors.append(str(error)))
            mobile.goto(bob.url)
            expect(mobile.locator('#version-description')).to_contain_text('v4')
            mobile.locator('#compare-versions').click()
            expect(mobile.locator('#version-comparison')).to_contain_text('第一标签页已保存的内容')
            assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
            mobile.screenshot(path=str(artifacts / 'versions-mobile.png'), full_page=True)
            mobile_context.close()
            bob_context.close()
            alice_context.close()
        browser.close()
    assert not errors, errors
    result = {'two_account_fork': True, 'upstream_unchanged': True, 'edit_add_preview_publish': True,
              'comparison_and_historical_download': True, 'fixed_link_reload': True,
              'replace_with_imported_package': True, 'stale_save_preserves_draft': True,
              'delayed_read_preserves_draft': True, 'mobile_no_overflow': True,
              'javascript_errors': errors, 'real_user_data_access': False}
    (artifacts / 'versions-web-verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
