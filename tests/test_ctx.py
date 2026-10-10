"""Synthetic, real HTTP acceptance for the Git-style terminal workflow."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from contexthub import ctx
from contexthub.hub_client import HubClient, HubClientError
from contexthub.package import build_package, read_package
from contexthub.server import create_server
from contexthub.workspace import Workspace


PASSWORD = 'synthetic-cli-password-123'


def sample_pack(text='Synthetic answer', ident='example'):
    return build_package([{'id': ident, 'title': '合成对话', 'source': {'platform': 'claude'}, 'messages': [
        {'id': 'question', 'role': 'user', 'content': 'Synthetic question'},
        {'id': 'answer', 'role': 'assistant', 'content': text},
    ]}], '合成测试库')


class CtxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server = create_server(self.root / 'server', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.hub = f'http://127.0.0.1:{self.server.server_port}'
        self.server.store.register('alice', PASSWORD)
        self.config = self.root / 'credentials'
        self.environment = mock.patch.dict(os.environ, {'CONTEXTHUB_HUB': self.hub, 'CONTEXTHUB_CONFIG_DIR': str(self.config)})
        self.environment.start()
        self.source = self.root / 'sample.contextpack'
        self.source.write_bytes(sample_pack())

    def tearDown(self):
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def command(self, *args, cwd=None, expected=0, stdin=''):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.chdir(cwd or self.root), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), mock.patch('sys.stdin', io.StringIO(stdin)):
            code = ctx.main(['--json', *map(str, args)])
        self.assertEqual(code, expected, error.getvalue() + output.getvalue())
        return json.loads(output.getvalue() if expected == 0 else error.getvalue())

    def login(self, user='alice'):
        return self.command('auth', 'login', '--username', user, '--password-stdin', stdin=PASSWORD + '\n')

    def make_workspace(self, name='alpha'):
        directory = self.root / name
        self.command('init', directory, '--title', '命令行对话库')
        self.command('add', self.source, cwd=directory)
        self.command('commit', '-m', 'initial conversations', cwd=directory)
        return directory

    def edit(self, directory, text, commit=True):
        path = next((directory / 'conversations').glob('*.json'))
        conversation = json.loads(path.read_text())
        conversation['messages'][1]['content'] = text
        path.write_text(json.dumps(conversation, ensure_ascii=False), encoding='utf-8')
        if commit:
            self.command('add', '.', cwd=directory)
            self.command('commit', '-m', text, cwd=directory)

    def published(self, visibility='private'):
        self.login()
        directory = self.make_workspace()
        result = self.command('push', '--visibility', visibility, cwd=directory)
        return directory, result['remote']

    def test_full_multiple_commit_push_clone_edit_pull_and_export(self):
        session = self.login()
        self.assertEqual(session['user']['username'], 'alice')
        self.assertNotIn('csrf_token', session)
        alpha = self.make_workspace()
        # Unmanaged files stay outside every committed and uploaded snapshot.
        (alpha / 'private-project.txt').write_text('DO NOT UPLOAD THIS UNMANAGED FILE')
        self.edit(alpha, 'Second local revision')
        self.assertEqual(self.command('status', cwd=alpha)['ahead'], 2)
        result = self.command('push', cwd=alpha)
        self.assertEqual(result['pushed'], 2)
        self.assertEqual(result['remote']['visibility'], 'private')
        client = HubClient(self.hub)
        detail = client.detail(result['remote']['package_id'])
        self.assertEqual(detail['version']['number'], 2)
        self.assertEqual([v['message'] for v in client.versions(detail['id'])['versions']], ['Second local revision', 'initial conversations'])
        beta = self.root / 'beta'
        self.command('clone', f"{self.hub}/?package={detail['id']}", beta)
        self.assertEqual(Workspace.find(beta).package_bytes(), Workspace.find(alpha).package_bytes())
        self.assertFalse((beta / 'private-project.txt').exists())
        self.edit(alpha, 'Third revision', commit=False)
        diff = self.command('diff', cwd=alpha)
        self.assertEqual(diff['summary']['messages_modified'], 1)
        self.command('add', '.', cwd=alpha)
        self.assertEqual(self.command('diff', '--staged', cwd=alpha)['summary']['messages_modified'], 1)
        self.command('commit', '-m', 'third version', cwd=alpha)
        self.command('push', cwd=alpha)
        self.assertTrue(self.command('pull', cwd=beta)['updated'])
        self.assertFalse(self.command('pull', cwd=beta)['updated'])
        output = self.root / 'export.contextpack'
        self.command('export', output, cwd=beta)
        self.assertEqual(output.read_bytes(), Workspace.find(alpha).package_bytes())
        self.assertEqual(read_package(output.read_bytes())['conversations'][0]['messages'][1]['content'], 'Third revision')
        self.assertEqual(self.command('status', cwd=beta)['ahead'], 0)
        self.assertEqual(self.command('remote', cwd=beta)['remote']['package_id'], detail['id'])
        self.assertEqual(len(self.command('log', cwd=alpha)['commits']), 3)
        self.command('export', output, cwd=beta, expected=1)

    def test_conflict_and_dirty_pull_preserve_local_files_and_checkpoints(self):
        alpha, remote = self.published()
        beta = self.root / 'beta'
        self.command('clone', remote['package_id'], beta)
        self.edit(alpha, 'Alice update')
        self.command('push', cwd=alpha)
        self.edit(beta, 'My unsaved edit', commit=False)
        files = {path: path.read_bytes() for path in (beta / 'conversations').glob('*.json')}
        self.command('pull', cwd=beta, expected=1)
        self.assertEqual(files, {path: path.read_bytes() for path in files})
        self.command('add', '.', cwd=beta)
        self.command('commit', '-m', 'local conflicting edit', cwd=beta)
        files = {path: path.read_bytes() for path in files}
        checkpoint = Workspace.find(beta).remote
        head = Workspace.find(beta).head
        failure = self.command('push', cwd=beta, expected=1)
        self.assertEqual(failure['status'], 409)
        self.assertEqual(Workspace.find(beta).remote, checkpoint)
        self.assertEqual(Workspace.find(beta).head, head)
        self.assertEqual(Workspace.find(beta).status()['ahead'], 1)
        self.command('pull', cwd=beta, expected=1)
        self.assertEqual(files, {path: path.read_bytes() for path in files})

    def test_pull_auto_merges_independent_commits_and_can_push_result(self):
        alpha, remote = self.published()
        beta = self.root / 'beta'
        self.command('clone', remote['package_id'], beta)
        self.edit(alpha, 'remote answer')
        self.command('push', cwd=alpha)
        path = next((beta / 'conversations').glob('*.json'))
        conversation = json.loads(path.read_text())
        conversation['messages'][0]['content'] = 'local question'
        path.write_text(json.dumps(conversation))
        self.command('add', '.', cwd=beta)
        self.command('commit', '-m', 'local question', cwd=beta)
        self.assertTrue(self.command('pull', cwd=beta)['updated'])
        self.assertEqual(self.command('status', cwd=beta)['ahead'], 1)
        self.assertEqual(self.command('push', cwd=beta)['pushed'], 1)
        self.command('pull', cwd=alpha)
        messages = read_package(Workspace.find(alpha).package_bytes())['conversations'][0]['messages']
        self.assertEqual([item['content'] for item in messages], ['local question', 'remote answer'])

    def test_merge_keeps_local_choice_and_topology_only_push_skips_remote_version(self):
        alpha, remote = self.published()
        self.command('branch', 'feature', cwd=alpha)
        self.command('switch', 'feature', cwd=alpha)
        self.edit(alpha, 'feature change')
        self.command('switch', 'main', cwd=alpha)
        # Explicit resolution may keep the existing staged snapshot unchanged.
        self.command('merge', 'feature', '--resolved', cwd=alpha)
        result = self.command('push', cwd=alpha)
        self.assertEqual(result['pushed'], 0)
        self.assertEqual(self.command('status', cwd=alpha)['ahead'], 0)
        self.assertEqual(HubClient(self.hub).detail(remote['package_id'])['version_count'], 1)

    def test_changed_remote_visibility_blocks_push_until_explicit_pull(self):
        directory, remote = self.published()
        self.edit(directory, 'private pending conversation edit')
        ws = Workspace(directory)
        before = ws.package_bytes()
        client = HubClient(self.hub)
        owner = client.session()['user']
        self.server.store.set_visibility(remote['package_id'], owner['id'], 'public', remote['version_id'])
        failure = self.command('push', cwd=directory, expected=1)
        self.assertEqual(failure['status'], 409)
        self.assertIn('可见性', failure['error'])
        self.assertEqual(ws.remote['visibility'], 'private')
        self.assertEqual(ws.package_bytes(), before)
        self.assertEqual(ws.status()['ahead'], 1)
        self.assertEqual(client.detail(remote['package_id'])['version_count'], 1)
        refreshed = self.command('pull', cwd=directory)
        self.assertTrue(refreshed['visibility_updated'])
        self.assertEqual(refreshed['remote']['visibility'], 'public')
        self.assertEqual(self.command('push', cwd=directory)['pushed'], 1)
        self.assertEqual(ws.package_bytes(), before)

    def test_new_branch_push_creates_independent_library_even_for_identical_snapshot(self):
        directory, original = self.published()
        client = HubClient(self.hub)
        original_blob = client.download(original['package_id'])
        self.command('switch', '-c', 'feature', cwd=directory)
        independent = self.command('push', cwd=directory)['remote']
        self.assertNotEqual(independent['package_id'], original['package_id'])
        self.edit(directory, 'feature evolves separately')
        self.command('push', cwd=directory)
        self.assertEqual(client.download(original['package_id']), original_blob)
        self.command('switch', 'main', cwd=directory)
        self.assertEqual(Workspace(directory).remote['package_id'], original['package_id'])
        self.assertEqual(Workspace(directory).package_bytes(), original_blob)

    def test_push_resume_after_lost_response_does_not_duplicate_versions(self):
        directory, remote = self.published()
        self.edit(directory, 'Local second version')
        self.edit(directory, 'Local third version')
        actual = HubClient.push
        calls = []
        def lost_response(client, *args, **kwargs):
            result = actual(client, *args, **kwargs)
            calls.append(result)
            if len(calls) == 2:
                raise HubClientError(None, 'Synthetic connection dropped after server commit')
            return result
        with mock.patch.object(HubClient, 'push', lost_response):
            self.command('push', cwd=directory, expected=1)
        ws = Workspace.find(directory)
        self.assertEqual(ws.status()['ahead'], 1)
        self.assertEqual(ws.remote['version_id'], calls[0]['version']['id'])
        self.assertEqual(self.command('push', cwd=directory)['pushed'], 1)
        self.assertEqual(ws.remote['version_id'], calls[1]['version']['id'])
        self.assertEqual(HubClient(self.hub).detail(remote['package_id'])['version_count'], 3)

    def test_first_push_lost_response_can_be_retried_without_a_duplicate_library(self):
        self.login()
        directory = self.make_workspace()
        actual = HubClient.upload
        def lost_response(client, *args, **kwargs):
            actual(client, *args, **kwargs)
            raise HubClientError(None, 'Synthetic lost upload response')
        with mock.patch.object(HubClient, 'upload', lost_response):
            self.command('push', cwd=directory, expected=1)
        self.assertIsNone(Workspace.find(directory).remote)
        self.assertEqual(self.command('push', cwd=directory)['pushed'], 1)
        libraries = HubClient(self.hub).list_packages()['packages']
        self.assertEqual(len(libraries), 1)
        self.assertEqual(libraries[0]['version_count'], 1)

    def test_public_download_needs_login_and_other_owner_cannot_push(self):
        _, remote = self.published('public')
        self.command('auth', 'logout')
        self.assertIsNone(self.command('auth', 'status')['user'])
        target = self.root / 'download'
        denied = self.command('clone', remote['package_id'], target, expected=1)
        self.assertEqual(denied['status'], 401)
        self.assertIn('ctx auth login', denied['error'])
        self.assertFalse(target.exists())
        self.server.store.register('bob', PASSWORD)
        self.login('bob')
        self.command('clone', remote['package_id'], target)
        self.edit(target, 'Bobs edit')
        denied = self.command('push', cwd=target, expected=1)
        self.assertEqual(denied['status'], 404)
        self.assertIn('ctx fork', denied['error'])
        self.assertEqual(Workspace.find(target).status()['ahead'], 1)

    def test_fork_to_new_directory_allows_another_owner_to_evolve_copy(self):
        _, remote = self.published('public')
        self.server.store.register('bob', PASSWORD)
        self.login('bob')
        target = self.root / 'forked'
        result = self.command('fork', f"{self.hub}/?package={remote['package_id']}&version={remote['version_id']}", target)
        copied = result['remote']
        self.assertNotEqual(copied['package_id'], remote['package_id'])
        self.assertEqual(copied['visibility'], 'private')
        self.edit(target, 'Bobs independent improvement')
        self.command('push', cwd=target)
        client = HubClient(self.hub)
        self.assertEqual(client.detail(remote['package_id'])['head_version_id'], remote['version_id'])
        self.assertEqual(client.detail(copied['package_id'])['version_count'], 2)

    def test_import_preflight_and_explicit_archive_confirmation(self):
        directory = self.root / 'imports'
        self.command('init', directory)
        malformed = self.root / 'bad.json'
        malformed.write_text('{bad json')
        self.command('add', self.source, malformed, cwd=directory, expected=1)
        self.assertEqual(self.command('status', cwd=directory)['conversations'], 0)
        unsupported = self.root / 'notes.txt'
        unsupported.write_text('Unstructured synthetic conversation')
        self.command('add', unsupported, '--platform', 'deepseek', '--mode', 'archive', cwd=directory, expected=1)
        self.assertEqual(self.command('status', cwd=directory)['conversations'], 0)
        self.command('add', unsupported, '--platform', 'deepseek', '--mode', 'archive', '--confirm-import', cwd=directory)
        self.command('commit', '-m', 'explicit archive', cwd=directory)
        package = read_package(Workspace.find(directory).package_bytes())
        self.assertEqual(package['conversations'][0]['source']['kind'], 'source_archive')
        duplicate = self.root / 'duplicate.contextpack'
        duplicate.write_bytes(sample_pack('Different content with same ID'))
        self.command('add', self.source, duplicate, cwd=directory, expected=1)
        self.command('add', self.source, duplicate, '--replace', cwd=directory)
        self.assertEqual(self.command('status', cwd=directory)['conversations'], 2)

    def test_clone_pinned_version_existing_destination_and_remote_constraints(self):
        directory, first = self.published()
        first_blob = Workspace.find(directory).package_bytes()
        self.edit(directory, 'Remote latest')
        self.command('push', cwd=directory)
        historical = self.root / 'historical'
        self.command('clone', f"{self.hub}/?package={first['package_id']}&version={first['version_id']}", historical)
        self.assertEqual(Workspace.find(historical).package_bytes(), first_blob)
        self.command('clone', first['package_id'], directory, expected=1)
        self.command('push', '--hub', 'https://example.invalid', cwd=directory, expected=1)
        self.command('push', '--visibility', 'public', cwd=directory, expected=1)
        self.assertTrue(self.command('pull', cwd=historical)['updated'])

    def test_codex_bridge_dry_run_and_module_help(self):
        result = self.command('codex', 'import', self.source, '--dry-run')
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['conversation_count'], 1)
        self.command('codex', 'serve', expected=1)
        repository = str(Path(__file__).resolve().parents[1])
        environment = dict(os.environ, PYTHONPATH=repository)
        for arguments in (['--help'], ['codex', 'export', '--help']):
            result = subprocess.run([sys.executable, '-m', 'contexthub.ctx', *arguments], cwd=self.root,
                                    env=environment, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout)


class ReferenceTests(unittest.TestCase):
    def test_inspect_runs_without_workspace_credentials_or_network_and_never_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / 'conversation.txt'
            path.write_text('Synthetic archive requiring confirmation', encoding='utf-8')
            output, error = io.StringIO(), io.StringIO()
            with contextlib.chdir(directory), mock.patch.dict(os.environ, {'CONTEXTHUB_CONFIG_DIR': str(directory / 'missing-config')}), \
                    mock.patch.object(ctx, 'HubClient', side_effect=AssertionError('inspect must remain offline')), \
                    mock.patch.object(ctx.Workspace, 'find', side_effect=AssertionError('inspect needs no workspace')), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                code = ctx.main(['inspect', str(path), '--platform', 'claude', '--mode', 'archive', '--json'])
            self.assertEqual(code, 0, error.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(result['platforms'], ['claude'])
            self.assertEqual(result['conversation_count'], 1)
            self.assertEqual(result['conversations'][0]['message_count'], 1)
            self.assertTrue(result['import_report']['requires_confirmation'])
            self.assertFalse(result['compatibility']['native_restore_verified'])
            self.assertEqual(list(directory.iterdir()), [path])
            self.assertEqual(path.read_text(), 'Synthetic archive requiring confirmation')

    def test_only_explicit_secret_free_share_urls_and_matching_origin(self):
        self.assertEqual(ctx.clone_reference('https://EXAMPLE.com:443/?package=abc&version=123'),
                         ('https://example.com', 'abc', '123'))
        values = ['https://user:pass@example.com/?package=abc', 'https://example.com/?package=abc&token=secret',
                  'https://example.com/?package=abc#token=secret', 'https://example.com/api/packages/abc',
                  'https://example.com/?package=abc&package=def', 'https://example.com/?package=..%2Fsecret',
                  'https://example.com/?package=abc&version=', 'https://example.com/?package=abc\n',
                  'http://example.com/?package=abc']
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ctx.clone_reference(value)
        with self.assertRaises(ValueError):
            ctx.clone_reference('https://example.com/?package=abc', hub='https://elsewhere.invalid')
        with self.assertRaises(ValueError):
            ctx.clone_reference('https://example.com/?package=abc&version=first', version='second')


if __name__ == '__main__':
    unittest.main()
