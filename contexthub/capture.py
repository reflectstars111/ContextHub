"""Read selected native conversations into a portable, deterministic snapshot.

Capture has no workspace writes and never starts a Codex model turn. Native
thread IDs are returned separately for local source bindings; only normalized
public conversation IDs enter the package.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

from .codex import CodexClient, CodexError, _turns, export_conversation, list_sessions
from .package import MAX_CONVERSATIONS, build_package, read_package, redact_conversations


class _CaptureReader:
    """Ensure every metadata read belongs to the requested native thread."""

    def __init__(self, client):
        self.client = client

    def request(self, method, params):
        if method not in {"thread/list", "thread/read", "thread/turns/list", "thread/items/list"}:
            raise CodexError("对话采集仅允许读取，不执行模型或会话修改")
        result = self.client.request(method, params)
        if method == "thread/read":
            thread = result.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != params.get("threadId"):
                raise CodexError("Codex 返回了不同的会话 ID；未采集或暂存任何内容")
        return result


def _identifier(value, label):
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(ord(char) < 32 for char in value)):
        raise CodexError(f"{label} 必须是非空会话或轮次 ID")
    return value


def _path_key(value):
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _bound_sources(sources):
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_CONVERSATIONS:
        raise CodexError(f"需要 1–{MAX_CONVERSATIONS} 个已绑定的 Codex 来源；请先 ctx add --codex")
    result, threads, conversations = [], set(), set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"provider", "thread_id", "conversation_id"}:
            raise CodexError("Codex 来源绑定必须含 provider、thread_id 和 conversation_id")
        if source["provider"] != "codex":
            raise CodexError("自动采集仅支持 Codex 来源绑定")
        identifier = _identifier(source["thread_id"], "thread_id")
        conversation = source["conversation_id"]
        if not isinstance(conversation, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", conversation):
            raise CodexError("来源绑定的 conversation_id 必须是规范对话标识")
        if identifier in threads or conversation in conversations:
            raise CodexError("来源绑定包含重复会话")
        threads.add(identifier)
        conversations.add(conversation)
        result.append(dict(source))
    return result


def _select_thread(client, workspace_path, thread_id, current_thread):
    if thread_id is not None:
        return thread_id
    if current_thread:
        return _identifier(current_thread, "CODEX_THREAD_ID")
    workspace_key = _path_key(workspace_path)
    matches = []
    for thread in list_sessions(client):
        cwd = thread.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            continue
        try:
            matches_workspace = _path_key(cwd) == workspace_key
        except (OSError, ValueError, RuntimeError):
            continue
        if matches_workspace:
            matches.append(thread["id"])
    if len(matches) != 1:
        reason = "没有找到" if not matches else f"找到 {len(matches)} 个"
        raise CodexError(f"{reason}与工作区目录完全匹配的 Codex 会话；请运行 ctx codex list，"
                         "再使用 ctx add --codex --thread <ID> 明确选择")
    return matches[0]


def _current_boundary(client, thread_id):
    thread = client.request("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread", {})
    turns = _turns(client, thread_id, thread.get("historyMode") or "legacy")
    if not turns:
        raise CodexError("当前 Codex 会话还没有可采集的历史正文；当前执行轮次不会被采集")
    return _identifier(turns[-1].get("id"), "当前轮次 ID")


def collect_codex(workspace_path, *, thread_id=None, before_turn=None, sources=None):
    """Capture one chosen thread, or refresh exactly the supplied local bindings.

    Explicit selection wins over CODEX_THREAD_ID. Without either, only one
    exact directory match can be selected. A current Codex thread omits its
    latest turn, even when an independent app-server reports it interrupted.
    The cutoff is recalculated on each call, never persisted in source bindings.

    All reads must succeed before a package is produced. The volatile exporter
    timestamp is omitted so unchanged native history stages unchanged bytes;
    workspace commits provide the save timestamp.
    """
    if thread_id is not None:
        _identifier(thread_id, "thread_id")
    if before_turn is not None:
        _identifier(before_turn, "before_turn")
    if sources is not None and thread_id is not None:
        raise CodexError("已绑定来源刷新不能同时指定 --thread")
    selected = _bound_sources(sources) if sources is not None else None
    if before_turn is not None and selected is not None and len(selected) != 1:
        raise CodexError("--before-turn 仅适用于单个 Codex 会话")

    current_thread = os.environ.get("CODEX_THREAD_ID")
    conversations, captures = [], []
    with CodexClient() as connection:
        client = _CaptureReader(connection)
        if selected is None:
            selected = [{"provider": "codex", "thread_id": _select_thread(
                client, workspace_path, thread_id, current_thread)}]
        for source in selected:
            identifier = source["thread_id"]
            cutoff = before_turn
            current_excluded = identifier == current_thread and cutoff is None
            if current_excluded:
                cutoff = _current_boundary(client, identifier)
            conversation = export_conversation(client, identifier, before_turn=cutoff)
            conversation["source"].pop("exported_at", None)
            warnings = conversation["source"]["warnings"]
            if current_excluded:
                warnings.append("已排除当前正在执行的轮次，避免把采集操作本身加入暂存区")
            warnings.append("Automatic redaction was applied; review selected text before publishing")
            conversations.append(conversation)
            captures.append({"thread_id": identifier, "current_turn_excluded": current_excluded,
                             "before_turn": cutoff})

    redacted = []
    for conversation, capture in zip(conversations, captures):
        cleaned, findings = redact_conversations([conversation])
        conversation = cleaned[0]
        redacted.append(conversation)
        capture.update(title=conversation["title"], message_count=len(conversation["messages"]),
                       warnings=conversation["source"]["warnings"], redactions=findings)
    title = redacted[0]["title"] if len(redacted) == 1 else "Codex conversations"
    blob = build_package(redacted, title=title, source={"adapter": "codex"})
    package = read_package(blob)
    bindings = []
    for source, conversation in zip(selected, package["conversations"]):
        canonical_id = conversation["id"]
        if "conversation_id" in source and source["conversation_id"] != canonical_id:
            raise CodexError("来源绑定的对话标识与导出结果不符；拒绝替换其他对话")
        bindings.append({"provider": "codex", "thread_id": source["thread_id"],
                         "conversation_id": canonical_id})
    return {"blob": blob, "sources": bindings, "captures": captures}
