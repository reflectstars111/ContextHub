# Contributions Implementation Plan

> The user's next-development request authorizes implementing the next collaboration stage. Independent modules have separate ownership; root integrates, verifies and obtains fresh review.

**Goal:** A public direct Fork can propose a fixed revision and its upstream owner can review and atomically merge the exact checked candidate.

**Architecture:** Add deterministic merge logic and a Contributions service reusing Store transactions, immutable blobs and Git primitives; expose HTTP endpoints and a browser review flow.

**Tech Stack:** Python 3.12 standard library, SQLite, Git, vanilla JS, unittest and optional Playwright.

**Spec:** `docs/superpowers/specs/2026-10-09-contributions-design.md`.

## Constraints and review focus

- Preserve 0.1 portable format, existing 0.2 version/Fork APIs and all connector behavior.
- Public direct Forks only; no privilege increase from submitting a proposal.
- Author pins source version; maintenance checks current target and exact reviewed candidate ID/OID.
- Reauthorize every dependency on every read/mutation; deleted content cannot leak through comments/candidate/history.
- Deterministic field/sequence merge; no model or guessed answer; preserve audit multiplicity.
- SQL failure or concurrent writer must not advance public head/ref; success retries do not duplicate versions.
- Async proposal/reader results must not replace active drafts; stale checks cannot leave an enabled merge button.

## Task 1: Pure merge

Owner merge_logic. Files `contexthub/merge.py`, `tests/test_merge.py`.
Interface `merge_packages(base,target,source)->{package:bytes|None,conflicts:list,conflict_count:int,truncated:bool}`.

- [x] Write failing tests for independent/same/conflicting fields, deletion, additions/order, audit/identity preservation and bounded conflict details.
- [x] Implement deterministic validated merge, then run dedicated and package/revision tests.

## Task 2: Contribution persistence

Owner contribution_store. Files `contexthub/contributions.py`, `tests/test_contributions_store.py`, minimal `store.py` Git helper extension.
Interface `Contributions(store)` with create/list/detail/comment/set_status/update_source/check/merge/candidate as specified.

- [x] Write failing tests for fixed source/dependencies, permissions, candidate bytes/OID, two Git parents, stale checks, merge retry/concurrency and rollback.
- [x] Implement additive schema and transactional service; validate before public head changes.
- [x] Run own tests and Store regression.

## Task 3: HTTP

Owner root. Files `server.py`, `tests/test_contributions_api.py`.

- [x] Test each new route, CSRF, role permissions, stale IDs, deleted/private dependencies and immutable candidate download.
- [x] Implement JSON contracts with 409 for stale/conflict state and versioned static resource.
- [x] Run real temporary HTTP acceptance.

## Task 4: Browser

Owner contribution_ui. Files static HTML/JS/CSS, new `contributions.js`.

- [x] Add submit/list/detail fixed links, comments/status/source-version update.
- [x] Add exact candidate review, conflicts/stale display, candidate download and explicit merge.
- [x] Preserve unsaved drafts and reject stale async navigation; run JS syntax checks and synthetic smoke test.

## Task 5: Delivery

Owner root.

- [x] Run full unit/HTTP suite and all browser acceptance scripts; inspect desktop/mobile screenshots.
- [x] Fresh independent review; reproduce and fix material findings with regression coverage.
- [x] Update README, design status and verification record with explicit limitations.
- [x] Back up existing DB index, restart local service, verify assets/version, commit final code.

## Completion

2026-10-09: Implemented all five tasks. 124 unit/HTTP tests and all three browser acceptance scripts passed. Independent review findings for semantic no-op ZIPs and stale proposal-form responses were reproduced, fixed and independently rechecked. Existing local data was backed up before starting 0.3.0 on port 8765. See `docs/verification/2026-10-09-contributions.md` for evidence and scope.
