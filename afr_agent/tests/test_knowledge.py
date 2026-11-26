import json
import sqlite3
import unittest
from unittest.mock import patch

from afr_agent import knowledge, questions
from afr_agent.library import RequestError
from afr_agent.tests import test_questions


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        fixture = test_questions.QuestionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.vault = fixture.vault
        self.policy = fixture.policy

    def topic_paths(self, topic):
        ledger = knowledge._state(self.vault / knowledge.STATE / "ledger.json")
        return knowledge._topic_paths(topic, knowledge._topic_path_map(ledger["topics"]))

    def approve(self, *shas):
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": shas}))
        self.policy.chmod(0o600)

    def evidence(self):
        first, _ = self.fixture.article("甲公司机器人投入公告", "# 甲公司机器人投入公告\n\n甲公司审批机器人项目预算十亿元。")
        second, _ = self.fixture.article("乙公司机器人投入公告", "# 乙公司机器人投入公告\n\n乙公司已支付机器人项目首期两亿元。")
        self.approve(first, second)
        return first, second

    def event(self, sha, *, number=2, origin, stage, quote, kind="事实", supersedes="", independence="独立"):
        return dict(sha256=sha, topic_id=number, evidence_number=number, quote=quote,
                    origin=origin, independence=independence, stage=stage, date="2026-11",
                    kind=kind, claim=quote, supersedes=supersedes)

    def proposal(self, first, second):
        return {"topics": [{"topic_id": "", "object": "机器人", "dimension": "技术投入",
                            "category": "robot-investment", "events": [
            self.event(first, origin="甲公司", stage="审批", quote="甲公司审批机器人项目预算十亿元。"),
            self.event(second, origin="乙公司", stage="支付", quote="乙公司已支付机器人项目首期两亿元。")
        ]}]}

    def test_review_projects_stable_time_line_and_preserves_human_changes(self):
        first, second = self.evidence()
        proposal = self.proposal(first, second)
        item = proposal["topics"][0]
        item.update(overview="跟踪机器人项目预算与付款。", follow_up="首期支付后的实际建设。")
        item["events"][0].update(headline="甲公司通过项目预算审批", context="先核对实际付款进展。")
        db_path = self.vault / ".afr/intake.sqlite"
        with sqlite3.connect(db_path) as db:
            db.executemany("UPDATE source_times SET modified_ns=? WHERE sha=?",
                           [(1791907199000000000, second), (1791907200000000000, first)])
        passes = []

        def runner(instructions, payload, schema):
            passes.append(instructions.splitlines()[0])
            self.assertEqual({item["sha256"] for item in payload["articles"]}, {first, second})
            self.assertEqual(schema, knowledge.SCHEMA)
            if len(passes) == 2:
                self.assertEqual(payload["draft"], proposal)
            return proposal

        result = knowledge.run(self.vault, [first, second], self.policy, runner)
        self.assertEqual(len(passes), 2)
        ledger = self.vault / knowledge.STATE / "ledger.json"
        before_restart = ledger.read_bytes()
        self.assertIsNone(knowledge.refresh_views(self.vault))
        self.assertEqual(ledger.read_bytes(), before_restart)
        self.assertEqual((result["topics"], result["added_events"], result["deferred_topics"]), (1, 2, 0))
        topic, = result["topic_ids"]
        page, canvas = (self.vault / path for path in self.topic_paths(topic))
        original = page.read_text()
        self.assertIn(r"材料中提到的事件时间 2026\-11", original)
        self.assertEqual(original.count("固定原文.md#^e2"), 2)
        self.assertIn("来源发表 未知 · 收录 未知", original)
        board = json.loads(canvas.read_text())
        cards = [node for node in board["nodes"] if node["id"].startswith("e-")]
        earlier, later = cards
        self.assertEqual(earlier["color"], later["color"])
        self.assertEqual(earlier["y"], later["y"])
        self.assertLess(earlier["x"] + earlier["width"], later["x"])
        self.assertIn("2026年10月13日", earlier["text"])
        self.assertIn("2026年10月14日", later["text"])
        self.assertIn("甲公司通过项目预算审批", later["text"])
        self.assertIn("先核对实际付款进展", later["text"])
        root = next(node for node in board["nodes"] if node["id"] == "root")
        self.assertIn("**2026年10月 · 2 条**", root["text"])
        self.assertIn("|固定原文]]", root["text"])
        month, = [node for node in board["nodes"] if node["id"].startswith("month-")]
        self.assertRegex(month["label"], r"^2026年10月 · 2 条　.+$")
        index_text = (self.vault / result["index_path"]).read_text()
        self.assertIn(canvas.relative_to(self.vault).as_posix(), index_text)
        self.assertNotIn(topic, index_text)
        self.assertIn(knowledge.OVERVIEW, index_text)
        overview = json.loads((self.vault / knowledge.OVERVIEW).read_text())
        self.assertEqual(overview["nodes"][1]["label"], "科技与产业")
        self.assertIn("具身 AI · 技术投入", overview["nodes"][2]["text"])
        self.assertNotIn(topic, {node["id"] for node in overview["nodes"]})
        category = json.loads((self.vault / knowledge._category_path("robot-investment")).read_text())
        self.assertEqual(category["nodes"][1]["id"], topic)
        self.assertEqual(category["nodes"][1]["color"], earlier["color"])
        for label in ("跟踪机器人项目预算与付款", "打开问题白板", "甲公司", "乙公司", "2 条记录"):
            self.assertIn(label, category["nodes"][1]["text"])
        self.assertEqual(questions.catalog(self.vault), [])
        page.write_text(original + "人工补充结果\n")
        board["nodes"][1]["x"] += 40
        canvas.write_text(json.dumps(board))
        replay = knowledge.run(self.vault, [second, first], self.policy, lambda *_: self.fail("unexpected model"))
        self.assertEqual(replay["topic_ids"], [topic])
        self.assertEqual(replay["added_events"], 0)
        self.assertEqual(replay["protected_paths"], [canvas.relative_to(self.vault).as_posix()])
        self.assertEqual(page.read_text(), original + "人工补充结果\n")
        self.assertEqual(json.loads(canvas.read_text()), board)

    def test_markerless_markdown_migrates_verified_regions_and_preserves_manual_bytes(self):
        first, second = self.evidence()
        result = knowledge.run(self.vault, [first, second], self.policy, lambda *_: self.proposal(first, second))
        note = self.topic_paths(result["topic_ids"][0])[0]
        ledger = self.vault / knowledge.STATE / "ledger.json"
        state = knowledge._state(ledger)
        prefix = "前置手写区\r\n\r\n".encode()
        suffix = "\n## 自由笔记\n\n手写结论\n".encode()
        bodies = {}
        for relative in (note, knowledge.INDEX):
            path = self.vault / relative
            _, body, _ = knowledge._checked_markdown(path.read_bytes(), state, relative)
            self.assertNotIn(b"AFR:KNOWLEDGE", body)
            bodies[relative] = body
            legacy = f"{knowledge.BEGIN}\n".encode() + body + f"{knowledge.END}\n".encode()
            path.write_bytes(prefix + legacy + suffix)
            state["projections"][relative] = knowledge.digest(legacy)
            state["markdown_boundaries"].pop(relative)
        knowledge._save(ledger, state)
        path = self.vault / note
        saved_legacy = path.read_bytes()
        legacy_edit = saved_legacy.replace(b"### ", b"#### ", 1)
        self.assertNotEqual(saved_legacy, legacy_edit)
        path.write_bytes(legacy_edit)
        self.assertEqual(knowledge.refresh_views(self.vault, force=True), [note])
        self.assertEqual(path.read_bytes(), legacy_edit)
        path.write_bytes(saved_legacy)

        self.assertEqual(knowledge.refresh_views(self.vault, force=True), [])
        state = knowledge._state(ledger)
        for relative, body in bodies.items():
            path = self.vault / relative
            self.assertEqual(path.read_bytes(), prefix + body + suffix)
            self.assertEqual(state["projections"][relative], knowledge.digest(body))
            self.assertEqual(knowledge._checked_markdown(path.read_bytes(), state, relative), (prefix, body, suffix))

        path = self.vault / note
        edited = prefix + bodies[note].replace(b"### ", b"#### ", 1) + suffix
        path.write_bytes(edited)
        self.assertEqual(knowledge.refresh_views(self.vault, force=True), [note])
        self.assertEqual(path.read_bytes(), edited)

    def test_untracked_or_ambiguous_markdown_is_never_adopted(self):
        body = knowledge._markdown("样例", ["论述"])
        manual = body + "\n手写摘抄\n".encode()
        path = self.vault / "无账本.md"
        path.write_bytes(manual)
        with self.assertRaisesRegex(RequestError, "user_edit_conflict"):
            knowledge._prepare_markdown(path, body, {"projections": {}}, path.name)
        self.assertEqual(path.read_bytes(), manual)
        state = {"projections": {path.name: knowledge.digest(body)}, "markdown_boundaries": {
            path.name: {"current": knowledge._markdown_boundary(body)}}}
        duplicated = manual + body
        path.write_bytes(duplicated)
        with self.assertRaisesRegex(RequestError, "user_edit_conflict"):
            knowledge._prepare_markdown(path, body, state, path.name)
        self.assertEqual(path.read_bytes(), duplicated)

    def test_interrupted_legacy_markdown_write_can_resume_without_force(self):
        first, second = self.evidence()
        result = knowledge.run(self.vault, [first, second], self.policy, lambda *_: self.proposal(first, second))
        note = self.topic_paths(result["topic_ids"][0])[0]
        file = self.vault / note
        ledger = self.vault / knowledge.STATE / "ledger.json"
        state = knowledge._state(ledger)
        _, body, suffix = knowledge._checked_markdown(file.read_bytes(), state, note)
        wrapped = f"{knowledge.BEGIN}\n".encode() + body + f"{knowledge.END}\n".encode()
        file.write_bytes(wrapped + suffix)
        state["projections"][note] = knowledge.digest(wrapped)
        state["markdown_boundaries"].pop(note)
        knowledge._save(ledger, state)
        write = knowledge._write_projection

        def fail_after_note(path, old, new):
            write(path, old, new)
            if path == file:
                raise OSError("interrupted after replacement")

        with patch.object(knowledge, "_write_projection", side_effect=fail_after_note):
            with self.assertRaisesRegex(OSError, "interrupted"):
                knowledge.refresh_views(self.vault, force=True)
        self.assertEqual(file.read_bytes(), body + suffix)
        self.assertIn("pending", knowledge._state(ledger)["markdown_boundaries"][note])
        self.assertEqual(knowledge.refresh_views(self.vault), [])
        self.assertEqual(file.read_bytes(), body + suffix)
        self.assertNotIn("pending", knowledge._state(ledger)["markdown_boundaries"][note])

    def test_publication_date_keeps_market_date_and_ambiguous_timestamps_separate(self):
        def headline(*parts):
            return {"parts": [{"text": part} for part in parts]}

        self.assertEqual(knowledge._publication_date(headline(
            "研报标题", "日期：2026年10月20日｜行情口径：美东 2026-10-19 收盘")), "2026-10-20")
        self.assertEqual(knowledge._publication_date(headline(
            "发布日期：\n2026 年08 月04 日")), "2026-08-04")
        self.assertIsNone(knowledge._publication_date(headline(
            "2026年08月29日 00:32", "行情日期：2026-10-19")))
        self.assertIsNone(knowledge._publication_date(headline(
            "日期：2026-02-29", "报告日期：2026-02-30")))
        self.assertIsNone(knowledge._publication_date(headline(
            "发布日期：2026-10-19", "报告日期：2026-10-20")))

    def test_correction_keeps_prior_entry_and_scope_does_not_send_unapproved_history(self):
        first, second = self.evidence()
        result = knowledge.run(self.vault, [first, second], self.policy, lambda *_: self.proposal(first, second))
        topic, = result["topic_ids"]
        page = self.vault / self.topic_paths(topic)[0]
        page.write_text(page.read_text() + "人工判断\n")
        third, _ = self.fixture.article("甲公司机器人审批更正", "# 甲公司机器人审批更正\n\n甲公司修订机器人项目预算为八亿元。")
        self.approve(first, second, third)

        def correction(_instructions, payload, _schema):
            existing, = payload["existing_topics"]
            self.assertEqual(existing["id"], topic)
            approved = next(event for event in existing["recent_events"] if event["stage"] == "审批")
            event = self.event(third, origin="甲公司", stage="审批", kind="更正", supersedes=approved["id"],
                               quote="甲公司修订机器人项目预算为八亿元。")
            self.assertEqual(existing["category"], "robot-investment")
            return {"topics": [{"topic_id": topic, "object": "机器人", "dimension": "技术投入",
                                "category": "robot-deployment", "events": [event]}]}

        update = knowledge.run(self.vault, [first, second, third], self.policy, correction, new_version_ids=[third])
        self.assertEqual(update["topic_ids"], [topic])
        self.assertEqual(update["added_events"], 1)
        ledger = knowledge._state(self.vault / knowledge.STATE / "ledger.json")
        self.assertEqual(ledger["topics"][topic]["category"], "robot-investment")
        self.assertFalse((self.vault / knowledge._category_path("robot-deployment")).exists())
        text = page.read_text()
        self.assertIn("预算十亿元", text)
        self.assertIn("预算为八亿元", text)
        self.assertIn("被修订的记录", text)
        self.assertTrue(text.endswith("人工判断\n"))

        self.approve(third)

        def narrow(_instructions, payload, _schema):
            self.assertEqual(payload["existing_topics"], [])
            self.assertEqual({article["sha256"] for article in payload["articles"]}, {third})
            return {"topics": []}

        knowledge.run(self.vault, [third], self.policy, narrow)
        with self.assertRaisesRegex(RequestError, "^local_only$"):
            knowledge.run(self.vault, [first, third], self.policy, lambda *_: self.fail("unauthorized upload"))

    def test_single_source_topic_quote_and_consent_are_rechecked(self):
        first, second = self.evidence()
        proposal = self.proposal(first, second)
        first_event = proposal["topics"][0]["events"][0]
        single = {"topics": [{**proposal["topics"][0], "events": [first_event]}]}
        result = knowledge.run(self.vault, [first], self.policy, lambda *_: single)
        self.assertEqual((result["topics"], result["added_events"], result["deferred_topics"]), (1, 1, 0))
        self.assertTrue((self.vault / knowledge.INDEX).exists())

        calls = []

        def revoke(*_):
            calls.append(True)
            self.approve(first)
            return proposal

        with self.assertRaisesRegex(RequestError, "^local_only$"):
            knowledge.run(self.vault, [first, second], self.policy, revoke, new_version_ids=[first])
        self.assertEqual(calls, [True])

        self.approve(first, second)
        proposal["topics"][0]["events"][1]["origin"] = "乙公司"
        proposal["topics"][0]["events"][1]["quote"] = "材料中不存在的引文。"
        result = knowledge.run(self.vault, [first, second], self.policy, lambda *_: proposal, new_version_ids=[second])
        self.assertEqual((result["topics"], result["deferred_topics"]), (0, 1))

    def test_zero_card_id_recovers_only_unique_same_source_and_literal_quote(self):
        sha = "a" * 64
        quote = "生产线已经交付"
        event = self.event(sha, number=8, origin="甲公司", stage="交付", quote=quote)
        pointers = {(sha, 3, 8): ("固定材料", "固定材料写明生产线已经交付。")}
        self.assertEqual(knowledge._validated_event({**event, "topic_id": 0}, pointers, set())["topic_id"], 3)
        for invalid in ({**event, "topic_id": 9}, {**event, "topic_id": 0, "quote": "与原文不符"}):
            with self.assertRaisesRegex(RequestError, "^invalid_knowledge_event$"):
                knowledge._validated_event(invalid, pointers, set())
        with self.assertRaisesRegex(RequestError, "^invalid_knowledge_event$"):
            knowledge._validated_event({**event, "topic_id": 0},
                                       {**pointers, (sha, 9, 8): ("固定材料", quote)}, set())

    def test_canvas_update_keeps_obsidian_order_but_protects_content_changes(self):
        path = self.vault / "知识白板.canvas"
        original = {"nodes": [{"id": "root", "x": 0}, {"id": "event", "x": 30}], "edges": []}
        previous = knowledge._json_bytes(original)
        reshuffled = {"nodes": list(reversed(original["nodes"])), "edges": []}
        current = json.dumps(reshuffled, separators=(",", ":")).encode()
        path.write_bytes(current)
        projected = {"nodes": [*original["nodes"], {"id": "new", "x": 60}], "edges": []}
        saved = knowledge._publish_projection(path, knowledge._json_bytes(projected),
                                              knowledge.digest(previous), previous)
        self.assertEqual([n["id"] for n in json.loads(path.read_bytes())["nodes"]], ["event", "root", "new"])
        self.assertEqual(saved, knowledge.digest(path.read_bytes()))
        reshuffled["nodes"][0] = {"id": "event", "x": 45}
        path.write_text(json.dumps(reshuffled))
        with self.assertRaisesRegex(RequestError, "^user_edit_conflict$"):
            knowledge._publish_projection(path, knowledge._json_bytes(projected),
                                          knowledge.digest(previous), previous)

    def test_requires_signed_article_even_when_version_is_approved(self):
        sha, _ = self.fixture.article("未签发", "只有草稿。", publish=False)
        self.approve(sha)
        with self.assertRaisesRegex(RequestError, "^review_required$"):
            knowledge.run(self.vault, [sha], self.policy, lambda *_: self.fail("unsigned article"))

    def test_ten_sources_two_topics_and_publish_recovery(self):
        first, second = self.evidence()
        extra = [self.fixture.article(f"机器人合成材料 {i}", f"# 机器人合成材料 {i}\n\n合成材料 {i} 描述机器人。")[0]
                 for i in range(8)]
        sources = [first, second, *extra]
        self.approve(*sources)
        proposal = self.proposal(first, second)
        proposal["topics"].append({**proposal["topics"][0], "object": "机器人项目", "dimension": "项目资金"})
        calls = []

        def run_model(_instructions, payload, _schema):
            calls.append(True)
            self.assertEqual(len(payload["articles"]), 10)
            return proposal

        write = knowledge._write_projection

        def interrupt(path, original, target):
            if path.name == "知识总结总白板.canvas":
                raise OSError("合成投影中断")
            return write(path, original, target)

        with patch.object(knowledge, "_write_projection", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "^合成投影中断$"):
                knowledge.run(self.vault, sources, self.policy, run_model)
        self.assertEqual(calls, [True, True])

        result = knowledge.run(self.vault, sources, self.policy, lambda *_: self.fail("existing receipt expected"))
        self.assertEqual((result["topics"], result["added_events"], result["protected_paths"]), (2, 0, []))
        category = json.loads((self.vault / knowledge._category_path("robot-investment")).read_text())
        colors = {node["id"]: node["color"] for node in category["nodes"][1:]}
        self.assertEqual(len(set(colors.values())), 2)
        for topic in result["topic_ids"]:
            board = json.loads((self.vault / self.topic_paths(topic)[1]).read_text())
            self.assertTrue(all(node["color"] == colors[topic] for node in board["nodes"]
                                if node["id"].startswith("e-")))

    def test_cross_card_recall_prioritizes_relevant_late_part(self):
        alpha, beta, background = ("a" * 64, "b" * 64, "c" * 64)
        sources = {
            alpha: ("六维力传感器量产标定", "甲厂量产线使用六维力传感器标定工装。",
                    ["开场说明", "资料介绍", "例行公告", "项目日程",
                     "甲厂六维力传感器量产标定工装已上线。"]),
            beta: ("六维力传感器标定节拍", "乙厂对六维力传感器量产标定节拍开展分析。",
                   ["例行公告", "行业背景", "乙厂六维力传感器量产标定环节影响生产节拍。"]),
            background: ("交易市场", "股票成交量变化。", ["例行公告", "成交额披露。"]),
        }

        def plan(_vault, sha, *, source):
            title, summary, parts = sources[sha]
            return {"plan_sha256": sha,
                    "groups": [{"start": 1, "end": len(parts), "title": title, "summary": summary}]}

        def evidence(_vault, sha):
            title, _, parts = sources[sha]
            return None, {"title": title, "parts": [{"text": text} for text in parts]}

        with patch.object(knowledge.semantic, "reviewed_plan", side_effect=plan), \
                patch.object(knowledge, "evidence_for", side_effect=evidence):
            articles, pointers, pairs = knowledge._article_payload(self.vault, sources)
        self.assertEqual([pair["cards"] for pair in pairs], [[[alpha, 1], [beta, 1]]])
        self.assertEqual(articles[0]["cards"][0]["evidence"][0]["number"], 5)
        self.assertEqual(articles[1]["cards"][0]["evidence"][0]["number"], 3)
        self.assertIn("标定工装已上线", pointers[alpha, 1, 5][1])

    def test_second_round_can_recover_valid_topic_from_empty_proposal(self):
        first, second = self.evidence()
        def runner(instructions, payload, _schema):
            if instructions.startswith(knowledge.PROPOSE):
                return {"topics": []}
            self.assertEqual(payload["draft"], {"topics": []})
            self.assertIn("cross_card_candidates", payload)
            return self.proposal(first, second)

        result = knowledge.run(self.vault, [first, second], self.policy, runner)
        self.assertEqual((result["topics"], result["added_events"]), (1, 2))
