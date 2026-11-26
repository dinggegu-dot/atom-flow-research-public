import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from afr_agent import article_argument_boards, intake, locations, questions, semantic
from afr_agent.library import RequestError
from afr_agent.presentation import evidence_for, reading_markdown


class QuestionTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name) / "vault"
        self.vault.mkdir()
        self.inbox = Path(temp.name) / "inbox"
        self.inbox.mkdir()
        self.policy = Path(temp.name) / "policy.json"

    def article(self, name, text, *, publish=True, viewpoints=False):
        (self.inbox / f"{name}.md").write_text(text)
        item, = intake.scan(self.vault, self.inbox)["added"]
        sha = item["sha256"]
        folder = self.vault / item["folder"]
        if publish:
            self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [sha]}))
            self.policy.chmod(0o600)

            def structured(instructions, payload, schema):
                if schema == semantic.REVIEW_SCHEMA:
                    return {"passed": True, "issues": []}
                groups = [{
                    "start": part["n"], "end": part["n"], "title": f"话题 {part['n']}",
                    "branch": "机柜算例", "summary": part["text"], "argument": part["text"],
                } for part in payload["evidence"]]
                if viewpoints:
                    for group in groups:
                        group["viewpoints"] = [{
                            "judgment": group["summary"], "speaker": "作者", "kind": "reported_fact",
                            "grounds": [], "conditions": [],
                            "evidence": [{"number": group["start"], "quote": group["summary"]}],
                        }]
                return {"groups": groups}

            with patch.object(semantic.codex, "run_structured", side_effect=structured):
                self.assertEqual(semantic.process(self.vault, sha, self.policy)["state"], "published")
        return sha, folder

    def proposed(self, title="哪些统计口径会改变出租率？", topic_id=1, relation="SUPPORTS"):
        return dict(new_question=title, topic_id=topic_id, relation=relation, reason="材料明示适用条件")

    def attach(self, identifier, topic_id=1, relation="CONTRADICTS"):
        item = self.proposed(topic_id=topic_id, relation=relation)
        item.pop("new_question")
        return dict(item, question_id=identifier)

    def test_archive_only_unedited_batch_and_restore_navigation(self):
        sha, _ = self.article("甲", "明确出处与统计口径。")
        other, _ = self.article("乙", "另一批材料。")
        archived = questions.apply(self.vault, sha, [self.proposed("草稿？")],
                                   confirm_new=True)["questions"][0]
        edited = questions.apply(self.vault, sha, [self.proposed("手改？")],
                                  confirm_new=True)["questions"][0]
        outside = questions.apply(self.vault, other, [self.proposed("独立问题？")],
                                   confirm_new=True)["questions"][0]
        edited_path = self.vault / edited["path"]
        edited_path.write_bytes(edited_path.read_bytes() + "保留手改".encode())
        before = (self.vault / archived["path"]).read_bytes()
        result = questions.archive_unedited_batch(self.vault, [sha])
        self.assertEqual((result["archived"], result["skipped"]), (1, 1))
        index = self.vault / questions.INDEX
        path = self.vault / result["archive_path"]
        self.assertIn(archived["path"], path.read_text())
        self.assertNotIn(archived["path"], index.read_text())
        self.assertEqual({entry["id"] for entry in questions.catalog(self.vault)}, {edited["id"], outside["id"]})
        self.assertFalse(questions.search(self.vault, "草稿", 1)["hits"])
        self.assertEqual(questions.read(self.vault, archived["id"], 1)["title"], "草稿？")
        path.write_bytes(path.read_bytes() + "保留清单手改".encode())
        self.assertEqual(questions.archive_unedited_batch(self.vault, [sha]), result)
        self.assertTrue(path.read_text().endswith("保留清单手改"))
        self.assertEqual(questions.restore_archived_batch(self.vault, [sha]), {"restored": 1})
        self.assertIn(archived["path"], index.read_text())
        self.assertEqual((self.vault / archived["path"]).read_bytes(), before)

    def test_index_conflict_prevents_batch_archive(self):
        sha, _ = self.article("甲", "明确出处。")
        saved = questions.apply(self.vault, sha, [self.proposed()], confirm_new=True)["questions"][0]
        index = self.vault / questions.INDEX
        index.write_text(index.read_text().replace("# 研究问题索引", "# 用户改过的索引"))
        with self.assertRaisesRegex(RequestError, "user_edit_conflict"):
            questions.archive_unedited_batch(self.vault, [sha])
        self.assertEqual(questions.catalog(self.vault)[0]["id"], saved["id"])
        self.assertFalse((self.vault / questions.ARCHIVE).exists())

    def test_topic_requires_approved_publication_and_exact_current_anchor(self):
        sha, folder = self.article("甲", "五台出租。\n\n分母为可用台数。", publish=False)
        with self.assertRaisesRegex(RequestError, "review_required"):
            questions.read_topic(self.vault, sha, 1)
        sha, folder = self.article("乙", "十台可用。\n\n分子为五台。")
        topic = questions.read_topic(self.vault, sha, 2)
        self.assertEqual(topic["text"], "分子为五台。")
        self.assertEqual(topic["citations"][0]["text"], "分子为五台。")
        self.assertEqual(topic["citations"][0]["locator"]["offset"], len("十台可用。\n\n"))
        self.assertTrue(topic["path"].endswith("完整阅读.md#^topic-2"))
        with self.assertRaisesRegex(RequestError, "not_found"):
            questions.read_topic(self.vault, sha, 3)
        reading = folder / "完整阅读.md"
        reading.write_text(reading.read_text().replace("分子为五台。", "分子为六台。"))
        with self.assertRaisesRegex(RequestError, "article_publication_changed"):
            questions.read_topic(self.vault, sha, 2)
        self.assertEqual(questions.read_topic(self.vault, sha, 1)["text"], "十台可用。")

    def test_signed_viewpoint_links_stay_verifiable_after_article_relocation(self):
        with patch.object(locations, "archive_folder", side_effect=lambda _, name: f"资料库/新收研报/{name}"):
            sha, folder = self.article("机柜观察", "十台可用。\n\n五台出租。", viewpoints=True)
        before = questions.read_topic(self.vault, sha, 2)
        old = folder.relative_to(self.vault).as_posix()
        previous = f"资料库/科技与产业/{folder.name}"
        new = f"资料档案/科技与产业/机柜供需观察 · {sha[:16]}"
        relocated = self.vault / new
        relocated.parent.mkdir(parents=True)
        folder.rename(relocated)
        mapping = {old: previous, previous: new}
        (self.vault / locations.MANIFEST).write_text(json.dumps({"version": 1, "folders": mapping}))
        for path in relocated.rglob("*"):
            if path.suffix in {".md", ".canvas"} and path.name != "原件.md":
                path.write_text(locations.rewrite(path.read_text(), mapping))

        with sqlite3.connect(self.vault / ".afr/intake.sqlite") as db:
            self.assertEqual(db.execute("SELECT folder FROM articles WHERE sha = ?", (sha,)).fetchone()[0], old)
        rescanned = intake.scan(self.vault, self.inbox)
        self.assertEqual(rescanned["errors"], [])
        self.assertEqual(rescanned["added"], [])
        self.assertEqual(rescanned["versions"], [{"sha256": sha, "folder": new, "readable": True}])
        self.assertFalse(folder.exists())
        self.assertEqual(locations.resolve(f"{new}/论述白板/话题.canvas", mapping, reverse=True),
                         f"{old}/论述白板/话题.canvas")

        after = questions.read_topic(self.vault, sha, 2)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["path"], f"{new}/完整阅读.md#^topic-2")
        self.assertEqual(after["citations"][0]["path"], f"{new}/固定原文.md#^e2")
        reading = relocated / "完整阅读.md"
        self.assertIn(f"{new}/文章白板.canvas|返回文章白板", reading.read_text())
        reading.write_text(reading.read_text().replace(f"{new}/固定原文.md#^e2", f"{new}/固定原文.md#^e1"))
        with self.assertRaisesRegex(RequestError, "article_publication_changed"):
            questions.read_topic(self.vault, sha, 2)

    def test_topic_revision_survives_reflow_and_duplicate_anchor_is_rejected(self):
        sha, folder = self.article("重排", "十台可用。\n\n五台出租。")
        reading = folder / "完整阅读.md"
        chapters = reading.read_text()
        _, evidence = evidence_for(self.vault, sha)
        plan = semantic.reviewed_plan(self.vault, sha)
        reading.write_text(reading_markdown(evidence, plan["groups"], include_source_text=True))

        before = questions.read_topic(self.vault, sha, 2)
        reading.write_text(chapters)
        self.assertEqual(questions.read_topic(self.vault, sha, 2), before)

        reading.write_text(chapters + "\n重复的锚点 ^topic-2\n")
        with self.assertRaisesRegex(RequestError, "article_publication_changed"):
            questions.read_topic(self.vault, sha, 2)
        self.assertEqual(questions.read_topic(self.vault, sha, 1)["text"], "十台可用。")

    def test_topic_requires_unique_signed_canvas_text_but_allows_relayout(self):
        sha, folder = self.article("白板手改", "十台可用。")
        before = questions.read_topic(self.vault, sha, 1)
        canvas = folder / "文章白板.canvas"
        saved = json.loads(canvas.read_text())
        node = next(node for node in saved["nodes"] if node["id"] == "t1")
        node.update(x=node["x"] + 80, color="5")
        canvas.write_text(json.dumps(saved))
        self.assertEqual(questions.read_topic(self.vault, sha, 1), before)

        saved["nodes"].append(dict(node))
        canvas.write_text(json.dumps(saved))
        with self.assertRaisesRegex(RequestError, "article_publication_changed"):
            questions.read_topic(self.vault, sha, 1)
        saved["nodes"].pop()
        node["text"] = node["text"].replace("十台可用。", "一台可用。")
        canvas.write_text(json.dumps(saved))
        with self.assertRaisesRegex(RequestError, "article_publication_changed"):
            questions.read_topic(self.vault, sha, 1)

    def test_missing_linked_argument_invalidates_the_article_topic(self):
        sha, folder = self.article("论述入口", "十台可用。")
        canvas = json.loads((folder / "文章白板.canvas").read_text())
        card = next(node["text"] for node in canvas["nodes"] if node["id"] == "t1")
        detail = folder / article_argument_boards.detail_relative(card)
        detail.unlink()
        with self.assertRaisesRegex(RequestError, "article_publication_changed"):
            questions.read_topic(self.vault, sha, 1)

    def test_argument_title_with_literal_brackets_produces_safe_detail_link(self):
        card = "## [[档案/完整阅读.md#^topic-1|条件 " + intake.plain("[限额]|项目") + "]]"
        changed = article_argument_boards.linked_text(card)
        self.assertEqual(article_argument_boards.detail_relative(changed),
                         "./篇内论述/条件  限额  项目 · 1.canvas")
        self.assertIn(intake.plain("[限额]|项目"), changed)

    def test_reviewed_alias_preserves_legacy_files_and_rejects_escaping_path(self):
        sha, folder = self.article("旧稿", "十台可用。\n\n五台出租。")
        before = questions.read_topic(self.vault, sha, 2)
        relative = folder.relative_to(self.vault).as_posix()
        reading = folder / "完整阅读.md"
        canvas = folder / "文章白板.canvas"
        reviewed_reading = folder / "完整阅读（本轮校读）.md"
        reviewed_canvas = folder / "文章白板（本轮校读）.canvas"
        reviewed_reading.write_text(reading.read_text().replace(canvas.name, reviewed_canvas.name))
        alias = json.loads(canvas.read_text())
        for node in alias["nodes"]:
            text = node.get("text", "")
            target = article_argument_boards.detail_relative(text)
            if target:
                number = node["id"].removeprefix("t")
                node["text"] = text.replace(target, f"{relative}/{reviewed_reading.name}#^topic-{number}")
        reviewed_canvas.write_text(json.dumps(alias, ensure_ascii=False))
        legacy_reading = "人工保存的旧阅读稿\n"
        legacy_canvas = '{"nodes":[],"edges":[]}'
        reading.write_text(legacy_reading)
        canvas.write_text(legacy_canvas)

        receipt = semantic.receipt(self.vault, sha, "publication")
        record = json.loads(receipt.read_text())
        record["paths"] = {"reading": f"{relative}/{reviewed_reading.name}",
                           "canvas": f"{relative}/{reviewed_canvas.name}"}
        receipt.write_text(json.dumps(record, ensure_ascii=False))
        after = questions.read_topic(self.vault, sha, 2)
        self.assertEqual(after["path"], f"{relative}/{reviewed_reading.name}#^topic-2")
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(article_argument_boards.migrate(self.vault), {"article_boards": 1, "arguments": 2})
        self.assertEqual(questions.read_topic(self.vault, sha, 2), after)
        migrated_canvas = json.loads(reviewed_canvas.read_text())
        node = next(item for item in migrated_canvas["nodes"] if item["id"] == "t2")
        target = folder / article_argument_boards.detail_relative(node["text"])
        self.assertEqual(target.parent.name, "篇内论述（本轮校读）")
        detail = json.loads(target.read_text())
        self.assertIn(f"../{reviewed_canvas.name}|返回文章白板", detail["nodes"][0]["text"])
        self.assertIn(f"../{reviewed_reading.name}#^topic-2|按原序读", detail["nodes"][0]["text"])
        self.assertIn("五台出租。", detail["nodes"][1]["text"])
        self.assertNotIn("本节原文", detail["nodes"][1]["text"])
        self.assertEqual(article_argument_boards.migrate(self.vault), {"article_boards": 0, "arguments": 0})
        self.assertEqual(semantic.process(self.vault, sha)["board"], record["paths"]["canvas"])
        self.assertEqual(reading.read_text(), legacy_reading)
        self.assertEqual(canvas.read_text(), legacy_canvas)

        record["paths"]["reading"] = "../完整阅读（本轮校读）.md"
        receipt.write_text(json.dumps(record, ensure_ascii=False))
        with self.assertRaisesRegex(RequestError, "invalid_review_receipt"):
            questions.read_topic(self.vault, sha, 2)

    def test_pending_cross_article_links_history_and_replay(self):
        first, _ = self.article("甲", "五台出租。\n\n分母为可用台数。")
        second, _ = self.article("乙", "采用额定总机柜为分母。")
        new = self.proposed(topic_id=2)
        pending = questions.apply(self.vault, first, [new])
        self.assertEqual(pending["pending"][0]["topic_id"], 2)
        self.assertEqual(questions.catalog(self.vault), [])
        apply = questions.apply(self.vault, first, [new], confirm_new=True)
        current, = apply["questions"]
        identifier = current["id"]
        self.assertEqual(questions.apply(self.vault, first, [new], confirm_new=True), apply)
        with self.assertRaisesRegex(RequestError, "existing_question_required"):
            questions.apply(self.vault, second, [self.proposed()], confirm_new=True)
        item = self.attach(identifier, relation="QUALIFIES")
        applied = questions.apply(self.vault, second, [item], expected_revisions={identifier: 1})
        self.assertEqual(questions.apply(self.vault, second, [item], expected_revisions={identifier: 1}), applied)
        self.assertEqual(questions.read(self.vault, identifier, 1)["links"][0]["sha256"], first)
        latest = questions.read(self.vault, identifier)
        self.assertEqual(latest["revision"], 2)
        self.assertEqual([(link["sha256"], link["topic_id"]) for link in latest["links"]], [(first, 2), (second, 1)])
        self.assertTrue(all(link["topic_revision"] for link in latest["links"]))
        self.assertEqual(questions.catalog(self.vault)[0]["links"], [
            {"sha256": first, "topic_id": 2}, {"sha256": second, "topic_id": 1}])
        projected = (self.vault / latest["path"]).read_text()
        self.assertIn("口径不同", projected)
        self.assertIn(latest["links"][0]["path"], projected)
        self.assertIn(latest["path"], (self.vault / questions.INDEX).read_text())
        self.assertEqual(len(questions.search(self.vault, "出租率", 1)["hits"]), 1)
        self.assertEqual(questions.search(self.vault, "出租率", 1, visible=lambda _: False)["hits"], [])

    def test_manual_edit_survives_updates_and_generation_conflict_stops(self):
        sha, _ = self.article("甲", "五台出租。")
        item, = questions.apply(self.vault, sha, [self.proposed()], confirm_new=True)["questions"]
        identifier, path = item["id"], self.vault / item["path"]
        path.write_text(path.read_text() + "在 Obsidian 手写：进一步核对。\n")
        save = questions.update_note(self.vault, identifier, 1, "讨论结论待证。")
        self.assertEqual(save, questions.update_note(self.vault, identifier, 1, "讨论结论待证。"))
        before = path.read_text()
        self.assertIn("在 Obsidian 手写：进一步核对。", before)
        self.assertIn("讨论结论待证。", before)
        self.assertEqual(questions.read(self.vault, identifier, 1)["note"], "")
        self.assertEqual(questions.read(self.vault, identifier, 2)["note"], "讨论结论待证。")
        altered = before.replace("支持 ·", "我的手改 ·")
        path.write_text(altered)
        with self.assertRaisesRegex(RequestError, "user_edit_conflict"):
            questions.update_note(self.vault, identifier, 2, "尝试覆盖")
        self.assertEqual(path.read_text(), altered)
        self.assertEqual(questions.read(self.vault, identifier)["revision"], 2)

    def test_transactional_batch_and_retry_after_projection_failure(self):
        sha, _ = self.article("甲", "五台出租。\n\n统计仅覆盖可用台数。")
        operations = [self.proposed(), self.proposed(topic_id=9)]
        with self.assertRaisesRegex(RequestError, "not_found"):
            questions.apply(self.vault, sha, operations, confirm_new=True)
        self.assertEqual(questions.catalog(self.vault), [])
        good = [self.proposed(), self.proposed(topic_id=2, relation="BACKGROUND")]
        with patch.object(questions, "_write_projection", side_effect=OSError("simulated interruption")):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                questions.apply(self.vault, sha, good, confirm_new=True)
        record, = questions.apply(self.vault, sha, good, confirm_new=True)["questions"]
        self.assertEqual(record["revision"], 1)
        self.assertTrue((self.vault / record["path"]).is_file())
        self.assertTrue((self.vault / questions.INDEX).is_file())

    def test_unrelated_mutation_repairs_pending_projection_and_preserves_manual_edit(self):
        sha, _ = self.article("待重放", "五台出租。")
        with patch.object(questions, "_write_projection", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                questions.apply(self.vault, sha, [self.proposed()], confirm_new=True)
        first, = questions.catalog(self.vault)
        path = self.vault / questions.read(self.vault, first["id"])["path"]
        self.assertFalse(path.exists())

        questions.apply(self.vault, sha, [self.proposed("另一问？")], confirm_new=True)
        self.assertTrue(path.is_file())
        self.assertIn(path.name, (self.vault / questions.INDEX).read_text())

        with patch.object(questions, "_write_projection", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                questions.update_note(self.vault, first["id"], 1, "需要核对。")
        before = path.read_text() + "追加的手写内容。\n"
        path.write_text(before)
        questions.apply(self.vault, sha, [self.proposed("第三问？")], confirm_new=True)
        self.assertIn("需要核对。", path.read_text())
        self.assertTrue(path.read_text().endswith("追加的手写内容。\n"))

    def test_pending_replay_preserves_conflicting_generated_region(self):
        sha, _ = self.article("中断手改", "五台出租。")
        item, = questions.apply(self.vault, sha, [self.proposed()], confirm_new=True)["questions"]
        path = self.vault / item["path"]
        with patch.object(questions, "_write_projection", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                questions.update_note(self.vault, item["id"], 1, "需要核对。")

        manual = path.read_text().replace("支持 ·", "手改 ·")
        path.write_text(manual)
        with self.assertRaisesRegex(RequestError, "user_edit_conflict"):
            questions.apply(self.vault, sha, [self.proposed("另一问？")], confirm_new=True)
        self.assertEqual(path.read_text(), manual)
        self.assertEqual(len(questions.catalog(self.vault)), 1)

    def test_legacy_queue_upgrade_distinguishes_known_interruption_from_missing_published_note(self):
        sha, _ = self.article("旧库恢复", "五台出租。")
        questions.apply(self.vault, sha, [self.proposed("已有问题？")], confirm_new=True)
        proposal = [self.proposed("等待恢复？")]
        with patch.object(questions, "_write_projection", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                questions.apply(self.vault, sha, proposal, confirm_new=True)
        db_path = self.vault / ".afr/questions.sqlite"
        with sqlite3.connect(db_path) as db:
            db.execute("DROP TABLE pending_projections")

        recovered, = questions.apply(self.vault, sha, proposal, confirm_new=True)["questions"]
        path = self.vault / recovered["path"]
        self.assertTrue(path.is_file())
        self.assertIn(recovered["path"], (self.vault / questions.INDEX).read_text())

        path.unlink()
        with sqlite3.connect(db_path) as db:
            db.execute("DROP TABLE pending_projections")
        with self.assertRaisesRegex(RequestError, "user_edit_conflict"):
            questions.apply(self.vault, sha, proposal, confirm_new=True)
        self.assertFalse(path.exists())

    def test_legacy_queue_upgrade_leaves_ambiguous_initial_absence_untouched(self):
        sha, _ = self.article("旧库缺页", "五台出租。")
        proposal = [self.proposed("等待恢复？")]
        with patch.object(questions, "_write_projection", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                questions.apply(self.vault, sha, proposal, confirm_new=True)
        with sqlite3.connect(self.vault / ".afr/questions.sqlite") as db:
            db.execute("DROP TABLE pending_projections")
        record, = questions.catalog(self.vault)
        path = self.vault / questions.read(self.vault, record["id"])["path"]

        with self.assertRaisesRegex(RequestError, "user_edit_conflict"):
            questions.apply(self.vault, sha, proposal, confirm_new=True)
        self.assertFalse(path.exists())
        self.assertFalse((self.vault / questions.INDEX).exists())

    def test_expected_revision_protects_catalog_observation(self):
        first, _ = self.article("甲", "五台出租。")
        second, _ = self.article("乙", "已出租仍含预留。")
        item, = questions.apply(self.vault, first, [self.proposed()], confirm_new=True)["questions"]
        identifier = item["id"]
        questions.update_note(self.vault, identifier, 1, "报告有口径差异。")
        with self.assertRaisesRegex(RequestError, "revision_changed"):
            questions.apply(self.vault, second, [self.attach(identifier)], expected_revisions={identifier: 1})
        with self.assertRaisesRegex(RequestError, "revision_changed"):
            questions.update_note(self.vault, identifier, 1, "过期更新")
        self.assertEqual(len(questions.read(self.vault, identifier)["links"]), 1)


if __name__ == "__main__":
    unittest.main()
