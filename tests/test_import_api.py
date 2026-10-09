import json
import http.client
import subprocess
import sys
import unittest
import threading
from urllib.parse import urlencode
from pathlib import Path
from unittest.mock import patch

from contexthub.package import build_package, read_package
from tests import test_server


def claude_export(attachments=False):
    message = {'uuid': 'a1', 'sender': 'assistant', 'text': '回答含中文与代码。'}
    if attachments:
        message['attachments'] = [{'file_name': 'private.pdf'}]
    return json.dumps([{'uuid': 'source-id', 'name': 'Claude 示例', 'chat_messages': [
        {'uuid': 'u1', 'sender': 'human', 'text': '如何保存？'}, message,
    ]}], ensure_ascii=False).encode()


class ImportAPITests(unittest.TestCase):
    setUp = test_server.ServerTests.setUp
    tearDown = test_server.ServerTests.tearDown
    request = test_server.ServerTests.request
    register = test_server.ServerTests.register

    def test_claude_preview_upload_catalog_and_authenticated_download(self):
        blob = claude_export()
        params = '?filename=conversations.json&platform=auto&mode=normalize'
        self.assertEqual(self.request('POST', '/api/inspect' + params, blob, auth=False)[0], 401)
        self.register()
        status, preview = self.request('POST', '/api/inspect' + params, blob)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview['platforms'], ['claude'])
        self.assertEqual(self.request('GET', '/api/packages')[1]['packages'], [])
        status, item = self.request('POST', '/api/packages' + params + '&visibility=public&confirm_import=1', blob)
        self.assertEqual(status, 201, item)
        self.assertEqual(item['platforms'], ['claude'])
        self.assertEqual(len(self.request('GET', '/api/packages?platform=claude', auth=False)[1]['packages']), 1)
        self.assertEqual(self.request('GET', '/api/packages?platform=deepseek', auth=False)[1]['packages'], [])
        self.assertEqual(self.request('GET', '/api/packages?platform=made-up')[0], 422)
        download = f"/api/packages/{item['id']}/download"
        self.assertEqual(self.request('GET', download, auth=False)[0], 401)
        exported = read_package(self.request('GET', download)[1])
        self.assertEqual(exported['conversations'][0]['messages'][1]['content'], '回答含中文与代码。')

    def test_conversion_warning_requires_explicit_confirmation(self):
        self.register()
        blob = claude_export(attachments=True)
        params = '?filename=conversations.json&platform=claude'
        status, preview = self.request('POST', '/api/inspect' + params, blob)
        self.assertEqual(status, 200, preview)
        self.assertTrue(preview['import_report']['requires_confirmation'])
        self.assertEqual(self.request('POST', '/api/packages' + params, blob)[0], 422)
        self.assertEqual(self.request('GET', '/api/packages')[1]['packages'], [])
        self.assertEqual(self.request('POST', '/api/packages' + params + '&confirm_import=1', blob)[0], 201)

    def test_unknown_format_is_only_archived_explicitly_and_remains_private(self):
        self.register()
        text = '\ufeff{"unrecognized":true}\r\n'
        blob = text.encode()
        params = '?filename=unknown.json&platform=deepseek'
        self.assertEqual(self.request('POST', '/api/inspect' + params, blob)[0], 422)
        status, preview = self.request('POST', '/api/inspect' + params + '&mode=archive', blob)
        self.assertEqual(status, 200, preview)
        self.assertTrue(preview['import_report']['original_preserved'])
        self.assertEqual(preview['conversations'][0]['messages'][0]['content'], text)
        self.assertEqual(self.request('POST', '/api/packages' + params + '&mode=archive', blob)[0], 422)
        status, item = self.request('POST', '/api/packages' + params + '&mode=archive&confirm_import=1', blob)
        self.assertEqual(status, 201, item)
        self.assertEqual(item['platforms'], ['deepseek'])
        self.assertEqual(item['visibility'], 'private')
        self.assertEqual(self.request('GET', '/api/packages?platform=deepseek', auth=False)[1]['packages'], [])
        self.register('another')
        self.assertEqual(self.request('GET', f"/api/packages/{item['id']}")[0], 404)

    def test_import_parameters_and_csrf_are_validated_without_writes(self):
        self.register()
        blob = claude_export()
        for query in ('?platform=deepseek', '?mode=wrong', '?platform=auto&platform=claude'):
            status, result = self.request('POST', '/api/inspect' + query, blob)
            self.assertEqual(status, 422, result)
        self.assertEqual(self.request('POST', '/api/inspect', blob, headers={'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('GET', '/api/packages')[1]['packages'], [])

    def test_cli_convert_and_native_upload_preserve_dialogue(self):
        self.register()
        source = Path(self.tmp.name) / 'claude.json'
        output = Path(self.tmp.name) / 'converted.contextpack'
        source.write_bytes(claude_export())
        command = [sys.executable, '-m', 'contexthub', 'convert', str(source), '-o', str(output)]
        converted = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(converted.returncode, 0, converted.stderr)
        self.assertEqual(read_package(output.read_bytes())['conversations'][0]['source']['platform'], 'claude')
        uploaded = subprocess.run([sys.executable, '-m', 'contexthub', 'upload', str(source),
                                   '--url', f'http://127.0.0.1:{self.server.server_port}',
                                   '--username', 'alice', '--password-stdin', '--confirm-import'],
                                  input='a-test-password-123\n', capture_output=True, text=True, timeout=20)
        self.assertEqual(uploaded.returncode, 0, uploaded.stderr)
        self.assertEqual(json.loads(uploaded.stdout)['platforms'], ['claude'])

    def test_unparsed_archive_cannot_start_a_codex_import(self):
        blob = build_package([{'source': {'kind': 'source_archive', 'platform': 'deepseek'},
                               'messages': [{'role': 'user', 'content': '{"unknown": true}'}]}], 'Archive')
        path = Path(self.tmp.name) / 'archive.contextpack'
        path.write_bytes(blob)
        result = subprocess.run([sys.executable, '-m', 'contexthub', 'import', str(path), '--cwd', self.tmp.name],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('原文存档', result.stderr)
        server = test_server.create_server(Path(self.tmp.name) / 'connector', port=0,
                                            mode='connector', token='synthetic-token')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch('contexthub.codex.CodexClient', side_effect=AssertionError('Must not start Codex')) as client:
                connection = http.client.HTTPConnection(*server.server_address, timeout=10)
                connection.request('POST', '/api/local/import', blob, {'X-ContextHub-Token': 'synthetic-token'})
                response = connection.getresponse()
                self.assertEqual(response.status, 422)
                self.assertIn('原文存档', response.read().decode())
                client.assert_not_called()
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_connector_modes_and_controlled_compatibility_errors(self):
        from contexthub.codex import CodexError, TimelineError
        blob = build_package([{'messages': [{'role': 'user', 'content': 'synthetic'}]}], 'Synthetic')
        server = test_server.create_server(Path(self.tmp.name) / 'modes', port=0,
                                           mode='connector', token='synthetic-token')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(mode=None):
            params = {'cwd': self.tmp.name}
            if mode is not None:
                params['mode'] = mode
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                connection.request('POST', '/api/local/import?' + urlencode(params), blob,
                                   {'X-ContextHub-Token': 'synthetic-token'})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            with patch('contexthub.codex.CodexClient') as client, patch('contexthub.codex.import_conversation') as importer:
                self.assertEqual(request('invalid')[0], 422)
                client.assert_not_called()
                importer.assert_not_called()
                for mode in (None, 'timeline', 'context'):
                    importer.return_value = {'mode': mode or 'timeline', 'verified': True}
                    status, body = request(mode)
                    self.assertEqual(status, 200)
                    self.assertEqual(importer.call_args.kwargs, {'mode': mode or 'timeline'})
                    self.assertTrue(body['results'][0]['verified'])
                importer.side_effect = TimelineError('请选择 --mode context')
                status, body = request()
                self.assertIn('--mode context', body['results'][0]['error'])
                importer.side_effect = CodexError('private upstream content must not leak')
                status, body = request()
                self.assertNotIn('private upstream', json.dumps(body))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
