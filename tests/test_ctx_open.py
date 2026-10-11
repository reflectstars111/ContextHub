"""One-command website-to-local restore uses synthetic HTTP and mocked Codex."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from contexthub import ctx
from contexthub.cli_runtime import quote_argument
from contexthub.hub_client import HubClient
from contexthub.package import build_package
from contexthub.server import create_server
from contexthub.workspace import Workspace
from tests.test_code_repository import CONVERSATIONS, repository


class CtxOpenTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server = create_server(self.root / 'server', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.hub = f'http://127.0.0.1:{self.server.server_port}'
        password = 'synthetic-open-password-123'
        self.server.store.register('alice', password)
        self.environment = mock.patch.dict(os.environ, {
            'CONTEXTHUB_CONFIG_DIR': str(self.root / 'credentials')})
        self.environment.start()
        self.client = HubClient(self.hub)
        self.client.login('alice', password)
        self.old = repository({'app.py': b'old source\n', 'docs/设计 note.txt': b'old note\n'}, bindings=[
            {'conversation_id': 'c1', 'message_id': 'm1', 'kind': 'captured'}])
        self.first_blob = build_package(CONVERSATIONS, 'One command project', code=self.old)
        self.first = self.client.upload(self.first_blob, visibility='public', message='first')
        self.code = repository({'app.py': b'new source\n'}, previous=self.old)
        self.code['bindings'].append({'conversation_id': 'c1', 'message_id': 'm2',
                                     'snapshot_id': self.code['head'], 'kind': 'captured'})
        self.blob = build_package(CONVERSATIONS, 'One command project', code=self.code)
        self.latest = self.client.push(self.first['id'], self.blob,
                                      base_version=self.first['version']['id'], message='second',
                                      expected_visibility='public')
        self.target = self.root / "project space's"
        self.transfer_paths = []
        actual_temporary = tempfile.TemporaryDirectory

        def tracked_temporary(*args, **kwargs):
            directory = actual_temporary(*args, **kwargs)
            self.transfer_paths.append(Path(directory.name))
            return directory

        self.transfers = mock.patch('contexthub.ctx.tempfile.TemporaryDirectory', side_effect=tracked_temporary)
        self.transfers.start()

    def tearDown(self):
        self.transfers.stop()
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def url(self, detail=None):
        detail = detail or self.latest
        return f"{self.hub}/?package={detail['id']}&version={detail['version']['id']}"

    def command(self, *arguments, expected=0, as_json=True):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.chdir(self.root), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            status = ctx.main((['--json'] if as_json else []) + list(map(str, arguments)))
        self.assertEqual(status, expected, output.getvalue() + error.getvalue())
        self.assertTrue(all(not path.exists() for path in self.transfer_paths), 'transfer workspace leaked')
        result = output.getvalue() if status == 0 else error.getvalue()
        return json.loads(result) if as_json else result

    def test_historical_pin_restores_bytes_and_keeps_original_package_without_git(self):
        with mock.patch('shutil.which', return_value=None):
            result = self.command('open', self.url(self.first), '--to', self.target)
        self.assertEqual((self.target / 'code/app.py').read_bytes(), b'old source\n')
        self.assertEqual((self.target / 'code/docs/设计 note.txt').read_bytes(), b'old note\n')
        self.assertEqual(Workspace(self.target / 'conversations').package_bytes(), self.first_blob)
        self.assertFalse((self.target / 'code/.git').exists())
        self.assertEqual(result['source']['version_id'], self.first['version']['id'])
        self.assertEqual(result['native_status'], 'not_requested')
        self.assertNotIn('resume_command', result)
        resumed = self.command('open', self.url(self.first), '--to', self.target, '--resume')
        self.assertTrue(resumed['resumed'])
        self.assertEqual(resumed['conversation_commit'], result['conversation_commit'])

    def test_message_import_uses_prefix_and_completed_retry_never_imports_twice(self):
        with mock.patch('contexthub.codex.CodexClient'), mock.patch('contexthub.codex.import_conversation') as importer:
            importer.return_value = {'mode': 'context-injection', 'injected': True, 'thread_id': 'new-thread'}
            arguments = ('open', self.url(), '--to', self.target, '--codex', '--conversation', 'c1',
                         '--message', 'm1', '--mode', 'context')
            result = self.command(*arguments)
            self.assertEqual(importer.call_args.kwargs, {'mode': 'context'})
            self.assertEqual([m['id'] for m in importer.call_args.args[1]['messages']], ['m1'])
            self.assertEqual(importer.call_args.args[2], self.target / 'code')
            self.assertEqual((self.target / 'code/app.py').read_bytes(), b'old source\n')
            self.assertEqual(Workspace(self.target / 'conversations').package_bytes(), self.blob)
            self.assertEqual(result['resume_command'],
                             'codex resume -C ' + quote_argument(self.target / 'code') + ' ' + quote_argument('new-thread'))
            if os.name != 'nt':
                self.assertEqual(shlex.split(result['resume_command']),
                                 ['codex', 'resume', '-C', str(self.target / 'code'), 'new-thread'])
            resumed = self.command(*arguments, '--resume')
            self.assertTrue(resumed['resumed'])
            self.assertEqual(importer.call_count, 1)

    def test_snapshot_import_keeps_full_dialogue_and_human_output_has_continue_command(self):
        with mock.patch('contexthub.codex.CodexClient'), mock.patch('contexthub.codex.import_conversation') as importer:
            importer.return_value = {'mode': 'timeline', 'verified': True, 'thread_id': 'new-thread'}
            output = self.command('open', self.first['id'], '--hub', self.hub,
                                  '--version', self.latest['version']['id'], '--to', self.target,
                                  '--codex', '--snapshot', self.old['head'], as_json=False)
            self.assertEqual(len(importer.call_args.args[1]['messages']), 2)
            self.assertEqual(importer.call_args.kwargs, {'mode': 'timeline'})
            self.assertIn('codex resume -C', output)
            self.assertIn(str(self.target / 'code'), output)
            self.assertIn('未自动调用模型', output)

    def test_public_download_still_requires_login_and_writes_nothing(self):
        self.client.logout()
        result = self.command('open', self.url(), '--to', self.target, expected=1)
        self.assertEqual(result['status'], 401)
        self.assertIn('ctx auth login', result['error'])
        self.assertFalse(self.target.exists())

    def test_invalid_reference_selection_and_existing_directory_do_not_overwrite(self):
        for url in (f'{self.hub}/?package={self.first["id"]}',
                    self.url() + '&tab=code', self.url() + '&version=duplicate',
                    self.url() + '#fragment'):
            with self.subTest(url=url):
                self.command('open', url, '--to', self.target, expected=1)
                self.assertFalse(self.target.exists())
        for selection in (('--message', 'missing'), ('--snapshot', '0' * 64), ('--conversation', 'missing')):
            with self.subTest(selection=selection):
                self.command('open', self.url(), '--to', self.target, *selection, expected=1)
                self.assertFalse(self.target.exists())
        self.command('open', self.url(), '--to', self.target, '--resume', expected=1)
        self.target.mkdir()
        marker = self.target / 'personal.txt'
        marker.write_bytes(b'preserve')
        self.command('open', self.url(), '--to', self.target, expected=1)
        self.assertEqual(marker.read_bytes(), b'preserve')

    def test_native_failure_preserves_receipt_and_retry_does_not_duplicate(self):
        with mock.patch('contexthub.codex.CodexClient'), mock.patch('contexthub.codex.import_conversation') as importer:
            importer.return_value = {'mode': 'failed', 'verified': False, 'thread_id': 'partial-thread'}
            arguments = ('open', self.url(), '--to', self.target, '--codex', '--message', 'm1')
            failed = self.command(*arguments, expected=1)
            self.assertEqual(failed['receipt']['native_status'], 'uncertain')
            self.assertEqual(failed['receipt']['native_receipt']['thread_id'], 'partial-thread')
            self.assertIn(str(self.target / 'receipt.json'), failed['error'])
            receipt = json.loads((self.target / 'receipt.json').read_text())
            self.assertEqual(receipt['pair_status'], 'complete')
            self.assertEqual((self.target / 'code/app.py').read_bytes(), b'old source\n')
            self.command(*arguments, '--resume', expected=1)
            self.assertEqual(importer.call_count, 1)

    def test_resume_rejects_different_source_selection_and_changed_code(self):
        self.command('open', self.url(), '--to', self.target, '--message', 'm1')
        self.command('open', self.url(), '--to', self.target, '--snapshot', self.old['head'], '--resume', expected=1)
        other = self.client.upload(self.blob, visibility='public', message='same bytes elsewhere',
                                   new_library_key='another-project-with-the-same-bytes')
        self.assertNotEqual(other['id'], self.latest['id'])
        self.command('open', self.url(other), '--to', self.target, '--message', 'm1', '--resume', expected=1)
        (self.target / 'code/app.py').write_bytes(b'local modification\n')
        self.command('open', self.url(), '--to', self.target, '--message', 'm1', '--resume', expected=1)
        self.assertEqual((self.target / 'code/app.py').read_bytes(), b'local modification\n')

    def test_missing_code_and_ambiguous_conversation_fail_before_native_import(self):
        plain = self.client.upload(build_package(CONVERSATIONS, 'No code'), visibility='public')
        missing = self.command('open', self.url(plain), '--to', self.target, '--codex', expected=1)
        self.assertIn('ctx codex import', missing['error'])
        self.assertFalse(self.target.exists())
        conversations = copy.deepcopy(CONVERSATIONS)
        conversations.append(dict(copy.deepcopy(CONVERSATIONS[0]), id='c2'))
        multiple = self.client.upload(build_package(conversations, 'Multiple', code=self.code), visibility='public')
        with mock.patch('contexthub.codex.CodexClient') as native:
            failed = self.command('open', self.url(multiple), '--to', self.target, '--codex', expected=1)
            self.assertIn('--conversation', failed['error'])
            native.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_hub_mismatched_content_is_rejected_and_temp_workspace_removed(self):
        with mock.patch.object(HubClient, 'download', return_value=self.first_blob):
            failed = self.command('open', self.url(), '--to', self.target, expected=1)
        self.assertIn('传输内容不一致', failed['error'])
        self.assertFalse(self.target.exists())

    def test_distributed_cli_can_open_outside_source_checkout(self):
        from contexthub.distribution import cli_archive
        executable = self.root / 'ctx.pyz'
        cli_archive.cache_clear()
        executable.write_bytes(cli_archive())
        result = subprocess.run([sys.executable, str(executable), '--json', 'open', self.url(),
                                 '--to', str(self.target), '--message', 'm1'],
                                cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['source']['version_id'], self.latest['version']['id'])
        self.assertEqual((self.target / 'code/app.py').read_bytes(), b'old source\n')
        self.assertEqual(Workspace(self.target / 'conversations').package_bytes(), self.blob)


if __name__ == '__main__':
    unittest.main()
