import json
import unittest
from unittest.mock import patch

from afr_agent import dashboard, knowledge, semantic, workflow
from afr_agent.library import RequestError
from afr_agent.tests import test_questions


class KnowledgeRelationTests(unittest.TestCase):
    def setUp(self):
        fixture = test_questions.QuestionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.vault = fixture.vault
        self.policy = fixture.policy
        self.approvals = []

    def article(self, quote, origin, *, independence="独立", stage="审批", kind="事实",
                relation="新增", related="", supersedes="", ref="", date="2026-11"):
        title = f"{origin}机器人项目技术投入"
        sha, _ = self.fixture.article(title, f"# {title}\n\n{quote}")
        self.approvals.append(sha)
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": self.approvals}))
        self.policy.chmod(0o600)
        return {"sha256": sha, "topic_id": 2, "evidence_number": 2, "quote": quote, "claim": quote,
                "origin": origin, "stage": stage, "independence": independence, "kind": kind,
                "date": date, "relation": relation, "related_event": related, "supersedes": supersedes,
                "ref": ref}

    def update(self, events, *, topic="", previous=(), inspect=None, **topic_fields):
        def runner(_instructions, payload, _schema):
            if inspect:
                inspect(payload)
            return {"topics": [{"topic_id": topic, "object": "机器人项目", "dimension": "技术投入",
                                "category": "robot-investment", "events": events, **topic_fields}]}

        versions = [event["sha256"] for event in events]
        return knowledge.run(self.vault, [*previous, *versions], self.policy, runner, new_version_ids=versions)

    def topic_paths(self, topic):
        ledger = knowledge._state(self.vault / knowledge.STATE / "ledger.json")
        return knowledge._topic_paths(topic, knowledge._topic_path_map(ledger["topics"]))

    def topic(self, identifier):
        return knowledge._state(self.vault / knowledge.STATE / "ledger.json")["topics"][identifier]

    def test_repost_and_support_are_linked_without_replacing_the_claim_or_summary(self):
        original = self.article("甲公司披露机器人项目预算获批十亿元。", "甲公司")
        result = self.update([original], overview="追踪甲公司项目预算。", change_summary="原始审批。")
        topic, = result["topic_ids"]
        before = self.topic(topic)
        original_id, = result["event_ids"]
        self.assertEqual(result["added_events"], 1)
        page = self.vault / self.topic_paths(topic)[0]
        page.write_text(page.read_text() + "个人判断保留\n")
        repost = self.article("周报转述甲公司披露机器人项目获批十亿元。", "甲公司",
                              independence="转载", relation="转载", related=original_id)
        support = self.article("乙投研查阅公开备案，独立核对了机器人项目十亿元额度。", "乙投研",
                               relation="佐证", related=original_id)
        result = self.update([repost, support], topic=topic, previous=[original["sha256"]],
                             overview="本批不应覆盖已有题面。", change_summary="不应重写阶段进展。")
        self.assertEqual((result["added_events"], result["added_evidence_links"], result["deferred_topics"]),
                         (0, 2, 0))
        self.assertEqual(result["event_ids"], [])
        self.assertEqual({link["event_id"] for link in result["evidence_links"]}, {original_id})
        after = self.topic(topic)
        self.assertEqual({key: value for key, value in after.items() if key != "events"},
                         {key: value for key, value in before.items() if key != "events"})
        record, = after["events"]
        self.assertEqual({key: value for key, value in record.items() if key != "evidence_links"},
                         before["events"][0])
        self.assertEqual({proof["relation"] for proof in record["evidence_links"]}, {"转载", "佐证"})
        self.assertIn("|转载来源]]", page.read_text())
        self.assertIn("|独立佐证]]", page.read_text())
        self.assertTrue(page.read_text().endswith("个人判断保留\n"))
        self.assertEqual(page.read_text().count(f"^{original_id}"), 1)
        replay = knowledge.run(self.vault, self.approvals, self.policy, lambda *_: self.fail("unexpected model"),
                               new_version_ids=[repost["sha256"], support["sha256"]])
        self.assertEqual(replay["added_evidence_links"], 0)
        self.assertEqual(replay["evidence_links"], result["evidence_links"])
        self.assertEqual(self.topic(topic), after)

    def test_same_supporting_origin_counts_once_and_digest_only_exposes_approved_links(self):
        original = self.article("甲公司机器人项目批准十亿元预算。", "甲公司")
        result = self.update([original])
        topic, = result["topic_ids"]
        original_id, = result["event_ids"]
        first = self.article("审计组机器人项目备案核验结论为十亿元。", "审计组",
                             relation="佐证", related=original_id)
        second = self.article("审计组发布简报重述机器人项目预算核验结果为十亿元。", "审计组",
                              relation="佐证", related=original_id)
        result = self.update([first, second], topic=topic, previous=[original["sha256"]])
        self.assertEqual([proof["relation"] for proof in result["evidence_links"]], ["佐证", "转载"])
        third = self.article("行业周报引用审计组对机器人项目十亿元预算的固定披露。", "审计组",
                             relation="转载", related=original_id, independence="转载")

        def check_digest(payload):
            existing, = payload["existing_topics"]
            primary, = existing["recent_events"]
            evidence, = primary["evidence_links"]
            self.assertEqual(evidence["sha256"], first["sha256"])

        result = self.update([third], topic=topic, previous=[original["sha256"], first["sha256"]],
                             inspect=check_digest)
        self.assertEqual((result["added_events"], result["added_evidence_links"]), (0, 1))

    def test_batch_refs_stage_disagreement_and_explicit_correction_preserve_history(self):
        original = self.article("甲公司机器人项目预算获批十亿元。", "甲公司", ref="r-original")
        support = self.article("审计组独立核对机器人项目审批预算为十亿元。", "审计组",
                               relation="佐证", related="r-original")
        result = self.update([support, original])
        topic, = result["topic_ids"]
        prior_id, = result["event_ids"]
        self.assertEqual((result["added_events"], result["added_evidence_links"]), (1, 1))
        advance = self.article("甲公司机器人项目已签约，先交付两百台。", "甲公司", stage="签约",
                               relation="进展", related=prior_id, ref="r-advance")
        conflict = self.article("丙供货方认为甲公司的首批签约仅覆盖一百台。", "丙供货方", stage="签约",
                                relation="分歧", related="r-advance", kind="观点")
        correction = self.article("甲公司更正此前公布的机器人项目审批预算为八亿元。", "甲公司",
                                  kind="更正", supersedes=prior_id)
        result = self.update([conflict, advance, correction], topic=topic, previous=[original["sha256"]])
        self.assertEqual((result["added_events"], result["added_evidence_links"]), (3, 0))
        records = self.topic(topic)["events"]
        self.assertEqual({event["sha256"] for event in records},
                         {original["sha256"], advance["sha256"], conflict["sha256"], correction["sha256"]})
        matches = {event["sha256"]: event for event in records}
        self.assertEqual(matches[advance["sha256"]]["related_event"], prior_id)
        self.assertEqual(matches[conflict["sha256"]]["related_event"], matches[advance["sha256"]]["id"])
        self.assertEqual(matches[conflict["sha256"]]["supersedes"], "")
        self.assertEqual(matches[correction["sha256"]]["supersedes"], prior_id)
        note, canvas = self.topic_paths(topic)
        paths = knowledge._argument_paths(canvas, records)
        prior_path = paths[prior_id]
        advance_path = paths[matches[advance["sha256"]]["id"]]
        self.assertIn(f"[[{prior_path}|此前记录]]", (self.vault / note).read_text())
        self.assertIn(f"[[{advance_path}|对照另一判断]]", (self.vault / note).read_text())
        board = json.loads((self.vault / canvas).read_text())
        cards = {node["id"]: node["text"] for node in board["nodes"] if node["type"] == "text"}
        self.assertIn(f"[[{prior_path}|此前记录]]", cards[matches[advance["sha256"]]["id"]])
        self.assertIn(f"[[{advance_path}|对照另一判断]]", cards[matches[conflict["sha256"]]["id"]])

    def test_different_independent_observations_in_one_paragraph_keep_both_proofs(self):
        original = self.article("甲公司机器人项目批准十亿元预算。", "甲公司")
        result = self.update([original])
        topic, = result["topic_ids"]
        original_id, = result["event_ids"]
        first_quote = "乙审计组从项目备案独立核对出十亿元预算。"
        second_quote = "丙研究机构以机器人项目资金预算表独立核对出十亿元。"
        first = self.article(first_quote + second_quote, "乙审计组", relation="佐证", related=original_id)
        first = {**first, "quote": first_quote, "claim": "机器人项目审批额度为十亿元。"}
        second = {**first, "quote": second_quote, "origin": "丙研究机构"}
        result = self.update([first, second], topic=topic, previous=[original["sha256"]])
        self.assertEqual(result["added_evidence_links"], 2)
        self.assertEqual({proof["relation"] for proof in result["evidence_links"]}, {"佐证"})
        self.assertEqual(len({proof["evidence_id"] for proof in result["evidence_links"]}), 2)

    def test_home_reports_evidence_only_and_protects_manually_edited_knowledge_note(self):
        original = self.article("甲公司机器人项目批准十亿元预算。", "甲公司")
        published = self.update([original])
        topic, = published["topic_ids"]
        original_id, = published["event_ids"]
        repost = self.article("周报转述甲公司机器人项目批准十亿元预算。", "甲公司", independence="转载",
                              relation="转载", related=original_id)
        result = self.update([repost], topic=topic, previous=[original["sha256"]])
        db = workflow.database(self.vault)
        self.addCleanup(db.close)
        with db:
            db.execute("INSERT INTO knowledge_batches(scope,state,result) VALUES('finished','ready',?)",
                       (json.dumps({**result, "new_versions": [repost["sha256"]]}),))
            db.execute("INSERT INTO workflow_events(scope,kind,at) VALUES('finished','knowledge_finished',120)")
        home = dashboard.render(self.vault, db).decode()
        note, canvas = self.topic_paths(topic)
        self.assertIn("补充出处 1 条", home)
        self.assertIn(f"[[{note}#^{original_id}|", home)
        self.assertNotIn("新增记录", home)
        board_path = self.vault / canvas
        board = json.loads(board_path.read_text())
        board["nodes"].reverse()
        board_path.write_text(json.dumps(board))
        home = dashboard.render(self.vault, db).decode()
        self.assertNotIn("保留手改", home)
        board["nodes"][0]["x"] += 24
        board_path.write_text(json.dumps(board))
        home = dashboard.render(self.vault, db).decode()
        self.assertIn("1 个入口保留手改", home)
        self.assertIn(f"[[{canvas}|", home)
        self.assertIn("补充出处 1 条", home)
        path = self.vault / note
        edited = path.read_text().replace("转载来源", "手工整理的出处")
        path.write_text(edited)
        home = dashboard.render(self.vault, db).decode()
        self.assertIn("2 个入口保留手改", home)
        self.assertIn(f"[[{note}|", home)
        self.assertNotIn(f"[[{note}#^{original_id}|", home)
        self.assertEqual(path.read_text(), edited)
        replay = knowledge.run(self.vault, self.approvals, self.policy, lambda *_: self.fail("unexpected model"),
                               new_version_ids=[repost["sha256"]])
        self.assertEqual(set(replay["protected_paths"]), {note, canvas})
        self.assertEqual(path.read_text(), edited)
        self.assertEqual(json.loads(board_path.read_text()), board)

    def test_existing_index_does_not_turn_an_empty_batch_into_a_published_change(self):
        original = self.article("甲公司机器人项目批准十亿元预算。", "甲公司")
        self.update([original])
        unrelated = self.article("航线调查只记下船名，没有形成任何可比较的判断。", "行业周报")
        db = workflow.database(self.vault)
        self.addCleanup(db.close)
        with db:
            db.execute("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES(?,?,100,'ready')",
                       (unrelated["sha256"], "测试材料"))
        calls = []
        actual_run = knowledge.run

        def run(vault, versions, approvals, **kwargs):
            def runner(*_):
                calls.append(True)
                return {"topics": []}

            return actual_run(vault, versions, approvals, runner, **kwargs)

        with patch.object(knowledge, "run", side_effect=run):
            workflow.supplement_knowledge(self.vault, db, self.policy)
            workflow.supplement_knowledge(self.vault, db, self.policy)
        batch = db.execute("SELECT state,result FROM knowledge_batches").fetchone()
        self.assertEqual(batch["state"], "no_common_topic")
        self.assertEqual(json.loads(batch["result"])["index_path"], knowledge.INDEX)
        self.assertEqual(len(calls), 2)
        home = dashboard.render(self.vault, db).decode()
        self.assertIn("本批没有形成可跟踪主题", home)
        self.assertNotIn("已发布", home)
        with db:
            db.execute("UPDATE knowledge_batches SET state='ready',result=?", (json.dumps({
                "topics": 1, "topic_ids": [], "event_ids": [], "evidence_links": [],
                "index_path": knowledge.INDEX,
            }),))
        home = dashboard.render(self.vault, db).decode()
        self.assertIn("本批未产生知识增量", home)
        self.assertNotIn("没有形成可跟踪主题", home)

    def test_signed_viewpoint_matches_its_own_fixed_quote_without_rewriting_event_id(self):
        event = self.article("甲公司机器人项目批准十亿元预算。", "甲公司")
        pointer = event["sha256"], event["topic_id"], event["evidence_number"]
        first_id = "v-" + "a" * 24
        second_id = "v-" + "b" * 24
        pointers = {pointer: ("合成测试", event["quote"], {
            first_id: (event["quote"],),
            second_id: ("另一组证据",),
        })}
        old = knowledge._validated_event(event, pointers, set())
        valid = knowledge._validated_event({**event, "viewpoint_id": first_id}, pointers, set())
        self.assertEqual(valid["viewpoint_id"], first_id)
        self.assertEqual(valid["id"], old["id"])
        ambiguous = {pointer: ("合成测试", event["quote"], {
            first_id: (event["quote"],),
            second_id: (event["quote"],),
        })}
        self.assertNotIn("viewpoint_id", knowledge._validated_event(event, ambiguous, set()))
        with self.assertRaisesRegex(RequestError, "invalid_knowledge_event"):
            knowledge._validated_event({**event, "viewpoint_id": second_id}, pointers, set())

    def test_signed_viewpoint_receipt_flows_into_the_original_and_a_supplement(self):
        def signed_event(quote, origin, *, relation="新增", related=""):
            title = f"{origin}机器人项目技术投入"
            sha, _ = self.fixture.article(title, f"# {title}\n\n{quote}", publish=False)
            self.approvals.append(sha)
            self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": self.approvals}))
            self.policy.chmod(0o600)

            def review(_instruction, payload, schema):
                if schema == semantic.REVIEW_SCHEMA:
                    return {"passed": True, "issues": []}
                groups = []
                for part in payload["evidence"]:
                    number = part["n"]
                    selected = [{"judgment": quote, "speaker": origin, "kind": "reported_fact", "grounds": [],
                                 "conditions": [], "evidence": [{"number": number, "quote": quote}]}] if number == 2 else []
                    groups.append({"start": number, "end": number, "title": f"话题 {number}",
                                   "branch": "预算", "summary": part["text"], "argument": part["text"],
                                   "viewpoints": selected})
                return {"groups": groups}

            with patch.object(semantic.codex, "run_structured", side_effect=review):
                self.assertEqual(semantic.process(self.vault, sha, self.policy)["state"], "published")
            record, = semantic.article_viewpoints(semantic.reviewed_plan(self.vault, sha))
            return {"sha256": sha, "topic_id": 2, "evidence_number": 2, "quote": quote, "claim": quote,
                    "origin": origin, "stage": "审批", "independence": "独立", "kind": "事实", "date": "2026-11",
                    "relation": relation, "related_event": related, "supersedes": "", "viewpoint_id": record["id"]}

        source = signed_event("甲公司机器人项目批准十亿元预算。", "甲公司")
        initial = self.update([source])
        topic, = initial["topic_ids"]
        original_id, = initial["event_ids"]
        support = signed_event("乙审计组独立核对机器人项目获批十亿元预算。", "乙审计组",
                               relation="佐证", related=original_id)
        result = self.update([support], topic=topic, previous=[source["sha256"]])
        self.assertEqual(result["added_evidence_links"], 1)
        original, = self.topic(topic)["events"]
        evidence, = original["evidence_links"]
        self.assertEqual(original["viewpoint_id"], source["viewpoint_id"])
        self.assertEqual(evidence["viewpoint_id"], support["viewpoint_id"])

    def test_links_require_an_authorized_target_and_independent_origin(self):
        original = self.article("甲公司机器人项目预算获批十亿元。", "甲公司")
        topic, = self.update([original])["topic_ids"]
        original_id = self.topic(topic)["events"][0]["id"]
        falsely_independent = self.article("甲公司公告再次确认机器人项目预算十亿元。", "甲公司",
                                           relation="佐证", related=original_id)
        result = self.update([falsely_independent], topic=topic, previous=[original["sha256"]])
        self.assertEqual((result["topics"], result["deferred_topics"]), (0, 1))
        unknown = self.article("他方独立核对机器人项目审批预算为十亿元。", "他方",
                               relation="佐证", related=original_id)
        result = self.update([unknown], inspect=lambda payload: self.assertEqual(payload["existing_topics"], []))
        self.assertEqual((result["topics"], result["deferred_topics"]), (0, 1))
        self.assertEqual(len(self.topic(topic)["events"]), 1)
        self.assertNotIn("evidence_links", self.topic(topic)["events"][0])


if __name__ == "__main__":
    unittest.main()
