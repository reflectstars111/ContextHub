# Library Versions and Fork Implementation Plan

> Execute the independent modules in parallel, then review and verify the integrated workflow. The user's request to continue development authorizes this implementation.

**Goal:** Turn uploaded packages into versioned conversation libraries with fixed-version Fork, editing, comparison and immutable historical downloads.

**Architecture:** Extend the existing SQLite store with immutable version rows and transactional head updates. Add pure revision/diff functions, authenticated HTTP routes and a browser editing/history workflow; preserve the current portable format and Codex connector.

**Tech Stack:** Python 3.12 standard library, SQLite, Git CLI, vanilla JavaScript, unittest, optional Playwright for verification.

**Spec:** `docs/superpowers/specs/2026-10-09-library-versions-design.md`.

## Global constraints

- Keep existing package format 0.1 and all Codex import/export behavior.
- Fixed library visibility; private content never enters Git or public history.
- Legacy database upgrade is additive and idempotent; never rewrite uploaded bytes.
- Drafts stay in the browser until explicit save; no real user conversation or model calls in tests.
- Preserve original provenance and record human edits on the server.

## Review focus

- Concurrent saves from separate Store instances cannot overwrite each other.
- Historical APIs and upstream metadata require current access checks.
- Successful retries, deleted upstreams and failed Git operations retain consistent state.
- Large diffs have accurate totals and honest bounded display.
- Browser drafts survive 409 responses; historical links/downloads stay pinned.

## Task 1: Store and Git history

Owner: version_store. Files: `contexthub/store.py`, `tests/test_versions_store.py`.

Interfaces: StoreConflict; `detail/download(...,version_id=None)`, `versions(ident,user_id=None)`, `add_version(ident,owner,data,base_version,message)`, `fork_package(ident,owner,version_id,visibility='private')`.

- [x] Write failing tests for migration, CAS/retry, history ACL, immutable blobs, Fork lineage and Git parents.
- [x] Implement additive schema, safe publication and fixed-version reads.
- [x] Run dedicated tests and existing server/CLI regression.

## Task 2: Revision construction and comparison

Owner: revision_logic. Files: `contexthub/revisions.py`, `tests/test_revisions.py`.

Interfaces: `build_revision(base_package,payload)->bytes`, `compare_packages(before,after)->dict`.

- [x] Write failing tests for provenance, human edit records, additions/deletions/order and large bounded diffs.
- [x] Implement canonical revision construction and ID-based comparison.
- [x] Run dedicated tests and package format regression.

## Task 3: HTTP integration

Owner: root. Files: `contexthub/server.py`, `tests/test_versions_api.py`.

- [x] Test new HTTP contracts before implementation: fixed versions, preview without persistence, save/retry/conflict, Fork, per-endpoint access control and CSRF.
- [x] Add routes with StoreConflict mapped to 409 and owner authorization before content processing.
- [x] Verify HTTP round-trip with real temporary storage.

## Task 4: Browser workflow

Owner: version_ui. Files: static HTML/JS/CSS and optional version module.

- [x] Add version history, fixed links/downloads and upstream Fork display.
- [x] Add owned draft editing, package replacement, review/save and conflict preservation.
- [x] Add readable version comparison and bounded-detail disclosure.
- [x] Validate JavaScript syntax and preserve old UI selectors.

## Task 5: Integration delivery

Owner: root. Files: browser acceptance script, README, verification record.

- [x] Run full unit/HTTP suite and old browser transfer test.
- [x] Run two-account browser Fork/edit/compare/history/conflict workflow and inspect screenshots.
- [x] Fresh review and fix material findings, then update documentation and plan.
- [x] Commit feature and restart local service on verified code without replacing user data.

## Completion evidence

2026-10-09: All five tasks completed. 77 unittest checks and both browser acceptance scripts passed. Independent review findings were reproduced, fixed, and verified by the reviewer. Existing development data was backed up and the local Hub restarted as 0.2.0. See `docs/verification/2026-10-09-versions.md` for exact scope and limitations.
