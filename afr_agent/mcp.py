"""通过 Codex 的 MCP stdio 入口检索固定来源，按显式许可写入判断。"""

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys

from . import questions
from .library import RequestError
from .presentation import evidence_for
from .semantic import approved_versions, reviewed_plan


PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SHA_PATTERN = r"[0-9a-f]{64}"
SHA = {"type": "string", "pattern": f"^{SHA_PATTERN}$"}
DISCUSSION_ID = {"type": "string", "pattern": r"^d-[0-9a-f]{32}$"}
TEXT = {"type": "string", "minLength": 1}
REVISION = {"type": "integer", "minimum": 1, "maximum": (1 << 63) - 1}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
OFFSET = {"type": "integer", "minimum": 0}
ARTICLE_TEXT_BYTES = 12_000
TOPIC_CITATION_LIMIT = 100
DATA_ERRORS = (OSError, ValueError, TypeError, KeyError, sqlite3.Error)


def tool(name, description, properties, required, *, read_only=True):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": read_only, "openWorldHint": False},
    }


TOOLS = {
    spec["name"]: spec for spec in (
        tool("list_articles", "列出当前 Codex 会话获准使用的固定文章版本及其已校读的论述。", {}, []),
        tool("read_article", "分页读取获准固定原文。start 为从 1 开始的段号，offset 为段内字符位置；继续时使用 next。",
             {"article_sha": SHA, "start": REVISION, "limit": LIMIT, "offset": OFFSET},
             ["article_sha", "start", "limit"]),
        tool("search_questions", "检索可用于当前 Codex 会话的研究问题。",
             {"query": TEXT, "limit": LIMIT}, ["query", "limit"]),
        tool("read_question", "按 ID 和可选版本读取研究问题及其固定来源引用。",
             {"id": TEXT, "revision": REVISION}, ["id"]),
        tool("read_discussion", "分页读取已授权的 Codex 讨论。revision 是研究问题版本，使用 next 继续。",
             {"id": TEXT, "discussion_id": DISCUSSION_ID, "revision": REVISION, "offset": OFFSET},
             ["id", "discussion_id", "revision"]),
        tool("read_topic", "分页读取已获批准的固定论述及其引文。使用 next 继续，过长引文用 read_article 回读。",
             {"article_sha": SHA, "topic_id": REVISION, "offset": OFFSET, "citation_start": REVISION},
             ["article_sha", "topic_id"]),
        tool("update_question", "用户确认保存后，把本次讨论的 Markdown 追加到指定版本的研究问题笔记，保留此前内容。",
             {"id": TEXT, "expected_revision": REVISION, "body": TEXT},
             ["id", "expected_revision", "body"], read_only=False),
        tool("revise_discussion", "用户确认修改后，修订指定的一条 Codex 讨论并保留历史版本和其他笔记。"
             "先用 read_question 获取 discussion_id。",
             {"id": TEXT, "discussion_id": DISCUSSION_ID, "expected_revision": REVISION, "replacement": TEXT},
             ["id", "discussion_id", "expected_revision", "replacement"], read_only=False),
    )
}


def utf8_prefix(text, size):
    return text.encode("utf-8")[:size].decode("utf-8", errors="ignore")


def validate_arguments(schema, args):
    if not isinstance(args, dict):
        raise RequestError("invalid_request")
    properties = schema["properties"]
    if set(args) - properties.keys() or set(schema["required"]) - args.keys():
        raise RequestError("invalid_request")
    for key, value in args.items():
        field = properties[key]
        if field["type"] == "integer":
            if type(value) is not int or not field["minimum"] <= value <= field.get("maximum", value):
                raise RequestError("invalid_request")
        elif (not isinstance(value, str) or len(value) < field.get("minLength", 0)
              or ("pattern" in field and not re.fullmatch(field["pattern"], value))):
            raise RequestError("invalid_request")


