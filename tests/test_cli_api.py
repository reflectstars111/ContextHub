"""Raw CLI snapshots obey the same audit and ownership rules as web edits."""
import copy
import hashlib
import io
import unittest
import zipfile
from urllib.parse import urlencode, unquote

from contexthub.package import build_package, read_package
from contexthub.revisions import build_revision
from tests import test_server as helpers


class CLIPushAPITests(unittest.TestCase):
    setUp = helpers.ServerTests.setUp
    tearDown = helpers.ServerTests.tearDown
    request = helpers.ServerTests.request
    register = helpers.ServerTests.register

    def upload(self, visibility='private'):
        self.register()
        status, detail = self.request('POST', '/api/packages?' + unquote(urlencode({
            'visibility': visibility, 'message': '本地首个提交'})), helpers.sample_pack())
        self.assertEqual(status, 201, detail)
        self.assertEqual(detail['version']['message'], '本地首个提交')
        return detail

    def revision(self, detail, content='下一版本'):
        base = read_package(self.request('GET', f"/api/packages/{detail['id']}/download?version={detail['version']['id']}")[1])
        conversations = copy.deepcopy(base['conversations'])
        conversations[0]['messages'][1]['content'] = content
        return build_revision(base, {'conversations': conversations})

    def path(self, detail, **changes):
        query = {'base_version': detail['version']['id'], 'message': '本地提交'}
        query.update(changes)
        return f"/api/packages/{detail['id']}/push?" + unquote(urlencode(query))

    def test_bytes_hash_retry_and_stale_base(self):
        first = self.upload()
        blob = self.revision(first)
        status, second = self.request('POST', self.path(first), blob)
        self.assertEqual(status, 201, second)
        self.assertEqual(second['version']['sha256'], hashlib.sha256(blob).hexdigest())
        self.assertEqual(second['version']['parent_id'], first['version']['id'])
        self.assertEqual(self.request('GET', f"/api/packages/{first['id']}/download")[1], blob)
        third = self.request('POST', self.path(second), self.revision(second, '又一版'))[1]
        repeated = self.request('POST', self.path(first), blob)
        self.assertEqual(repeated[0], 201, repeated)
        self.assertEqual(repeated[1]['version']['id'], second['version']['id'])
        self.assertEqual(repeated[1]['head_version_id'], third['version']['id'])
        self.assertEqual(self.request('POST', self.path(first), self.revision(first, '旧分支'))[0], 409)

    def test_auth_ownership_csrf_and_cross_library_base(self):
        first = self.upload('public')
        blob, path = self.revision(first), self.path(first)
        self.assertEqual(self.request('POST', path, blob, auth=False)[0], 401)
        self.assertEqual(self.request('POST', path, blob, headers={'X-CSRF-Token': ''})[0], 403)
        fork = self.request('POST', f"/api/packages/{first['id']}/fork", {'version_id': first['version']['id']})[1]
        self.assertEqual(self.request('POST', self.path(first, base_version=fork['version']['id']), blob)[0], 404)
        self.register('bob')
        self.assertEqual(self.request('POST', path, blob)[0], 404)

    def test_forged_or_erased_edit_records_are_not_stored(self):
        first = self.upload()
        canonical = read_package(self.revision(first))
        conversations = copy.deepcopy(canonical['conversations'])
        conversations[0]['annotations'] = []
        conversations[0]['messages'][1]['transformations'] = []
        forged = build_package(conversations, canonical['library']['title'], canonical['library']['description'], source=canonical['library']['source'])
        self.assertEqual(self.request('POST', self.path(first), forged)[0], 422)
        self.assertEqual(self.request('POST', self.path(first, message=''), forged)[0], 422)
        self.assertEqual(self.request('POST', self.path(first) + '&message=duplicate', forged)[0], 422)
        self.assertEqual(self.request('GET', f"/api/packages/{first['id']}")[1]['head_version_id'], first['version']['id'])
        self.assertEqual(self.request('POST', '/api/packages?message=', helpers.sample_pack())[0], 201)  # absent default
        self.assertEqual(self.request('POST', '/api/packages?message=a&message=b', helpers.sample_pack())[0], 422)

    def test_portable_compression_preserves_hash_but_cannot_create_noop(self):
        def repack(blob):
            output = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(blob)) as source, zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as target:
                for name in reversed(source.namelist()):
                    target.writestr(name, source.read(name))
            return output.getvalue()
        first = self.upload()
        self.assertEqual(self.request('POST', self.path(first), repack(helpers.sample_pack()))[0], 422)
        original = self.revision(first)
        submitted = repack(original)
        self.assertNotEqual(original, submitted)
        status, second = self.request('POST', self.path(first), submitted)
        self.assertEqual(status, 201, second)
        self.assertEqual(second['version']['sha256'], hashlib.sha256(submitted).hexdigest())
        self.assertEqual(self.request('GET', f"/api/packages/{first['id']}/download")[1], submitted)


if __name__ == '__main__':
    unittest.main()
