import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from afr_agent import knowledge
from afr_agent.__main__ import main
from afr_agent.tests import test_knowledge


class RefreshSelectionTests(unittest.TestCase):
    def test_cli_renders_only_the_requested_topic_and_uses_zero_model_calls(self):
        fixture = test_knowledge.KnowledgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        a, b = fixture.evidence()
        draft = fixture.proposal(a, b)
        item, = draft["topics"]
        first, second = item["events"]
        draft["topics"] = [
            {**item, "object": "甲公司", "events": [first]},
            {**item, "object": "乙公司", "events": [second]},
        ]
        result = knowledge.run(fixture.vault, [a, b], fixture.policy, lambda *_: draft)
        selected, untouched = result["topic_ids"]
        local_files = [fixture.vault / path for path in fixture.topic_paths(untouched)]
        before = [(file.read_bytes(), file.stat().st_mtime_ns) for file in local_files]
        output = io.StringIO()
        command = ["afr_agent", "refresh-views", "--vault", str(fixture.vault), "--topic", selected]
        with patch("sys.argv", command), redirect_stdout(output), \
                patch.object(knowledge, "run_structured", side_effect=AssertionError("unexpected model")), \
                patch.object(knowledge, "_render_topic", wraps=knowledge._render_topic) as render:
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "refreshed")
        self.assertEqual([call.args[0] for call in render.call_args_list], [selected])
        self.assertEqual([(file.read_bytes(), file.stat().st_mtime_ns) for file in local_files], before)
        self.assertNotIn("AFR:", (fixture.vault / fixture.topic_paths(selected)[0]).read_text())
        body = (fixture.vault / knowledge._category_path("robot-investment")).read_text()
        self.assertIn(selected, body)
        self.assertIn(untouched, body)
        with self.assertRaisesRegex(knowledge.RequestError, "unknown_knowledge_topic"):
            knowledge.refresh_views(fixture.vault, force=True, selected={"k-unknown"})


class KnowledgePathMigrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_knowledge.KnowledgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.vault = self.fixture.vault
        self.ledger = self.vault / knowledge.STATE / "ledger.json"

    def publish_legacy_paths(self):
        a, b = self.fixture.evidence()
        proposal = self.fixture.proposal(a, b)
        topic = proposal["topics"][0]
        topic["reading_branches"] = [
            {"title": "项目预算审批", "reason": "审批记录可核对授权金额。",
             "arguments": [{"event_number": 1, "title": "十亿元获批",
                            "reason": "甲公司披露审批额度。", "scope": "审批阶段"}]},
            {"title": "实际付款进度", "reason": "跟踪已发生的首笔付款。",
             "arguments": [{"event_number": 2, "title": "两亿元首款",
                            "reason": "乙公司披露首付款。", "scope": "支付阶段"}]},
        ]
        # 重现旧投影的编号式路径，同时沿用真实发布入口和投影账本。
        def legacy_branch_ids(topic):
            return {branch["id"]: branch["id"] for branch in topic.get("reading_hierarchy", {}).get("branches", ())}

        with patch.object(knowledge, "_topic_path_map", lambda topics: {name: name for name in topics}), \
                patch.object(knowledge, "_branch_labels", side_effect=legacy_branch_ids):
            result = knowledge.run(self.vault, [a, b], self.fixture.policy, lambda *_: proposal)
        topic_id, = result["topic_ids"]
        state = knowledge._state(self.ledger)
        state["view_revision"] = knowledge.VIEW_REVISION - 1
        state["topics"][topic_id].pop("branch_labels", None)
        knowledge._save(self.ledger, state)
        return topic_id

    def test_migration_resumes_after_paths_move_and_preserves_manual_note(self):
        topic_id = self.publish_legacy_paths()
        old_note, old_canvas = knowledge._topic_paths(topic_id)
        note, canvas = self.fixture.topic_paths(topic_id)
        old_path = self.vault / old_note
        prefix, suffix = "开头手写区\r\n\r\n".encode(), "补充手写内容\r\n".encode()
        old_path.write_bytes(prefix + old_path.read_bytes() + suffix)
        moves = knowledge._legacy_projection_moves(knowledge._state(self.ledger))
        self.assertIn(canvas, moves.values())
        self.assertTrue(any("/论述/" in path for path in moves.values()))

        with patch.object(knowledge, "_projections", side_effect=InterruptedError("interrupted after move")), \
                self.assertRaises(InterruptedError):
            knowledge.refresh_views(self.vault)

        self.assertTrue(knowledge._state(self.ledger)["topic_view_pending"])
        self.assertFalse((self.vault / old_canvas).exists())
        self.assertIn(old_note.replace(".md", ".canvas"), (self.vault / knowledge.INDEX).read_text())
        self.assertEqual(knowledge.refresh_views(self.vault), [])
        self.assertFalse(knowledge._state(self.ledger).get("topic_view_pending"))
        self.assertFalse((self.vault / old_canvas.removesuffix(".canvas")).exists())
        self.assertIn(canvas, (self.vault / knowledge.INDEX).read_text())
        self.assertNotIn(topic_id, (self.vault / knowledge.INDEX).read_text())
        published = (self.vault / note).read_bytes()
        self.assertTrue(published.startswith(prefix))
        self.assertTrue(published.endswith(suffix))
        self.assertIn(canvas.encode(), published)
        self.assertIsNone(knowledge.refresh_views(self.vault))

    def test_existing_destination_stops_migration_and_keeps_both_files(self):
        topic_id = self.publish_legacy_paths()
        old_note, _ = knowledge._topic_paths(topic_id)
        note, _ = self.fixture.topic_paths(topic_id)
        old = (self.vault / old_note).read_bytes()
        (self.vault / note).parent.mkdir(parents=True)
        (self.vault / note).write_text("用户已有笔记")
        self.assertEqual(knowledge.refresh_views(self.vault), [note])
        self.assertEqual((self.vault / old_note).read_bytes(), old)
        self.assertEqual((self.vault / note).read_text(), "用户已有笔记")
        self.assertIn(old_note.replace(".md", ".canvas"), (self.vault / knowledge.INDEX).read_text())

    def test_filesystem_equivalent_topic_titles_have_distinct_paths(self):
        topics = {
            "k-first": {"object": "A/B", "dimension": "部署", "color_index": 0},
            "k-second": {"object": "a／b", "dimension": "部署", "color_index": 1},
        }
        names = knowledge._topic_path_map(topics)
        self.assertEqual(names, {"k-first": "待归类主题/A／B · 部署", "k-second": "待归类主题/a／b · 部署（2）"})
        self.assertEqual(names, knowledge._topic_path_map(dict(reversed(list(topics.items())))))
