"""Synthetic schema, safety and independent-process readback contracts."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contexthub.codex import CodexError, import_conversation
from contexthub.codex_timeline import _rollout, _write_new


TEXT = [('user', '中文\n```py\nx=1\n```'), ('assistant', '回答\n保留空格  '),
        ('assistant', '第二条回答'), ('user', '下一轮')]
CONVERSATION = {'id': 'source', 'title': 'Imported', 'context': 'source instruction not imported',
                'messages': [{'role': role, 'content': text} for role, text in TEXT] +
                            [{'role': 'tool', 'content': 'NEVER_EXECUTE'}],
                'source': {'cwd': '/source/private', 'sandbox': 'danger-full-access',
                           'developerInstructions': 'NEVER_MIGRATE'}}


class Client:
    def __init__(self, home, *, version='0.153.4', mismatch=False):
        self.info = {'userAgent': 'context_hub/' + version + ' test', 'codexHome': str(home)}
        self.home, self.calls, self.reopens, self.mismatch = home, [], 0, mismatch

    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'config/read':
            return {'config': {'model_provider': 'target-fixture'}}
        if method in ('thread/name/set', 'thread/unsubscribe'):
            return {}
        if method not in ('thread/read', 'thread/resume'):
            raise AssertionError('Unexpected side effect: ' + method)
        identifier = params['threadId']
        paths = list(self.home.rglob('*' + identifier + '.jsonl'))
        assert len(paths) == 1
        records = [json.loads(line) for line in paths[0].read_text(encoding='utf-8').splitlines()]
        turns = []
        for row in records:
            if row['type'] != 'event_msg':
                continue
            payload = row['payload']
            if payload['type'] == 'task_started':
                turns.append({'id': payload['turn_id'], 'status': 'completed', 'items': []})
            if payload['type'] == 'user_message':
                turns[-1]['items'].append({'type': 'userMessage', 'content': [{'type': 'text', 'text': payload['message']}]})
            if payload['type'] == 'agent_message':
                turns[-1]['items'].append({'type': 'agentMessage', 'text': payload['message']})
        if self.mismatch:
            turns[0]['items'] = []
        return {'thread': {'id': identifier, 'historyMode': 'legacy', 'turns': turns}}

    def reopen(self):
        self.reopens += 1
        return contextlib.nullcontext(self)


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        self.work = self.root / '中文 project'
        self.work.mkdir()

    def test_default_timeline_exact_text_disk_read_resume_and_no_privilege_copy(self):
        existing = self.home / 'existing.jsonl'
        existing.write_bytes(b'untouched')
        client = Client(self.home)
        result = import_conversation(client, CONVERSATION, self.work)
        self.assertEqual(result['mode'], 'timeline')
        self.assertTrue(all(result[field] for field in ('verified', 'api_readback_verified', 'persistence_verified', 'resume_verified')))
        self.assertFalse(result['ui_history_verified'])
        self.assertFalse(result['model_context_verified'])
        self.assertEqual(result['message_count'], 4)
        self.assertEqual(client.reopens, 1)
        self.assertEqual(existing.read_bytes(), b'untouched')
        stored = Path(result['rollout_path']).read_text(encoding='utf-8')
        self.assertNotIn('NEVER_', stored)
        self.assertNotIn('/source/private', stored)
        self.assertNotIn('danger-full-access', stored)
        self.assertNotIn('source instruction', stored)
        records = [json.loads(line) for line in stored.splitlines()]
        self.assertEqual(records[0]['payload']['model_provider'], 'target-fixture')
        self.assertEqual(records[0]['payload']['cwd'], str(self.work))
        self.assertIsNone(records[0]['payload']['base_instructions'])
        self.assertEqual([(r['payload']['role'], r['payload']['content'][0]['text']) for r in records
                          if r['type'] == 'response_item'], TEXT)
        self.assertNotIn('turn/start', [method for method, _ in client.calls])
        self.assertNotIn('thread/inject_items', [method for method, _ in client.calls])
        self.assertNotEqual(result['thread_id'], CONVERSATION['id'])

    def test_unknown_version_refuses_before_config_or_disk_write(self):
        client = Client(self.home, version='0.154.0')
        with self.assertRaisesRegex(CodexError, '--mode context'):
            import_conversation(client, CONVERSATION, self.work)
        self.assertEqual(client.calls, [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_readback_mismatch_is_failed_without_silent_context_fallback(self):
        client = Client(self.home, mismatch=True)
        result = import_conversation(client, CONVERSATION, self.work)
        self.assertEqual(result['mode'], 'failed')
        self.assertFalse(result['verified'])
        self.assertEqual(client.reopens, 0)
        self.assertIn('error', result)
        self.assertNotIn('thread/inject_items', [method for method, _ in client.calls])

    def test_exclusive_create_does_not_replace_a_colliding_session(self):
        now = dt.datetime.now(dt.timezone.utc)
        path = _write_new(self.home, 'collision', b'old', now)
        with self.assertRaises(FileExistsError):
            _write_new(self.home, 'collision', b'new', now)
        self.assertEqual(path.read_bytes(), b'old')

    def test_write_failure_removes_only_new_file(self):
        existing = self.home / 'existing.jsonl'
        existing.write_bytes(b'old')
        with patch('contexthub.codex_timeline.os.fsync', side_effect=OSError('no space')):
            with self.assertRaises(OSError):
                _write_new(self.home, 'new', b'data', dt.datetime.now(dt.timezone.utc))
        self.assertEqual(list(self.home.rglob('rollout-*.jsonl')), [])
        self.assertEqual(existing.read_bytes(), b'old')

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink fixture; Windows adapter has native reparse tests')
    def test_home_and_cwd_symlinks_rejected_before_writes(self):
        link = self.root / 'link'
        link.symlink_to(self.home, target_is_directory=True)
        for client, cwd in ((Client(link), self.work), (Client(self.home), link)):
            with self.subTest(cwd=str(cwd), home=client.info['codexHome']):
                with self.assertRaises(OSError):
                    import_conversation(client, CONVERSATION, cwd)
                self.assertEqual(client.calls, [])

    def test_assistant_only_timeline_has_explicit_completed_turn(self):
        blob = _rollout('new', '0.153.4', self.work, 'fixture', [('assistant', 'text')], dt.datetime.now(dt.timezone.utc))
        records = [json.loads(line) for line in blob.splitlines()]
        self.assertEqual(records[1]['payload']['type'], 'task_started')
        self.assertEqual(records[-1]['payload']['type'], 'task_complete')

    def test_cli_defaults_to_timeline_and_context_remains_explicit(self):
        from contexthub.__main__ import parser
        self.assertEqual(parser().parse_args(['import', 'fixture.contextpack']).mode, 'timeline')
        self.assertEqual(parser().parse_args(['import', 'fixture.contextpack', '--mode', 'context']).mode, 'context')
