"""Versioned local Codex text-history reconstruction, never a native-state clone.

The private rollout schema is intentionally pinned to an actually tested CLI
version. Only new random session paths are written; no existing history, auth,
configuration, tool calls, or source instructions are read or copied.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import uuid

from . import safe_fs
from .codex import CodexError, TimelineError, _import_text, _project_messages


SUPPORTED_VERSIONS = frozenset({"0.153.4"})
SCHEMA = "codex-0.153.4-legacy-text-v1"


def _version(client):
    info = getattr(client, "info", {})
    match = re.match(r"^context_hub/([^\s]+)(?:\s|$)", info.get("userAgent", ""))
    version = match.group(1) if match else None
    if version not in SUPPORTED_VERSIONS:
        raise TimelineError("原生文本时间线仅验证 Codex 0.153.4；当前版本未验证。"
                         "可显式使用 --mode context 导入上下文，或安装受支持版本")
    home = info.get("codexHome")
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise TimelineError("Codex 未返回有效本地存储路径；请显式使用 --mode context")
    return version, Path(home)


def _rollout(identifier, version, target, provider, expected, now):
    stamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    records = []

    def append(kind, payload):
        records.append({"timestamp": stamp, "type": kind, "payload": payload})

    append("session_meta", {
        "session_id": identifier, "id": identifier, "timestamp": stamp,
        "cwd": str(target), "originator": "contexthub-text-import",
        "cli_version": version, "source": "cli", "model_provider": provider,
        "base_instructions": None, "history_mode": "legacy",
    })
    current, last = None, None
    for role, text in expected:
        if current is None or role == "user":
            if current is not None:
                append("event_msg", {"type": "task_complete", "turn_id": current,
                                     "last_agent_message": last})
            current, last = str(uuid.uuid4()), None
            append("event_msg", {"type": "task_started", "turn_id": current,
                                 "model_context_window": None, "collaboration_mode_kind": "default"})
        append("response_item", {
            "type": "message", "id": "msg_" + uuid.uuid4().hex, "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        })
        if role == "user":
            append("event_msg", {"type": "user_message", "message": text,
                                 "images": [], "local_images": [], "text_elements": []})
        else:
            append("event_msg", {"type": "agent_message", "message": text})
            last = text
    append("event_msg", {"type": "task_complete", "turn_id": current, "last_agent_message": last})
    return "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for record in records).encode("utf-8")


def _write_new(home, identifier, blob, now):
    """Exclusive creation with anchored parents. Never truncate or replace a file.

    A concurrent Codex scan can see the new file while it is being written; the
    explicit read/resume verification only runs after fsync and close. Readers
    which raced a short write can retry. Existing sessions cannot be affected.
    """
    directory = home / "sessions" / now.strftime("%Y/%m/%d")
    name = "rollout-" + now.strftime("%Y-%m-%dT%H-%M-%S") + "-" + identifier + ".jsonl"
    parent = safe_fs.directory(directory, create=True)
    created = False
    try:
        descriptor = safe_fs.open_file(parent, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as file:
            file.write(blob)
            file.flush()
            os.fsync(file.fileno())
        safe_fs.sync_dir(parent)
    except BaseException:
        if created:
            safe_fs.unlink(parent, name)
        raise
    finally:
        safe_fs.close(parent)
    return directory / name


def _verify(result, identifier, expected):
    thread = result.get("thread", {})
    if thread.get("id") != identifier or thread.get("historyMode") != "legacy":
        raise TimelineError("Codex 返回的目标时间线标识或格式不匹配")
    turns = thread.get("turns", [])
    if not turns or any(turn.get("status") != "completed" for turn in turns):
        raise TimelineError("Codex 未完整读取导入的文本回合")
    messages, warnings = _project_messages(turns, identifier)
    if warnings or [(m["role"], m["content"]) for m in messages] != expected:
        raise TimelineError("Codex 原生时间线读回与导入文本不一致")


def import_timeline(client, conversation, cwd):
    target, _, expected, warnings = _import_text(conversation, cwd)
    version, home = _version(client)
    # Reject symlink/junction ancestors before asking Codex to resolve project
    # config. No source path, provider, permission or instruction is used.
    target_fd = safe_fs.directory(target)
    safe_fs.close(target_fd)
    home_fd = safe_fs.directory(home)
    safe_fs.close(home_fd)
    config = client.request("config/read", {"cwd": str(target), "includeLayers": False}).get("config", {})
    provider = config.get("model_provider") or "openai"
    if not isinstance(provider, str) or not provider.strip():
        raise TimelineError("Codex 接收端模型配置无效")
    identifier = str(uuid.uuid4())
    if identifier == conversation.get("id"):
        raise TimelineError("生成的目标 ID 与来源重复，请重试")
    now = dt.datetime.now(dt.timezone.utc)
    blob = _rollout(identifier, version, target, provider, expected, now)
    path = _write_new(home, identifier, blob, now)
    warnings += [
        "Reconstructed user/assistant text timeline with new IDs and import timestamps; original turn boundaries, timestamps and native state are not restored",
        "Tools, attachments, hidden reasoning, instructions, permissions and credentials are not migrated; no model turn was started",
        "Native GUI rendering and model continuation are not tested by this import",
    ]
    receipt = {
        "source_id": conversation.get("id"), "thread_id": identifier,
        "mode": "timeline", "schema": SCHEMA, "codex_version": version,
        "message_count": len(expected), "rollout_path": str(path),
        "imported": True, "verified": False, "api_readback_verified": False,
        "persistence_verified": False, "resume_verified": False,
        "model_context_verified": False, "ui_history_verified": False,
        "resume_command": "codex resume " + identifier, "warnings": warnings,
    }
    try:
        _verify(client.request("thread/read", {"threadId": identifier, "includeTurns": True}), identifier, expected)
        receipt["api_readback_verified"] = True
        # A new process reads the disk history and resumes it without sending a
        # turn, which verifies persistence independently of the first process.
        with client.reopen() as verifier:
            _verify(verifier.request("thread/read", {"threadId": identifier, "includeTurns": True}), identifier, expected)
            receipt["persistence_verified"] = True
            _verify(verifier.request("thread/resume", {"threadId": identifier, "cwd": str(target)}), identifier, expected)
            receipt["resume_verified"] = True
            title = conversation.get("title")
            if isinstance(title, str) and title.strip():
                verifier.request("thread/name/set", {"threadId": identifier, "name": title[:200]})
            verifier.request("thread/unsubscribe", {"threadId": identifier})
        receipt["verified"] = True
    except (CodexError, OSError) as exc:
        detail = str(exc) if isinstance(exc, TimelineError) else type(exc).__name__
        receipt.update(mode="failed", error="文本时间线已创建，但验证失败：" + detail)
        warnings.append("The new import file is retained for inspection; no existing session was overwritten. Context mode is never selected silently")
    return receipt
