#!/usr/bin/env python3
"""Capture README examples from the real UI using an isolated demo Hub.

Requires optional Playwright + Chromium. Does not access real accounts, Codex
sessions, production storage or the public Hub. Temporary data is removed.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from contexthub.code_repository import snapshot_id
from contexthub.package import build_package, read_package
from scripts.verify_versions_web import preview, save
from scripts.verify_web import register, service
from playwright.sync_api import expect, sync_playwright


def code_snapshot(files, created_at, message_id, previous=None):
    code = copy.deepcopy(previous) if previous else {
        'schema_version': 1, 'snapshots': [], 'blobs': {}, 'bindings': [],
    }
    entries = []
    for path, text in sorted(files.items()):
        data = text.encode('utf-8')
        digest = hashlib.sha256(data).hexdigest()
        code['blobs'][digest] = base64.b64encode(data).decode('ascii')
        entries.append({'path': path, 'sha256': digest, 'size': len(data), 'mode': '100644'})
    snapshot = {'parents': [code['head']] if previous else [], 'created_at': created_at,
                'git_commit': '', 'branch': 'main', 'files': entries}
    snapshot['id'] = snapshot_id(snapshot)
    code['snapshots'].append(snapshot)
    code['head'] = snapshot['id']
    code['bindings'].append({'conversation_id': 'taskflow', 'message_id': message_id,
                             'snapshot_id': snapshot['id'], 'kind': 'captured'})
    return code


def demonstration_package():
    rounds = [
        ('2026-10-09T09:30:00Z', '先做一个待办清单，支持添加和查看任务。',
         '已完成基础功能：添加任务、查看列表。每条任务都有编号，方便后续操作。'),
        ('2026-10-10T09:00:00Z', '加上完成任务的功能，只显示还没完成的任务。',
         '可以通过任务编号标记完成。列表默认只显示未完成项，已完成任务仍保留在数据中。'),
        ('2026-10-11T02:20:00Z', '完善使用说明，处理没有待办任务的情况。',
         '已补充使用示例。所有任务完成后会显示「暂无待办任务」。\n\n'
         '本轮代码已经保存，可从下方查看；也可以在本机 Codex 中继续添加新功能。'),
    ]
    messages = []
    for number, (created_at, user, assistant) in enumerate(rounds, 1):
        for role, suffix, content in [('user', 'u', user), ('assistant', 'a', assistant)]:
            messages.append({'id': f'm{number}{suffix}', 'role': role, 'content': content,
                             'provenance': {'turn_id': f'turn-{number}', 'created_at': created_at,
                                            'platform': 'codex'}})
    conversations = [{'id': 'taskflow', 'title': '待办清单开发记录',
                      'source': {'platform': 'codex'}, 'messages': messages}]
    initial = '''tasks = []


def add_task(title):
    tasks.append({"id": len(tasks) + 1, "title": title, "done": False})


def list_tasks():
    return "\\n".join(f"{task['id']}. {task['title']}" for task in tasks)
'''
    latest = '''tasks = []


def add_task(title):
    tasks.append({"id": len(tasks) + 1, "title": title, "done": False})


def complete_task(task_id):
    for task in tasks:
        if task["id"] == task_id:
            task["done"] = True


def list_tasks():
    pending = [task for task in tasks if not task["done"]]
    if not pending:
        return "暂无待办任务"
    return "\\n".join(f"{task['id']}. {task['title']}" for task in pending)
'''
    first = code_snapshot({'tasks.py': initial, 'README.md': '# TaskFlow\n\n一个简单的待办清单。\n'},
                          rounds[0][0], 'm1a')
    final = code_snapshot({'tasks.py': latest, 'README.md': '# TaskFlow\n\n一个简单的待办清单。\n\n'
                           '```python\nfrom tasks import add_task, complete_task, list_tasks\n'
                           'add_task("整理项目文档")\nprint(list_tasks())\ncomplete_task(1)\n```\n'},
                          rounds[2][0], 'm3a', previous=first)
    return build_package(conversations, 'TaskFlow 待办清单',
                         '从想法到代码，保留每一步开发对话。', code=final)


def next_demonstration_package(original):
    """A small, real follow-up: reject an empty task title and record why."""
    parsed = read_package(original)
    conversations = parsed['conversations']
    created_at = '2026-10-11T03:00:00Z'
    for role, suffix, content in [
        ('user', 'u', '任务标题不能留空，输入空格也要提醒。'),
        ('assistant', 'a', '已去掉标题首尾空格；空标题会提示「请输入任务标题」，不再添加空任务。'),
    ]:
        conversations[0]['messages'].append({
            'id': f'm4{suffix}', 'role': role, 'content': content,
            'provenance': {'turn_id': 'turn-4', 'created_at': created_at, 'platform': 'codex'},
        })
    code = parsed['code']
    current = next(snapshot for snapshot in code['snapshots'] if snapshot['id'] == code['head'])
    files = {entry['path']: base64.b64decode(code['blobs'][entry['sha256']]).decode('utf-8')
             for entry in current['files']}
    files['tasks.py'] = files['tasks.py'].replace(
        'def add_task(title):\n',
        'def add_task(title):\n    title = title.strip()\n'
        '    if not title:\n        raise ValueError("请输入任务标题")\n',
    )
    updated = code_snapshot(files, created_at, 'm4a', previous=code)
    return build_package(conversations, parsed['library']['title'],
                         parsed['library']['description'], code=updated)


def main():
    images = PROJECT / 'docs' / 'images'
    images.mkdir(parents=True, exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory(prefix='contexthub-readme-demo-') as temporary, \
            sync_playwright() as playwright:
        root = Path(temporary)
        package = root / 'taskflow.contextpack'
        package.write_bytes(demonstration_package())
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        with service(root / 'hub') as (_, url):
            context = browser.new_context(viewport={'width': 1440, 'height': 1100},
                                          locale='zh-CN', timezone_id='Asia/Shanghai',
                                          device_scale_factor=1)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            register(page, 'demo')
            page.locator('#open-upload').click()
            page.locator('#upload-file').set_input_files(str(package))
            page.locator('input[name="visibility"][value="public"]').check()
            expect(page.locator('#upload-submit')).to_be_enabled()
            expect(page.locator('#upload-preview')).to_contain_text('2 个代码快照')
            page.set_viewport_size({'width': 1440, 'height': 1600})
            # Focus on the real file selection, visibility and inspection summary.
            # The full dialog is unusually tall; crop the browser capture without
            # changing page content, styling, warnings or confirmation controls.
            dialog = page.locator('#upload-dialog').bounding_box()
            picker = page.locator('#upload-dialog .file-picker').bounding_box()
            summary = page.locator('#upload-preview .inspect-summary').bounding_box()
            assert dialog and picker and summary
            page.screenshot(path=str(images / 'upload-preview.png'), clip={
                'x': dialog['x'], 'y': picker['y'] - 12, 'width': dialog['width'],
                'height': summary['y'] + summary['height'] - picker['y'] + 24,
            })
            page.set_viewport_size({'width': 1440, 'height': 1100})
            page.locator('#upload-submit').click()
            expect(page.locator('#reader-view')).to_be_visible()
            expect(page.locator('#messages')).to_contain_text('暂无待办任务')
            expect(page.locator('#repository-codex-action')).to_be_enabled()
            for group in page.locator('#turn-directory details').all():
                if group.get_attribute('open') is None:
                    group.locator('summary').click()
            page.evaluate('document.fonts.ready')
            page.locator('#reader-view').screenshot(path=str(images / 'conversation.png'))

            page.locator('.message-code-link button').click()
            expect(page.locator('#code-origin')).to_contain_text('第 3 轮')
            page.locator('#code-files-tab').click()
            page.locator('#code-tree [data-path="tasks.py"]').click()
            expect(page.locator('#code-file')).to_contain_text('暂无待办任务')
            page.locator('#code-browser-panel').screenshot(path=str(images / 'code.png'))

            page.locator('#repository-codex-action').click()
            page.locator('#codex-open-target').fill('./taskflow')
            expect(page.locator('#codex-open-dialog')).to_be_visible()
            expect(page.locator('#codex-open-command')).to_contain_text('--to')
            page.locator('#codex-open-dialog').screenshot(path=str(images / 'codex-import.png'))
            page.keyboard.press('Escape')

            candidate = root / 'taskflow-v2.contextpack'
            candidate.write_bytes(next_demonstration_package(package.read_bytes()))
            page.locator('[data-repository-section="conversation-section"]').click()
            page.locator('#edit-package').click()
            expect(page.locator('#draft-autosave-status')).to_contain_text('草稿已保存到服务器')
            page.locator('#version-file').set_input_files(str(candidate))
            expect(page.locator('#candidate-actions')).to_be_visible()
            page.locator('#apply-candidate').click()
            expect(page.locator('#messages')).to_contain_text('请输入任务标题')
            preview(page, '修复空标题任务，补充处理说明')
            save(page, 2)
            page.locator('#compare-versions').click()
            expect(page.locator('#version-comparison')).to_contain_text('新增消息 2')
            expect(page.locator('#version-comparison .code-version-comparison')).to_contain_text('修改 1')
            page.locator('#version-comparison .code-file-diff > summary').click()
            expect(page.locator('#version-comparison .code-patch')).to_contain_text('title.strip()')
            page.locator('#compare-panel').screenshot(path=str(images / 'version-compare.png'))

            page.locator('#fork-package').click()
            expect(page.locator('#fork-description')).to_contain_text('v2')
            expect(page.locator('input[name="fork-visibility"][value="private"]')).to_be_checked()
            expect(page.locator('input[name="fork-visibility"][value="public"]')).to_be_visible()
            page.locator('#fork-dialog').screenshot(path=str(images / 'fork.png'))
            context.close()
        browser.close()
    assert not errors, errors
    print(json.dumps({'images': {path.name: path.stat().st_size for path in images.glob('*.png')},
                      'javascript_errors': errors, 'real_user_data_access': False},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
