import unittest

from afr_agent.presentation import CHAPTER_READING_LAYOUT, ORIGINAL_ARGUMENT, reading_markdown


class ReadingLayoutTests(unittest.TestCase):
    def test_chapters_follow_canvas_themes_and_source_order(self):
        branches = ["供给", "供给", "需求", "供给"]
        paragraphs = ["供给的起点。", "供给的补充说明。", "需求的中间机制。", "重新讨论供给。"]
        evidence = {"title": "供需研究", "folder": "资料档案/供需研究",
                    "parts": [{"kind": "paragraph", "text": text} for text in paragraphs]}
        groups = [{"branch": branch, "start": number, "end": number,
                   "title": f"白板话题 {number}", "argument": ORIGINAL_ARGUMENT}
                  for number, branch in enumerate(branches, 1)]

        reading = reading_markdown(evidence, groups, include_source_text=True, layout=CHAPTER_READING_LAYOUT)

        self.assertEqual([line for line in reading.splitlines() if line.startswith("## ")],
                         ["## 供给", "## 需求", "## 供给"])
        self.assertEqual([reading.count(text) for text in paragraphs], [1, 1, 1, 1])
        self.assertEqual([reading.index(text) for text in paragraphs],
                         sorted(reading.index(text) for text in paragraphs))
        for number, paragraph in enumerate(paragraphs, 1):
            self.assertIn(f"{paragraph} ^topic-{number}", reading)
        self.assertNotIn("白板话题", reading)
        self.assertNotIn(ORIGINAL_ARGUMENT, reading)
        self.assertNotIn("原文依据", reading)
        self.assertEqual(reading.count("|本节原文]]"), 3)

    def test_export_filename_uses_author_title_once(self):
        evidence = {"title": "export_12345", "folder": "资料档案/样本", "parts": [
            {"kind": "heading", "text": "作者的文章标题"},
            {"kind": "paragraph", "text": "正文第一段。"},
        ]}
        groups = [{"branch": "开篇", "start": 1, "end": 2, "title": "话题", "argument": ORIGINAL_ARGUMENT}]

        reading = reading_markdown(evidence, groups, layout=CHAPTER_READING_LAYOUT)

        self.assertIn("# 作者的文章标题\n", reading)
        self.assertEqual(reading.count("作者的文章标题"), 1)
        self.assertIn("正文第一段。 ^topic-1", reading)
