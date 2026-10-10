"""Real CLI + HTTP + Git acceptance for sharing and restoring a code checkpoint."""
import subprocess
import unittest

from contexthub.package import read_package
from contexthub.workspace import Workspace
from tests import test_ctx as helpers


class CodeSyncHubTests(unittest.TestCase):
    setUp = helpers.CtxTests.setUp
    tearDown = helpers.CtxTests.tearDown
    command = helpers.CtxTests.command
    login = helpers.CtxTests.login
    make_workspace = helpers.CtxTests.make_workspace
    edit = helpers.CtxTests.edit

    def git(self, repository, *args):
        result = subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Synthetic',
                                 '-c', 'user.email=synthetic@example.invalid',
                                 '-c', 'commit.gpgSign=false', *args], capture_output=True,
                                text=True, check=True, timeout=20)
        return result.stdout.strip()

    def test_share_clone_restore_pair_then_reject_stale_conversation(self):
        self.login()
        conversations = self.make_workspace()
        first = self.command('push', cwd=conversations)['remote']
        code = self.root / 'project'
        code.mkdir()
        self.git(code, 'init', '-b', 'main')
        self.git(code, 'config', 'core.autocrlf', 'false')
        (code / 'app.py').write_text('print("checkpoint code")\n')
        self.git(code, 'add', '.')
        self.git(code, 'commit', '-m', 'checkpoint code')
        code_commit = self.git(code, 'rev-parse', 'HEAD')
        self.command('checkpoint', '--code', code, '-m', 'pair conversation and code', cwd=conversations)
        linked = self.command('push', cwd=conversations)['remote']
        self.assertEqual(linked['package_id'], first['package_id'])
        original_blob = Workspace(conversations).package_bytes()
        self.assertEqual(read_package(original_blob)['library']['code_checkpoint']['commit'], code_commit)

        # Moving code HEAD and leaving uncommitted work cannot change the
        # historical version shared through the Hub.
        (code / 'app.py').write_text('print("later committed code")\n')
        self.git(code, 'add', '.')
        self.git(code, 'commit', '-m', 'later code')
        (code / 'app.py').write_text('print("ongoing unsaved code")\n')
        code_head = self.git(code, 'rev-parse', 'HEAD')
        code_index = (code / '.git/index').read_bytes()
        recipient = self.root / 'recipient-conversations'
        self.command('clone', f"{self.hub}/?package={linked['package_id']}&version={linked['version_id']}", recipient)
        restored = self.root / 'paired-restore'
        self.command('restore', 'HEAD', '--code', code, '--to', restored, cwd=recipient)
        self.assertEqual((restored / 'code/app.py').read_text(), 'print("checkpoint code")\n')
        self.assertEqual(self.git(restored / 'code', 'rev-parse', 'HEAD'), code_commit)
        self.assertEqual(Workspace(restored / 'conversations').package_bytes(), original_blob)
        self.assertTrue((restored / 'receipt.json').is_file())
        self.assertEqual((code / 'app.py').read_text(), 'print("ongoing unsaved code")\n')
        self.assertEqual(self.git(code, 'rev-parse', 'HEAD'), code_head)
        self.assertEqual((code / '.git/index').read_bytes(), code_index)
        self.edit(recipient, 'New conversation without a matching code checkpoint')
        rejected = self.root / 'must-not-restore'
        self.command('restore', 'HEAD', '--code', code, '--to', rejected, cwd=recipient, expected=1)
        self.assertFalse(rejected.exists())


if __name__ == '__main__':
    unittest.main()
