#!/usr/bin/env python3
"""Offline integration: two isolated Codex stores and a deterministic HTTP fixture.

The fixture is not a model. bwrap removes external networking and binds an empty
temporary directory over the real Codex home without changing HOME/CODEX_HOME.
Only this verification program starts turns, against the local canned responder.
The product import/export adapter never starts a turn.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from contexthub.codex import CodexClient, export_conversation, import_conversation, list_sessions

QUESTION = "合成迁移问题 CTXHUB-7318：保存代码\n```python\nprint('你好')\n```"
ANSWER = "合成回答 CTXHUB-9312：代码与中文应完整保留。\n" + "长文本测试。" * 250
QUESTION_TWO = "第二轮 CTXHUB-4256：请保留原样。"
ANSWER_TWO = "第二轮合成回答 CTXHUB-6639。"


class ProbeClient(CodexClient):
    """Only the isolated fixture opts into selecting its source history format."""

    def request(self, method, params):
        if method == "initialize":
            params = dict(params, capabilities={"experimentalApi": True})
        return super().request(method, params)


def mock_child(capture_dir):
    """Run *inside* the network namespace. stdout remains JSON-RPC only."""
    capture = Path(capture_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16 * 1024 * 1024 or self.headers.get("Content-Encoding"):
                self.send_error(400, "Unsupported fixture request")
                return
            payload = json.loads(self.rfile.read(length))
            (capture / (str(uuid.uuid4()) + ".json")).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            user_inputs = [item for item in payload.get("input", []) if item.get("role") == "user"]
            last_input = json.dumps(user_inputs[-1:], ensure_ascii=False)
            answer = ANSWER_TWO if "CTXHUB-4256" in last_input else ANSWER
            identifier = "msg_" + uuid.uuid4().hex
            item = {"type": "message", "id": identifier, "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": answer, "annotations": []}]}
            response = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "status": "completed",
                        "model": "fixture", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            events = [
                {"type": "response.created", "response": dict(response, status="in_progress", output=[])},
                {"type": "response.output_item.added", "output_index": 0, "item": dict(item, status="in_progress", content=[])},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": identifier, "delta": answer},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            data = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return subprocess.call(["codex", "app-server", "--listen", "stdio://"])
    finally:
        server.shutdown()


def sandbox_prefix(state, workspace, captures):
    actual_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()
    # Only the mount target uses the real path; its contents are never opened.
    return ["bwrap", "--ro-bind", "/", "/", "--bind", str(state), str(actual_home),
            "--bind", str(workspace), str(workspace), "--bind", str(captures), str(captures),
            "--unshare-net", "--unshare-pid", "--proc", "/proc", "--die-with-parent",
            "--chdir", str(workspace)]


def setup(root, label):
    base = root / label
    state, work, captures = base / "codex", base / "work", base / "captures"
    for path in (state, work, captures):
        path.mkdir(parents=True)
    # This is a synthetic store, not the user's config. No credential is needed.
    (state / "config.toml").write_text('''model = "fixture"
model_provider = "fixture"
approval_policy = "on-request"
sandbox_mode = "read-only"
[model_providers.fixture]
name = "Offline deterministic fixture"
base_url = "http://127.0.0.1:8765/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
''', encoding="utf-8")
    command = sandbox_prefix(state, work, captures) + [sys.executable, str(Path(__file__).resolve()), "--mock-child", str(captures)]
    return state, work, captures, command


def fixture_turn(client, thread_id, text):
    result = client.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text, "text_elements": []}]})
    turn_id = result["turn"]["id"]
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        result = client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        turn = next((t for t in result["thread"]["turns"] if t["id"] == turn_id), None)
        if turn and turn["status"] == "completed":
            return turn
        # A legacy read may briefly project an unfinished tail as interrupted.
        if turn and turn["status"] == "failed" and turn.get("error"):
            raise AssertionError("Offline fixture turn failed: " + json.dumps(turn.get("error")))
        time.sleep(0.05)
    raise AssertionError("Offline fixture turn timed out")


def projection(conversation):
    return [(m["role"], m["content"]) for m in conversation["messages"]]


def main(output=None):
    if not shutil.which("codex") or not shutil.which("bwrap"):
        raise SystemExit("Verification requires codex and bwrap; no unisolated fallback is allowed")
    with tempfile.TemporaryDirectory(prefix="contexthub-verify-") as temporary:
        root = Path(temporary)
        source_state, source_work, source_captures, source_command = setup(root, "source")
        target_state, target_work, target_captures, target_command = setup(root, "target")
        with ProbeClient(source_command, timeout=45) as client:
            started = client.request("thread/start", {"cwd": str(source_work), "ephemeral": False, "historyMode": "legacy"})
            source_id = started["thread"]["id"]
            fixture_turn(client, source_id, QUESTION)
            fixture_turn(client, source_id, QUESTION_TWO)
            exported = export_conversation(client, source_id)
            expected = [("user", QUESTION), ("assistant", ANSWER), ("user", QUESTION_TWO), ("assistant", ANSWER_TWO)]
            assert projection(exported) == expected, "Source text export did not match synthetic fixture"
            assert source_id in {s["id"] for s in list_sessions(client)}, "Source missing from session list"
            client.request("thread/unsubscribe", {"threadId": source_id})
        # Exercise the real portable format if available, including hash validation.
        from contexthub.package import build_package, read_package
        package_bytes = build_package([exported], "Offline Codex transfer")
        downloaded = read_package(package_bytes)["conversations"][0]
        assert projection(downloaded) == expected
        with ProbeClient(target_command, timeout=45) as client:
            existing = client.request("thread/start", {"cwd": str(target_work), "ephemeral": False})["thread"]["id"]
            fixture_turn(client, existing, "Existing target sentinel")
            existing_before = projection(export_conversation(client, existing))
            client.request("thread/unsubscribe", {"threadId": existing})
            requests_before_import = set(target_captures.glob("*.json"))
            receipt = import_conversation(client, downloaded, str(target_work), mode="context")
            target_id = receipt["thread_id"]
            assert receipt["injected"], receipt
            assert target_id not in {source_id, existing}, "Import did not create a unique new thread"
            client.request("thread/unsubscribe", {"threadId": target_id})
            assert set(target_captures.glob("*.json")) == requests_before_import, "Import unexpectedly made a provider request"
        # Restart the app-server against the same isolated target store.
        with ProbeClient(target_command, timeout=45) as client:
            resumed = client.request("thread/resume", {"threadId": target_id, "excludeTurns": True})
            assert resumed["thread"]["id"] == target_id
            assert resumed["approvalPolicy"] == "on-request"
            assert resumed["sandbox"]["type"] == "readOnly"
            pre_turn = client.request("thread/read", {"threadId": target_id, "includeTurns": True})["thread"]["turns"]
            fixture_turn(client, target_id, "Offline context check; this is a canned response, not model inference.")
            client.request("thread/unsubscribe", {"threadId": target_id})
            assert existing in {s["id"] for s in list_sessions(client)}, "Existing destination disappeared"
            assert projection(export_conversation(client, existing)) == existing_before, "Existing destination content changed"
        captured = [json.loads(p.read_text()) for p in set(target_captures.glob("*.json")) - requests_before_import]
        assert len(captured) == 1, "Unexpected number of offline continuation requests"
        model_input = captured[0].get("input", [])
        input_text = json.dumps(model_input, ensure_ascii=False)
        for marker in (QUESTION, ANSWER, QUESTION_TWO, ANSWER_TWO):
            assert any(marker == part.get("text") for item in model_input for part in item.get("content", []) if isinstance(part, dict)), "Imported text missing from restarted provider request"
        assert "Existing target sentinel" not in input_text
        with ProbeClient(source_command, timeout=45) as client:
            assert projection(export_conversation(client, source_id)) == expected, "Source content changed"
        report = {
            "codex_version": subprocess.check_output(["codex", "--version"], text=True).strip(),
            "isolation": "bwrap mount/network/PID namespaces; HOME and CODEX_HOME unchanged",
            "private_user_history_read": False, "real_model_calls": 0,
            "fixture_provider_requests": len(list(source_captures.glob("*.json"))) + len(list(target_captures.glob("*.json"))),
            "source_id": source_id, "target_id": target_id,
            "message_count": len(expected), "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
            "source_history_mode": started["thread"].get("historyMode", "legacy"),
            "target_history_mode": resumed["thread"].get("historyMode", "legacy"),
            "export_exact_text": True, "package_round_trip": True, "new_destination_id": True,
            "source_unchanged": True, "existing_target_preserved": True,
            "target_permissions_preserved": True, "restart_resume": True,
            "persisted_model_input_verified": True,
            "public_timeline_before_continuation_has_history": bool(pre_turn),
            "native_cli_ui": "not tested", "desktop_app_ui": "not tested",
            "real_model_continuation": "not tested",
            "import_receipt": receipt,
        }
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if output:
            Path(output).write_text(text + "\n", encoding="utf-8")
        print(text)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Optional JSON verification report path")
    parser.add_argument("--mock-child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mock_child:
        raise SystemExit(mock_child(args.mock_child))
    main(args.output)
