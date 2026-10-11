#!/usr/bin/env python3
"""Real HTTP/browser acceptance for turn directories and portable code browsing.

All conversations, accounts and code are synthetic; storage is temporary.
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from contexthub.package import build_package
from scripts.verify_web import service, register
from scripts.verify_versions_web import preview, save
from tests.test_code_repository import repository
from playwright.sync_api import sync_playwright, expect


def upload(page, path):
    page.locator('#open-upload').click()
    page.locator('#upload-file').set_input_files(str(path))
    expect(page.locator('#upload-submit')).to_be_enabled()
    page.locator('#upload-submit').click()
    expect(page.locator('#reader-view')).to_be_visible()


def main():
    messages = []
    for number, date in ((1, '2026-10-08T08:00:00Z'), (2, '2026-10-09T10:30:00Z'), (3, None)):
        for role, suffix in (('user', 'u'), ('assistant', 'a')):
            source = {'turn_id': f'turn-{number}'}
            if date:
                source['created_at'] = date
            messages.append({'id': f'm{number}{suffix}', 'role': role,
                             'content': f'第{number}轮 {role} 合成内容', 'provenance': source,
                             'transformations': ['codex-user-assistant-projection']})
    conversations = [{'id': 'c1', 'title': '有日期的开发记录', 'messages': messages},
                     {'id': 'long', 'title': '较长轮次', 'messages': [{'id': f'long-{i}', 'role': 'user' if i == 0 else 'assistant', 'content': f'同轮消息 {i}', 'provenance': {'turn_id': 'long'}} for i in range(25)]}]
    first_code = repository({'app.py': b'print("first")\n', 'docs/readme.txt': b'old\n'})
    code = repository({'app.py': b'print("second")\n', 'docs/readme.txt': b'<script>window.codeInjected=true</script>\n', 'assets/image.bin': bytes(range(256))},
                      previous=first_code, bindings=[{'conversation_id': 'c1', 'message_id': 'm3a', 'kind': 'captured'}])
    errors = []
    with tempfile.TemporaryDirectory(prefix='contexthub-code-web-') as temp, sync_playwright() as playwright:
        root = Path(temp)
        pack = root / 'code.contextpack'
        pack.write_bytes(build_package(conversations, '代码与对话目录验收', code=code))
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        with service(root / 'hub') as (_, url):
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            register(page, 'codebrowseralice')
            upload(page, pack)
            pinned = page.url
            expect(page.locator("#code-browser-panel")).not_to_be_visible()
            expect(page.locator("#version-history-panel")).not_to_be_visible()
            expect(page.locator("#library-settings")).not_to_be_visible()
            page.locator('#conversation-tabs button').nth(1).click()
            expect(page.locator('#messages .message')).to_have_count(20)
            page.get_by_text('较新消息', exact=True).click()
            expect(page.locator('#messages .message')).to_have_count(5)
            expect(page.locator('#messages')).to_contain_text('同轮消息 24')
            page.locator('#conversation-tabs button').first.click()
            expect(page.locator('#messages .message')).to_have_count(2)
            expect(page.locator('#messages')).to_contain_text('第3轮')
            expect(page.locator('#messages')).not_to_contain_text('第1轮')
            expect(page.locator('#turn-directory')).to_contain_text('2026/10/08')
            expect(page.locator('#turn-directory')).to_contain_text('时间未记录')
            expect(page.locator('#messages')).not_to_contain_text('查看本条消息的变更记录')
            expect(page.locator('#messages .provenance summary').first).to_have_text('导入与处理记录')
            expect(page.locator('#messages')).to_contain_text('未记录当时代码')
            page.locator('.message-code-link button').click()
            expect(page.locator('#conversation-section')).not_to_be_visible()
            expect(page.locator('#code-browser-panel')).to_be_visible()
            expect(page.locator('#code-diff-panel')).to_contain_text('修改 2')
            expect(page.locator('#code-diff-panel')).to_contain_text('新增 1')
            expect(page.locator('#code-diff-panel details[open]')).to_have_count(0)
            page.locator('#code-diff-panel summary').filter(has_text='app.py').click()
            expect(page.locator('#code-diff-panel .code-patch').first).to_contain_text('-print("first")')
            expect(page.locator('#code-diff-panel .code-patch').first).to_contain_text('+print("second")')
            page.locator('#code-files-tab').click()
            expect(page.locator('#code-tree [data-path="docs/readme.txt"]')).to_have_count(0)
            page.locator('#code-tree [data-path="docs/"]').click()
            page.locator('#code-tree [data-path="docs/readme.txt"]').click()
            expect(page.locator('#code-file')).to_contain_text('<script>window.codeInjected=true</script>')
            assert not page.evaluate('window.codeInjected')
            page.locator('#code-breadcrumb button').first.click()
            page.locator('#code-tree [data-path="assets/"]').click()
            page.locator('#code-tree [data-path="assets/image.bin"]').click()
            expect(page.locator('#code-file')).to_contain_text('二进制文件')
            page.locator('#code-snapshot-select').select_option(first_code['head'])
            page.locator('#code-tree [data-path="app.py"]').click()
            expect(page.locator('#code-file-content, .code-file-content')).to_have_text('print("first")\n')
            assert all(parse_qs(urlsplit(page.url).query)[key] == parse_qs(urlsplit(pinned).query)[key] for key in ('package', 'version')), 'Code browsing changed the pinned package/version'
            page.locator('#code-snapshot-select').select_option(code['head'])
            page.locator('[data-repository-section="conversation-section"]').click()
            page.locator('#turn-search').fill('第1轮')
            expect(page.locator('#messages')).to_contain_text('第1轮')
            expect(page.locator('#messages .message')).to_have_count(2)
            page.locator('#turn-search').fill('找不到的关键词')
            expect(page.locator('#messages .message')).to_have_count(0)
            expect(page.locator('#turn-directory')).to_contain_text('没有匹配')
            page.locator('#turn-search').fill('')
            page.locator('#edit-package').click()
            expect(page.locator('#draft-panel')).to_be_visible()
            page.locator('#turn-search').fill('第1轮')
            expect(page.locator('#messages')).to_contain_text('第1轮 user 合成内容')
            page.locator('#messages .message').first.get_by_text('编辑', exact=True).click()
            page.locator('#messages textarea').fill('第1轮 user 已修改')
            # Navigating away from an unsaved inline editor must preserve it.
            page.locator('#conversation-tabs button').nth(1).click()
            expect(page.locator('#messages textarea')).to_have_value('第1轮 user 已修改')
            page.get_by_text('保存修改', exact=True).click()
            expect(page.locator('#draft-autosave-status')).to_contain_text('草稿已保存到服务器')
            ident = page.evaluate('state.reader.id')
            draft = page.request.get(url + f'/api/packages/{ident}/draft').json()['draft']
            assert draft['conversations'][0]['messages'][0]['content'] == '第1轮 user 已修改', draft['conversations'][0]['messages']
            assert draft['conversations'][0]['messages'][4]['content'] == '第3轮 user 合成内容'
            page.get_by_text('＋ 用户消息', exact=True).click()
            expect(page.locator('#messages textarea')).to_be_visible()
            page.locator('#messages textarea').fill('新轮次自动定位')
            page.get_by_text('保存修改', exact=True).click()
            expect(page.locator('#messages .message')).to_have_count(1)
            expect(page.locator('#messages')).to_contain_text('新轮次自动定位')
            expect(page.locator('#draft-autosave-status')).to_contain_text('草稿已保存到服务器')
            # Replace the draft with a package whose only difference is code.
            next_code = repository({'app.py': b'print("third")\n'}, previous=code)
            replacement = root / 'replacement.contextpack'
            replacement.write_bytes(build_package(conversations, '代码与对话目录验收', code=next_code))
            page.locator('#version-file').set_input_files(str(replacement))
            expect(page.locator('#candidate-actions')).to_be_visible()
            page.locator('#apply-candidate').click()
            expect(page.locator('#code-browser-note')).to_contain_text('候选代码尚未发布')
            expect(page.locator('#code-diff-tab')).to_be_disabled()
            expect(page.locator('#draft-autosave-status')).to_contain_text('草稿已保存到服务器')
            page.reload()
            expect(page.locator('#draft-recovery')).to_be_visible()
            page.locator('#restore-draft').click()
            expect(page.locator('#draft-panel')).to_be_visible()
            expect(page.locator('#code-browser-note')).to_contain_text('候选代码尚未发布')
            preview(page, '仅更新代码文件')
            expect(page.locator('#draft-comparison .code-version-comparison')).to_contain_text('代码 · 新增 0 · 修改 1 · 删除 2')
            save(page, 2)
            expect(page.locator('#code-snapshot-select')).to_have_value(next_code['head'])
            page.locator('[data-repository-section="code-browser-panel"]').click()
            page.locator('#code-tree [data-path="app.py"]').click()
            expect(page.locator('.code-file-content')).to_have_text('print("third")\n')
            page.screenshot(path=str(PROJECT / '.artifacts/code-browser-desktop.png'), full_page=True)
            page.locator('[data-repository-section="version-history-panel"]').click()
            expect(page.locator('#version-history-list .version-history-row')).to_have_count(2)
            expect(page.locator('#code-browser-panel')).not_to_be_visible()
            expect(page.locator('#conversation-section')).not_to_be_visible()
            page.locator('[data-repository-section="conversation-section"]').click()
            page.screenshot(path=str(PROJECT / '.artifacts/code-conversation-desktop.png'), full_page=True)
            for width in (320, 390, 768):
                page.set_viewport_size({'width': width, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Overflow at {width}px'
            page.screenshot(path=str(PROJECT / '.artifacts/code-conversation-mobile.png'), full_page=True)
            context.close()
        browser.close()
    assert not errors, errors
    result = {'exclusive_repository_tabs': True, 'version_history_list': True, 'turn_directory_latest_only': True, 'bounded_long_turn_pagination': True, 'code_only_candidate_autosave_reload_publish': True, 'real_dates_and_unknown_time': True,
              'search_and_original_index_edit': True, 'new_message_selected': True,
              'code_tree_file_and_binary_preview': True, 'message_to_real_diff': True,
              'historical_snapshot_and_pinned_version': True, 'safe_text_rendering': True,
              'mobile_no_overflow': True, 'javascript_errors': errors, 'real_user_data_access': False}
    (PROJECT / '.artifacts/code-web-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
