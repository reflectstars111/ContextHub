"""HTTP acceptance for public, fixed-version contribution review."""
import copy
import unittest
from tests import test_server as helpers
from contexthub.package import read_package


class ContributionAPITests(unittest.TestCase):
    setUp = helpers.ServerTests.setUp
    tearDown = helpers.ServerTests.tearDown
    request = helpers.ServerTests.request
    register = helpers.ServerTests.register

    def login_as(self, who):
        self.cookie, self.csrf = self.accounts[who]

    def revise(self, detail, text, message_index=1):
        conversations = copy.deepcopy(detail['conversations'])
        conversations[0]['messages'][message_index]['content'] = text
        status, result = self.request('POST', f"/api/packages/{detail['id']}/versions", {
            'base_version': detail['version']['id'], 'title': detail['title'],
            'description': detail['description'], 'conversations': conversations, 'message': '合成贡献修改'})
        self.assertEqual(status, 201, result)
        return result

    def setup_proposal(self):
        self.accounts = {}
        self.register('alice')
        self.accounts['alice'] = self.cookie, self.csrf
        status, self.target = self.request('POST', '/api/packages?visibility=public', helpers.sample_pack())
        self.assertEqual(status, 201, self.target)
        self.register('bob')
        self.accounts['bob'] = self.cookie, self.csrf
        status, self.source = self.request('POST', f"/api/packages/{self.target['id']}/fork", {
            'version_id': self.target['version']['id'], 'visibility': 'public'})
        self.assertEqual(status, 201, self.source)
        self.source = self.revise(self.source, 'Bob 提供的改进回答。')
        status, proposal = self.request('POST', f"/api/packages/{self.source['id']}/contributions", {
            'source_version_id': self.source['version']['id'], 'title': '补充保存步骤', 'description': '解释为什么要保留来源。'})
        self.assertEqual(status, 201, proposal)
        self.proposal = proposal
        return '/api/contributions/' + proposal['id']

    def checked(self, path):
        self.login_as('alice')
        status, detail = self.request('POST', path + '/check', {'expected_target_version_id': self.target['version']['id']})
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail['check']['status'], 'ready')
        return detail

    def test_review_merges_exact_candidate_and_source_remains_pinned(self):
        path = self.setup_proposal()
        status, public = self.request('GET', path, auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(public['source']['version_id'], self.source['version']['id'])
        self.assertEqual(public['comparison']['summary']['messages_modified'], 1)
        checked = self.checked(path)
        check = checked['check']
        status, candidate = self.request('GET', path + '/candidate?check_id=' + check['id'], auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(read_package(candidate)['conversations'][0]['messages'][1]['content'], 'Bob 提供的改进回答。')
        self.assertEqual(self.request('GET', f"/api/packages/{self.target['id']}")[1]['head_version_id'], self.target['version']['id'])
        self.login_as('bob')
        self.revise(self.source, '源库后来的内容不应进入既有提案。')
        self.login_as('alice')
        reviewed = {'check_id': check['id'], 'candidate_oid': check['candidate_oid']}
        status, merged = self.request('POST', path + '/merge', reviewed)
        self.assertEqual(status, 200, merged)
        self.assertEqual(merged['status'], 'merged')
        latest = self.request('GET', f"/api/packages/{self.target['id']}")[1]
        self.assertEqual(latest['version']['git_commit'], check['candidate_oid'])
        self.assertEqual(latest['version']['id'], merged['merged_version_id'])
        self.assertEqual(self.request('GET', f"/api/packages/{self.target['id']}/download")[1], candidate)
        repeated = self.request('POST', path + '/merge', reviewed)
        self.assertEqual(repeated[0], 200, repeated)
        self.assertEqual(repeated[1]['merged_version_id'], merged['merged_version_id'])
        self.assertEqual(len(self.request('GET', f"/api/packages/{self.target['id']}/versions")[1]['versions']), 2)

    def test_target_changes_invalidate_review_and_conflicts_prevent_merge(self):
        path = self.setup_proposal()
        checked = self.checked(path)['check']
        self.target = self.revise(self.target, 'Alice 在相同位置给出的不同回答。')
        status, detail = self.request('GET', path)
        self.assertEqual(status, 200)
        self.assertTrue(detail['check']['stale'])
        self.assertFalse(detail['permissions']['can_merge'])
        self.assertEqual(self.request('POST', path + '/merge', {'check_id': checked['id'], 'candidate_oid': checked['candidate_oid']})[0], 409)
        self.assertEqual(self.request('GET', path + '/candidate?check_id=' + checked['id'])[0], 409)
        status, conflicts = self.request('POST', path + '/check', {'expected_target_version_id': self.target['version']['id']})
        self.assertEqual(status, 200, conflicts)
        self.assertEqual(conflicts['check']['status'], 'conflicts')
        self.assertGreater(conflicts['check']['conflict_count'], 0)
        self.assertFalse(conflicts['permissions']['can_merge'])
        self.assertEqual(self.request('GET', f"/api/packages/{self.target['id']}")[1]['head_version_id'], self.target['version']['id'])

    def test_comments_close_reopen_and_explicit_source_update(self):
        path = self.setup_proposal()
        self.assertEqual(self.request('POST', path + '/comments', {'body': '<script>literal comment</script>'})[0], 200)
        self.assertEqual(self.request('POST', path + '/comments', {'body': ''})[0], 422)
        old_check = self.checked(path)['check']
        self.login_as('bob')
        status, closed = self.request('POST', path + '/status', {'status': 'closed'})
        self.assertEqual(status, 200)
        self.assertEqual(closed['status'], 'closed')
        status, reopened = self.request('POST', path + '/status', {'status': 'open'})
        self.assertEqual(status, 200)
        self.assertIsNone(reopened['check'])
        self.assertEqual(reopened['comments'][0]['body'], '<script>literal comment</script>')
        updated_source = self.revise(self.source, '明确更新后提交的回答')
        status, proposal = self.request('POST', path + '/source', {'source_version_id': updated_source['version']['id']})
        self.assertEqual(status, 200, proposal)
        self.assertEqual(proposal['source']['version_id'], updated_source['version']['id'])
        self.assertIsNone(proposal['check'])
        self.login_as('alice')
        self.assertEqual(self.request('POST', path + '/merge', {'check_id': old_check['id'], 'candidate_oid': old_check['candidate_oid']})[0], 409)

    def test_permissions_csrf_and_private_sources(self):
        path = self.setup_proposal()
        self.assertEqual(self.request('POST', path + '/comments', {'body': 'x'}, headers={'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('POST', path + '/comments', {'body': 'x'}, auth=False)[0], 401)
        self.assertEqual(self.request('POST', path + '/check', {'expected_target_version_id': self.target['version']['id']})[0], 403)
        self.register('charlie')
        for action, payload in [('comments', {'body': 'x'}), ('status', {'status': 'closed'}), ('source', {'source_version_id': self.source['version']['id']}), ('check', {'expected_target_version_id': self.target['version']['id']}), ('merge', {'check_id': 'x', 'candidate_oid': 'x'})]:
            self.assertEqual(self.request('POST', path + '/' + action, payload)[0], 403, action)
        self.assertEqual(self.request('POST', f"/api/packages/{self.source['id']}/contributions", {'source_version_id': self.source['version']['id'], 'title': '冒名'})[0], 403)
        self.login_as('bob')
        private = self.request('POST', f"/api/packages/{self.target['id']}/fork", {'version_id': self.target['version']['id']})[1]
        self.assertEqual(self.request('POST', f"/api/packages/{private['id']}/contributions", {'source_version_id': private['version']['id'], 'title': '私有内容'})[0], 404)

    def test_deleted_dependency_blocks_lists_details_and_candidate(self):
        path = self.setup_proposal()
        check = self.checked(path)['check']
        self.login_as('bob')
        self.assertEqual(self.request('DELETE', f"/api/packages/{self.source['id']}")[0], 200)
        self.login_as('alice')
        self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('GET', path + '/candidate?check_id=' + check['id'])[0], 404)
        self.assertEqual(self.request('POST', path + '/comments', {'body': 'x'})[0], 404)
        self.assertEqual(self.request('POST', path + '/merge', {'check_id': check['id'], 'candidate_oid': check['candidate_oid']})[0], 404)
        items = self.request('GET', f"/api/packages/{self.target['id']}/contributions")[1]['contributions']
        self.assertEqual(items, [])


if __name__ == '__main__':
    unittest.main()
