import hashlib
import sqlite3
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from contexthub.package import build_package
from contexthub.store import Store


def pack(text='first', title='Shared conversation'):
    return build_package([{'id': 'conversation', 'title': 'Discussion', 'messages': [
        {'id': 'm1', 'role': 'user', 'content': text},
    ]}], title, source={'method': 'synthetic'})


class VersionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.alice = self.store.register('alice', 'test-password-123')[0]['user']
        self.bob = self.store.register('bob', 'test-password-123')[0]['user']

    def tearDown(self):
        self.temp.cleanup()

    def public(self):
        return self.store.add_package(self.alice, pack(), 'public')

    def git(self, *args):
        return subprocess.run(['git', '--git-dir', str(self.root / 'public.git'), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def test_new_version_preserves_fixed_downloads_metadata_and_git_parent(self):
        first = self.public()
        first_id = first['version']['id']
        second = self.store.add_version(first['id'], self.alice, pack('second', 'Renamed'), first_id, 'Explain more')
        self.assertEqual(second['version']['number'], 2)
        self.assertEqual(second['version']['parent_id'], first_id)
        self.assertEqual(second['head_version_id'], second['version']['id'])
        self.assertEqual(second['version_count'], 2)
        self.assertEqual(second['library_source'], {'method': 'synthetic'})
        self.assertEqual(self.store.download(first['id'], version_id=first_id), pack())
        self.assertEqual(self.store.download(first['id']), pack('second', 'Renamed'))
        historical = self.store.detail(first['id'], version_id=first_id)
        self.assertEqual(historical['title'], 'Shared conversation')
        self.assertEqual(historical['head_version_id'], second['version']['id'])
        self.assertEqual([v['number'] for v in self.store.versions(first['id'])], [2, 1])
        self.assertEqual(self.git('rev-parse', second['git_commit'] + '^'), first['git_commit'])
        self.assertEqual(self.git('rev-parse', 'refs/heads/packages/' + first['id']), second['git_commit'])
        self.assertEqual(self.store.list_packages(query='second')[0]['title'], 'Renamed')
        self.assertEqual(self.store.list_packages(query='first'), [])

    def test_multiple_stores_compare_and_swap_and_successful_retry(self):
        from contexthub.store import StoreConflict
        first = self.public()
        other = Store(self.root)
        barrier = threading.Barrier(2)
        def write(store, text):
            barrier.wait()
            try:
                return store.add_version(first['id'], self.alice, pack(text), first['version']['id'], text)
            except StoreConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda args: write(*args), [(self.store, 'two'), (other, 'three')]))
        winners = [item for item in outcomes if item]
        self.assertEqual(len(winners), 1)
        winner = winners[0]
        self.assertEqual(len(self.store.versions(first['id'])), 2)
        text = winner['version']['message']
        third = self.store.add_version(first['id'], self.alice, pack('four'), winner['version']['id'], 'four')
        retry = other.add_version(first['id'], self.alice, pack(text), first['version']['id'], text)
        self.assertEqual(retry['version']['id'], winner['version']['id'])
        self.assertEqual(retry['head_version_id'], third['version']['id'])
        with self.assertRaises(StoreConflict):
            self.store.add_version(first['id'], self.alice, pack('different'), first['version']['id'], 'different')
        with self.assertRaises(ValueError):
            self.store.add_version(first['id'], self.alice, pack('four'), third['version']['id'], 'no change')

    def test_history_acl_and_cross_library_version_rejected(self):
        first = self.store.add_package(self.alice, pack())
        second = self.store.add_version(first['id'], self.alice, pack('secret'), first['version']['id'], 'private update')
        public = self.public()
        self.assertIsNone(second['git_commit'])
        for user_id in (None, self.bob['id']):
            for operation in (
                lambda: self.store.versions(first['id'], user_id),
                lambda: self.store.detail(first['id'], user_id, first['version']['id']),
                lambda: self.store.download(first['id'], user_id, first['version']['id']),
            ):
                with self.assertRaises(FileNotFoundError):
                    operation()
        with self.assertRaises(FileNotFoundError):
            self.store.detail(public['id'], self.alice['id'], first['version']['id'])
        with self.assertRaises(FileNotFoundError):
            self.store.add_version(public['id'], self.bob, pack('bad'), public['version']['id'], 'bad')
        with self.assertRaises(FileNotFoundError):
            self.store.add_version('absent', self.alice, b'invalid data', 'x', 'x')
        self.store.delete(first['id'], self.alice['id'])
        with self.assertRaises(FileNotFoundError):
            self.store.download(first['id'], self.alice['id'], first['version']['id'])

    def test_forks_are_independent_fixed_snapshots_and_hide_deleted_upstream(self):
        first = self.public()
        second = self.store.add_version(first['id'], self.alice, pack('next'), first['version']['id'], 'next')
        fork = self.store.fork_package(first['id'], self.bob, first['version']['id'], 'public')
        self.assertNotEqual(fork['id'], first['id'])
        self.assertEqual(fork['version_count'], 1)
        self.assertEqual(fork['upstream']['version_id'], first['version']['id'])
        self.assertEqual(fork['upstream']['number'], 1)
        self.assertEqual(self.store.download(fork['id']), pack())
        self.assertEqual(self.git('rev-parse', fork['git_commit'] + '^'), first['git_commit'])
        revised = self.store.add_version(fork['id'], self.bob, pack('fork changes'), fork['version']['id'], 'fork changes')
        self.assertEqual(self.store.download(first['id']), pack('next'))
        self.assertEqual(revised['upstream']['version_id'], first['version']['id'])
        self.store.delete(first['id'], self.alice['id'])
        for item in (self.store.detail(fork['id']), self.store.list_packages()[0]):
            self.assertIsNone(item['upstream'])
        self.assertEqual(self.store.download(fork['id'], version_id=fork['version']['id']), pack())

    def test_private_forks_never_enter_git_and_cannot_be_public(self):
        first = self.store.add_package(self.alice, pack())
        fork = self.store.fork_package(first['id'], self.alice, first['version']['id'])
        another = self.store.fork_package(first['id'], self.alice, first['version']['id'])
        self.assertNotEqual(fork['id'], another['id'])
        self.assertEqual(fork['visibility'], 'private')
        self.assertFalse((self.root / 'public.git').exists())
        with self.assertRaises(PermissionError):
            self.store.fork_package(first['id'], self.alice, first['version']['id'], 'public')
        with self.assertRaises(FileNotFoundError):
            self.store.fork_package(first['id'], self.bob, first['version']['id'])
        public = self.public()
        private_fork = self.store.fork_package(public['id'], self.bob, public['version']['id'])
        self.assertIsNone(private_fork['git_commit'])

    def test_git_failure_does_not_advance_database_head(self):
        first = self.public()
        with patch.object(self.store, '_git_snapshot', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                self.store.add_version(first['id'], self.alice, pack('new'), first['version']['id'], 'new')
        self.assertEqual(self.store.detail(first['id'])['head_version_id'], first['version']['id'])
        self.assertEqual(len(self.store.versions(first['id'])), 1)
        self.assertEqual(self.store.download(first['id']), pack())

    def test_database_failure_does_not_publish_already_written_git_objects(self):
        first = self.public()
        with self.store.db() as db:
            db.execute('''CREATE TRIGGER fail_versions BEFORE INSERT ON package_versions
                WHEN NEW.number=2 BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_version(first['id'], self.alice, pack('new'), first['version']['id'], 'new')
        self.assertEqual(self.store.detail(first['id'])['head_version_id'], first['version']['id'])
        self.assertEqual(len(self.store.versions(first['id'])), 1)
        self.assertEqual(self.git('rev-parse', 'refs/heads/packages/' + first['id']), first['git_commit'])

    def test_long_metadata_is_preserved_for_editing_and_message_limit_is_characters(self):
        data = build_package([{'id': 'c', 'messages': []}], '题' * 300, '描述' * 3000)
        first = self.store.add_package(self.alice, data)
        self.assertEqual(first['title'], '题' * 300)
        self.assertEqual(first['description'], '描述' * 3000)
        second = self.store.add_version(first['id'], self.alice, pack('change'), first['version']['id'], '字' * 2000)
        self.assertEqual(len(second['version']['message']), 2000)
        for message in ('', '   ', '字' * 2001):
            with self.assertRaises(ValueError):
                self.store.add_version(first['id'], self.alice, pack('another'), second['version']['id'], message)

    def test_database_is_authority_for_git_ref_recovery(self):
        first = self.public()
        second = self.store.add_version(first['id'], self.alice, pack('new'), first['version']['id'], 'new')
        self.git('update-ref', 'refs/heads/packages/' + first['id'], first['git_commit'])
        reopened = Store(self.root)
        self.assertEqual(reopened.detail(first['id'])['version']['id'], second['version']['id'])
        self.assertEqual(self.git('rev-parse', 'refs/heads/packages/' + first['id']), second['git_commit'])

    def test_legacy_database_migration_is_idempotent_and_keeps_bytes_and_commit(self):
        public = self.public()
        legacy_root = self.root / 'legacy'
        legacy_root.mkdir()
        (legacy_root / 'packages').mkdir()
        shutil.copytree(self.root / 'public.git', legacy_root / 'public.git')
        data = pack()
        sha = hashlib.sha256(data).hexdigest()
        (legacy_root / 'packages' / (sha + '.contextpack')).write_bytes(data)
        from contextlib import closing
        with closing(sqlite3.connect(legacy_root / 'hub.sqlite3')) as db, db:
            db.executescript('''
                CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE packages (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,title TEXT NOT NULL,description TEXT NOT NULL,
                    sha256 TEXT NOT NULL,visibility TEXT NOT NULL,conversation_count INTEGER NOT NULL,search_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,git_commit TEXT,deleted INTEGER NOT NULL DEFAULT 0);
            ''')
            db.execute('INSERT INTO users VALUES (?,?,?,?)', ('legacy-owner', 'legacy', '00', 'unused'))
            db.execute('INSERT INTO packages VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       ('legacy-id', 'legacy-owner', 'Old title', '', sha, 'private', 1, 'old text', '2026-01-01', None, 0))
            db.execute('INSERT INTO packages VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       ('legacy-public', 'legacy-owner', 'Old public', '', sha, 'public', 1, 'old text', '2026-01-02', public['git_commit'], 0))
        old = Store(legacy_root)
        migrated = old.detail('legacy-id', 'legacy-owner')
        self.assertEqual(migrated['version']['number'], 1)
        self.assertEqual(migrated['version']['created_at'], '2026-01-01')
        self.assertEqual(migrated['version']['sha256'], sha)
        self.assertEqual(old.download('legacy-id', 'legacy-owner'), data)
        published = old.detail('legacy-public')
        self.assertEqual(published['version']['git_commit'], public['git_commit'])
        self.assertEqual(published['version']['created_at'], '2026-01-02')
        self.assertEqual(old.download('legacy-public'), data)
        again = Store(legacy_root)
        self.assertEqual(again.detail('legacy-id', 'legacy-owner')['version']['id'], migrated['version']['id'])
        self.assertEqual(len(again.versions('legacy-id', 'legacy-owner')), 1)


if __name__ == '__main__':
    unittest.main()
