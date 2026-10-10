import errno
import hashlib
import multiprocessing
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from contexthub.contributions import Contributions
from contexthub.store import Store, StoreCapacityError
from tests.test_contributions_store import pack


MIB = 1024 * 1024


def _process_blob(root, limit, value, start, results):
    store = Store(root, max_storage_bytes=limit)
    start.wait(10)
    try:
        store._save_blob(value * 100000)
        results.put('saved')
    except StoreCapacityError:
        results.put('full')


class StorageBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, max_storage_bytes=50 * MIB)
        self.alice = self.store.register('alice', 'test-password-123')[0]['user']

    def tearDown(self):
        self.temp.cleanup()

    def refuse_sql(self):
        self.store.budget.max_storage_bytes = self.store.budget.usage_bytes() + 128 * 1024

    @unittest.skipUnless(os.name == 'nt', 'Windows case-insensitive directory aliases')
    def test_windows_case_aliases_share_reentrant_lock(self):
        alias = Store(Path(str(self.root).swapcase()), max_storage_bytes=50 * MIB)
        self.assertIs(self.store.budget._lock, alias.budget._lock)
        self.assertIs(self.store.budget._local, alias.budget._local)
        with self.store.db(), alias.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 1)

    def proposal(self):
        contributions = Contributions(self.store)
        bob = self.store.register('bob', 'test-password-123')[0]['user']
        target = self.store.add_package(self.alice, pack(), 'public')
        source = self.store.fork_package(target['id'], bob, target['version']['id'], 'public')
        source = self.store.add_version(source['id'], bob, pack('contributed'),
                                        source['version']['id'], 'Contribute question')
        proposal = contributions.create(source['id'], bob, source['version']['id'], 'Improve')
        return contributions, target, proposal

    def test_unlimited_and_budget_modes_share_native_cleanup_lock(self):
        with patch('contexthub.storage_budget.safe_fs.lock', wraps=__import__('contexthub.safe_fs', fromlist=['lock']).lock) as locking:
            store = Store(self.root)
            self.assertFalse(store.budget.enabled)
            self.assertEqual(store.list_packages(), [])
            self.assertTrue(locking.called)
        store = Store(self.root, min_free_bytes=1)
        with store.budget.hold(), store.budget.hold():
            self.assertTrue(store.budget.enabled)

    def test_invalid_configuration(self):
        for value in (-1, 0, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Store(self.root, max_storage_bytes=value)
        for value in (-1, None, True):
            with self.subTest(min_free=value), self.assertRaises(ValueError):
                Store(self.root, min_free_bytes=value)

    def test_registration_session_and_direct_cursor_writes_are_guarded(self):
        self.refuse_sql()
        with self.assertRaises(StoreCapacityError):
            self.store.register('refused', 'test-password-123')
        with self.assertRaises(StoreCapacityError):
            self.store.new_session(self.alice)
        with self.assertRaises(StoreCapacityError), self.store.db() as db:
            db.cursor().execute('UPDATE users SET username=? WHERE id=?', ('changed', self.alice['id']))
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT username FROM users').fetchone()[0], 'alice')
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)

    def test_registration_rolls_back_user_when_initial_session_cannot_be_saved(self):
        original_before_sql = self.store.budget.before_sql
        for index, refused_statement in enumerate(('DELETE FROM sessions', 'INSERT INTO sessions')):
            with self.subTest(statement=refused_statement):
                username = f'atomic{index}'
                with self.store.db() as db:
                    original_sessions = db.execute('SELECT count(*) FROM sessions').fetchone()[0]

                def refuse_session(connection, sql, parameters=()):
                    if sql.startswith(refused_statement):
                        raise StoreCapacityError('Initial session cannot be saved')
                    return original_before_sql(connection, sql, parameters)

                with patch.object(self.store.budget, 'before_sql', side_effect=refuse_session):
                    with self.assertRaises(StoreCapacityError):
                        self.store.register(username, 'test-password-123')
                with self.store.db() as db:
                    self.assertIsNone(db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone())
                    self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], original_sessions)
                registered, token = self.store.register(username, 'test-password-123')
                self.assertEqual(self.store.session(token)['user'], registered['user'])
                logged_in, login_token = self.store.login(username, 'test-password-123')
                self.assertEqual(self.store.session(login_token)['user'], logged_in['user'])

    def test_reserved_free_space_blocks_writes_but_keeps_reads_available(self):
        library = self.store.add_package(self.alice, pack(), 'private')
        self.store.budget.min_free_bytes = 5 * MIB
        with patch('contexthub.storage_budget.shutil.disk_usage',
                   return_value=SimpleNamespace(free=5 * MIB + 100)):
            with self.assertRaises(StoreCapacityError):
                self.store.new_session(self.alice)
            self.assertEqual(self.store.detail(library['id'], self.alice['id'])['title'], 'Shared library')
            with self.assertRaises(StoreCapacityError):
                self.store._save_blob(b'new blob')

    def test_denied_blob_and_actual_disk_full_leave_no_partial_file(self):
        self.store.budget.max_storage_bytes = self.store.budget.usage_bytes() + 8192
        with self.assertRaises(StoreCapacityError):
            self.store._save_blob(b'oversized' * 10000)
        self.assertEqual(list(self.store.blobs.iterdir()), [])
        self.store.budget.max_storage_bytes = 50 * MIB
        with patch('contexthub.store.os.fsync', side_effect=OSError(errno.ENOSPC, 'full')):
            with self.assertRaises(StoreCapacityError):
                self.store._save_blob(b'physical disk failure')
        self.assertEqual(list(self.store.blobs.iterdir()), [])

    def test_git_rejection_never_advances_version_or_public_ref(self):
        library = self.store.add_package(self.alice, pack(), 'public')
        changed = pack('a new question')
        original_ensure = self.store.budget.ensure

        def reject_git(amount):
            if amount >= MIB:
                raise StoreCapacityError('Git reservation refused')
            return original_ensure(amount)

        with patch.object(self.store.budget, 'ensure', side_effect=reject_git):
            with self.assertRaises(StoreCapacityError):
                self.store.add_version(library['id'], self.alice, changed,
                                       library['version']['id'], 'New question')
        self.assertEqual(self.store.detail(library['id'])['version']['id'], library['version']['id'])
        self.assertEqual(len(self.store.versions(library['id'])), 1)
        self.assertEqual(self.store._git('rev-parse', 'refs/heads/packages/' + library['id']), library['git_commit'])
        # Immutable objects from a refused transaction remain accounted for.
        self.assertTrue((self.store.blobs / (hashlib.sha256(changed).hexdigest() + '.contextpack')).exists())

    def test_contribution_candidate_and_comment_database_writes_are_guarded(self):
        contributions, target, proposal = self.proposal()
        original_ensure = self.store.budget.ensure

        def reject_git(amount):
            if amount >= MIB:
                raise StoreCapacityError('Git reservation refused')
            return original_ensure(amount)

        with patch.object(self.store.budget, 'ensure', side_effect=reject_git):
            with self.assertRaises(StoreCapacityError):
                contributions.check(proposal['id'], self.alice, target['version']['id'])
        self.assertIsNone(contributions.detail(proposal['id'])['check'])
        self.assertEqual(self.store.detail(target['id'])['version']['id'], target['version']['id'])
        self.refuse_sql()
        with self.assertRaises(StoreCapacityError):
            contributions.comment(proposal['id'], self.alice, 'Cannot persist at capacity')
        self.assertEqual(contributions.detail(proposal['id'])['comments'], [])

    def test_capacity_during_ref_sync_defers_ref_without_failing_committed_write(self):
        library = self.store.add_package(self.alice, pack(), 'public')
        original_git = self.store._git

        def refuse_ref(*args, **kwargs):
            if args[0] == 'update-ref':
                raise StoreCapacityError('Ref reservation refused')
            return original_git(*args, **kwargs)

        with patch.object(self.store, '_git', side_effect=refuse_ref), self.assertLogs('contexthub.store', 'WARNING'):
            changed = self.store.add_version(library['id'], self.alice, pack('committed'),
                                             library['version']['id'], 'Publish committed content')
        self.assertEqual(self.store.detail(library['id'])['version']['id'], changed['version']['id'])
        self.assertEqual(original_git('rev-parse', 'refs/heads/packages/' + library['id']), library['git_commit'])
        Store(self.root, max_storage_bytes=50 * MIB)
        self.assertEqual(original_git('rev-parse', 'refs/heads/packages/' + library['id']), changed['git_commit'])

    def test_soft_delete_retains_and_counts_immutable_data(self):
        library = self.store.add_package(self.alice, pack(), 'public')
        blobs = list(self.store.blobs.iterdir())
        git_files = [p for p in (self.root / 'public.git' / 'objects').rglob('*') if p.is_file()]
        self.store.delete(library['id'], self.alice['id'])
        self.assertTrue(all(path.exists() for path in blobs + git_files))
        stored_files = sum(self.store.budget._size(path.stat()) for path in blobs + git_files)
        self.assertGreaterEqual(self.store.budget.usage_bytes(), stored_files)
        self.assertEqual(self.store.list_packages(self.alice['id']), [])

    def test_two_store_threads_cannot_admit_two_blobs_against_one_remaining_slot(self):
        other = Store(self.root, max_storage_bytes=50 * MIB)
        limit = self.store.budget.usage_bytes() + 140000
        for store in (self.store, other):
            store.budget.max_storage_bytes = limit
        barrier = threading.Barrier(2)

        def write(store, value):
            barrier.wait(10)
            try:
                store._save_blob(value * 100000)
                return 'saved'
            except StoreCapacityError:
                return 'full'

        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(write, store, value)
                       for store, value in ((self.store, b'a'), (other, b'b'))]
            self.assertCountEqual([future.result(timeout=15) for future in futures], ['saved', 'full'])
        self.assertLessEqual(self.store.budget.usage_bytes(), limit)
        # A nested operation through another Store shares the reentrant lock.
        with self.store.db(), other.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 1)

    def test_two_processes_share_the_admission_lock(self):
        limit = self.store.budget.usage_bytes() + 140000
        context = multiprocessing.get_context('spawn')
        start, results = context.Event(), context.Queue()
        processes = [context.Process(target=_process_blob,
            args=(str(self.root), limit, value, start, results)) for value in (b'a', b'b')]
        try:
            for process in processes:
                process.start()
            start.set()
            self.assertCountEqual([results.get(timeout=15), results.get(timeout=15)], ['saved', 'full'])
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            self.assertLessEqual(self.store.budget.usage_bytes(), limit)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()

    def test_restart_at_budget_allows_existing_schema_and_reads(self):
        library = self.store.add_package(self.alice, pack(), 'private')
        limit = self.store.budget.usage_bytes()
        restarted = Store(self.root, max_storage_bytes=limit)
        self.assertEqual(restarted.detail(library['id'], self.alice['id'])['version']['id'], library['version']['id'])
        with self.assertRaises(StoreCapacityError):
            restarted.new_session(self.alice)
