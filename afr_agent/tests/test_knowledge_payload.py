import unittest
from unittest.mock import patch

from afr_agent import knowledge, semantic


def signed_group(number, title, text, *, viewpoint=None):
    return ({"start": number, "end": number, "title": title, "branch": title, "summary": text,
             **({"viewpoints": [viewpoint]} if viewpoint else {})},
            {"text": text})


def viewpoint(number, quote, *, speaker="作者", conditions=(), grounds=()):
    return {"judgment": f"观点 {number} 的结论", "speaker": speaker, "kind": "reported_fact",
            "grounds": list(grounds), "conditions": list(conditions),
            "evidence": [{"number": number, "quote": quote}]}


class KnowledgePayloadTests(unittest.TestCase):
    def payload(self, documents):
        plans = {sha: {"sha256": sha, "plan_sha256": sha, "groups": groups}
                 for sha, (groups, _) in documents.items()}

        def evidence(_vault, sha):
            return None, {"title": "试验材料", "parts": documents[sha][1]}

        with patch.object(knowledge, "evidence_for", side_effect=evidence), patch.object(
                semantic, "reviewed_plan", side_effect=lambda _vault, sha, **_: plans[sha]), patch.object(
                semantic, "card_supplements", return_value={}), patch.object(
                knowledge, "card_body", side_effect=lambda _evidence, group: (group["summary"],)):
            return knowledge._article_payload(None, documents), plans

    def test_signed_quote_keeps_exact_source_and_mixed_card_can_supply_other_paragraph(self):
        legacy, signed = "a" * 64, "b" * 64
        text = "全球 HBM 市场面临新的散热限制。"
        extra = "试点已正式部署，适用于机房东侧。"
        group, part = signed_group(1, "散热限制与正式部署", text, viewpoint=viewpoint(
            1, "全球HBM市场面临新的散热限制", speaker="材料原作者甲",
            conditions=("仅限第三期样本", "南侧机房仍需核实"), grounds=("负载变化后设备停机",)))
        group["end"] = 2
        old_group, old_part = signed_group(1, "散热限制与正式部署", "此前仍在试点期间。")
        (articles, pointers, _), plans = self.payload({
            legacy: ([old_group], [old_part]), signed: ([group], [part, {"text": extra}]),
        })
        actual_id = semantic.article_viewpoints(plans[signed])[0]["id"]
        signed_card = articles[1]["cards"][0]
        self.assertEqual(signed_card["viewpoints"][0]["speaker"], "材料原作者甲")
        self.assertEqual(signed_card["viewpoints"][0]["conditions"], ["仅限第三期样本", "南侧机房仍需核实"])
        self.assertEqual(signed_card["evidence"][0]["number"], 2)
        self.assertIn(extra, pointers[signed, 1, 2][1])
        self.assertEqual(pointers[signed, 1, 1][2], {actual_id: ("全球 HBM 市场面临新的散热限制",)})
        self.assertEqual(len(pointers[legacy, 1, 1]), 2)

    def test_long_mixed_batch_keeps_each_source_citable_and_reserves_review_space(self):
        documents = {}
        for source in range(1, 11):
            groups, parts = [], []
            for number in range(1, 112 if source == 1 else 15):
                text = f"第{source}批第{number}项调查原文。" + "这是长篇论述证据的固定出处。" * 45
                record = viewpoint(number, text[:600], speaker="完整作者署名", conditions=(
                    "在大型设备正常维护、现场执行流程符合规范等条件下。" * 12,),
                    grounds=("明确记录每个条件的材料理由。" * 12,)) if source % 2 else None
                group, part = signed_group(number, f"第{source}批项目独立阶段 {number}", text, viewpoint=record)
                groups.append(group)
                parts.append(part)
            documents[f"{source:064x}"] = groups, parts
        (articles, pointers, candidates), _ = self.payload(documents)
        self.assertLessEqual(knowledge._payload_size(articles), 64_000)
        knowledge._model_payload({"articles": articles, "cross_card_candidates": candidates,
                                  "existing_topics": "历" * 15_000, "draft": "拟" * 33_000})
        self.assertEqual(len(articles), 10)
        self.assertTrue(any(article.get("omitted_cards") for article in articles))
        self.assertTrue(any(card.get("omitted_viewpoints") for article in articles for card in article["cards"]))
        for article in articles:
            sha = article["sha256"]
            self.assertTrue(any(pointer[0] == sha for pointer in pointers))
            for card in article["cards"]:
                for record in card.get("viewpoints", ()):
                    source = card["id"]
                    quote, = [citation["quote"] for citation in record["evidence"]]
                    self.assertEqual(pointers[sha, source, source][2][record["id"]], (quote,))
                    self.assertEqual(record["conditions"][0], "在大型设备正常维护、现场执行流程符合规范等条件下。" * 12)
