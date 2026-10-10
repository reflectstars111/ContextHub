"""Read-only native capture using real export and package code over RPC fixtures."""
import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from contexthub.codex import CodexError
from contexthub.package import read_package


def history_turn(identifier, text, status="completed"):
    return {"id": identifier, "status": status, "itemsView": "full", "items": [
        {"id": "u", "type": "userMessage", "content": [{"type": "text", "text": text}]},
        {"id": "a", "type": "agentMessage", "text": "Saved reply", "phase": "final_answer"},
        {"id": "tool", "type": "commandExecution", "command": "private tool state"},
    ]}


class ProtocolClient:
    """The only substituted boundary is the external Codex app-server."""

    def __init__(self, threads, listing=None, failures=()):
        self.threads = threads
        self.listing = listing or []
        self.failures = failures
        self.calls = []
        self.entered = 0
        self.closed = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *args):
        self.closed += 1

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "thread/list":
            if not params["useStateDbOnly"]:
                raise AssertionError("Capture must not repair or scan the session store")
            return {"data": copy.deepcopy(self.listing), "nextCursor": None}
        if method not in {"thread/read", "thread/turns/list"}:
            raise AssertionError("Unexpected mutation or model call: " + method)
        identifier = params["threadId"]
        if identifier in self.failures:
            raise CodexError("Source thread is unavailable")
        thread = self.threads[identifier]
        if method == "thread/read":
            return {"thread": {"id": identifier, "name": thread.get("title", "Development"),
                               "cwd": thread.get("cwd", "/example"), "cliVersion": "0.153.4",
                               "historyMode": "paginated"}}
        if params["sortDirection"] != "asc" or params["itemsView"] != "full":
            raise AssertionError("Capture must request full chronological history")
        return {"data": copy.deepcopy(thread["turns"]), "nextCursor": None}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        try:
            self.capture = importlib.import_module("contexthub.capture")
        except ModuleNotFoundError:
            self.fail("The direct Codex capture adapter is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {}, clear=True).start()

    def collect(self, client, **kwargs):
        with mock.patch.object(self.capture, "CodexClient", return_value=client):
            return self.capture.collect_codex(self.workspace, **kwargs)

    def client(self, turns=None, **kwargs):
        return ProtocolClient({"native-thread": {"turns": turns if turns is not None else [
            history_turn("old", "Useful history")]}}, **kwargs)

    def test_explicit_selection_overrides_current_environment_and_normalizes_identity(self):
        os.environ["CODEX_THREAD_ID"] = "another-thread"
        client = self.client()
        result = self.collect(client, thread_id="native-thread")
        package = read_package(result["blob"])
        self.assertEqual(package["conversations"][0]["messages"][0]["content"], "Useful history")
        self.assertEqual(result["sources"], [{"provider": "codex", "thread_id": "native-thread",
                                              "conversation_id": package["conversations"][0]["id"]}])
        self.assertTrue(result["sources"][0]["conversation_id"].startswith("sha256:"))
        self.assertNotIn("native-thread", json.dumps(package))
        self.assertFalse(result["captures"][0]["current_turn_excluded"])
        self.assertIsNone(result["captures"][0]["before_turn"])
        self.assertEqual(result["captures"][0]["message_count"], 2)
        self.assertEqual(client.entered, 1)
        self.assertEqual(client.closed, 1)
        self.assertFalse(any(method == "thread/list" for method, _ in client.calls))

    def test_current_environment_excludes_last_turn_even_if_reported_interrupted(self):
        os.environ["CODEX_THREAD_ID"] = "native-thread"
        client = self.client([history_turn("old", "Useful history"),
                              history_turn("active", "ctx add must not capture itself", "interrupted")])
        result = self.collect(client)
        package = read_package(result["blob"])
        self.assertEqual(len(package["conversations"][0]["messages"]), 2)
        self.assertNotIn("ctx add must not capture itself", json.dumps(package))
        self.assertTrue(result["captures"][0]["current_turn_excluded"])
        self.assertEqual(result["captures"][0]["before_turn"], "active")
        self.assertTrue(any("当前" in w for w in result["captures"][0]["warnings"]))

    def test_explicit_current_thread_also_excludes_its_running_turn(self):
        os.environ["CODEX_THREAD_ID"] = "native-thread"
        result = self.collect(self.client([history_turn("old", "old"), history_turn("active", "active")]),
                              thread_id="native-thread")
        self.assertEqual(result["captures"][0]["message_count"], 2)
        self.assertEqual(result["captures"][0]["before_turn"], "active")

    def test_current_thread_without_prior_visible_history_fails_instead_of_empty_capture(self):
        os.environ["CODEX_THREAD_ID"] = "native-thread"
        for turns in ([], [history_turn("active", "first turn", "interrupted")]):
            with self.subTest(turns=turns), self.assertRaisesRegex(CodexError, "历史|No visible"):
                self.collect(self.client(turns))

    def test_explicit_before_turn_respects_requested_older_boundary(self):
        os.environ["CODEX_THREAD_ID"] = "native-thread"
        result = self.collect(self.client([history_turn("old", "old"), history_turn("cut", "cut"),
                                          history_turn("active", "active")]), before_turn="cut")
        self.assertEqual(result["captures"][0]["message_count"], 2)
        self.assertEqual(result["captures"][0]["before_turn"], "cut")

    def test_cwd_discovery_uses_one_exact_normalized_match(self):
        client = self.client(listing=[{"id": "unrelated", "cwd": str(self.workspace / "child")},
                                    {"id": "native-thread", "cwd": str(self.workspace) + "/child/.."},
                                    {"id": "missing-cwd"}])
        result = self.collect(client)
        self.assertEqual(result["sources"][0]["thread_id"], "native-thread")

    def test_cwd_discovery_does_not_guess_newest_or_scan_parent_and_child_projects(self):
        for listing in ([], [{"id": "parent", "cwd": str(self.workspace.parent)},
                              {"id": "child", "cwd": str(self.workspace / "child")}],
                        [{"id": "one", "cwd": str(self.workspace)},
                         {"id": "two", "cwd": str(self.workspace)}]):
            with self.subTest(listing=listing), self.assertRaisesRegex(CodexError, "ctx codex list.*--thread"):
                self.collect(self.client(listing=listing))

    def test_interrupted_and_failed_history_are_retained_until_current_inprogress(self):
        result = self.collect(self.client([history_turn("interrupted", "Partial", "interrupted"),
                                          history_turn("failed", "Failed", "failed"),
                                          history_turn("completed", "Complete"),
                                          history_turn("active", "Active", "inProgress")]),
                              thread_id="native-thread")
        self.assertEqual(result["captures"][0]["message_count"], 6)
        warnings = " ".join(result["captures"][0]["warnings"])
        self.assertIn("partial", warnings)
        self.assertIn("commandExecution", warnings)
        package_text = json.dumps(read_package(result["blob"]))
        self.assertNotIn("private tool state", package_text)
        self.assertNotIn('"content": "Active"', package_text)

    def test_redacts_before_packaging_without_leaking_matches_into_capture_report(self):
        secret = "sk-abcdefghijklmnopqrstuvwx"
        result = self.collect(self.client([history_turn("old", "Token " + secret + " and alice@example.com")]),
                              thread_id="native-thread")
        package = read_package(result["blob"])
        self.assertNotIn(secret, json.dumps(package))
        self.assertNotIn(secret, json.dumps(result["captures"]))
        self.assertIn("[REDACTED_TOKEN]", package["conversations"][0]["messages"][0]["content"])
        self.assertTrue(result["captures"][0]["redactions"])
        self.assertTrue(any("redact" in warning.lower() for warning in result["captures"][0]["warnings"]))

    def test_repeated_unchanged_history_produces_identical_package_bytes(self):
        first = self.collect(self.client(), thread_id="native-thread")
        second = self.collect(self.client(), thread_id="native-thread")
        self.assertEqual(first["blob"], second["blob"])

    def test_bound_sources_refresh_only_bound_threads_using_one_client(self):
        original = self.collect(self.client(), thread_id="native-thread")
        os.environ["CODEX_THREAD_ID"] = "unrelated-current"
        client = self.client([history_turn("old", "Old"), history_turn("new", "New")])
        result = self.collect(client, sources=original["sources"])
        self.assertEqual(result["sources"], original["sources"])
        self.assertEqual(result["captures"][0]["message_count"], 4)
        self.assertFalse(any(method == "thread/list" for method, _ in client.calls))
        self.assertEqual(client.entered, 1)

    def test_current_bound_source_recalculates_cutoff_on_each_capture(self):
        os.environ["CODEX_THREAD_ID"] = "native-thread"
        first = self.collect(self.client([history_turn("old", "Old"), history_turn("then", "Then")]))
        second = self.collect(self.client([history_turn("old", "Old"), history_turn("then", "Then"),
                                           history_turn("now", "Now", "interrupted")]), sources=first["sources"])
        self.assertEqual(second["captures"][0]["message_count"], 4)
        self.assertEqual(second["captures"][0]["before_turn"], "now")
        self.assertNotIn("before_turn", second["sources"][0])

    def test_source_identity_mismatch_fails_instead_of_rebinding_different_conversation(self):
        with self.assertRaisesRegex(CodexError, "identity|标识|绑定"):
            self.collect(self.client(), sources=[{"provider": "codex", "thread_id": "native-thread",
                                                   "conversation_id": "sha256:" + "0" * 64}])

    def test_protocol_cannot_mislabel_another_threads_history_as_selected_thread(self):
        for current_thread, mismatch_read in (("", 1), ("native-thread", 1), ("native-thread", 2)):
            with self.subTest(current=current_thread, mismatch_read=mismatch_read):
                os.environ["CODEX_THREAD_ID"] = current_thread
                client = self.client([history_turn("old", "Old"), history_turn("active", "Active")])
                original_request = client.request
                reads = 0

                def request(method, params):
                    nonlocal reads
                    result = original_request(method, params)
                    if method == "thread/read":
                        reads += 1
                        if reads == mismatch_read:
                            result["thread"]["id"] = "wrong-thread"
                    return result

                client.request = request
                with self.assertRaisesRegex(CodexError, "ID|标识|不同|mismatch"):
                    self.collect(client, thread_id="native-thread")
                self.assertEqual(list(self.workspace.iterdir()), [])

    def test_multiple_bound_sources_keep_all_conversations(self):
        one = self.collect(self.client(), thread_id="native-thread")["sources"]
        two_client = ProtocolClient({"other-thread": {"turns": [history_turn("other", "Other")]}})
        two = self.collect(two_client, thread_id="other-thread")["sources"]
        combined = self.client()
        combined.threads["other-thread"] = {"turns": [history_turn("other", "Other")]}
        result = self.collect(combined, sources=one + two)
        self.assertEqual([item["thread_id"] for item in result["sources"]], ["native-thread", "other-thread"])
        self.assertEqual(len(read_package(result["blob"])["conversations"]), 2)
        self.assertEqual(combined.entered, 1)

    def test_later_source_failure_returns_no_partial_package_or_workspace_files(self):
        original = self.collect(self.client(), thread_id="native-thread")["sources"]
        client = self.client(failures={"missing"})
        with self.assertRaisesRegex(CodexError, "unavailable"):
            self.collect(client, sources=original + [{"provider": "codex", "thread_id": "missing",
                                                      "conversation_id": "sha256:" + "1" * 64}])
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(client.closed, 1)

    def test_invalid_capture_arguments_fail_before_reading_native_store(self):
        cases = [dict(sources=[]), dict(sources="thread"), dict(thread_id=""), dict(before_turn=""),
                 dict(sources=[{"provider": "claude", "thread_id": "t", "conversation_id": "c"}]),
                 dict(sources=[{"provider": "codex", "thread_id": "t"}]),
                 dict(sources=[{"provider": "codex", "thread_id": "t", "conversation_id": "c"}], thread_id="t"),
                 dict(sources=[{"provider": "codex", "thread_id": "t", "conversation_id": "c"},
                               {"provider": "codex", "thread_id": "t", "conversation_id": "c"}]),
                 dict(sources=[{"provider": "codex", "thread_id": "a", "conversation_id": "c"},
                               {"provider": "codex", "thread_id": "b", "conversation_id": "d"}], before_turn="cut")]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                client = self.client()
                with self.assertRaises(ValueError):
                    self.collect(client, **kwargs)
                self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
