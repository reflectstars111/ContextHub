"""Deterministic, conservative three-way merges of validated contextpacks.

Stable conversation/message IDs identify entities, not their text. Objects
merge by field, lists remain atomic except entity order and append-only audit.
Concurrent appends use a stable topological order respecting both inputs;
different simultaneous moves or middle insertions require human resolution.
The caller verifies archive integrity with read_package before this module.
"""

from __future__ import annotations

from collections import Counter
import copy
import heapq
import json

from .package import (PackageError, SCHEMA_VERSION, _SOURCE_KEYS, _compatibility,
                      _metadata, _normalize_conversations, _text, build_package)


MAX_CONFLICTS = 100
MAX_CONFLICT_VALUE = 2000
_ABSENT = object()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bounded(value):
    if value is _ABSENT:
        return None, False
    if isinstance(value, str):
        if len(value) > MAX_CONFLICT_VALUE:
            return value[:MAX_CONFLICT_VALUE - 1] + "…", True
        return value, False
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    pieces, length = [], 0
    for piece in encoder.iterencode(value):
        if length + len(piece) > MAX_CONFLICT_VALUE:
            pieces.append(piece[:MAX_CONFLICT_VALUE - length])
            return "".join(pieces)[:MAX_CONFLICT_VALUE - 1] + "…", True
        pieces.append(piece)
        length += len(piece)
    return copy.deepcopy(value), False


class _Conflicts:
    def __init__(self):
        self.items = []
        self.count = 0
        self.truncated = False

    def add(self, path, kind, base, target, source):
        self.count += 1
        if len(self.items) >= MAX_CONFLICTS:
            self.truncated = True
            return
        item = {"path": path[:MAX_CONFLICT_VALUE], "kind": kind}
        for key, value in (("base", base), ("target", target), ("source", source)):
            item[key], truncated = _bounded(value)
            self.truncated |= truncated
        self.items.append(item)

    def result(self, package=None):
        return {"package": package if not self.count else None,
                "conflicts": self.items, "conflict_count": self.count,
                "truncated": self.truncated}


