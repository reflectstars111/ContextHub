"""Three-way collaboration preserves identity, independent edits, and audit."""

import copy
import importlib
import json
import unittest

from contexthub.package import build_package, read_package


def conversation(identifier="c1", ids=("m1", "m2", "m3")):
    return {"id": identifier, "title": "中文代码", "source": {"platform": "codex"},
            "messages": [{"id": mid, "role": "user", "content": f"print('{mid} 你好 🌏')",
                          "provenance": {"item_id": "original-" + mid},
                          "transformations": ["text_extracted"]} for mid in ids]}


def package(conversations=None, **library):
    return read_package(build_package(
        [conversation()] if conversations is None else conversations,
        library.get("title", "Library"), library.get("description", "Original"),
        source=library.get("source", {"method": "codex-export"})))


def modified(base, modify, **library):
    conversations = copy.deepcopy(base["conversations"])
    modify(conversations)
    return package(conversations, **{**base["library"], **library})


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.merge = importlib.import_module("contexthub.merge").merge_packages

    def clean(self, base, target, source):
        result = self.merge(base, target, source)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["conflict_count"], 0)
        self.assertFalse(result["truncated"])
        return read_package(result["package"])

    def test_independent_text_metadata_and_library_edits_preserve_sources(self):
        base = package()
        target = modified(base, lambda cs: cs[0]["messages"][0].update(content="目标更改"),
                          description="Target description", source={"method": "target-identity"})
        source = modified(base, lambda cs: cs[0]["messages"][1].update(content="```py\nprint('来源 🌏')\n```"),
                          title="Source title", source={"method": "foreign-identity"})
        before = copy.deepcopy((base, target, source))
        merged = self.clean(base, target, source)
        self.assertEqual(merged["library"]["title"], "Source title")
        self.assertEqual(merged["library"]["description"], "Target description")
        self.assertEqual(merged["library"]["source"], target["library"]["source"])
        self.assertEqual(merged["conversations"][0]["messages"][0]["content"], "目标更改")
        self.assertEqual(merged["conversations"][0]["messages"][1], source["conversations"][0]["messages"][1])
        self.assertEqual((base, target, source), before)
        self.assertEqual(self.merge(base, target, source), self.merge(base, target, source))

    def test_same_message_different_fields_and_nested_metadata_merge(self):
        base = package()
        target = modified(base, lambda cs: cs[0]["messages"][0].update(content="new text"))
        source = modified(base, lambda cs: cs[0]["messages"][0]["provenance"].update(platform="imported"))
        result = self.clean(base, target, source)["conversations"][0]["messages"][0]
        self.assertEqual(result["content"], "new text")
        self.assertEqual(result["provenance"], {"item_id": "original-m1", "platform": "imported"})

    def test_identical_edits_are_accepted_but_same_text_different_metadata_conflicts(self):
        base = package()
        target = modified(base, lambda cs: cs[0]["messages"][0].update(content="shared"))
        self.clean(base, target, target)
        target = modified(target, lambda cs: cs[0]["messages"][0]["provenance"].update(item_id="target"))
        source = modified(target, lambda cs: cs[0]["messages"][0]["provenance"].update(item_id="source"))
        result = self.merge(base, target, source)
        self.assertIsNone(result["package"])
        self.assertEqual(result["conflict_count"], 1)
        self.assertTrue(result["conflicts"][0]["path"].endswith("/provenance/item_id"))

    def test_deletion_of_unchanged_messages_and_conversations(self):
        base = package([conversation(), conversation("removed")])
        target = modified(base, lambda cs: cs.pop())
        source = modified(base, lambda cs: cs[0]["messages"].pop(0))
        result = self.clean(base, target, source)
        self.assertEqual([c["id"] for c in result["conversations"]], ["c1"])
        self.assertEqual([m["id"] for m in result["conversations"][0]["messages"]], ["m2", "m3"])

    def test_delete_versus_edit_conflicts_at_message_and_conversation(self):
        base = package([conversation(), conversation("c2")])
        target = modified(base, lambda cs: (cs[0]["messages"].pop(0), cs.pop()))
        source = modified(base, lambda cs: (cs[0]["messages"][0].update(role="assistant"), cs[1].update(title="edit")))
        result = self.merge(base, target, source)
        self.assertIsNone(result["package"])
        self.assertEqual(result["conflict_count"], 2)
        self.assertEqual({c["kind"] for c in result["conflicts"]}, {"delete/edit"})

    def test_two_additions_with_same_id_are_compared_as_whole_entities(self):
        base = package()
        target = modified(base, lambda cs: cs[0]["messages"].append({"id": "new", "role": "user", "content": "same"}))
        source = modified(target, lambda cs: cs[0]["messages"][-1].update(provenance="human_authored"))
        result = self.merge(base, target, source)
        self.assertEqual(result["conflicts"][0]["kind"], "add/add")
        self.assertIsNone(result["package"])
        self.clean(base, target, target)

    def test_single_reorder_and_identical_reorders(self):
        base = package()
        target = modified(base, lambda cs: cs[0]["messages"].reverse())
        source = modified(base, lambda cs: cs[0]["messages"][0].update(content="independent"))
        merged = self.clean(base, target, source)
        self.assertEqual([m["id"] for m in merged["conversations"][0]["messages"]], ["m3", "m2", "m1"])
        self.assertEqual(merged["conversations"][0]["messages"][-1]["content"], "independent")
        self.clean(base, target, target)

    def test_conflicting_reorders_and_insertions_do_not_guess(self):
        base = package()
        target = modified(base, lambda cs: cs[0]["messages"].reverse())
        source = modified(base, lambda cs: cs[0].update(messages=cs[0]["messages"][1:] + cs[0]["messages"][:1]))
        result = self.merge(base, target, source)
        self.assertIsNone(result["package"])
        self.assertEqual(result["conflicts"][0]["kind"], "order")
        target = modified(base, lambda cs: cs[0]["messages"].insert(1, {"id": "x", "role": "user", "content": "x"}))
        source = modified(base, lambda cs: cs[0]["messages"].insert(1, {"id": "y", "role": "user", "content": "y"}))
        self.assertEqual(self.merge(base, target, source)["conflicts"][0]["kind"], "order")

    def test_independent_appends_respect_both_order_constraints(self):
        base = package()
        def append(ids):
            return lambda cs: cs[0]["messages"].extend({"id": mid, "role": "user", "content": mid} for mid in ids)
        target, source = modified(base, append(["x", "y"])), modified(base, append(["z", "x"]))
        result = self.clean(base, target, source)
        order = [m["id"] for m in result["conversations"][0]["messages"]]
        self.assertEqual(order, ["m1", "m2", "m3", "z", "x", "y"])
        cycle = modified(base, append(["y", "x"]))
        self.assertEqual(self.merge(base, target, cycle)["conflicts"][0]["kind"], "order")

    def test_conversation_order_merges_additions_and_detects_reorder_conflicts(self):
        base = package([conversation("a"), conversation("b"), conversation("c")])
        target = modified(base, lambda cs: cs.append(conversation("x")))
        source = modified(base, lambda cs: cs.append(conversation("y")))
        result = self.clean(base, target, source)
        self.assertEqual([c["id"] for c in result["conversations"]], ["a", "b", "c", "x", "y"])
        target = modified(base, lambda cs: cs.reverse())
        source = modified(base, lambda cs: cs.insert(0, cs.pop(1)))
        self.assertEqual(self.merge(base, target, source)["conflicts"][0]["path"], "/conversations/order")

    def test_audit_append_only_union_preserves_base_multiplicity(self):
        base = package()
        base = modified(base, lambda cs: (cs[0].update(annotations=["older", "older"]),
                                         cs[0]["messages"][0].update(transformations=["old", "old"])))
        target = modified(base, lambda cs: (cs[0]["annotations"].extend(["target", "shared"]),
                                           cs[0]["messages"][0]["transformations"].extend(["target", "shared"])))
        source = modified(base, lambda cs: (cs[0]["annotations"].extend(["source", "shared"]),
                                           cs[0]["messages"][0]["transformations"].extend(["source", "shared"])))
        merged = self.clean(base, target, source)["conversations"][0]
        self.assertEqual(merged["annotations"], ["older", "older", "target", "shared", "source"])
        self.assertEqual(merged["messages"][0]["transformations"], ["old", "old", "target", "shared", "source"])

    def test_audit_rewrite_is_conflict_even_when_both_sides_clear_it(self):
        base = modified(package(), lambda cs: cs[0].update(annotations=["prior"]))
        target = modified(base, lambda cs: (cs[0].update(annotations=[]), cs[0]["messages"][0].update(transformations=[])))
        for source in [base, target]:
            result = self.merge(base, target, source)
            self.assertIsNone(result["package"])
            self.assertEqual(result["conflict_count"], 2)
            self.assertEqual({c["kind"] for c in result["conflicts"]}, {"audit_rewrite"})

    def test_deleted_conversation_does_not_discard_prior_annotations(self):
        base = package([conversation(), {**conversation("removed"), "annotations": ["a", "a", "b"]}])
        target = modified(base, lambda cs: cs.pop())
        result = self.clean(base, target, base)
        self.assertEqual(result["conversations"][0]["annotations"], ["a", "a", "b"])

    def test_moved_audit_is_not_duplicated_during_deletion(self):
        base = package([{**conversation(), "annotations": ["shared"]},
                        {**conversation("removed"), "annotations": ["shared", "shared"]}])
        target = modified(base, lambda cs: (cs.pop(), cs[0]["annotations"].extend(["shared", "shared"])))
        result = self.clean(base, target, base)
        self.assertEqual(result["conversations"][0]["annotations"], ["shared", "shared", "shared"])

    def test_deleted_message_does_not_discard_unrelated_source_audit(self):
        base = package()
        target = modified(base, lambda cs: cs[0]["messages"].pop(0))
        source = modified(base, lambda cs: (cs[0].update(annotations=[{"kind": "note", "text": "import source"}]),
                                           cs[0]["messages"][1]["transformations"].append("human_edited")))
        merged = self.clean(base, target, source)["conversations"][0]
        self.assertEqual(merged["annotations"], source["conversations"][0]["annotations"])
        self.assertEqual(merged["messages"][0]["transformations"], ["text_extracted", "human_edited"])

    def test_contributed_conversation_source_and_message_provenance_survive(self):
        base = package()
        target = modified(base, lambda cs: cs[0].update(context="target context"))
        source = modified(base, lambda cs: (cs[0]["source"].update(version="source-version"),
                                           cs.append({**conversation("source-only"),
                                                      "source": {"platform": "other-app", "method": "imported"}})))
        merged = self.clean(base, target, source)
        self.assertEqual(merged["conversations"][0]["source"], source["conversations"][0]["source"])
        self.assertEqual(merged["conversations"][1], source["conversations"][1])

    def test_ids_with_slashes_use_unambiguous_json_pointer_paths(self):
        base = package([conversation("c/1", ids=("m~1",))])
        target = modified(base, lambda cs: cs[0]["messages"][0].update(content="target"))
        source = modified(base, lambda cs: cs[0]["messages"][0].update(content="source"))
        self.assertEqual(self.merge(base, target, source)["conflicts"][0]["path"],
                         "/conversations/c~11/messages/m~01/content")

    def test_revision_audit_from_independent_message_edits_is_preserved(self):
        from contexthub.revisions import build_revision
        base = package()
        target_conv, source_conv = copy.deepcopy(base["conversations"]), copy.deepcopy(base["conversations"])
        target_conv[0]["messages"][0]["content"] = "target"
        source_conv[0]["messages"][1]["content"] = "source"
        target = read_package(build_revision(base, {"conversations": target_conv}))
        source = read_package(build_revision(base, {"conversations": source_conv}))
        merged = self.clean(base, target, source)["conversations"][0]
        self.assertEqual(len(merged["annotations"]), 2)
        for annotation in target["conversations"][0]["annotations"] + source["conversations"][0]["annotations"]:
            self.assertIn(annotation, merged["annotations"])

    def test_repeated_identical_revision_audit_retains_each_branch_multiplicity(self):
        from contexthub.revisions import build_revision
        def rename(base, title):
            return read_package(build_revision(base, {"conversations": base["conversations"], "title": title}))
        base = rename(package(), "first")
        target = rename(base, "target")
        source = rename(rename(base, "source intermediate"), "target")
        self.assertEqual(base["conversations"][0]["annotations"] * 2,
                         target["conversations"][0]["annotations"])
        result = self.clean(base, target, base)
        self.assertEqual(result["conversations"][0]["annotations"], target["conversations"][0]["annotations"])
        result = self.clean(base, target, source)
        self.assertEqual(result["conversations"][0]["annotations"], source["conversations"][0]["annotations"])

    def test_audit_addition_duplicates_are_deduplicated_only_across_branches(self):
        base = modified(package(), lambda cs: cs[0].update(annotations=["old", "old"]))
        target = modified(base, lambda cs: cs[0]["annotations"].extend(["old", "old", "target"]))
        source = modified(base, lambda cs: cs[0]["annotations"].extend(["old", "source", "source"]))
        result = self.clean(base, target, source)
        self.assertEqual(result["conversations"][0]["annotations"],
                         ["old", "old", "old", "old", "target", "source", "source"])

    def test_detail_truncation_keeps_exact_count_and_bounded_values(self):
        base = package([conversation(ids=tuple(f"m{i}" for i in range(151)))])
        target = modified(base, lambda cs: [m.update(content="T" * 3000) for m in cs[0]["messages"]])
        source = modified(base, lambda cs: [m.update(content="S" * 3000) for m in cs[0]["messages"]])
        result = self.merge(base, target, source)
        self.assertIsNone(result["package"])
        self.assertEqual(result["conflict_count"], 151)
        self.assertEqual(len(result["conflicts"]), 100)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["conflicts"][0]["target"]), 2000)
        self.assertLess(len(json.dumps(result)), 500_000)

    def test_invalid_structure_and_combined_format_limits_are_validation_conflicts(self):
        base = package()
        malformed = copy.deepcopy(base)
        malformed["conversations"][0]["messages"].append(copy.deepcopy(malformed["conversations"][0]["messages"][0]))
        for value in [None, {}, malformed]:
            result = self.merge(base, value, base)
            self.assertIsNone(result["package"])
            self.assertEqual(result["conflicts"][0]["kind"], "validation")
        base = package([conversation(ids=tuple(f"m{i}" for i in range(1999)))])
        target = modified(base, lambda cs: cs[0]["messages"].append({"id": "x", "role": "user", "content": "x"}))
        source = modified(base, lambda cs: cs[0]["messages"].append({"id": "y", "role": "user", "content": "y"}))
        result = self.merge(base, target, source)
        self.assertIsNone(result["package"])
        self.assertEqual(result["conflicts"][0]["kind"], "validation")

    def test_deleting_all_conversations_is_explicit_validation_conflict(self):
        base = package([conversation("a"), conversation("b")])
        target = modified(base, lambda cs: cs.pop(0))
        source = modified(base, lambda cs: cs.pop())
        result = self.merge(base, target, source)
        self.assertIsNone(result["package"])
        self.assertEqual(result["conflicts"][0]["kind"], "validation")

    def test_combined_conversation_and_audit_limits_are_validation_conflicts(self):
        base = package([conversation(f"c{i}", ids=()) for i in range(199)])
        target = modified(base, lambda cs: cs.append(conversation("x", ids=())))
        source = modified(base, lambda cs: cs.append(conversation("y", ids=())))
        result = self.merge(base, target, source)
        self.assertIsNone(result["package"])
        self.assertEqual(result["conflicts"][0]["kind"], "validation")
        base = package()
        target = modified(base, lambda cs: cs[0]["messages"][0]["transformations"].extend(f"target-{i}" for i in range(60)))
        source = modified(base, lambda cs: cs[0]["messages"][0]["transformations"].extend(f"source-{i}" for i in range(60)))
        result = self.merge(base, target, source)
        self.assertIsNone(result["package"])
        self.assertEqual(result["conflicts"][0]["kind"], "validation")

    def test_noop_merge_preserves_canonical_package_exactly(self):
        base = modified(package(), lambda cs: cs[0].update(annotations=["a", "a"]))
        merged = self.clean(base, base, base)
        self.assertEqual(merged, base)


if __name__ == "__main__":
    unittest.main()
