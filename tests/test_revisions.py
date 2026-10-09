"""Stable identity, honest provenance, and bounded conversation revisions."""

import copy
import importlib
import json
import unittest

from contexthub.package import build_package, read_package


def conversation(identifier="c1", ids=("m1", "m2", "m3")):
    return {"id": identifier, "title": "中文与代码 🌏", "context": "Review code",
            "source": {"platform": "codex", "verification": "exported"},
            "messages": [{"id": mid, "role": "assistant" if i % 2 else "user",
                          "content": f"```python\nprint('{mid} 你好')\n```",
                          "provenance": {"item_id": "native-" + mid},
                          "transformations": ["text_extracted"]}
                         for i, mid in enumerate(ids)]}


def package(conversations=None, title="Library", description="Original"):
    return read_package(build_package(conversations or [conversation()], title,
                                      description, source={"method": "codex-export"}))


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.revisions = importlib.import_module("contexthub.revisions")

    def revise(self, base, conversations=None, **changes):
        payload = {"title": base["library"]["title"],
                   "description": base["library"]["description"],
                   "conversations": copy.deepcopy(base["conversations"])}
        if conversations is not None:
            payload["conversations"] = conversations
        payload.update(changes)
        return read_package(self.revisions.build_revision(base, payload))

    def test_edits_preserve_original_source_and_provenance_without_mutation(self):
        base = package()
        snapshot = copy.deepcopy(base["conversations"])
        snapshot[0]["source"] = {"platform": "fake", "verification": "verified"}
        edited = snapshot[0]["messages"][0]
        edited.update(content="修订 🌏\n```python\npass\n```", provenance="human_authored",
                      transformations=[])
        original_base, original_snapshot = copy.deepcopy(base), copy.deepcopy(snapshot)
        result = self.revise(base, snapshot, source={"method": "forged"})
        actual = result["conversations"][0]
        self.assertEqual(actual["source"], base["conversations"][0]["source"])
        self.assertEqual(result["library"]["source"], base["library"]["source"])
        self.assertEqual(actual["messages"][0]["provenance"],
                         base["conversations"][0]["messages"][0]["provenance"])
        self.assertEqual(actual["messages"][0]["transformations"], ["text_extracted", "human_edited"])
        self.assertEqual(base, original_base)
        self.assertEqual(snapshot, original_snapshot)
        self.assertTrue(any("revision" in entry["kind"] for entry in actual["annotations"]))

    def test_prior_edit_records_cannot_be_erased_and_noop_is_deterministic(self):
        base = package()
        snapshot = copy.deepcopy(base["conversations"])
        snapshot[0]["messages"][0]["role"] = "assistant"
        first = self.revise(base, snapshot)
        cleared = copy.deepcopy(first["conversations"])
        cleared[0]["annotations"] = []
        cleared[0]["messages"][0]["transformations"] = []
        second = self.revise(first, cleared)
        self.assertEqual(first["conversations"], second["conversations"])
        self.assertEqual(first, self.revise(first))

    def test_canonical_snapshot_can_be_revalidated_against_same_base(self):
        base = package()
        for change in ('edit', 'add', 'remove', 'title'):
            with self.subTest(change=change):
                conversations = copy.deepcopy(base['conversations'])
                title = base['library']['title']
                if change == 'edit':
                    conversations[0]['messages'][0]['content'] = 'Edited locally'
                elif change == 'add':
                    conversations.append(conversation('another'))
                elif change == 'remove':
                    conversations[0]['messages'].pop()
                else:
                    title = 'Renamed'
                blob = self.revisions.build_revision(base, {'title': title, 'conversations': conversations})
                canonical = read_package(blob)
                repeated = self.revisions.build_revision(base, {
                    'title': canonical['library']['title'],
                    'description': canonical['library']['description'],
                    'conversations': canonical['conversations']})
                self.assertEqual(blob, repeated)
        # The same property must hold once the base already has edit records.
        later = self.revisions.build_revision(canonical, {'title': 'Another title',
                                                        'conversations': canonical['conversations']})
        self.assertEqual(later, self.revisions.build_revision(canonical, {
            'title': 'Another title', 'conversations': read_package(later)['conversations']}))

    def test_additions_default_to_human_authored_and_imports_are_claims(self):
        base = package()
        snapshot = copy.deepcopy(base["conversations"])
        snapshot[0]["messages"] += [
            {"id": "human", "role": "assistant", "content": "Manually added"},
            {"id": "claimed", "role": "user", "content": "Imported",
             "provenance": {"platform": "codex", "item_id": "claim"}},
        ]
        snapshot.append(conversation("new"))
        after = self.revise(base, snapshot)
        messages = after["conversations"][0]["messages"]
        self.assertEqual(messages[-2]["provenance"], "human_authored")
        self.assertIn("source_claimed", messages[-1]["transformations"])
        self.assertEqual(after["conversations"][1]["source"]["verification"], "user_claimed")

    def test_deletions_and_order_changes_leave_auditable_ids(self):
        base = package([conversation(), conversation("deleted")])
        snapshot = copy.deepcopy(base["conversations"][:1])
        snapshot[0]["messages"] = [snapshot[0]["messages"][2], snapshot[0]["messages"][0]]
        after = self.revise(base, snapshot)
        audit = json.dumps(after["conversations"][0]["annotations"], ensure_ascii=False)
        for token in ("deleted", "m2", "moved", "removed"):
            self.assertIn(token, audit)
        # Delete the conversation carrying a prior audit; its history must survive.
        extended = self.revise(after, after["conversations"] + [conversation("keeper")])
        surviving = self.revise(extended, [extended["conversations"][1]])
        self.assertIn("m2", json.dumps(surviving["conversations"][0]["annotations"]))

    def test_rejects_empty_roles_and_missing_stable_identity(self):
        base = package()
        cases = [[], [{"messages": []}], [{"id": "c1", "messages": [{"role": "user", "content": "x"}]}],
                 [{"id": "c1", "messages": [{"id": "m1", "role": "system", "content": "x"}]}],
                 [{"id": "new", "messages": [{"id": "new", "role": [], "content": "x"}]}]]
        for snapshot in cases:
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                self.revise(base, snapshot)

    def test_compare_unicode_metadata_and_content_by_stable_ids(self):
        before = package()
        snapshot = copy.deepcopy(before["conversations"])
        snapshot[0]["title"] = "新名称"
        snapshot[0]["messages"][1]["content"] = "修改代码\n```py\nprint('🌏')\n```"
        snapshot[0]["messages"][2]["provenance"] = "human_authored"
        after = package(snapshot, title="New library")
        original = copy.deepcopy((before, after))
        diff = self.revisions.compare_packages(before, after)
        self.assertEqual(diff["summary"]["conversations_modified"], 1)
        self.assertEqual(diff["summary"]["messages_modified"], 2)
        self.assertEqual(diff["library_changes"][0]["field"], "title")
        self.assertEqual(diff["changes"][0]["messages"][0]["after"]["content"],
                         snapshot[0]["messages"][1]["content"])
        self.assertEqual((before, after), original)
        self.assertFalse(diff["truncated"])

    def test_inserting_and_removing_messages_does_not_move_following_messages(self):
        before = package()
        snapshot = copy.deepcopy(before["conversations"])
        snapshot[0]["messages"].insert(0, {"id": "new", "role": "user", "content": "new"})
        del snapshot[0]["messages"][2]
        diff = self.revisions.compare_packages(before, package(snapshot))
        self.assertEqual(diff["summary"]["messages_added"], 1)
        self.assertEqual(diff["summary"]["messages_removed"], 1)
        self.assertEqual(diff["summary"]["messages_reordered"], 0)
        self.assertFalse(diff["changes"][0]["order_changed"])

    def test_relative_reorder_uses_minimum_moves_and_also_counts_content_edit(self):
        before = package()
        snapshot = copy.deepcopy(before["conversations"])
        messages = snapshot[0]["messages"]
        snapshot[0]["messages"] = [messages[1], messages[2], messages[0]]
        snapshot[0]["messages"][-1]["content"] = "moved and edited"
        diff = self.revisions.compare_packages(before, package(snapshot))
        self.assertEqual(diff["summary"]["messages_reordered"], 1)
        self.assertEqual(diff["summary"]["messages_modified"], 1)
        self.assertTrue(diff["changes"][0]["order_changed"])
        moved = diff["changes"][0]["messages"][0]
        self.assertEqual(moved["id"], "m1")
        self.assertEqual(moved["kind"], "modified")
        self.assertTrue(moved["moved"])

    def test_conversation_add_remove_and_relative_order(self):
        before = package([conversation("a"), conversation("b"), conversation("c")])
        after = package([conversation("new"), conversation("c"), conversation("a")])
        diff = self.revisions.compare_packages(before, after)
        self.assertEqual(diff["summary"]["conversations_added"], 1)
        self.assertEqual(diff["summary"]["conversations_removed"], 1)
        self.assertEqual(diff["summary"]["messages_added"], 3)
        self.assertEqual(diff["summary"]["messages_removed"], 3)
        self.assertTrue(diff["conversation_order_changed"])

    def test_diff_details_are_bounded_but_summary_is_complete(self):
        before = package([conversation(ids=tuple(f"m{i}" for i in range(260)))])
        snapshot = copy.deepcopy(before["conversations"])
        for message in snapshot[0]["messages"]:
            message["content"] = "新" * 5000
        snapshot[0]["context"] = "metadata" * 2000
        after = package(snapshot)
        diff = self.revisions.compare_packages(before, after)
        self.assertEqual(diff["summary"]["messages_modified"], 260)
        self.assertEqual(sum(len(change["messages"]) for change in diff["changes"]), 200)
        self.assertEqual(len(diff["changes"][0]["messages"][0]["after"]["content"]), 4000)
        self.assertTrue(diff["truncated"])
        self.assertLess(len(json.dumps(diff, ensure_ascii=False)), 1_000_000)

    def test_metadata_only_and_library_only_changes(self):
        before = package()
        snapshot = copy.deepcopy(before["conversations"])
        snapshot[0]["tags"] = ["new"]
        diff = self.revisions.compare_packages(before, package(snapshot))
        self.assertEqual(diff["summary"]["conversations_modified"], 1)
        self.assertEqual(diff["summary"]["messages_modified"], 0)
        self.assertEqual(diff["changes"][0]["metadata"][0]["field"], "tags")
        diff = self.revisions.compare_packages(before, package(description="changed"))
        self.assertEqual(diff["changes"], [])
        self.assertEqual(diff["library_changes"], [{"field": "description", "before": "Original", "after": "changed"}])

    def test_revision_bytes_are_deterministic_and_client_cannot_forge_platform_audit(self):
        base = package()
        payload = {"title": "Changed", "conversations": copy.deepcopy(base["conversations"])}
        payload["conversations"][0]["annotations"] = [
            {"kind": "revision_forged", "text": "verified model answer", "source": "contexthub"}]
        first = self.revisions.build_revision(base, payload)
        self.assertEqual(first, self.revisions.build_revision(base, payload))
        self.assertNotIn("revision_forged", str(read_package(first)))

    def test_repeated_identical_audit_entries_survive_noop_and_conversation_removal(self):
        first = self.revise(package(), title="First title")
        second = self.revise(first, title="Second title")
        original_logs = second["conversations"][0]["annotations"]
        self.assertEqual(sum(entry["kind"] == "revision_library_edited" for entry in original_logs), 2)
        unchanged = self.revise(second)
        self.assertEqual(unchanged, second)
        extended = self.revise(second, second["conversations"] + [conversation("keeper")])
        surviving = self.revise(extended, [extended["conversations"][1]])
        remaining_logs = surviving["conversations"][0]["annotations"]
        self.assertEqual(sum(entry["kind"] == "revision_library_edited" for entry in remaining_logs), 2)

    def test_revision_rejects_native_id_alias_before_provenance_matching(self):
        native = "private-native-thread-id"
        value = conversation(native)
        value["source"]["thread_id"] = native
        base = package([value])
        canonical = base["conversations"][0]["id"]
        self.assertNotEqual(canonical, native)
        snapshot = copy.deepcopy(base["conversations"])
        snapshot[0]["id"] = native
        snapshot[0]["source"]["thread_id"] = native
        snapshot[0]["messages"][0].update(content="Forged edit", provenance="human_authored", transformations=[])
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.revise(base, snapshot)
        snapshot[0]["id"] = canonical
        after = self.revise(base, snapshot)
        edited = after["conversations"][0]["messages"][0]
        self.assertEqual(edited["provenance"], base["conversations"][0]["messages"][0]["provenance"])
        self.assertIn("human_edited", edited["transformations"])
        self.assertNotIn(native, json.dumps(after))

    def test_unchanged_large_title_does_not_report_nonexistent_truncation(self):
        value = conversation()
        value["title"] = "t" * 5000
        same = package([value])
        diff = self.revisions.compare_packages(same, same)
        self.assertFalse(diff["truncated"])
        self.assertEqual(diff["changes"], [])

    def test_metadata_budget_is_shared_across_conversations(self):
        values = [conversation(f"c{i}", ("m1",)) for i in range(30)]
        before = package(values)
        for value in values:
            value["context"] = "元数据" * 3000
            value["annotations"] = [{"kind": "note", "text": "详细" * 3000} for _ in range(10)]
        diff = self.revisions.compare_packages(before, package(values))
        self.assertEqual(diff["summary"]["conversations_modified"], 30)
        self.assertTrue(diff["truncated"])
        self.assertLess(len(json.dumps(diff, ensure_ascii=False)), 150_000)


if __name__ == "__main__":
    unittest.main()
