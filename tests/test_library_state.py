import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from contexthub.contributions import Contributions
from contexthub.package import read_package
from contexthub.revisions import build_revision
from contexthub.store import Store, StoreConflict, StoreCapacityError
from tests.test_versions_store import pack
from tests import test_server


class LibraryStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.alice = self.store.register('alice', 'test-password-123')[0]['user']
        self.bob = self.store.register('bob', 'test-password-123')[0]['user']
        self.library = self.store.add_package(self.alice, pack(), 'public')

    def tearDown(self):
        self.temp.cleanup()

    def draft(self, revision=0, text='not yet public'):
        conversations = copy.deepcopy(self.library['conversations'])
        conversations[0]['messages'][0]['content'] = text
        return {'base_version': self.library['head_version_id'], 'title': 'Draft title',
                'description': 'private draft description', 'conversations': conversations,
                'message': '', 'expected_revision': revision}

    def test_draft_persists_privately_and_provenance_is_validated(self):
        ident = self.library['id']
        submitted = self.draft()
        submitted['conversations'][0]['source'] = {'platform': 'forged'}
        state = self.store.save_draft(ident, self.alice['id'], submitted)
        self.assertEqual(state['revision'], 1)
        self.assertEqual(state['draft']['conversations'][0]['source'], self.library['conversations'][0]['source'])
        self.assertIn('human_edited', state['draft']['conversations'][0]['messages'][0]['transformations'])
        self.assertEqual(Store(self.root).get_draft(ident, self.alice['id']), state)
        for user_id in (None, self.bob['id']):
            with self.assertRaises(FileNotFoundError):
                self.store.get_draft(ident, user_id)
            with self.assertRaises(FileNotFoundError):
                self.store.save_draft(ident, user_id, submitted)
        self.assertEqual(self.store.list_packages(query='not yet public'), [])
        self.assertNotIn('Draft title', self.store._git('log', '--all', '-p'))
        self.assertEqual(self.store.download(ident), pack())

    def test_draft_cas_no_aba_and_two_writers(self):
        ident = self.library['id']
        other = Store(self.root)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(s.save_draft, ident, self.alice['id'], self.draft()) for s in (self.store, other)]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except StoreConflict:
                    outcomes.append(None)
        self.assertEqual(sum(item is not None for item in outcomes), 1)
        with self.assertRaises(StoreConflict):
            self.store.delete_draft(ident, self.alice['id'], 0)
        self.assertEqual(self.store.delete_draft(ident, self.alice['id'], 1)['revision'], 2)
        self.assertEqual(other.get_draft(ident, self.alice['id']), {'draft': None, 'revision': 2})
        self.store.save_draft(ident, self.alice['id'], self.draft(2))
        with self.assertRaises(StoreConflict):
            other.save_draft(ident, self.alice['id'], self.draft(1))

    def test_publish_only_consumes_exact_draft_revision_and_retry_preserves_new_draft(self):
        ident = self.library['id']
        first = self.store.save_draft(ident, self.alice['id'], self.draft())
        latest = self.store.save_draft(ident, self.alice['id'], self.draft(1, 'other tab'))
        blob = build_revision(read_package(pack()), first['draft'])
        version = self.store.add_version(ident, self.alice, blob, self.library['head_version_id'], 'published', draft_revision=1)
        self.assertEqual(self.store.get_draft(ident, self.alice['id']), latest)
        with self.assertRaises(StoreConflict):
            self.store.save_draft(ident, self.alice['id'], self.draft(1))
        updated = self.draft(2, 'next draft')
        updated['base_version'] = version['head_version_id']
        latest = self.store.save_draft(ident, self.alice['id'], updated)
        self.store.add_version(ident, self.alice, blob, self.library['head_version_id'], 'published', draft_revision=1)
        self.assertEqual(self.store.get_draft(ident, self.alice['id']), latest)
        blob = build_revision(read_package(blob), latest['draft'])
        self.store.add_version(ident, self.alice, blob, version['head_version_id'], 'publish next', draft_revision=3)
        self.assertEqual(self.store.get_draft(ident, self.alice['id']), {'draft': None, 'revision': 4})

    def test_historical_base_draft_survives_head_advance_without_publishing(self):
        ident = self.library['id']
        before = self.store.save_draft(ident, self.alice['id'], self.draft())
        latest = self.store.add_version(ident, self.alice, pack('published elsewhere'),
                                       self.library['head_version_id'], 'CLI update')
        saved = self.store.save_draft(ident, self.alice['id'], self.draft(1, 'continued local editing'))
        self.assertEqual(saved['revision'], before['revision'] + 1)
        self.assertEqual(saved['draft']['base_version'], self.library['head_version_id'])
        self.assertEqual(saved['draft']['conversations'][0]['messages'][0]['content'], 'continued local editing')
        self.assertEqual(self.store.detail(ident)['head_version_id'], latest['head_version_id'])
        self.assertEqual(len(self.store.versions(ident)), 2)
        self.assertEqual(self.store.download(ident), pack('published elsewhere'))
        historical_blob = build_revision(read_package(pack()), saved['draft'])
        with self.assertRaises(StoreConflict):
            self.store.add_version(ident, self.alice, historical_blob,
                                   self.library['head_version_id'], 'stale publish', draft_revision=2)
        self.assertEqual(self.store.get_draft(ident, self.alice['id']), saved)
        other = self.store.add_package(self.alice, pack('other library'))
        for base in ('unknown', other['head_version_id']):
            with self.subTest(base=base), self.assertRaises(FileNotFoundError):
                self.store.save_draft(ident, self.alice['id'],
                                      {**self.draft(2), 'base_version': base})
        self.assertEqual(self.store.get_draft(ident, self.alice['id']), saved)

    def test_capacity_failure_does_not_overwrite_draft(self):
        store = Store(self.root, max_storage_bytes=50 * 1024**2)
        before = store.save_draft(self.library['id'], self.alice['id'], self.draft())
        store.budget.max_storage_bytes = store.budget.usage_bytes() + 4096
        with self.assertRaises(StoreCapacityError):
            store.save_draft(self.library['id'], self.alice['id'], self.draft(1, 'failed update'))
        self.assertEqual(store.get_draft(self.library['id'], self.alice['id']), before)

    def test_visibility_cas_acl_search_upstream_contributions_and_refs(self):
        ident, base = self.library['id'], self.library['head_version_id']
        contributions = Contributions(self.store)
        fork = self.store.fork_package(ident, self.bob, base, 'public')
        fork = self.store.add_version(fork['id'], self.bob, pack('contributed'), fork['head_version_id'], 'improvement')
        proposal = contributions.create(fork['id'], self.bob, fork['head_version_id'], 'Review me')
        contributions.check(proposal['id'], self.alice, base)
        with self.assertRaises(FileNotFoundError):
            self.store.set_visibility(ident, self.bob['id'], 'private', base)
        with self.assertRaises(StoreConflict):
            self.store.set_visibility(ident, self.alice['id'], 'private', 'stale')
        self.store.set_visibility(ident, self.alice['id'], 'private', base)
        for user_id in (None, self.bob['id']):
            with self.assertRaises(FileNotFoundError):
                self.store.detail(ident, user_id)
            with self.assertRaises(FileNotFoundError):
                contributions.detail(proposal['id'], user_id)
        self.assertEqual(contributions.list(fork['id']), [])
        self.assertIsNone(self.store.detail(fork['id'])['upstream'])
        self.assertNotIn(ident, self.store._git('for-each-ref', '--format=%(refname)'))
        self.assertEqual([row['id'] for row in self.store.list_packages()], [fork['id']])
        # The existing public fork remains a valid independent copy.
        self.assertEqual(self.store.download(fork['id']), pack('contributed'))
        self.store.set_visibility(ident, self.alice['id'], 'public', base)
        self.assertIsNone(contributions.detail(proposal['id'])['check'])
        checked = contributions.check(proposal['id'], self.alice, base)
        self.assertEqual(contributions.merge(proposal['id'], self.alice,
                         checked['check']['id'], checked['check']['candidate_oid'])['status'], 'merged')

    def test_private_history_not_implicitly_written_into_git_and_forks_cannot_publish(self):
        library = self.store.add_package(self.alice, pack('private first', 'Private'))
        fork = self.store.fork_package(library['id'], self.alice, library['head_version_id'])
        library = self.store.add_version(library['id'], self.alice, pack('current approved', 'Private'), library['head_version_id'], 'private explanation')
        self.store.set_visibility(library['id'], self.alice['id'], 'public', library['head_version_id'])
        published = self.store.detail(library['id'])
        self.assertEqual(self.store._git('show', '-s', '--format=%P', published['git_commit']), '')
        self.assertNotIn('private first', self.store._git('log', '--all', '-p'))
        # Library-level visibility deliberately exposes history through the API.
        self.assertEqual(len(self.store.versions(library['id'])), 2)
        with self.assertRaises(PermissionError):
            self.store.set_visibility(fork['id'], self.alice['id'], 'public', fork['head_version_id'])

    def test_publish_visibility_cas_preserves_private_draft_and_blocks_legacy_after_toggle(self):
        library = self.store.add_package(self.alice, pack('private base'))
        ident, base = library['id'], library['head_version_id']
        payload = {**self.draft(), 'base_version': base}
        draft = self.store.save_draft(ident, self.alice['id'], payload)
        blob = build_revision(read_package(pack('private base')), draft['draft'])
        Store(self.root).set_visibility(ident, self.alice['id'], 'public', base)
        for expected in (None, 'private'):
            with self.subTest(expected=expected), self.assertRaises(StoreConflict):
                self.store.add_version(ident, self.alice, blob, base, 'private editor publishing',
                                       draft_revision=1, expected_visibility=expected)
        self.assertEqual(len(self.store.versions(ident)), 1)
        self.assertEqual(self.store.get_draft(ident, self.alice['id']), draft)
        self.assertEqual(self.store.list_packages(query='not yet public'), [])
        saved = self.store.save_draft(ident, self.alice['id'], {**payload, 'expected_revision': 1})
        self.assertEqual(saved['revision'], 2)
        self.store.set_visibility(ident, self.alice['id'], 'private', base)
        # A legacy editor must not silently resume after a visibility round trip.
        with self.assertRaises(StoreConflict):
            self.store.add_version(ident, self.alice, blob, base, 'legacy no visibility')
        updated = self.store.add_version(ident, self.alice, blob, base, 'confirmed private',
                                         draft_revision=2, expected_visibility='private')
        self.assertEqual(updated['visibility'], 'private')
        self.assertEqual(updated['version_count'], 2)
        self.assertIsNone(self.store.get_draft(ident, self.alice['id'])['draft'])

    def test_purge_shared_blob_preserves_fork_and_removes_unique_data_and_metadata(self):
        ident, base = self.library['id'], self.library['head_version_id']
        fork = self.store.fork_package(ident, self.bob, base, 'public')
        secret = 'only_this_library_UNIQUE_PURGE_SENTINEL'
        updated = self.store.add_version(ident, self.alice, pack(secret), base, 'remove this version')
        self.store.save_draft(ident, self.alice['id'], {**self.draft(), 'base_version': updated['head_version_id']})
        result = self.store.purge(ident, self.alice['id'], ident)
        self.assertEqual(result['removed_blobs'], 1)
        self.assertEqual(result['retained_shared_blobs'], 1)
        self.assertFalse((self.store.blobs / (updated['sha256'] + '.contextpack')).exists())
        self.assertEqual(self.store.download(fork['id']), pack())
        self.assertIsNone(self.store.detail(fork['id'])['upstream'])
        with self.store.db() as db:
            for table, column in (('packages', 'id'), ('package_versions', 'package_id'), ('package_drafts', 'package_id'), ('package_platforms', 'package_id'), ('package_cleanup', 'package_id')):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table} WHERE {column}=?', (ident,)).fetchone()[0], 0)
        self.assertNotIn(secret.encode(), (self.root / 'hub.sqlite3').read_bytes())
        self.assertEqual(self.store._git('fsck', '--no-reflogs'), '')
        self.assertEqual(self.store._git('cat-file', '-t', self.library['git_commit']), 'commit')
        with self.assertRaises(Exception):
            self.store._git('cat-file', '-t', updated['git_commit'])

    def test_purge_retries_after_failure_and_preserves_other_soft_deleted_library(self):
        ident = self.library['id']
        retained = self.store.add_package(self.bob, pack('retained deleted library'), 'public')
        self.store.delete(retained['id'], self.bob['id'])
        with patch.object(self.store, '_prune_git', side_effect=OSError('simulated cleanup failure')):
            with self.assertRaises(OSError):
                self.store.purge(ident, self.alice['id'], ident)
        with self.assertRaises(FileNotFoundError):
            self.store.detail(ident, self.alice['id'])
        with self.assertRaises(FileNotFoundError):
            self.store.purge(ident, self.bob['id'], ident)
        self.assertTrue(Store(self.root).purge(ident, self.alice['id'], ident)['purged'])
        self.assertTrue((self.store.blobs / (retained['sha256'] + '.contextpack')).exists())
        self.assertEqual(self.store._git('cat-file', '-t', retained['git_commit']), 'commit')

    def test_purge_packed_git_and_other_live_contribution_candidate(self):
        contributions = Contributions(self.store)
        target = self.store.add_package(self.alice, pack('other base', 'Other'), 'public')
        source = self.store.fork_package(target['id'], self.bob, target['head_version_id'], 'public')
        source = self.store.add_version(source['id'], self.bob, pack('contributed', 'Other'), source['head_version_id'], 'improve')
        proposal = contributions.create(source['id'], self.bob, source['head_version_id'], 'preserve candidate')
        checked = contributions.check(proposal['id'], self.alice, target['head_version_id'])
        self.assertEqual(checked['check']['status'], 'ready')
        candidate = contributions.candidate(proposal['id'], check_id=checked['check']['id'])
        self.store._git('gc')
        self.assertTrue(list((self.root / 'public.git' / 'objects' / 'pack').glob('*.pack')))
        self.store.purge(self.library['id'], self.alice['id'], self.library['id'])
        self.assertEqual(contributions.candidate(proposal['id'], check_id=checked['check']['id']), candidate)
        merged = contributions.merge(proposal['id'], self.alice, checked['check']['id'], checked['check']['candidate_oid'])
        self.assertEqual(merged['status'], 'merged')
        with self.assertRaises(Exception):
            self.store._git('cat-file', '-t', self.library['git_commit'])

    def test_purge_removes_dependent_proposal_comments_and_candidate(self):
        contributions = Contributions(self.store)
        fork = self.store.fork_package(self.library['id'], self.bob, self.library['head_version_id'], 'public')
        fork = self.store.add_version(fork['id'], self.bob, pack('proposed'), fork['head_version_id'], 'improve')
        proposal = contributions.create(fork['id'], self.bob, fork['head_version_id'], 'remove private review text')
        contributions.comment(proposal['id'], self.alice, 'REMOVE_COMMENT_SENTINEL')
        renamed = self.store.add_version(self.library['id'], self.alice, pack(title='New target title'), self.library['head_version_id'], 'Rename target')
        checked = contributions.check(proposal['id'], self.alice, renamed['head_version_id'])
        candidate_path = self.store.blobs / (checked['check']['candidate_sha256'] + '.contextpack')
        self.assertTrue(candidate_path.exists())
        self.store.purge(self.library['id'], self.alice['id'], self.library['id'])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM contributions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM contribution_comments').fetchone()[0], 0)
        self.assertFalse(candidate_path.exists())
        self.assertNotIn(b'REMOVE_COMMENT_SENTINEL', (self.root / 'hub.sqlite3').read_bytes())
        self.assertEqual(self.store.download(fork['id']), pack('proposed'))

    def test_purge_excludes_concurrent_writes_even_without_budget(self):
        other = Store(self.root)
        started, release, writing = threading.Event(), threading.Event(), threading.Event()
        original = self.store._prune_git
        def pause(db):
            started.set()
            self.assertTrue(release.wait(10))
            original(db)
        def upload():
            writing.set()
            return other.add_package(self.bob, pack(), 'public')
        with patch.object(self.store, '_prune_git', side_effect=pause), ThreadPoolExecutor(2) as pool:
            cleanup = pool.submit(self.store.purge, self.library['id'], self.alice['id'], self.library['id'])
            self.assertTrue(started.wait(5))
            upload_future = pool.submit(upload)
            self.assertTrue(writing.wait(5))
            self.assertFalse(upload_future.done())
            release.set()
            self.assertTrue(cleanup.result(timeout=15)['purged'])
            item = upload_future.result(timeout=15)
        self.assertEqual(other.download(item['id']), pack())


