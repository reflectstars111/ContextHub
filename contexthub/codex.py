"""Codex app-server adapter. No model turn is started by transfer operations.

Exports are a text projection of a stable prefix of terminal turns. Imports
rebuild a native text timeline on explicitly supported Codex versions, or use
the explicit context-only compatibility mode. Neither restores source machine
state, permissions, instructions, or tool execution.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import platform
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time


MAX_CONVERSATION_BYTES = 5 * 1024 * 1024
MAX_MESSAGES = 2000
MAX_RPC_BYTES = 16 * 1024 * 1024
MAX_PAGES = 10000
SOURCES = ["cli", "vscode", "exec", "appServer", "unknown"]
SUBAGENTS = ["subAgent", "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther"]


class CodexError(ValueError):
    """Protocol, compatibility or incomplete snapshot error."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class TimelineError(CodexError):
    """A controlled, user-facing timeline compatibility or validation message."""


def _default_command(*, windows=None, machine=None):
    """Resolve a native Windows executable without routing through cmd.exe."""
    windows = os.name == 'nt' if windows is None else windows
    override = os.environ.get('CONTEXTHUB_CODEX_EXE')
    suffix = ['app-server', '--listen', 'stdio://']
    if override:
        executable = Path(override).expanduser()
        if not executable.is_file() or (windows and executable.suffix.lower() != '.exe'):
            raise CodexError('CONTEXTHUB_CODEX_EXE 必须指向已安装的 Codex 可执行文件（Windows 为 codex.exe）')
        return [str(executable.resolve()), *suffix]
    if not windows:
        return ['codex', *suffix]
    direct = shutil.which('codex.exe')
    if direct:
        return [direct, *suffix]
    # npm on Windows exposes codex.cmd. Resolve its adjacent native optional
    # package instead of letting shell metacharacters enter a batch launcher.
    shim = shutil.which('codex.cmd')
    if shim:
        package = Path(shim).parent / 'node_modules' / '@openai' / 'codex'
        architecture = (machine or platform.machine()).lower()
        target, variant = (('aarch64', 'arm64') if architecture in ('arm64', 'aarch64')
                           else ('x86_64', 'x64'))
        triple = target + '-pc-windows-msvc'
        roots = (package.parent / ('codex-win32-' + variant),
                 package / 'node_modules' / '@openai' / ('codex-win32-' + variant), package)
        for root in roots:
            for binary_directory in ('bin', 'codex'):
                candidate = root / 'vendor' / triple / binary_directory / 'codex.exe'
                if candidate.is_file():
                    return [str(candidate.resolve()), *suffix]
    raise CodexError('未找到 Windows Codex 可执行文件。请先安装 Codex CLI，或设置 CONTEXTHUB_CODEX_EXE 为 codex.exe 的完整路径')


