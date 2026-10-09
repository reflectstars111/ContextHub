"""Synthetic native exports cross the same inert contextpack boundary."""
import importlib
import io
import json
import stat
import unittest
import warnings
import zipfile

from contexthub.package import PackageError, build_package, read_package


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def zipped(entries):
    out = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, value in entries:
                archive.writestr(name, value)
    return out.getvalue()


def claude():
    return [{'uuid': 'private-id', 'name': '中文 Claude', 'chat_messages': [
        {'uuid': 'm1', 'sender': 'human', 'text': '你好'},
        {'uuid': 'm2', 'sender': 'assistant', 'text': 'fallback',
         'content': [{'type': 'text', 'text': '答复'}]},
    ]}]


def chatgpt():
    def node(parent, role, text):
        return {'parent': parent, 'message': {'author': {'role': role},
                'content': {'content_type': 'text', 'parts': [text]}}}
    return [{'id': 'native-gpt', 'title': 'Branch', 'current_node': 'chosen', 'mapping': {
        'root': {'parent': None, 'message': None},
        'user': node('root', 'user', 'Question'),
        'old': node('user', 'assistant', 'Wrong branch'),
        'chosen': node('user', 'assistant', 'Selected answer'),
    }}]


class ImporterTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('contexthub.importers')
        except ModuleNotFoundError:
            self.fail('Native conversation importer implementation is missing')

    def load(self, value, **kwargs):
        return self.module.import_upload(encoded(value), **kwargs)

    def test_existing_package_bytes_are_preserved_without_new_confirmation(self):
        raw = build_package([{'messages': [{'role': 'user', 'content': 'Hi'}]}], 'Original', source={'adapter': 'codex'})
        blob, package, report = self.module.import_upload(raw, filename='a.contextpack')
        self.assertEqual(blob, raw)
        self.assertEqual(package, read_package(raw))
        self.assertEqual(report['format'], 'contextpack')
        self.assertFalse(report['requires_confirmation'])
        self.assertTrue(report['original_preserved'])

    def test_claude_native_text_blocks_and_private_id_hashing(self):
        _, package, report = self.load(claude(), filename='conversations.json')
        conversation = package['conversations'][0]
        self.assertEqual([m['content'] for m in conversation['messages']], ['你好', '答复'])
        self.assertNotEqual(conversation['id'], 'private-id')
        self.assertEqual(conversation['source']['platform'], 'claude')
        self.assertEqual(report['platforms'], ['claude'])
        self.assertEqual(report['format'], 'claude-json')
        self.assertFalse(report['original_preserved'])

    def test_claude_empty_blocks_use_text_fallback_but_nonempty_blocks_win(self):
        for content in [None, []]:
            data = [{'chat_messages': [{'sender': 'human', 'text': 'Readable fallback', 'content': content}]}]
            _, package, _ = self.load(data)
            self.assertEqual(package['conversations'][0]['messages'][0]['content'], 'Readable fallback')
        data = [{'chat_messages': [{'sender': 'assistant', 'text': 'Ignored fallback', 'content': [{'type': 'text', 'text': 'Canonical blocks'}]}]}]
        _, package, _ = self.load(data)
        self.assertEqual(package['conversations'][0]['messages'][0]['content'], 'Canonical blocks')

    def test_conversation_level_attachments_are_reported_and_omitted(self):
        data = claude()
        data[0]['attachments'] = [{'content': 'private-file'}]
        _, package, report = self.load(data)
        self.assertTrue(report['partial'])
        self.assertIn('attachment', ' '.join(report['warnings']).lower())
        self.assertNotIn('private-file', json.dumps(package))

    def test_chatgpt_follows_current_node_and_warns_other_branches_omitted(self):
        _, package, report = self.load(chatgpt())
        self.assertEqual([m['content'] for m in package['conversations'][0]['messages']], ['Question', 'Selected answer'])
        self.assertEqual(report['platforms'], ['chatgpt'])
        self.assertTrue(report['partial'])
        self.assertTrue(report['requires_confirmation'])
        self.assertTrue(any('branch' in w.lower() for w in report['warnings']))

    def test_native_hidden_reasoning_and_tool_directed_messages_are_omitted(self):
        cases = [
            {'channel': 'analysis'}, {'channel': 'reasoning'}, {'channel': 'unknown'},
            {'metadata': {'channel': 'analysis'}},
            {'is_visually_hidden_from_conversation': True},
            {'metadata': {'is_visually_hidden_from_conversation': True}},
            {'metadata': {'is_user_system_message': True}},
            {'recipient': 'python'},
        ]
        for fields in cases:
            data = chatgpt()
            data[0]['mapping']['chosen']['message'].update(fields)
            data[0]['mapping']['chosen']['message']['content']['parts'] = ['PRIVATE INTERNAL TEXT']
            _, package, report = self.load(data)
            self.assertNotIn('PRIVATE INTERNAL TEXT', json.dumps(package))
            self.assertEqual([m['content'] for m in package['conversations'][0]['messages']], ['Question'])
            self.assertTrue(report['partial']); self.assertTrue(report['requires_confirmation'])
        for channel in [None, '', 'final', 'commentary']:
            _, package, _ = self.load({'messages': [{'role': 'assistant', 'channel': channel, 'recipient': 'all', 'content': 'visible'}]})
            self.assertEqual(package['conversations'][0]['messages'][0]['content'], 'visible')

    def test_generic_branch_fields_require_explicit_archive_instead_of_flattening(self):
        cases = [
            {'current_node': 'b', 'messages': [{'id': 'u', 'role': 'user', 'content': 'Q'}, {'id': 'a', 'parent_id': 'u', 'role': 'assistant', 'content': 'old'}, {'id': 'b', 'parent_id': 'u', 'role': 'assistant', 'content': 'new'}]},
            {'messages': [{'role': 'user', 'content': 'Q', 'parent_id': None}]},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(PackageError): self.load(value)

    def test_chatgpt_ambiguous_or_cyclic_branches_fail(self):
        cases = []
        data = chatgpt(); del data[0]['current_node']; cases.append(data)
        data = chatgpt(); data[0]['mapping']['user']['parent'] = 'chosen'; cases.append(data)
        data = chatgpt(); data[0]['current_node'] = 'absent'; cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(PackageError): self.load(data)

    def test_generic_role_content_requires_declared_platform_for_classification(self):
        _, package, report = self.load({'messages': [{'role': 'user', 'content': 'Hi'}, {'role': 'assistant', 'content': 'Hello'}]}, platform='deepseek', filename='claude.json')
        self.assertEqual(report['platforms'], ['deepseek'])
        self.assertEqual(package['conversations'][0]['source']['verification'], 'user_claimed')
        _, _, auto = self.load({'messages': [{'role': 'user', 'content': 'Hi'}]}, filename='claude.json')
        self.assertEqual(auto['platforms'], ['other'])

    def test_explicit_platform_conflict_rejected(self):
        with self.assertRaises(PackageError): self.load(claude(), platform='deepseek')

    def test_omitted_roles_media_reasoning_tools_each_reported(self):
        data = {'messages': [
            {'role': 'system', 'content': 'private instruction'},
            {'role': 'developer', 'content': 'private developer'},
            {'role': 'tool', 'content': 'tool output'},
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'visible'},
                {'type': 'image', 'source': {'data': 'secret-image'}},
                {'type': 'thinking', 'thinking': 'hidden'},
                {'type': 'tool_use', 'input': {'command': 'rm dangerous'}},
                {'type': 'document', 'source': {'data': 'secret-file'}},
            ]},
        ]}
        blob, package, report = self.load(data)
        self.assertEqual([m['content'] for m in package['conversations'][0]['messages']], ['visible'])
        warning = ' '.join(report['warnings']).lower()
        for term in ['system', 'developer', 'tool', 'image', 'reasoning', 'attachment']:
            self.assertIn(term, warning)
        self.assertTrue(report['partial']); self.assertTrue(report['requires_confirmation'])
        contents = json.dumps(package)
        for secret in ['secret-image', 'hidden', 'rm dangerous', 'secret-file', 'private instruction']:
            self.assertNotIn(secret, contents)

    def test_no_readable_messages_rejected_instead_of_silent_archive(self):
        for value in ({'unknown': 'data'}, {'messages': [{'role': 'system', 'content': 'skip'}]}):
            with self.subTest(value=value), self.assertRaises(PackageError): self.load(value)

    def test_deepseek_fragments_only_response_text_not_thinking(self):
        _, package, report = self.load({'title': 'DS', 'messages': [
            {'role': 'USER', 'content': 'Question'},
            {'role': 'ASSISTANT', 'fragments': [{'type': 'THINK', 'content': 'hidden'}, {'type': 'RESPONSE', 'content': 'Answer'}]},
        ]}, platform='deepseek')
        self.assertEqual([m['content'] for m in package['conversations'][0]['messages']], ['Question', 'Answer'])
        self.assertTrue(report['partial'])
        self.assertEqual(report['platforms'], ['deepseek'])

    def test_claude_code_jsonl_extracts_text_and_not_runtime_state(self):
        records = [
            {'type': 'user', 'sessionId': 'secret-session', 'uuid': 'u', 'message': {'role': 'user', 'content': 'Question'}},
            {'type': 'assistant', 'sessionId': 'secret-session', 'uuid': 'a', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Answer'}]}},
            {'type': 'progress', 'data': {'token': 'private-token'}},
        ]
        _, package, report = self.module.import_upload(b'\n'.join(encoded(v) for v in records), filename='session.jsonl')
        self.assertEqual(report['format'], 'claude-code-jsonl')
        self.assertEqual(report['platforms'], ['claude'])
        self.assertEqual([m['content'] for m in package['conversations'][0]['messages']], ['Question', 'Answer'])
        self.assertNotIn('private-token', json.dumps(package))
        self.assertTrue(report['partial'])

    def test_single_line_claude_code_jsonl_is_recognized(self):
        _, _, report = self.load({'type': 'user', 'sessionId': 's', 'message': {'role': 'user', 'content': 'Hello'}}, filename='one.jsonl')
        self.assertEqual(report['format'], 'claude-code-jsonl')

    def test_dsh_event_jsonl_is_partial_historical_text(self):
        records = [
            {'type': 'user/message', 'seq': 0, 'time': 1, 'data': {'id': 'u', 'role': 'user', 'content': [{'type': 'text', 'text': 'Question'}], 'source': {'kind': 'user'}}},
            {'type': 'assistant/message', 'seq': 1, 'time': 2, 'data': {'turn': 0, 'step': 0, 'message': {'id': 'a', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'Answer'}], 'source': {'kind': 'model', 'provider': 'deepseek', 'model': 'x'}}}},
        ]
        _, package, report = self.module.import_upload(b'\n'.join(encoded(v) for v in records), filename='session.jsonl')
        self.assertEqual(report['format'], 'deepseek-harness-jsonl')
        self.assertEqual(report['platforms'], ['deepseek'])
        self.assertEqual([m['content'] for m in package['conversations'][0]['messages']], ['Question', 'Answer'])
        self.assertTrue(report['partial']); self.assertTrue(report['requires_confirmation'])

    def test_archive_is_explicit_utf8_byte_preserving_and_not_redacted(self):
        raw = b'\xef\xbb\xbf{ "unknown": "password=secret-value" }\r\n'
        _, package, report = self.module.import_upload(raw, filename='C:\\Users\\private\\record.json', platform='deepseek', mode='archive')
        conversation = package['conversations'][0]
        self.assertEqual(conversation['messages'][0]['content'].encode('utf-8'), raw)
        self.assertEqual(conversation['source']['kind'], 'source_archive')
        self.assertEqual(conversation['source']['adapter'], 'text-archive')
        self.assertEqual(conversation['source']['title'], 'record.json')
        self.assertTrue(report['original_preserved']); self.assertTrue(report['requires_confirmation'])

    def test_archive_rejects_binary_zip_and_unsupported_extension(self):
        for data, filename in [(b'\xff', 'a.txt'), (b'PK\x03\x04junk', 'a.txt'), (b'text', 'a.html'), (b'text\x00binary', 'a.txt')]:
            with self.subTest(filename=filename), self.assertRaises(PackageError):
                self.module.import_upload(data, filename=filename, mode='archive')

    def test_zip_imports_conversations_only_and_warns_other_entries(self):
        raw = zipped([('conversations.json', encoded(claude())), ('user.json', b'{"email":"private-email"}'), ('files/picture.png', b'private-png')])
        _, package, report = self.module.import_upload(raw, filename='export.zip')
        self.assertEqual(report['platforms'], ['claude'])
        self.assertTrue(report['partial'])
        self.assertNotIn('private-email', json.dumps(package))
        self.assertTrue(any('account' in v.lower() or 'attachment' in v.lower() for v in report['warnings']))

    def test_zip_traversal_symlink_duplicate_and_ambiguous_payloads_rejected(self):
        link = zipfile.ZipInfo('link'); link.create_system = 3; link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for extra in [('../escape', b'x'), ('/absolute', b'x'), ('a\\b', b'x'), ('conversations.json', b'[]'), (link, b'x'), ('session.jsonl', b'{}')]:
            with self.subTest(extra=extra), self.assertRaises(PackageError):
                self.module.import_upload(zipped([('conversations.json', encoded(claude())), extra]))

    def test_manifest_zip_cannot_be_reinterpreted_as_native_export(self):
        with self.assertRaises(PackageError):
            self.module.import_upload(zipped([('manifest.json', b'{}'), ('conversations.json', encoded(claude()))]))

    def test_strict_json_rejects_duplicate_nan_invalid_unicode_and_depth(self):
        for raw in [b'{"messages":[],"messages":[]}', b'{"value":NaN}', b'{"value":"\\ud800"}', b'[' * 80 + b'0' + b']' * 80, b'\xff']:
            with self.subTest(raw=raw[:40]), self.assertRaises(PackageError): self.module.import_upload(raw, filename='x.json')

    def test_limits_fail_without_truncating_conversations_or_messages(self):
        for value in [[{'messages': [{'role': 'user', 'content': 'x'}]}] * 201,
                      {'messages': [{'role': 'user', 'content': 'x'}] * 2001}]:
            with self.subTest(count=len(value)), self.assertRaises(PackageError): self.load(value)
        with self.assertRaises(PackageError):
            self.module.import_upload(b'x' * (16 * 1024 * 1024 + 1), filename='large.txt', mode='archive')

    def test_normalized_single_conversation_round_trip_preserves_provenance(self):
        original = read_package(build_package([{'id': 'c', 'source': {'platform': 'claude'}, 'messages': [{'id': 'm', 'role': 'user', 'content': 'hello', 'provenance': 'human_authored'}]}], 'One'))['conversations'][0]
        for value in [original, [original]]:
            _, result, report = self.load(value)
            self.assertEqual(result['conversations'], [original])
            self.assertEqual(report['format'], 'contexthub-json')
            self.assertEqual(report['platforms'], ['claude'])

    def test_normalized_library_description_is_preserved(self):
        original = read_package(build_package([{'messages': [{'role': 'user', 'content': 'hello'}]}], 'One', 'Description'))
        _, result, _ = self.load(original)
        self.assertEqual(result['library'], original['library'])

    def test_dsh_export_zip_has_root_log_and_omits_subagents(self):
        records = [
            {'type': 'session', 'version': 4, 'id': 'native-session', 'createdAt': 1, 'isSeeded': False, 'delegationDepth': 0},
            {'type': 'user/message', 'seq': 0, 'time': 1, 'surfaceOp': 'append', 'data': {'id': 'u', 'role': 'user', 'source': {'kind': 'user'}, 'content': [{'type': 'text', 'text': 'Question'}]}},
        ]
        raw = zipped([('session.v4.jsonl', b'\n'.join(encoded(v) for v in records)), ('subagents/a/session.v4.jsonl', b'private child')])
        _, package, report = self.module.import_upload(raw, filename='dsh.zip')
        self.assertEqual(report['format'], 'deepseek-harness-jsonl')
        self.assertEqual(package['conversations'][0]['messages'][0]['content'], 'Question')
        self.assertNotIn('private child', json.dumps(package))
        records[0]['version'] = 999
        with self.assertRaises(PackageError):
            self.module.import_upload(b'\n'.join(encoded(v) for v in records), filename='session.jsonl')

    def test_mixed_platform_import_remains_mixed_and_is_deterministic(self):
        raw = encoded(claude() + chatgpt())
        one = self.module.import_upload(raw)
        two = self.module.import_upload(raw)
        self.assertEqual(one, two)
        self.assertEqual(one[2]['platforms'], ['chatgpt', 'claude'])

    def test_float_overflow_and_non_string_options_fail_as_package_errors(self):
        for raw in [b'{"messages":[{"role":"user","content":"x"}],"value":1e999}', b'{"messages":[{"role":"user","content":"x"}],"value":-1e999}']:
            with self.assertRaises(PackageError): self.module.import_upload(raw)
        for kwargs in [{'platform': []}, {'mode': []}, {'platform': None}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(PackageError):
                self.module.import_upload(encoded(claude()), **kwargs)

    def test_log_role_conflict_and_mixed_session_ids_rejected(self):
        for records in [[{'type': 'assistant', 'sessionId': 'a', 'message': {'role': 'user', 'content': 'Wrong role'}}],
                        [{'type': 'user', 'sessionId': value, 'message': {'role': 'user', 'content': value}} for value in ['a', 'b']]]:
            with self.subTest(records=records), self.assertRaises(PackageError):
                self.module.import_upload(b'\n'.join(encoded(v) for v in records), filename='session.jsonl')

    def test_malformed_log_event_type_is_rejected_without_internal_error(self):
        valid = {'type': 'user', 'sessionId': 's', 'message': {'role': 'user', 'content': 'question'}}
        for kind in [[], {}, None]:
            invalid = {**valid, 'type': kind}
            with self.subTest(kind=kind), self.assertRaises(PackageError):
                self.module.import_upload(b'\n'.join(encoded(v) for v in [invalid, valid]), filename='session.jsonl')

    def test_old_packed_dsh_and_multiple_headers_are_not_silently_dropped(self):
        header = {'type': 'session', 'version': 1, 'id': 's', 'createdAt': 1}
        for records in [[header, [0, 1, 'user/message', {}]],
                        [{**header, 'version': 4}, {**header, 'version': 4},
                         {'type': 'user/message', 'seq': 0, 'time': 1, 'data': {'role': 'user', 'source': {'kind': 'user'}, 'content': 'Hello'}}]]:
            with self.subTest(records=records), self.assertRaises(PackageError):
                self.module.import_upload(b'\n'.join(encoded(v) for v in records), filename='session.jsonl')

    def test_external_limits_do_not_reduce_existing_package_limit(self):
        from unittest.mock import patch
        raw = build_package([{'messages': [{'role': 'user', 'content': 'Existing large package'}]}], 'Existing')
        with patch.object(self.module, 'MAX_IMPORT_BYTES', 10):
            self.assertEqual(self.module.import_upload(raw, filename='a.contextpack')[0], raw)
            with self.assertRaises(PackageError): self.load(claude())

    def test_zip_bomb_and_entry_count_fail_before_read(self):
        from unittest.mock import patch
        with patch.object(self.module, 'MAX_TOTAL_BYTES', 1024):
            with self.assertRaises(PackageError):
                self.module.import_upload(zipped([('conversations.json', encoded(claude())), ('large.txt', b'x' * 2048)]))
        with patch.object(self.module, 'MAX_IMPORT_FILES', 1):
            with self.assertRaises(PackageError):
                self.module.import_upload(zipped([('conversations.json', encoded(claude())), ('user.json', b'{}')]))

    def test_normalized_json_preserves_ids_text_and_provenance(self):
        original = read_package(build_package([{'id': 'c', 'messages': [{'id': 'm', 'role': 'user', 'content': 'hello', 'provenance': 'human_authored'}]}], 'Normalized'))
        _, result, report = self.load({'library': original['library'], 'conversations': original['conversations']})
        self.assertEqual(result['conversations'], original['conversations'])
        self.assertEqual(result['library']['title'], 'Normalized')
        self.assertEqual(report['format'], 'contexthub-json')


if __name__ == '__main__':
    unittest.main()
