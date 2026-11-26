import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from afr_agent import intake, library, presentation, semantic


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.vault = base / "知识库"
        self.inbox = base / "研报"
        self.vault.mkdir()
        self.inbox.mkdir()
        self.material = self.inbox / "测试.md"
        self.material.write_text("# 合成研究\n\n## 一项测试\n\n五台已出租，只有十台可用，若可用数降至八台，占比随之上升。")

    def scan(self):
        return intake.scan(self.vault, self.inbox)

    def first(self):
        item, = self.scan()["added"]
        folder = self.vault / item["folder"]
        doc = json.loads((folder / "证据索引.json").read_text())
        group = dict(start=1, end=len(doc["parts"]), title="出租", branch="算例", summary="可用数改变分母。",
                     argument="可用机柜从十台降至八台，五台出租对应的比率升高。")
        return item["sha256"], folder, dict(sha256=item["sha256"],groups=[group])

    def test_replay_alias_replacement_preserves_old_user_edit(self):
        sha,folder,plan = self.first()
        self.assertEqual((folder / "原件.md").read_bytes(),self.material.read_bytes())
        shutil.copy(self.material,self.inbox / "同内容另名.md")
        self.assertEqual(self.scan()["added"],[])
        approvals = self.vault.parent / "approvals.json"
        approvals.touch(mode=0o600)
        approvals.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=[sha])))
        with patch.object(semantic.codex, "run_structured", side_effect=[
                {"groups": plan["groups"]}, {"passed": True, "issues": []}]):
            semantic.process(self.vault, sha, approvals)
        note = folder / "完整阅读.md"
        note.write_text(note.read_text()+"\n人工笔记")
        self.assertEqual(semantic.process(self.vault, sha, approvals)["state"], "already_published")
        self.material.write_text("新版本包含独立的内容。")
        changed, = self.scan()["added"]
        self.assertNotEqual(sha,changed["sha256"])
        self.assertIn("人工笔记",note.read_text())

    def test_partial_coverage_rejected_with_no_final_board(self):
        sha,folder,plan = self.first()
        plan["groups"][0]["end"] -= 1
        with self.assertRaisesRegex(library.RequestError,"invalid_coverage"):
            presentation.publish(self.vault,sha,plan)
        self.assertFalse((folder / "文章白板.canvas").exists())
        self.assertTrue((folder / "固定原文.md").exists())

    def test_default_processing_has_no_private_model_egress(self):
        sha,_,_ = self.first()
        with patch.object(semantic.codex, "run_structured", side_effect=AssertionError("unexpected model call")):
            with self.assertRaisesRegex(library.RequestError,"local_only"):
                semantic.process(self.vault,sha)

    def test_demo_cannot_bypass_local_only_policy(self):
        demo = self.inbox / "上架率小样.md"
        shutil.copyfile(Path(__file__).parent / "fixtures" / demo.name, demo)
        intake.scan(self.vault, self.inbox)
        sha = intake.digest(demo.read_bytes())
        self.assertEqual(sha, semantic.DEMO_SHA256)
        policy = self.inbox.parent / "approvals.json"
        policy.touch(mode=0o600)
        policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": []}))
        for approvals in (None, policy):
            with self.subTest(approvals=approvals), patch.object(semantic.codex, "run_structured") as invoke:
                with self.assertRaisesRegex(library.RequestError, "local_only"):
                    semantic.process(self.vault, sha, approvals)
                invoke.assert_not_called()

    def test_symlink_input_and_vault_escape(self):
        (self.inbox / "别名.md").symlink_to(self.material)
        sha,folder,_=self.first()
        self.assertEqual(len(self.scan()["pending"]),1)
        (self.vault / "逃逸").symlink_to(self.inbox,target_is_directory=True)
        with self.assertRaisesRegex(library.RequestError,"invalid_path"):
            intake.inside(self.vault,"逃逸/别名.md")
        self.assertFalse(any(p.name=="别名.md" for p in library.notes(self.vault)))
        self.assertEqual(hashlib.sha256((folder/"原件.md").read_bytes()).hexdigest(),sha)

    def test_initial_reading_keeps_anchors_and_preserves_old_canvas(self):
        _,folder,_=self.first()
        self.assertEqual(list(folder.glob("*.canvas")), [])
        reading = (folder / "固定原文.md").read_text()
        self.assertIn("一项测试", reading)
        self.assertIn("五台已出租", reading)
        self.assertIn("^e3", reading)
        legacy = folder / "原文结构.canvas"
        legacy.write_text('这个旧白板包含手改布局')
        self.assertEqual(self.scan()["errors"], [])
        self.assertEqual(legacy.read_text(), '这个旧白板包含手改布局')

    def test_new_report_uses_author_title_and_existing_domain_without_overwrite(self):
        self.material.unlink()
        source = self.inbox / 'export_12345.md'
        title = '城市治理与基层协作'
        body = f'# {title}\n\n地方治理需要明确责任。'
        source.write_text(body)
        occupied = self.inbox / f'{title}.md'
        occupied.write_text('另一个版本的资料。')

        original_time = source.stat().st_mtime_ns
        collected = intake.scan(self.vault, self.inbox, rename_generic=True)

        self.assertEqual(collected['errors'], [])
        self.assertEqual(collected['renamed'], 1)
        self.assertFalse(source.exists())
        self.assertEqual(occupied.read_text(), '另一个版本的资料。')
        article = next(item for item in collected['added'] if item['folder'].startswith('资料档案/治理与地缘/'))
        date_name = intake.source_day(original_time).strftime("%Y年%m月%d日") + ".md"
        self.assertEqual((self.inbox / date_name).read_text(), body)
        self.assertEqual((self.inbox / date_name).stat().st_mtime_ns, original_time)
        self.assertEqual((self.vault / article['folder'] / '原件.md').read_text(), body)
        self.assertEqual(intake.scan(self.vault, self.inbox, rename_generic=True)['added'], [])
        self.assertFalse((self.vault / '资料库').exists())

    def test_moved_search_reference_keeps_identity_and_hashes_returned_text(self):
        old = '资料库/新收研报/export_12345 · 0123456789abcdef'
        new = '资料库/治理与地缘/城市治理 · 0123456789abcdef'
        old_note = self.vault / old / '完整阅读.md'
        old_note.parent.mkdir(parents=True)
        original = f'# 城市治理\n\n[[{old}/固定原文.md|原文]]\n'
        old_note.write_text(original)
        before, = library.search(self.vault, '城市治理', 5, 'article')['hits']
        new_note = self.vault / new / '完整阅读.md'
        new_note.parent.parent.mkdir(parents=True)
        old_note.parent.rename(new_note.parent)
        new_note.write_text(original.replace(old, new))
        state = self.vault / '.afr'
        state.mkdir()
        (state / 'article-locations.json').write_text(json.dumps({'version': 1, 'folders': {old: new}}))

        after, = library.search(self.vault, '城市治理', 5, 'article')['hits']

        self.assertEqual(after['ref'], before['ref'])
        self.assertEqual(after['path'], f'{new}/完整阅读.md')
        with self.assertRaisesRegex(library.RequestError, 'revision_changed'):
            library.read(self.vault, before['ref'], before['revision'])
        current = library.read(self.vault, after['ref'], after['revision'])
        self.assertEqual(hashlib.sha256(current['text'].encode()).hexdigest(), after['revision'])

    def test_cli_current_revision_and_errors(self):
        src=self.vault/"笔记.md"
        src.write_text("# 一项测试\n\n临时机柜观察")
        def call(*args):
            return subprocess.run([sys.executable,"-m","afr_agent",*args,"--vault",str(self.vault)],capture_output=True,text=True)
        hit,=json.loads(call("search","--query","临时机柜").stdout)["hits"]
        args=("read","--ref",hit["ref"],"--revision",hit["revision"])
        self.assertIn("临时机柜观察",json.loads(call(*args).stdout)["text"])
        src.write_text("# 一项测试\n\n新增证据")
        stale=call(*args)
        self.assertEqual(stale.returncode,2)
        self.assertEqual(json.loads(stale.stdout)["error"],"revision_changed")
        sha,_,_=self.first()
        response=json.loads(call("evidence","--sha256",sha).stdout)
        self.assertTrue(response["ok"])
        self.assertEqual(response["parts"][-1]["text"],self.material.read_text().split("\n")[-1])


    def test_missing_file_repair_and_tamper_detection(self):
        sha, folder, _ = self.first()
        for name in ("固定原文.md", "证据索引.json", "原件.md"):
            path = folder / name
            original = path.read_bytes()
            path.unlink()
            self.assertEqual(self.scan()["errors"], [])
            self.assertEqual(path.read_bytes(), original)
        altered = folder / "证据索引.json"
        data = json.loads(altered.read_text())
        data["parts"][0]["text"] = "不同材料"
        altered.write_text(json.dumps(data, ensure_ascii=False))
        self.assertEqual(self.scan()["errors"][0]["error"], "evidence_changed")
        with self.assertRaisesRegex(library.RequestError, "evidence_changed"):
            presentation.evidence_for(self.vault, sha)

    def test_docx_recovery_with_stable_element_identity(self):
        import docx
        self.material.unlink()
        path = self.inbox / "恢复案例.docx"
        document = docx.Document()
        document.add_heading("设备使用率", level=1)
        document.add_paragraph("先确认统计范围，再比较使用率。")
        document.save(path)
        sha, folder, _ = self.first()
        originals = (folder / "证据索引.json").read_bytes()
        # 模拟刚发布目录、尚未写入首批 SQLite 记录时程序中断。
        (self.vault / ".afr" / "intake.sqlite").unlink()
        result = self.scan()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["pending"][0]["sha256"], sha)
        self.assertEqual((folder / "证据索引.json").read_bytes(), originals)

    def test_unavailable_parser_does_not_stop_other_files(self):
        missing = self.inbox / "00测试.docx"
        missing.write_bytes(b"placeholder")
        parse = intake.units

        def units(path, *args):
            if path == missing:
                raise ImportError("test parser missing")
            return parse(path, *args)

        with patch.object(intake, "units", side_effect=units):
            result = self.scan()
        self.assertEqual(result["errors"], [{"file": missing.name, "error": "parser_unavailable"}])
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(result["added"][0]["sha256"], intake.digest(self.material.read_bytes()))

    def test_fragment_offsets_and_superseded_pending(self):
        self.material.write_text("# 原标题\n理由与条件。\n## 两种情况\r\n两个例子。")
        first, = self.scan()["added"]
        _, doc = presentation.evidence_for(self.vault, first["sha256"])
        raw = self.material.read_bytes().decode()
        self.assertEqual([p["kind"] for p in doc["parts"]], ["heading","paragraph","heading","paragraph"])
        for part in doc["parts"]:
            self.assertTrue(raw[part["locator"]["offset"]:].startswith(part["text"]))
        self.material.write_text("同名文件的最终内容。")
        self.assertEqual(intake.scan(self.vault, self.inbox, settle_seconds=2)["pending"], [])
        latest = self.scan()
        self.assertEqual(len(latest["pending"]), 1)
        self.assertNotEqual(latest["pending"][0]["sha256"], first["sha256"])

    def test_search_skips_bad_note_and_locates_duplicate_heading(self):
        (self.vault / "坏笔记.md").write_bytes(bytes([255]))
        (self.vault / "重复.md").write_text("## 分类\n先说甲\n\n^a1\n\n## 分类\n再说乙\n\n^b2")
        results = library.search(self.vault, "说", 10)
        self.assertEqual(results["skipped_notes"], 1)
        self.assertEqual(len({hit["uri"] for hit in results["hits"]}), 2)
        for hit in results["hits"]:
            self.assertIn("说", library.read(self.vault, hit["ref"], hit["revision"])["text"])

    def test_readable_without_model_for_outsize_input_or_blank_scan(self):
        with self.assertRaisesRegex(library.RequestError, "oversized_unit"):
            list(semantic.partitions([dict(text="字"*50000,kind="paragraph")]))
        self.material.write_text("")
        self.assertEqual(self.scan()["pending"], [])
        policy = self.inbox.parent / "model-approvals.json"
        sha = "a"*64
        policy.write_text(json.dumps({"destination":"OpenAI Codex","sha256":sha + "invalid"}))
        self.assertFalse(semantic.allowed(sha,policy))

    def test_invalid_manifest_has_machine_readable_error(self):
        (self.vault / "资料导入清单.json").write_text("[{}]")
        result = subprocess.run([sys.executable, "-m", "afr_agent", "scan", "--vault", str(self.vault),
                                 "--inbox", str(self.inbox)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertEqual(json.loads(result.stdout)["error"], "invalid_manifest")


    def test_card_summary_retains_short_complete_claim(self):
        short = "甲试点共有十台机柜，已有7台上架，上架率为70%。下次巡检要复核剩余3台并记录日期。"
        group = {"summary": "记录了机柜上架情况和复核要求。", "argument": short}
        semantic.complete_card_summaries([group])
        self.assertEqual(group["summary"], short)


    def test_pdf_extraction_counts_gap_and_original_page(self):
        import pymupdf
        self.material.unlink()
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((36,52), "Seven of ten units are available.")
            pdf.new_page()
            pdf.save(self.inbox / "report.pdf")
        sha, folder, _ = self.first()
        _, doc = presentation.evidence_for(self.vault, sha)
        self.assertEqual(doc["parts"][0]["locator"]["page"], 1)
        self.assertEqual(len(doc["parts"][0]["locator"]["bbox"]), 4)
        self.assertIn("第 2 页需要 OCR", doc["gaps"])
        self.assertIn("原件.pdf#page=1", (folder / "固定原文.md").read_text())

    def test_misc_file_is_visible_and_not_executed(self):
        (self.inbox / "资料.unknown").write_bytes(b"sample")
        run = self.scan()
        self.assertEqual(run["unsupported"], ["资料.unknown"])
        self.assertTrue((self.inbox / "资料.unknown").exists())


if __name__=="__main__":
    unittest.main()
