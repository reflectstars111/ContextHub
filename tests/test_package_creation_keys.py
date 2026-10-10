"""Independent branch publication with owner-scoped creation request keys."""
import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from contexthub.store import Store, StoreConflict
from tests.test_versions_store import pack
from tests import test_server


class CreationKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.alice = self.store.register('alice', 'test-password-123')[0]['user']
        self.bob = self.store.register('bob', 'test-password-123')[0]['user']

    def tearDown(self):
        self.temp.cleanup()

    def test_distinct_keys_create_independent_libraries_and_same_key_is_idempotent(self):
        first = self.store.add_package(self.alice, pack(), new_library_key='a' * 32)
        second = self.store.add_package(self.alice, pack(), new_library_key='b' * 32)
        self.assertNotEqual(first['id'], second['id'])
        retry = Store(self.root).add_package(self.alice, pack(), new_library_key='a' * 32)
        self.assertEqual(retry['id'], first['id'])
        self.assertEqual(retry['version']['id'], first['version']['id'])
        same_key_other_owner = self.store.add_package(self.bob, pack(), new_library_key='a' * 32)
        self.assertNotEqual(same_key_other_owner['id'], first['id'])
        # Ordinary uploads retain their existing de-duplication behavior.
        legacy = self.store.add_package(self.alice, pack())
        self.assertIn(legacy['id'], (first['id'], second['id']))
        changed = self.store.add_version(second['id'], self.alice, pack('only branch B'),
                                         second['head_version_id'], 'B changes')
        self.assertEqual(changed['version_count'], 2)
        self.assertEqual(self.store.download(first['id'], self.alice['id']), pack())

    def test_same_key_rejects_changed_payload_or_visibility_and_changed_library_visibility(self):
        library = self.store.add_package(self.alice, pack(), new_library_key='a' * 32)
        for data, visibility in ((pack('different'), 'private'), (pack(), 'public')):
            with self.subTest(visibility=visibility), self.assertRaises(StoreConflict):
                self.store.add_package(self.alice, data, visibility, new_library_key='a' * 32)
        self.store.set_visibility(library['id'], self.alice['id'], 'public', library['head_version_id'])
        with self.assertRaises(StoreConflict):
            self.store.add_package(self.alice, pack(), new_library_key='a' * 32)
        for invalid in ('short', '../' + 'a' * 32, True, 'a' * 129):
            with self.subTest(key=invalid), self.assertRaises(ValueError):
                self.store.add_package(self.alice, pack(), new_library_key=invalid)

    def test_retry_returns_original_fixed_version_after_later_updates(self):
        first = self.store.add_package(self.alice, pack(), new_library_key='a' * 32)
        updated = self.store.add_version(first['id'], self.alice, pack('later'), first['head_version_id'], 'update')
        retry = self.store.add_package(self.alice, pack(), new_library_key='a' * 32)
        self.assertEqual(retry['version']['id'], first['version']['id'])
        self.assertEqual(retry['sha256'], first['sha256'])
        self.assertEqual(retry['head_version_id'], updated['head_version_id'])

    def test_two_stores_share_one_creation_request_and_purge_keeps_only_key_tombstone(self):
        other = Store(self.root)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda store: store.add_package(self.alice, pack(), new_library_key='a' * 32),
                                    (self.store, other)))
        self.assertEqual(results[0]['id'], results[1]['id'])
        retained = self.store.add_package(self.alice, pack(), new_library_key='b' * 32)
        ident = results[0]['id']
        self.store.delete(ident, self.alice['id'])
        with self.assertRaises(StoreConflict):
            self.store.add_package(self.alice, pack(), new_library_key='a' * 32)
        self.store.purge(ident, self.alice['id'], ident)
        with self.assertRaises(StoreConflict):
            Store(self.root).add_package(self.alice, pack(), new_library_key='a' * 32)
        self.assertEqual(self.store.download(retained['id'], self.alice['id']), pack())
        with self.store.db() as db:
            row = db.execute('SELECT * FROM package_create_requests WHERE owner_id=? AND key_hash=?',
                             (self.alice['id'], hashlib.sha256(('a' * 32).encode()).hexdigest())).fetchone()
        self.assertTrue(all(row[field] is None for field in ('package_id', 'version_id', 'sha256', 'visibility')))


class CreationKeyAPITests(unittest.TestCase):
    setUp = test_server.ServerTests.setUp
    tearDown = test_server.ServerTests.tearDown
    request = test_server.ServerTests.request
    register = test_server.ServerTests.register

    def test_upload_request_keys_are_forwarded_and_validated(self):
        self.register()
        endpoint = '/api/packages?new_library_key=' + 'a' * 32
        data = test_server.sample_pack()
        status, first = self.request('POST', endpoint, data)
        self.assertEqual(status, 201)
        self.assertEqual(self.request('POST', endpoint, data)[1]['id'], first['id'])
        other = self.request('POST', '/api/packages?new_library_key=' + 'b' * 32, data)[1]
        self.assertNotEqual(other['id'], first['id'])
        self.assertEqual(self.request('POST', endpoint + '&visibility=public', data)[0], 409)
        self.assertEqual(self.request('POST', endpoint + '&new_library_key=' + 'a' * 32, data)[0], 422)
        self.assertEqual(self.request('POST', '/api/packages?new_library_key=short', data)[0], 422)


if __name__ == '__main__':
    unittest.main()
