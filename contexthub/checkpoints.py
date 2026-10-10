"""Portable links between conversation snapshots and immutable Git objects.

A matched digest binds the recorded claim to these exact conversation contents.
It does not authenticate the Git repository or prove that imported code is safe.
Only the local code connector verifies the objects before restoring them.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import re
from urllib.parse import urlsplit

from .package import PackageError, _json_bytes, _text, build_package, read_package


_FIELDS = frozenset({"schema_version", "repository_url", "commit", "tree", "branch",
                     "conversation_digest", "captured_at", "verification"})
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def validate_checkpoint(value):
    """Return a bounded copy of checkpoint metadata, rejecting unknown fields."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise PackageError("code_checkpoint must contain exactly the supported fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PackageError("Unsupported code_checkpoint schema_version")
    for key in ("commit", "tree"):
        if not isinstance(value[key], str) or not _OID.fullmatch(value[key]):
            raise PackageError(f"code_checkpoint.{key} must be a full lowercase Git object ID")
    if len(value["commit"]) != len(value["tree"]):
        raise PackageError("Checkpoint commit and tree must use the same Git object format")
    if not isinstance(value["conversation_digest"], str) or not _DIGEST.fullmatch(value["conversation_digest"]):
        raise PackageError("code_checkpoint.conversation_digest must be a SHA-256 digest")
    url = _text(value["repository_url"], "code_checkpoint.repository_url", 4096)
    if url:
        try:
            parsed = urlsplit(url)
            # Parsing port also rejects malformed and out-of-range values.
            parsed.port
        except ValueError as exc:
            raise PackageError("Invalid checkpoint repository URL") from exc
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(character.isspace() or ord(character) < 32 for character in url)
                or "\\" in url or "%" in parsed.netloc):
            raise PackageError("Checkpoint repository URL must be credential-free HTTPS without query or fragment")
    branch = _text(value["branch"], "code_checkpoint.branch", 1024)
    if any(ord(character) < 32 or ord(character) == 127 for character in branch):
        raise PackageError("Checkpoint branch cannot contain control characters")
    captured = _text(value["captured_at"], "code_checkpoint.captured_at", 64)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", captured):
            raise ValueError("not UTC ISO")
        datetime.datetime.fromisoformat(captured.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PackageError("code_checkpoint.captured_at must be an ISO UTC timestamp ending in Z") from exc
    if value["verification"] != "local_clean":
        raise PackageError("Unsupported checkpoint verification claim")
    return copy.deepcopy(value)


def conversation_digest(package):
    """Hash canonical library metadata and conversations, excluding the link."""
    library = {key: value for key, value in package["library"].items() if key != "code_checkpoint"}
    return hashlib.sha256(_json_bytes({"library": library, "conversations": package["conversations"]})).hexdigest()


def checkpoint_status(package):
    """Return none, matched, or stale; matched is not independent verification."""
    checkpoint = package["library"].get("code_checkpoint")
    if checkpoint is None:
        return "none"
    checkpoint = validate_checkpoint(checkpoint)
    return "matched" if checkpoint["conversation_digest"] == conversation_digest(package) else "stale"


def attach_checkpoint(blob, git_info):
    """Bind supplied Git metadata to an already validated conversation snapshot."""
    package = read_package(blob)
    if not isinstance(git_info, dict):
        raise PackageError("Checkpoint Git metadata must be an object")
    checkpoint = dict(git_info, schema_version=1, conversation_digest=conversation_digest(package))
    checkpoint.setdefault("verification", "local_clean")
    checkpoint = validate_checkpoint(checkpoint)
    library = package["library"]
    return build_package(package["conversations"], library["title"], library["description"],
                         source=library["source"], code_checkpoint=checkpoint, code=package.get('code'))