class CodexClient:
    """Own one stdio app-server process, with bounded synchronous requests."""

    def __init__(self, command=None, timeout=30):
        self.command = command or _default_command()
        if isinstance(self.command, str):
            if os.name == 'nt':
                raise CodexError('Windows 自定义命令请传参数列表；可用 CONTEXTHUB_CODEX_EXE 指定 Codex 路径')
            self.command = shlex.split(self.command)
        if os.name == 'nt' and Path(self.command[0]).suffix.lower() in ('.bat', '.cmd'):
            raise CodexError('请直接使用可执行文件；不通过 Windows 批处理脚本启动 Codex')
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.process = None
        self._messages = queue.Queue(maxsize=128)
        self._lock = threading.Lock()
        self._sequence = 0
        self.info = {}

    def __enter__(self):
        if self.process is not None:
            raise CodexError("CodexClient is already open")
        try:
            self.process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=(os.name == "posix"),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
        except OSError as exc:
            raise CodexError(f"Cannot start Codex app-server: {exc}") from exc
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.info = self.request("initialize", {
                "clientInfo": {"name": "context_hub", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False},
            })
            self._send({"method": "initialized"})
        except Exception:
            self.close()
            raise
        return self

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_RPC_BYTES + 1)
                if not line:
                    self._messages.put(CodexError("Codex app-server closed its output"))
                    return
                if len(line) > MAX_RPC_BYTES:
                    raise CodexError("Codex response exceeds the protocol size limit")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise CodexError("Invalid JSON-RPC response object")
                self._messages.put(value)
        except (OSError, ValueError) as exc:
            self._messages.put(CodexError(f"Cannot read Codex protocol: {type(exc).__name__}"))

    def _send(self, value):
        if self.process is None or self.process.poll() is not None:
            raise CodexError("Codex app-server is not running")
        data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > MAX_RPC_BYTES:
            raise CodexError("Codex request exceeds the protocol size limit")
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexError("Codex app-server closed its input") from exc

    def request(self, method, params):
        with self._lock:
            self._sequence += 1
            request_id = self._sequence
            self._send({"id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexError(f"Codex request timed out: {method}")
                try:
                    message = self._messages.get(timeout=remaining)
                except queue.Empty as exc:
                    raise CodexError(f"Codex request timed out: {method}") from exc
                if isinstance(message, Exception):
                    raise message
                if "method" in message:
                    if "id" in message:
                        # A transfer never grants a tool, permission, or auth approval.
                        self._send({"id": message["id"], "error": {
                            "code": -32601, "message": "ContextHub transfer does not handle server requests",
                        }})
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    error = message["error"]
                    raise CodexError(str(error.get("message", "Codex request failed")), error.get("code"))
                if not isinstance(message.get("result"), dict):
                    raise CodexError("Invalid JSON-RPC result")
                return message["result"]

    def close(self):
        process = self.process
        if process is None:
            return
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=min(self.timeout, 3))
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
        if process.stdout:
            process.stdout.close()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def reopen(self):
        """A separate process for verifying persisted imports, using the same target."""
        return CodexClient(command=self.command, timeout=self.timeout)


def _pages(client, method, params):
    cursor = None
    seen = set()
    for _ in range(MAX_PAGES):
        request = dict(params, limit=100)
        if cursor is not None:
            request["cursor"] = cursor
        result = client.request(method, request)
        if not isinstance(result.get("data"), list) or "nextCursor" not in result:
            raise CodexError(f"Incomplete pagination response: {method}")
        yield from result["data"]
        cursor = result["nextCursor"]
        if cursor is None:
            return
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise CodexError(f"Repeated or invalid pagination cursor: {method}")
        seen.add(cursor)
    raise CodexError(f"Pagination limit reached before completion: {method}")


def list_sessions(client, include_archived=False, include_subagents=False):
    """List all selected pages without triggering rollout scan-and-repair."""
    result, seen = [], set()
    sources = SOURCES + (SUBAGENTS if include_subagents else [])
    for archived in ([False, True] if include_archived else [False]):
        for thread in _pages(client, "thread/list", {
            "archived": archived, "sourceKinds": sources, "modelProviders": [],
            "sortKey": "updated_at", "sortDirection": "desc", "useStateDbOnly": True,
        }):
            identifier = thread.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise CodexError("Thread list contains an invalid thread ID")
            if identifier not in seen:
                result.append(dict(thread, archived=archived))
                seen.add(identifier)
    return result


def _turns(client, thread_id, history_mode):
    if history_mode == "legacy":
        result = client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        turns = result.get("thread", {}).get("turns")
        if not isinstance(turns, list):
            raise CodexError("Codex did not return legacy thread history")
        return turns
    turns = list(_pages(client, "thread/turns/list", {
        "threadId": thread_id, "sortDirection": "asc", "itemsView": "full",
    }))
    for turn in turns:
        if turn.get("itemsView", "full") != "full":
            turn["items"] = [entry["item"] for entry in _pages(client, "thread/items/list", {
                "threadId": thread_id, "turnId": turn["id"], "sortDirection": "asc",
            })]
            turn["itemsView"] = "full"
    return turns


def _terminal_prefix(turns, before_turn=None):
    prefix = []
    seen = set()
    for turn in turns:
        identifier = turn.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise CodexError("Thread contains missing or duplicate turn IDs")
        seen.add(identifier)
    if before_turn is not None and before_turn not in seen:
        raise CodexError("Requested before-turn boundary was not found in thread history")
    for turn in turns:
        identifier = turn["id"]
        if identifier == before_turn or turn.get("status") not in {"completed", "interrupted", "failed"}:
            return prefix, identifier
        prefix.append(turn)
    return prefix, None


def _timestamp(*values):
    """Normalize a declared timestamp; missing/ambiguous values stay absent.

    Current Codex turn.startedAt is Unix seconds. Millisecond epochs and
    timezone-qualified ISO values are accepted for adapter compatibility.
    The thread creation time and export clock are never message substitutes.
    """
    for value in values:
        try:
            if type(value) in (int, float):
                if value < 0:
                    continue
                seconds = value / 1000 if value >= 100_000_000_000 else value
                parsed = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
            elif isinstance(value, str) and len(value) <= 64 and 'T' in value:
                parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    continue
                parsed = parsed.astimezone(dt.timezone.utc)
            else:
                continue
            return parsed.isoformat().replace('+00:00', 'Z')
        except (OverflowError, OSError, ValueError):
            continue
    return None


def _project_messages(turns, thread_id):
    messages, warnings = [], []
    for turn in turns:
        turn_time = _timestamp(*(turn.get(key) for key in
                                 ('startedAt', 'started_at', 'createdAt', 'created_at', 'timestamp')))
        for index, item in enumerate(turn.get("items", [])):
            kind = item.get("type", "unknown")
            if kind == "userMessage":
                chunks = []
                for content in item.get("content", []):
                    if content.get("type") == "text" and isinstance(content.get("text"), str):
                        chunks.append(content["text"])
                    else:
                        warnings.append(f"Omitted non-text user content: {content.get('type', 'unknown')}")
                text = "\n".join(chunks)
                role = "user"
            elif kind == "agentMessage":
                text, role = item.get("text", ""), "assistant"
            else:
                warnings.append(f"Omitted Codex item: {kind}")
                continue
            if not isinstance(text, str):
                raise CodexError("Codex message text is not a string")
            if not text:
                warnings.append(f"Omitted empty {role} message in turn {turn['id']}")
                continue
            item_id = item.get("id") or f"{turn['id']}-{index}"
            item_time = _timestamp(*(item.get(key) for key in ('createdAt', 'created_at', 'timestamp')))
            provenance = {"provider": "codex", "thread_id": thread_id,
                          "turn_id": turn["id"], "item_id": item_id}
            transformations = ["codex-user-assistant-projection"]
            if item_time or turn_time:
                provenance['created_at'] = item_time or turn_time
                if not item_time:
                    transformations.append('codex-turn-start-timestamp')
                    warnings.append('Some message timestamps record the turn start time, not the individual message time')
            messages.append({
                "id": f"{turn['id']}:{item_id}", "role": role, "content": text,
                "provenance": provenance,
                "transformations": transformations,
            })
            if len(messages) > MAX_MESSAGES:
                raise CodexError("Conversation exceeds 2000 messages; choose a smaller completed range")
    return messages, list(dict.fromkeys(warnings))


def export_conversation(client, thread_id, *, before_turn=None):
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("thread_id is required")
    if before_turn is not None and (not isinstance(before_turn, str) or not before_turn):
        raise ValueError("before_turn must be a nonempty turn ID")
    thread = client.request("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread", {})
    history_mode = thread.get("historyMode") or "legacy"
    captured, boundary = _terminal_prefix(_turns(client, thread_id, history_mode), before_turn)
    # Compare complete turn objects, including status and items. Later terminal
    # turns may be appended without changing the selected snapshot.
    current, _ = _terminal_prefix(_turns(client, thread_id, history_mode), before_turn)
    if current[:len(captured)] != captured:
        raise CodexError("Thread history changed during export; retry for a stable snapshot")
    messages, warnings = _project_messages(captured, thread_id)
    warnings.append("Export contains visible user/assistant timeline text from terminal turns only; injected context and hidden system/developer/tool state are not included")
    unsuccessful = [turn for turn in captured if turn["status"] != "completed"]
    for turn in unsuccessful:
        warnings.append(f"Turn {turn['id']} ended as {turn['status']}; its visible text may be partial")
    if boundary:
        reason = "requested boundary" if boundary == before_turn else "unfinished or unknown-status turn"
        warnings.append(f"Stopped before {reason} {boundary}; this turn and later turns excluded")
    if not messages:
        raise CodexError("No visible user/assistant messages in the selected terminal history; refusing an empty export. Injected context may not appear in the Codex timeline")
    result = {
        "schema_version": "0.1",
        "id": thread_id, "title": thread.get("name") or "Codex conversation",
        "context": "", "tags": [], "messages": messages,
        "source": {"provider": "codex", "thread_id": thread_id,
                   "history_mode": history_mode,
                   "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "completed_turn_ids": [t["id"] for t in captured if t["status"] == "completed"],
                   "completeness": "partial-visible-text" if boundary or unsuccessful else "visible-text-only",
                   "warnings": warnings},
    }
    if isinstance(thread.get("cliVersion"), str) and thread["cliVersion"]:
        result["source"]["cli_version"] = thread["cliVersion"]
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_CONVERSATION_BYTES:
        raise CodexError("Conversation exceeds 5 MiB; choose a smaller completed range")
    return result


def _import_text(conversation, cwd):
    """Validate untrusted package text before any Codex mutation."""
    target = Path(cwd).expanduser().absolute()
    if not target.is_dir():
        raise ValueError("Import cwd must be an existing directory")
    if not isinstance(conversation, dict) or not isinstance(conversation.get("messages"), list):
        raise ValueError("A conversation with a messages list is required")
    if len(conversation["messages"]) > MAX_MESSAGES:
        raise ValueError("Import exceeds 2000 messages; choose a smaller conversation")
    items, expected, warnings = [], [], []
    for message in conversation["messages"]:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}:
            raise ValueError("Only user, assistant and reference tool roles are accepted")
        role, content = message["role"], message.get("content")
        if not isinstance(content, str):
            raise ValueError("Message content must be text")
        if role == "tool":
            warnings.append("Omitted tool reference message; imported tools are never executed")
            continue
        if not content:
            warnings.append(f"Omitted empty {role} message")
            continue
        items.append({"type": "message", "role": role, "content": [{
            "type": "input_text" if role == "user" else "output_text", "text": content,
        }]})
        expected.append((role, content))
    if not items:
        raise ValueError("Conversation contains no importable user/assistant text")
    if len(json.dumps(items, ensure_ascii=False).encode("utf-8")) > MAX_CONVERSATION_BYTES:
        raise ValueError("Import context exceeds 5 MiB; select a smaller conversation")
    if conversation.get("context"):
        warnings.append("Conversation context metadata remains in the package and is not injected as instructions")
    return target, items, expected, warnings


