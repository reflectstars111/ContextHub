"""Immutable snapshot editing and bounded, identity-based conversation diffs.

Revision annotations describe platform edits, not authenticity of the original
conversation. Stable IDs match messages; an imported provenance claim never
proves that a model produced its text. Relative moves use an O(n log n) longest
increasing subsequence, so inserting a message does not move every later one.
"""

from __future__ import annotations

import bisect
import copy
import hashlib
import json

from .package import (MAX_CONVERSATIONS, MAX_MESSAGES, PackageError,
                      _normalize_conversations, build_package)


MAX_DETAIL_MESSAGES = 200
MAX_DETAIL_TEXT = 4000
MAX_METADATA_CHARACTERS = 100_000
MAX_METADATA_NODES = 3000
_CONVERSATION_FIELDS = ("title", "context", "tags", "source", "annotations")
_MESSAGE_FIELDS = ("role", "content", "provenance", "transformations")
_LIBRARY_FIELDS = ("title", "description", "source", "code_checkpoint")


def _digest(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _changed_fields(before, after, fields):
    return [field for field in fields if before.get(field) != after.get(field)]


def _moved_ids(before_ids, after_ids):
    """Choose a deterministic minimum set of moves among IDs in both inputs."""
    positions = {identifier: index for index, identifier in enumerate(before_ids)}
    shared = [identifier for identifier in after_ids if identifier in positions]
    tails, tail_indices, previous = [], [], [-1] * len(shared)
    for index, identifier in enumerate(shared):
        position = positions[identifier]
        slot = bisect.bisect_left(tails, position)
        if slot:
            previous[index] = tail_indices[slot - 1]
        if slot == len(tails):
            tails.append(position)
            tail_indices.append(index)
        else:
            tails[slot] = position
            tail_indices[slot] = index
    unchanged = set()
    index = tail_indices[-1] if tail_indices else -1
    while index >= 0:
        unchanged.add(shared[index])
        index = previous[index]
    return set(shared) - unchanged


def _annotation(kind, detail, message_id=None):
    result = {"kind": "revision_" + kind, "source": "contexthub",
              "text": json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}
    if message_id is not None:
        result["message_id"] = message_id
    return result


def _is_revision_annotation(annotation):
    return (isinstance(annotation, dict)
            and isinstance(annotation.get("kind"), str)
            and annotation["kind"].startswith("revision_"))


def _merge_annotations(original, submitted):
    if not isinstance(submitted, list):
        raise PackageError("annotations must be a list")
    # Identical stored records may describe separate past edits. Preserve their
    # multiplicity and order; de-duplication only applies to new client entries.
    result = copy.deepcopy(original)
    seen = {_digest(entry) for entry in original}
    # Only the stored snapshot supplies existing platform revision annotations.
    for entry in submitted:
        if _is_revision_annotation(entry):
            continue
        key = _digest(entry)
        if key not in seen:
            result.append(copy.deepcopy(entry))
            seen.add(key)
    return result


def build_revision(base_package, payload):
    """Build a complete replacement snapshot while retaining edit provenance.

    Payload conversations/messages must have stable IDs. Existing source,
    provenance, transformations, and annotations cannot be erased by the client.
    Newly supplied source metadata is explicitly a user claim. Deletions and
    relative ordering changes receive portable revision annotations. Source
    authenticity, model continuation, and native restoration remain unverified.
    """
    if not isinstance(payload, dict):
        raise PackageError("Revision payload must be an object")
    submitted = payload.get("conversations")
    if not isinstance(submitted, list) or not 1 <= len(submitted) <= MAX_CONVERSATIONS:
        raise PackageError(f"A revision requires 1–{MAX_CONVERSATIONS} conversations")
    base_conversations = {item["id"]: item for item in base_package["conversations"]}
    conversations = copy.deepcopy(submitted)
    for conversation in conversations:
        if not isinstance(conversation, dict) or not isinstance(conversation.get("id"), str):
            raise PackageError("Every revision conversation requires a stable id")
        # Package creation can hash source-native conversation IDs. Revisions
        # must already use the canonical IDs read from the stored package, or
        # an alias could evade the original provenance match below and leak the
        # native ID into an audit record before final normalization.
        identity = _normalize_conversations([{
            "id": conversation["id"], "source": conversation.get("source", {}), "messages": []
        }])[0]["id"]
        if identity != conversation["id"]:
            raise PackageError("Revision conversation id must use its canonical package identity")
        messages = conversation.get("messages", [])
        if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
            raise PackageError(f"messages must be a list of at most {MAX_MESSAGES} messages")
        previous = base_conversations.get(conversation["id"])
        previous_messages = {message["id"]: message for message in previous["messages"]} if previous else {}
        old_annotations = previous.get("annotations", []) if previous else []
        annotations = _merge_annotations(old_annotations, conversation.get("annotations", []))
        if previous:
            conversation["source"] = copy.deepcopy(previous["source"])
        elif conversation.get("source"):
            if not isinstance(conversation["source"], dict):
                raise PackageError("conversation.source must be an object")
            conversation["source"]["verification"] = "user_claimed"
            annotations.append(_annotation("source_claimed", {"conversation_id": conversation["id"]}))
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                raise PackageError("Every revision message requires a stable id")
            original = previous_messages.get(message["id"])
            if original:
                message["provenance"] = copy.deepcopy(original["provenance"])
                message["transformations"] = list(original["transformations"])
                if _changed_fields(original, message, ("role", "content")):
                    if "human_edited" not in message["transformations"]:
                        message["transformations"].append("human_edited")
            else:
                supplied_source = message.get("provenance")
                if supplied_source is None:
                    message["provenance"] = "human_authored" if message.get("role") in ("user", "assistant") else "imported"
                if message["provenance"] != "human_authored":
                    transformations = message.setdefault("transformations", [])
                    if not isinstance(transformations, list):
                        raise PackageError("message.transformations must be a list")
                    if "source_claimed" not in transformations:
                        transformations.append("source_claimed")
        if annotations or (previous is not None and "annotations" in previous):
            conversation["annotations"] = annotations
        else:
            # A canonical submitted snapshot includes this revision's records.
            # After removing those records, preserve the base representation
            # of an absent/empty field so revalidation produces identical bytes.
            conversation.pop("annotations", None)
    # Normalize before computing audit records: IDs and metadata have the same
    # representation as the resulting package, and malformed fields fail early.
    conversations = _normalize_conversations(conversations)
    current = {item["id"]: item for item in conversations}
    first = conversations[0]
    pending = {item["id"]: [] for item in conversations}

    # A deleted conversation may carry the only copy of earlier revision logs.
    for old in base_package["conversations"]:
        if old["id"] in current:
            continue
        for entry in old.get("annotations", []):
            if _is_revision_annotation(entry):
                first.setdefault("annotations", []).append(copy.deepcopy(entry))
        pending[first["id"]].append(_annotation("conversation_removed", {
            "conversation_id": old["id"], "snapshot_sha256": _digest(old)}))

    for conversation in conversations:
        identifier = conversation["id"]
        previous = base_conversations.get(identifier)
        if previous is None:
            pending[identifier].append(_annotation("conversation_added", {
                "conversation_id": identifier, "message_ids": [message["id"] for message in conversation["messages"]]}))
            continue
        old_messages = {message["id"]: message for message in previous["messages"]}
        new_messages = {message["id"]: message for message in conversation["messages"]}
        moved = _moved_ids(old_messages, new_messages)
        records = []
        for message_id, message in new_messages.items():
            old = old_messages.get(message_id)
            if old is None:
                records.append({"id": message_id, "kind": "added", "after_sha256": _digest(message)})
            elif _changed_fields(old, message, _MESSAGE_FIELDS):
                records.append({"id": message_id, "kind": "edited", "fields": _changed_fields(old, message, _MESSAGE_FIELDS),
                                "before_sha256": _digest(old), "after_sha256": _digest(message)})
        for message_id, message in old_messages.items():
            if message_id not in new_messages:
                records.append({"id": message_id, "kind": "removed", "before_sha256": _digest(message)})
        if moved:
            records.append({"kind": "moved", "message_ids": sorted(moved),
                            "order": list(new_messages)})
        fields = _changed_fields(previous, conversation, _CONVERSATION_FIELDS)
        if records or fields:
            pending[identifier].append(_annotation("conversation_edited", {
                "conversation_id": identifier, "fields": fields, "messages": records}))

    title = payload.get("title", base_package["library"]["title"])
    description = payload.get("description", base_package["library"]["description"])
    library_changes = [field for field, value in (("title", title), ("description", description))
                       if value != base_package["library"][field]]
    if library_changes:
        pending[first["id"]].append(_annotation("library_edited", {"fields": library_changes}))
    if _moved_ids(base_conversations, current):
        pending[first["id"]].append(_annotation("conversations_moved", {"order": list(current)}))
    for conversation in conversations:
        if pending[conversation["id"]]:
            conversation.setdefault("annotations", []).extend(pending[conversation["id"]])
    checkpoint = payload.get("code_checkpoint", base_package["library"].get("code_checkpoint"))
    return build_package(conversations, title, description, source=base_package["library"]["source"],
                         code_checkpoint=checkpoint)


class _Details:
    """Shared budgets prevent many large metadata fields bypassing text caps."""

    def __init__(self):
        self.truncated = False
        self.message_count = 0
        self.characters = MAX_METADATA_CHARACTERS
        self.nodes = MAX_METADATA_NODES

    def text(self, value, *, metadata=False):
        limit = min(MAX_DETAIL_TEXT, self.characters) if metadata else MAX_DETAIL_TEXT
        result = value[:limit]
        if len(result) < len(value):
            self.truncated = True
        if metadata:
            self.characters -= len(result)
        return result

    def metadata(self, value, depth=0):
        if self.nodes <= 0 or depth > 6:
            self.truncated = True
            return None
        self.nodes -= 1
        if isinstance(value, str):
            return self.text(value, metadata=True)
        if isinstance(value, dict):
            if len(value) > 50:
                self.truncated = True
            return {self.text(str(key), metadata=True): self.metadata(item, depth + 1)
                    for key, item in list(value.items())[:50]}
        if isinstance(value, list):
            if len(value) > 50:
                self.truncated = True
            return [self.metadata(item, depth + 1) for item in value[:50]]
        return value

    def fields(self, before, after, fields):
        return [{"field": field, "before": self.metadata(before.get(field)),
                 "after": self.metadata(after.get(field))} for field in fields
                if before.get(field) != after.get(field)]

    def message(self, identifier, kind, before, after, *, moved=False):
        if self.message_count >= MAX_DETAIL_MESSAGES:
            self.truncated = True
            return None
        self.message_count += 1
        def body(message):
            return {"role": message["role"], "content": self.text(message["content"])} if message else None
        result = {"id": identifier, "kind": kind, "before": body(before), "after": body(after)}
        if moved:
            result["moved"] = True
        if before and after:
            metadata = self.fields(before, after, ("provenance", "transformations"))
            if metadata:
                result["metadata"] = metadata
        return result


def compare_packages(before, after):
    """Compare validated packages without mutation; summaries are never clipped.

    A metadata-only message edit counts as modified. A message both edited and
    moved is one ``modified`` detail with ``moved: true``, counted in both summary
    fields. Added/removed conversations contribute all their messages to the
    corresponding totals. Conversation order compares surviving stable IDs.
    """
    summary = {key: 0 for key in ("conversations_added", "conversations_removed", "conversations_modified",
                                 "messages_added", "messages_removed", "messages_modified", "messages_reordered")}
    details = _Details()
    old_conversations = {item["id"]: item for item in before["conversations"]}
    new_conversations = {item["id"]: item for item in after["conversations"]}
    result = {"summary": summary,
              "library_changes": details.fields(before["library"], after["library"], _LIBRARY_FIELDS),
              "conversation_order_changed": bool(_moved_ids(old_conversations, new_conversations)),
              "changes": [], "truncated": False}
    identifiers = list(new_conversations) + [identifier for identifier in old_conversations if identifier not in new_conversations]
    for identifier in identifiers:
        old, new = old_conversations.get(identifier), new_conversations.get(identifier)
        kind = "added" if old is None else "removed" if new is None else "modified"
        old_messages = {message["id"]: message for message in old["messages"]} if old else {}
        new_messages = {message["id"]: message for message in new["messages"]} if new else {}
        moved = _moved_ids(old_messages, new_messages)
        metadata = details.fields(old or {}, new or {}, _CONVERSATION_FIELDS)
        change = {"conversation_id": identifier, "title": (new or old)["title"],
                  "kind": kind, "metadata": metadata, "messages": [], "order_changed": bool(moved)}
        changed_messages = 0
        message_ids = list(new_messages) + [mid for mid in old_messages if mid not in new_messages]
        for mid in message_ids:
            previous, current = old_messages.get(mid), new_messages.get(mid)
            message_kind = None
            if previous is None:
                summary["messages_added"] += 1
                message_kind = "added"
            elif current is None:
                summary["messages_removed"] += 1
                message_kind = "removed"
            elif _changed_fields(previous, current, _MESSAGE_FIELDS):
                summary["messages_modified"] += 1
                message_kind = "modified"
            if mid in moved:
                summary["messages_reordered"] += 1
                message_kind = message_kind or "moved"
            if message_kind:
                changed_messages += 1
                item = details.message(mid, message_kind, previous, current, moved=mid in moved)
                if item is not None:
                    change["messages"].append(item)
        if kind != "modified" or metadata or changed_messages:
            summary["conversations_" + kind] += 1
            change["title"] = details.text(change["title"])
            result["changes"].append(change)
    result["truncated"] = details.truncated
    return result
