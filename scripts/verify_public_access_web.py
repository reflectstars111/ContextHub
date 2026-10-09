#!/usr/bin/env python3
"""Verify public reading and authenticated exports with synthetic browser fixtures.

Requires optional Playwright, available in the environment or through PYTHONPATH.
All Hub storage is temporary. Codex and the clipboard sink are synthetic fixtures;
this script never reads private Codex sessions or the operating-system clipboard.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from playwright.sync_api import expect, sync_playwright

from contexthub.package import build_package, read_package
from scripts.verify_versions_web import api
from scripts.verify_web import CONTENT, CONVERSATION, register, service


PASSWORD = 'synthetic-password-123'


def new_page(browser, errors):
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    return page


def fill_registration(page, name):
    expect(page.locator('#auth-dialog')).to_be_visible()
    page.locator('#register-tab').click()
    page.locator('#username').fill(name)
    page.locator('#password').fill(PASSWORD)


def register_for_action(page, name):
    fill_registration(page, name)
    page.locator('#auth-submit').click()


def add_package(page, url, title, visibility='public'):
    blob = build_package([CONVERSATION], title)
    csrf = api(page, url, '/api/session')['csrf_token']
    response = page.request.post(
        f'{url}/api/packages?visibility={visibility}',
        data=blob,
        headers={'X-CSRF-Token': csrf, 'Content-Type': 'application/zip'},
    )
    assert response.ok, (response.status, response.text())
    return response.json(), blob


def install_clipboard_sink(page):
    # Observe the gated text passed to the clipboard adapter without touching
    # the host clipboard or depending on browser clipboard permissions.
    page.evaluate('''() => {
        window.copyText = async (text) => { window.copiedExport = text; };
    }''')


def verify_hub_exports(browser, root, errors):
    with service(root / 'hub') as (hub, url):
        hub.max_body_bytes = 16 * 1024 * 1024
        owner = new_page(browser, errors)
        owner.goto(url)
        register(owner, 'downloadowner')
        source, original = add_package(owner, url, '固定下载原始库')
        other, _ = add_package(owner, url, '换页后不同库')
        private, private_bytes = add_package(owner, url, '私有所有者库', 'private')

        conversations = copy.deepcopy(source['conversations'])
        conversations[0]['messages'][0]['content'] = '新版内容'
        updated = api(owner, url, f'/api/packages/{source["id"]}/versions', {
            'base_version': source['version']['id'],
            'title': source['title'],
            'description': 'v2',
            'message': '新版',
            'conversations': conversations,
        })
        source_link = (
            f'{url}?package={source["id"]}&version={source["version"]["id"]}'
        )

        for format_name in ('package', 'json', 'markdown', 'copy'):
            page = new_page(browser, errors)
            page.goto(source_link)
            expect(page.locator('#messages')).to_contain_text(CONTENT)
            public_path = f'{url}/api/packages/{source["id"]}'
            assert page.request.get(public_path).status == 200
            assert page.request.get(public_path + '/download').status == 401
            assert page.request.get(
                public_path + '/download?version=' + source['version']['id']
            ).status == 401
            assert page.request.get(
                f'{url}/api/packages/{private["id"]}'
            ).status == 404

            if format_name == 'package':
                page.locator('#download-package').click()
            else:
                page.locator('#copy-format').select_option(
                    'json' if format_name == 'json' else 'markdown'
                )
                if format_name == 'copy':
                    install_clipboard_sink(page)
                    page.locator('#copy-conversation').click()
                    assert page.evaluate('window.copiedExport === undefined')
                else:
                    page.locator('#save-conversation').click()
            expect(page.locator('#auth-dialog')).to_be_visible()
            expect(page.locator('#auth-description')).to_contain_text('登录后')

            # An asynchronous reader change must not change the chosen export.
            page.evaluate('(id) => loadPackage(id)', other['id'])
            expect(page.locator('#reader-title')).to_have_text(other['title'])
            if format_name == 'copy':
                register_for_action(page, 'download_' + format_name)
                page.wait_for_function('() => Boolean(window.copiedExport)')
                assert CONTENT in page.evaluate('window.copiedExport')
            else:
                with page.expect_download() as info:
                    register_for_action(page, 'download_' + format_name)
                exported = root / (format_name + '.out')
                info.value.save_as(exported)
                if format_name == 'package':
                    assert exported.read_bytes() == original
                elif format_name == 'json':
                    parsed = json.loads(exported.read_text())
                    assert parsed['messages'][0]['content'] == CONTENT
                else:
                    assert CONTENT in exported.read_text()
            expect(page.locator('#auth-dialog')).not_to_be_visible()
            assert page.request.get(
                f'{url}/api/packages/{private["id"]}'
            ).status == 404
            assert page.request.get(
                f'{url}/api/packages/{private["id"]}/download'
            ).status == 404

        owner.goto(url + '?package=' + private['id'])
        expect(owner.locator('#reader-title')).to_have_text(private['title'])
        with owner.expect_download() as info:
            owner.locator('#download-package').click()
        private_export = root / 'private.out'
        info.value.save_as(private_export)
        assert private_export.read_bytes() == private_bytes

        # The configured limit controls both the displayed label and rejection.
        owner.locator('#reader-back').click()
        owner.locator('#open-upload').click()
        expect(owner.locator('#upload-dialog [data-upload-limit]')).to_have_text(
            '当前服务单个对话包上限：16 MiB。'
        )
        oversized = root / 'large.contextpack'
        with oversized.open('wb') as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
        owner.locator('#upload-file').set_input_files(oversized)
        expect(owner.locator('#upload-error')).to_contain_text('16 MiB')
        expect(owner.locator('#upload-submit')).to_be_disabled()
        owner.keyboard.press('Escape')

        forker = new_page(browser, errors)
        forker.goto(source_link)
        forker.locator('#fork-package').click()
        expect(forker.locator('#auth-dialog')).to_be_visible()
        forker.evaluate('(id) => loadPackage(id)', other['id'])
        register_for_action(forker, 'download_fork')
        expect(forker.locator('#fork-dialog')).to_be_visible()
        expect(forker.locator('#fork-description')).to_contain_text('固定下载原始库')
        expect(forker.locator('#fork-description')).to_contain_text('v1')
        forker.locator('#fork-submit').click()
        expect(forker.locator('#fork-dialog')).not_to_be_visible()
        forks = api(forker, url, '/api/packages')['packages']
        assert any(
            item['upstream']['version_id'] == source['version']['id']
            for item in forks if item.get('upstream')
        )

        fork = api(owner, url, f'/api/packages/{source["id"]}/fork', {
            'version_id': updated['version']['id'], 'visibility': 'public',
        })
        conversations = copy.deepcopy(fork['conversations'])
        conversations[0]['messages'][0]['content'] = '候选内容'
        fork_version = api(owner, url, f'/api/packages/{fork["id"]}/versions', {
            'base_version': fork['version']['id'],
            'title': fork['title'],
            'description': fork['description'],
            'message': '贡献',
            'conversations': conversations,
        })
        proposal = api(owner, url, f'/api/packages/{fork["id"]}/contributions', {
            'source_version_id': fork_version['version']['id'],
            'title': '下载候选验收',
            'description': '',
        })
        checked = api(owner, url, f'/api/contributions/{proposal["id"]}/check', {
            'expected_target_version_id': updated['version']['id'],
        })
        assert checked['check']['status'] == 'ready'
        candidate = new_page(browser, errors)
        candidate.goto(url + '?contribution=' + proposal['id'])
        candidate_path = (
            f'{url}/api/contributions/{proposal["id"]}/candidate'
            f'?check_id={checked["check"]["id"]}'
        )
        assert candidate.request.get(candidate_path).status == 401
        candidate.locator('#contribution-download-candidate').click()
        expect(candidate.locator('#auth-dialog')).to_be_visible()
        with candidate.expect_download() as info:
            register_for_action(candidate, 'download_candidate')
        candidate_export = root / 'candidate.out'
        info.value.save_as(candidate_export)
        parsed = read_package(candidate_export.read_bytes())
        assert parsed['conversations'][0]['messages'][0]['content'] == '候选内容'


def verify_auth_races(browser, root, errors):
    with service(root / 'auth-races') as (_, url):
        owner = new_page(browser, errors)
        owner.goto(url)
        register(owner, 'race_owner')
        first, _ = add_package(owner, url, '原下载A')
        second, second_bytes = add_package(owner, url, '新下载B')
        for outcome in ('success', 'failure'):
            page = new_page(browser, errors)
            downloads = []
            page.on('download', lambda download: downloads.append(download))
            page.goto(url + '?package=' + first['id'])
            page.locator('#download-package').click()
            pending = []
            page.route('**/api/register', lambda route: pending.append(route))
            register_for_action(page, 'old_' + outcome)
            expect(page.locator('#auth-submit')).to_be_disabled()
            page.keyboard.press('Escape')
            page.evaluate('(id) => loadPackage(id)', second['id'])
            page.locator('#download-package').click()
            fill_registration(page, 'new_' + outcome)
            if outcome == 'success':
                page.locator('#auth-submit').click()
                expect(page.locator('#auth-submit')).to_be_disabled()
            assert pending
            with page.expect_response(
                lambda response: response.url.endswith('/api/register')
            ):
                if outcome == 'success':
                    pending.pop(0).continue_()
                else:
                    pending.pop(0).fulfill(
                        status=422, content_type='application/json',
                        body='{"error":"old auth failure"}',
                    )
            page.wait_for_timeout(100)
            assert not downloads
            expect(page.locator('#auth-dialog')).to_be_visible()
            expect(page.locator('#username')).to_have_value('new_' + outcome)
            expect(page.locator('#auth-error')).not_to_be_visible()
            expect(page.locator('#auth-description')).to_contain_text('新下载B')
            if outcome == 'success':
                expect(page.locator('#auth-submit')).to_be_disabled()
                assert len(pending) == 1
                with page.expect_download() as info:
                    pending.pop().continue_()
            else:
                expect(page.locator('#auth-submit')).to_be_enabled()
                page.unroute('**/api/register')
                with page.expect_download() as info:
                    page.locator('#auth-submit').click()
            exported = root / (outcome + '.contextpack')
            info.value.save_as(exported)
            assert exported.read_bytes() == second_bytes
            assert len(downloads) == 1


def verify_connector_exports(browser, root, errors):
    fixture = {
        'id': 'fixture', 'name': '本地合成会话',
        'updatedAt': 1791500000, 'source': 'cli',
    }
    with (
        patch('contexthub.codex.CodexClient'),
        patch('contexthub.codex.list_sessions', return_value=[fixture]),
        patch('contexthub.codex.export_conversation', return_value=CONVERSATION),
        service(root / 'connector', mode='connector') as (_, url),
    ):
        local = new_page(browser, errors)
        local.goto(url + '/#token=browser-fixture-token')
        expect(local.locator('#session-rows')).to_contain_text('本地合成会话')
        local.locator('#select-all').check()
        local.locator('#preview-selected').click()
        expect(local.locator('#messages')).to_contain_text(CONTENT)
        with local.expect_download() as info:
            local.locator('#download-package').click()
        exported = root / 'local.out'
        info.value.save_as(exported)
        assert read_package(exported.read_bytes())['conversations']
        with local.expect_download() as info:
            local.locator('#save-conversation').click()
        assert info.value.suggested_filename.endswith('.md')
        install_clipboard_sink(local)
        local.locator('#copy-conversation').click()
        local.wait_for_function('() => Boolean(window.copiedExport)')
        expect(local.locator('#auth-dialog')).not_to_be_visible()


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with (
        tempfile.TemporaryDirectory(prefix='contexthub-public-access-') as temp,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            root = Path(temp)
            verify_hub_exports(browser, root, errors)
            verify_auth_races(browser, root, errors)
            verify_connector_exports(browser, root, errors)
        finally:
            browser.close()
    assert not errors, errors
    result = {
        'anonymous_public_read_200': True,
        'anonymous_raw_download_401': True,
        'anonymous_raw_candidate_download_401': True,
        'historical_download_snapshot': True,
        'single_json_markdown_copy_login': True,
        'fork_fixed_version_login': True,
        'candidate_login': True,
        'owner_private_only': True,
        'configured_16MiB_limit': True,
        'canceled_auth_success_no_wrong_download': True,
        'canceled_auth_failure_preserves_new_dialog': True,
        'old_finally_preserves_new_pending_button': True,
        'connector_exports_do_not_require_hub_login': True,
        'clipboard_sink': 'synthetic in-page adapter; OS clipboard not accessed',
        'javascript_errors': errors,
        'real_codex_data_access': False,
    }
    output = artifacts / 'public-access-web-verification.json'
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
