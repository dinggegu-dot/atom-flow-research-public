import json
import unittest
from unittest.mock import patch

from afr_agent import knowledge
from afr_agent.tests import test_knowledge


class KnowledgeReviewBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_knowledge.KnowledgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.vault = self.fixture.vault
        self.policy = self.fixture.policy

    def test_oversized_valid_draft_and_finished_review_part_survive_retry(self):
        first, second = self.fixture.evidence()
        topics = []
        for number in range(1, 6):
            topic, = self.fixture.proposal(first, second)["topics"]
            topic["dimension"] = f"投入比较维度 {number}"
            for event in topic["events"]:
                event.update(context="需对照独立的材料和投资阶段。" * 15,
                             estimate="测算口径使用企业已公告的阶段。" * 12)
            topics.append(topic)
        draft = {"topics": topics}
        proposal_sizes = []
        review_parts = []
        completed = []

        def runner(instructions, payload, _schema):
            if instructions.startswith("第一轮"):
                proposal_sizes.append(knowledge._payload_size({**payload, "draft": draft}))
                self.assertGreater(proposal_sizes[-1], knowledge.MAX_PAYLOAD)
                return draft
            self.assertLessEqual(knowledge._payload_size(payload), knowledge.MAX_PAYLOAD)
            self.assertEqual({article["sha256"] for article in payload["articles"]}, {first, second})
            scope = payload["draft_review_scope"]
            numbers = tuple(scope["topic_numbers"])
            self.assertEqual([topics[number - 1] for number in numbers], payload["draft"]["topics"])
            review_parts.append(numbers)
            if len(review_parts) == 2:
                raise OSError("synthetic interrupted review")
            completed.extend(numbers)
            return payload["draft"]

        with patch.object(knowledge, "MAX_PAYLOAD", 5_200):
            with self.assertRaisesRegex(OSError, "interrupted"):
                knowledge.run(self.vault, [first, second], self.policy, runner)
            directory = self.vault / knowledge.STATE
            proposal, = directory.glob("*-proposal.json")
            self.assertEqual(json.loads(proposal.read_text())["output"], draft)
            self.assertEqual(len(list(directory.glob("*-review-*.json"))), 1)

            result = knowledge.run(self.vault, [first, second], self.policy, runner)
            self.assertEqual(result["topics"], len(topics))
            self.assertEqual(result["added_events"], len(topics) * 2)
            self.assertEqual(proposal_sizes, proposal_sizes[:1])
            self.assertEqual(sorted(completed), list(range(1, len(topics) + 1)))
            self.assertEqual(review_parts.count(review_parts[0]), 1)
            review, = directory.glob("*-review.json")
            saved = json.loads(review.read_text())
            self.assertEqual(saved["output"], draft)
            self.assertGreaterEqual(len(saved["segments"]), 2)

    def test_one_oversized_topic_compacts_display_fields_with_explicit_scope(self):
        first, second = self.fixture.evidence()
        draft = self.fixture.proposal(first, second)
        topic, = draft["topics"]
        topic["overview"] = "给出白板卡片展示的条目。" * 12
        for event in topic["events"]:
            event.update(context="这部分用于白板展示。" * 21,
                         estimate="项目阶段披露的对照表。" * 17,
                         headline="卡片标题")
        payload = {"articles": ["卡片和证据内容" * 100]}
        expected_core = [{key: value for key, value in item.items() if key not in knowledge.EVENT_CARD_FIELDS}
                         for item in topic["events"]]
        limit = knowledge._payload_size({
            **payload, "draft": {"topics": [{**topic, "events": expected_core}]},
        }) + 400
        self.assertGreater(knowledge._payload_size({**payload, "draft": draft}), limit)
        with patch.object(knowledge, "MAX_PAYLOAD", limit):
            part, = knowledge._review_parts(payload, draft)
        self.assertLessEqual(knowledge._payload_size(part), limit)
        self.assertEqual(part["articles"], payload["articles"])
        self.assertEqual(part["draft_review_scope"]["regenerate_presentation_for"], [1])
        self.assertEqual(part["draft"]["topics"][0]["events"], expected_core)
