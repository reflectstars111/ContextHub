"""Portable packages are data, never instructions or an extraction target."""

import copy
import hashlib
import importlib
import io
import json
import stat
import unittest
import warnings
import zipfile


def sample():
    return [{
        "id": "thread-你好",
        "title": "中文与代码",
        "context": "A portable example.",
        "tags": ["python", "中文"],
        "messages": [
            {"id": "m1", "role": "user", "content": "解释这段代码。"},
            {"id": "m2", "role": "assistant",
             "content": "```python\nprint('你好 🌏')\n```\n\nDone.",
             "provenance": {"item_id": "native-item-2"},
             "transformations": ["text_extracted"]},
        ],
    }]


def zip_entries(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {entry.filename: archive.read(entry) for entry in archive.infolist()}


def make_zip(entries, extra=None):
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
            if extra:
                archive.writestr(*extra)
    return output.getvalue()


def rehash(entries):
    """Build hostile but self-consistent packages without the production writer."""
    manifest = json.loads(entries["manifest.json"])
    manifest["files"] = [
        {"path": path, "size": len(value),
         "sha256": hashlib.sha256(value).hexdigest()}
        for path, value in sorted(entries.items()) if path != "manifest.json"
    ]
    entries["manifest.json"] = json.dumps(manifest).encode()
    return make_zip(entries)


class PackageTests(unittest.TestCase):
    def setUp(self):
        try:
            self.package = importlib.import_module("contexthub.package")
        except ModuleNotFoundError:
            self.fail("The portable package implementation is missing")

    def built(self):
        return self.package.build_package(sample(), "迁移库", "Example library")

    def test_unicode_code_round_trip_and_readable_projection(self):
        data = self.built()
        result = self.package.read_package(data)
        self.assertEqual(set(result), {"manifest", "library", "conversations", "compatibility"})
        self.assertEqual(result["library"]["title"], "迁移库")
        self.assertEqual(result["conversations"][0]["messages"][1]["content"],
                         "```python\nprint('你好 🌏')\n```\n\nDone.")
        self.assertEqual(result["conversations"][0]["messages"][1]["provenance"],
                         {"item_id": "native-item-2"})
        entries = zip_entries(data)
        markdown = next(value.decode() for path, value in entries.items()
                        if path.startswith("readable/"))
        self.assertIn("# 中文与代码", markdown)
        self.assertIn("print('你好 🌏')", markdown)
        self.assertIn("解释这段代码。", markdown)
        self.assertFalse(result["compatibility"]["native_restore_verified"])
        self.assertFalse(result["compatibility"]["model_continuation_verified"])

    def test_build_normalizes_defaults_without_mutation_or_unknown_fields(self):
        conversations = [{"title": "Minimal", "auth": "never-export-this", "messages": [
            {"role": "user", "content": "hello", "system_prompt": "hidden"}
        ]}]
        before = copy.deepcopy(conversations)
        data = self.package.build_package(conversations, "Library")
        value = self.package.read_package(data)["conversations"][0]
        self.assertEqual(conversations, before)
        self.assertEqual(value["schema_version"], "0.1")
        self.assertEqual(value["context"], "")
        self.assertEqual(value["tags"], [])
        self.assertTrue(value["id"])
        self.assertTrue(value["messages"][0]["id"])
        self.assertEqual(value["messages"][0]["provenance"], "imported")
        self.assertEqual(value["messages"][0]["transformations"], [])
        self.assertNotIn("auth", value)
        self.assertNotIn("system_prompt", value["messages"][0])
        self.assertEqual(data, self.package.build_package(conversations, "Library"))

    def test_rejects_invalid_roles_ids_and_non_text_content(self):
        cases = []
        for role in ("system", "developer", "invalid", 123):
            value = sample()
            value[0]["messages"][0]["role"] = role
            cases.append(value)
        value = sample()
        value[0]["messages"][1]["id"] = "m1"
        cases.append(value)
        cases.append(sample() * 2)
        value = sample()
        value[0]["messages"][0]["content"] = {"type": "image"}
        cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.package.build_package(value, "Library")

    def test_rejects_traversal_absolute_backslash_and_unlisted_files(self):
        for path in ("../escape", "/tmp/escape", "conversations/../escape.json",
                     "conversations\\escape.json", "auth.json", "readable/hello.html"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.package.read_package(make_zip(zip_entries(self.built()), (path, b"oops")))

    def test_rejects_duplicate_archive_names(self):
        entries = zip_entries(self.built())
        with self.assertRaises(ValueError):
            self.package.read_package(make_zip(entries, ("library.json", entries["library.json"])))

    def test_rejects_symlink(self):
        entries = zip_entries(self.built())
        key = next(name for name in entries if name.startswith("readable/"))
        contents = entries.pop(key)
        link = zipfile.ZipInfo(key)
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            self.package.read_package(make_zip(entries, (link, contents)))

    def test_rejects_corrupt_hash_missing_file_and_unhashed_file(self):
        for mutation in ("hash", "missing", "unhashed"):
            entries = zip_entries(self.built())
            path = next(name for name in entries if name.startswith("conversations/"))
            if mutation == "hash":
                entries[path] += b" "
            elif mutation == "missing":
                del entries[path]
            else:
                entries["conversations/extra.json"] = b"{}"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.package.read_package(make_zip(entries))

    def test_rejects_divergent_readable_copy_even_with_updated_hash(self):
        entries = zip_entries(self.built())
        path = next(name for name in entries if name.startswith("readable/"))
        entries[path] = b"Original secret accidentally remained in Markdown."
        with self.assertRaises(ValueError):
            self.package.read_package(rehash(entries))

    def test_rejects_unknown_main_fields_even_with_updated_hash(self):
        entries = zip_entries(self.built())
        path = next(name for name in entries if name.startswith("conversations/"))
        value = json.loads(entries[path])
        value["authentication"] = {"token": "private"}
        entries[path] = json.dumps(value).encode()
        with self.assertRaises(ValueError):
            self.package.read_package(rehash(entries))

    def test_rejects_conversation_count_and_conversation_size_limits(self):
        with self.assertRaises(ValueError):
            self.package.build_package([{"id": str(n), "messages": []} for n in range(201)], "Big")
        value = sample()
        value[0]["messages"][0]["content"] = "x" * (5 * 1024 * 1024)
        with self.assertRaises(ValueError):
            self.package.build_package(value, "Big")

    def test_rejects_decompression_file_and_total_bounds(self):
        # Large repetitive inputs compress to KiB; reject declared sizes before reading.
        entries = zip_entries(self.built())
        key = next(name for name in entries if name.startswith("conversations/"))
        entries[key] = b"x" * (5 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            self.package.read_package(make_zip(entries))
        entries = {f"conversations/{n:04d}.json": b"x" * (5 * 1024 * 1024)
                   for n in range(21)}
        with self.assertRaises(ValueError):
            self.package.read_package(make_zip(entries))

    def test_redaction_is_recursive_nonmutating_and_applies_to_both_representations(self):
        value = sample()
        value[0]["source"] = {"cwd": "/home/alice/private/project"}
        value[0]["title"] = "Contact alice@example.com"
        value[0]["messages"][0]["content"] = (
            "token sk-proj-abcdefghijklmnopqrstuv and ghp_abcdefghijklmnopqrstuvwxyz1234567890 "
            "and Bearer eyJhbGciOiJub25lIn0.abc.signature at /Users/Alice/project "
            "or C:\\Users\\Bob\\project"
        )
        before = copy.deepcopy(value)
        redacted, findings = self.package.redact_conversations(value)
        self.assertEqual(value, before)
        self.assertTrue(findings)
        self.assertTrue(all(isinstance(item, str) for item in findings))
        exported = b"\n".join(zip_entries(self.package.build_package(redacted, "Safe")).values()).decode()
        for secret in ("alice@example.com", "sk-proj-abcdefghijklmnopqrstuv", "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
                       "eyJhbGciOiJub25lIn0.abc.signature", "/home/alice", "/Users/Alice", "C:\\Users\\Bob"):
            self.assertNotIn(secret, exported)
        self.assertIn("REDACTED", exported)

    def test_invalid_zip_and_invalid_json_use_value_errors(self):
        for value in (b"not a zip", b"PK", b"", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.package.read_package(value)
        entries = zip_entries(self.built())
        entries["library.json"] = b'{"title":"x","title":"ambiguous"}'
        with self.assertRaises(ValueError):
            self.package.read_package(rehash(entries))

    def test_redacts_quoted_json_credentials_and_empty_home_paths(self):
        value = [{"messages": [{"role": "user", "content":
            '{"api_key": "custom-private-key-value", "access_token": "secret-value-123"} '
            '/home/alice and C:\\Users\\Bob'
        }]}]
        result, findings = self.package.redact_conversations(value)
        text = result[0]["messages"][0]["content"]
        self.assertNotIn("custom-private-key-value", text)
        self.assertNotIn("secret-value-123", text)
        self.assertNotIn("/home/alice", text)
        self.assertNotIn("C:\\Users\\Bob", text)
        self.assertGreaterEqual(len(findings), 2)

    def test_rejects_mistyped_compatibility_flags_even_if_equivalent_in_python(self):
        for key, value in (("schema_version", True), ("native_restore_verified", 0)):
            entries = zip_entries(self.built())
            compatibility = json.loads(entries["compatibility.json"])
            compatibility[key] = value
            entries["compatibility.json"] = json.dumps(compatibility).encode()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.package.read_package(rehash(entries))

    def test_version_point_one_accepts_imported_and_human_authored_provenance(self):
        conversations = sample()
        conversations[0]["schema_version"] = "0.1"
        conversations[0]["messages"][0]["provenance"] = "imported"
        conversations[0]["messages"][1]["provenance"] = "human_authored"
        result = self.package.read_package(self.package.build_package(conversations, "Public"))
        self.assertEqual(result["library"]["schema_version"], "0.1")
        self.assertEqual(result["manifest"]["schema_version"], "0.1")
        self.assertEqual([m["provenance"] for m in result["conversations"][0]["messages"]],
                         ["imported", "human_authored"])
        for invalid in ("unknown", "verified", {"unrecognized": "secret"}):
            conversations[0]["messages"][0]["provenance"] = invalid
            with self.subTest(provenance=invalid), self.assertRaises(ValueError):
                self.package.build_package(conversations, "Public")

    def test_public_metadata_keeps_source_claims_and_maps_private_thread_ids(self):
        conversations = sample()
        conversations[0]["id"] = "private-native-thread"
        conversations[0]["source"] = {
            "thread_id": "private-native-thread", "source_thread_id": "original-private-thread",
            "method": "connector", "platform_claimed": "Codex", "verification": "unverified",
            "completeness": "selected_excerpt", "public_url": "https://example.com/public",
            "adapter": "codex", "version": "0.153.4", "warnings": ["Selected completed turns only"],
            "cwd": "/home/private/work", "project": "C:\\Users\\Private\\work",
        }
        conversations[0]["messages"][0]["provenance"] = {
            "provider": "codex", "thread_id": "private-native-thread", "item_id": "item-1",
        }
        data = self.package.build_package(conversations, "Public", source={"method": "synthetic"})
        result = self.package.read_package(data)
        conversation = result["conversations"][0]
        self.assertEqual(result["library"]["source"], {"method": "synthetic"})
        for field in ("method", "platform_claimed", "verification", "completeness", "public_url",
                      "adapter", "version", "warnings"):
            self.assertEqual(conversation["source"][field], conversations[0]["source"][field])
        mapped = "sha256:" + hashlib.sha256(b"private-native-thread").hexdigest()
        self.assertEqual(conversation["id"], mapped)
        self.assertEqual(conversation["source"]["thread_id"], mapped)
        self.assertEqual(conversation["messages"][0]["provenance"]["thread_id"], mapped)
        exported = b"\n".join(zip_entries(data).values()).decode()
        for private in ("private-native-thread", "original-private-thread", "/home/private", "C:\\Users\\Private"):
            self.assertNotIn(private, exported)
        self.assertIn("Selected completed turns only", result["compatibility"]["warnings"])

    def test_message_count_accepts_2000_but_rejects_2001(self):
        conversation = {"messages": [{"role": "user", "content": "message"} for _ in range(2000)]}
        result = self.package.read_package(self.package.build_package([conversation], "At limit"))
        self.assertEqual(len(result["conversations"][0]["messages"]), 2000)
        conversation["messages"].append({"role": "assistant", "content": "one too many"})
        with self.assertRaises(ValueError):
            self.package.build_package([conversation], "Over limit")


if __name__ == "__main__":
    unittest.main()
