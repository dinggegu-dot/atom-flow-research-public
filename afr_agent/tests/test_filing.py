import unittest

from afr_agent.filing import classify_article, is_generic_title


class ArticleFilingTests(unittest.TestCase):
    def test_selects_each_existing_domain_from_the_article_title(self):
        examples = (
            ("并购估值与经营现金流", "企业与投资"),
            ("央行货币政策与信用利差", "宏观与金融"),
            ("地缘博弈中的外交与停火", "治理与地缘"),
            ("青年就业与社会保障", "社会与劳动"),
            ("人形机器人的算力需求", "科技与产业"),
            ("实验设计与因果推断", "认知与方法"),
        )
        for title, domain in examples:
            with self.subTest(domain=domain):
                self.assertEqual(classify_article(title, []), (title, domain))

    def test_docx_heading_names_export_and_outranks_repeated_body_terms(self):
        parts = [{"kind": "heading", "text": "半导体行业的供需变化"},
                 {"kind": "paragraph", "text": "通胀及国债。" * 90}]
        self.assertEqual(classify_article("export_12345", parts), ("半导体行业的供需变化", "科技与产业"))

    def test_governance_title_controls_filing_with_financial_and_social_sections(self):
        secondary = [{"kind": "heading", "text": "城投、财政赤字与国债"},
                     {"kind": "paragraph", "text": "货币政策、通胀、降息、利率、汇率和央行。"},
                     {"kind": "heading", "text": "农村就业与社保"}]
        for phrase in ("城市治理", "基层治理", "地方治理"):
            title = f"{phrase}如何协调不同部门？"
            with self.subTest(phrase=phrase):
                parts = [{"kind": "heading", "text": title}, *secondary]
                self.assertEqual(classify_article("export_1000000", parts), (title, "治理与地缘"))

    def test_explicit_titles_and_markdown_h1_override_generic_and_opaque_file_names(self):
        for stem, first in (("未命名 2", {"kind": "title", "text": "工资结构改变岗位分布"}),
                            ("Q4-brief", {"kind": "heading", "text": "  # 工资结构改变岗位分布 #"})):
            with self.subTest(stem=stem):
                self.assertEqual(classify_article(stem, [first]), ("工资结构改变岗位分布", "社会与劳动"))

    def test_heading_evidence_breaks_ambiguous_title_and_unknowns_remain_pending(self):
        title = "产业链和汇率"
        self.assertEqual(classify_article(title, []), (title, "待整理"))
        parts = [{"kind": "heading", "text": "## 央行的两种货币政策"}]
        self.assertEqual(classify_article(title, parts), (title, "宏观与金融"))
        self.assertEqual(classify_article("扫描件001", [{"kind": "heading", "text": "前言"}]),
                         ("待命名研报", "待整理"))

    def test_recognizes_generic_names_and_uses_english_topic_aliases(self):
        for stem in ("Export_1A2B3C4D", "2026-11-23", "新建 Microsoft Word 文档", "scan0001", "Untitled Document"):
            self.assertTrue(is_generic_title(stem), stem)
        self.assertFalse(is_generic_title("Robotics briefing 2026"))
        self.assertEqual(classify_article("ROBOTICS briefing", [])[1], "科技与产业")
        self.assertEqual(classify_article("Global labour market report", [])[1], "社会与劳动")
        self.assertEqual(classify_article("Macrorobotics", [])[1], "待整理")

    def test_specialized_technology_titles_and_body_after_pdf_cover(self):
        for title in ("端侧 AI 的推理任务", "存储行业、全球 AI ASIC 与季度财报"):
            self.assertEqual(classify_article(title, [])[1], "科技与产业")
        title = "成长板块筹码出清交流"
        self.assertEqual(classify_article(title, [])[1], "企业与投资")
        cover = [{"kind": "paragraph", "text": "报告信息"}] * 24
        findings = [{"kind": "paragraph", "text": "半导体、芯片、数据中心、算力和光刻设备。"}]
        self.assertEqual(classify_article("行业观察", [*cover, *findings])[1], "科技与产业")