def _path(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _copy(value):
    return _ABSENT if value is _ABSENT else copy.deepcopy(value)


def _field(base, target, source, path, conflicts):
    if target == source:
        return _copy(target)
    if target == base:
        return _copy(source)
    if source == base:
        return _copy(target)
    if all(isinstance(value, dict) for value in (base, target, source)):
        merged = {}
        for key in sorted(base.keys() | target.keys() | source.keys()):
            value = _field(base.get(key, _ABSENT), target.get(key, _ABSENT),
                           source.get(key, _ABSENT), _path(path, key), conflicts)
            if value is not _ABSENT:
                merged[key] = value
        return merged
    conflicts.add(path, "field", base, target, source)
    return _copy(target)


def _audit(base, target, source, path, conflicts):
    """Old records cannot be rewritten, even by identical edits on both sides."""
    if target[:len(base)] != base or source[:len(base)] != base:
        conflicts.add(path, "audit_rewrite", base, target, source)
        return copy.deepcopy(base)
    # Identical records can describe different past edits. Preserve every
    # record within a branch; only overlap between branches is deduplicated.
    # This is a multiset union of the tails, keeping target order first.
    result = copy.deepcopy(target)
    target_counts = Counter(_canonical(entry) for entry in target[len(base):])
    source_counts = Counter()
    for entry in source[len(base):]:
        key = _canonical(entry)
        source_counts[key] += 1
        if source_counts[key] > target_counts[key]:
            result.append(copy.deepcopy(entry))
    return result


def _append_order(target, source):
    """O(n log n) stable topological sort, or None for opposing constraints."""
    edges = {identifier: set() for identifier in target + source}
    incoming = dict.fromkeys(edges, 0)
    for order in (target, source):
        for first, second in zip(order, order[1:]):
            if second not in edges[first]:
                edges[first].add(second)
                incoming[second] += 1
    ready = [identifier for identifier in edges if incoming[identifier] == 0]
    heapq.heapify(ready)
    result = []
    while ready:
        identifier = heapq.heappop(ready)
        result.append(identifier)
        for following in sorted(edges[identifier]):
            incoming[following] -= 1
            if incoming[following] == 0:
                heapq.heappush(ready, following)
    return result if len(result) == len(edges) else None


def _order(base, target, source, surviving, path, conflicts):
    base = [identifier for identifier in base if identifier in surviving]
    target = [identifier for identifier in target if identifier in surviving]
    source = [identifier for identifier in source if identifier in surviving]
    if target == source:
        return target
    if target == base:
        return source
    if source == base:
        return target
    if target[:len(base)] == base and source[:len(base)] == base:
        appended = _append_order(target[len(base):], source[len(base):])
        if appended is not None:
            return base + appended
    conflicts.add(path + "/order", "order", base, target, source)
    # Continue collecting field conflicts, but never publish this fallback.
    return sorted(surviving)


def _record(base, target, source, path, conflicts, kind):
    merged = {}
    for key in sorted(base.keys() | target.keys() | source.keys()):
        field_path = _path(path, key)
        if key == "messages" and kind == "conversation":
            merged[key] = _entities(base[key], target[key], source[key], field_path,
                                    conflicts, "message")
        elif ((key == "annotations" and kind == "conversation")
              or (key == "transformations" and kind == "message")):
            merged[key] = _audit(base.get(key, []), target.get(key, []), source.get(key, []),
                                 field_path, conflicts)
        else:
            value = _field(base.get(key, _ABSENT), target.get(key, _ABSENT),
                           source.get(key, _ABSENT), field_path, conflicts)
            if value is not _ABSENT:
                merged[key] = value
    return merged


def _entities(base, target, source, path, conflicts, kind):
    base_map, target_map, source_map = ({entity["id"]: entity for entity in entities}
                                      for entities in (base, target, source))
    merged = {}
    for identifier in sorted(base_map.keys() | target_map.keys() | source_map.keys()):
        original = base_map.get(identifier, _ABSENT)
        ours, theirs = target_map.get(identifier, _ABSENT), source_map.get(identifier, _ABSENT)
        entity_path = _path(path, identifier)
        if original is _ABSENT:
            if ours is not _ABSENT and theirs is not _ABSENT and ours != theirs:
                conflicts.add(entity_path, "add/add", original, ours, theirs)
            merged[identifier] = _copy(theirs if ours is _ABSENT else ours)
        elif ours is _ABSENT or theirs is _ABSENT:
            remaining = theirs if ours is _ABSENT else ours
            if remaining is not _ABSENT and remaining != original:
                conflicts.add(entity_path, "delete/edit", original, ours, theirs)
        else:
            merged[identifier] = _record(original, ours, theirs, entity_path, conflicts, kind)
    order = _order(base_map, target_map, source_map, merged, path, conflicts)
    return [merged[identifier] for identifier in order]


def _retain_deleted_audit(base, merged):
    """Keep old annotation multiplicity when its owning conversation is removed.

    A revision may have already moved the records. Counts across the snapshot
    prevent copying those a second time while preserving genuine duplicates.
    """
    if not merged:
        return
    surviving = {conversation["id"] for conversation in merged}
    expected = Counter(_canonical(entry) for conversation in base
                       for entry in conversation.get("annotations", []))
    actual = Counter(_canonical(entry) for conversation in merged
                     for entry in conversation.get("annotations", []))
    for conversation in base:
        if conversation["id"] in surviving:
            continue
        for entry in conversation.get("annotations", []):
            key = _canonical(entry)
            if actual[key] < expected[key]:
                merged[0].setdefault("annotations", []).append(copy.deepcopy(entry))
                actual[key] += 1


def _validate(package):
    """Check canonical payload structure; the caller verifies original ZIP hashes."""
    if not isinstance(package, dict) or set(package) != {"manifest", "library", "conversations", "compatibility"}:
        raise PackageError("Merge inputs must be validated read_package results")
    conversations = _normalize_conversations(package["conversations"])
    if conversations != package["conversations"]:
        raise PackageError("Merge conversation data must be canonical")
    library = package["library"]
    if not isinstance(library, dict):
        raise PackageError("Merge library must be an object")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "title": _text(library.get("title"), "library.title", 16384),
        "description": _text(library.get("description"), "library.description", 262144),
        "source": _metadata(library.get("source"), "library.source", _SOURCE_KEYS),
        "conversation_ids": [conversation["id"] for conversation in conversations],
    }
    if library != expected:
        raise PackageError("Merge library data or conversation order is not canonical")
    manifest = package["manifest"]
    if (not isinstance(manifest, dict) or manifest.get("format") != "contextpack"
            or manifest.get("schema_version") != SCHEMA_VERSION
            or type(manifest.get("conversation_count")) is not int
            or manifest["conversation_count"] != len(conversations)):
        raise PackageError("Merge manifest does not describe the canonical conversations")
    if package["compatibility"] != _compatibility(conversations, library["source"]):
        raise PackageError("Merge compatibility report is not canonical")


def merge_packages(base, target, source):
    """Return a valid immutable package or bounded conflicts with an exact count.

    Library title/description accept contributions; the target's library source
    identity stays fixed. No input is mutated. No revision builder is invoked:
    independently contributed source/provenance and audit are retained as data.
    Any conflict, including a merged package exceeding format limits, prevents
    producing a package. Display truncation never alters the conflict count.
    """
    conflicts = _Conflicts()
    for label, value in (("base", base), ("target", target), ("source", source)):
        try:
            _validate(value)
        except (PackageError, TypeError, KeyError, RecursionError) as exc:
            conflicts.add("/" + label, "validation", None, str(exc), None)
    if conflicts.count:
        return conflicts.result()
    library = {"source": copy.deepcopy(target["library"]["source"])}
    for key in ("title", "description"):
        library[key] = _field(base["library"][key], target["library"][key], source["library"][key],
                              "/library/" + key, conflicts)
    conversations = _entities(base["conversations"], target["conversations"], source["conversations"],
                              "/conversations", conflicts, "conversation")
    if conflicts.count:
        return conflicts.result()
    _retain_deleted_audit(base["conversations"], conversations)
    try:
        package = build_package(conversations, **library)
    except PackageError as exc:
        conflicts.add("/package", "validation", None, str(exc), None)
        return conflicts.result()
    return conflicts.result(package)
