#!/usr/bin/env python3
"""Web-to-local Codex handoff acceptance with temporary, synthetic data.

Requires optional Playwright. Never reads or writes a real Codex session store.
Copied Bash commands run only against a fixture executable that records argv.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from contexthub.package import build_package
from scripts.verify_reader_navigation_web import CONVERSATION_ID, MESSAGE_ID, fixture, option_value
from scripts.verify_versions_web import preview, save, version_ids
from scripts.verify_web import register, service
from playwright.sync_api import expect, sync_playwright


def command_arguments(page):
    text = page.locator('#codex-open-command').text_content()
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    assert len(lines) == 1, text
    arguments = shlex.split(lines[0])
    assert arguments[:2] == ['ctx', 'open'], arguments
    return arguments


def assert_command(page, version, *, target='./contexthub-project', mode='context', message=None, snapshot=None):
    arguments = command_arguments(page)
    assert parse_qs(urlsplit(arguments[2]).query)['version'] == [version], arguments
    assert option_value(arguments, '--to') == target, arguments
    assert option_value(arguments, '--mode') == mode, arguments
    assert option_value(arguments, '--conversation') == CONVERSATION_ID, arguments
    assert '--codex' in arguments, arguments
    assert option_value(arguments, '--message') == message, arguments
    assert option_value(arguments, '--snapshot') == snapshot, arguments
    assert 'synthetic-password-123' not in page.locator('#codex-open-dialog').text_content()
    assert 'browser-fixture-token' not in page.locator('#codex-open-dialog').text_content()
    return arguments


def execute_fixture_command(command, root):
    """Prove pasted Bash treats target punctuation as inert argument data."""
    work = root / 'copied-command'
    work.mkdir(exist_ok=True)
    binaries = work / 'bin'
    binaries.mkdir(exist_ok=True)
    capture = work / 'arguments.json'
    executable = binaries / 'ctx'
    executable.write_text('#!' + sys.executable + '\nimport json, os, sys\n'
                          'with open(os.environ["CONTEXTHUB_FIXTURE_ARGV"], "w") as stream:\n'
                          '    json.dump(sys.argv[1:], stream)\n')
    executable.chmod(0o755)
    environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ.get('PATH', ''),
                       CONTEXTHUB_FIXTURE_ARGV=str(capture))
    result = subprocess.run(['bash', '--noprofile', '--norc', '-c', command], cwd=work,
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert not (work / 'unexpected-command-execution').exists()
    return ['ctx', *json.loads(capture.read_text())]


def close_dialog(page):
    page.keyboard.press('Escape')
    expect(page.locator('#codex-open-dialog')).not_to_be_visible()


def assert_login_command(page, hub, username):
    arguments = shlex.split(page.locator('#codex-open-login-command').text_content())
    assert arguments[:3] == ['ctx', 'auth', 'login'], arguments
    assert option_value(arguments, '--hub') == hub, arguments
    assert option_value(arguments, '--username') == username, arguments
    assert all('password' not in item and 'token' not in item for item in arguments), arguments


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    conversations, older, newer = fixture()
    errors = []
    with tempfile.TemporaryDirectory(prefix='contexthub-codex-open-web-') as temp, sync_playwright() as playwright:
        root = Path(temp)
        pack = root / 'codex-open.contextpack'
        pack.write_bytes(build_package(conversations, '网页继续本地开发验收', code=newer))
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        with service(root / 'hub') as (_, url):
            context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                          permissions=['clipboard-read', 'clipboard-write'])
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            register(page, 'codexopenfixture')
            page.locator('#open-upload').click()
            page.locator('#upload-file').set_input_files(str(pack))
            page.locator('input[name="visibility"][value="public"]').check()
            expect(page.locator('#upload-submit')).to_be_enabled()
            page.locator('#upload-submit').click()
            expect(page.locator('#reader-view')).to_be_visible()
            ident, version = version_ids(page)
            published = page.url

            # Default choice continues the selected, recorded turn, not all chats.
            page.locator('#repository-codex-action').click()
            expect(page.locator('#codex-open-dialog')).to_be_visible()
            expect(page.locator('#codex-open-target')).to_have_value('./contexthub-project')
            expect(page.locator('#codex-open-mode')).to_have_value('context')
            expect(page.locator('#codex-open-shell')).to_have_value('bash')
            assert_command(page, version, message='m3a')
            assert_login_command(page, url, 'codexopenfixture')
            expect(page.locator('#codex-open-conversation')).to_have_value(CONVERSATION_ID)
            expect(page.locator('#codex-open-conversation')).to_be_disabled()
            close_dialog(page)

            # Unrecorded history cannot silently acquire the newest code snapshot.
            page.get_by_text('← 上一轮', exact=True).click()
            expect(page.locator('#repository-codex-action')).to_be_disabled()
            assert page.locator('#repository-codex-action').get_attribute('title')
            page.get_by_text('← 上一轮', exact=True).click()
            page.locator('#repository-codex-action').click()
            assert_command(page, version, message=MESSAGE_ID)
            page.locator('#copy-codex-open-command').click()
            assert page.evaluate('navigator.clipboard.readText()') == page.locator('#codex-open-command').text_content()

            # Both quoting modes must preserve spaces, single quotes and leading '-'.
            target = "-work space's $(touch unexpected-command-execution)"
            page.locator('#codex-open-target').fill(target)
            arguments = assert_command(page, version, target=target, message=MESSAGE_ID)
            assert execute_fixture_command(page.locator('#codex-open-command').text_content(), root) == arguments
            page.locator('#codex-open-mode').select_option('timeline')
            assert_command(page, version, target=target, mode='timeline', message=MESSAGE_ID)
            page.locator('#codex-open-shell').select_option('powershell')
            windows_command = page.locator('#codex-open-command').text_content()
            assert "--to='" + target.replace("'", "''") + "'" in windows_command, windows_command
            assert '--mode timeline' in windows_command or '--mode=timeline' in windows_command, windows_command
            assert 'codexopenfixture' in page.locator('#codex-open-dialog').text_content()
            page.locator('#copy-codex-open-command').click()
            assert page.evaluate('navigator.clipboard.readText()') == windows_command
            page.locator('#codex-open-shell').select_option('bash')
            page.locator('#codex-open-mode').select_option('context')
            page.locator('#codex-open-target').fill('./contexthub-project')
            page.locator('#codex-open-target').fill('')
            expect(page.locator('#copy-codex-open-command')).to_be_disabled()
            expect(page.locator('#codex-open-error')).to_be_visible()
            assert not page.locator('#codex-open-command').text_content().strip()
            page.locator('#codex-open-target').fill('./contexthub-project')
            close_dialog(page)

            # A directly chosen code snapshot is explicit and drops message binding.
            page.locator('.message-code-link button').click()
            page.locator('#code-snapshot-select').select_option(newer['head'])
            page.locator('#repository-codex-action').click()
            assert_command(page, version, snapshot=newer['head'])
            expect(page.locator('#codex-open-conversation')).to_be_enabled()
            page.locator('#codex-open-conversation').select_option('long')
            assert option_value(command_arguments(page), '--conversation') == 'long'
            assert option_value(command_arguments(page), '--snapshot') == newer['head']
            assert option_value(command_arguments(page), '--message') is None
            page.locator('#codex-open-conversation').select_option(CONVERSATION_ID)
            close_dialog(page)

            # Unpublished drafts cannot generate commands for the older published text.
            page.locator('[data-repository-section="conversation-section"]').click()
            page.locator('#edit-package').click()
            expect(page.locator('#draft-panel')).to_be_visible()
            expect(page.locator('#repository-codex-action')).to_be_disabled()
            assert page.locator('#repository-codex-action').get_attribute('title')
            page.locator('#messages .message').first.get_by_text('编辑', exact=True).click()
            page.locator('#messages textarea').fill('只用于历史版本固定测试的后续修改')
            page.get_by_text('保存修改', exact=True).click()
            preview(page, '创建后续版本，验收固定版本导入')
            save(page, 2)
            assert version_ids(page)[1] != version
            page.goto(published)
            expect(page.locator('#version-position')).to_have_text('历史版本')
            page.locator('#repository-codex-action').click()
            assert_command(page, version, message='m3a')

            # Modal remains usable even on narrow terminals' companion browsers.
            page.screenshot(path=str(artifacts / 'codex-open-desktop.png'))
            for width in (320, 390, 768):
                page.set_viewport_size({'width': width, 'height': 844})
                overflow = page.evaluate('''() => ({width: innerWidth, document: document.documentElement.scrollWidth,
                    elements: Array.from(document.querySelectorAll('body *')).map(element => ({
                      tag: element.tagName, id: element.id, class: element.className,
                      right: element.getBoundingClientRect().right, left: element.getBoundingClientRect().left
                    })).filter(element => element.right > innerWidth + 1 || element.left < -1).slice(0, 20)})''')
                assert overflow['document'] <= width, overflow
                bounds = page.locator('#codex-open-dialog').bounding_box()
                assert bounds and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1, bounds
                page.locator('#copy-codex-open-command').scroll_into_view_if_needed()
                expect(page.locator('#copy-codex-open-command')).to_be_visible()
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('#codex-open-dialog').evaluate('(dialog) => { dialog.scrollTop = 0; }')
            page.screenshot(path=str(artifacts / 'codex-open-mobile.png'))
            page.locator('#copy-codex-open-command').scroll_into_view_if_needed()
            page.screenshot(path=str(artifacts / 'codex-open-mobile-command.png'))
            context.close()

            # Public readers may copy instructions, but the command still requires
            # terminal authentication; opening the dialog never exports the pack.
            anonymous = browser.new_context()
            page = anonymous.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            downloads = []
            page.on('request', lambda request: downloads.append(request.url) if '/download' in request.url else None)
            page.goto(published)
            page.locator('#repository-codex-action').click()
            expect(page.locator('#codex-open-dialog')).to_be_visible()
            expect(page.locator('#auth-dialog')).not_to_be_visible()
            assert_command(page, version, message='m3a')
            assert_login_command(page, url, 'YOUR_USERNAME')
            assert not downloads, downloads
            close_dialog(page)
            page.locator('#reader-back').click()
            expect(page.locator('#library-view')).to_be_visible()
            page.locator('#package-grid .card-title').first.click()
            expect(page.locator('#reader-view')).to_be_visible()
            page.locator('#repository-codex-action').click()
            expect(page.locator('#codex-open-dialog')).to_be_visible()
            page.go_back()
            expect(page.locator('#library-view')).to_be_visible()
            expect(page.locator('#codex-open-dialog')).not_to_be_visible()
            anonymous.close()
        browser.close()
    assert not errors, errors
    result = {'pinned_historical_version': True, 'selected_turn_and_snapshot': True,
              'one_command_clipboard': True, 'bash_quoting_executed_with_synthetic_stub': True,
              'powershell_literal_quoting': True, 'windows_command_executed': False,
              'draft_and_unbound_turn_guards': True, 'no_credentials_in_snippets': True,
              'anonymous_instructions_without_download': True, 'empty_target_disabled': True,
              'browser_back_closes_stale_dialog': True,
              'mobile_dialog_no_overflow': True, 'javascript_errors': errors,
              'real_codex_data_access': False}
    (artifacts / 'codex-open-web-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
