import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from afr_agent import dashboard, workflow


class DashboardTests(unittest.TestCase):
    def test_late_article_and_last_published_topic_remain_visible_during_next_batch(self):
        with TemporaryDirectory() as temp:
            vault = Path(temp)
            first, late = "a" * 64, "b" * 64
            topic = "k-" + "c" * 24
            event = "e-" + "d" * 24
            db = workflow.database(vault)
            self.addCleanup(db.close)
            knowledge = vault / "知识总结"
            knowledge.mkdir()
            topic_note, _ = dashboard.knowledge._topic_paths(
                topic, {topic: "待归类主题/测试对象 · 部署"})
            note = vault / topic_note
            note.parent.mkdir(parents=True)
            note.write_text(f"原记录 ^{event}", encoding="utf-8")
            state_dir = vault / ".afr/knowledge-summary"
            state_dir.mkdir()
            (state_dir / "ledger.json").write_text(json.dumps({
                "contract": "knowledge-summary-v1", "batches": {}, "projections": {},
                "topics": {topic: {"object": "测试对象", "dimension": "部署",
                                   "events": [{"id": event, "sha256": first, "kind": "事实",
                                               "headline": "开始部署", "source_path": "原文.md#^e1"}]}},
            }), encoding="utf-8")
            (vault / "原文.md").write_text("测试出处 ^e1", encoding="utf-8")
            with db:
                for sha, folder in [(first, "测试甲"), (late, "测试乙")]:
                    article = vault / folder
                    article.mkdir()
                    (article / "文章白板.canvas").touch()
                    db.execute("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES(?,?,100,'ready')",
                               (sha, folder))
                db.execute("INSERT INTO knowledge_batches(scope,state,result) VALUES('finished','ready',?)",
                           (json.dumps({"new_versions": [first], "topic_ids": [topic], "event_ids": [event]}),))
                db.executemany("INSERT INTO workflow_events(scope,kind,at) VALUES(?,?,?)",
                               [(first, "article_signed", 110), ("finished", "knowledge_finished", 120),
                                (late, "article_signed", 125)])
            dashboard.refresh(vault)
            text = (vault / dashboard.HOME).read_text(encoding="utf-8")
            self.assertIn("测试乙/文章白板.canvas", text)
            self.assertIn("上一批的主题变化", text)
            self.assertIn(f"{topic_note}#^{event}", text)
            with db:
                db.execute("INSERT INTO knowledge_batches(scope,state,result) VALUES('next','working',?)",
                           (json.dumps({"new_versions": [late]}),))
            dashboard.refresh(vault)
            text = (vault / dashboard.HOME).read_text(encoding="utf-8")
            self.assertIn("正在整理知识总结", text)
            self.assertIn("测试乙/文章白板.canvas", text)
            self.assertIn(f"{topic_note}#^{event}", text)
