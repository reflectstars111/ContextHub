#!/usr/bin/env python3
"""Verify paired restore and actual Codex resume in an empty networkless home."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def child(root, output):
    from contexthub import ctx
    from contexthub.codex import CodexClient, export_conversation, list_sessions
    from contexthub.package import build_package, read_package
    from contexthub.workspace import Workspace

    def git(path, *args):
        return subprocess.run(['git', '-C', str(path), '-c', 'user.name=Synthetic',
                               '-c', 'user.email=synthetic@example.invalid',
                               '-c', 'commit.gpgSign=false', *args], capture_output=True,
                              text=True, check=True, timeout=20).stdout.strip()

    def command(path, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(path), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = ctx.main(['--json', *map(str, args)])
        assert result == 0, err.getvalue()
        return json.loads(out.getvalue())

    expected = [('user', '实现合成登录功能 CTX-PAIR-427'), ('assistant', '已完成合成代码。\n```python\nprint("登录")\n```')]
    blob = build_package([{'id': 'paired-fixture', 'title': '配对恢复验证', 'messages': [
        {'id': str(i), 'role': role, 'content': text} for i, (role, text) in enumerate(expected)]}], 'Synthetic pair')
    ws = Workspace.init(root / 'dialogues', 'Synthetic pair')
    ws.add(blob); ws.commit('conversation')
    code = root / 'project'; code.mkdir()
    git(code, 'init', '-b', 'main')
    git(code, 'config', 'core.autocrlf', 'false')
    (code / 'app.py').write_text('print("登录")\n', encoding='utf-8')
    git(code, 'add', '.'); git(code, 'commit', '-m', 'paired code')
    fixed = git(code, 'rev-parse', 'HEAD')
    command(ws.root, 'checkpoint', '--code', code, '-m', 'verified pair')
    linked_blob = ws.package_bytes()
    (code / 'app.py').write_text('ongoing work must remain\n')
    target = root / 'restored'
    receipt = command(ws.root, 'restore', 'HEAD', '--code', code, '--to', target, '--codex')
    assert receipt['pair_status'] == 'complete' and receipt['native_status'] == 'complete', receipt
    native = receipt['native_receipt']
    assert native['verified'] and native['mode'] == 'timeline', native
    assert Workspace(target / 'conversations').package_bytes() == linked_blob
    assert git(target / 'code', 'rev-parse', 'HEAD') == fixed
    assert (target / 'code/app.py').read_text() == 'print("登录")\n'
    assert (code / 'app.py').read_text() == 'ongoing work must remain\n'
    with CodexClient(timeout=45) as client:
        exported = export_conversation(client, native['thread_id'])
        assert [(m['role'], m['content']) for m in exported['messages']] == expected
        resumed = client.request('thread/resume', {'threadId': native['thread_id']})
        assert Path(resumed['thread']['cwd']) == target / 'code', resumed['thread']
        assert resumed['sandbox']['type'] == 'readOnly'
        assert resumed['approvalPolicy'] == 'on-request'
        ids_before = {s['id'] for s in list_sessions(client)}
        client.request('thread/unsubscribe', {'threadId': native['thread_id']})
    repeated = command(ws.root, 'restore', 'HEAD', '--code', code, '--to', target, '--codex', '--resume')
    assert repeated['native_receipt']['thread_id'] == native['thread_id']
    with CodexClient(timeout=45) as client:
        assert {s['id'] for s in list_sessions(client)} == ids_before
    report = {'passed': True, 'codex_version': native['codex_version'],
              'isolated_home': True, 'network_disabled': True, 'model_turns': 0,
              'paired_code_commit': fixed, 'exact_conversation_snapshot': True,
              'native_text_roundtrip': True, 'native_cwd_is_restored_code': True,
              'resume_without_duplicate_session': True, 'source_edits_preserved': True,
              'native_gui_rendering': 'not tested', 'model_continuation': 'not tested'}
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


def main(output):
    if not shutil.which('codex') or not shutil.which('bwrap'):
        raise SystemExit('Requires Codex 0.153.4 and bwrap; no unisolated fallback')
    with tempfile.TemporaryDirectory(prefix='contexthub-code-sync-') as temp:
        root = Path(temp); state = root / 'codex'; state.mkdir()
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
''')
        actual_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve()
        result = root / 'result.json'
        subprocess.run(['bwrap', '--ro-bind', '/', '/', '--dev-bind', '/dev', '/dev',
                        '--bind', str(state), str(actual_home), '--bind', str(root), str(root),
                        '--unshare-net', '--unshare-pid', '--proc', '/proc', '--die-with-parent',
                        '--chdir', str(root), sys.executable, str(Path(__file__).resolve()),
                        '--child', str(root), '--output', str(result)], check=True, timeout=120)
        text = result.read_text()
        Path(output).write_text(text)
        print(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--child', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(Path(args.child), args.output)
    else:
        main(args.output)
