"""HTTP acceptance for immutable versions, forks and authorisation."""
import copy
from tests import test_server as helpers
from contexthub.package import read_package
import unittest


class VersionAPITests(unittest.TestCase):
    setUp = helpers.ServerTests.setUp
    tearDown = helpers.ServerTests.tearDown
    request = helpers.ServerTests.request
    register = helpers.ServerTests.register

    def upload(self, visibility='public'):
        self.register()
        status, detail = self.request('POST', '/api/packages?visibility=' + visibility, helpers.sample_pack())
        self.assertEqual(status, 201, detail)
        return detail

    def revision(self, detail, text='改进后的合成回答。'):
        conversations = copy.deepcopy(detail['conversations'])
        conversations[0]['messages'][1]['content'] = text
        return {'base_version': detail['version']['id'], 'title': detail['title'],
                'description': detail['description'], 'conversations': conversations,
                'message': '补充回答并记录来源'}

    def test_preview_save_fixed_download_and_successful_retry(self):
        first = self.upload()
        ident = first['id']
        self.assertIn('version', first, 'Uploaded packages need an initial version')
        self.assertEqual(first['version']['number'], 1)
        self.assertEqual(first['head_version_id'], first['version']['id'])
        payload = self.revision(first)
        status, preview = self.request('POST', f'/api/packages/{ident}/preview', payload)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview['comparison']['summary']['messages_modified'], 1)
        self.assertEqual(len(self.request('GET', f'/api/packages/{ident}/versions')[1]['versions']), 1)
        status, second = self.request('POST', f'/api/packages/{ident}/versions', payload)
        self.assertEqual(status, 201, second)
        self.assertEqual(second['version']['number'], 2)
        self.assertEqual(second['version']['parent_id'], first['version']['id'])
        self.assertEqual(second['conversations'][0]['messages'][1]['content'], '改进后的合成回答。')
        self.assertIn('human_edited', second['conversations'][0]['messages'][1]['transformations'])
        status, repeated = self.request('POST', f'/api/packages/{ident}/versions', payload)
        self.assertEqual(status, 201, repeated)
        self.assertEqual(repeated['version']['id'], second['version']['id'])
        old_id, new_id = first['version']['id'], second['version']['id']
        self.assertEqual(self.request('GET', f'/api/packages/{ident}/download?version={old_id}', auth=False)[0], 401)
        status, old_blob = self.request('GET', f'/api/packages/{ident}/download?version={old_id}')
        self.assertEqual(status, 200)
        self.assertEqual(old_blob, helpers.sample_pack())
        status, old = self.request('GET', f'/api/packages/{ident}?version={old_id}', auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(old['version']['id'], old_id)
        self.assertEqual(old['head_version_id'], new_id)
        status, compared = self.request('GET', f'/api/packages/{ident}/compare?from={old_id}&to={new_id}', auth=False)
        self.assertEqual(status, 200, compared)
        self.assertEqual(compared['summary']['messages_modified'], 1)
        self.assertEqual(self.request('GET', f'/api/packages/{ident}/versions')[1]['versions'][0]['id'], new_id)
        newer = self.request('GET', f'/api/packages/{ident}/download')[1]
        self.assertEqual(read_package(newer)['conversations'][0]['messages'][1]['content'], '改进后的合成回答。')

    def test_stale_base_conflicts_and_invalid_snapshot_does_not_mutate(self):
        first = self.upload()
        self.assertIn('version', first)
        ident = first['id']
        initial = self.revision(first)
        second = self.request('POST', f'/api/packages/{ident}/versions', initial)[1]
        stale = self.revision(first, '另一浏览器的修改')
        self.assertEqual(self.request('POST', f'/api/packages/{ident}/versions', stale)[0], 409)
        self.assertEqual(self.request('POST', f'/api/packages/{ident}/preview', stale)[0], 409)
        malformed = self.revision(second)
        malformed['conversations'][0]['messages'][0]['role'] = 'system'
        self.assertEqual(self.request('POST', f'/api/packages/{ident}/versions', malformed)[0], 422)
        malformed = self.revision(second)
        malformed['message'] = ''
        self.assertEqual(self.request('POST', f'/api/packages/{ident}/preview', malformed)[0], 422)
        self.assertEqual(len(self.request('GET', f'/api/packages/{ident}/versions')[1]['versions']), 2)
        self.assertEqual(self.request('GET', f'/api/packages/{ident}')[1]['head_version_id'], second['version']['id'])

    def test_fork_pins_source_and_changes_only_own_library(self):
        source = self.upload()
        self.assertIn('version', source)
        source_version = source['version']['id']
        second = self.request('POST', f"/api/packages/{source['id']}/versions", self.revision(source))[1]
        self.register('bob')
        status, fork = self.request('POST', f"/api/packages/{source['id']}/fork", {'version_id': source_version, 'visibility': 'public'})
        self.assertEqual(status, 201, fork)
        self.assertNotEqual(fork['id'], source['id'])
        self.assertEqual(fork['upstream']['version_id'], source_version)
        self.assertEqual(fork['version']['number'], 1)
        self.assertEqual(fork['conversations'], source['conversations'])
        status, changed = self.request('POST', f"/api/packages/{fork['id']}/versions", self.revision(fork, 'Bob 的整理'))
        self.assertEqual(status, 201, changed)
        unchanged = self.request('GET', f"/api/packages/{source['id']}")[1]
        self.assertEqual(unchanged['head_version_id'], second['version']['id'])
        self.assertEqual(unchanged['conversations'], second['conversations'])
        self.assertEqual(self.request('POST', f"/api/packages/{source['id']}/versions", self.revision(second))[0], 404)

    def test_private_history_and_forks_check_every_object_and_csrf(self):
        private = self.upload('private')
        self.assertIn('version', private)
        ident, version = private['id'], private['version']['id']
        payload = self.revision(private)
        self.assertEqual(self.request('POST', f'/api/packages/{ident}/versions', payload, headers={'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('POST', f'/api/packages/{ident}/fork', {'version_id': version, 'visibility': 'public'})[0], 403)
        status, own_fork = self.request('POST', f'/api/packages/{ident}/fork', {'version_id': version})
        self.assertEqual(status, 201, own_fork)
        self.assertEqual(own_fork['visibility'], 'private')
        paths = [f'/api/packages/{ident}/versions', f'/api/packages/{ident}?version={version}',
                 f'/api/packages/{ident}/download?version={version}', f'/api/packages/{ident}/compare?from={version}&to={version}']
        for path in paths:
            self.assertEqual(self.request('GET', path, auth=False)[0], 401 if '/download?' in path else 404, path)
        self.register('bob')
        for path in paths:
            self.assertEqual(self.request('GET', path)[0], 404, path)
        for action, body in [('versions', payload), ('preview', payload), ('fork', {'version_id': version})]:
            self.assertEqual(self.request('POST', f'/api/packages/{ident}/{action}', body)[0], 404)

    def test_version_cannot_be_borrowed_from_another_library(self):
        first = self.upload()
        self.assertIn('version', first)
        second = self.request('POST', f"/api/packages/{first['id']}/fork", {'version_id': first['version']['id']})[1]
        a, b = first['version']['id'], second['version']['id']
        ident = first['id']
        for path in [f'/api/packages/{ident}?version={b}', f'/api/packages/{ident}/download?version={b}',
                     f'/api/packages/{ident}/compare?from={a}&to={b}']:
            self.assertEqual(self.request('GET', path)[0], 404, path)


if __name__ == '__main__':
    unittest.main()
