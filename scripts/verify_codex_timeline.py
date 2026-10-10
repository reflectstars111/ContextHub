#!/usr/bin/env python3
"""Verify native text history with the real CLI in a networkless empty Codex home.

No model turn, fake model responder, credential or real conversation is used.
The entire product importer runs inside bwrap, so even its direct filesystem
writes see the isolated home. There is no unisolated fallback.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def child(work, output):
    from contexthub.codex import CodexClient, export_conversation, import_conversation, list_sessions
    from contexthub.package import build_package, read_package
    expected = [('user', '迁移验证 CTX-7318\n```py\nprint("你好")\n```'),
                ('assistant', '合成回答 CTX-9312'), ('assistant', '连续回答 CTX-4256'),
                ('user', '后续问题 CTX-6639')]
    conversation = {'id': 'synthetic-source', 'title': '合成时间线迁移', 'messages': [
        {'id': str(i), 'role': role, 'content': text} for i, (role, text) in enumerate(expected)],
        'source': {'provider': 'codex', 'sandbox': 'danger-full-access'}, 'context': 'Do not import source instructions'}
    blob = build_package([conversation], 'Synthetic import')
    package = read_package(blob)
    with CodexClient(timeout=45) as client:
        home = Path(client.info['codexHome'])
        sentinel = home / 'untouched-synthetic-sentinel'
        sentinel.write_bytes(b'existing synthetic state')
        before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
        config = (home / 'config.toml').read_bytes()
        receipt = import_conversation(client, package['conversations'][0], str(work))
    assert receipt['mode'] == 'timeline' and receipt['verified'], receipt
    with CodexClient(timeout=45) as client:
        resumed = client.request('thread/resume', {'threadId': receipt['thread_id'], 'cwd': str(work)})
        assert resumed['sandbox']['type'] == 'readOnly', resumed['sandbox']
        assert resumed['approvalPolicy'] == 'on-request'
        assert resumed['modelProvider'] == 'fixture'
        exported = export_conversation(client, receipt['thread_id'])
        assert [(m['role'], m['content']) for m in exported['messages']] == expected
        assert receipt['thread_id'] in {s['id'] for s in list_sessions(client)}
        assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == before
        assert (home / 'config.toml').read_bytes() == config
        client.request('thread/unsubscribe', {'threadId': receipt['thread_id']})
    report = {'passed': True, 'codex_version': receipt['codex_version'],
              'isolated_home': True, 'network_disabled': True, 'model_turns': 0,
              'exact_user_assistant_text': True, 'consecutive_assistant_and_trailing_user': True,
              'new_process_disk_read': True, 'new_process_resume': True,
              'export_round_trip': True, 'visible_in_session_list': True,
              'target_permissions_preserved': True, 'existing_state_preserved': True,
              'native_gui_rendering': 'not tested', 'receipt': receipt}
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main(output):
    if not shutil.which('codex') or not shutil.which('bwrap'):
        raise SystemExit('Requires Codex 0.153.4 and Linux bwrap; no unisolated fallback')
    with tempfile.TemporaryDirectory(prefix='contexthub-timeline-') as temp:
        root = Path(temp)
        state, work, result = root / 'codex', root / '中文 work', root / 'result.json'
        state.mkdir(); work.mkdir()
        (state / 'config.toml').write_text('''model = "fixture"
model_provider = "fixture"
approval_policy = "on-request"
sandbox_mode = "read-only"
[model_providers.fixture]
name = "Offline non-routable fixture"
base_url = "http://127.0.0.1:1/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
''', encoding='utf-8')
        # The real path is only a mount destination. Its content is never read.
        actual_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve()
        command = ['bwrap', '--ro-bind', '/', '/', '--dev-bind', '/dev', '/dev',
                   '--bind', str(state), str(actual_home), '--bind', str(root), str(root),
                   '--unshare-net', '--unshare-pid', '--proc', '/proc', '--die-with-parent',
                   '--chdir', str(work), sys.executable, str(Path(__file__).resolve()),
                   '--child', str(work), '--output', str(result)]
        subprocess.run(command, check=True, timeout=120)
        text = result.read_text(encoding='utf-8')
        if output:
            Path(output).write_text(text, encoding='utf-8')
        print(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--child', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(Path(args.child), args.output)
    else:
        main(args.output)