class ResearchTools:
    def __init__(self, vault, approvals, allow_write=False, *, allow_discussion_read=False):
        self.vault = vault
        self.approvals = approvals
        self.allow_write = allow_write
        self.allow_discussion_read = allow_discussion_read

    def available_tools(self):
        return [spec for name, spec in TOOLS.items()
                if (spec["annotations"]["readOnlyHint"] or self.allow_write)
                and (name != "read_discussion" or self.allow_discussion_read)]

    @staticmethod
    def question_allowed(record, versions):
        links = record["links"]
        return bool(links) and all(link["sha256"] in versions for link in links)

    @staticmethod
    def discussion_allowed(discussion, versions):
        sources = discussion["sources"]
        return bool(sources) and all(source["sha256"] in versions for source in sources)

    @staticmethod
    def discussion_sources(discussion):
        return [{key: source[key] for key in ("sha256", "topic_id", "topic_revision")}
                for source in discussion["sources"]]

    def question_result(self, record, *, versions=None):
        result = {key: record[key] for key in ("id", "revision", "title")}
        if versions is not None:
            result["links"] = [
                {key: link[key] for key in ("sha256", "topic_id", "relation")}
                for link in record["links"]
            ]
            result["discussions"] = [
                {**{key: entry[key] for key in ("id", "revision", "created_at", "updated_at")},
                 "sources": self.discussion_sources(entry)}
                for entry in record.get("discussions", []) if self.discussion_allowed(entry, versions)
            ]
        return result

    def list_articles(self, versions):
        articles = []
        unavailable = []
        for sha in sorted(versions):
            try:
                article = self.article_summary(sha)
            except DATA_ERRORS:
                unavailable.append(sha)
            else:
                if article is not None:
                    articles.append(article)
        result = {"articles": articles}
        if unavailable:
            result["unavailable"] = unavailable
        return result

    def article_summary(self, sha):
        try:
            folder, evidence = evidence_for(self.vault, sha)
        except RequestError as exc:
            if str(exc) == "not_found":
                return None
            raise
        topics = []
        state = "review_pending" if evidence["parts"] else "requires_ocr"
        try:
            plan = reviewed_plan(self.vault, sha)
        except RequestError as exc:
            if str(exc) != "review_required":
                raise
        else:
            state = "reviewed"
            topics = [{"topic_id": number, "title": group["title"]}
                      for number, group in enumerate(plan["groups"], 1)]
        title = evidence["title"]
        if folder.relative_to(self.vault).as_posix() != evidence["folder"]:
            title = folder.name.rsplit(" · ", 1)[0]
        return {"sha256": sha, "title": title, "state": state, "topics": topics}

    def read_article(self, sha, start, limit, offset=0):
        folder, evidence = evidence_for(self.vault, sha)
        parts = evidence["parts"]
        if not parts:
            raise RequestError("requires_ocr")
        if start > len(parts) or (offset and offset >= len(parts[start - 1]["text"])):
            raise RequestError("invalid_request")
        remaining = ARTICLE_TEXT_BYTES
        page = []
        cursor = None
        for number in range(start, min(start + limit, len(parts) + 1)):
            part = parts[number - 1]
            suffix = part["text"][offset:]
            raw = suffix.encode("utf-8")
            fragment = raw[:remaining].decode("utf-8", errors="ignore")
            if not fragment and raw:
                cursor = {"start": number, "offset": offset}
                break
            page.append({
                "number": number,
                "offset": offset,
                "text": fragment,
                "path": f"{folder.relative_to(self.vault).as_posix()}/固定原文.md#^e{number}",
                "locator": part["locator"],
            })
            if len(fragment) < len(suffix):
                cursor = {"start": number, "offset": offset + len(fragment)}
                break
            remaining -= len(raw)
            offset = 0
        else:
            if number < len(parts):
                cursor = {"start": number + 1, "offset": 0}
        return {"sha256": sha, "parts": page, "total_parts": len(parts), "next": cursor}

    def read_topic(self, sha, topic_id, offset=0, citation_start=1):
        topic = questions.read_topic(self.vault, sha, topic_id)
        argument = topic["text"]
        citations = topic["citations"]
        if offset > len(argument) or citation_start > len(citations) + 1:
            raise RequestError("invalid_request")
        page = {key: topic[key] for key in ("sha256", "topic_id", "revision", "title", "path")}
        text = utf8_prefix(argument[offset:], ARTICLE_TEXT_BYTES)
        page.update(text=text, citations=[], next=None)
        next_offset = offset + len(text)
        if next_offset < len(argument):
            page["next"] = {"offset": next_offset, "citation_start": citation_start}
            return page

        remaining = ARTICLE_TEXT_BYTES - len(text.encode("utf-8"))
        start = citation_start - 1
        stop = min(start + TOPIC_CITATION_LIMIT, len(citations))
        for index in range(start, stop):
            citation = citations[index]
            size = len(citation["text"].encode("utf-8"))
            if size > ARTICLE_TEXT_BYTES:
                citation = {key: value for key, value in citation.items() if key != "text"}
                citation["read_article"] = {"article_sha": sha, "start": citation["number"], "limit": 1}
            elif size > remaining:
                stop = index
                break
            else:
                remaining -= size
            page["citations"].append(citation)
        if stop < len(citations):
            page["next"] = {"offset": next_offset, "citation_start": stop + 1}
        return page

    def call(self, name, args):
        if name not in TOOLS:
            raise RequestError("unknown_tool")
        validate_arguments(TOOLS[name]["inputSchema"], args)
        versions = approved_versions(self.approvals)
        if name == "list_articles":
            return self.list_articles(versions)
        if name == "search_questions":
            result = questions.search(self.vault, args["query"], args["limit"],
                                      visible=lambda record: self.question_allowed(record, versions))
            return {"hits": [self.question_result(record) for record in result["hits"]]}
        if name in ("read_article", "read_topic"):
            sha = args["article_sha"]
            if sha not in versions:
                raise RequestError("local_only")
            if name == "read_article":
                return self.read_article(sha, args["start"], args["limit"], args.get("offset", 0))
            return self.read_topic(sha, args["topic_id"], args.get("offset", 0),
                                   args.get("citation_start", 1))
        if name in ("read_question", "read_discussion"):
            if name == "read_discussion" and not self.allow_discussion_read:
                raise RequestError("discussion_read_disabled")
            record = questions.read(self.vault, args["id"], args.get("revision"))
            if not self.question_allowed(record, versions):
                raise RequestError("local_only")
            if name == "read_question":
                return self.question_result(record, versions=versions)
            discussion = next((item for item in record.get("discussions", [])
                               if item["id"] == args["discussion_id"]), None)
            if discussion is None:
                raise RequestError("not_found")
            if not self.discussion_allowed(discussion, versions):
                raise RequestError("local_only")
            offset = args.get("offset", 0)
            body = discussion["body"]
            if offset > len(body):
                raise RequestError("invalid_request")
            fragment = utf8_prefix(body[offset:], ARTICLE_TEXT_BYTES)
            next_offset = offset + len(fragment)
            return {
                "id": record["id"], "revision": record["revision"],
                "discussion_id": discussion["id"], "discussion_revision": discussion["revision"],
                "body": fragment,
                "sources": self.discussion_sources(discussion),
                "next": {"offset": next_offset} if next_offset < len(body) else None,
            }
        if not self.allow_write:
            raise RequestError("write_disabled")
        body = args["body"] if name == "update_question" else args["replacement"]
        if not body.strip():
            raise RequestError("invalid_request")
        # 确认该固定研究问题版本的全部来源，保存时由存储层检查版本是否变化。
        record = questions.read(self.vault, args["id"], args["expected_revision"])
        if not self.question_allowed(record, versions):
            raise RequestError("local_only")
        return questions.save_discussion(
            self.vault, args["id"], args["expected_revision"], body,
            discussion_id=args.get("discussion_id"),
        )


