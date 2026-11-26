import unittest
from collections import Counter
from itertools import combinations

from afr_agent.board_style import ROOT_COLOR, branch_color
from afr_agent.presentation import ARCHIVE_BOARD_LAYOUT, PASTEL_ARCHIVE_BOARD_LAYOUT, board


class ArchiveBoardLayoutTests(unittest.TestCase):
    def test_pastel_archive_stays_consistent_across_cards_edges_and_continuations(self):
        evidence = {"folder": "资料库/文章", "title": "主题样本", "suffix": ".md", "gaps": [],
                    "parts": [{"kind": "paragraph", "text": "正文"}] * 8}
        groups = [
            {"start": number, "end": number, "title": f"话题 {number}", "summary": "摘要",
             "branch": "同一主线" if number <= 5 else f"主线 {number}"}
            for number in range(1, 9)
        ]
        pastel = board(evidence, groups, layout=PASTEL_ARCHIVE_BOARD_LAYOUT)
        legacy = board(evidence, groups, layout=ARCHIVE_BOARD_LAYOUT)
        nodes = {node["id"]: node for node in pastel["nodes"]}
        self.assertEqual(nodes["article"]["color"], ROOT_COLOR)
        self.assertEqual(nodes["row2"]["color"], ROOT_COLOR)
        self.assertEqual({nodes[f"t{n}"]["color"] for n in range(1, 6)}, {branch_color(0)})
        self.assertIn("同一主线（续）", nodes["t5"]["text"])
        self.assertEqual(len({nodes[f"t{n}"]["color"] for n in (1, 6, 7, 8)} | {ROOT_COLOR}), 5)
        for edge in pastel["edges"]:
            self.assertEqual(edge["color"], nodes[edge["toNode"]]["color"])
        self.assertEqual({node["color"] for node in legacy["nodes"] if node["id"] in {"article", "t1"}}, {"#97b0c0"})
        for key in ("nodes", "edges"):
            without_colors = lambda entries: [{k: v for k, v in item.items() if k != "color"} for item in entries]
            self.assertEqual(without_colors(pastel[key]), without_colors(legacy[key]))

    def test_long_branch_continues_in_columns_and_rows(self):
        count = 21
        evidence = {
            "folder": "资料库/文章",
            "title": "主题样本",
            "suffix": ".md",
            "gaps": [],
            "parts": [{"kind": "paragraph", "text": "正文"}] * count,
        }
        groups = [
            {"start": number, "end": number, "title": f"话题 {number}", "summary": "摘要", "branch": "主题"}
            for number in range(1, count + 1)
        ]
        document = board(evidence, groups, layout=ARCHIVE_BOARD_LAYOUT)
        nodes = {node["id"]: node for node in document["nodes"]}
        cards = [nodes[f"t{i}"] for i in range(1, count + 1)]
        self.assertEqual((nodes["article"]["x"], nodes["article"]["y"]), (0, 0))
        self.assertEqual(len({card["x"] for card in cards}), 4)
        self.assertEqual(max(Counter(card["x"] for card in cards[:16]).values()), 4)
        self.assertGreater(cards[16]["y"], max(card["y"] + card["height"] for card in cards[:16]))
        self.assertEqual(len({card["color"] for card in cards}), 1)
        for number, card in enumerate(cards, 1):
            self.assertIn(f"完整阅读.md#^topic-{number}|", card["text"])
            self.assertIn(f"固定原文.md#^e{number}|", card["text"])
        for left, right in combinations(nodes.values(), 2):
            self.assertTrue(left["x"] + left["width"] <= right["x"]
                            or right["x"] + right["width"] <= left["x"]
                            or left["y"] + left["height"] <= right["y"]
                            or right["y"] + right["height"] <= left["y"])
        self.assertEqual(board(evidence, groups)["nodes"][0]["x"], 920)
