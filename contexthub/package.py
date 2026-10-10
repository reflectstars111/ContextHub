"""A bounded, inert, dependency-free portable conversation format.

Only normalized text and explicit public metadata enter a contextpack. ZIPs
are inspected in memory and never extracted. Hashes provide integrity, not
authenticity. ``redact_conversations`` is a best-effort convenience detector;
users must still review the selected text before publishing it.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import stat
import zipfile
import zlib


SCHEMA_VERSION = "0.1"
MAX_CONVERSATIONS = 200
MAX_MESSAGES = 2000
MAX_CONVERSATION_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_BYTES = 110 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_FILES = MAX_CONVERSATIONS * 2 + 3
_ROLES = frozenset({"user", "assistant", "tool"})
_SOURCE_KEYS = frozenset({
    "provider", "platform", "adapter", "kind", "version", "cli_version",
    "thread_id", "conversation_id", "source_thread_id", "history_mode",
    "exported_at", "created_at", "updated_at", "url",
    "public_url", "title", "warnings", "completed_turn_ids", "method",
    "platform_claimed", "verification", "completeness",
})
_PROVENANCE_KEYS = frozenset({
    "provider", "platform", "thread_id", "conversation_id", "turn_id",
    "item_id", "message_id", "original_role", "source_id", "source_url",
    "created_at", "type",
})
_ANNOTATION_KEYS = frozenset({"kind", "text", "message_id", "warning", "source"})
_FILE_PATTERN = re.compile(r"(?:conversations/[A-Za-z0-9_-]{1,128}\.json|readable/[A-Za-z0-9_-]{1,128}\.md)\Z")
_ROOT_FILES = frozenset({"manifest.json", "library.json", "compatibility.json"})
_PRIVATE_ID_KEYS = frozenset({"thread_id", "source_thread_id", "conversation_id"})


class PackageError(ValueError):
    """Invalid, oversized, or internally inconsistent portable data."""


def _text(value, label, maximum=MAX_CONVERSATION_BYTES, *, nonempty=False):
    if not isinstance(value, str):
        raise PackageError(f"{label} must be text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PackageError(f"{label} contains invalid Unicode") from exc
    if size > maximum:
        raise PackageError(f"{label} exceeds the {maximum}-byte limit")
    if nonempty and (not value.strip() or any(ord(char) < 32 for char in value)):
        raise PackageError(f"{label} must be a nonempty identifier without control characters")
    return value


def _text_list(value, label, maximum=20000):
    if not isinstance(value, list) or len(value) > maximum:
        raise PackageError(f"{label} must be a list of at most {maximum} text values")
    return [_text(item, label) for item in value]


def _metadata(value, label, allowed):
    if not isinstance(value, dict):
        raise PackageError(f"{label} must be an object")
    result = {}
    for key in allowed:
        if key not in value:
            continue
        if key in {"warnings", "completed_turn_ids"}:
            result[key] = _text_list(value[key], f"{label}.{key}")
        else:
            result[key] = _text(value[key], f"{label}.{key}")
            if key in _PRIVATE_ID_KEYS:
                result[key] = _public_id(result[key])
    return result


def _public_id(value):
    """Keep stable public references without embedding source-native thread IDs."""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _provenance(value):
    if isinstance(value, str):
        if value not in {"imported", "human_authored"}:
            raise PackageError("Message provenance must be imported or human_authored")
        return value
    if not isinstance(value, dict) or not set(value) <= _PROVENANCE_KEYS:
        raise PackageError("Message provenance must be a recognized string or bounded adapter metadata")
    return _metadata(value, "message.provenance", _PROVENANCE_KEYS)


def _annotations(value):
    if not isinstance(value, list) or len(value) > 20000:
        raise PackageError("annotations must be a bounded list")
    return [
        _metadata(item, "annotation", _ANNOTATION_KEYS) if isinstance(item, dict)
        else _text(item, "annotation")
        for item in value
    ]


def _normalize_conversations(conversations):
    if not isinstance(conversations, list) or not 1 <= len(conversations) <= MAX_CONVERSATIONS:
        raise PackageError(f"A package requires 1–{MAX_CONVERSATIONS} conversations")
    normalized = []
    seen_conversations = set()
    for number, conversation in enumerate(conversations, 1):
        if not isinstance(conversation, dict):
            raise PackageError("Each conversation must be an object")
        if conversation.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise PackageError("Unsupported conversation schema_version")
        identifier = _text(conversation.get("id", f"conversation-{number:04d}"), "conversation.id", 512, nonempty=True)
        raw_source = conversation.get("source", {})
        source = _metadata(raw_source, "conversation.source", _SOURCE_KEYS)
        if any(identifier == raw_source.get(key) for key in _PRIVATE_ID_KEYS):
            identifier = _public_id(identifier)
        if identifier in seen_conversations:
            raise PackageError(f"Duplicate conversation ID: {identifier}")
        seen_conversations.add(identifier)
        messages = conversation.get("messages", [])
        if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
            raise PackageError(f"messages must be a list of at most {MAX_MESSAGES} text messages")
        item = {
            "schema_version": SCHEMA_VERSION,
            "id": identifier,
            "title": _text(conversation.get("title", "Untitled conversation"), "conversation.title", 16384),
            "context": _text(conversation.get("context", ""), "conversation.context"),
            "tags": _text_list(conversation.get("tags", []), "conversation.tags", 100),
            "source": source,
            "messages": [],
        }
        seen_messages = set()
        for index, message in enumerate(messages, 1):
            if not isinstance(message, dict):
                raise PackageError("Each message must be an object")
            message_id = _text(message.get("id", f"message-{index:06d}"), "message.id", 512, nonempty=True)
            if message_id in seen_messages:
                raise PackageError(f"Duplicate message ID in {identifier}: {message_id}")
            seen_messages.add(message_id)
            role = message.get("role")
            if not isinstance(role, str) or role not in _ROLES:
                raise PackageError("Message role must be user, assistant, or tool; privileged instructions are not portable")
            item["messages"].append({
                "id": message_id,
                "role": role,
                "content": _text(message.get("content"), "message.content"),
                "provenance": _provenance(message.get("provenance", "imported")),
                "transformations": _text_list(message.get("transformations", []), "message.transformations", 100),
            })
        if "annotations" in conversation:
            item["annotations"] = _annotations(conversation["annotations"])
        normalized.append(item)
    return normalized


def _json_bytes(value):
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise PackageError("Data is not valid bounded UTF-8 JSON") from exc


def _markdown(conversation):
    """One deterministic projection of the normalized primary data."""
    parts = [f"# {conversation['title']}", ""]
    if conversation["context"]:
        parts.extend([conversation["context"], ""])
    if conversation["tags"]:
        parts.extend(["Tags: " + ", ".join(conversation["tags"]), ""])
    for message in conversation["messages"]:
        role = "Tool (historical reference)" if message["role"] == "tool" else message["role"].capitalize()
        parts.extend([f"## {role}", "", message["content"], ""])
    return ("\n".join(parts).rstrip("\n") + "\n").encode("utf-8")


def _compatibility(conversations, source):
    warnings = [
        "This package contains selected conversation text and public metadata, not native session state.",
        "Native UI restoration and paid-model continuation are not verified by package validation.",
        "Automatic redaction is best effort; review selected content before publishing.",
    ]
    warnings.extend(source.get("warnings", []))
    for conversation in conversations:
        warnings.extend(conversation["source"].get("warnings", []))
        if any(message["role"] == "tool" for message in conversation["messages"]):
            warnings.append("Tool messages are historical reference text; importing them must not execute tools.")
    return {
        "schema_version": SCHEMA_VERSION,
        "content_mode": "normalized_text",
        "native_restore_verified": False,
        "model_continuation_verified": False,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _check_size(path, data):
    limit = MAX_METADATA_BYTES if path in _ROOT_FILES else MAX_CONVERSATION_BYTES
    if len(data) > limit:
        raise PackageError(f"{path} exceeds its {limit}-byte limit")


def build_package(conversations, title, description='', source=None, code_checkpoint=None) -> bytes:
    """Normalize selected conversations and return a deterministic .contextpack.

    Unknown fields are omitted rather than exporting arbitrary application
    state. System/developer roles are rejected. Caller objects are not mutated.
    """
    conversations = _normalize_conversations(conversations)
    source = _metadata({} if source is None else source, "source", _SOURCE_KEYS)
    library = {
        "schema_version": SCHEMA_VERSION,
        "title": _text(title, "library.title", 16384),
        "description": _text(description, "library.description", 262144),
        "source": source,
        "conversation_ids": [conversation["id"] for conversation in conversations],
    }
    if code_checkpoint is not None:
        from .checkpoints import validate_checkpoint
        library["code_checkpoint"] = validate_checkpoint(code_checkpoint)
    entries = {
        "library.json": _json_bytes(library),
        "compatibility.json": _json_bytes(_compatibility(conversations, source)),
    }
    for number, conversation in enumerate(conversations, 1):
        entries[f"conversations/{number:04d}.json"] = _json_bytes(conversation)
        entries[f"readable/{number:04d}.md"] = _markdown(conversation)
    manifest = {
        "format": "contextpack",
        "schema_version": SCHEMA_VERSION,
        "conversation_count": len(conversations),
        "files": [
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(entries.items())
        ],
    }
    entries["manifest.json"] = _json_bytes(manifest)
    for path, data in entries.items():
        _check_size(path, data)
    if sum(len(data) for data in entries.values()) > MAX_TOTAL_BYTES:
        raise PackageError("Decompressed package exceeds 100 MiB")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in sorted(entries.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return output.getvalue()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(data, path):
    def invalid_constant(value):
        raise PackageError(f"Nonfinite JSON value in {path}: {value}")

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PackageError(f"Invalid JSON in {path}") from exc
    if not isinstance(value, dict):
        raise PackageError(f"{path} must contain a JSON object")
    return value


def _read_entries(data):
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_ARCHIVE_BYTES:
        raise PackageError("Package must be ZIP bytes no larger than 110 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not 5 <= len(infos) <= MAX_FILES:
                raise PackageError("Invalid number of files in package")
            names = set()
            total = 0
            for info in infos:
                path = info.filename
                # orig_filename catches the NUL truncation performed by ZipInfo.
                if path != info.orig_filename or (path not in _ROOT_FILES and not _FILE_PATTERN.fullmatch(path)):
                    raise PackageError(f"Unsafe or unsupported ZIP path: {path!r}")
                if path in names:
                    raise PackageError(f"Duplicate ZIP path: {path}")
                names.add(path)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if mode not in (0, stat.S_IFREG) or info.is_dir() or info.external_attr & 0x10:
                    raise PackageError("Only regular files are permitted in a package")
                if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise PackageError("Encrypted or unsupported ZIP compression")
                limit = MAX_METADATA_BYTES if path in _ROOT_FILES else MAX_CONVERSATION_BYTES
                if not 0 <= info.file_size <= limit:
                    raise PackageError(f"{path} exceeds its decompressed size limit")
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise PackageError("Decompressed package exceeds 100 MiB")
            if not _ROOT_FILES <= names:
                raise PackageError("Package is missing required root files")
            entries = {}
            actual_total = 0
            for info in infos:
                limit = MAX_METADATA_BYTES if info.filename in _ROOT_FILES else MAX_CONVERSATION_BYTES
                with archive.open(info) as stream:
                    contents = stream.read(limit + 1)
                    if len(contents) != info.file_size or len(contents) > limit or stream.read(1):
                        raise PackageError("ZIP entry length does not match its declared bounded size")
                actual_total += len(contents)
                if actual_total > MAX_TOTAL_BYTES:
                    raise PackageError("Decompressed package exceeds 100 MiB")
                entries[info.filename] = contents
            return entries
    except PackageError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError, NotImplementedError, zlib.error) as exc:
        raise PackageError("Invalid or corrupt ZIP package") from exc


def read_package(data) -> dict:
    """Validate a portable package, returning manifest/library/data/compatibility."""
    entries = _read_entries(data)
    manifest = _load_json(entries["manifest.json"], "manifest.json")
    if set(manifest) != {"format", "schema_version", "conversation_count", "files"}:
        raise PackageError("Unsupported manifest fields")
    if manifest["format"] != "contextpack" or manifest["schema_version"] != SCHEMA_VERSION:
        raise PackageError("Unsupported package format or schema_version")
    count = manifest["conversation_count"]
    if type(count) is not int or not 1 <= count <= MAX_CONVERSATIONS:
        raise PackageError("Invalid conversation count")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != len(entries) - 1:
        raise PackageError("Manifest must list every file exactly once")
    listed = set()
    for record in files:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise PackageError("Invalid manifest file record")
        path = record["path"]
        if not isinstance(path, str) or path == "manifest.json" or path not in entries or path in listed:
            raise PackageError("Manifest has duplicate, missing, or self-referencing entries")
        listed.add(path)
        contents = entries[path]
        if type(record["size"]) is not int or record["size"] != len(contents):
            raise PackageError(f"Size mismatch: {path}")
        if not isinstance(record["sha256"], str) or record["sha256"] != hashlib.sha256(contents).hexdigest():
            raise PackageError(f"SHA-256 mismatch: {path}")
    if listed != set(entries) - {"manifest.json"}:
        raise PackageError("Archive contents do not match the manifest")
    conversation_paths = sorted(path for path in entries if path.startswith("conversations/"))
    readable_paths = {path for path in entries if path.startswith("readable/")}
    if len(conversation_paths) != count or len(readable_paths) != count:
        raise PackageError("Conversation and readable file counts disagree")
    conversations = [_load_json(entries[path], path) for path in conversation_paths]
    normalized = _normalize_conversations(conversations)
    if normalized != conversations:
        raise PackageError("Conversation files must contain only canonical fields and defaults")
    for path, conversation in zip(conversation_paths, normalized):
        readable_path = "readable/" + path.removeprefix("conversations/").removesuffix(".json") + ".md"
        if entries.get(readable_path) != _markdown(conversation):
            raise PackageError("Readable Markdown is not the projection of its conversation")
    library = _load_json(entries["library.json"], "library.json")
    expected_library = {
        "schema_version": SCHEMA_VERSION,
        "title": _text(library.get("title"), "library.title", 16384),
        "description": _text(library.get("description"), "library.description", 262144),
        "source": _metadata(library.get("source"), "library.source", _SOURCE_KEYS),
        "conversation_ids": [conversation["id"] for conversation in normalized],
    }
    if "code_checkpoint" in library:
        from .checkpoints import validate_checkpoint
        expected_library["code_checkpoint"] = validate_checkpoint(library["code_checkpoint"])
    if library != expected_library:
        raise PackageError("Library fields or conversation order are inconsistent")
    compatibility = _load_json(entries["compatibility.json"], "compatibility.json")
    if (compatibility != _compatibility(normalized, library["source"])
            or type(compatibility.get("native_restore_verified")) is not bool
            or type(compatibility.get("model_continuation_verified")) is not bool):
        raise PackageError("Compatibility report disagrees with normalized contents")
    return {"manifest": manifest, "library": library, "conversations": normalized, "compatibility": compatibility}


_REDACTIONS = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z]+ )?PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    ("credential", re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"), "[REDACTED_TOKEN]"),
    ("bearer token", re.compile(r"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/-]{8,}=*"), "Bearer [REDACTED_TOKEN]"),
    ("assigned credential", re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)\b[\"']?[ \t]*[:=][ \t]*[\"']?[^\s\"',;]{6,}[\"']?"), "[REDACTED_CREDENTIAL]"),
    ("email address", re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+\b"), "[REDACTED_EMAIL]"),
    ("home path", re.compile(r"(?<![A-Za-z0-9])(?:/(?:home|Users)/[^/\s\"'<>]+|/root(?=/|\b)|[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'<>]+)", re.IGNORECASE), "[REDACTED_HOME]"),
)


def redact_conversations(conversations) -> tuple[list, list]:
    """Return a deep copy plus readable findings, without retaining matched secrets.

    Common credentials, emails, and home directory prefixes are replaced in
    string values. This is not a complete secret detector or a publishing gate.
    """
    if not isinstance(conversations, list):
        raise PackageError("conversations must be a list")
    findings = []

    def visit(value, path, depth=0):
        if depth > 32:
            raise PackageError("Conversation metadata is too deeply nested")
        if isinstance(value, str):
            for label, pattern, replacement in _REDACTIONS:
                value, count = pattern.subn(replacement, value)
                if count:
                    findings.append(f"{path}: redacted {count} {label} match(es)")
            return value
        if isinstance(value, list):
            return [visit(item, f"{path}[{index}]", depth + 1) for index, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: visit(item, f"{path}.{key}", depth + 1) for key, item in value.items()}
        return copy.deepcopy(value)

    return visit(conversations, "conversations"), findings
