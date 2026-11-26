import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from afr_agent import article_argument_boards, intake, presentation, semantic
from afr_agent.library import RequestError


class SemanticReviewTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.vault = base / "vault"
        self.vault.mkdir()
        inbox = base / "inbox"
        inbox.mkdir()
        text = (
            "# 配电样本\n\n2025年9月，观察的50家机房中有10家使用新配电方案。"
            "5家校外机房未入样。同期新增35家机房。\n\n"
            "# 散热样本\n\n液冷开机时间下降，但两家老机房因改造暂停，所以结论只适用于新机房。"
        )
        original = inbox / "合成观点.md"
        original.write_text(text)
        self.sha = intake.digest(original.read_bytes())
        intake.scan(self.vault, inbox)
        self.folder, _ = presentation.evidence_for(self.vault, self.sha)
        self.policy = base / "approvals.json"
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [self.sha]}))
        self.policy.chmod(0o600)
        self.base = self.group(1, 2, "配电样本", "2025年9月，纳入样本的50家机房中有10家采用新配电方案，5家校外机房未入样。")
        self.other = self.group(3, 4, "散热样本", "液冷开机时间下降。两家老机房改造暂停，结论限于新机房。")
        self.passed = {"passed": True, "issues": []}

    @staticmethod
    def group(first, last, title, text):
        return {"start": first, "end": last, "title": title, "branch": title, "summary": text, "argument": text}

    @staticmethod
    def failed(start, source, kind):
        return {"passed": False, "issues": [{"start": start, "source": source, "kind": kind, "detail": "核对原文条件"}]}

    def approve_draft(self, draft_sha):
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [self.sha],
                                           "draft_sha256": [draft_sha]}))

    def assert_phase(self, instructions, expected):
        self.assertTrue(instructions.startswith(expected + "\n\n"))
        self.assertTrue(instructions.endswith("\n\n" + semantic.load_article_board_standard()))

    def assert_no_formal_output(self):
        for name in ("完整阅读.md", "文章白板.canvas"):
            self.assertFalse((self.folder / name).exists())

    def test_preserve_original_keeps_topic_titles_and_complete_source(self):
        model_groups = [self.base, self.other]
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": model_groups}, self.passed]) as harness:
            result = semantic.process(self.vault, self.sha, self.policy, preserve_original=True)
        self.assertEqual(result["review"], "passed")
        self.assert_phase(harness.call_args_list[0].args[0], semantic.SOURCE_TOPIC_INSTRUCTIONS)
        self.assert_phase(harness.call_args_list[1].args[0], semantic.REVIEW_INSTRUCTIONS)
        groups = semantic.reviewed_plan(self.vault, self.sha)["groups"]
        self.assertEqual([group["title"] for group in groups], ["配电样本", "散热样本"])
        self.assertEqual([group["summary"] for group in groups], [group["summary"] for group in model_groups])
        rendered = (self.folder / "完整阅读.md").read_text()
        self.assertIn("同期新增35家机房", rendered)
        self.assertIn("但两家老机房因改造暂停", rendered)

    def test_signed_viewpoints_drive_parallel_topic_cards_and_keep_specific_sources(self):
        power = {"judgment": "样本内有10家机房采用新配电", "speaker": "作者", "kind": "reported_fact",
                 "grounds": ["作者观察50家样本机房"], "conditions": ["排除5家校外机房"],
                 "evidence": [{"number": 2, "quote": "观察的50家机房中有10家使用新配电方案"},
                              {"number": 2, "quote": "5家校外机房未入样"}]}
        cooling = {"judgment": "液冷开机时间缩短", "speaker": "作者", "kind": "interpretation",
                   "grounds": ["液冷开机时间下降"], "conditions": ["结论只适用于新机房"],
                   "evidence": [{"number": 4, "quote": "液冷开机时间下降"}]}
        groups = [{**self.base, "branch": "机房样本", "viewpoints": [power]},
                  {**self.other, "branch": "机房样本", "viewpoints": [cooling]}]
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": groups}, self.passed]) as harness:
            semantic.process(self.vault, self.sha, self.policy, preserve_original=True)
        self.assertEqual(harness.call_count, 2)
        self.assertIn("viewpoints", harness.call_args_list[0].args[2]["properties"]["groups"]["items"]["required"])
        self.assertEqual(harness.call_args_list[1].args[1]["groups"][0]["viewpoints"], [power])
        plan = semantic.reviewed_plan(self.vault, self.sha)
        records = semantic.article_viewpoints(plan)
        self.assertEqual([(record["topic_id"], record["kind"]) for record in records],
                         [(1, "reported_fact"), (2, "interpretation")])
        reordered = {**plan, "groups": [{**groups[0], "viewpoints": [
            {**power, "evidence": list(reversed(power["evidence"]))}]}]}
        self.assertEqual(semantic.article_viewpoints(reordered)[0]["id"], records[0]["id"])
        self.assertEqual(semantic.article_viewpoints({"groups": [self.base]}), [])
        canvas = json.loads((self.folder / "文章白板.canvas").read_text())
        by_id = {node["id"]: node for node in canvas["nodes"]}
        self.assertEqual(by_id["t1"]["y"], by_id["t2"]["y"])
        self.assertNotEqual(by_id["t1"]["x"], by_id["t2"]["x"])
        self.assertIn("#^e2|查看原文", by_id["t1"]["text"])
        self.assertEqual({edge["toNode"] for edge in canvas["edges"] if edge["fromNode"] == "branch-0"},
                         {"t1", "t2"})
        reading = (self.folder / "完整阅读.md").read_text()
        self.assertIn("样本内有10家机房采用新配电", reading)
        self.assertIn("#^e2|原文第 2 段", reading)
        self.assertIn("适用条件", reading)
        self.assertIn("返回文章白板", reading)
        self.assertEqual(semantic.process(self.vault, self.sha)["state"], "already_published")

    def test_wrong_viewpoint_quote_requires_repair_even_if_model_reviewer_passes(self):
        record = {"judgment": "有10家使用新配电", "speaker": "作者", "kind": "reported_fact",
                  "grounds": [], "conditions": [],
                  "evidence": [{"number": 2, "quote": "有十家使用新配电方案"}]}
        bad = {**self.base, "viewpoints": [record]}
        fixed = {**self.base, "viewpoints": [{**record, "evidence": [
            {"number": 2, "quote": "有10家使用新配电方案"}]}]}
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [bad, self.other]}, self.passed,
                                       {"groups": [fixed]}, self.passed]) as harness:
            semantic.process(self.vault, self.sha, self.policy, preserve_original=True)
        issue, = harness.call_args_list[2].args[1]["issues"]
        self.assertEqual((issue["kind"], issue["source"]), ("unsupported", 2))
        plan = semantic.reviewed_plan(self.vault, self.sha)
        self.assertEqual(semantic.article_viewpoints(plan)[0]["evidence"], fixed["viewpoints"][0]["evidence"])

    def test_word_spacing_is_required_for_new_signatures(self):
        original = self.vault.parent / "inbox" / "全球研究.md"
        original.write_text("全球 HBM 市场报告显示出货增加。")
        sha = intake.digest(original.read_bytes())
        intake.scan(self.vault, original.parent)
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [sha]}))
        group = self.group(1, 1, "全球市场变化", "全球 HBM 市场报告显示出货增加。")
        record = {"judgment": "全球市场出货增加", "speaker": "作者", "kind": "reported_fact",
                  "grounds": [], "conditions": [], "evidence": [{"number": 1, "quote": "全球HBM市场"}]}
        bad = {**group, "viewpoints": [record]}
        fixed = {**group, "viewpoints": [{**record, "evidence": [{"number": 1, "quote": "全球 HBM 市场"}]}]}
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [bad]}, self.passed, {"groups": [fixed]}, self.passed]) as harness:
            self.assertEqual(semantic.process(self.vault, sha, self.policy, preserve_original=True)["state"], "published")
        self.assertEqual(harness.call_args_list[2].args[1]["issues"][0]["kind"], "unsupported")
        plan = semantic.reviewed_plan(self.vault, sha)
        self.assertEqual(plan["viewpoint_quote_mode"], semantic.VIEWPOINT_QUOTE_MODE)
        self.assertEqual(semantic.article_viewpoints(plan)[0]["evidence"], fixed["viewpoints"][0]["evidence"])

    def test_historical_quote_is_readable_and_recovers_only_unique_fixed_source(self):
        original = self.vault.parent / "inbox" / "旧全球研究.md"
        original.write_text("全球 HBM 市场报告显示出货增加。")
        sha = intake.digest(original.read_bytes())
        intake.scan(self.vault, original.parent)
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [sha]}))
        group = self.group(1, 1, "全球市场变化", "全球 HBM 市场报告显示出货增加。")
        citation = {"number": 1, "quote": "全球HBM市场"}
        record = {"judgment": "全球市场出货增加", "speaker": "作者", "kind": "reported_fact",
                  "grounds": [], "conditions": [], "evidence": [citation]}
        group["viewpoints"] = [record]
        quote_issues = semantic.viewpoint_quote_issues

        def old_receipt(path, value):
            if path.name == "review.json":
                plan = {key: data for key, data in value["plan"].items() if key != "viewpoint_quote_mode"}
                value = {**value, "plan": plan, "plan_sha256": semantic.fingerprint(plan)}
            intake.exclusive_json(path, value)

        with patch.object(semantic, "exclusive_json", side_effect=old_receipt), \
                patch.object(semantic, "viewpoint_quote_issues",
                             side_effect=lambda groups, items, **_: quote_issues(groups, items, historical=True)), \
                patch.object(semantic.codex, "run_structured", side_effect=[{"groups": [group]}, self.passed]):
            self.assertEqual(semantic.process(self.vault, sha, self.policy, preserve_original=True)["state"], "published")
        plan = semantic.reviewed_plan(self.vault, sha)
        self.assertNotIn("viewpoint_quote_mode", plan)
        self.assertEqual(semantic.article_viewpoints(plan)[0]["evidence"], [citation])
        _, evidence = presentation.evidence_for(self.vault, sha)
        self.assertEqual(semantic.viewpoint_source_quote(group, citation, evidence["parts"]), "全球 HBM 市场")
        self.assertEqual(semantic.viewpoint_source_quote(
            group, {"number": 1, "quote": "全球 HBM\n 市场"}, evidence["parts"]), "全球 HBM 市场")
        self.assertIsNone(semantic.viewpoint_source_quote(
            group, citation, [{"text": "全球 HBM 市场；全球HBM 市场"}]))
        self.assertIsNone(semantic.viewpoint_source_quote(
            group, {**citation, "number": 2}, [*evidence["parts"], {"text": "全球 HBM 市场"}]))

    def test_preserve_original_repairs_empty_cards_and_rechecks_them(self):
        empty = {**self.base, "summary": "打开本话题阅读对应的完整固定原文。"}
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [empty, self.other]}, self.passed, {"groups": [self.base]},
                                       self.passed]) as harness:
            semantic.process(self.vault, self.sha, self.policy, preserve_original=True)
        self.assert_phase(harness.call_args_list[2].args[0], semantic.REPAIR_INSTRUCTIONS)
        self.assertEqual(harness.call_args_list[2].args[1]["issues"][0]["kind"], "coverage")
        self.assert_phase(harness.call_args_list[3].args[0], semantic.REVIEW_INSTRUCTIONS)
        canvas = json.loads((self.folder / "文章白板.canvas").read_text())
        topic, = (node for node in canvas["nodes"] if node["id"] == "t1")
        self.assertIn(self.base["summary"], topic["text"])

    def test_placeholder_cannot_publish_if_repair_and_recovery_fail(self):
        empty = {**self.base, "summary": "查看原文。"}
        failed = self.failed(1, 2, "coverage")
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [empty, self.other]}, self.passed, {"groups": []},
                                       {"groups": [empty, self.other]}, failed,
                                       {"groups": [empty, self.other]}, failed, {"groups": []}]):
            with self.assertRaisesRegex(RequestError, "review_requires_human"):
                semantic.process(self.vault, self.sha, self.policy, preserve_original=True)
        self.assert_no_formal_output()

    def test_runtime_standard_is_required_for_new_article(self):
        with patch.object(semantic, "ARTICLE_BOARD_STANDARD", self.vault / "missing-standard.md"), \
                patch.object(semantic.codex, "run_structured", side_effect=AssertionError("unexpected egress")):
            with self.assertRaisesRegex(RequestError, "article_standard_unavailable"):
                semantic.process(self.vault, self.sha, self.policy)
        self.assert_no_formal_output()

    def test_original_boundary_repair_reuses_first_review_and_resigns(self):
        merged = self.group(1, 4, "机房样本", self.base["summary"] + self.other["summary"])
        verdict = {"passed": False, "issues": [*self.failed(1, 3, "boundary")["issues"],
                                               *self.failed(1, 2, "condition")["issues"]]}

        def revoke_after_review(instructions, *_):
            if instructions.startswith(semantic.SOURCE_TOPIC_INSTRUCTIONS + "\n\n"):
                return {"groups": [merged]}
            self.policy.unlink()
            return verdict

        with patch.object(semantic.codex, "run_structured", side_effect=revoke_after_review) as harness:
            with self.assertRaisesRegex(RequestError, "local_only"):
                semantic.process(self.vault, self.sha, self.policy, preserve_original=True)
        self.assertEqual(harness.call_count, 2)
        self.assert_no_formal_output()
        self.policy.touch(mode=0o600)
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [self.sha]}))
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [self.base, self.other]}, self.passed]) as harness:
            result = semantic.process(self.vault, self.sha, self.policy, preserve_original=True)
        self.assertEqual(harness.call_count, 2)
        instructions, payload, schema = harness.call_args_list[0].args
        self.assert_phase(instructions, semantic.SOURCE_RESEGMENT_INSTRUCTIONS)
        self.assertEqual(schema, semantic.SCHEMA)
        self.assertEqual([item["n"] for item in payload["evidence"]], [1, 2, 3, 4])
        self.assertEqual(payload["groups"][0]["title"], merged["title"])
        self.assertEqual(payload["issues"], verdict["issues"])
        self.assert_phase(harness.call_args_list[1].args[0], semantic.REVIEW_INSTRUCTIONS)
        self.assertEqual(result["state"], "published")
        signed = semantic.read_json(semantic.receipt(self.vault, self.sha, "review"))
        self.assertEqual(signed["reviews"][0]["review_role"], semantic.RESEGMENT_REVIEW_ROLE)
        self.assertEqual([g["title"] for g in semantic.reviewed_plan(self.vault, self.sha)["groups"]],
                         [self.base["title"], self.other["title"]])

    def test_original_boundary_repair_is_bounded_and_cannot_publish_on_failure(self):
        merged = self.group(1, 4, "机房样本", "保留固定原文")
        verdict = self.failed(1, 3, "boundary")
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [merged]}, verdict, {"groups": [merged]}, verdict,
                                       {"groups": [merged]}, verdict, {"groups": []}]) as harness:
            with self.assertRaisesRegex(RequestError, "review_requires_human"):
                semantic.process(self.vault, self.sha, self.policy, preserve_original=True)
        self.assertEqual(harness.call_count, 7)
        with patch.object(semantic.codex, "run_structured", side_effect=AssertionError("unexpected egress")):
            with self.assertRaisesRegex(RequestError, "review_requires_human"):
                semantic.process(self.vault, self.sha, self.policy, preserve_original=True)
        self.assert_no_formal_output()

    def test_small_boundary_repair_uses_local_evidence_before_full_resegmentation(self):
        items = [{"n": number, "kind": "paragraph", "text": f"合成固定原文第{number}段"}
                 for number in range(1, 9)]
        groups = [self.group(n, n + 1, f"主题{n}", f"第{n}组说明样本和成立条件。")
                  for n in (1, 3, 5, 7)]
        issues = self.failed(3, 5, "boundary")
        replacement = [self.group(3, 5, "同一组说明", "合并相邻固定段，保留理由和样本范围。"),
                       self.group(6, 6, "另一项内容", "另一个判断和成立条件。")]
        semantic.receipt(self.vault, self.sha, "review").parent.mkdir(parents=True)
        standard = semantic.load_article_board_standard()
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": groups}, issues, {"groups": replacement}, self.passed]) as harness:
            result, signature = semantic.stages(self.vault, self.sha, self.policy, items,
                                                preserve_original=True, standard=standard)
        self.assertEqual(harness.call_count, 4)
        self.assert_phase(harness.call_args_list[2].args[0], semantic.LOCAL_BOUNDARY_INSTRUCTIONS)
        self.assertEqual([item["n"] for item in harness.call_args_list[2].args[1]["evidence"]], [3, 4, 5, 6])
        self.assertEqual(harness.call_args_list[3].args[1]["evidence"], items)
        self.assertEqual(result, semantic.original_groups([groups[0], *replacement, groups[-1]], items))
        self.assertEqual(signature["review_role"], semantic.FIRST_LOCAL_BOUNDARY_REVIEW_ROLE)
        receipt = semantic.read_json(semantic.receipt(
            self.vault, self.sha, f"1-8-{signature['contract']}-{signature['review_role']}"))
        self.assertEqual(receipt["input_sha256"], semantic.fingerprint({"evidence": items, "groups": result}))
        self.assertGreater(receipt["timing"]["input_bytes"], 0)

    def test_original_final_repair_preserves_unaffected_card_and_is_independently_reviewed(self):
        merged = self.group(1, 4, "机房样本", self.base["summary"] + self.other["summary"])
        boundary = self.failed(1, 3, "boundary")
        unsupported = self.failed(1, 2, "unsupported")
        draft = [{"groups": [merged]}, boundary, {"groups": [merged]}, boundary,
                 {"groups": [self.base, self.other]}, unsupported, {"groups": [self.base]}, self.passed]
        with patch.object(semantic.codex, "run_structured", side_effect=draft) as harness:
            self.assertEqual(semantic.process(self.vault, self.sha, self.policy, preserve_original=True)["state"],
                             "published")
        self.assertEqual(len(harness.call_args_list[-2].args[1]["groups"]), 1)
        self.assertIn("组起点为 [1]", harness.call_args_list[-1].args[0])
        signed = semantic.read_json(semantic.receipt(self.vault, self.sha, "review"))
        self.assertEqual(signed["reviews"][0]["review_role"], semantic.FINAL_SOURCE_REVIEW_ROLE)

    def test_external_plan_is_local_draft(self):
        plan = {"sha256": self.sha, "groups": [self.base, self.other]}
        result = presentation.publish(self.vault, self.sha, plan)
        self.assertEqual(result["state"], "draft")
        self.assertEqual(presentation.publish(self.vault, self.sha, plan), result)
        self.assert_no_formal_output()
        with self.assertRaisesRegex(RequestError, "review_required"):
            semantic.reviewed_plan(self.vault, self.sha)

    def test_selected_draft_skips_generation_and_is_independently_repaired(self):
        wrong = self.group(1, 2, "配电样本", "2024年8月新增的35家机房中有10家采用新配电方案。")
        missing = self.group(3, 4, "散热样本", "液冷开机时间下降。")
        result = presentation.publish(self.vault, self.sha, {"sha256": self.sha, "groups": [wrong, missing]})
        draft_sha = result["draft_sha256"]
        with patch.object(semantic.codex, "run_structured", side_effect=AssertionError("unexpected egress")):
            with self.assertRaisesRegex(RequestError, "local_only"):
                semantic.process(self.vault, self.sha, self.policy, draft_sha=draft_sha)
        self.approve_draft(draft_sha)
        verdict = {"passed": False, "issues": [
            self.failed(1, 2, "denominator")["issues"][0],
            self.failed(1, 2, "date")["issues"][0],
            self.failed(3, 4, "counterexample")["issues"][0],
        ]}
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[verdict, {"groups": [self.base, self.other]}, self.passed]) as harness:
            published = semantic.process(self.vault, self.sha, self.policy, draft_sha=draft_sha)
        self.assertEqual(harness.call_count, 3)
        self.assert_phase(harness.call_args_list[0].args[0], semantic.REVIEW_INSTRUCTIONS)
        self.assertEqual(harness.call_args_list[0].args[1]["groups"], [wrong, missing])
        self.assertEqual(published["state"], "published")
        self.assertEqual(semantic.reviewed_plan(self.vault, self.sha)["groups"], [self.base, self.other])
        self.assertEqual(semantic.process(self.vault, self.sha, draft_sha=draft_sha)["state"], "already_published")
        with self.assertRaisesRegex(RequestError, "publication_locked"):
            semantic.process(self.vault, self.sha, draft_sha="a" * 64)

    def test_draft_authorization_is_rechecked_before_repair(self):
        saved = presentation.publish(self.vault, self.sha, {"sha256": self.sha, "groups": [self.base, self.other]})
        draft_sha = saved["draft_sha256"]
        self.approve_draft(draft_sha)

        def revoke_draft(*args):
            self.approve_draft("a" * 64)
            return self.failed(1, 2, "date")

        with patch.object(semantic.codex, "run_structured", side_effect=revoke_draft) as harness:
            with self.assertRaisesRegex(RequestError, "local_only"):
                semantic.process(self.vault, self.sha, self.policy, draft_sha=draft_sha)
        self.assertEqual(harness.call_count, 1)
        self.assert_no_formal_output()

    def test_missing_tampered_or_oversized_drafts_are_local_errors(self):
        with patch.object(semantic.codex, "run_structured", side_effect=AssertionError("unexpected egress")):
            with self.assertRaisesRegex(RequestError, "draft_not_found"):
                semantic.process(self.vault, self.sha, self.policy, draft_sha="a" * 64)
            saved = presentation.publish(self.vault, self.sha, {"sha256": self.sha, "groups": [self.base, self.other]})
            draft_sha = saved["draft_sha256"]
            path = semantic.receipt(self.vault, self.sha, f"draft-{draft_sha}")
            path.write_text(json.dumps({"sha256": self.sha, "groups": [self.other]}))
            with self.assertRaisesRegex(RequestError, "invalid_draft"):
                semantic.process(self.vault, self.sha, self.policy, draft_sha=draft_sha)
            long = self.group(1, 2, "长文本", "字" * 26_000)
            saved = presentation.publish(self.vault, self.sha, {"sha256": self.sha, "groups": [long, self.other]})
            with self.assertRaisesRegex(RequestError, "draft_too_large"):
                semantic.process(self.vault, self.sha, self.policy, draft_sha=saved["draft_sha256"])
        self.assert_no_formal_output()

    def test_draft_uses_separate_review_cache_from_generated_plan(self):
        plan = {"sha256": self.sha, "groups": [self.base, self.other]}
        saved = presentation.publish(self.vault, self.sha, plan)
        _, evidence = presentation.evidence_for(self.vault, self.sha)
        items = semantic.evidence_items(evidence["parts"], 1, len(evidence["parts"]))
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": plan["groups"]}, self.passed]):
            semantic.stages(self.vault, self.sha, self.policy, items, standard=semantic.load_article_board_standard())
        self.approve_draft(saved["draft_sha256"])
        with patch.object(semantic.codex, "run_structured", return_value=self.passed) as harness:
            semantic.process(self.vault, self.sha, self.policy, draft_sha=saved["draft_sha256"])
        self.assertEqual(harness.call_count, 1)

    def test_locally_repair_wrong_denominator_date_and_missing_counterexample(self):
        wrong = self.group(1, 2, "配电样本", "2024年8月新增的35家机房中有10家采用新配电方案。")
        missing = self.group(3, 4, "散热样本", "液冷开机时间下降。")
        verdict = {
            "passed": False,
            "issues": [self.failed(1, 2, "denominator")["issues"][0],
                       self.failed(1, 2, "date")["issues"][0],
                       self.failed(3, 4, "counterexample")["issues"][0]],
        }
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [wrong, missing]}, verdict,
                                       {"groups": [self.base, self.other]}, self.passed]) as harness:
            result = semantic.process(self.vault, self.sha, self.policy)
        self.assertEqual(harness.call_count, 4)
        self.assertEqual(result["review"], "passed")
        self.assert_phase(harness.call_args_list[1].args[0], semantic.REVIEW_INSTRUCTIONS)
        self.assert_phase(harness.call_args_list[3].args[0], semantic.REVIEW_INSTRUCTIONS)
        self.assertNotIn("title", harness.call_args_list[0].args[1])
        self.assertEqual(semantic.reviewed_plan(self.vault, self.sha)["groups"], [self.base, self.other])
        article = (self.folder / "完整阅读.md").read_text()
        self.assertIn("2025年9月", article)
        self.assertIn("50家", article)
        self.assertIn("两家老机房改造暂停", article)

    def test_repair_preserves_unrelated_groups_and_resume_after_revocation(self):
        wrong = self.group(1, 2, "配电样本", "2025年9月新配电方案占新增机房10/35。")

        def revoke_after_draft(*args):
            self.policy.unlink()
            return {"groups": [wrong, self.other]}

        with patch.object(semantic.codex, "run_structured", side_effect=revoke_after_draft) as harness:
            with self.assertRaisesRegex(RequestError, "local_only"):
                semantic.process(self.vault, self.sha, self.policy)
        self.assertEqual(harness.call_count, 1)
        self.assert_no_formal_output()
        self.policy.touch(mode=0o600)
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [self.sha]}))
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[self.failed(1, 2, "denominator"), {"groups": [self.base]}, self.passed]) as harness:
            semantic.process(self.vault, self.sha, self.policy)
        self.assertEqual(harness.call_count, 3)
        self.assertEqual(harness.call_args_list[1].args[1]["groups"], [wrong])
        self.assertEqual(semantic.reviewed_plan(self.vault, self.sha)["groups"][1], self.other)

    def test_second_repair_is_checked_against_source_before_publication(self):
        first = self.failed(1, 2, "condition")
        remaining = self.failed(1, 2, "attribution")
        interim = self.group(1, 2, "配电样本", "2025年9月，样本内50家机房中有10家使用新配电方案。")
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [self.base, self.other]}, first, {"groups": [interim]},
                                       remaining, {"groups": [self.base]}, self.passed]) as harness:
            result = semantic.process(self.vault, self.sha, self.policy)
        self.assertEqual(result["state"], "published")
        self.assertEqual(harness.call_count, 6)
        self.assertEqual(harness.call_args_list[4].args[1]["issues"], remaining["issues"])
        self.assert_phase(harness.call_args_list[5].args[0], semantic.REVIEW_INSTRUCTIONS)
        review = semantic.read_json(semantic.receipt(self.vault, self.sha, "review"))
        self.assertEqual(review["reviews"][0]["review_role"], "reviewer_after_second_repair")
        self.assertEqual(semantic.reviewed_plan(self.vault, self.sha)["groups"], [self.base, self.other])

    def test_failed_review_after_two_repairs_is_terminal_across_retries(self):
        verdict = self.failed(1, 2, "date")
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [self.base, self.other]}, verdict, {"groups": [self.base]}, verdict,
                                       {"groups": [self.base]}, verdict, verdict]) as harness:
            with self.assertRaisesRegex(RequestError, "review_requires_human"):
                semantic.process(self.vault, self.sha, self.policy)
        self.assertEqual(harness.call_count, 7)
        with patch.object(semantic.codex, "run_structured", side_effect=AssertionError("unexpected egress")):
            with self.assertRaisesRegex(RequestError, "review_requires_human"):
                semantic.process(self.vault, self.sha, self.policy)
        self.assert_no_formal_output()
        self.assertTrue((self.folder / "固定原文.md").is_file())

    def test_prior_reader_draft_is_reused_but_legacy_review_is_not(self):
        _, evidence = presentation.evidence_for(self.vault, self.sha)
        items = semantic.evidence_items(evidence["parts"], 1, len(evidence["parts"]))
        name = f"1-4-{'a' * 64}"
        reader = semantic.receipt(self.vault, self.sha, name + "-reader")
        reader.parent.mkdir(parents=True)
        groups = [self.base, self.other]
        standard_sha = semantic.fingerprint(semantic.load_article_board_standard())
        reader_input = {"evidence": items, "article_standard_sha256": standard_sha}
        old = {"input_sha256": semantic.fingerprint(reader_input), "role": "reader", "state": "completed",
               "harness": "codex_exec", "output": groups}
        intake.exclusive_json(reader, old)
        intake.exclusive_json(semantic.receipt(self.vault, self.sha, name + "-reviewer"),
                              {**old, "role": "reviewer", "output": self.passed})
        with patch.object(semantic.codex, "run_structured", return_value=self.passed) as harness:
            semantic.process(self.vault, self.sha, self.policy)
        self.assertEqual(harness.call_count, 1)
        self.assert_phase(harness.call_args.args[0], semantic.REVIEW_INSTRUCTIONS)

    def test_stale_reader_receipt_does_not_bypass_new_standard(self):
        _, evidence = presentation.evidence_for(self.vault, self.sha)
        items = semantic.evidence_items(evidence["parts"], 1, len(evidence["parts"]))
        old_input = {"evidence": items, "article_standard_sha256": semantic.fingerprint("旧标准")}
        path = semantic.receipt(self.vault, self.sha, f"1-4-{'a' * 64}-reader")
        path.parent.mkdir(parents=True)
        intake.exclusive_json(path, {"input_sha256": semantic.fingerprint(old_input), "role": "reader",
                                     "state": "completed", "harness": "codex_exec", "output": [self.base, self.other]})
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [self.base, self.other]}, self.passed]) as harness:
            semantic.process(self.vault, self.sha, self.policy)
        self.assertEqual(harness.call_count, 2)
        self.assert_phase(harness.call_args_list[0].args[0], semantic.INSTRUCTIONS)

    def test_unsigned_publication_with_old_standard_is_reprocessed(self):
        answers = [{"groups": [self.base, self.other]}, self.passed]
        with patch.object(semantic.codex, "run_structured", side_effect=answers):
            with patch.object(semantic, "_publish_reviewed", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    semantic.process(self.vault, self.sha, self.policy, preserve_original=True)
        path = semantic.receipt(self.vault, self.sha, "review")
        legacy = semantic.read_json(path)
        del legacy["article_standard_sha256"]
        path.write_text(json.dumps(legacy))
        with patch.object(semantic, "load_article_board_standard", return_value="更新后的卡面要求"), \
                patch.object(semantic.codex, "run_structured", return_value=self.passed) as harness:
            self.assertEqual(semantic.process(self.vault, self.sha, self.policy, preserve_original=True)["state"], "published")
        self.assertTrue(all(call.args[0].endswith("更新后的卡面要求") for call in harness.call_args_list))
        harness.assert_called_once()
        backup, = path.parent.glob("review-before-board-standard-*.json")
        self.assertEqual(semantic.read_json(backup), legacy)

    def test_extractive_recovery_preserves_evidence_and_gets_its_own_review(self):
        verdict = self.failed(1, 2, "coverage")
        wrong = self.group(1, 2, "新增机房", "2024年8月新增的35家机房中有10家采用新配电方案。")
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [wrong, self.other]}, verdict, {"groups": [wrong]}, verdict,
                                       {"groups": [wrong]}, verdict, self.passed]) as harness:
            semantic.process(self.vault, self.sha, self.policy)
        self.assertEqual(harness.call_count, 7)
        recovery_input = harness.call_args_list[-1].args[1]
        recovered = recovery_input["groups"][0]
        literal = "\n\n".join(item["text"] for item in recovery_input["evidence"][:2])
        self.assertEqual(recovered["summary"], "固定原文\n\n" + literal)
        self.assertEqual(recovered["argument"], "该组保留完整固定原文。")
        self.assertEqual(recovery_input["groups"][1], self.other)
        self.assert_phase(harness.call_args_list[-1].args[0], semantic.REVIEW_INSTRUCTIONS)
        plan = semantic.reviewed_plan(self.vault, self.sha)
        self.assertTrue(plan["includes_source_text"])
        self.assertEqual(plan["groups"], [recovered, self.other])

    def test_extractive_recovery_does_not_publish_invalid_boundaries(self):
        verdict = self.failed(1, 2, "boundary")
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [self.base, self.other]}, verdict, {"groups": []}]) as harness:
            with self.assertRaisesRegex(RequestError, "review_requires_human"):
                semantic.process(self.vault, self.sha, self.policy)
        self.assertEqual(harness.call_count, 3)
        self.assert_no_formal_output()

    def test_published_reading_and_canvas_keep_user_edits(self):
        with patch.object(semantic.codex, "run_structured", side_effect=[{"groups": [self.base, self.other]}, self.passed]):
            semantic.process(self.vault, self.sha, self.policy)
        reading = self.folder / "完整阅读.md"
        reading.write_text("人工修订")
        self.assertEqual(semantic.process(self.vault, self.sha)["state"], "already_published")
        self.assertEqual(reading.read_text(), "人工修订")
        self.assertEqual(semantic.reviewed_plan(self.vault, self.sha)["groups"], [self.base, self.other])
        (self.folder / "文章白板.canvas").unlink()
        with self.assertRaisesRegex(RequestError, "user_edit_conflict"):
            semantic.process(self.vault, self.sha)

    def test_partial_publication_resumes_with_exact_signed_content(self):
        write_json = presentation.exclusive_json

        def fail_canvas(path, value):
            if path.suffix == ".canvas":
                raise OSError("interrupted")
            return write_json(path, value)

        with patch.object(semantic.codex, "run_structured", side_effect=[{"groups": [self.base, self.other]}, self.passed]):
            with patch.object(presentation, "exclusive_json", side_effect=fail_canvas):
                with self.assertRaises(OSError):
                    semantic.process(self.vault, self.sha, self.policy)
        reading = self.folder / "完整阅读.md"
        self.assertTrue(reading.is_file())
        self.assertFalse((self.folder / "文章白板.canvas").exists())
        with self.assertRaisesRegex(RequestError, "review_required"):
            semantic.reviewed_plan(self.vault, self.sha)
        with patch.object(semantic.codex, "run_structured", side_effect=AssertionError("unexpected egress")):
            result = semantic.process(self.vault, self.sha)
        self.assertEqual(result["state"], "published")
        self.assertEqual(semantic.reviewed_plan(self.vault, self.sha)["groups"], [self.base, self.other])

    def test_retries_legacy_board_written_before_publication_receipt(self):
        write_json = presentation.exclusive_json

        def interrupt_receipt(path, value):
            if path == semantic.receipt(self.vault, self.sha, "publication"):
                raise OSError("interrupted after writing the article board")
            return write_json(path, value)

        with patch.object(semantic.codex, "run_structured", side_effect=[{"groups": [self.base, self.other]}, self.passed]):
            with patch.object(presentation, "exclusive_json", side_effect=interrupt_receipt):
                with patch.object(article_argument_boards, "project"):
                    with self.assertRaises(OSError):
                        semantic.process(self.vault, self.sha, self.policy)
        canvas = self.folder / "文章白板.canvas"
        self.assertIn("完整阅读.md#^topic-", canvas.read_text())
        self.assertFalse((self.folder / "篇内论述").exists())
        with patch.object(semantic.codex, "run_structured", side_effect=AssertionError("unexpected egress")):
            result = semantic.process(self.vault, self.sha)
        self.assertEqual(result["state"], "published")
        card = next(node for node in json.loads(canvas.read_text())["nodes"] if node["id"] == "t1")
        detail = self.folder / article_argument_boards.detail_relative(card["text"])
        self.assertTrue(detail.is_file())
        self.assertTrue(semantic.receipt(self.vault, self.sha, "publication").is_file())

    def test_mutated_review_output_blocks_publish(self):
        with patch.object(semantic.codex, "run_structured", side_effect=[{"groups": [self.base, self.other]}, self.passed]):
            with patch.object(semantic, "_publish_reviewed", side_effect=OSError):
                with self.assertRaises(OSError):
                    semantic.process(self.vault, self.sha, self.policy)
        reviewer = next((self.vault / ".afr" / "analysis" / self.sha).glob("*-reviewer.json"))
        result = json.loads(reviewer.read_text())
        result["output"] = self.failed(1, 2, "date")
        reviewer.write_text(json.dumps(result))
        with self.assertRaisesRegex(RequestError, "invalid_review_receipt"):
            semantic.process(self.vault, self.sha)
        self.assert_no_formal_output()
