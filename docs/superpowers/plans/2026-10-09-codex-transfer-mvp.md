# ContextHub Codex Transfer MVP Implementation Plan

> **For agentic workers:** Execute the independently owned modules below, verify their shared interfaces, then perform a fresh integration review. The user's “继续完成” authorizes continuing implementation in this session.

**Goal:** Deliver a usable local Codex connector and self-hosted conversation upload/download site, with verified content transfer and explicit compatibility reporting.

**Architecture:** A dependency-free Python 3.12 package owns the portable ZIP format, Codex JSON-RPC adapter, CLI and SQLite-backed HTTP service. A static browser interface provides batch selection, preview, upload, reading and downloads. The connector is a separate loopback-only process with a per-launch token; the hub never reads host Codex data.

**Tech Stack:** Python standard library, SQLite, Git CLI, HTML/CSS/JavaScript, unittest; optional Playwright only for browser verification.

**Spec:** `docs/research/2026-10-08-codex-portability.md`; conversation platform design remains the longer-term scope.

## Global Constraints

- Do not modify or bulk export existing private Codex conversations during tests; use synthetic sessions in isolated storage.
- No model invocation during export/import. Never copy source authentication, system permissions or developer instructions.
- `.contextpack`: ZIP, exact manifest hashes, safe paths, 200 conversations, 5 MiB per conversation, 100 MiB decompressed maximum.
- Never label untested native UI restoration or paid-model continuation as verified.
- First ship the requested transfer workflow. Community PRs and advanced branches remain later stages.

## Decisions

- Use installed Python 3.12 instead of the proposed Next.js/PostgreSQL for this first executable slice: it runs without downloading product dependencies and is easy to self-host. Storage and HTTP boundaries remain replaceable.
- Work in this new project directory; there is no preexisting Git checkout to isolate. Initialize a feature branch once the initial artifacts are ready.
- Parallelize only separate file ownership; one final reviewer checks the integrated product.

## Review Focus

- Corrupt/hostile ZIPs must not escape paths, inflate without bounds or bypass hashes.
- Unknown Codex items and partial pagination must produce honest warnings, not silently incomplete success.
- Imports must create new IDs, persist, and never activate imported system/tool instructions.
- Anonymous reads must not expose private uploads or a local connector's conversations.
- Duplicate uploads, failed imports, browser refreshes and restarts must preserve consistent data.

## Task 1: Portable format

Files: `contexthub/package.py`, `tests/test_package.py`.

Interfaces: `build_package(conversations, title, description='', source=None) -> bytes`; `read_package(data) -> dict` containing `manifest`, `library`, `conversations`, `compatibility`; `redact_conversations(conversations) -> (list, list)`; errors inherit `ValueError`.

- [x] Write and observe failing tests for Unicode/code round-trip, traversal, duplicates, corrupt hashes, decompression bounds and credential redaction.
- [x] Implement the package format and deterministic Markdown projection; validate message IDs/roles/content.
- [x] Verify with `python3 -m unittest tests.test_package -v`.

## Task 2: Codex adapter

Files: `contexthub/codex.py`, `tests/test_codex.py`, `scripts/verify_codex_transfer.py`.

Interfaces: `CodexClient(command=None, timeout=30)` context manager; `.request(method, params)`; `list_sessions(client, include_archived=False, include_subagents=False) -> list`; `export_conversation(client, thread_id) -> dict`; `import_conversation(client, conversation, cwd) -> dict` with `thread_id`, `mode`, `warnings`, `verified`.

- [x] Pin pagination/conversion behavior with failing unit tests, then implement.
- [x] Probe real 0.153.4 in bwrap-isolated temporary Codex storage. Do not repurpose HOME/CODEX_HOME or touch the real session database.
- [x] Verify persisted and restarted readback, source preservation and unique destination IDs. Record failures as compatibility limits.
- [x] Verify with `python3 -m unittest tests.test_codex -v` and the isolated integration script.

## Task 3: Hub and connector HTTP boundaries

Files: `contexthub/server.py`, `contexthub/store.py`, `tests/test_server.py`.

Hub API: `GET /api/config`, `GET /api/session`, `POST /api/register`, `POST /api/login`, `POST /api/logout`, `GET /api/packages?q=`, `POST /api/packages` (raw ZIP; title from package; visibility query), `GET /api/packages/{id}`, `GET /api/packages/{id}/download`, `DELETE /api/packages/{id}`. Responses are JSON objects; errors `{error: text}`. List is `{packages:[{id,title,description,author,conversation_count,created_at,visibility}]}`. Detail adds `conversations` and `compatibility`. Cookie session; authenticated mutations require `X-CSRF-Token` returned in session response.

Connector API: `GET /api/config` gives `{mode:'connector'}`; all `/api/local/*` require `X-ContextHub-Token` from launch URL fragment. `GET /api/local/sessions?archived=1&subagents=1`; `POST /api/local/preview` JSON `{thread_ids:[...]}` -> `{conversations:[...],warnings:[...]}`; `POST /api/local/export` JSON `{conversations,title,description,redact:true}` -> ZIP; `POST /api/local/inspect` raw ZIP -> preview; `POST /api/local/import?cwd=...` raw ZIP -> `{results:[...]}`. Preview is read-only. Import is explicit, creates new sessions and never calls `turn/start`.

- [x] Write failing tests for registration, auth/CSRF, private package ACL, hashes/download equality, restart persistence, connector token separation.
- [x] Implement SQLite accounts/packages and content-addressed immutable file storage; create Git snapshots of published package content.
- [x] Implement HTTP routes with body limits, safe static serving, no CORS, no shared writable session global.
- [x] Verify with `python3 -m unittest tests.test_server -v`.

## Task 4: Browser workflow

Files: `contexthub/static/index.html`, `contexthub/static/app.js`, `contexthub/static/style.css`.

- [x] Implement readable Chinese interface with responsive sidebar, task-oriented empty states and real API-backed content.
- [x] Hub: login/register, upload preview, public/private selection, search, reader, single JSON/Markdown copy and complete package download.
- [x] Connector: session filter/multi-select, content preview, export package, inspect package, choose cwd, explicit import and receipts.
- [x] Verify browser navigation, file upload/download and safe rendering with synthetic content.

## Task 5: CLI and delivery

Files: `contexthub/__main__.py`, `pyproject.toml`, `README.md`, `tests/test_cli.py`, `.gitignore`, `docs/verification/2026-10-09-transfer.md`.

- [x] Implement `serve`, `connect`, `list`, `export`, `inspect`, `import`, `upload`, `download`, and `demo` commands.
- [x] Run the full offline suite and the real isolated Codex protocol test; perform two-account HTTP upload/download round-trip.
- [x] Run browser smoke verification and fresh code review; fix material findings.
- [x] Document startup, exact tested compatibility and remaining limitations; leave a local demo running if practical.

## Progress

2026-10-09: Plan recorded; implementation beginning. bwrap and Docker are available; Node is 18, Python is 3.12.3; no Python web/test dependencies installed. Use unittest and standard-library runtime.

2026-10-09: 迁移 MVP 已实现。46 项单元/HTTP/CLI测试、真实 Codex 隔离迁移和浏览器验收通过；独立代码审查问题已修复。最终证据与明确限制见 `docs/verification/2026-10-09-transfer.md`。公开时间线未恢复，真实模型续聊未验证；该限制已贯穿回执、网页与 README。Hub 本机 8765 端口运行。
