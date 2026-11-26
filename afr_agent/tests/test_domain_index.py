import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from afr_agent import domain_index, questions, semantic, workflow
from afr_agent.filing import DOMAIN_TERMS
from afr_agent.library import RequestError


class DomainIndexTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name)

    def article(self, domain, title, character, *, aliases=False, root="资料档案"):
        sha = character * 64
        folder = f"{root}/{domain}/{title} · {sha[:16]}"
        (self.vault / folder).mkdir(parents=True)
        suffix = "（本轮校读）" if aliases else ""
        paths = {"reading": f"{folder}/完整阅读{suffix}.md", "canvas": f"{folder}/文章白板{suffix}.canvas"}
        for relative in paths.values():
            (self.vault / relative).write_text("示例")
        (self.vault / folder / "固定原文.md").write_text("示例原文")
        if aliases:
            receipt = semantic.receipt(self.vault, sha, "publication")
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({"paths": paths}, ensure_ascii=False))
        return folder, sha, paths

    def test_preserves_manual_notes_and_appends_once_for_each_signed_report(self):
        folder, sha, paths = self.article("治理与地缘", "城市治理与公共服务", "a")
        label = "# 治理与地缘\n\n已有手工文字。\n"
        index = self.vault / "资料档案/治理与地缘/本领域资料索引.md"
        index.write_text(label)

        self.assertTrue(domain_index.ensure_entry(self.vault, folder, sha))
        self.assertFalse(domain_index.ensure_entry(self.vault, folder, sha))
        after = index.read_text()
        self.assertTrue(after.startswith(label))
        self.assertIn(f"[[{paths['reading']}|城市治理与公共服务]]", after)
        self.assertIn(f"[[{paths['canvas']}|文章白板]]", after)
        self.assertIn(f"[[{folder}/固定原文.md|固定原文]]", after)
        index.write_text(after + "\n手工补充。\n")

        second, second_sha, _ = self.article("治理与地缘", "基层治理案例", "b")
        self.assertTrue(domain_index.ensure_entry(self.vault, second, second_sha))
        self.assertIn(f"{second}/完整阅读.md", index.read_text())
        self.assertEqual(index.read_text().count("## 自动归档"), 1)
        self.assertTrue(index.read_text().endswith("\n手工补充。\n"))

    def test_pending_index_honors_review_alias_and_existing_extensionless_link(self):
        folder, sha, paths = self.article("待整理", "尚需核对的研报", "c", aliases=True, root="资料库")
        index = self.vault / "资料库/待整理/缺少既有分类.md"
        prior = f"# 待整理\n\n- [[{paths['reading'].removesuffix('.md')}|手工条目]]\n"
        index.write_text(prior)
        self.assertFalse(domain_index.ensure_entry(self.vault, folder, sha))
        self.assertEqual(index.read_text(), prior)

        index.write_text("# 待整理\n")
        self.assertTrue(domain_index.ensure_entry(self.vault, folder, sha))
        self.assertIn(f"[[{paths['reading']}|尚需核对的研报]]", index.read_text())

    def test_existing_vault_missing_index_is_not_initialized_automatically(self):
        folder, sha, _ = self.article("企业与投资", "资产估值", "d")
        self.assertFalse(domain_index.ensure_entry(self.vault, folder, sha))
        self.assertFalse((self.vault / "资料档案/企业与投资/本领域资料索引.md").exists())
        self.assertFalse((self.vault / ".afr").exists())
        with TemporaryDirectory() as inbox:
            workflow.seed_baseline(self.vault, Path(inbox))
        self.assertFalse((self.vault / "资料档案/企业与投资/本领域资料索引.md").exists())

    def test_seed_baseline_initializes_empty_vault_and_indexes_signed_report(self):
        (self.vault / ".obsidian").mkdir()
        with TemporaryDirectory() as inbox:
            workflow.seed_baseline(self.vault, Path(inbox))
        for domain in DOMAIN_TERMS:
            index = self.vault / "资料档案" / domain / "本领域资料索引.md"
            self.assertEqual(index.read_text(), f"# {domain}\n")
        index = self.vault / "资料档案/综合研究/本领域资料索引.md"
        self.assertEqual(index.read_text(), "# 综合研究\n")
        self.assertFalse((self.vault / "资料库").exists())
        self.assertFalse((self.vault / "资料档案/待整理").exists())
        folder, sha, paths = self.article("企业与投资", "资产估值", "d")
        self.assertTrue(domain_index.ensure_entry(self.vault, folder, sha))
        index = self.vault / "资料档案/企业与投资/本领域资料索引.md"
        edited = index.read_text() + "\n手工补充。\n"
        index.write_text(edited)
        domain_index.initialize(self.vault)
        self.assertEqual(index.read_text(), edited)
        self.assertIn(f"[[{paths['canvas']}|文章白板]]", edited)

    def test_conflicting_user_edit_is_left_intact(self):
        folder, sha, _ = self.article("科技与产业", "人形机器人", "e")
        index = self.vault / "资料档案/科技与产业/本领域资料索引.md"
        index.write_text("# 科技与产业\n")
        original_writer = questions._write_projection

        def concurrent_edit(path, before, after):
            path.write_text("# 科技与产业\n\n这是同时提交的手工修改。\n")
            return original_writer(path, before, after)

        with patch.object(questions, "_write_projection", side_effect=concurrent_edit):
            with self.assertRaisesRegex(RequestError, "user_edit_conflict"):
                domain_index.ensure_entry(self.vault, folder, sha)
        self.assertEqual(index.read_text(), "# 科技与产业\n\n这是同时提交的手工修改。\n")
