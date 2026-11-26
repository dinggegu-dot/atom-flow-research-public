import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from afr_agent import intake, mcp, questions, semantic
from afr_agent.library import RequestError
from afr_agent.semantic import DEMO_SHA256


PRIVATE_SHA = "a" * 64
PRIVATE_MARKER = "未经批准的研究内容"
QUESTION = {
    "id": "q-1",
    "revision": 2,
    "title": "合成研究问题",
    "path": PRIVATE_MARKER,
    "note": PRIVATE_MARKER,
    "links": [{"sha256": DEMO_SHA256, "topic_id": 1, "relation": "supports", "reason": PRIVATE_MARKER}],
}


def call(research, name, **arguments):
    return mcp.tool_result(research, {"name": name, "arguments": arguments})


class MCPTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name) / "合成知识库"
        self.vault.mkdir()
        self.policy = self.vault.parent / "fixture-approval.json"
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [DEMO_SHA256]}))
        self.policy.chmod(0o600)
        self.research = mcp.ResearchTools(self.vault, self.policy)

    def test_stdio_reads_five_explicit_versions_from_the_fixed_vault_evidence(self):
        inbox = self.vault.parent / "合成研报"
        inbox.mkdir()
        for i in range(5):
            (inbox / f"合成{i}.md").write_text(f"# 样本 {i}\n\n合成正文编号 {i}。", encoding="utf-8")
        (inbox / f"{PRIVATE_MARKER}.md").write_text(f"# {PRIVATE_MARKER}", encoding="utf-8")
        receipts = intake.scan(self.vault, inbox)["added"]
        private, = [item["sha256"] for item in receipts if PRIVATE_MARKER in item["folder"]]
        versions = sorted(item["sha256"] for item in receipts if item["sha256"] != private)
        self.assertEqual(len(versions), 5)
        policy = self.vault / "allow.json"
        policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": versions}), encoding="utf-8")
        policy.chmod(0o600)

        requests = [
            self.message("initialize", 1, protocolVersion="2025-06-18", capabilities={},
                         clientInfo={"name": "synthetic test", "version": "1"}),
            self.message("notifications/initialized"),
            self.message("tools/call", 2, name="list_articles"),
        ]
        for identifier, sha in enumerate(versions + [private], 3):
            requests.append(self.message("tools/call", identifier, name="read_article",
                                         arguments={"article_sha": sha, "start": 1, "limit": 100}))
        server = self.stdio(requests, "--approvals", str(policy))
        responses = [json.loads(line) for line in server.splitlines()]
        self.assertNotIn(PRIVATE_MARKER, json.dumps(responses, ensure_ascii=False))
        _, listing, *reads, denied = responses
        articles = json.loads(listing["result"]["content"][0]["text"])["articles"]
        self.assertEqual([article["sha256"] for article in articles], versions)
        self.assertTrue(all(article["state"] == "review_pending" and article["topics"] == [] for article in articles))
        paragraphs = set()
        for expected_sha, response in zip(versions, reads):
            result = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(result["sha256"], expected_sha)
            self.assertIsNone(result["next"])
            paragraphs.add(result["parts"][-1]["text"])
        self.assertEqual(paragraphs, {f"合成正文编号 {i}。" for i in range(5)})
        self.assertTrue(denied["result"]["isError"])

    def test_list_does_not_open_unapproved_articles(self):
        folder = "资料库/待整理/合成标题 · 0123456789abcdef"
        approved = {"title": "合成标题", "folder": folder, "parts": [{"text": "测试正文"}]}
        with patch.object(mcp, "evidence_for", return_value=(self.vault / folder, approved)) as evidence:
            with patch.object(mcp, "reviewed_plan", side_effect=RequestError("review_required")):
                result = call(self.research, "list_articles")
        evidence.assert_called_once_with(self.vault, DEMO_SHA256)
        self.assertEqual(json.loads(result["content"][0]["text"]), {
            "articles": [{"sha256": DEMO_SHA256, "title": "合成标题", "state": "review_pending", "topics": []}],
        })

    def test_listing_retains_other_approved_articles_when_one_original_is_unavailable(self):
        inbox = self.vault.parent / "投放"
        inbox.mkdir()
        (inbox / "完好.md").write_text("# 完好正文", encoding="utf-8")
        (inbox / "已编辑.md").write_text("# 原始正文", encoding="utf-8")
        receipts = intake.scan(self.vault, inbox)["added"]
        intact, = [item for item in receipts if "完好正文" in item["folder"]]
        edited, = [item for item in receipts if item is not intact]
        (self.vault / edited["folder"] / "固定原文.md").write_text(PRIVATE_MARKER, encoding="utf-8")
        self.policy.write_text(json.dumps({"destination": "OpenAI Codex",
                                          "sha256": [item["sha256"] for item in receipts]}))
        result = self.research.call("list_articles", {})
        self.assertEqual([article["sha256"] for article in result["articles"]], [intact["sha256"]])
        self.assertEqual(result["unavailable"], [edited["sha256"]])
        self.assertNotIn(PRIVATE_MARKER, json.dumps(result, ensure_ascii=False))

    def test_topic_reconstructs_argument_and_preserves_citations_across_pages(self):
        argument = "例🙂" * mcp.ARTICLE_TEXT_BYTES
        span = "原" * (mcp.ARTICLE_TEXT_BYTES // 6)
        spans = [span, span + "终", "异" * mcp.ARTICLE_TEXT_BYTES, "最后"]
        citations = [{"number": i, "path": f"固定原文.md#^e{i}", "locator": {"page": i}, "text": text}
                     for i, text in enumerate(spans, 1)]
        topic = {"sha256": DEMO_SHA256, "topic_id": 1, "revision": "b" * 64, "title": "合成论述",
                 "path": "完整阅读.md#^topic-1", "text": argument, "citations": citations}
        with patch.object(mcp.questions, "read_topic", return_value=topic):
            cursor = {}
            pages = []
            while cursor is not None:
                self.assertLess(len(pages), 20)
                page = self.research.call("read_topic", {"article_sha": DEMO_SHA256, "topic_id": 1, **cursor})
                content = page["text"] + "".join(item.get("text", "") for item in page["citations"])
                self.assertLessEqual(len(content.encode("utf-8")), mcp.ARTICLE_TEXT_BYTES)
                self.assertEqual((page["revision"], page["path"]), (topic["revision"], topic["path"]))
                pages.append(page)
                cursor = page["next"]
        self.assertEqual("".join(page["text"] for page in pages), argument)
        returned = [item for page in pages for item in page["citations"]]
        self.assertEqual([item["number"] for item in returned], [1, 2, 3, 4])
        self.assertEqual([returned[i] for i in (0, 1, 3)], [citations[i] for i in (0, 1, 3)])
        self.assertNotIn("text", returned[2])
        self.assertEqual(returned[2]["path"], citations[2]["path"])
        self.assertEqual(returned[2]["read_article"], {"article_sha": DEMO_SHA256, "start": 3, "limit": 1})

    def test_read_article_reconstructs_a_large_multibyte_part_without_accessing_other_versions(self):
        long_text = "甲𐀀" * (mcp.ARTICLE_TEXT_BYTES // 3)
        evidence = {
            "folder": "资料档案/合成材料",
            "parts": [{"text": long_text, "locator": {"offset": 0}}, {"text": "结束", "locator": {"offset": 1}}],
        }
        with patch.object(mcp, "evidence_for", return_value=(self.vault, evidence)) as read:
            denied = call(self.research, "read_article", article_sha=PRIVATE_SHA, start=1, limit=5)
            self.assertEqual(denied["content"][0]["text"], "local_only")
            read.assert_not_called()
            cursor = {"start": 1, "offset": 0}
            pages = []
            while cursor is not None:
                self.assertLess(len(pages), 5)
                payload = self.research.call("read_article", {"article_sha": DEMO_SHA256, "limit": 1, **cursor})
                page = payload["parts"]
                self.assertLessEqual(sum(len(part["text"].encode("utf-8")) for part in page),
                                     mcp.ARTICLE_TEXT_BYTES)
                pages.append(page)
                cursor = payload["next"]
            self.assertGreater(len(pages), 2)
            self.assertEqual("".join(part["text"] for page in pages for part in page), long_text + "结束")

    def test_search_and_read_redact_mixed_sources_and_manual_notes(self):
        mixed = {
            **QUESTION, "id": "q-2", "title": PRIVATE_MARKER,
            "links": QUESTION["links"] + [{"sha256": PRIVATE_SHA, "topic_id": 2}],
        }
        def search(vault, query, limit, *, visible):
            return {"hits": [record for record in (mixed, QUESTION) if visible(record)][:limit]}

        with patch.object(mcp.questions, "search", side_effect=search):
            result = call(self.research, "search_questions", query="研究", limit=10)
        self.assertEqual(json.loads(result["content"][0]["text"]), {
            "hits": [{"id": "q-1", "revision": 2, "title": "合成研究问题"}],
        })
        with patch.object(mcp.questions, "read", side_effect=[mixed, QUESTION]):
            denied = call(self.research, "read_question", id="q-2")
            result = call(self.research, "read_question", id="q-1", revision=2)
        self.assertEqual(denied["content"][0]["text"], "local_only")
        self.assertTrue(denied["isError"])
        self.assertNotIn(PRIVATE_MARKER, result["content"][0]["text"])
        self.assertIn(DEMO_SHA256, result["content"][0]["text"])

    def test_topic_read_checks_exact_sha_and_approval_file_permissions(self):
        approved = {
            "sha256": PRIVATE_SHA, "topic_id": 1, "revision": "b" * 64, "title": "合成论述",
            "text": "已批准的材料", "path": "资料档案/合成论述/完整阅读.md#^topic-1", "citations": [],
        }
        policy = self.vault / "approvals.json"
        policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [PRIVATE_SHA]}))
        research = mcp.ResearchTools(self.vault, policy)
        with patch.object(mcp.questions, "read_topic", return_value=approved) as read:
            policy.chmod(0o644)
            denied = call(research, "read_topic", article_sha=PRIVATE_SHA, topic_id=1)
            self.assertEqual(denied["content"][0]["text"], "local_only")
            denied = call(research, "read_topic", article_sha=PRIVATE_SHA, topic_id=True)
            self.assertEqual(denied["content"][0]["text"], "invalid_request")
            read.assert_not_called()
            policy.chmod(0o600)
            result = call(research, "read_topic", article_sha=PRIVATE_SHA, topic_id=1)
            self.assertIn(approved["text"], result["content"][0]["text"])
            read.assert_called_once()

    def test_write_denied_by_default_and_returns_only_saved_id_and_revision(self):
        args = {"id": "q-1", "expected_revision": 2, "body": "此次确认保存的讨论"}
        receipt = {"id": "q-1", "revision": 3, "discussion_id": "d-" + "a" * 32, "discussion_revision": 1}
        with patch.object(mcp.questions, "read", return_value=QUESTION) as read:
            with patch.object(mcp.questions, "save_discussion", return_value=receipt) as update:
                self.assertEqual(call(self.research, "update_question", **args)["content"][0]["text"],
                                 "write_disabled")
                read.assert_not_called()
                update.assert_not_called()
                research = mcp.ResearchTools(self.vault, self.policy, allow_write=True)
                result = call(research, "update_question", **args)
                read.assert_called_once_with(self.vault, "q-1", 2)
                update.assert_called_once_with(self.vault, "q-1", 2, args["body"], discussion_id=None)
                self.assertEqual(json.loads(result["content"][0]["text"]), receipt)
                read.side_effect = RequestError("revision_changed")
                self.assertTrue(call(research, "update_question", **args)["isError"])
                self.assertEqual(update.call_count, 1)
                blank = call(research, "update_question", **{**args, "body": " \n"})
                self.assertEqual(blank["content"][0]["text"], "invalid_request")
                self.assertEqual(read.call_count, 2)

    def test_append_preserves_manual_edits_and_retry_does_not_duplicate_discussion(self):
        inbox = self.vault.parent / "研报"
        inbox.mkdir()
        (inbox / "样本.md").write_text("统计包含五台设备。", encoding="utf-8")
        receipt, = intake.scan(self.vault, inbox)["added"]
        sha = receipt["sha256"]
        policy = self.vault.parent / "approval.json"
        policy.write_text(json.dumps({"destination": "OpenAI Codex", "sha256": [sha]}), encoding="utf-8")
        policy.chmod(0o600)
        group = {"start": 1, "end": 1, "title": "设备口径", "branch": "统计",
                 "summary": "统计包含五台设备。", "argument": "统计包含五台设备。"}
        review = {"passed": True, "issues": []}
        with patch.object(semantic.codex, "run_structured", side_effect=[{"groups": [group]}, review]):
            self.assertEqual(semantic.process(self.vault, sha, policy)["state"], "published")
        item, = questions.apply(
            self.vault, sha,
            [{"new_question": "统计包含哪些设备？", "topic_id": 1, "relation": "SUPPORTS", "reason": "材料说明台数"}],
            confirm_new=True,
        )["questions"]
        identifier = item["id"]
        questions.update_note(self.vault, identifier, 1, "已有保存的讨论")
        note_path = self.vault / item["path"]
        note_path.write_text(note_path.read_text() + "手写意见：再核对统计口径。\n", encoding="utf-8")
        research = mcp.ResearchTools(self.vault, policy, allow_write=True)
        args = {"id": identifier, "expected_revision": 2, "body": "本次新增讨论"}
        first_save = call(research, "update_question", **args)
        saved = json.loads(first_save["content"][0]["text"])
        self.assertEqual((saved["id"], saved["revision"], saved["discussion_revision"]), (identifier, 3, 1))
        self.assertEqual(call(research, "update_question", **args), first_save)
        changed = call(research, "update_question", **{**args, "body": "过期并发更新"})
        self.assertEqual(changed["content"][0]["text"], "revision_changed")
        self.assertEqual(questions.read(self.vault, identifier, 2)["note"], "已有保存的讨论")
        result = note_path.read_text()
        self.assertIn("已有保存的讨论", result)
        self.assertIn("手写意见：再核对统计口径。", result)
        self.assertEqual(result.count("本次新增讨论"), 1)

        other = call(research, "update_question", id=identifier, expected_revision=3, body="另一条独立讨论")
        self.assertEqual(json.loads(other["content"][0]["text"])["revision"], 4)
        discovered = call(research, "read_question", id=identifier)["content"][0]["text"]
        self.assertNotIn("本次新增讨论", discovered)
        self.assertNotIn("手写意见", discovered)
        discussion, _ = json.loads(discovered)["discussions"]
        self.assertEqual(discussion["id"], saved["discussion_id"])
        self.assertEqual(discussion["sources"][0]["sha256"], sha)
        revise_args = {"id": identifier, "discussion_id": discussion["id"],
                       "expected_revision": 4, "replacement": "修订后的讨论"}
        readonly = call(mcp.ResearchTools(self.vault, policy), "revise_discussion", **revise_args)
        self.assertEqual(readonly["content"][0]["text"], "write_disabled")
        revised = call(research, "revise_discussion", **revise_args)
        self.assertEqual(json.loads(revised["content"][0]["text"]), {
            "id": identifier, "revision": 5, "discussion_id": discussion["id"], "discussion_revision": 2,
        })
        self.assertEqual(call(research, "revise_discussion", **revise_args), revised)
        self.assertEqual(questions.read(self.vault, identifier, 3)["discussions"][0]["body"], "本次新增讨论")
        reader = mcp.ResearchTools(self.vault, policy, allow_discussion_read=True)
        for revision, body in ((3, "本次新增讨论"), (5, "修订后的讨论")):
            output = call(reader, "read_discussion", id=identifier, discussion_id=discussion["id"], revision=revision)
            payload = json.loads(output["content"][0]["text"])
            self.assertEqual(payload["body"], body)
            self.assertEqual(payload["revision"], revision)
            self.assertNotIn("手写意见", output["content"][0]["text"])
            self.assertNotIn("已有保存的讨论", output["content"][0]["text"])
        result = note_path.read_text()
        self.assertNotIn("本次新增讨论", result)
        for text in ("修订后的讨论", "另一条独立讨论", "已有保存的讨论", "手写意见：再核对统计口径。"):
            self.assertIn(text, result)

    def test_discussion_requires_flag_and_approval_for_question_and_entry_sources(self):
        entry_id = "d-" + "1" * 32
        args = {"id": QUESTION["id"], "discussion_id": entry_id, "revision": 2}
        entry = {"id": entry_id, "revision": 1, "body": PRIVATE_MARKER,
                 "sources": [{"sha256": PRIVATE_SHA}]}
        research = mcp.ResearchTools(self.vault, self.policy, allow_discussion_read=True)
        with patch.object(questions, "read", return_value={**QUESTION, "discussions": [entry]}) as read:
            disabled = call(self.research, "read_discussion", **args)
            self.assertEqual(disabled["content"][0]["text"], "discussion_read_disabled")
            read.assert_not_called()
            denied = call(research, "read_discussion", **args)
            self.assertEqual(denied["content"][0]["text"], "local_only")

            entry["sources"] = [{"sha256": DEMO_SHA256}]
            read.return_value["links"] = QUESTION["links"] + [{"sha256": PRIVATE_SHA}]
            denied = call(research, "read_discussion", **args)
            self.assertEqual(denied["content"][0]["text"], "local_only")

    def test_discussion_pages_keep_the_pinned_question_revision(self):
        entry_id = "d-" + "1" * 32
        body = "合成讨论🙂" * mcp.ARTICLE_TEXT_BYTES
        source = {"sha256": DEMO_SHA256, "topic_id": 1, "topic_revision": "b" * 64}
        entry = {"id": entry_id, "revision": 1, "body": body, "sources": [source]}
        research = mcp.ResearchTools(self.vault, self.policy, allow_discussion_read=True)
        args = {"id": QUESTION["id"], "discussion_id": entry_id, "revision": QUESTION["revision"]}
        fragments = []
        with patch.object(questions, "read", return_value={**QUESTION, "discussions": [entry]}) as read:
            cursor = {}
            while cursor is not None:
                self.assertLess(len(fragments), 20)
                page = research.call("read_discussion", {**args, **cursor})
                self.assertLessEqual(len(page["body"].encode("utf-8")), mcp.ARTICLE_TEXT_BYTES)
                self.assertEqual(page["sources"], [source])
                fragments.append(page["body"])
                cursor = page["next"]
        self.assertEqual("".join(fragments), body)
        self.assertTrue(all(item.args == (self.vault, QUESTION["id"], QUESTION["revision"])
                            for item in read.call_args_list))

    def test_question_discovery_filters_unapproved_historical_discussion_sources(self):
        entry = {"id": "d-" + "1" * 32, "revision": 1, "created_at": "2026-11-22",
                 "updated_at": "2026-11-22", "body": PRIVATE_MARKER}
        source = {"sha256": DEMO_SHA256, "topic_id": 1, "topic_revision": "b" * 64}
        approved = {**entry, "sources": [{**source, "metadata": PRIVATE_MARKER}]}
        private = {**entry, "id": "d-" + "2" * 32, "sources": [{**source, "sha256": PRIVATE_SHA}]}
        with patch.object(questions, "read", return_value={**QUESTION, "discussions": [approved, private]}):
            result = self.research.call("read_question", {"id": QUESTION["id"]})
        self.assertEqual(result["discussions"], [{**{key: entry[key] for key in
                         ("id", "revision", "created_at", "updated_at")}, "sources": [source]}])

    def test_internal_errors_do_not_leak_paths(self):
        with patch.object(mcp.questions, "read", side_effect=OSError(PRIVATE_MARKER)):
            result = call(self.research, "read_question", id="q-1")
        self.assertEqual(result["content"][0]["text"], "research_data_unavailable")

    @staticmethod
    def message(method, identifier=None, **params):
        request = {"jsonrpc": "2.0", "method": method, "params": params}
        if identifier is not None:
            request["id"] = identifier
        return json.dumps(request)

    def stdio(self, requests, *args):
        command = [sys.executable, "-B", "-m", "afr_agent.mcp", "--vault", str(self.vault), *args]
        server = subprocess.run(command, input="\n".join(requests) + "\n", encoding="utf-8",
                                capture_output=True, check=True, timeout=10)
        self.assertEqual(server.stderr, "")
        return server.stdout

    def test_stdio_rejects_out_of_range_question_version_and_keeps_serving(self):
        message = self.message
        requests = [message("initialize", 1, protocolVersion="2025-06-18"),
                    message("notifications/initialized"),
                    message("tools/call", 2, name="read_question",
                            arguments={"id": "q-" + "1" * 32, "revision": mcp.REVISION["maximum"] + 1}),
                    message("ping", 3)]
        _, rejected, ping = map(json.loads, self.stdio(requests).splitlines())
        self.assertEqual(rejected["result"]["content"][0]["text"], "invalid_request")
        self.assertEqual(ping["result"], {})

    def test_stdio_initialize_and_tools_discovery(self):
        message = self.message
        requests = [
            message("initialize", 1, protocolVersion="2025-06-18", capabilities={},
                    clientInfo={"name": "synthetic test", "version": "1"}),
            message("notifications/initialized"),
            message("tools/list", 2),
            message("tools/call", 3, name="read_topic",
                    arguments={"article_sha": PRIVATE_SHA, "topic_id": 1}),
            "malformed input",
            message("ping", 4),
        ]
        for writable, discussion_read in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(writable=writable, discussion_read=discussion_read):
                args = (("--allow-write",) if writable else ()) + (
                    ("--allow-discussion-read",) if discussion_read else ())
                output = self.stdio(requests, *args)
                initialized, listed, denied, parse_error, ping = map(json.loads, output.splitlines())
                self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
                tools = {tool["name"] for tool in listed["result"]["tools"]}
                write_tools = {"update_question", "revise_discussion"}
                self.assertEqual(tools - write_tools - {"read_discussion"}, {
                                                        "list_articles", "read_article", "search_questions",
                                                        "read_question", "read_topic"})
                self.assertEqual(tools & write_tools, write_tools if writable else set())
                self.assertEqual("read_discussion" in tools, discussion_read)
                self.assertEqual(denied["result"]["content"][0]["text"], "local_only")
                self.assertEqual(parse_error["error"]["code"], -32700)
                self.assertEqual(ping["result"], {})