SAFE_ERRORS = {
    "unknown_tool", "invalid_request", "local_only", "write_disabled", "not_found",
    "revision_changed", "revision_conflict", "invalid_vault", "review_required",
    "article_publication_changed", "requires_ocr", "user_edit_conflict", "discussion_read_disabled",
}


def tool_result(research, params):
    try:
        if not isinstance(params.get("name"), str):
            raise RequestError("invalid_request")
        data = research.call(params["name"], params.get("arguments", {}))
        payload = json.dumps(data, ensure_ascii=False)
    except RequestError as exc:
        code = str(exc)
        message = code if code in SAFE_ERRORS else "research_data_unavailable"
        return {"content": [{"type": "text", "text": message}], "isError": True}
    except DATA_ERRORS:
        return {"content": [{"type": "text", "text": "research_data_unavailable"}], "isError": True}
    return {"content": [{"type": "text", "text": payload}]}


def rpc_error(identifier, code, message):
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


class Session:
    def __init__(self, research):
        self.research = research
        self.version = None
        self.ready = False

    def dispatch(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return rpc_error(None, -32600, "Invalid request")
        identifier = request.get("id")
        method = request.get("method")
        if not isinstance(method, str) or ("id" in request and type(identifier) not in (int, str)):
            return rpc_error(None, -32600, "Invalid request")
        if "id" not in request:
            if method == "notifications/initialized" and self.version:
                self.ready = True
            return None

        params = request.get("params", {})
        if not isinstance(params, dict):
            return rpc_error(identifier, -32602, "Invalid params")
        if method == "ping":
            result = {}
        elif method == "initialize" and self.version is None:
            version = params.get("protocolVersion")
            if not isinstance(version, str):
                return rpc_error(identifier, -32602, "Invalid params")
            self.version = version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            result = {
                "protocolVersion": self.version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "afr", "version": "1.0.0"},
            }
        elif not self.ready:
            return rpc_error(identifier, -32002, "Server not initialized")
        elif method == "tools/list":
            result = {"tools": self.research.available_tools()}
        elif method == "tools/call":
            result = tool_result(self.research, params)
        else:
            return rpc_error(identifier, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def receive(self, request):
        if not isinstance(request, list):
            return self.dispatch(request)
        if not request:
            return rpc_error(None, -32600, "Invalid request")
        # MCP 2025-03-26 and 2024-11-05 support JSON-RPC batches.
        replies = [reply for item in request if (reply := self.dispatch(item)) is not None]
        return replies or None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--approvals", type=Path)
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--allow-discussion-read", action="store_true")
    args = parser.parse_args()
    session = Session(ResearchTools(args.vault.resolve(), args.approvals, args.allow_write,
                                    allow_discussion_read=args.allow_discussion_read))
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError):
            response = rpc_error(None, -32700, "Parse error")
        else:
            response = session.receive(request)
        if response is not None:
            sys.stdout.buffer.write(json.dumps(response, ensure_ascii=True).encode("ascii") + b"\n")
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
