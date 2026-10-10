"""Real offline Git interchange, including hostile repository data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from contexthub.git_bridge import export_git, import_git, GitBridgeError
from contexthub.package import read_package
from contexthub.workspace import Workspace, WorkspaceError
from tests.test_workspace_branches import pack


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class GitBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ws = Workspace.init(self.root / 'workspace', 'Synthetic')
        self.ws.add(pack()); self.ws.commit('initial')
        self.repo = self.root / 'git'
        self.target = self.root / 'restored'

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        result = subprocess.run(['git', '-c', 'user.name=Synthetic', '-c', 'user.email=test@example.invalid',
                                 '-c', 'commit.gpgSign=false', '-C', str(self.repo), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()

    def edit(self, index, text):
        path = next((self.ws.root / 'conversations').glob('*.json'))
        value = json.loads(path.read_text()); value['messages'][index]['content'] = text
        path.write_text(json.dumps(value)); self.ws.stage(); self.ws.commit(text)

    def test_roundtrip_keeps_all_branches_merge_parents_ids_and_exact_snapshots(self):
        self.ws.branch('feature'); self.edit(0, 'question')
        self.ws.switch('feature'); self.edit(1, 'answer'); self.ws.switch('main'); self.ws.merge('feature')
        (self.ws.root / 'private-project.txt').write_text('DO NOT EXPORT')
        result = export_git(self.ws, self.repo)
        self.assertEqual(result['commits'], 4)
        self.assertEqual(self.git('status', '--porcelain'), '')
        parents = self.git('rev-list', '--parents', '-n', '1', 'HEAD').split()
        self.assertEqual(len(parents), 3)
        self.assertFalse((self.repo / 'private-project.txt').exists())
        self.assertEqual(import_git(self.repo, self.target)['branches'], 2)
        restored = Workspace(self.target)
        self.assertEqual(restored.head, self.ws.head)
        self.assertEqual(restored.branches(), self.ws.branches())
        self.assertEqual(restored.history(), self.ws.history())
        for record in self.ws.history():
            self.assertEqual(restored.package_bytes(record['id']), self.ws.package_bytes(record['id']))

    def test_code_checkpoint_survives_git_history_roundtrip(self):
        from contexthub.checkpoints import attach_checkpoint, checkpoint_status
        blob = attach_checkpoint(self.ws.package_bytes(), {
            'repository_url': 'https://github.com/example/synthetic',
            'commit': 'a' * 40, 'tree': 'b' * 40, 'branch': 'main',
            'captured_at': '2026-10-10T00:00:00Z', 'verification': 'local_clean',
        })
        checkpoint = read_package(blob)['library']['code_checkpoint']
        self.ws.record_code_checkpoint(checkpoint, 'link exact code revision')
        export_git(self.ws, self.repo)
        import_git(self.repo, self.target)
        restored = Workspace(self.target)
        self.assertEqual(restored.package_bytes(), self.ws.package_bytes())
        self.assertEqual(checkpoint_status(read_package(restored.package_bytes())), 'matched')

    def test_standard_git_json_commit_imports_as_new_ctx_revision(self):
        export_git(self.ws, self.repo)
        path = next((self.repo / 'conversations').glob('*.json'))
        value = json.loads(path.read_text()); value['messages'][1]['content'] = 'edited using Git'
        path.write_text(json.dumps(value))
        (self.repo / 'README.md').write_text('Git project files must remain outside ctx')
        self.git('add', '.'); self.git('commit', '-m', 'ordinary Git edit')
        import_git(self.repo, self.target)
        restored = Workspace(self.target)
        self.assertEqual(len(restored.history()), 2)
        self.assertEqual(restored.history()[0]['parent'], self.ws.head)
        self.assertEqual(read_package(restored.package_bytes())['conversations'][0]['messages'][1]['content'], 'edited using Git')
        self.assertFalse((self.target / 'README.md').exists())
        self.assertFalse(restored.status()['modified'])

    def test_import_never_executes_repo_config_hooks_filters_or_reads_worktree_edits(self):
        export_git(self.ws, self.repo)
        marker = self.root / 'EXECUTED'
        payload = self.root / 'payload.sh'
        payload.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n'); payload.chmod(0o700)
        self.git('config', 'core.fsmonitor', str(payload))
        self.git('config', 'core.hooksPath', str(self.root))
        self.git('config', 'filter.evil.smudge', str(payload))
        # Repository configuration, hooks and uncommitted files are never used.
        (self.repo / 'library.json').write_text('uncommitted broken JSON')
        import_git(self.repo, self.target)
        self.assertFalse(marker.exists())
        self.assertEqual(Workspace(self.target).package_bytes(), self.ws.package_bytes())

    def test_malicious_managed_path_rejected_without_destination(self):
        export_git(self.ws, self.repo)
        (self.repo / 'conversations/evil.json').write_text('{}')
        self.git('add', '.'); self.git('commit', '-m', 'malicious extra managed path')
        with self.assertRaises(GitBridgeError):
            import_git(self.repo, self.target)
        self.assertFalse(self.target.exists())

    @unittest.skipIf(os.name == 'nt', 'requires local symlink permission')
    def test_symlinked_managed_file_rejected_without_following(self):
        export_git(self.ws, self.repo)
        (self.repo / 'library.json').unlink()
        (self.repo / 'library.json').symlink_to(self.root / 'secret')
        self.git('add', '.'); self.git('commit', '-m', 'symlink')
        with self.assertRaises(GitBridgeError):
            import_git(self.repo, self.target)
        self.assertFalse(self.target.exists())

    def test_no_overwrite_and_no_secret_hub_binding_transfer(self):
        remote = {'url': 'https://example.test', 'package_id': 'private', 'version_id': 'v1',
                  'local_commit': self.ws.head, 'visibility': 'private'}
        self.ws.mark_pushed(remote, self.ws.head)
        export_git(self.ws, self.repo)
        with self.assertRaises(GitBridgeError):
            export_git(self.ws, self.repo)
        import_git(self.repo, self.target)
        self.assertIsNone(Workspace(self.target).remote)
        self.assertNotIn(b'example.test', (self.repo / '.contexthub-git.json').read_bytes())
        with self.assertRaises(GitBridgeError):
            import_git(self.repo, self.target)

    def test_packed_git_refs_and_objects_roundtrip(self):
        export_git(self.ws, self.repo)
        self.git('gc')
        import_git(self.repo, self.target)
        self.assertEqual(Workspace(self.target).package_bytes(), self.ws.package_bytes())

    def test_unrelated_git_only_commit_preserves_topology_without_importing_files(self):
        export_git(self.ws, self.repo)
        (self.repo / 'README.md').write_text('not conversation data')
        self.git('add', '.'); self.git('commit', '-m', 'documentation only')
        import_git(self.repo, self.target)
        restored = Workspace(self.target)
        self.assertEqual(len(restored.history()), 2)
        self.assertEqual(restored.package_bytes(), self.ws.package_bytes())
        self.assertFalse((self.target / 'README.md').exists())

    def test_git_cli_export_and_import(self):
        import contextlib
        import io
        from contexthub import ctx
        for arguments, directory in ((['git', 'export', str(self.repo)], self.ws.root),
                                     (['git', 'import', str(self.repo), str(self.target)], self.root)):
            output = io.StringIO()
            with contextlib.chdir(directory), contextlib.redirect_stdout(output):
                self.assertEqual(ctx.main(['--json', *arguments]), 0)
            self.assertEqual(json.loads(output.getvalue())['format'], 'contexthub-git')
        self.assertEqual(Workspace(self.target).head, self.ws.head)


class DirectoryPublicationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows read-only deletion semantics')
    def test_failed_publication_cleans_read_only_git_files(self):
        from contexthub.git_bridge import _destination
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'result'
            with self.assertRaisesRegex(ValueError, 'original failure'):
                with _destination(target) as (staged, destination):
                    pack = staged / 'objects.pack'
                    pack.write_bytes(b'packed objects')
                    pack.chmod(0o400)
                    raise ValueError('original failure')
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_publish_with_guarded_parent_and_unicode_paths(self):
        from contexthub.git_bridge import _destination
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / '对话 Git result'
            with _destination(target) as (staged, destination):
                (staged / 'nested').mkdir()
                (staged / 'nested/data.json').write_text('{}')
            self.assertEqual((target / 'nested/data.json').read_text(), '{}')

    def test_publication_race_preserves_new_target_directory(self):
        from contexthub.git_bridge import _destination
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'result'
            with self.assertRaises(OSError):
                with _destination(target) as (staged, destination):
                    (staged / 'payload').write_text('data')
                    target.mkdir()  # Another process wins the name reservation.
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(list(Path(temporary).iterdir()), [target])
