#!/usr/bin/env python3
"""Public contribution acceptance with synthetic libraries and two browsers."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from contexthub.package import build_package, read_package
from scripts.verify_web import service, register, upload, CONVERSATION
from scripts.verify_versions_web import api, version_ids, edit_message, preview, save
from playwright.sync_api import sync_playwright, expect


def revise(page, url, library, content):
    conversations = copy.deepcopy(library['conversations'])
    conversations[0]['messages'][1]['content'] = content
    return api(page, url, f"/api/packages/{library['id']}/versions", {
        'base_version': library['version']['id'], 'title': library['title'],
        'description': library['description'], 'conversations': conversations,
        'message': '合成贡献验收修改',
    })


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory(prefix='contexthub-contributions-web-') as temp, sync_playwright() as playwright:
        root = Path(temp)
        pack = root / 'source.contextpack'
        pack.write_bytes(build_package([CONVERSATION], '贡献流程合成对话库'))
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        with service(root / 'hub') as (hub, url):
            alice_context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            bob_context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            alice, bob = alice_context.new_page(), bob_context.new_page()
            for page in (alice, bob):
                page.on('pageerror', lambda error: errors.append(str(error)))
            alice.goto(url)
            register(alice, 'contributionalice')
            upload(alice, pack, 'public')
            target_id, target_v1 = version_ids(alice)
            bob.goto(alice.url)
            register(bob, 'contributionbob')
            bob.locator('#fork-package').click()
            bob.locator('input[name="fork-visibility"][value="public"]').check()
            bob.locator('#fork-submit').click()
            expect(bob.locator('#upstream-info')).to_contain_text('contributionalice')
            bob.locator('#edit-package').click()
            edit_message(bob, 'Bob 整理的回答，准备贡献给上游。', index=1)
            preview(bob, '完善回答')
            save(bob, 2)
            source_id, source_v2 = version_ids(bob)
            source_link = bob.url
            bob.locator('#open-contribution').click()
            bob.locator('#contribution-title-input').fill('补充对话复用说明')
            bob.locator('#contribution-description-input').fill('请检查这份固定版本的改进。')
            # Closing and reopening a form creates a new draft. Old requests
            # may neither close it, navigate away, nor inject old errors.
            pending = []
            create_pattern = f'**/api/packages/{source_id}/contributions'
            bob.route(create_pattern, lambda route: pending.append(route) if route.request.method == 'POST' else route.continue_())
            bob.locator('#contribution-submit').click()
            assert pending
            bob.keyboard.press('Escape')
            bob.locator('#open-contribution').click()
            bob.locator('#contribution-title-input').fill('需要保留的新提案草稿')
            with bob.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/contributions')):
                pending.pop().continue_()
            expect(bob.locator('#contribution-dialog')).to_be_visible()
            expect(bob.locator('#contribution-title-input')).to_have_value('需要保留的新提案草稿')
            expect(bob.locator('#reader-view')).to_be_visible()
            expect(bob.locator('#contribution-submit')).to_be_enabled()
            bob.locator('#contribution-submit').click()
            assert pending
            bob.keyboard.press('Escape')
            bob.locator('#open-contribution').click()
            bob.locator('#contribution-title-input').fill('旧错误不能覆盖这个新表单')
            with bob.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/contributions')):
                pending.pop().fulfill(status=422, content_type='application/json', body=json.dumps({'error': '旧窗口的合成错误'}))
            expect(bob.locator('#contribution-dialog')).to_be_visible()
            expect(bob.locator('#contribution-title-input')).to_have_value('旧错误不能覆盖这个新表单')
            expect(bob.locator('#contribution-form-error')).not_to_be_visible()
            bob.keyboard.press('Escape')
            bob.unroute(create_pattern)
            expect(bob.locator('#package-contributions')).to_contain_text('补充对话复用说明')
            bob.locator('[data-repository-section="package-contributions-panel"]').click()
            bob.locator('#package-contributions').get_by_text('补充对话复用说明', exact=True).click()
            expect(bob.locator('#contribution-view')).to_be_visible()
            expect(bob.locator('#contribution-status')).to_have_text('待合并')
            link = bob.url
            proposal_id = parse_qs(urlsplit(link).query)['contribution'][0]
            proposal_path = '/api/contributions/' + proposal_id
            expect(bob.locator('#contribution-check')).not_to_be_visible()
            bob.reload()
            expect(bob.locator('#contribution-title')).to_have_text('补充对话复用说明')
            assert bob.url == link

            # A pending proposal read cannot replace a newly started edit draft.
            bob.goto(source_link)
            expect(bob.locator('#package-contributions')).to_contain_text('补充对话复用说明')
            pending = []
            pattern = '**' + proposal_path
            bob.route(pattern, lambda route: pending.append(route))
            bob.locator('[data-repository-section="package-contributions-panel"]').click()
            bob.locator('#package-contributions').get_by_text('补充对话复用说明', exact=True).click()
            bob.locator('#edit-package').click()
            bob.locator('#draft-title').fill('仍在编辑的草稿')
            assert pending
            with bob.expect_response(lambda response: response.url.endswith(proposal_path)):
                pending.pop().continue_()
            bob.wait_for_timeout(100)
            expect(bob.locator('#draft-panel')).to_be_visible()
            expect(bob.locator('#draft-title')).to_have_value('仍在编辑的草稿')
            bob.unroute(pattern)
            # Full navigation intentionally discards this synthetic draft.
            bob.once('dialog', lambda dialog: dialog.accept())
            bob.goto(link)
            expect(bob.locator('#contribution-comment')).to_be_visible()

            # Submitting an older comment must preserve text typed in flight.
            pending = []
            comment_pattern = '**' + proposal_path + '/comments'
            bob.route(comment_pattern, lambda route: pending.append(route))
            literal = '<script>window.contributionInjected=true</script> 已核对。'
            bob.locator('#contribution-comment').fill(literal)
            bob.locator('#contribution-comment-submit').click()
            bob.locator('#contribution-comment').fill('尚未提交的下一条评论')
            assert pending
            with bob.expect_response(lambda response: response.url.endswith('/comments')):
                pending.pop().continue_()
            expect(bob.locator('#contribution-comments')).to_contain_text(literal)
            expect(bob.locator('#contribution-comment')).to_have_value('尚未提交的下一条评论')
            assert bob.evaluate('window.contributionInjected === undefined')
            bob.unroute(comment_pattern)
            bob.locator('#contribution-back').click()
            expect(bob.locator('#package-contributions')).to_contain_text('补充对话复用说明')
            bob.locator('[data-repository-section="package-contributions-panel"]').click()
            bob.locator('#package-contributions').get_by_text('补充对话复用说明', exact=True).click()
            expect(bob.locator('#contribution-comment')).to_have_value('尚未提交的下一条评论')
            bob.locator('#contribution-comment').fill('')

            alice.goto(link)
            expect(alice.locator('#contribution-source-comparison')).to_contain_text('Bob 整理的回答')
            alice.locator('#contribution-check').click()
            expect(alice.locator('#contribution-check-summary')).to_contain_text('合并候选已生成')
            expect(alice.locator('#contribution-merge')).to_be_disabled()
            alice.locator('#contribution-review-check').click()
            expect(alice.locator('#contribution-check-comparison')).to_contain_text('Bob 整理的回答')
            expect(alice.locator('#contribution-merge')).to_be_enabled()
            checked = api(alice, url, proposal_path)
            with alice.expect_download() as download:
                alice.locator('#contribution-download-candidate').click()
            candidate = root / 'candidate.contextpack'
            download.value.save_as(candidate)
            assert read_package(candidate.read_bytes())['conversations'][0]['messages'][1]['content'] == 'Bob 整理的回答，准备贡献给上游。'
            assert api(alice, url, f'/api/packages/{target_id}')['version']['id'] == target_v1
            source = api(bob, url, f'/api/packages/{source_id}')
            revise(bob, url, source, '源库后续编辑不应混入既有提案。')
            assert api(alice, url, proposal_path)['source']['version_id'] == source_v2
            alice.screenshot(path=str(artifacts / 'contribution-review.png'), full_page=True)
            alice.locator('#contribution-merge').click()
            expect(alice.locator('#contribution-status')).to_have_text('已合并')
            latest = api(alice, url, f'/api/packages/{target_id}')
            assert latest['version']['git_commit'] == checked['check']['candidate_oid']
            assert alice.request.get(url + f'/api/packages/{target_id}/download').body() == candidate.read_bytes()
            alice.locator('#contribution-merged-version button').click()
            expect(alice.locator('#messages')).to_contain_text('Bob 整理的回答')
            assert version_ids(alice) == (target_id, latest['version']['id'])

            # A second proposal exercises invalidation and explicit source update.
            fork = api(bob, url, f'/api/packages/{target_id}/fork', {
                'version_id': latest['version']['id'], 'visibility': 'public'})
            fork = revise(bob, url, fork, 'Bob 的第二份改进。')
            proposal = api(bob, url, f"/api/packages/{fork['id']}/contributions", {
                'source_version_id': fork['version']['id'], 'title': '并行编辑冲突验收'})
            second_path = '/api/contributions/' + proposal['id']
            second_link = url + '/?contribution=' + proposal['id']
            alice.goto(second_link)
            alice.locator('#contribution-check').click()
            expect(alice.locator('#contribution-check-summary')).to_contain_text('合并候选已生成')
            alice.locator('#contribution-review-check').click()
            expect(alice.locator('#contribution-merge')).to_be_enabled()
            alice.locator('#contribution-comment').fill('过期检查后仍需保留的评论')
            revised_target = revise(alice, url, latest, 'Alice 同时修改了相同回答。')
            alice.locator('#contribution-merge').click()
            expect(alice.locator('#contribution-check-summary')).to_contain_text('已失效')
            expect(alice.locator('#contribution-comment')).to_have_value('过期检查后仍需保留的评论')
            expect(alice.locator('#contribution-merge')).to_be_disabled()
            alice.locator('#contribution-check-summary').get_by_text('重新加载贡献请求', exact=True).click()
            expect(alice.locator('#contribution-comment')).to_have_value('过期检查后仍需保留的评论')
            alice.locator('#contribution-check').click()
            expect(alice.locator('#contribution-conflicts')).to_contain_text('Alice 同时修改了相同回答')
            expect(alice.locator('#contribution-conflicts')).to_contain_text('Bob 的第二份改进')
            expect(alice.locator('#contribution-merge')).to_be_disabled()
            assert api(alice, url, f'/api/packages/{target_id}')['version']['id'] == revised_target['version']['id']
            alice.screenshot(path=str(artifacts / 'contribution-conflicts.png'), full_page=True)

            updated_fork = revise(bob, url, fork, 'Alice 同时修改了相同回答。')
            bob.goto(second_link)
            expect(bob.locator('#contribution-source-version option')).to_have_count(3)
            bob.locator('#contribution-source-version').select_option(updated_fork['version']['id'])
            bob.locator('#contribution-update-source').click()
            expect(bob.locator('#contribution-source')).to_contain_text('v3')
            assert api(bob, url, second_path)['check'] is None
            bob.locator('#contribution-close').click()
            expect(bob.locator('#contribution-status')).to_have_text('已关闭')
            bob.locator('#contribution-reopen').click()
            expect(bob.locator('#contribution-status')).to_have_text('待合并')
            alice.locator('#contribution-comment').fill('')
            alice.goto(second_link)
            alice.locator('#contribution-check').click()
            expect(alice.locator('#contribution-check-summary')).to_contain_text('合并候选已生成')

            mobile_context = browser.new_context(viewport={'width': 390, 'height': 844})
            mobile = mobile_context.new_page()
            mobile.on('pageerror', lambda error: errors.append(str(error)))
            mobile.goto(second_link)
            expect(mobile.locator('#contribution-title')).to_have_text('并行编辑冲突验收')
            expect(mobile.locator('#contribution-check')).not_to_be_visible()
            expect(mobile.locator('#contribution-comment-form')).not_to_be_visible()
            mobile.locator('#contribution-review-check').click()
            assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
            mobile.screenshot(path=str(artifacts / 'contribution-mobile.png'), full_page=True)
            mobile_context.close()
            bob_context.close()
            alice_context.close()
        browser.close()
    assert not errors, errors
    result = {'create_and_fixed_link': True, 'source_version_pinned': True,
              'review_before_merge': True, 'exact_candidate_bytes_and_oid': True,
              'stale_merge_and_conflict': True, 'explicit_source_update_close_reopen': True,
              'inflight_comment_and_navigation_preserved': True, 'delayed_read_preserves_draft': True,
              'old_submission_preserves_new_proposal_form': True,
              'untrusted_comment_inert': True, 'public_read_mobile_no_overflow': True,
              'javascript_errors': errors, 'real_user_data_access': False}
    (artifacts / 'contributions-web-verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
