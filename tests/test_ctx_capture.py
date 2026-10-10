"""Direct Codex capture through the real workspace and HTTP Hub workflow."""
import contextlib
import copy
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from contexthub.package import read_package
from contexthub.workspace import Workspace
from tests.test_codex import FixtureClient, page, turn
from tests import test_ctx as ctx_helpers


def user(identifier, text):
    return {'type': 'userMessage', 'id': identifier,
            'content': [{'type': 'text', 'text': text}]}


class CtxCaptureTests(unittest.TestCase):
    setUp = ctx_helpers.CtxTests.setUp
    tearDown = ctx_helpers.CtxTests.tearDown
    command = ctx_helpers.CtxTests.command
    login = ctx_helpers.CtxTests.login

    @contextlib.contextmanager
    def codex(self, directory, turns, current=''):
        def handle(method, params):
            if method == 'thread/read':
                return {'thread': {'id': params['threadId'], 'name': 'Captured coding conversation',
                                   'historyMode': 'paginated', 'cwd': str(directory)}}
            if method == 'thread/turns/list':
                return page(copy.deepcopy(turns))
            if method == 'thread/list':
                return page([{'id': 'coding-thread', 'cwd': str(directory), 'name': 'Coding'}])
            self.fail('Unexpected Codex mutation: ' + method)
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': current}), \
                mock.patch('contexthub.capture.CodexClient') as client:
            client.return_value.__enter__.return_value = FixtureClient(handle)
            yield client

    def test_direct_capture_incremental_add_commit_push_clone_needs_no_export_file(self):
        directory = self.root / 'project'
        self.command('init', directory)
        turns = [turn('first', [user('request', 'Implement login')])]
        with self.codex(directory, turns):
            first = self.command('add', '--codex', cwd=directory)
            self.assertEqual(first['captures'][0]['message_count'], 1)
            self.assertEqual(len(first['status']['sources']), 1)
            self.command('commit', '-m', 'Capture the requirement', cwd=directory)
            original_head = Workspace.find(directory).head
            turns.append(turn('second', [{'type': 'agentMessage', 'id': 'reply', 'text': 'Login implemented'}]))
            self.command('add', '.', cwd=directory)
            self.assertEqual(Workspace.find(directory).head, original_head)
            self.assertEqual(self.command('diff', '--staged', cwd=directory)['summary']['messages_added'], 1)
            self.command('commit', '-m', 'Implementation', cwd=directory)
            again = self.command('add', '.', cwd=directory)
            self.assertFalse(again['status']['staged'])
            self.assertFalse(again['status']['modified'])
            self.assertEqual(len(self.command('log', cwd=directory)['commits']), 2)
        self.assertFalse(list(directory.glob('*.contextpack')))
        self.login()
        pushed = self.command('push', cwd=directory)
        self.assertEqual(pushed['pushed'], 2)
        self.assertEqual(pushed['remote']['visibility'], 'private')
        clone = self.root / 'copy'
        self.command('clone', pushed['remote']['package_id'], clone)
        self.assertEqual(self.command('status', cwd=clone)['sources'], [])
        self.assertEqual(read_package(Workspace.find(clone).package_bytes())['conversations'][0]['messages'],
                         read_package(Workspace.find(directory).package_bytes())['conversations'][0]['messages'])

    def test_bare_add_uses_current_conversation_and_excludes_its_executing_turn(self):
        directory = self.root / 'project'
        self.command('init', directory)
        turns = [turn('old', [user('past', 'Past request')]),
                 turn('executing', [user('now', 'Save this conversation')], 'interrupted')]
        with self.codex(directory, turns, current='coding-thread'):
            result = self.command('add', cwd=directory)
        self.assertTrue(result['captures'][0]['current_turn_excluded'])
        package = read_package(Workspace.find(directory).package_bytes('INDEX'))
        self.assertEqual([m['content'] for m in package['conversations'][0]['messages']], ['Past request'])

    def test_local_edits_can_be_staged_offline_without_silent_capture_overwrite(self):
        directory = self.root / 'project'
        self.command('init', directory)
        turns = [turn('first', [user('request', 'Original native text')])]
        with self.codex(directory, turns):
            self.command('add', '--codex', '--thread', 'coding-thread', cwd=directory)
            self.command('commit', '-m', 'Native capture', cwd=directory)
            path = next((directory / 'conversations').glob('*.json'))
            conversation = json.loads(path.read_text())
            conversation['messages'][0]['content'] = 'My curated text'
            path.write_text(json.dumps(conversation))
            original_bytes = path.read_bytes()
            error = self.command('add', '.', cwd=directory, expected=1)
            self.assertIn('--no-refresh', error['error'])
            self.assertEqual(path.read_bytes(), original_bytes)
        with mock.patch('contexthub.capture.CodexClient', side_effect=AssertionError('Offline staging must not open Codex')):
            self.command('add', '.', '--no-refresh', cwd=directory)
            self.command('commit', '-m', 'Curated text', cwd=directory)
            self.command('status', cwd=directory)
        with self.codex(directory, turns):
            self.command('add', '.', cwd=directory, expected=1)
            self.command('add', '.', '--replace', cwd=directory)
        package = read_package(Workspace.find(directory).package_bytes('INDEX'))
        self.assertEqual(package['conversations'][0]['messages'][0]['content'], 'Original native text')

    def test_codex_options_cannot_silently_ignore_an_import_file(self):
        directory = self.root / 'project'
        self.command('init', directory)
        with mock.patch('contexthub.capture.CodexClient', side_effect=AssertionError('Invalid command must not open Codex')):
            error = self.command('add', self.source, '--codex', cwd=directory, expected=1)
        self.assertIn('文件', error['error'])
        self.assertEqual(Workspace.find(directory).status()['conversations'], 0)


if __name__ == '__main__':
    unittest.main()
