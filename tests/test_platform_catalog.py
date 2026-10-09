import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contexthub.contributions import Contributions
from contexthub.package import build_package, read_package
from contexthub.platforms import PLATFORMS, normalize_platform, package_platforms
from contexthub.storage_budget import StoreCapacityError
from contexthub.store import Store


def pack(*platforms, text='Question', library_source=None):
    conversations = [
        {'id': f'conversation-{index}', 'title': platform or 'Unspecified',
         'source': {'platform': platform} if platform else {},
         'messages': [{'id': 'question', 'role': 'user', 'content': text}]}
        for index, platform in enumerate(platforms or (None,))
    ]
    return build_package(conversations, text, source=library_source)


class PlatformDerivationTests(unittest.TestCase):
    def test_explicit_ids_and_bounded_aliases(self):
        self.assertEqual(tuple(PLATFORMS), ('codex', 'chatgpt', 'claude', 'deepseek', 'other'))
        for value, expected in [('Claude', 'claude'), ('DeepSeek', 'deepseek'),
                                (' OpenAI Codex ', 'codex'), ('ChatGPT', 'chatgpt'),
                                ('codex-cli', 'codex'), ('claude-code', 'claude'),
                                ('deepseek-harness', 'deepseek'), ('DeepSeek Harness', 'deepseek'),
                                ('dsh', 'deepseek'),
                                ('openai', 'other'), ('future-platform', 'other'),
                                (None, 'other'), ([], 'other')]:
            with self.subTest(value=value):
                self.assertEqual(normalize_platform(value), expected)

    def test_mixed_sources_priority_unknowns_and_library_fallback(self):
        sources = [
            {'platform': 'claude', 'adapter': 'codex', 'provider': 'openai'},
            {'adapter': 'codex', 'provider': 'openai'},
            {'provider': 'deepseek'},
            {'platform_claimed': 'ChatGPT'},
            {'provider': 'openai'},
            {'platform': 'future-platform'},
            {},
        ]
        package = {'conversations': [{'source': source} for source in sources],
                   'library': {'source': {'platform': 'claude'}}}
        self.assertEqual(package_platforms(package), list(PLATFORMS))
        self.assertEqual(package_platforms({'conversations': [{'source': {'provider': 'openai'}}],
                                           'library': {'source': {'platform': 'codex'}}}), ['other'])
        self.assertEqual(package_platforms({'conversations': [{'source': {'method': 'paste'}}],
                                           'library': {'source': {'provider': 'codex'}}}), ['codex'])
        self.assertEqual(package_platforms({'conversations': [], 'library': {'source': {}}}), ['other'])


class PlatformCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.alice = self.store.register('alice', 'test-password-123')[0]['user']
        self.bob = self.store.register('bob', 'test-password-123')[0]['user']

    def tearDown(self):
        self.temp.cleanup()

    def ids(self, user_id=None, query='', platform=None):
        return [item['id'] for item in self.store.list_packages(user_id, query, platform)]

    def test_mixed_catalog_filter_search_and_private_acl(self):
        mixed = self.store.add_package(self.alice, pack('claude', 'deepseek', 'claude', text='Shared'), 'public')
        private = self.store.add_package(self.alice, pack('claude', text='Private secret'))
        unknown = self.store.add_package(self.bob, pack(text='Unknown'), 'public')
        self.assertEqual(mixed['platforms'], ['claude', 'deepseek'])
        self.assertEqual(self.store.get(mixed['id'])['platforms'], ['claude', 'deepseek'])
        for platform in ('claude', 'deepseek'):
            self.assertEqual(self.ids(platform=platform), [mixed['id']])
        self.assertEqual(self.ids(platform='other'), [unknown['id']])
        self.assertEqual(self.ids(query='Private', platform='claude'), [])
        self.assertEqual(self.ids(self.bob['id'], platform='claude'), [mixed['id']])
        self.assertEqual(set(self.ids(self.alice['id'], platform='claude')), {mixed['id'], private['id']})
        for user_id in (None, self.bob['id']):
            with self.assertRaises(FileNotFoundError):
                self.store.get(private['id'], user_id)

    def test_versions_replace_catalog_while_history_and_fork_keep_sources(self):
        first = self.store.add_package(self.alice, pack('codex'), 'public')
        second = self.store.add_version(first['id'], self.alice, pack('deepseek'),
                                        first['version']['id'], 'Changed source')
        self.assertEqual(self.ids(platform='codex'), [])
        self.assertEqual(self.ids(platform='deepseek'), [first['id']])
        self.assertEqual(second['platforms'], ['deepseek'])
        self.assertEqual(self.store.get(first['id'])['platforms'], ['deepseek'])
        self.assertEqual(self.store.detail(first['id'], version_id=first['version']['id'])['platforms'], ['codex'])
        fork = self.store.fork_package(first['id'], self.bob, first['version']['id'], 'public')
        self.assertEqual(fork['platforms'], ['codex'])
        self.assertEqual(self.ids(platform='codex'), [fork['id']])
        self.store.delete(fork['id'], self.bob['id'])
        self.assertEqual(self.ids(platform='codex'), [])

    def test_existing_database_backfills_once_without_changing_versions_or_bytes(self):
        old = self.store.add_package(self.alice, pack(library_source={'adapter': 'codex'}))
        public = self.store.add_package(self.alice, pack('claude'), 'public')
        data = self.store.download(old['id'], self.alice['id'])
        with self.store.db() as db:
            db.execute('DROP TABLE package_platforms')
        migrated = Store(self.root)
        self.assertEqual(migrated.get(old['id'], self.alice['id'])['platforms'], ['codex'])
        self.assertEqual(migrated.list_packages(platform='claude')[0]['id'], public['id'])
        self.assertEqual(migrated.download(old['id'], self.alice['id']), data)
        self.assertEqual(migrated.detail(old['id'], self.alice['id'])['version']['id'], old['version']['id'])
        # A completed migration does not parse old archives or update classification on startup.
        with patch('contexthub.store.read_package', side_effect=AssertionError('Migration repeated')):
            restarted = Store(self.root)
        self.assertEqual(restarted.get(public['id'])['platforms'], ['claude'])

    def test_platform_and_search_filter_precede_result_limit(self):
        # Real Store writes populate the catalogue; mock Git only for volume setup.
        with patch.object(self.store, '_git_snapshot', return_value=None), patch.object(self.store, '_sync_git_ref'):
            oldest = self.store.add_package(self.alice, pack('deepseek', text='Rare matching record'), 'public')
            for index in range(205):
                self.store.add_package(self.alice, pack('codex', text=f'Newer record {index}'), 'public')
        self.assertEqual(len(self.store.list_packages()), 200)
        self.assertEqual(self.ids(platform='deepseek'), [oldest['id']])
        self.assertEqual(self.ids(query='Rare', platform='deepseek'), [oldest['id']])

    def test_contribution_merge_synchronizes_head_catalog(self):
        contributions = Contributions(self.store)
        target = self.store.add_package(self.alice, pack('codex'), 'public')
        source = self.store.fork_package(target['id'], self.bob, target['version']['id'], 'public')
        source = self.store.add_version(source['id'], self.bob, pack('claude', text='Contributed'),
                                       source['version']['id'], 'New conversation source')
        proposal = contributions.create(source['id'], self.bob, source['version']['id'], 'New source', '')
        checked = contributions.check(proposal['id'], self.alice, target['version']['id'])['check']
        self.assertEqual(checked['status'], 'ready')
        candidate_platforms = package_platforms(read_package(contributions.candidate(proposal['id'], check_id=checked['id'])))
        self.assertEqual(candidate_platforms, ['claude'])
        contributions.merge(proposal['id'], self.alice, checked['id'], checked['candidate_oid'])
        self.assertEqual(self.store.get(target['id'])['platforms'], ['claude'])
        self.assertEqual(set(self.ids(platform='claude')), {target['id'], source['id']})
        self.assertEqual(self.ids(platform='codex'), [])
        self.assertEqual(self.store.detail(target['id'], version_id=target['version']['id'])['platforms'], ['codex'])

    def test_index_write_failure_rolls_back_head_and_version(self):
        first = self.store.add_package(self.alice, pack('codex'))
        original = self.store._index_platforms
        def reject_after_index(db, ident, package):
            original(db, ident, package)
            raise StoreCapacityError('Synthetic exhausted disk budget')
        with patch.object(self.store, '_index_platforms', side_effect=reject_after_index):
            with self.assertRaises(StoreCapacityError):
                self.store.add_version(first['id'], self.alice, pack('deepseek'), first['version']['id'], 'Will roll back')
        self.assertEqual(self.store.get(first['id'], self.alice['id'])['platforms'], ['codex'])
        self.assertEqual(self.store.get(first['id'], self.alice['id'])['head_version_id'], first['version']['id'])
        self.assertEqual(len(self.store.versions(first['id'], self.alice['id'])), 1)

    def test_concurrent_head_change_cannot_mix_old_metadata_with_new_categories(self):
        first = self.store.add_package(self.alice, pack('codex'))
        other = Store(self.root)
        index_reader = self.store._indexed_platforms
        def write_before_index_read(db, identifiers):
            other.add_version(first['id'], self.alice, pack('deepseek'), first['version']['id'], 'Concurrent update')
            return index_reader(db, identifiers)
        with patch.object(self.store, '_indexed_platforms', side_effect=write_before_index_read):
            visible = self.store.get(first['id'], self.alice['id'])
        self.assertEqual(visible['head_version_id'], first['version']['id'])
        self.assertEqual(visible['platforms'], ['codex'])
        self.assertEqual(self.store.get(first['id'], self.alice['id'])['platforms'], ['deepseek'])


if __name__ == '__main__':
    unittest.main()
