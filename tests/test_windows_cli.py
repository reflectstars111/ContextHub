"""Exercise every ctx command through real Windows PE and zipapp processes.

All accounts, conversations and Codex RPC responses are synthetic and local.
No production Hub or native Codex history is accessed.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

from contexthub.cli_runtime import quote_argument
from contexthub.distribution import cli_archive
from contexthub.package import build_package, read_package
from contexthub.server import create_server
from contexthub.windows_launcher import build_launcher


CODEX_FIXTURE = r'''
import json, os, sys
items = [
    {'type':'userMessage','id':'u','content':[{'type':'text','text':'Synthetic question'}]},
    {'type':'agentMessage','id':'a','text':'\u5408\u6210\u56de\u7b54','phase':'final_answer'}]
turns = [{'id':'t','items':items,'itemsView':'full','status':'completed'}]
for line in sys.stdin.buffer:
    request = json.loads(line)
    if 'id' not in request or 'method' not in request:
        continue
    method = request['method']
    with open(os.environ['CTX_RPC_LOG'], 'a', encoding='utf-8') as log:
        log.write(method + '\n')
    if method == 'initialize': result = {'userAgent':'synthetic-fixture'}
    elif method == 'thread/list': result = {'data':[{'id':'fixture-source','name':'Synthetic Codex'}],'nextCursor':None}
    elif method == 'thread/read': result = {'thread':{'id':request['params']['threadId'],'name':'Synthetic Codex','historyMode':'paginated','turns':turns}}
    elif method == 'thread/turns/list': result = {'data':turns,'nextCursor':None}
    elif method == 'thread/start': result = {'thread':{'id':'fixture-import'}}
    elif method in ('thread/inject_items','thread/name/set'): result = {}
    else: raise AssertionError('Unapproved method: ' + method)
    print(json.dumps({'id':request['id'],'result':result}), flush=True)
'''


@unittest.skipUnless(os.name == 'nt', 'native Windows command acceptance')
class NativeWindowsCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ctx-commands-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / '中文 & % ! bin'
        self.bin.mkdir()
        (self.bin / 'ctx.pyz').write_bytes(cli_archive())
        self.exe = build_launcher(self.bin / 'ctx.exe')
        self.server = create_server(self.root / 'hub', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.password = 'synthetic-windows-password-123'
        self.server.store.register('alice', self.password)
        self.env = dict(os.environ, CONTEXTHUB_CONFIG_DIR=str(self.root / 'credentials'),
                        CONTEXTHUB_HUB=f'http://127.0.0.1:{self.server.server_port}',
                        PYTHONIOENCODING='cp1252')
        self.env.pop('PYTHONUTF8', None)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def command(self, *args, cwd=None, stdin=None, expected=0, json_output=True):
        argv = [str(self.exe), *(['--json'] if json_output else []), *map(str, args)]
        result = subprocess.run(argv, cwd=cwd or self.root, env=self.env, input=stdin,
                                capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout if expected == 0 else result.stderr) if json_output else result.stdout

    def test_all_workspace_commands_and_authentication_through_exe(self):
        title = '本机对话 & %PATH% ! 中文'
        source = self.root / '输入 & space.contextpack'
        source.write_bytes(build_package([{'id':'sample', 'messages':[
            {'id':'q','role':'user','content':'问题'},
            {'id':'a','role':'assistant','content':'第一版回答'}]}], title))
        self.assertEqual(self.command('inspect', source)['conversation_count'], 1)
        self.assertFalse(self.command('auth', 'status')['authenticated'])
        self.assertEqual(self.command('auth', 'login', '--username', 'alice', '--password-stdin',
                                      stdin=self.password + '\n')['user']['username'], 'alice')
        self.assertTrue(self.command('auth', 'status')['authenticated'])
        alpha, beta = self.root / '中文 alpha', self.root / '中文 beta'
        self.command('init', alpha, '--title', title)
        self.command('add', source, cwd=alpha)
        self.assertEqual(self.command('diff', '--staged', cwd=alpha)['summary']['conversations_added'], 1)
        self.command('commit', '-m', '第一版 & %PATH% !', cwd=alpha)
        remote = self.command('push', '--visibility', 'public', cwd=alpha)['remote']
        self.assertEqual(self.command('remote', cwd=alpha)['remote'], remote)
        self.command('clone', remote['package_id'], beta)
        path = next((beta / 'conversations').glob('*.json'))
        conversation = json.loads(path.read_text(encoding='utf-8'))
        conversation['messages'][1]['content'] = '第二版回答 & %PATH% !'
        path.write_text(json.dumps(conversation, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(self.command('diff', cwd=beta)['summary']['messages_modified'], 1)
        self.command('add', '.', cwd=beta)
        self.command('commit', '-m', '第二版', cwd=beta)
        self.command('push', cwd=beta)
        self.assertTrue(self.command('pull', cwd=alpha)['updated'])
        self.assertFalse(self.command('pull', cwd=alpha)['updated'])
        self.assertFalse(self.command('status', cwd=alpha)['modified'])
        self.assertEqual(len(self.command('log', cwd=beta)['commits']), 2)
        output = self.root / '导出.contextpack'
        self.command('export', output, cwd=alpha)
        self.assertEqual(read_package(output.read_bytes())['conversations'][0]['messages'][1]['content'],
                         conversation['messages'][1]['content'])
        self.assertEqual(read_package(output.read_bytes())['library']['title'], title)
        fork = self.command('fork', remote['package_id'], self.root / '中文 fork')
        self.assertEqual(fork['remote']['visibility'], 'private')
        self.assertNotEqual(fork['remote']['package_id'], remote['package_id'])
        self.command('auth', 'logout')
        self.assertFalse(self.command('auth', 'status')['authenticated'])
        self.assertEqual(self.command('clone', remote['package_id'], self.root / 'denied', expected=1)['status'], 401)

    def test_codex_commands_use_native_fixture_and_utf8_without_model_calls(self):
        fixture = self.root / '合成 Codex & space'
        fixture.mkdir()
        with zipfile.ZipFile(fixture / 'ctx.pyz', 'w') as archive:
            archive.writestr('__main__.py', CODEX_FIXTURE)
        native = build_launcher(fixture / 'codex.exe')
        log = self.root / 'rpc.log'
        self.env.update(CONTEXTHUB_CODEX_EXE=str(native), CTX_RPC_LOG=str(log))
        self.assertEqual(self.command('codex', 'list')[0]['id'], 'fixture-source')
        exported = self.root / 'Codex 导出.contextpack'
        self.command('codex', 'export', '--thread', 'fixture-source', '--title', '中文导出', '-o', exported)
        self.assertEqual(read_package(exported.read_bytes())['library']['title'], '中文导出')
        self.assertTrue(self.command('codex', 'import', exported, '--dry-run')['dry_run'])
        receipt = self.command('codex', 'import', exported, '--cwd', self.root)
        self.assertTrue(receipt['results'][0]['injected'])
        methods = log.read_text(encoding='utf-8').splitlines()
        self.assertIn('thread/inject_items', methods)
        self.assertNotIn('turn/start', methods)

    def test_all_help_commands_and_powershell_example_quoting(self):
        for args in [[], ['auth'], *[[name] for name in
                     ('init','inspect','add','status','diff','commit','log','clone','fork','push','pull','remote','export')],
                     *[['codex', name] for name in ('list','export','import')]]:
            with self.subTest(command=args):
                self.assertIn('usage:', self.command(*args, '--help', json_output=False))
        value = "中文 space & O'Brien %PATH% !"
        script = '[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; Write-Output ' + quote_argument(value)
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), value)