class LibraryStateAPITests(unittest.TestCase):
    setUp = test_server.ServerTests.setUp
    tearDown = test_server.ServerTests.tearDown
    request = test_server.ServerTests.request
    register = test_server.ServerTests.register

    def test_draft_login_csrf_owner_and_conditional_publish(self):
        self.register()
        library = self.request('POST', '/api/packages?visibility=public', test_server.sample_pack())[1]
        endpoint = f"/api/packages/{library['id']}/draft"
        self.assertEqual(self.request('GET', endpoint, auth=False)[0], 401)
        payload = {'base_version': library['head_version_id'], 'title': 'Auto saved',
                   'description': '', 'conversations': library['conversations'],
                   'message': '', 'expected_revision': 0}
        self.assertEqual(self.request('POST', endpoint, payload, headers={'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('POST', endpoint, payload)[0], 200)
        self.assertEqual(self.request('POST', endpoint, payload)[0], 409)
        self.assertEqual(self.request('DELETE', endpoint, {'expected_revision': 1}, headers={'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('POST', f"/api/packages/{library['id']}/versions",
                         {**payload, 'message': 'publish draft', 'draft_revision': 1})[0], 201)
        self.assertEqual(self.request('GET', endpoint)[1], {'draft': None, 'revision': 2})
        self.register('bob')
        self.assertEqual(self.request('GET', endpoint)[0], 404)
        self.assertEqual(self.request('POST', endpoint, {**payload, 'expected_revision': 2})[0], 404)

    def test_visibility_and_purge_csrf_acl_confirmation(self):
        self.register()
        library = self.request('POST', '/api/packages?visibility=public', test_server.sample_pack())[1]
        url = f"/api/packages/{library['id']}"
        payload = {'visibility': 'private', 'base_version': library['head_version_id']}
        self.assertEqual(self.request('POST', url + '/visibility', payload, auth=False)[0], 401)
        self.assertEqual(self.request('POST', url + '/visibility', payload, headers={'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('POST', url + '/visibility', payload)[0], 200)
        self.assertEqual(self.request('GET', url, auth=False)[0], 404)
        self.assertEqual(self.request('POST', url + '/purge', {'confirmation': 'wrong'})[0], 422)
        self.assertEqual(self.request('POST', url + '/purge', {'confirmation': library['id']}, headers={'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('DELETE', url)[0], 200)
        self.assertEqual(self.request('POST', url + '/purge', {'confirmation': library['id']})[0], 200)

    def test_versions_and_push_check_editor_visibility(self):
        self.register()
        library = self.request('POST', '/api/packages', test_server.sample_pack())[1]
        url = f"/api/packages/{library['id']}"
        payload = {'base_version': library['head_version_id'], 'title': 'New content',
                   'description': '', 'conversations': library['conversations'], 'message': 'publish',
                   'expected_visibility': 'private'}
        self.assertEqual(self.request('POST', url + '/visibility',
                         {'visibility': 'public', 'base_version': library['head_version_id']})[0], 200)
        self.assertEqual(self.request('POST', url + '/versions', payload)[0], 409)
        legacy = {key: value for key, value in payload.items() if key != 'expected_visibility'}
        self.assertEqual(self.request('POST', url + '/versions', legacy)[0], 409)
        blob = build_revision(read_package(test_server.sample_pack()), payload)
        push = url + '/push?base_version=' + library['head_version_id'] + '&message=publish'
        self.assertEqual(self.request('POST', push, blob)[0], 409)
        self.assertEqual(self.request('POST', push + '&expected_visibility=private', blob)[0], 409)
        self.assertEqual(len(self.request('GET', url + '/versions')[1]['versions']), 1)
        self.assertEqual(self.request('POST', push + '&expected_visibility=public', blob)[0], 201)


if __name__ == '__main__':
    unittest.main()
