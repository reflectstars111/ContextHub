"""Code follows conversations through workspace, Git, Hub and permissions."""
import copy
import subprocess
from pathlib import Path
import tempfile
import unittest

from contexthub.package import build_package, read_package
from contexthub.workspace import Workspace
from tests.test_code_repository import CONVERSATIONS, repository
from tests import test_server
from tests import test_ctx


def pack(code):
    return build_package(CONVERSATIONS, 'Code project', code=code)


class CodeWorkspaceTests(unittest.TestCase):
    def test_workspace_merge_preserves_code_from_both_branches(self):
        from contexthub.code_repository import blob_bytes
        with tempfile.TemporaryDirectory() as directory:
            ws = Workspace.init(Path(directory) / 'workspace', 'Merging code')
            base = repository({'a.py': b'old a', 'b.py': b'old b'})
            ws.add(pack(base)); ws.commit('Base'); ws.branch('feature')
            ws.add(pack(repository({'a.py': b'new a', 'b.py': b'old b'}, previous=base)))
            ws.commit('Main a')
            ws.switch('feature')
            ws.add(pack(repository({'a.py': b'old a', 'b.py': b'new b'}, previous=base)))
            ws.commit('Feature b'); ws.switch('main')
            ws.merge('feature')
            code = read_package(ws.package_bytes())['code']
            head = next(s for s in code['snapshots'] if s['id'] == code['head'])
            self.assertEqual(len(head['parents']), 2)
            self.assertEqual({f['path']: blob_bytes(code['blobs'][f['sha256']]) for f in head['files']},
                             {'a.py': b'new a', 'b.py': b'new b'})

    def test_code_only_change_stage_commit_branch_and_git_roundtrip(self):
        from contexthub.git_bridge import export_git, import_git
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ws = Workspace.init(root / 'ws', 'Code project')
            code = repository()
            ws.add(pack(code)); first = ws.commit('First')
            self.assertEqual(read_package(ws.package_bytes())['code'], code)
            self.assertFalse(ws.status()['modified'])
            ws.branch('original')
            changed = repository({'app.py': b'print("changed")\n'}, previous=code)
            ws.add(pack(changed))
            self.assertTrue(ws.status()['staged'])
            self.assertEqual(ws.diff(staged=True)['code']['summary']['files_modified'], 1)
            ws.commit('Code only')
            self.assertEqual(read_package(ws.package_bytes())['code'], changed)
            export_git(ws, root / 'git')
            import_git(root / 'git', root / 'roundtrip')
            self.assertEqual(read_package(Workspace(root / 'roundtrip').package_bytes())['code'], changed)
            ws.switch('original')
            self.assertEqual(read_package(ws.package_bytes())['code'], code)


class CodeServerTests(unittest.TestCase):
    setUp = test_server.ServerTests.setUp
    tearDown = test_server.ServerTests.tearDown
    request = test_server.ServerTests.request
    register = test_server.ServerTests.register

    def test_public_read_private_acl_pinned_files_diff_and_download_auth(self):
        self.register()
        original = repository(bindings=[{'conversation_id': 'c1', 'message_id': 'm2', 'kind': 'captured'}])
        status, item = self.request('POST', '/api/packages?visibility=public', pack(original))
        self.assertEqual(status, 201, item)
        ident, version = item['id'], item['version']['id']
        prefix = f'/api/packages/{ident}/code'
        status, summary = self.request('GET', prefix, auth=False)
        self.assertEqual(status, 200, summary)
        self.assertNotIn('blobs', summary)
        status, file = self.request('GET', prefix + '/file?path=app.py', auth=False)
        self.assertEqual(status, 200, file)
        self.assertIn('hello', file['content'])
        self.assertEqual(self.request('GET', f'/api/packages/{ident}/download', auth=False)[0], 401)
        changed = repository({'app.py': b'print("changed")\n'}, previous=original)
        status, second = self.request('POST', f'/api/packages/{ident}/push?base_version={version}&message=Code', pack(changed))
        self.assertEqual(status, 201, second)
        self.assertEqual(self.request('GET', prefix + '/diff', auth=False)[1]['summary']['files_modified'], 1)
        self.assertIn('hello', self.request('GET', prefix + f'/file?path=app.py&version={version}', auth=False)[1]['content'])
        self.assertEqual(self.request('GET', prefix + '/file?path=../library.json')[0], 422)
        status, fork = self.request('POST', f'/api/packages/{ident}/fork', {'version_id': second['version']['id'], 'visibility': 'private'})
        self.assertEqual(status, 201, fork)
        private_prefix = f"/api/packages/{fork['id']}/code"
        for suffix in ('', '/file?path=app.py', '/diff'):
            self.assertEqual(self.request('GET', private_prefix + suffix, auth=False)[0], 404)
        self.register('bob')
        self.assertEqual(self.request('GET', private_prefix)[0], 404)


class CodeCliTests(unittest.TestCase):
    setUp = test_ctx.CtxTests.setUp
    tearDown = test_ctx.CtxTests.tearDown
    command = test_ctx.CtxTests.command
    login = test_ctx.CtxTests.login

    def test_default_add_carries_dirty_code_through_push_clone_and_restore(self):
        directory = self.root / 'project'
        self.command('init', directory)
        subprocess.run(['git', 'init', '-b', 'main', str(directory)], check=True, capture_output=True)
        (directory / 'app.py').write_text('print("uncommitted")\n')
        (directory / '.gitignore').write_text('ignored.txt\n')
        (directory / 'ignored.txt').write_text('keep local')
        (directory / '.env').write_text('TOKEN=private\n')
        first = self.command('add', self.source, cwd=directory)
        self.assertEqual(first['status']['code']['files'], 2)
        self.command('commit', '-m', 'Capture working code', cwd=directory)
        (directory / 'app.py').write_text('print("changed")\n')
        self.command('add', '.', '--no-refresh', cwd=directory)
        self.assertEqual(self.command('diff', '--staged', cwd=directory)['code']['summary']['files_modified'], 1)
        self.command('commit', '-m', 'Code only change', cwd=directory)
        self.login()
        pushed = self.command('push', cwd=directory)
        clone = self.root / 'copy'
        self.command('clone', pushed['remote']['package_id'], clone)
        self.assertEqual(read_package(Workspace.find(clone).package_bytes())['code'],
                         read_package(Workspace.find(directory).package_bytes())['code'])
        restored = self.root / 'restored'
        self.command('restore', 'HEAD', '--to', restored, cwd=clone)
        self.assertEqual((restored / 'code/app.py').read_text(), 'print("changed")\n')
        self.assertFalse((restored / 'code/.env').exists())
        self.assertFalse((restored / 'code/ignored.txt').exists())

    def test_import_no_code_keeps_existing_snapshot(self):
        directory = self.root / 'workspace'
        self.command('init', directory)
        first = repository()
        source = self.root / 'embedded.contextpack'
        source.write_bytes(pack(first))
        self.command('add', source, cwd=directory)
        self.command('commit', '-m', 'Original', cwd=directory)
        changed = repository({'app.py': b'different\n'}, previous=first)
        source.write_bytes(pack(changed))
        self.command('add', source, '--no-code', cwd=directory)
        self.assertEqual(read_package(Workspace.find(directory).package_bytes('INDEX'))['code'], first)


if __name__ == '__main__':
    unittest.main()
