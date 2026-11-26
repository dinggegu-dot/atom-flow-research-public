import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from afr_agent import questions, reconcile
from afr_agent.library import RequestError
from afr_agent.tests import test_questions


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_questions.QuestionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.vault = self.fixture.vault
        self.policy = self.fixture.policy

    def sources(self):
        first, _ = self.fixture.article("甲", "资料甲描述经销渠道。")
        second, _ = self.fixture.article("乙", "资料乙描述最终成交。")
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [first, second]}))
        plan = {"clusters": [{"question_id": "", "new_question": "渠道如何影响成交？", "links": [
            {"sha256": sha, "topic_id": 1, "relation": "BACKGROUND", "reason": "同一问题的不同环节"}
            for sha in (first, second)
        ]}]}
        return [first, second], plan

    def test_reconciliation_persists_and_reuses_independent_review(self):
        sources, plan = self.sources()
        roles = []

        def runner(role, payload, schema):
            roles.append(role)
            self.assertEqual({item["sha256"] for item in payload["articles"]}, set(sources))
            self.assertTrue(all(article["topics"][0]["original_excerpt"] for article in payload["articles"]))
            return plan

        result = reconcile.run(self.vault, sources, self.policy, runner)
        self.assertEqual(roles, [reconcile.PROPOSE, reconcile.REVIEW])
        self.assertEqual(result["added_links"], 0)
        self.assertEqual(result["candidates"], 1)
        path = self.vault / result["candidate_path"]
        body = path.read_text()
        self.assertIn("渠道如何影响成交？", body)
        self.assertEqual(body.count("#^topic-1|"), 2)
        self.assertEqual(questions.catalog(self.vault), [])
        path.write_text(body + "我的笔记\n")
        replay = reconcile.run(self.vault, sources, self.policy, runner)
        self.assertEqual(roles, [reconcile.PROPOSE, reconcile.REVIEW])
        self.assertEqual(replay["candidate_path"], result["candidate_path"])
        self.assertEqual(replay["added_links"], 0)
        self.assertEqual(path.read_text(), body + "我的笔记\n")

    def test_resumes_partial_apply_with_same_id_and_original_relation(self):
        sources, plan = self.sources()
        one, _ = sources
        assignment = dict(topic_id=1, relation="SUPPORTS", reason="保留先前证据关系",
                          new_question=plan["clusters"][0]["new_question"])
        saved, = questions.apply(self.vault, one, [assignment], confirm_new=True)["questions"]
        identifier = saved["id"]
        apply = questions.apply

        def interrupt(vault, sha, *args, **kwargs):
            if sha != one:
                raise OSError("synthetic interruption")
            return apply(vault, sha, *args, **kwargs)

        with patch.object(questions, "apply", side_effect=interrupt):
            with self.assertRaises(OSError):
                reconcile.run(self.vault, sources, self.policy, lambda *_: plan)
        replay = reconcile.run(self.vault, sources, self.policy, lambda *_: self.fail("checkpoint required"))
        self.assertEqual(replay["question_ids"], [identifier])
        latest = questions.read(self.vault, identifier)
        self.assertEqual([link["relation"] for link in latest["links"]], ["SUPPORTS", "BACKGROUND"])

    def test_adopt_candidate_cli_publishes_atomic_history_and_replays(self):
        sources, plan = self.sources()
        result = reconcile.run(self.vault, sources, self.policy, lambda *_: plan)
        page = self.vault / result["candidate_path"]
        body = page.read_bytes()
        batch = page.stem.removeprefix("归并-")
        command = [sys.executable, "-B", "-m", "afr_agent", "adopt-candidate", "--vault", str(self.vault),
                   "--batch", batch, "--position", "1"]
        self.policy.unlink()
        first = json.loads(subprocess.check_output(command))
        self.assertTrue(first["adopted"])
        record = first["question"]
        self.assertEqual(record["revision"], 1)
        self.assertEqual({link["sha256"] for link in record["links"]}, set(sources))
        index = self.vault / questions.INDEX
        index.write_text(index.read_text() + "\n手写索引备注\n")
        replay = json.loads(subprocess.check_output(command))
        self.assertFalse(replay["adopted"])
        self.assertEqual(replay["question"], record)
        history = questions.read(self.vault, record["id"], 1)
        self.assertEqual({key: history[key] for key in record}, record)
        self.assertEqual(history["candidate"], {"batch": batch, "position": 1})
        self.assertIn("手写索引备注", index.read_text())
        self.assertIn(record["path"], index.read_text())
        self.assertEqual(page.read_bytes(), body)

    def test_adopt_rejects_stale_topic_and_conflicting_existing_title(self):
        sources, plan = self.sources()
        result = reconcile.run(self.vault, sources, self.policy, lambda *_: plan)
        batch = (self.vault / result["candidate_path"]).stem.removeprefix("归并-")
        with patch.object(questions, "read_topic", side_effect=RequestError("article_publication_changed")):
            with self.assertRaisesRegex(RequestError, "^article_publication_changed$"):
                reconcile.adopt_candidate(self.vault, batch, 1)
        self.assertEqual(questions.catalog(self.vault), [])
        title = plan["clusters"][0]["new_question"]
        existing = questions.apply(self.vault, sources[0], [self.fixture.proposed(title)], confirm_new=True)
        with self.assertRaisesRegex(RequestError, "^existing_question_required$"):
            reconcile.adopt_candidate(self.vault, batch, 1)
        self.assertEqual(questions.catalog(self.vault)[0]["revision"], existing["questions"][0]["revision"])

    def test_adopt_uses_fixed_candidate_order_even_if_page_is_reordered(self):
        sources, plan = self.sources()
        first, = plan["clusters"]
        second = {**first, "new_question": "渠道如何改变信息传递？"}
        plan["clusters"].append(second)
        result = reconcile.run(self.vault, sources, self.policy, lambda *_: plan)
        page = self.vault / result["candidate_path"]
        batch = page.stem.removeprefix("归并-")
        prefix, first_section, second_section = page.read_text().split("\n## ")
        order = self.vault / ".afr" / "reconciliation" / f"{batch}-candidate-order.json"
        original = page.read_bytes()
        page.write_text("\n## ".join([prefix, second_section, first_section]))
        with self.assertRaisesRegex(RequestError, "^candidate_order_changed$"):
            reconcile.adopt_candidate(self.vault, batch, 1)
        order.unlink()
        with self.assertRaisesRegex(RequestError, "^candidate_order_changed$"):
            reconcile.adopt_candidate(self.vault, batch, 1)
        self.assertEqual(questions.catalog(self.vault), [])
        page.write_bytes(original)
        self.assertEqual(reconcile.adopt_candidate(self.vault, batch, 2)["question"]["title"],
                         second["new_question"])

    def test_malformed_approved_receipt_is_reported_without_a_new_model_call(self):
        sources, plan = self.sources()
        reconcile.run(self.vault, sources, self.policy, lambda *_: plan)
        receipt, = (self.vault / ".afr/reconciliation").glob("*-approved.json")
        for contents in ('{"result": {"clusters": []}}', '{incomplete json'):
            with self.subTest(contents=contents):
                receipt.write_text(contents)
                with self.assertRaisesRegex(RequestError, "^invalid_review_receipt$"):
                    reconcile.run(self.vault, sources, self.policy, lambda *_: self.fail("unexpected model call"))
                self.assertEqual(receipt.read_text(), contents)

    def test_new_review_uses_free_sequence_number_after_damaged_cache_with_gap(self):
        sources, plan = self.sources()
        reconcile.run(self.vault, sources, self.policy, lambda *_: plan)
        directory = self.vault / ".afr/reconciliation"
        approved, = directory.glob("*-approved.json")
        first, = directory.glob("*-review-1.json")
        damaged = first.with_name(first.name.replace("-review-1.json", "-review-2.json"))
        first.rename(damaged)
        damaged.write_text("{incomplete json")
        approved.unlink()
        roles = []

        def runner(role, *_):
            roles.append(role)
            return plan

        self.assertEqual(reconcile.run(self.vault, sources, self.policy, runner)["candidates"], 1)
        self.assertEqual(roles, [reconcile.REVIEW])
        self.assertEqual(damaged.read_text(), "{incomplete json")
        self.assertTrue(first.with_name(first.name.replace("-review-1.json", "-review-3.json")).is_file())

    def test_invalid_reviewed_bundle_is_withheld_and_other_bundle_can_publish(self):
        sources, proposal = self.sources()
        good, = proposal["clusters"]
        invalid = {**good, "new_question": "只有一份材料能确认吗？", "links": good["links"][:1]}
        checked = {"clusters": [invalid, good]}
        with patch.object(reconcile, "_apply", wraps=reconcile._apply) as apply:
            result = reconcile.run(self.vault, sources, self.policy,
                                   lambda role, *_: checked if role == reconcile.REVIEW else proposal)
        self.assertEqual(apply.call_args.args[1], [good])
        self.assertEqual(result["deferred_clusters"], 1)
        self.assertEqual(result["added_links"], 0)
        self.assertEqual(result["candidates"], 1)

    def test_rejects_scope_and_invalid_links_before_writing(self):
        sources, plan = self.sources()
        with self.assertRaisesRegex(RequestError, "local_only"):
            reconcile.run(self.vault, sources, None, lambda *_: self.fail("unauthorized runner"))
        cluster, = plan["clusters"]
        cluster["links"][1]["topic_id"] = 200
        with self.assertRaisesRegex(RequestError, "invalid_reconciliation"):
            reconcile.run(self.vault, sources, self.policy, lambda *_: plan)
        self.assertEqual(questions.catalog(self.vault), [])
