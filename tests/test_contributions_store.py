import sqlite3
import subprocess
import tempfile
import threading
import unittest
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from contexthub.package import build_package, read_package
from contexthub.store import Store, StoreConflict
from contexthub.contributions import Contributions


def pack(first='original', second='original answer', title='Shared library'):
    return build_package([{'id': 'conversation', 'title': 'Discussion', 'messages': [
        {'id': 'm1', 'role': 'user', 'content': first},
        {'id': 'm2', 'role': 'assistant', 'content': second},
    ]}], title, source={'method': 'synthetic'})


class ContributionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.contributions = Contributions(self.store)
        self.alice = self.store.register('alice', 'test-password-123')[0]['user']
        self.bob = self.store.register('bob', 'test-password-123')[0]['user']
        self.carol = self.store.register('carol', 'test-password-123')[0]['user']
        self.target = self.store.add_package(self.alice, pack(), 'public')
        self.source = self.store.fork_package(self.target['id'], self.bob,
                                               self.target['version']['id'], 'public')
        self.source = self.store.add_version(self.source['id'], self.bob, pack('contributed'),
                                             self.source['version']['id'], 'Contribute a question')

    def tearDown(self):
        self.temp.cleanup()

    def proposal(self):
        return self.contributions.create(self.source['id'], self.bob,
                                         self.source['version']['id'], 'Improve the question', 'A fixed proposal')

    def checked(self):
        proposal = self.proposal()
        return self.contributions.check(proposal['id'], self.alice, self.target['version']['id'])

    def git(self, *args):
        return subprocess.run(['git', '--git-dir', str(self.root / 'public.git'), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def test_fixed_source_exact_candidate_publication_and_two_git_parents(self):
        proposal = self.checked()
        checked = proposal['check']
        self.assertEqual(checked['status'], 'ready')
        self.assertFalse(checked['stale'])
        candidate = self.contributions.candidate(proposal['id'], check_id=checked['id'])
        self.assertEqual(read_package(candidate)['conversations'][0]['messages'][0]['content'], 'contributed')
        # A newer source release does not change the revision offered in the proposal.
        latest_source = self.store.add_version(self.source['id'], self.bob, pack('not offered'),
                                              self.source['version']['id'], 'Unrelated later release')
        visible = self.contributions.detail(proposal['id'], self.alice['id'])
        self.assertFalse(visible['check']['stale'])
        self.assertEqual(visible['source']['head_version_id'], latest_source['version']['id'])
        merged = self.contributions.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
        self.assertEqual(merged['status'], 'merged')
        self.assertEqual(self.store.download(self.target['id']), candidate)
        target = self.store.detail(self.target['id'])
        self.assertEqual(target['git_commit'], checked['candidate_oid'])
        self.assertEqual(target['version']['parent_id'], self.target['version']['id'])
        self.assertEqual(self.git('show', '-s', '--format=%P', target['git_commit']).split(),
                         [self.target['git_commit'], self.source['git_commit']])
        self.assertEqual(self.git('rev-parse', 'refs/heads/packages/' + self.target['id']), checked['candidate_oid'])
        self.assertEqual(self.contributions.candidate(proposal['id'], check_id=checked['id']), candidate)
        self.assertEqual(self.store.download(self.target['id'], version_id=self.target['version']['id']), pack())

    def test_successful_retry_after_later_target_release_and_restart(self):
        proposal = self.checked()
        checked = proposal['check']
        result = self.contributions.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
        self.store.add_version(self.target['id'], self.alice, pack('new target'), result['merged_version_id'], 'Later')
        reopened = Contributions(Store(self.root))
        retried = reopened.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
        self.assertEqual(retried['merged_version_id'], result['merged_version_id'])
        self.assertEqual(len(self.store.versions(self.target['id'])), 3)
        self.assertEqual(reopened.candidate(proposal['id'], check_id=checked['id']),
                         self.store.download(self.target['id'], version_id=result['merged_version_id']))
        with self.assertRaises(StoreConflict):
            reopened.merge(proposal['id'], self.alice, checked['id'], '0' * 40)

    def test_stale_target_rejects_old_checks_and_rechecks_three_way_changes(self):
        proposal = self.checked()
        old = proposal['check']
        target = self.store.add_version(self.target['id'], self.alice, pack(second='upstream improvement'),
                                        self.target['version']['id'], 'Improve answer')
        stale = self.contributions.detail(proposal['id'], self.alice['id'])
        self.assertTrue(stale['check']['stale'])
        self.assertFalse(stale['permissions']['can_merge'])
        with self.assertRaises(StoreConflict):
            self.contributions.merge(proposal['id'], self.alice, old['id'], old['candidate_oid'])
        with self.assertRaises(StoreConflict):
            self.contributions.candidate(proposal['id'], check_id=old['id'])
        with self.assertRaises(StoreConflict):
            self.contributions.check(proposal['id'], self.alice, self.target['version']['id'])
        fresh = self.contributions.check(proposal['id'], self.alice, target['version']['id'])['check']
        candidate = read_package(self.contributions.candidate(proposal['id'], check_id=fresh['id']))
        self.assertEqual([m['content'] for m in candidate['conversations'][0]['messages']],
                         ['contributed', 'upstream improvement'])
        with self.assertRaises(StoreConflict):
            self.contributions.merge(proposal['id'], self.alice, old['id'], old['candidate_oid'])

    def test_conflicting_changes_cannot_be_published(self):
        proposal = self.proposal()
        target = self.store.add_version(self.target['id'], self.alice, pack('other question'),
                                        self.target['version']['id'], 'Conflicting change')
        result = self.contributions.check(proposal['id'], self.alice, target['version']['id'])
        checked = result['check']
        self.assertEqual(checked['status'], 'conflicts')
        self.assertGreater(checked['conflict_count'], 0)
        self.assertIsNone(checked['candidate_oid'])
        with self.assertRaises(StoreConflict):
            self.contributions.merge(proposal['id'], self.alice, checked['id'], '')
        self.assertEqual(len(self.store.versions(self.target['id'])), 2)

    def test_check_reuse_close_reopen_and_source_rebinding(self):
        proposal = self.checked()
        first = proposal['check']
        self.assertEqual(self.contributions.check(proposal['id'], self.alice, self.target['version']['id'])['check']['id'], first['id'])
        closed = self.contributions.set_status(proposal['id'], self.bob, 'closed')
        self.assertEqual(closed['status'], 'closed')
        self.assertIsNone(closed['check'])
        with self.assertRaises(StoreConflict):
            self.contributions.check(proposal['id'], self.alice, self.target['version']['id'])
        opened = self.contributions.set_status(proposal['id'], self.alice, 'open')
        self.assertEqual(opened['status'], 'open')
        checked = self.contributions.check(proposal['id'], self.alice, self.target['version']['id'])['check']
        replacement = self.store.add_version(self.source['id'], self.bob, pack('replacement proposal'),
                                             self.source['version']['id'], 'Replace offered revision')
        updated = self.contributions.update_source(proposal['id'], self.bob, replacement['version']['id'])
        self.assertIsNone(updated['check'])
        self.assertEqual(updated['source']['version_id'], replacement['version']['id'])
        with self.assertRaises(StoreConflict):
            self.contributions.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
        checked = self.contributions.check(proposal['id'], self.alice, self.target['version']['id'])['check']
        self.contributions.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
        for operation in (lambda: self.contributions.set_status(proposal['id'], self.bob, 'open'),
                          lambda: self.contributions.set_status(proposal['id'], self.alice, 'closed'),
                          lambda: self.contributions.update_source(proposal['id'], self.bob, self.source['version']['id'])):
            with self.assertRaises(StoreConflict):
                operation()

    def test_permissions_comments_and_input_bounds(self):
        proposal = self.proposal()
        self.assertTrue(proposal['permissions']['can_update'])
        self.assertFalse(proposal['permissions']['can_check'])
        anonymous = self.contributions.detail(proposal['id'])
        self.assertFalse(any(anonymous['permissions'].values()))
        updated = self.contributions.comment(proposal['id'], self.alice, 'Please include an example.')
        self.assertEqual(updated['comments'][0]['author'], 'alice')
        self.assertEqual(updated['comments'][0]['body'], 'Please include an example.')
        for operation in (
            lambda: self.contributions.create(self.source['id'], self.alice, self.source['version']['id'], 'No'),
            lambda: self.contributions.check(proposal['id'], self.bob, self.target['version']['id']),
            lambda: self.contributions.merge(proposal['id'], self.bob, 'absent', 'absent'),
            lambda: self.contributions.comment(proposal['id'], self.carol, 'No'),
            lambda: self.contributions.set_status(proposal['id'], self.carol, 'closed'),
            lambda: self.contributions.update_source(proposal['id'], self.alice, self.source['version']['id']),
        ):
            with self.assertRaises(PermissionError):
                operation()
        for invalid in ('', ' ', '字' * 4001, None):
            with self.assertRaises(ValueError):
                self.contributions.comment(proposal['id'], self.bob, invalid)
        for invalid in ('', ' ', '字' * 201):
            with self.assertRaises(ValueError):
                self.contributions.create(self.source['id'], self.bob, self.source['version']['id'], invalid)
        with self.assertRaises(ValueError):
            self.contributions.set_status(proposal['id'], self.bob, 'merged')
        with self.assertRaises(ValueError):
            self.contributions.create(self.source['id'], self.bob, self.source['version']['id'], 'Title', '字' * 10001)
        listed = self.contributions.list(self.target['id'])
        self.assertEqual([item['id'] for item in listed], [proposal['id']])
        self.assertEqual(self.contributions.list(self.source['id'])[0]['id'], proposal['id'])
        self.assertNotIn('comments', listed[0])
        self.assertNotIn('comparison', listed[0])

    def test_private_unrelated_unchanged_and_wrong_version_sources_are_rejected(self):
        private = self.store.fork_package(self.target['id'], self.bob, self.target['version']['id'])
        with self.assertRaises(FileNotFoundError):
            self.contributions.create(private['id'], self.bob, private['version']['id'], 'Private')
        with self.assertRaises(ValueError):
            self.contributions.create(self.target['id'], self.alice, self.target['version']['id'], 'Not a fork')
        initial_fork = self.store.versions(self.source['id'])[-1]
        with self.assertRaises(ValueError):
            self.contributions.create(self.source['id'], self.bob, initial_fork['id'], 'Unchanged fork')
        with self.assertRaises(FileNotFoundError):
            self.contributions.create(self.source['id'], self.bob, self.target['version']['id'], 'Wrong source version')

    def test_deleted_source_invalidates_every_operation_including_successful_retry(self):
        proposal = self.checked()
        checked = proposal['check']
        self.contributions.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
        self.store.delete(self.source['id'], self.bob['id'])
        for operation in (
            lambda: self.contributions.detail(proposal['id']),
            lambda: self.contributions.check(proposal['id'], self.alice, self.target['version']['id']),
            lambda: self.contributions.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid']),
            lambda: self.contributions.comment(proposal['id'], self.alice, 'No'),
            lambda: self.contributions.candidate(proposal['id'], check_id=checked['id']),
            lambda: self.contributions.set_status(proposal['id'], self.alice, 'open'),
        ):
            with self.assertRaises(FileNotFoundError):
                operation()
        self.assertEqual(self.contributions.list(self.target['id']), [])

    def test_deleted_target_or_fork_point_blocks_checked_content(self):
        proposal = self.checked()
        checked = proposal['check']
        with self.store.db() as db:
            db.execute('DELETE FROM package_versions WHERE id=?', (self.target['version']['id'],))
        with self.assertRaises(FileNotFoundError):
            self.contributions.detail(proposal['id'])
        with self.assertRaises(FileNotFoundError):
            self.contributions.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
        self.assertEqual(self.contributions.list(self.source['id']), [])

    def test_two_stores_merge_same_candidate_once(self):
        proposal = self.checked()
        checked = proposal['check']
        another = Contributions(Store(self.root))
        barrier = threading.Barrier(2)
        def merge(service):
            barrier.wait()
            return service.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(merge, [self.contributions, another]))
        self.assertEqual(outcomes[0]['merged_version_id'], outcomes[1]['merged_version_id'])
        self.assertEqual(len(self.store.versions(self.target['id'])), 2)

    def test_competing_candidates_cannot_overwrite_an_accepted_target(self):
        first, second = self.checked(), self.checked()
        another = Contributions(Store(self.root))
        barrier = threading.Barrier(2)
        def merge(args):
            service, proposal = args
            barrier.wait()
            try:
                checked = proposal['check']
                return service.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
            except StoreConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(merge, [(self.contributions, first), (another, second)]))
        self.assertEqual(sum(outcome is not None for outcome in outcomes), 1)
        self.assertEqual(len(self.store.versions(self.target['id'])), 2)
        self.assertEqual(sorted(self.contributions.detail(item['id'])['status'] for item in (first, second)),
                         ['merged', 'open'])

    def test_rules_change_invalidates_check_and_recheck_uses_new_rules(self):
        proposal = self.checked()
        original = proposal['check']
        with patch('contexthub.contributions.RULES_VERSION', 'test-rules-2'):
            self.assertTrue(self.contributions.detail(proposal['id'])['check']['stale'])
            with self.assertRaises(StoreConflict):
                self.contributions.merge(proposal['id'], self.alice, original['id'], original['candidate_oid'])
            with self.assertRaises(StoreConflict):
                self.contributions.candidate(proposal['id'], check_id=original['id'])
            revised = self.contributions.check(proposal['id'], self.alice, self.target['version']['id'])['check']
            self.assertNotEqual(revised['id'], original['id'])
            self.assertEqual(revised['rules_version'], 'test-rules-2')
            self.assertFalse(revised['stale'])

    def test_no_change_candidate_is_reported_without_a_new_version(self):
        proposal = self.proposal()
        same = self.store.add_version(self.target['id'], self.alice, pack('contributed'),
                                     self.target['version']['id'], 'Already adopted independently')
        checked = self.contributions.check(proposal['id'], self.alice, same['version']['id'])['check']
        self.assertEqual(checked['status'], 'unchanged')
        self.assertIsNone(checked['candidate_oid'])
        self.assertIsNone(checked['candidate_sha256'])
        self.assertEqual(checked['conflict_count'], 0)
        with self.assertRaises(StoreConflict):
            self.contributions.merge(proposal['id'], self.alice, checked['id'], '')
        self.assertEqual(len(self.store.versions(self.target['id'])), 2)

    def test_archive_representation_changes_do_not_create_semantic_contributions(self):
        def repackage(data):
            destination = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(destination, 'w') as output:
                for name in original.namelist():
                    output.writestr(name, original.read(name), compress_type=zipfile.ZIP_STORED)
            return destination.getvalue()
        # The source revision is legitimately a different ZIP but contributes no
        # different conversation or metadata from the recorded Fork point.
        equivalent = repackage(pack())
        self.assertNotEqual(equivalent, pack())
        changed_archive = self.store.add_version(self.source['id'], self.bob, equivalent,
                                                self.source['version']['id'], 'Archive encoding only')
        with self.assertRaises(ValueError):
            self.contributions.create(self.source['id'], self.bob, changed_archive['version']['id'], 'Empty contribution')
        # Equally, an upstream version with the offered semantic content needs
        # no merge just because the deterministic candidate ZIP differs.
        proposal = self.proposal()
        already = self.store.add_version(self.target['id'], self.alice, repackage(pack('contributed')),
                                         self.target['version']['id'], 'Already has the offered contents')
        checked = self.contributions.check(proposal['id'], self.alice, already['version']['id'])['check']
        self.assertEqual(checked['status'], 'unchanged')
        self.assertIsNone(checked['candidate_oid'])

    def test_public_dependency_withdrawal_and_comment_limit(self):
        proposal = self.proposal()
        with self.store.db() as db:
            db.executemany('INSERT INTO contribution_comments VALUES (?,?,?,?,?)',
                           [(str(i), proposal['id'], self.alice['id'], 'A bounded comment', '2026-10-09')
                            for i in range(200)])
        self.assertEqual(len(self.contributions.detail(proposal['id'])['comments']), 200)
        with self.assertRaises(ValueError):
            self.contributions.comment(proposal['id'], self.bob, 'Comment 201')
        with self.store.db() as db:
            db.execute("UPDATE packages SET visibility='private' WHERE id=?", (self.target['id'],))
        with self.assertRaises(FileNotFoundError):
            self.contributions.detail(proposal['id'], self.alice['id'])
        self.assertEqual(self.contributions.list(self.source['id']), [])

    def test_git_failure_during_check_keeps_proposal_unchecked(self):
        proposal = self.proposal()
        with patch.object(self.store, '_git_snapshot', side_effect=RuntimeError('simulated Git failure')):
            with self.assertRaises(RuntimeError):
                self.contributions.check(proposal['id'], self.alice, self.target['version']['id'])
        self.assertIsNone(self.contributions.detail(proposal['id'])['check'])
        self.assertEqual(len(self.store.versions(self.target['id'])), 1)
        self.assertEqual(self.git('rev-parse', 'refs/heads/packages/' + self.target['id']), self.target['git_commit'])

    def test_database_failure_does_not_publish_candidate_or_close_proposal(self):
        proposal = self.checked()
        checked = proposal['check']
        with self.store.db() as db:
            db.execute('''CREATE TRIGGER fail_merge BEFORE INSERT ON package_versions
                WHEN NEW.package_id='%s' AND NEW.number=2
                BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END''' % self.target['id'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.contributions.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
        self.assertEqual(self.store.get(self.target['id'])['head_version_id'], self.target['version']['id'])
        self.assertEqual(self.git('rev-parse', 'refs/heads/packages/' + self.target['id']), self.target['git_commit'])
        self.assertEqual(self.contributions.detail(proposal['id'])['status'], 'open')
        self.assertEqual(len(self.store.versions(self.target['id'])), 1)


if __name__ == '__main__':
    unittest.main()
