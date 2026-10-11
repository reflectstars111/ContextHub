#!/usr/bin/env python3
"""Repository UI and code checkpoint acceptance against a temporary real Hub."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from contexthub.checkpoints import attach_checkpoint
from contexthub.package import build_package
from scripts.verify_web import service, register, upload, CONVERSATION
from scripts.verify_versions_web import api, preview, save, version_ids
from playwright.sync_api import sync_playwright, expect


def linked(blob, commit='a' * 40):
    return attach_checkpoint(blob, {
        'repository_url': 'https://github.com/example/vibe-project.git',
        'commit': commit, 'tree': 'b' * 40, 'branch': 'feature/dialogue-checkpoints',
        'captured_at': '2026-10-10T00:00:00Z', 'verification': 'local_clean',
    })


def main():
    artifacts = PROJECT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory(prefix='contexthub-repository-web-') as temp, sync_playwright() as playwright:
        root = Path(temp)
        initial = build_package([CONVERSATION], 'vibe-coding-checkpoints', '一起审阅 AI 对话与代码改动，恢复到同一次开发检查点。')
        package = root / 'linked.contextpack'
        package.write_bytes(linked(initial))
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        with service(root / 'hub') as (_, url):
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            register(page, 'repositoryalice')
            upload(page, package, 'public')
            ident, first = version_ids(page)
            first_url = page.url
            panel = page.locator('#code-checkpoint-content')
            expect(panel).to_have_attribute('data-status', 'matched')
            expect(page.locator('#reader-owner')).to_have_text('repositoryalice')
            expect(page.locator('#repository-visibility')).to_have_text('公开')
            expect(panel.locator('.checkpoint-commit')).to_have_attribute('href', 'https://github.com/example/vibe-project/commit/' + 'a' * 40)
            expect(panel).to_contain_text('不代表 GitHub 认证')
            expect(page.locator('#checkpoint-restore-command')).to_contain_text('version=' + first)
            page.locator('[data-repository-section="code-browser-panel"]').click()
            assert all(parse_qs(urlsplit(page.url).query)[key] == parse_qs(urlsplit(first_url).query)[key] for key in ('package', 'version')), 'Repository section navigation dropped the pinned version'
            page.locator('[data-repository-section="conversation-section"]').click()
            page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(artifacts / 'repository-desktop.png'), full_page=True)

            anonymous = browser.new_context(viewport={'width': 390, 'height': 844})
            mobile = anonymous.new_page()
            mobile.on('pageerror', lambda error: errors.append(str(error)))
            mobile.goto(first_url)
            expect(mobile.locator('#code-checkpoint-content')).to_have_attribute('data-status', 'matched')
            expect(mobile.locator('#repository-settings-tab')).not_to_be_visible()
            for width in (320, 390, 768):
                mobile.set_viewport_size({'width': width, 'height': 844})
                assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Repository overflows at {width}px'
            mobile.set_viewport_size({'width': 390, 'height': 844})
            mobile.screenshot(path=str(artifacts / 'repository-mobile.png'), full_page=True)
            mobile.locator('#download-package').click()
            expect(mobile.locator('#auth-dialog')).to_be_visible()
            assert mobile.request.get(url + f'/api/packages/{ident}/download').status == 401
            anonymous.close()

            # The saved claim remains with a draft, but must never be displayed as verified.
            page.locator('#edit-package').click()
            page.locator('#draft-title').fill('修改需求后的对话')
            expect(panel).to_have_attribute('data-status', 'draft')
            expect(page.locator('#draft-autosave-status')).to_contain_text('草稿已保存到服务器')
            page.reload()
            expect(page.locator('#draft-recovery')).to_be_visible()
            page.locator('#restore-draft').click()
            expect(panel).to_have_attribute('data-status', 'draft')
            expect(panel.locator('.checkpoint-commit')).to_have_text('a' * 12)
            preview(page, '补充开发需求')
            save(page, 2)
            expect(panel).to_have_attribute('data-status', 'stale')
            latest = version_ids(page)[1]
            page.locator('#version-select').select_option(first)
            expect(panel).to_have_attribute('data-status', 'matched')
            page.locator('#version-select').select_option(latest)
            expect(panel).to_have_attribute('data-status', 'stale')

            # Replacing the whole draft must explicitly clear the old association.
            no_link = root / 'no-link.contextpack'
            no_link.write_bytes(build_package([CONVERSATION], '没有代码关联的新候选'))
            page.locator('#edit-package').click()
            expect(page.locator('#draft-panel')).to_be_visible()
            page.locator('#version-file').set_input_files(str(no_link))
            expect(page.locator('#candidate-actions')).to_be_visible()
            page.locator('#apply-candidate').click()
            expect(panel.locator('.checkpoint-commit')).to_have_count(0)
            preview(page, '用独立对话替换内容')
            save(page, 3)
            expect(panel).to_have_attribute('data-status', 'none')
            assert api(page, url, f'/api/packages/{ident}')['code_checkpoint'] is None

            # A new matching candidate survives auto-save/reload and publishes its own link.
            replacement = root / 'replacement.contextpack'
            # Include the server's accumulated edit provenance in the bound snapshot.
            # Importing earlier content would correctly make its old digest stale.
            replacement.write_bytes(linked(page.request.get(url + f'/api/packages/{ident}/download').body(), 'c' * 40))
            page.locator('#edit-package').click()
            expect(page.locator('#draft-panel')).to_be_visible()
            page.locator('#version-file').set_input_files(str(replacement))
            expect(page.locator('#candidate-actions')).to_be_visible()
            page.locator('#apply-candidate').click()
            expect(panel.locator('.checkpoint-commit')).to_have_text('c' * 12)
            expect(page.locator('#draft-autosave-status')).to_contain_text('草稿已保存到服务器')
            page.reload()
            expect(page.locator('#draft-recovery')).to_be_visible()
            page.locator('#restore-draft').click()
            expect(panel.locator('.checkpoint-commit')).to_have_text('c' * 12)
            preview(page, '关联新的实现检查点')
            save(page, 4)
            expect(panel).to_have_attribute('data-status', 'matched')
            assert api(page, url, f'/api/packages/{ident}')['code_checkpoint']['commit'] == 'c' * 40

            page.locator('#reader-back').click()
            expect(page.locator('#package-grid')).to_contain_text('repositoryalice /')
            page.screenshot(path=str(artifacts / 'repository-list.png'), full_page=True)
            assert not page.evaluate('window.contextHubInjected'), 'Imported content executed'
            context.close()
        browser.close()
    assert not errors, errors
    result = {'checkpoint_match_stale_and_history': True, 'fixed_version_navigation_and_restore_command': True,
              'draft_checkpoint_recovery': True, 'candidate_checkpoint_replacement_and_clear': True,
              'public_read_authenticated_download': True, 'mobile_no_overflow': True,
              'github_commit_link': True, 'javascript_errors': errors, 'real_codex_data_access': False}
    (artifacts / 'repository-web-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
