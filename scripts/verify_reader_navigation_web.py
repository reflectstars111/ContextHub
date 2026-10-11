#!/usr/bin/env python3
"""Reader permalinks and selected-code restore acceptance using synthetic data.

Requires optional Playwright. Runs a temporary real Hub; never reads Codex data,
uses real accounts, or contacts a public service.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlencode, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from contexthub.package import build_package
from scripts.verify_code_web import upload
from scripts.verify_versions_web import preview, save, version_ids
from scripts.verify_web import register, service
from tests.test_code_repository import repository
from playwright.sync_api import expect, sync_playwright


FILE_PATH = 'docs/设计 note.txt'
CONVERSATION_ID = '-conversation'
MESSAGE_ID = '-message'


def params(page):
    return {key: values[0] for key, values in parse_qs(urlsplit(page.url).query).items()}


def expect_params(page, /, **expected):
    page.wait_for_function('''expected => {
      const query = new URL(location.href).searchParams;
      return Object.entries(expected).every(([key, value]) => query.get(key) === value);
    }''', arg=expected)


def navigate_once(page, callback, /, **expected):
    """One reader action must produce one useful back-button destination."""
    previous = page.evaluate('window.navigationPushes')
    callback()
    expect_params(page, **expected)
    assert page.evaluate('window.navigationPushes') == previous + 1, (page.url, previous)


def command_arguments(page):
    lines = page.locator('#checkpoint-restore-command').text_content().splitlines()
    commands = [shlex.split(line) for line in lines if line.strip()]
    assert len(commands) == 3, commands
    for command in commands[:-1]:
        assert command[-1] == '&&', commands
        command.pop()
    return commands


def option_value(arguments, name):
    for index, argument in enumerate(arguments):
        if argument == name:
            return arguments[index + 1]
        if argument.startswith(name + '='):
            return argument[len(name) + 1:]
    return None


def assert_restore(page, version, *, snapshot=None, message=None):
    expect(page.locator('#checkpoint-restore-command')).to_contain_text('ctx clone')
    commands = command_arguments(page)
    clones = [command for command in commands if command[:2] == ['ctx', 'clone']]
    assert clones, {'commands': commands, 'url': page.url, 'reader': page.evaluate('({kind: state.reader.kind, tab: document.querySelector("#reader-view").dataset.repositoryTab, plan: repositoryRestorePlan()})')}
    clone = clones[0]
    restore = next(command for command in commands if command[:2] == ['ctx', 'restore'])
    assert parse_qs(urlsplit(clone[2]).query)['version'] == [version], clone
    assert restore[2] == 'HEAD', restore
    if message:
        assert option_value(restore, '--message') == message, restore
        assert option_value(restore, '--conversation') == CONVERSATION_ID, restore
        assert option_value(restore, '--snapshot') is None, restore
    if snapshot:
        assert option_value(restore, '--snapshot') == snapshot, restore
        assert option_value(restore, '--message') is None, restore


def unavailable(locator):
    assert not locator.is_visible() or not locator.is_enabled()


def assert_failed_clone_stops_restore(command, root):
    """Exercise the actual copied Bash chain with a safe, always-failing ctx."""
    work = root / 'failed-clone-fixture'
    (work / 'conversation-workspace').mkdir(parents=True)
    binaries = work / 'bin'
    binaries.mkdir()
    calls = work / 'calls.txt'
    stub = binaries / 'ctx'
    stub.write_text('''#!/bin/sh
printf '%s\\n' "$1" >> "$CONTEXTHUB_FIXTURE_CALLS"
if [ "$1" = clone ]; then exit 31; fi
if [ "$1" = restore ]; then touch "$CONTEXTHUB_FIXTURE_RESTORED"; exit 0; fi
exit 2
''')
    stub.chmod(0o755)
    restored = work / 'unexpected-restore'
    environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ.get('PATH', ''),
                       CONTEXTHUB_FIXTURE_CALLS=str(calls), CONTEXTHUB_FIXTURE_RESTORED=str(restored))
    result = subprocess.run(['bash', '--noprofile', '--norc', '-c', command], cwd=work,
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 31, (result.returncode, result.stdout, result.stderr)
    assert calls.read_text().splitlines() == ['clone']
    assert not restored.exists(), 'Failed clone still invoked restore in an existing unrelated workspace'


def execute_restore_commands(commands, root, hub):
    """Execute copied argv safely, with only the temporary synthetic Hub account."""
    work = root / 'terminal-fixture'
    work.mkdir()
    environment = dict(os.environ, CONTEXTHUB_CONFIG_DIR=str(root / 'cli-config'), CONTEXTHUB_HUB=hub)

    def run(arguments, stdin=None):
        result = subprocess.run([sys.executable, str(PROJECT / 'ctx'), *arguments], cwd=work,
                                env=environment, input=stdin, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (arguments, result.stdout, result.stderr)

    run(['auth', 'login', '--hub', hub, '--username', 'navigationfixture', '--password-stdin'],
        stdin='synthetic-password-123\n')
    for command in commands:
        if command[0] == 'cd':
            assert len(command) == 2
            work = (work / command[1]).resolve()
            assert work.is_relative_to(root)
        else:
            assert command[:2] in (['ctx', 'clone'], ['ctx', 'restore']), command
            run(command[1:])
    assert (work / 'NEWDIR/code/app.py').read_bytes() == b'print("first")\n'
    assert (work / 'NEWDIR/code' / FILE_PATH).read_bytes() == b'older selected file\n'
    assert (work / 'NEWDIR/receipt.json').is_file()


def fixture():
    messages = [
        {'id': MESSAGE_ID if number == 1 and suffix == 'a' else f'm{number}{suffix}', 'role': role,
         'content': f'第{number}轮 {role} 合成内容',
         'provenance': {'turn_id': f'turn-{number}',
                        'created_at': f'2026-10-0{number + 5}T08:00:00Z'}}
        for number in (1, 2, 3)
        for role, suffix in (('user', 'u'), ('assistant', 'a'))
    ]
    conversations = [
        {'id': CONVERSATION_ID, 'title': '有历史代码的开发记录', 'messages': messages},
        {'id': 'long', 'title': '较长轮次', 'messages': [
            {'id': f'long-{i}', 'role': 'user' if i == 0 else 'assistant',
             'content': f'同轮消息 {i}', 'provenance': {'turn_id': 'long'}}
            for i in range(25)
        ]},
    ]
    older = repository({'app.py': b'print("first")\n', FILE_PATH: b'older selected file\n'},
                       bindings=[{'conversation_id': CONVERSATION_ID, 'message_id': MESSAGE_ID, 'kind': 'captured'}])
    newer = repository({'app.py': b'print("second")\n', FILE_PATH: b'newer selected file\n'},
                       previous=older, time='2026-10-11T08:00:00Z')
    newer['bindings'].append({'conversation_id': CONVERSATION_ID, 'message_id': 'm3a',
                              'snapshot_id': newer['head'], 'kind': 'captured'})
    return conversations, older, newer


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    conversations, older, newer = fixture()
    errors = []
    with tempfile.TemporaryDirectory(prefix='contexthub-navigation-web-') as temp, sync_playwright() as playwright:
        root = Path(temp)
        pack = root / 'navigation.contextpack'
        pack.write_bytes(build_package(conversations, '可分享的对话与代码位置', code=newer))
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        with service(root / 'hub') as (_, url):
            context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                          permissions=['clipboard-read', 'clipboard-write'])
            context.add_init_script('''window.navigationPushes = 0;
              const push = history.pushState.bind(history);
              history.pushState = (...args) => { window.navigationPushes++; return push(...args); };
            ''')
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            register(page, 'navigationfixture')
            upload(page, pack)
            ident, version = version_ids(page)
            expect_params(page, conversation=CONVERSATION_ID, turn='m3u')
            navigate_once(page, lambda: page.locator('#conversation-tabs button').nth(1).click(),
                          conversation='long', turn='long-0')
            expect(page.locator('#messages .message')).to_have_count(20)
            navigate_once(page, lambda: page.get_by_text('较新消息', exact=True).click(), page='2')
            expect(page.locator('#messages .message')).to_have_count(5)
            second_page_link = page.url
            history = page.evaluate('history.length')
            page.reload()
            expect(page.locator('#messages')).to_contain_text('同轮消息 24')
            expect(page.locator('#messages .message')).to_have_count(5)
            assert page.url == second_page_link
            assert page.evaluate('history.length') == history
            page.locator('#copy-location-link').click()
            copied = page.evaluate('navigator.clipboard.readText()')
            assert copied == second_page_link, copied
            copied_page = context.new_page()
            copied_page.on('pageerror', lambda error: errors.append(str(error)))
            copied_page.goto(copied)
            expect(copied_page.locator('#conversation-title')).to_have_text('较长轮次')
            expect(copied_page.locator('#messages .message')).to_have_count(5)
            copied_page.close()
            page.go_back()
            expect_params(page, conversation='long', page=None)
            expect(page.locator('#messages .message')).to_have_count(20)
            page.go_forward()
            expect_params(page, conversation='long', page='2')
            expect(page.locator('#messages .message')).to_have_count(5)

            navigate_once(page, lambda: page.locator('#conversation-tabs button').first.click(),
                          conversation=CONVERSATION_ID, turn='m3u', page=None)
            navigate_once(page, lambda: page.get_by_text('← 上一轮', exact=True).click(), turn='m2u')
            unavailable(page.locator('#repository-restore-action'))
            assert not page.locator('#checkpoint-restore-command').text_content()
            navigate_once(page, lambda: page.get_by_text('← 上一轮', exact=True).click(), turn='m1u')
            navigate_once(page, lambda: page.locator('.message-code-link button').click(),
                          tab='code', snapshot=older['head'], message=MESSAGE_ID, codeview='diff')
            expect(page.locator('#code-snapshot-select')).to_have_value(older['head'])
            expect(page.locator('#code-origin')).to_contain_text('第 1 轮')
            expect(page.locator('#code-origin')).to_contain_text('采集')
            expect(page.locator('#repository-restore-action')).to_have_text('恢复所选代码')
            page.locator('#repository-restore-action').click()
            expect(page.locator('#repository-terminal-guide')).to_have_attribute('open', '')
            assert_restore(page, version, message=MESSAGE_ID)
            page.locator('#copy-restore-command').click()
            assert page.evaluate('navigator.clipboard.readText()') == page.locator('#checkpoint-restore-command').text_content()
            assert_failed_clone_stops_restore(page.locator('#checkpoint-restore-command').text_content(), root)
            execute_restore_commands(command_arguments(page), root, url)
            navigate_once(page, lambda: page.locator('#code-origin').get_by_role('button', name='返回来源消息').click(),
                          tab=None, conversation=CONVERSATION_ID, turn='m1u')
            expect(page.locator('#messages')).to_contain_text('第1轮 assistant')
            expect(page.locator('#messages .message')).to_have_count(2)
            page.go_back()
            expect_params(page, tab='code', snapshot=older['head'], message=MESSAGE_ID)
            expect(page.locator('#code-browser-panel')).to_be_visible()
            assert_restore(page, version, message=MESSAGE_ID)

            navigate_once(page, lambda: page.locator('#code-files-tab').click(), codeview=None)
            navigate_once(page, lambda: page.locator('#code-tree [data-path="docs/"]').click(), path='docs/')
            navigate_once(page, lambda: page.locator('#code-tree').get_by_role('button').filter(has_text='设计 note.txt').click(),
                          path=FILE_PATH)
            expect(page.locator('.code-file-content')).to_have_text('older selected file\n')
            file_link = page.url
            history = page.evaluate('history.length')
            page.reload()
            expect(page.locator('.code-file-content')).to_have_text('older selected file\n')
            expect(page.locator('#code-breadcrumb')).to_contain_text('docs')
            expect(page.locator('#code-origin')).to_contain_text('第 1 轮')
            assert page.url == file_link
            assert page.evaluate('history.length') == history
            assert_restore(page, version, message=MESSAGE_ID)
            page.locator('#copy-location-link').click()
            assert page.evaluate('navigator.clipboard.readText()') == file_link
            copied_page = context.new_page()
            copied_page.goto(file_link)
            expect(copied_page.locator('.code-file-content')).to_have_text('older selected file\n')
            copied_page.close()
            navigate_once(page, lambda: page.locator('#code-snapshot-select').select_option(newer['head']),
                          snapshot=newer['head'], path=None, message=None)
            assert_restore(page, version, snapshot=newer['head'])
            page.go_back()
            expect(page.locator('.code-file-content')).to_have_text('older selected file\n')
            expect_params(page, path=FILE_PATH, snapshot=older['head'])
            page.go_forward()
            expect_params(page, path=None, snapshot=newer['head'])
            expect(page.locator('.code-file-content')).to_have_count(0)

            # A late response from an abandoned file must never replace new state.
            pending = []
            pattern = '**/code/file?*'
            page.route(pattern, lambda route: pending.append(route))
            page.locator('#code-tree [data-path="app.py"]').click()
            page.wait_for_function("document.querySelector('#code-file').textContent.includes('正在')")
            page.locator('#code-snapshot-select').select_option(older['head'])
            assert pending, 'Expected delayed file request'
            with page.expect_response(lambda response: '/code/file?' in response.url):
                pending.pop().continue_()
            page.unroute(pattern)
            expect(page.locator('.code-file-content')).to_have_count(0)
            expect(page.locator('#code-snapshot-select')).to_have_value(older['head'])
            expect_params(page, snapshot=older['head'], path=None)

            # User-controlled selectors are validated against the immutable package.
            requests = []
            page.on('request', lambda request: requests.append(request.url) if '/code/file?' in request.url else None)
            bad_query = dict(package=ident, version=version, conversation='unknown', turn='unknown',
                             page='-7', tab='code', snapshot='f' * 64, path='../../outside.txt',
                             message='unknown', codeview='unknown')
            page.goto(url + '/?' + urlencode(bad_query))
            expect(page.locator('#reader-view')).to_be_visible()
            expect_params(page, conversation=CONVERSATION_ID, turn='m3u', snapshot=newer['head'], path=None,
                          page=None, message=None, codeview=None)
            assert not requests, requests
            assert params(page)['version'] == version

            # A second Hub version makes the same deep link genuinely historical.
            page.locator('[data-repository-section="conversation-section"]').click()
            page.locator('#edit-package').click()
            expect(page.locator('#draft-panel')).to_be_visible()
            unavailable(page.locator('#copy-location-link'))
            unavailable(page.locator('#copy-restore-command'))
            unavailable(page.locator('#repository-restore-action'))
            assert 'ctx clone' not in page.locator('#checkpoint-restore-command').text_content()
            page.locator('#messages .message').first.get_by_text('编辑', exact=True).click()
            page.locator('#messages textarea').fill('尚未保存的行内编辑')
            guarded_link = page.url
            page.locator('[data-repository-section="code-browser-panel"]').click()
            expect(page.locator('#messages textarea')).to_have_value('尚未保存的行内编辑')
            assert page.url == guarded_link
            page.get_by_text('保存修改', exact=True).click()
            preview(page, '添加后续版本以验证历史链接')
            save(page, 2)
            assert version_ids(page)[1] != version
            page.goto(file_link)
            expect(page.locator('#version-position')).to_have_text('历史版本')
            expect(page.locator('.code-file-content')).to_have_text('older selected file\n')
            assert_restore(page, version, message=MESSAGE_ID)
            page.locator('#repository-restore-action').click()
            expect(page.locator('#repository-terminal-guide')).to_have_attribute('open', '')
            page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(artifacts / 'reader-navigation-desktop.png'), full_page=True)
            page.locator('#code-origin').get_by_role('button', name='返回来源消息').click()
            expect(page.locator('#messages')).to_contain_text('第1轮 assistant')
            page.locator('#repository-terminal-guide > summary').click()
            for width in (320, 390, 768):
                page.set_viewport_size({'width': width, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Overflow at {width}px'
            page.set_viewport_size({'width': 390, 'height': 844})
            page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(artifacts / 'reader-navigation-mobile.png'), full_page=True)
            context.close()
        browser.close()
    assert not errors, errors
    result = {'conversation_turn_page_permalink': True, 'file_snapshot_and_origin_permalink': True,
              'browser_back_forward_and_no_duplicate_entries': True,
              'clipboard_location_and_restore_commands': True, 'selected_message_and_snapshot_restore': True,
              'copied_commands_restore_exact_older_bytes': True, 'unbound_turn_does_not_restore_head': True,
              'failed_clone_never_runs_restore': True, 'leading_dash_ids_restore_correctly': True,
              'historical_version_stays_pinned': True, 'invalid_selectors_normalized_before_fetch': True,
              'delayed_file_response_ignored': True, 'unpublished_and_inline_edit_guards': True,
              'mobile_no_overflow': True, 'javascript_errors': errors, 'real_user_data_access': False}
    (artifacts / 'reader-navigation-web-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
