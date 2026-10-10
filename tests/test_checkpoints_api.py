"""Portable code associations follow version identity and existing access rules."""
import copy
import unittest
from urllib.parse import urlencode

from contexthub.checkpoints import attach_checkpoint
from contexthub.package import read_package
from tests import test_server as helpers


def checkpoint_blob(blob=None, commit='a' * 40):
    return attach_checkpoint(blob or helpers.sample_pack(), {
        'repository_url': 'https://github.com/example/synthetic',
        'commit': commit, 'tree': 'b' * 40, 'branch': 'main',
        'captured_at': '2026-10-10T00:00:00Z', 'verification': 'local_clean',
    })


class CheckpointAPITests(unittest.TestCase):
    setUp = helpers.ServerTests.setUp
    tearDown = helpers.ServerTests.tearDown
    request = helpers.ServerTests.request
    register = helpers.ServerTests.register

    def test_checkpoint_public_read_download_login_fork_and_history(self):
        self.register()
        blob = checkpoint_blob()
        status, first = self.request('POST', '/api/packages?visibility=public', blob)
        self.assertEqual(status, 201, first)
        path = '/api/packages/' + first['id']
        old = first['version']['id']
        status, public = self.request('GET', path, auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(public['code_checkpoint_status'], 'matched')
        self.assertEqual(public['code_checkpoint']['commit'], 'a' * 40)
        self.assertEqual(self.request('GET', path + '/download', auth=False)[0], 401)
        self.assertEqual(self.request('GET', path + '/download')[1], blob)
        edited = copy.deepcopy(first['conversations'])
        edited[0]['messages'][0]['content'] = 'Changed requirements need a new code checkpoint.'
        status, changed = self.request('POST', path + '/versions', {
            'base_version': old, 'title': first['title'], 'description': first['description'],
            'conversations': edited, 'message': 'new requirements',
        })
        self.assertEqual(status, 201, changed)
        self.assertEqual(changed['code_checkpoint_status'], 'stale')
        self.assertEqual(self.request('GET', path + '?version=' + old)[1]['code_checkpoint_status'], 'matched')
        self.register('bob')
        status, fork = self.request('POST', path + '/fork', {'version_id': old})
        self.assertEqual(status, 201, fork)
        self.assertEqual(fork['code_checkpoint'], first['code_checkpoint'])
        self.assertEqual(fork['code_checkpoint_status'], 'matched')

    def test_checkpoint_only_cli_push_roundtrip_and_private_acl(self):
        self.register()
        status, first = self.request('POST', '/api/packages?visibility=private', helpers.sample_pack())
        self.assertEqual(status, 201, first)
        self.assertEqual(first['code_checkpoint_status'], 'none')
        path = '/api/packages/' + first['id']
        candidate = checkpoint_blob()
        query = urlencode({'base_version': first['version']['id'], 'message': 'bind code',
                           'expected_visibility': 'private'})
        status, bound = self.request('POST', path + '/push?' + query, candidate)
        self.assertEqual(status, 201, bound)
        self.assertEqual(bound['code_checkpoint_status'], 'matched')
        self.assertEqual(self.request('GET', path + '/download')[1], candidate)
        self.assertEqual(read_package(candidate)['library']['code_checkpoint'], bound['code_checkpoint'])
        self.assertEqual(self.request('GET', path, auth=False)[0], 404)
        self.register('bob')
        self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('GET', path + '/download')[0], 404)

    def test_private_draft_keeps_explicit_checkpoint_replacement_and_clear(self):
        self.register()
        _, first = self.request('POST', '/api/packages?visibility=public', helpers.sample_pack())
        path = '/api/packages/' + first['id']
        checkpoint = read_package(checkpoint_blob())['library']['code_checkpoint']
        payload = {'base_version': first['version']['id'], 'expected_revision': 0,
                   'title': first['title'], 'description': first['description'],
                   'conversations': first['conversations'], 'message': 'linked draft',
                   'code_checkpoint': checkpoint}
        status, saved = self.request('POST', path + '/draft', payload)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved['draft']['code_checkpoint'], checkpoint)
        self.assertEqual(self.request('GET', path + '/draft', auth=False)[0], 401)
        self.assertIsNone(self.request('GET', path, auth=False)[1]['code_checkpoint'])
        recovered = self.request('GET', path + '/draft')[1]['draft']
        recovered['message'] = 'publish checkpoint'
        status, published = self.request('POST', path + '/versions', recovered)
        self.assertEqual(status, 201, published)
        self.assertEqual(published['code_checkpoint_status'], 'matched')
        payload.update(base_version=published['version']['id'], expected_revision=saved['revision'],
                       code_checkpoint=None)
        status, cleared = self.request('POST', path + '/draft', payload)
        self.assertEqual(status, 200, cleared)
        self.assertIsNone(cleared['draft']['code_checkpoint'])


if __name__ == '__main__':
    unittest.main()
