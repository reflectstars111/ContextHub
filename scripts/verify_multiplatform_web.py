#!/usr/bin/env python3
"""Exercise classified imports and exact archive exports with synthetic data.

All accounts, uploads, and storage are temporary. No model or real conversation
store is accessed. Requires the optional Playwright browser dependency.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from playwright.sync_api import expect, sync_playwright

from contexthub.package import read_package
from scripts.verify_versions_web import api
from scripts.verify_web import register, service


def fixture_file(root, name, data):
    path = root / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def new_page(browser, errors, **kwargs):
    context = browser.new_context(accept_downloads=True, **kwargs)
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    return page


def inspect_file(page, path, platform='auto', mode='normalize'):
    page.locator('#open-upload').click()
    page.locator('#upload-platform').select_option(platform)
    page.locator('#upload-mode').select_option(mode)
    page.locator('#upload-file').set_input_files(path)
    expect(page.locator('#upload-file-info')).to_contain_text('格式检查通过')
    return page.locator('#upload-confirmation').is_visible()


def finish_upload(page, visibility='public'):
    page.locator(f'input[name="visibility"][value="{visibility}"]').check()
    if page.locator('#upload-confirmation').is_visible():
        expect(page.locator('#upload-submit')).to_be_disabled()
        page.locator('#upload-confirm').check()
    expect(page.locator('#upload-submit')).to_be_enabled()
    page.locator('#upload-submit').click()
    expect(page.locator('#upload-dialog')).not_to_be_visible()
    expect(page.locator('#reader-view')).to_be_visible()
    # The selected version is the click-time reader snapshot used by exports.
    return page.evaluate('JSON.parse(JSON.stringify(state.reader))')


def return_to_library(page):
    page.locator('#reader-back').click()
    expect(page.locator('#library-view')).to_be_visible()


def verify_imports(browser, root, errors):
    claude = fixture_file(root, 'claude.json', [{
        'uuid': 'synthetic-claude', 'name': 'Claude 合成来源',
        'chat_messages': [
            {'uuid': 'u', 'sender': 'human', 'text': '保存 Claude 正文'},
            {'uuid': 'a', 'sender': 'assistant', 'text': 'Claude 合成回复',
             'attachments': [{'file_name': 'synthetic-attachment.pdf'}]},
        ],
    }])
    deepseek = fixture_file(root, 'deepseek.json', {
        'title': 'DeepSeek 合成来源', 'messages': [
            {'role': 'USER', 'content': '保存 DeepSeek 正文'},
            {'role': 'ASSISTANT', 'fragments': [
                {'type': 'THINK', 'content': 'synthetic-omitted-reasoning'},
                {'type': 'RESPONSE', 'content': 'DeepSeek 合成回复'},
            ]},
        ],
    })
    chatgpt = fixture_file(root, 'chatgpt.json', [{
        'title': 'ChatGPT 合成来源', 'current_node': 'chosen', 'mapping': {
            'root': {'parent': None, 'message': None},
            'u': {'parent': 'root', 'message': {
                'author': {'role': 'user'},
                'content': {'content_type': 'text', 'parts': ['保存 ChatGPT 正文']},
            }},
            'old': {'parent': 'u', 'message': {
                'author': {'role': 'assistant'},
                'content': {'content_type': 'text', 'parts': ['synthetic-old-branch']},
            }},
            'chosen': {'parent': 'u', 'message': {
                'author': {'role': 'assistant'},
                'content': {'content_type': 'text', 'parts': ['ChatGPT 当前分支']},
            }},
        },
    }])
    generic = fixture_file(root, 'generic.json', {
        'title': '参数竞争合成来源',
        'messages': [{'role': 'user', 'content': '通用正文'}],
    })
    archive_bytes = '\ufeff{ "unknown": "存档中文 <script>window.injected=true</script>" }\r\n'.encode('utf-8')
    archive_file = root / '原文档案.json'
    archive_file.write_bytes(archive_bytes)

    with service(root / 'hub') as (_, url):
        owner = new_page(browser, errors, viewport={'width': 1440, 'height': 1000})
        owner.goto(url)
        register(owner, 'multiplatformowner')
        assert inspect_file(owner, claude)
        expect(owner.locator('#upload-preview')).to_contain_text('Claude')
        expect(owner.locator('#upload-preview .notice')).to_contain_text('已略过附件和文件')
        expect(owner.locator('#upload-confirm')).not_to_be_checked()
        owner.locator('#upload-form').evaluate('(form) => form.requestSubmit()')
        expect(owner.locator('#upload-error')).to_contain_text('勾选确认')
        assert not api(owner, url, '/api/packages')['packages']
        first = finish_upload(owner)
        expect(owner.locator('#messages')).to_contain_text('Claude 合成回复')
        expect(owner.locator('#reader-metadata .platform-badge')).to_have_text('Claude')
        expect(owner.locator('#conversation-provenance')).to_contain_text('声明来源：Claude')
        return_to_library(owner)

        assert inspect_file(owner, deepseek, platform='deepseek')
        second = finish_upload(owner)
        expect(owner.locator('#messages')).to_contain_text('DeepSeek 合成回复')
        expect(owner.locator('#messages')).not_to_contain_text('synthetic-omitted-reasoning')
        return_to_library(owner)

        assert inspect_file(owner, chatgpt)
        expect(owner.locator('#upload-preview .notice')).to_contain_text('其他分支已略过')
        third = finish_upload(owner)
        expect(owner.locator('#messages')).to_contain_text('ChatGPT 当前分支')
        expect(owner.locator('#messages')).not_to_contain_text('synthetic-old-branch')
        owner.locator('#copy-format').select_option('json')
        with owner.expect_download() as download:
            owner.locator('#save-conversation').click()
        destination = root / 'normalized-chatgpt.json'
        download.value.save_as(destination)
        normalized = json.loads(destination.read_text(encoding='utf-8'))
        assert normalized['source']['platform'] == 'chatgpt'
        assert normalized['messages'][-1]['content'] == 'ChatGPT 当前分支'
        return_to_library(owner)

        # Filtering is done by the server and combines with existing text search.
        for category, package in [('claude', first), ('deepseek', second), ('chatgpt', third)]:
            owner.locator('#platform-filter').select_option(category)
            expect(owner.locator('.package-card')).to_have_count(1)
            expect(owner.locator('#package-grid')).to_contain_text(package['title'])
        owner.locator('#platform-filter').select_option('all')
        expect(owner.locator('.package-card')).to_have_count(3)
        owner.locator('#platform-filter').select_option('deepseek')
        owner.locator('#search-input').fill('Claude')
        owner.locator('#search-form').evaluate('(form) => form.requestSubmit()')
        expect(owner.locator('.package-card')).to_have_count(0)
        owner.locator('#search-input').fill('')
        owner.locator('#platform-filter').select_option('all')
        expect(owner.locator('.package-card')).to_have_count(3)

        # Reader categories belong to the selected version, not the current head.
        recategorized = copy.deepcopy(second['conversations'])
        api(owner, url, f'/api/packages/{first["id"]}/versions', {
            'base_version': first['version']['id'], 'title': first['title'],
            'description': '', 'message': '合成来源标签更新', 'conversations': recategorized,
        })
        owner.goto(f'{url}?package={first["id"]}')
        expect(owner.locator('#reader-metadata .platform-badge')).to_have_text('DeepSeek')
        owner.goto(f'{url}?package={first["id"]}&version={first["version"]["id"]}')
        expect(owner.locator('#reader-metadata .platform-badge')).to_have_text('Claude')
        return_to_library(owner)

        # Source record copying is also an authenticated fixed-version export.
        copier = new_page(browser, errors)
        copier.goto(f'{url}?package={first["id"]}&version={first["version"]["id"]}')
        expect(copier.locator('#reader-metadata .platform-badge')).to_have_text('Claude')
        copier.evaluate('() => { window.copyText = async (text) => { window.copiedSource = text; }; }')
        copier.locator('#conversation-provenance summary').click()
        copier.get_by_role('button', name='复制来源记录', exact=True).click()
        expect(copier.locator('#auth-dialog')).to_be_visible()
        assert copier.evaluate('window.copiedSource === undefined')
        copier.evaluate('(id) => loadPackage(id)', second['id'])
        expect(copier.locator('#reader-title')).to_have_text(second['title'])
        copier.locator('#register-tab').click()
        copier.locator('#username').fill('sourceexporter')
        copier.locator('#password').fill('synthetic-password-123')
        copier.locator('#auth-submit').click()
        copier.wait_for_function('() => Boolean(window.copiedSource)')
        copied = json.loads(copier.evaluate('window.copiedSource'))
        assert copied['source']['platform'] == 'claude'
        assert copied['id'] == first['conversations'][0]['id']

        # Unknown input cannot silently fall back to an archive.
        owner.locator('#open-upload').click()
        owner.locator('#upload-platform').select_option('deepseek')
        owner.locator('#upload-file').set_input_files(archive_file)
        expect(owner.locator('#upload-error')).to_be_visible()
        expect(owner.locator('#upload-mode')).to_have_value('normalize')
        expect(owner.locator('#upload-submit')).to_be_disabled()
        owner.locator('#upload-mode').select_option('archive')
        expect(owner.locator('#upload-file-info')).to_contain_text('格式检查通过')
        expect(owner.locator('#upload-confirmation')).to_be_visible()
        expect(owner.locator('#upload-preview')).to_contain_text('原文存档')
        expect(owner.locator('#upload-preview .notice')).to_contain_text('不会自动脱敏')
        archived = finish_upload(owner)
        expect(owner.locator('#conversation-provenance')).to_contain_text('原文存档')
        expect(owner.locator('#save-archive')).to_have_text('下载存档文本')
        assert owner.evaluate('window.injected === undefined')
        with owner.expect_download() as download:
            owner.locator('#save-archive').click()
        destination = root / 'archive-original.out'
        download.value.save_as(destination)
        assert download.value.suggested_filename == archive_file.name
        assert destination.read_bytes() == archive_bytes, 'Archive UTF-8/BOM/CRLF changed'

        # A newer edited version does not replace the historical archive export.
        conversations = copy.deepcopy(archived['conversations'])
        conversations[0]['messages'][0]['content'] = '当前存档文本\r\n'
        conversations[0]['messages'][0]['transformations'] = ['human_edited']
        updated = api(owner, url, f'/api/packages/{archived["id"]}/versions', {
            'base_version': archived['version']['id'], 'title': archived['title'],
            'description': '', 'message': '更新合成存档', 'conversations': conversations,
        })
        owner.goto(f'{url}?package={archived["id"]}&version={updated["version"]["id"]}')
        expect(owner.locator('#save-archive')).to_have_text('下载当前存档文本')
        historical_link = f'{url}?package={archived["id"]}&version={archived["version"]["id"]}'
        owner.goto(historical_link)
        expect(owner.locator('#save-archive')).to_have_text('下载存档文本')

        anonymous = new_page(browser, errors, viewport={'width': 390, 'height': 844})
        anonymous.goto(historical_link)
        expect(anonymous.locator('#save-archive')).to_be_visible()
        anonymous.locator('#save-archive').click()
        expect(anonymous.locator('#auth-dialog')).to_be_visible()
        anonymous.evaluate('(id) => loadPackage(id)', second['id'])
        expect(anonymous.locator('#reader-title')).to_have_text(second['title'])
        anonymous.locator('#register-tab').click()
        anonymous.locator('#username').fill('archiveexporter')
        anonymous.locator('#password').fill('synthetic-password-123')
        with anonymous.expect_download() as download:
            anonymous.locator('#auth-submit').click()
        destination = root / 'archive-authenticated.out'
        download.value.save_as(destination)
        assert destination.read_bytes() == archive_bytes, 'Login changed selected archive snapshot'
        expect(anonymous.locator('#auth-dialog')).not_to_be_visible()
        anonymous.goto(url)
        expect(anonymous.locator('#library-view')).to_be_visible()
        assert anonymous.evaluate('document.documentElement.scrollWidth <= innerWidth')

        # Private archives stay owner-only even when filtering by source.
        owner.goto(url)
        inspect_file(owner, archive_file, platform='deepseek', mode='archive')
        private = finish_upload(owner, visibility='private')
        private_path = f'/api/packages/{private["id"]}'
        assert anonymous.request.get(url + private_path).status == 404
        assert anonymous.request.get(url + private_path + '/download').status == 404
        assert private['id'] not in [item['id'] for item in api(anonymous, url, '/api/packages?platform=deepseek')['packages']]
        with owner.expect_download() as download:
            owner.locator('#download-package').click()
        destination = root / 'private.contextpack'
        download.value.save_as(destination)
        parsed = read_package(destination.read_bytes())
        assert parsed['conversations'][0]['messages'][0]['content'].encode('utf-8') == archive_bytes

        # Cached private text cannot be downloaded under another valid account.
        session = api(owner, url, '/api/session')
        result = owner.request.post(url + '/api/register', data={
            'username': 'archiveotheraccount', 'password': 'synthetic-password-123',
        }, headers={'X-CSRF-Token': session['csrf_token']})
        assert result.ok, result.text()
        downloads = []
        owner.on('download', lambda download: downloads.append(download))
        owner.locator('#save-archive').click()
        expect(owner.locator('#status')).to_have_attribute('role', 'alert')
        assert not downloads, 'Stale private archive exported after account change'

        race_page = new_page(browser, errors)
        race_page.goto(url)
        register(race_page, 'importraceowner')
        verify_inspection_races(race_page, generic)
        return {'claude': first['platforms'], 'deepseek': second['platforms'], 'chatgpt': third['platforms']}


def verify_inspection_races(page, generic):
    pending = []

    def hold_first(route):
        parameters = parse_qs(urlsplit(route.request.url).query)
        if parameters.get('platform') == ['claude']:
            # Fetch actual server response, then delay only its browser delivery.
            pending.append((route, route.fetch()))
        else:
            route.continue_()

    page.route('**/api/inspect?**', hold_first)
    page.locator('#open-upload').click()
    page.locator('#upload-platform').select_option('claude')
    page.locator('#upload-file').set_input_files(generic)
    page.wait_for_function('() => document.querySelector("#upload-file-info").textContent.includes("正在检查")')
    page.locator('#upload-platform').select_option('deepseek')
    expect(page.locator('#upload-file-info')).to_contain_text('格式检查通过')
    expect(page.locator('#upload-preview .platform-badge')).to_have_text('DeepSeek')
    assert pending
    for route, response in pending:
        route.fulfill(response=response)
    page.wait_for_function('() => state.uploadSnapshot !== null')
    expect(page.locator('#upload-preview .platform-badge')).to_have_text('DeepSeek')
    page.unroute('**/api/inspect?**', hold_first)

    page.locator('#upload-mode').select_option('archive')
    expect(page.locator('#upload-confirm')).not_to_be_checked()
    expect(page.locator('#upload-file-info')).to_contain_text('格式检查通过')
    expect(page.locator('#upload-submit')).to_be_disabled()
    page.locator('#upload-confirm').check()
    expect(page.locator('#upload-submit')).to_be_enabled()
    # Changing either source or file invalidates confirmation immediately.
    page.locator('#upload-platform').select_option('other')
    expect(page.locator('#upload-confirm')).not_to_be_checked()
    expect(page.locator('#upload-file-info')).to_contain_text('格式检查通过')
    expect(page.locator('#upload-submit')).to_be_disabled()
    page.locator('#upload-confirm').check()
    replacement = generic.with_name('replacement.json')
    replacement.write_bytes(generic.read_bytes())
    page.locator('#upload-file').set_input_files(replacement)
    expect(page.locator('#upload-confirm')).not_to_be_checked()
    expect(page.locator('#upload-file-info')).to_contain_text('格式检查通过')
    final = finish_upload(page, visibility='private')
    assert final['platforms'] == ['other']
    assert final['conversations'][0]['source']['kind'] == 'source_archive'


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory(prefix='contexthub-multiplatform-') as temp, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            categories = verify_imports(browser, Path(temp), errors)
        finally:
            browser.close()
    assert not errors, errors
    report = {
        'native_formats_classified': categories,
        'filter_and_private_isolation': True,
        'historical_source_labels_match_selected_version': True,
        'normalized_conversation_json_export': True,
        'source_record_copy_auth_fixed_historical_snapshot': True,
        'known_import_warnings_translated': True,
        'omission_confirmation_required': True,
        'unknown_format_no_silent_fallback': True,
        'explicit_archive_utf8_bom_crlf_exact': True,
        'archive_download_auth_fixed_historical_snapshot': True,
        'cached_private_archive_acl_rechecked': True,
        'parameter_and_file_changes_invalidate_preview_confirmation': True,
        'late_inspection_response_ignored': True,
        'mobile_no_horizontal_overflow': True,
        'javascript_errors': errors,
        'real_conversations_accessed': False,
    }
    (artifacts / 'multiplatform-web-verification.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