def import_conversation(client, conversation, cwd, *, mode="timeline"):
    """Create a fresh text timeline, or explicitly inject context only."""
    if mode not in ("timeline", "context"):
        raise ValueError("Import mode must be timeline or context")
    if mode == "timeline":
        from .codex_timeline import import_timeline
        return import_timeline(client, conversation, cwd)
    return _import_context(client, conversation, cwd)


def _import_context(client, conversation, cwd):
    """Inject text context into a fresh thread using target-local configuration.

    `verified` only reports observed public API content readback. A successful
    injection alone does not verify restart persistence, UI, or model inference.
    """
    target, items, expected, warnings = _import_text(conversation, cwd)
    thread = client.request("thread/start", {"cwd": str(target), "ephemeral": False}).get("thread", {})
    identifier = thread.get("id")
    if not isinstance(identifier, str) or not identifier or identifier == conversation.get("id"):
        raise CodexError("Codex did not create a new destination thread ID")
    receipt = {
        "source_id": conversation.get("id"), "thread_id": identifier,
        "mode": "context-injection", "warnings": warnings, "verified": False,
        "injected": False, "persistence_verified": False,
        "model_context_verified": False, "ui_history_verified": False,
        "message_count": len(items),
    }
    try:
        client.request("thread/inject_items", {"threadId": identifier, "items": items})
        receipt["injected"] = True
    except CodexError as exc:
        warnings.append(f"Injection failed; a new empty or partial thread may remain: {exc}")
        receipt["mode"] = "failed"
        return receipt
    if isinstance(conversation.get("title"), str) and conversation["title"].strip():
        try:
            client.request("thread/name/set", {"threadId": identifier, "name": conversation["title"][:200]})
        except CodexError as exc:
            warnings.append(f"Could not set destination title: {exc}")
    try:
        read = client.request("thread/read", {"threadId": identifier, "includeTurns": True})
        projected, _ = _project_messages(read.get("thread", {}).get("turns", []), identifier)
        matched = [(m["role"], m["content"]) for m in projected] == expected
        receipt["verified"] = matched
        receipt["ui_history_verified"] = False  # API data does not prove any UI rendered it.
        receipt["api_readback_verified"] = matched
        if not matched:
            warnings.append("Codex accepted context injection but its public timeline did not expose matching messages; native history display is unverified")
    except CodexError as exc:
        receipt["api_readback_verified"] = False
        warnings.append(f"Public API readback is unavailable: {exc}")
    warnings.append("Restart persistence, native CLI/App display and model continuation are not verified by this import; no model turn was started")
    receipt["warnings"] = list(dict.fromkeys(warnings))
    return receipt
