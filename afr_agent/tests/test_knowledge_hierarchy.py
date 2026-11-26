import json
import unittest

from afr_agent import knowledge, knowledge_hierarchy
from afr_agent.tests import test_knowledge


def branches():
    return [
        {"title": "审批记录显示项目投入的授权", "reason": "审批上限衡量企业能承诺的最高预算。",
         "arguments": [{"event_number": 1, "title": "十亿元额度提供预算基线",
                        "reason": "甲公司披露机器人的项目预算审批金额为十亿元。",
                        "scope": "对应审批阶段"}]},
        {"title": "付款记录显示已经发生的资金动作", "reason": "首期付款是可以观察的合同执行动作。",
         "arguments": [{"event_number": 2, "title": "乙公司首款两亿元",
                        "reason": "乙公司的两亿元属于已支付首期款，可继续追踪后续款项。",
                        "scope": "对应首期款"}]},
    ]


class NativeHierarchyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_knowledge.KnowledgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.vault = self.fixture.vault
        self.policy = self.fixture.policy

    def board(self, relative):
        return json.loads((self.vault / relative).read_text())

    def test_one_question_board_contains_branches_full_history_and_revision(self):
        first, second = self.fixture.evidence()
        proposal = self.fixture.proposal(first, second)
        proposal["topics"][0]["reading_branches"] = branches()

        result = knowledge.run(self.vault, [first, second], self.policy, lambda *_: proposal)
        topic_id, = result["topic_ids"]
        page, canvas = self.fixture.topic_paths(topic_id)
        full_canvas = knowledge._full_timeline_path(canvas)
        overview = self.board(canvas)["nodes"]
        self.assertFalse((self.vault / full_canvas).exists())
        self.assertEqual(sum(n["id"].startswith("branch-") for n in overview), 2)
        self.assertEqual(sum(n["id"].startswith("e-") for n in overview), 2)
        self.assertTrue(all("/论述/" in n["text"] for n in overview if n["id"].startswith("branch-")))
        ledger = self.vault / knowledge.STATE / "ledger.json"
        published = knowledge._state(ledger)["topics"][topic_id]

        third, _ = self.fixture.fixture.article(
            "甲公司机器人审批更正", "# 甲公司机器人审批更正\n\n甲公司修订机器人项目预算为八亿元。")
        self.fixture.approve(first, second, third)
        prior_event = published["events"][0]
        update = {"topics": [{"topic_id": topic_id, "object": "机器人", "dimension": "技术投入",
                              "events": [self.fixture.event(third, origin="甲公司", stage="审批", kind="更正",
                                                            supersedes=prior_event["id"],
                                                            quote="甲公司修订机器人项目预算为八亿元。")]}]}
        update["topics"][0]["reading_branches"] = []
        update_result = knowledge.run(
            self.vault, [first, second, third], self.policy, lambda *_: update, new_version_ids=[third])
        self.assertEqual(update_result["added_events"], 1)
        updated = self.board(canvas)["nodes"]
        self.assertEqual(sum(n["id"].startswith("branch-") for n in updated), 1)
        self.assertEqual(sum(n["id"].startswith("e-") for n in updated), 3)
        self.assertIn("被修订的记录", (self.vault / page).read_text())
        self.assertFalse((self.vault / full_canvas).exists())

    def test_single_new_record_keeps_existing_branches_locally_and_replay_is_idempotent(self):
        first, second = self.fixture.evidence()
        proposal = self.fixture.proposal(first, second)
        proposal["topics"][0]["reading_branches"] = branches()
        initial = knowledge.run(self.vault, [first, second], self.policy, lambda *_: proposal)
        topic_id, = initial["topic_ids"]
        page, canvas = self.fixture.topic_paths(topic_id)
        original = self.board(canvas)["nodes"]
        third, _ = self.fixture.fixture.article(
            "丙公司机器人项目公告", "# 丙公司机器人项目公告\n\n丙公司机器人项目已开始建设。")
        self.fixture.approve(third)

        def runner(_instructions, payload, _schema):
            self.assertEqual(payload["existing_topics"], [])
            self.assertEqual([article["sha256"] for article in payload["articles"]], [third])
            return {"topics": [{"topic_id": "", "object": "机器人", "dimension": "技术投入",
                               "events": [self.fixture.event(third, origin="丙公司", stage="实际建设",
                                                             quote="丙公司机器人项目已开始建设。")],
                               "reading_branches": []}]}

        update = knowledge.run(self.vault, [third], self.policy, runner)
        self.assertEqual(update["topic_ids"], [topic_id])
        self.assertEqual(update["added_events"], 1)
        actual = self.board(canvas)["nodes"]
        self.assertEqual([n for n in original if n["id"].startswith("branch-")],
                         [n for n in actual if n["id"].startswith("branch-")])
        self.assertEqual(sum(n["id"].startswith("e-") for n in actual), 3)
        self.assertIn("丙公司机器人项目已开始建设", (self.vault / page).read_text())
        replay = knowledge.run(self.vault, [third], self.policy, lambda *_: self.fail("replayed model"))
        self.assertEqual(replay["added_events"], 0)
        self.assertEqual(self.board(canvas)["nodes"], actual)

    def test_unadmitted_withdrawn_and_repeated_events_cannot_support_a_branch(self):
        events = [{"id": "first"}, {"id": "second"}]
        parsed = knowledge_hierarchy.candidate(branches(), events, knowledge._normalized)
        self.assertEqual([branch["arguments"][0]["event"] for branch in parsed], ["first", "second"])
        first = {"id": "first", "kind": "事实", "supersedes": ""}
        second = {"id": "second", "kind": "撤回", "supersedes": ""}
        for history in ([first], [first, second]):
            self.assertIsNone(knowledge_hierarchy.admitted(
                "标题", "概述", parsed, history, {"first"}, knowledge._normalized))
        repeated = branches()
        repeated[1]["arguments"][0]["event_number"] = 1
        parsed = knowledge_hierarchy.candidate(repeated, events, knowledge._normalized)
        self.assertIsNone(knowledge_hierarchy.admitted(
            "标题", "概述", parsed, [first], {"first"}, knowledge._normalized))

    def test_new_branch_selection_preserves_omitted_existing_branches(self):
        event = lambda identifier: {"id": identifier, "kind": "事实", "supersedes": ""}
        history = [event("first"), event("second")]
        prior = knowledge_hierarchy.admitted(
            "标题", "概述", knowledge_hierarchy.candidate(branches(), history, knowledge._normalized), history,
            {"first", "second"}, knowledge._normalized)
        third = event("third")
        incoming = [{"title": "建设记录显示进入施工阶段", "reason": "开工信息与在建工程可作交叉核对。",
                     "arguments": [{"event_number": 1, "title": "丙公司已开始建设",
                                    "reason": "丙公司正式披露项目已经开工。", "scope": ""}]}]
        merged = knowledge_hierarchy.admitted(
            "标题", "概述", knowledge_hierarchy.candidate(incoming, [third], knowledge._normalized),
            [*history, third], {"third"}, knowledge._normalized, prior=prior)
        self.assertEqual(merged["branches"][:2], prior["branches"])
        self.assertEqual(merged["branches"][2]["arguments"][0]["event"], "third")

    def test_partly_corrected_branch_title_uses_the_remaining_argument(self):
        initial = [{"id": "a", "title": "项目预算十亿元", "reason": "原审批总金额十亿元。",
                    "arguments": [{"event": "first", "title": "十亿元属于审批额度", "reason": "原披露金额。"},
                                  {"event": "second", "title": "项目仍在等待分期拨款", "reason": "拨款未有披露。"}]}]
        history = [{"id": "first", "kind": "事实", "supersedes": ""},
                   {"id": "second", "kind": "事实", "supersedes": ""},
                   {"id": "third", "kind": "更正", "supersedes": "first"}]
        hierarchy = knowledge_hierarchy.admitted(
            "标题", "概述", [], history, {"third"}, knowledge._normalized, prior={"branches": initial})
        branch, = hierarchy["branches"]
        self.assertEqual(branch["id"], "a")
        self.assertEqual(branch["title"], "项目仍在等待分期拨款")
        self.assertEqual(branch["reason"], "拨款未有披露。")

    def test_many_events_alone_do_not_create_a_second_reading_level(self):
        events = [{
            "id": f"record-{index}", "kind": "事实", "stage": "其他", "supersedes": "",
            "origin": "甲公司", "article_title": "季度披露", "claim": "按期更新项目进展",
            "quote": "按期更新项目进展", "source_path": "固定原文.md#^e2", "card_path": "篇内卡.md",
            "date": "", "published_at": None, "admitted_at": None, "recorded_at": "2026-11-25",
        } for index in range(knowledge.LONG_TIMELINE + 1)]
        topic = {"object": "甲公司", "dimension": "材料更新", "events": events, "color_index": 0,
                 "native_reading": True}
        page, canvas = knowledge._topic_paths("k-test")
        projections = knowledge._render_topic("k-test", topic, {})
        paths = knowledge._argument_paths(canvas, events)
        self.assertEqual(len(set(paths.values())), len(events))
        self.assertEqual(set(projections), {page, canvas, *paths.values()})
        first_detail = paths[events[0]["id"]]
        self.assertIn(f"[[{first_detail}|按期更新项目进展]]", projections[page].decode())
        self.assertIn(f"[[{first_detail}|按期更新项目进展]]",
                      next(node["text"] for node in json.loads(projections[canvas])["nodes"] if node["id"] == "root"))
        detail = json.loads(projections[first_detail])
        self.assertEqual(len({node["id"] for node in detail["nodes"]}), len(detail["nodes"]))
        self.assertIn(f"[[{canvas}|回到问题白板]]", detail["nodes"][-1]["text"])
        self.assertIn("[[固定原文.md#^e2|固定原文]]", detail["nodes"][1]["text"])
        board = json.loads(projections[canvas])
        cards = [n for n in board["nodes"] if n["id"].startswith("record-")]
        self.assertEqual([n["id"] for n in cards], [event["id"] for event in events])
        self.assertEqual([n["y"] for n in cards], sorted(n["y"] for n in cards))
        self.assertGreater(cards[1]["x"], cards[0]["x"] + cards[0]["width"])
        self.assertGreater(cards[2]["y"], max(n["y"] + n["height"] for n in cards[:2]))
        self.assertTrue(any(n.get("label", "").startswith("材料时间待核") for n in board["nodes"]))

    def test_argument_detail_preserves_signed_source_scope_and_stable_filenames(self):
        _, second = self.fixture.evidence()
        first, _ = self.fixture.fixture.article(
            "甲公司机器人投入与条件", "# 甲公司机器人投入与条件\n\n"
            "甲公司审批机器人项目预算十亿元。只有主要厂房验收后才会启动采购。")
        self.fixture.approve(first, second)
        proposal = self.fixture.proposal(first, second)
        proposal["topics"][0]["reading_branches"] = branches()
        result = knowledge.run(self.vault, [first, second], self.policy, lambda *_: proposal)
        topic_id, = result["topic_ids"]
        topics = knowledge._state(self.vault / knowledge.STATE / "ledger.json")["topics"]
        topic = topics[topic_id]
        page, canvas = knowledge._topic_paths(topic_id, knowledge._topic_path_map(topics))
        paths = knowledge._argument_paths(canvas, topic["events"])
        event = next(item for item in topic["events"] if item["origin"] == "甲公司")
        branch = topic["reading_hierarchy"]["branches"][0]
        self.assertEqual(branch["arguments"][0]["event"], event["id"])
        detail_path = paths[event["id"]]
        self.assertTrue(detail_path.endswith("/论述/甲公司审批机器人项目预算十亿元.canvas"))
        detail = self.board(detail_path)
        nodes = detail["nodes"]
        body = "\n".join(node["text"] for node in nodes)
        self.assertEqual({node["x"] for node in nodes}, {0})
        self.assertTrue(all(a["y"] + a["height"] < b["y"] for a, b in zip(nodes, nodes[1:])))
        self.assertIn("甲公司审批机器人项目预算十亿元。", body)
        self.assertIn("甲公司", nodes[0]["text"])
        self.assertIn("对应审批阶段", body)
        self.assertIn(f"[[{canvas}|回到问题白板]]", body)
        self.assertIn("固定原文.md#^e2|原文第 2 段", body)
        self.assertIn("只有主要厂房验收后才会启动采购。", body)
        branch_card = next(node["text"] for node in self.board(canvas)["nodes"]
                           if node["id"] == f"branch-{branch['id']}")
        self.assertIn(f"[[{detail_path}|十亿元额度提供预算基线]]", branch_card)
        self.assertIn(detail_path, (self.vault / page).read_text())
        category = self.board(knowledge._category_path("robot-investment"))
        self.assertIn(detail_path, category["nodes"][1]["text"])

        unsafe = {**event, "claim": "占比/路径#片段[" + "长" * 90}
        duplicate = {**unsafe, "id": "e-second"}
        previous = knowledge._argument_paths(canvas, [unsafe])
        after = knowledge._argument_paths(canvas, [{**unsafe, "headline": "新版标题"}, duplicate])
        self.assertEqual(previous[event["id"]], after[event["id"]])
        self.assertNotEqual(after[event["id"]], after["e-second"])
        filename = after["e-second"].rsplit("/", 1)[-1]
        self.assertLess(len(filename.encode()), 255)
        self.assertNotIn("#", filename)
        self.assertNotIn("[", filename)
