# Codex transfer verification

Verified on 2026-10-09 with the installed **codex-cli 0.153.4** and Python 3.12.3.

## Result

The implemented adapter exports visible completed user/assistant messages,
passes them through the real `.contextpack` writer and reader, and injects them
into a newly created destination Codex thread. After stopping and restarting the
destination app-server, the next request to a local deterministic Responses
fixture contained all four imported messages, including Chinese, code fences,
and a long text answer, with exact text equality.

The fixture is a small HTTP server returning canned SSE responses. It performs
**no model inference** and uses no account or API key. This verifies the input
Codex prepares after restart, not an actual model's ability to answer a question.

| Check | Observed result |
| --- | --- |
| Source normal conversation export | Exact match: two turns, four messages |
| Portable package build/read | Exact match after hash validation |
| Destination ID | New, different from source and existing destination |
| Original source | Content unchanged after migration |
| Existing destination | Content unchanged after migration |
| Target permissions | `readOnly` sandbox and `on-request` approval retained |
| Import/export provider requests | Zero |
| Fixture requests for test turns | Four, entirely local |
| Destination restart and resume | Passed |
| Restarted model-visible input | All four imported texts present, exact equality |
| Imported messages in public timeline before continuation | Empty on this version |
| Native CLI UI | Not tested |
| Desktop App UI | Not tested |
| Actual model continuation | Not tested |

The script explicitly selects legacy storage for its source fixture using the
experimental `historyMode` field. Destination import uses target defaults. The
ordinary adapter does not use `resume.history`, `resume.path`, raw database
writes, or the experimental history selection field. Paginated turn/item
enumeration, full-item hydration, repeated-cursor rejection and stable completed
boundaries are covered by unit tests; the real content-transfer proof here uses
legacy source history. Some runtime history operations in this build returned
`list_turns is not supported yet`; such errors remain failures rather than being
converted into empty successful exports.

## Reproduce

```bash
python3 -m unittest tests.test_codex -v
python3 scripts/verify_codex_transfer.py --output /tmp/codex-transfer-report.json
```

The integration script requires `codex` and Linux `bwrap`. It refuses to fall
back to an unisolated run. Each participant gets an empty temporary directory
mounted over the normal Codex storage path, a separate workspace and capture
directory, a separate PID namespace, and a network namespace without external
network access. `HOME` and `CODEX_HOME` are not rewritten. The fixture provider
configuration is written only inside these temporary stores. No real user
conversation body or credential file is opened or copied. Temporary storage is
removed after the report is produced.

Only the verification script calls `turn/start`, against its local fixture.
The product's `import_conversation` and `export_conversation` do not start turns.
An additional lifecycle check confirmed that ordinary `CodexClient` context
manager exit permits resume by a fresh process without manual unsubscribe.

## Honest compatibility reporting

`thread/inject_items` succeeds and persists useful model context, but on this
version the original injected messages are absent from `thread/read` and public
turn/item history. The import receipt therefore keeps `verified=false`,
`api_readback_verified=false` and `ui_history_verified=false` when the public
readback does not match. A protocol acknowledgement alone is never verification.
The per-import `persistence_verified` and `model_context_verified` fields remain
false because an ordinary user import does not perform the isolated restart or
start a verification turn. The integration report separately records the tested
restart and model-input behavior.

The resulting thread is a **new conversation with imported context**, not a
restored native timeline. Re-exporting it through public history after subsequent
turns may include only those later visible turns; the injected earlier messages
cannot be reconstructed from an empty public timeline. Every export discloses
this limitation, and the original package should be retained.

Exports stop before the first incomplete or unsuccessful turn and re-read the
chosen completed prefix to detect concurrent history changes. Unsupported tool,
reasoning, image or other items are omitted with warnings; imported system and
developer roles are rejected, and tool references are never executed or injected
as active tool calls. More than 2,000 messages or 5 MiB of conversation content
is rejected without silent truncation. Permissions and source machine paths are
not transplanted to the destination.
