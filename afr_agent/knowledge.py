"""从签发的篇内白板提炼可回源的跨篇知识总结。"""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
import fcntl
import json
from math import ceil, log1p, sqrt
import os
from pathlib import Path
import re
import sqlite3
import unicodedata

from . import knowledge_hierarchy, locations, reading_canvas, semantic
from .board_style import ROOT_COLOR, branch_color
from .codex import measured, run_structured
from .intake import digest, exclusive_json, inside, plain, source_bytes, source_day
from .library import RequestError
from .presentation import card_body, evidence_for
from .questions import _write_projection, read_topic

ROOT = "知识总结"
INDEX = f"{ROOT}/知识总结索引.md"
OVERVIEW = f"{ROOT}/知识总结总白板.canvas"
STATE = ".afr/knowledge-summary"
STANDARD = Path(__file__).with_name("KNOWLEDGE-SUMMARY-STANDARD.md")
CONTRACT = "knowledge-summary-v1"
APPLICATION_REVISION = "resolve-evidence-card-and-preserve-canvas-v2"
VIEW_REVISION = 14
LONG_TIMELINE = 12
READING_BRANCH_COLORS = ("#9ab2bd", "#a3b59d", "#b2a6c5", "#bba997")
MAX_TOPICS = 18
MAX_EVENTS = 24
MAX_PAYLOAD = 115_000
MAX_HISTORY_PAYLOAD = 15_000
STAGES = ["意向", "审批", "签约", "支付", "实际建设", "投产", "定点", "样件", "量产",
          "交付", "试点", "生产部署", "推广", "其他", "未知"]
KINDS = ["事实", "观点", "预期", "更正", "撤回"]
INDEPENDENCE = ["独立", "转载", "待核"]
RELATIONS = ["新增", "转载", "佐证", "进展", "分歧"]
EVIDENCE_RELATIONS = {"转载", "佐证"}
EVENT_RELATION_FIELDS = {"relation", "related_event", "ref", "viewpoint_id"}
STRING = {"type": "string"}
INTEGER = {"type": "integer"}
EVENT_CARD_FIELDS = {"headline": 100, "context": 260, "estimate": 260, "to_verify": 180}
TOPIC_CARD_FIELDS = {"main_judgment": 120, "overview": 180, "follow_up": 160, "change_summary": 180}

DOMAINS = (
    ("technology", "科技与产业", "#8faec5", (
        ("semiconductors", "半导体与存储"), ("computing", "算力与通信基础设施"),
        ("ai-application", "AI 应用与商业化"), ("robot-investment", "具身 AI · 技术投入"),
        ("robot-components", "具身 AI · 核心零部件"), ("robot-deployment", "具身 AI · 现场部署"),
        ("emerging-industry", "生物制造与新产业"), ("resources", "资源、农业与公用事业"),
    )),
    ("markets", "宏观与金融", "#a6b78f", (
        ("money-credit", "货币、汇率与信用"), ("assets-market", "资产定价与市场策略"),
        ("public-finance", "地方财政与基建"),
    )),
    ("enterprises", "企业与投资", "#c4a28b", (
        ("business-model", "经营模式与转型"), ("enterprise-finance", "企业融资与项目"),
    )),
    ("governance", "治理与地缘", "#ae9fc3", (
        ("trade-policy", "贸易通道与政策"), ("geopolitics", "地缘冲突与外部影响"),
        ("regional-development", "区域发展与产业政策"),
    )),
    ("society", "社会与劳动", "#92b8b2", (
        ("work-employment", "劳动、就业与分配"), ("family-mobility", "家庭与机会"),
    )),
    ("methods", "认知与方法", "#c5a0ab", (
        ("research-method", "研究与证据"), ("organization", "组织、决策与协作"),
    )),
    ("unclassified", "待归类", ROOT_COLOR, (("unclassified", "待归类主题"),)),
)
CATEGORIES = {key: label for _, _, _, entries in DOMAINS for key, label in entries}


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _array(items):
    return {"type": "array", "items": items}


def _enum(values):
    return {**STRING, "enum": values}


EVENT = _object({
    "sha256": STRING, "topic_id": INTEGER, "evidence_number": INTEGER, "quote": STRING,
    "origin": STRING, "independence": _enum(INDEPENDENCE), "stage": _enum(STAGES),
    "date": STRING, "kind": _enum(KINDS), "claim": STRING, "supersedes": STRING,
    "relation": _enum(RELATIONS), "related_event": STRING, "ref": STRING, "viewpoint_id": STRING,
    **dict.fromkeys(EVENT_CARD_FIELDS, STRING),
})
READING_ARGUMENT = _object({"event_number": INTEGER, "title": STRING, "reason": STRING, "scope": STRING})
READING_BRANCH = _object({"title": STRING, "reason": STRING, "arguments": _array(READING_ARGUMENT)})
TOPIC = _object({"topic_id": STRING, "object": STRING, "dimension": STRING,
                 "category": _enum(list(CATEGORIES)),
                 **dict.fromkeys(TOPIC_CARD_FIELDS, STRING), "events": _array(EVENT),
                 "reading_branches": _array(READING_BRANCH)})
SCHEMA = _object({"topics": _array(TOPIC)})
PROPOSE = "第一轮。先查跨篇候选，再遍历篇内卡补漏。围绕具体对象和维度找有出处的新进展，单篇可建立值得持续跟踪的主题。最多 18 个主题，每主题最多 24 条事件。"
REVIEW = "第二轮。对照固定片段独立校读，另查候选遗漏。草案为空时仍逐组检查证据。最多 18 个主题，每主题最多 24 条事件。"
BEGIN = "<!-- AFR:KNOWLEDGE:BEGIN -->"
END = "<!-- AFR:KNOWLEDGE:END -->"


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in value if char.isalnum())


def _words(value):
    han = re.findall(r"[\u3400-\u9fff]{2,}", value)
    terms = {term[i:i + 2] for term in han for i in range(len(term) - 1)}
    return terms | set(re.findall(r"[a-z0-9]{2,}", value.casefold()))


def _short(value, budget):
    if len(value) <= budget:
        return value
    half = budget // 2
    return value[:half] + " … " + value[-half:]


def _publication_date(evidence):
    if evidence.get("published_at"):
        return evidence["published_at"]
    labeled_dates = set()
    for index, part in enumerate(evidence["parts"][:35]):
        label = r"(?:发布|发表|报告)日期" if index > 3 else r"(?:发布|发表|报告)?日期"
        match = re.match(rf"\s*{label}\s*[：:]\s*(\d{{4}})\s*(?:年|[-/.])\s*(\d{{1,2}})"
                         r"\s*(?:月|[-/.])\s*(\d{1,2})(?:\s*日|(?=\D|$))", part["text"])
        if match:
            try:
                labeled_dates.add(date(*(int(value) for value in match.groups())).isoformat())
            except ValueError:
                continue
    return next(iter(labeled_dates)) if len(labeled_dates) == 1 else None


def _lead_terms(text):
    # 连续三字和连续四字比单个汉字更能识别跨文档技术用语。
    normalized = unicodedata.normalize("NFKC", text).casefold()
    terms = set(re.findall(r"[a-z][a-z0-9+-]{1,}", normalized))
    for chunk in re.findall(r"[\u3400-\u9fff]+", normalized):
        for length in (2, 3, 4):
            terms.update(chunk[i:i + length] for i in range(len(chunk) - length + 1))
    return terms


def _cross_candidates(cards):
    card_terms = {key: _lead_terms(card["title"] + " " + card["summary"]) for key, card in cards.items()}
    titles = {key: _lead_terms(card["title"]) for key, card in cards.items()}
    counts = Counter(term for terms in card_terms.values() for term in terms)
    keys = sorted(cards)
    matches = defaultdict(list)
    for index, left in enumerate(keys):
        for right in keys[index + 1:]:
            if left[0] == right[0]:
                continue
            shared = {term for term in card_terms[left] & card_terms[right]
                      if (len(term) >= 3 or term.isascii()) and counts[term] < max(5, len(cards) // 4)}
            if not shared:
                continue
            weights = {term: log1p(len(cards) / counts[term]) * (1.6 if len(term) >= 4 else 1)
                       * (1.4 if term in titles[left] or term in titles[right] else 1)
                       for term in shared}
            ranked = sorted(shared, key=lambda term: (-weights[term], term))
            concrete = []
            for term in ranked:
                if not any(term in previous for previous in concrete):
                    concrete.append(term)
                if len(concrete) == 8:
                    break
            score = sum(weights[term] for term in concrete) / sqrt(8 + len(card_terms[left]) + len(card_terms[right]))
            matches[left[0], right[0]].append((score, left, right, concrete))
    # 对来源组合限额，再给低频来源入选的机会，避免高重复的两篇霸占预算。
    pool = sorted((item for pair in matches.values() for item in sorted(pair, reverse=True)[:3]), reverse=True)
    selected = []
    source_used = Counter()
    card_used = Counter()
    for _, left, right, concrete in pool:
        if any(source_used[key[0]] >= 12 or card_used[key] >= 3 for key in (left, right)):
            continue
        selected.append({"cards": [list(left), list(right)], "matching_terms": concrete})
        for key in (left, right):
            source_used[key[0]] += 1
            card_used[key] += 1
        if len(selected) == 32:
            break
    return selected


def _term_weight(text, terms):
    return sum(weight for term, weight in terms.items() if term in text.casefold())


def _evidence_excerpt(text, terms, budget):
    text = " ".join(text.split())
    if len(text) <= budget:
        return text
    stride = max(1, budget // 3)
    starts = [*range(0, len(text) - budget, stride), len(text) - budget]
    start = max(starts, key=lambda offset: _term_weight(text[offset:offset + budget], terms))
    return text[start:start + budget]


def _payload_size(value):
    return len(json.dumps(value, ensure_ascii=False))


def _spaced(items, count):
    if count >= len(items):
        return items
    if count == 1:
        return items[:1]
    return [items[index * (len(items) - 1) // (count - 1)] for index in range(count)]


def _card_directory(cards, limit):
    size = _payload_size(cards)
    while size > limit and len(cards) > 1:
        count = min(len(cards) - 1, max(1, len(cards) * limit // size))
        cards = _spaced(cards, count)
        size = _payload_size(cards)
    return cards


def _signed_reading_viewpoints(records, group, parts):
    for record in records:
        citations = []
        for citation in record["evidence"]:
            quote = semantic.viewpoint_source_quote(group, citation, parts)
            if quote is None:
                break
            citations.append({**citation, "quote": quote})
        else:
            yield {**record, "evidence": citations}


def _card_excerpts(card, group, parts, match_terms, viewpoints):
    terms = {term: 1 for term in _lead_terms(card["summary"]) if len(term) >= 3}
    terms.update({term: 3 for term in _lead_terms(card["title"]) if len(term) >= 3})
    terms.update({term: 7 for term in match_terms})
    covered = {citation["number"] for record in viewpoints for citation in record["evidence"]}
    budget = 250 if match_terms else 120
    ranked = []
    for number in range(group["start"], group["end"] + 1):
        raw = parts[number - 1]["text"]
        excerpt = _evidence_excerpt(raw, terms, budget)
        if not excerpt:
            continue
        score = _term_weight(excerpt, terms) + len(excerpt) / budget
        ranked.append((number not in covered, score, -number, {
            "number": number, "text": excerpt, "excerpt": len(excerpt) < len(" ".join(raw.split())),
        }))
    return [item[-1] for item in sorted(ranked, reverse=True)[:3 if match_terms else 2]]


def _allocate_card_details(entries, remaining):
    # 各来源每轮都有机会补一项。先给可见卡一段可引用的原文，再加入完整签发观点和辅助段。
    for position in range(3):
        queues = [iter(source[position]) for source in entries]
        while queues:
            next_queues = []
            for queue in queues:
                item = next(queue, None)
                if item is None:
                    continue
                card, key, value = item
                added = _payload_size(value) + 2
                if added <= remaining:
                    card[key].append(value)
                    remaining -= added
                next_queues.append(queue)
            queues = next_queues


def _article_payload(vault, scope):
    # 留出历史摘要、跨篇候选和独立校读时的提案空间。
    article_budget = 64_000
    directory_share = 25_000 // len(scope)
    records = []
    cards = {}
    sources = {}
    for sha in sorted(scope):
        source = evidence_for(vault, sha)
        _, evidence = source
        plan = semantic.reviewed_plan(vault, sha, source=source)
        supplements = semantic.card_supplements(vault, sha, evidence, plan)
        all_cards = []
        for number, group in enumerate(plan["groups"], 1):
            supplement = supplements.get(number)
            summary = supplement["summary"] if supplement else card_body(evidence, group)[0]
            all_cards.append({
                "id": number, "title": _short(group["title"], 100), "summary": _short(summary, 120),
                "paragraphs": [group["start"], group["end"]], "evidence": [],
            })
        article_cards = _card_directory(all_cards, directory_share)
        cards.update({(sha, card["id"]): card for card in article_cards})
        sources[sha] = (plan, evidence["parts"], evidence["title"])
        opening = " ".join(part["text"] for part in evidence["parts"][:2])
        article = {"sha256": sha, "plan_sha256": plan["plan_sha256"],
                   "title": _short(evidence["title"], 180), "published_at": _publication_date(evidence),
                   "opening": _short(opening, 240), "cards": article_cards}
        if omitted := len(all_cards) - len(article_cards):
            article["omitted_cards"] = omitted
        records.append(article)

    candidates = []
    clues = defaultdict(set)
    candidate_budget = 6_000
    for pair in _cross_candidates(cards):
        size = _payload_size(pair) + 2
        if size > candidate_budget:
            continue
        candidate_budget -= size
        candidates.append(pair)
        for key in pair["cards"]:
            clues[tuple(key)].update(pair["matching_terms"])

    entries = []
    for article in records:
        sha = article["sha256"]
        plan, parts, _ = sources[sha]
        signed = defaultdict(list)
        for record in semantic.article_viewpoints(plan):
            if (sha, record["topic_id"]) in cards:
                signed[record["topic_id"]].append(record)
        first, preferred, extra = [], [], []
        for card in sorted(article["cards"], key=lambda item: not clues[sha, item["id"]]):
            number = card["id"]
            group = plan["groups"][number - 1]
            viewpoints = list(_signed_reading_viewpoints(signed[number], group, parts))
            excerpts = _card_excerpts(card, group, parts, clues[sha, number], viewpoints)
            first.extend((card, "evidence", item) for item in excerpts[:1])
            extra.extend((card, "evidence", item) for item in excerpts[1:])
            if signed[number]:
                card["viewpoints"] = []
                card["omitted_viewpoints"] = len(signed[number])
                preferred.extend((card, "viewpoints", item) for item in viewpoints)
        entries.append((first, preferred, extra))

    # 每次追加按完整 JSON 大小扣费，并预留每卡的省略提示。
    _allocate_card_details(entries, article_budget - _payload_size(records) - 38 * len(cards))
    pointers = {}
    for article in records:
        sha = article["sha256"]
        _, _, title = sources[sha]
        for card in article["cards"]:
            passages = defaultdict(list)
            viewpoints = defaultdict(lambda: defaultdict(list))
            for record in card.get("viewpoints", ()):
                for citation in record["evidence"]:
                    number, quote = citation["number"], citation["quote"]
                    passages[number].append(quote)
                    viewpoints[number][record["id"]].append(quote)
            for part in card["evidence"]:
                passages[part["number"]].append(part["text"])
            if "omitted_viewpoints" in card:
                card["omitted_viewpoints"] -= len(card["viewpoints"])
                if not card["omitted_viewpoints"]:
                    del card["omitted_viewpoints"]
            start, end = card["paragraphs"]
            if omitted := end - start + 1 - len(passages):
                card["omitted_paragraphs"] = omitted
            for number, quotes in passages.items():
                pointer = (title, " … ".join(dict.fromkeys(quotes)))
                if viewpoints[number]:
                    pointer += ({key: tuple(items) for key, items in viewpoints[number].items()},)
                pointers[sha, card["id"], number] = pointer
    return records, pointers, candidates


def _topic_digest(topics, articles, scope, new_sources=None):
    def article_terms(items):
        return _words(" ".join(article["title"] + " " +
                               " ".join(card["title"] + " " + card["summary"] for card in article["cards"])
                               for article in items))

    terms = article_terms(articles)
    incoming = article_terms(article for article in articles if new_sources is None
                             or article["sha256"] in new_sources)
    ranked = []
    for identifier, topic in topics.items():
        events = [event for event in topic["events"] if event["sha256"] in scope]
        # 每次只发送本批许可证据。主题名也须能由本批材料词项支持。
        labels = _words(topic["object"] + " " + topic["dimension"])
        hits = len(terms & labels)
        if events and hits >= max(1, (len(labels) + 1) // 2):
            # 旧的原始判断也可能是新佐证或更正的目标，优先提供与来料有关的记录。
            relevant = sorted(enumerate(events), key=lambda pair: (
                len(incoming & _words(pair[1]["claim"] + " " + pair[1].get("origin", ""))), pair[0]),
                reverse=True)[:6]
            ranked.append((len(incoming & labels), hits, identifier, topic, relevant))

    def event_digest(event, visible_ids):
        detail = {key: event[key] for key in
                  ("id", "date", "stage", "kind", "claim", "origin", "independence", "sha256")}
        if event.get("supersedes") in visible_ids:
            detail["supersedes"] = event["supersedes"]
        if event.get("related_event") in visible_ids:
            detail.update(relation=event["relation"], related_event=event["related_event"])
        links = [{key: evidence[key] for key in ("sha256", "relation", "origin")}
                 for evidence in event.get("evidence_links", ()) if evidence["sha256"] in scope]
        if links:
            detail["evidence_links"] = links[-3:]
        return detail

    def topic_digest(identifier, topic, ranked_events, limit):
        events = [event for _, event in sorted(ranked_events[:limit])]
        visible_ids = {event["id"] for event in events}
        return {"id": identifier, "object": topic["object"], "dimension": topic["dimension"],
                "category": _category(topic),
                "recent_events": [event_digest(event, visible_ids) for event in events]}

    selected = sorted(ranked, reverse=True)[:12]
    for limit in range(6, 0, -1):
        result = [topic_digest(identifier, topic, events, limit) for _, _, identifier, topic, events in selected]
        if len(json.dumps(result, ensure_ascii=False)) <= MAX_HISTORY_PAYLOAD:
            return result
    return result


def _bounded_string(value, maximum, *, empty=False):
    return (isinstance(value, str) and (empty or bool(value)) and value == value.strip()
            and len(value) <= maximum and not any(ord(char) < 32 for char in value))


def _valid_date(value):
    if value == "":
        return True
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        return False
    try:
        date.fromisoformat(value + {4: "-01-01", 7: "-01", 10: ""}[len(value)])
    except ValueError:
        return False
    return True


def _topic_id(subject, dimension):
    return "k-" + semantic.fingerprint([_normalized(subject), _normalized(dimension)])[:24]


def _validated_event(event, pointers, prior_events):
    fields = set(EVENT["properties"])
    if (not isinstance(event, dict)
            or not fields - EVENT_CARD_FIELDS.keys() - EVENT_RELATION_FIELDS <= set(event) <= fields
            or any(not _bounded_string(event.get(key, ""), size, empty=True)
                   for key, size in EVENT_CARD_FIELDS.items())):
        raise RequestError("invalid_knowledge_event")
    if (not isinstance(event["sha256"], str) or type(event["topic_id"]) is not int
            or type(event["evidence_number"]) is not int):
        raise RequestError("invalid_knowledge_event")
    pointer = (event["sha256"], event["topic_id"], event["evidence_number"])
    supplied = pointers.get(pointer)
    if supplied is None and event["topic_id"] == 0:
        # 模型偶尔用 0 占位。仅在同篇且同段的签发卡唯一时恢复编号。
        matches = [(key, value) for key, value in pointers.items()
                   if key[0] == event["sha256"] and key[2] == event["evidence_number"]]
        if len(matches) == 1:
            pointer, supplied = matches[0]
            event = {**event, "topic_id": pointer[1]}
    if supplied is None:
        raise RequestError("invalid_knowledge_event")
    article_title, excerpt, *extra = supplied
    viewpoint_id = event.get("viewpoint_id", "")
    signed_quotes = extra[0] if extra else {}
    if (not _bounded_string(event["quote"], 220) or len(event["quote"]) < 4
            or " … " in event["quote"] or event["quote"] not in " ".join(excerpt.split())
            or not _bounded_string(event["claim"], 500)
            or not _bounded_string(event["origin"], 100, empty=True)
            or event["independence"] not in INDEPENDENCE
            or event["stage"] not in STAGES or event["kind"] not in KINDS
            or not _valid_date(event["date"])
            or not _bounded_string(event["supersedes"], 64, empty=True)
            or event["supersedes"] and (event["supersedes"] not in prior_events
                                        or event["kind"] not in ("更正", "撤回"))
            or event.get("relation", "新增") not in RELATIONS
            or not _bounded_string(viewpoint_id, 26, empty=True)
            or viewpoint_id and not any(event["quote"] in " ".join(quote.split())
                                        for quote in signed_quotes.get(viewpoint_id, ()))
            or not _bounded_string(event.get("related_event", ""), 64, empty=True)
            or not _bounded_string(event.get("ref", ""), 40, empty=True)
            or event.get("ref") and not re.fullmatch(r"r-[a-z0-9-]+", event["ref"])):
        raise RequestError("invalid_knowledge_event")
    if event["independence"] == "独立" and not _normalized(event["origin"]):
        raise RequestError("invalid_knowledge_event")
    stable_fields = [*pointer, event["stage"], event["kind"], event["date"], _normalized(event["claim"])]
    if event.get("relation") in EVIDENCE_RELATIONS:
        stable_fields.extend((_normalized(event["origin"]), _normalized(event["quote"])))
    return {**event, "id": "e-" + semantic.fingerprint(stable_fields)[:24], "article_title": article_title}


def _related_events(raw_events, pointers, permitted, eligible_sources=None):
    refs = [event.get("ref") for event in raw_events if isinstance(event, dict) and event.get("ref")]
    if any(not isinstance(ref, str) for ref in refs) or len(refs) != len(set(refs)):
        raise RequestError("invalid_knowledge_event")
    available = set(permitted).union(refs)
    events = [_validated_event(event, pointers, available) for event in raw_events]
    if len({event["id"] for event in events}) != len(events):
        raise RequestError("invalid_knowledge_event")
    lookup = {**permitted, **{event["ref"]: event for event in events if event.get("ref")}}
    targets = {}
    support_origins = defaultdict(set)
    for event in events:
        if event.get("relation") == "佐证" and (target := lookup.get(event.get("related_event", ""))):
            support_origins[target["id"]].add(_normalized(event["origin"]))
    for event in events:
        relation = event.get("relation", "新增")
        related = event.get("related_event", "")
        supersedes = event["supersedes"]
        if ((bool(related) != (relation != "新增"))
                or related and supersedes
                or relation in ("进展", "分歧") and event["kind"] in ("更正", "撤回")
                or relation == "转载" and event["independence"] != "转载"
                or relation == "佐证" and event["independence"] != "独立"
                or "relation" in event and event["independence"] == "转载" and relation != "转载"):
            raise RequestError("invalid_knowledge_event")
        reference = related or supersedes
        if not reference:
            continue
        target = lookup.get(reference)
        if (target is None or target["id"] == event["id"]
                or target["independence"] == "转载"
                or target.get("relation") in EVIDENCE_RELATIONS
                or reference not in permitted and eligible_sources is not None
                and target["id"] not in permitted and target["sha256"] not in eligible_sources):
            raise RequestError("invalid_knowledge_event")
        if relation in EVIDENCE_RELATIONS:
            origin = _normalized(event["origin"])
            same_origin = origin == _normalized(target["origin"])
            linked_origins = {_normalized(proof["origin"]) for proof in target.get("evidence_links", ())}
            if (relation == "佐证" and (same_origin or event["sha256"] == target["sha256"])
                    or relation == "转载" and (not origin or not (
                        same_origin or origin in linked_origins or origin in support_origins[target["id"]]))):
                raise RequestError("invalid_knowledge_event")
        targets[event["id"]] = target["id"]
        event["related_event" if related else "supersedes"] = target["id"]
    for identifier in targets:
        visited = {identifier}
        current = identifier
        while current in targets:
            current = targets[current]
            if current in visited:
                raise RequestError("invalid_knowledge_event")
            visited.add(current)
    return events


def _topic_updates(result, pointers, known, catalog, new_sources, *, reviewed):
    if (not isinstance(result, dict) or set(result) != {"topics"}
            or not isinstance(result["topics"], list) or len(result["topics"]) > MAX_TOPICS):
        raise RequestError("invalid_knowledge_summary")
    updates = {}
    deferred = 0
    for topic in result["topics"]:
        try:
            fields = set(TOPIC["properties"])
            if (not isinstance(topic, dict) or not fields - TOPIC_CARD_FIELDS.keys() - {"category", "reading_branches"} <= set(topic) <= fields
                    or "category" in topic and (not isinstance(topic["category"], str)
                                                  or topic["category"] not in CATEGORIES)
                    or any(not _bounded_string(topic.get(key, ""), size, empty=True)
                           for key, size in TOPIC_CARD_FIELDS.items())
                    or not _bounded_string(topic["topic_id"], 26, empty=True)
                    or not _bounded_string(topic["object"], 100)
                    or not _bounded_string(topic["dimension"], 100)
                    or not _normalized(topic["object"]) or not _normalized(topic["dimension"])
                    or not isinstance(topic["events"], list) or not 1 <= len(topic["events"]) <= MAX_EVENTS):
                raise RequestError("invalid_knowledge_topic")
            identifier = topic["topic_id"] or _topic_id(topic["object"], topic["dimension"])
            if identifier in updates or topic["topic_id"] and identifier not in catalog:
                raise RequestError("invalid_knowledge_topic")
            prior = known.get(identifier)
            if prior and topic["topic_id"] and (topic["object"], topic["dimension"]) != (
                    prior["object"], prior["dimension"]):
                raise RequestError("invalid_knowledge_topic")
            permitted = {event["id"]: event for event in catalog.get(identifier, {}).get("recent_events", ())}
            events = _related_events(topic["events"], pointers, permitted, new_sources if prior else None)
            if reviewed and not any(event["sha256"] in new_sources and (
                    event["independence"] != "转载" or event.get("relation") == "转载") for event in events):
                raise RequestError("insufficient_knowledge_evidence")
            updates[identifier] = {"object": topic["object"], "dimension": topic["dimension"],
                                   "category": _category(topic),
                                   "events": events, "permitted_event_ids": set(permitted),
                                   "reading_branches": knowledge_hierarchy.candidate(
                                       topic.get("reading_branches"), events, _normalized),
                                   **{key: topic[key] for key in TOPIC_CARD_FIELDS
                                                        if topic.get(key)}}
        except RequestError:
            deferred += 1
    return updates, deferred


def _same_origin_claim(event, previous):
    return (event["kind"] == previous["kind"] and event["stage"] == previous["stage"]
            and event["date"] == previous["date"]
            and bool(_normalized(event["origin"]))
            and _normalized(event["origin"]) == _normalized(previous["origin"])
            and _normalized(event["claim"]) == _normalized(previous["claim"]))


def _evidence_key(event):
    return event["sha256"], event["topic_id"], event["evidence_number"]


def _recorded_event(event, history):
    return next((previous for previous in history.values() if event["id"] == previous["id"] or (
        _evidence_key(event) == _evidence_key(previous)
        and event["stage"] == previous["stage"] and event["kind"] == previous["kind"]
        and event["date"] == previous["date"]
        and _normalized(event["quote"]) == _normalized(previous["quote"])
        and _normalized(event["claim"]) == _normalized(previous["claim"])
    )), None)


def _evidence_identity(event):
    key = [*_evidence_key(event), _normalized(event["quote"]), _normalized(event["origin"])]
    return "p-" + semantic.fingerprint(key)[:24]


def _attach_evidence(history, target, event, relation, stamp):
    original = history[target]
    identifier = _evidence_identity(event)
    links = original.get("evidence_links", ())
    if identifier == _evidence_identity(original) or any(identifier == _evidence_identity(link) for link in links):
        return None
    if relation == "佐证" and any(link["relation"] == "佐证"
                              and _normalized(link["origin"]) == _normalized(event["origin"]) for link in links):
        relation = "转载"
    evidence = {key: event[key] for key in ("sha256", "topic_id", "evidence_number", "quote",
                                           "article_title", "origin", "card_path", "source_path", "claim",
                                           "published_at", "admitted_at", "article_revision")}
    if event.get("viewpoint_id"):
        evidence["viewpoint_id"] = event["viewpoint_id"]
    history[target] = {**original, "evidence_links": [*links, {
        **evidence, "id": identifier, "relation": relation, "recorded_at": stamp,
    }]}
    return identifier, relation


def _merge_events(history, fresh, comparable, stamp):
    history = {event["id"]: event for event in history}
    aliases = {}
    attachments = []
    new_events = {}
    linked = []
    references = {event.get("related_event") or event["supersedes"] for event in fresh}
    for event in fresh:
        relation = event.get("relation", "新增")
        if relation in EVIDENCE_RELATIONS:
            attachments.append((event["related_event"], event, relation))
            continue
        recorded = _recorded_event(event, history)
        if recorded:
            aliases[event["id"]] = recorded["id"]
            continue
        linked_before = next((previous["id"] for previous in history.values()
                              for proof in previous.get("evidence_links", ())
                              if _evidence_key(event) == _evidence_key(proof)
                              and _normalized(event["claim"]) == _normalized(proof.get("claim", ""))), "")
        if linked_before and relation == "新增" and not event["supersedes"] and event["id"] not in references:
            aliases[event["id"]] = linked_before
            continue
        # 相同署名、措辞与口径的再次引用沿用原判断；较早的证据必须在本次比较范围。
        original = next((previous["id"] for previous in history.values()
                         if previous["id"] in comparable and _same_origin_claim(event, previous)), "")
        if original and relation == "新增" and not event["supersedes"] and (
                event["id"] not in references or original not in new_events):
            aliases[event["id"]] = original
            attachments.append((original, event, "转载"))
            continue
        record = {key: value for key, value in event.items() if key != "ref"}
        history[event["id"]] = new_events[event["id"]] = {**record, "recorded_at": stamp}
        comparable.add(event["id"])
    for event in new_events.values():
        for field in ("related_event", "supersedes"):
            target = aliases.get(event.get(field))
            if target:
                event[field] = target
    for target, event, relation in attachments:
        target = aliases.get(target, target)
        if target in history:
            if attached := _attach_evidence(history, target, event, relation, stamp):
                evidence_id, attached_relation = attached
                linked.append({"event_id": target, "evidence_id": evidence_id, "sha256": event["sha256"],
                               "card_id": event["topic_id"], "evidence_number": event["evidence_number"],
                               "relation": attached_relation})
    return list(history.values()), list(new_events), linked


def _admitted_at(vault, shas):
    path = inside(vault, ".afr/workflow.sqlite")
    if not path.is_file() or not shas:
        return {}
    parameters = ",".join("?" for _ in shas)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow'").fetchone():
            return {}
        rows = db.execute(f"SELECT sha256, admitted FROM workflow WHERE sha256 IN ({parameters})", tuple(shas))
        return {sha: datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds")
                for sha, when in rows if type(when) is int and 0 < when < 253402300800}


def _signed_viewpoint_quotes(vault, sha, source):
    plan = semantic.reviewed_plan(vault, sha, source=source)
    _, evidence = source
    quotes = defaultdict(list)
    for record in semantic.article_viewpoints(plan):
        card = record["topic_id"]
        for citation in record["evidence"]:
            quote = semantic.viewpoint_source_quote(plan["groups"][card - 1], citation, evidence["parts"])
            if quote:
                quotes[card, citation["number"], record["id"]].append(quote)
    return quotes


def _checked_evidence(vault, events):
    cache = {}
    publications = {}
    signed_viewpoints = {}
    tags = {event["sha256"] for event in events if event.get("viewpoint_id")}
    mapping = locations.load(vault)
    admission = _admitted_at(vault, {event["sha256"] for event in events})
    for event in events:
        sha, card, number = _evidence_key(event)
        key = sha, card
        if key not in cache:
            cache[key] = read_topic(vault, *key)
        topic = cache[key]
        citation = next((item for item in topic["citations"] if item["number"] == number), None)
        if not citation or event["quote"] not in " ".join(citation["text"].split()):
            raise RequestError("knowledge_evidence_changed")
        if sha not in publications:
            source = evidence_for(vault, sha)
            publications[sha] = _publication_date(source[1])
            if sha in tags:
                signed_viewpoints[sha] = _signed_viewpoint_quotes(vault, sha, source)
        if viewpoint_id := event.get("viewpoint_id"):
            quotes = signed_viewpoints[sha].get((card, number, viewpoint_id), ())
            if not any(event["quote"] in quote for quote in quotes):
                raise RequestError("knowledge_evidence_changed")
        for label, path in (("card_path", topic["path"]), ("source_path", citation["path"])):
            event[label] = locations.resolve(path, mapping, reverse=True)
        event["published_at"] = publications[sha]
        event["article_revision"] = topic["revision"]
        event["admitted_at"] = admission.get(sha)


def _apply(vault, state, updates, new_sources):
    added = 0
    touched = []
    event_ids = []
    evidence_links = []
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for identifier, item in updates.items():
        prior = state["topics"].get(identifier)
        previous = prior["events"] if prior else []
        fresh = [event for event in item["events"]
                 if (event["independence"] != "转载" or event.get("relation") == "转载")
                 and (not prior or event["sha256"] in new_sources)]
        _checked_evidence(vault, fresh)
        history, new_records, links = _merge_events(
            previous, fresh, set(item.get("permitted_event_ids", ())), stamp)
        previous_count = len(previous)
        if not history or not fresh:
            continue
        touched.append(identifier)
        event_ids.extend(new_records)
        evidence_links.extend({"topic_id": identifier, **link} for link in links)
        if len(history) == previous_count:
            if links:
                state["topics"][identifier] = {**prior, "events": history}
        else:
            added += len(history) - previous_count
            record = prior or {"object": item["object"], "dimension": item["dimension"],
                               "color_index": max((topic["color_index"] for topic in state["topics"].values()),
                                                  default=-1) + 1}
            category = _category(record)
            if category == "unclassified":
                category = _category(item)
            refreshed = {"change_summary", "reading_card", "reading_hierarchy", "reading_points", "reading_preview"}
            record = {key: value for key, value in record.items() if key not in refreshed}
            topic = {**record, "events": history, "category": category, "native_reading": True,
                     **{key: item[key] for key in TOPIC_CARD_FIELDS if key in item}}
            hierarchy = knowledge_hierarchy.admitted(
                _title(topic), _topic_overview(topic), item.get("reading_branches", []), history, new_records,
                _normalized, prior=(prior or {}).get("reading_hierarchy"))
            if hierarchy:
                topic["reading_hierarchy"] = hierarchy
                topic["retain_full_timeline"] = True
            elif prior and not prior.get("reading_hierarchy") and previous_count > LONG_TIMELINE:
                topic["retain_full_timeline"] = True
            previous_branches = (prior or {}).get("reading_hierarchy", {}).get("branches", ())
            retired = set(topic.get("retired_reading_branches", ()))
            retired.update(branch.get("label", branch["id"]) for branch in previous_branches)
            retired.difference_update(branch.get("label", branch["id"])
                                      for branch in (hierarchy or {}).get("branches", ()))
            if retired:
                topic["retired_reading_branches"] = sorted(retired)
            else:
                topic.pop("retired_reading_branches", None)
            if prior:
                topic["branch_labels"] = _branch_labels(prior)
            if labels := _branch_labels(topic):
                topic["branch_labels"] = labels
            state["topics"][identifier] = topic
    return sorted(touched), added, list(dict.fromkeys(event_ids)), evidence_links


def _title(topic):
    return f"{topic['object']} · {topic['dimension']}"


def _category(topic):
    value = topic.get("category")
    return value if isinstance(value, str) and value in CATEGORIES else "unclassified"


def _markdown(title, lines):
    return (f"# {plain(title)}\n\n" + "\n".join(lines) + "\n").encode("utf-8")


def _event_links(event, mapping):
    card = locations.resolve(event["card_path"], mapping)
    source = locations.resolve(event["source_path"], mapping)
    links = [f"[[{card}|篇内卡]]", f"[[{source}|固定原文]]"]
    links.extend(f"[[{locations.resolve(item, mapping)}|原文 {item.rsplit('#^', 1)[-1]}]]"
                 for item in event.get("support_paths", ()))
    for evidence in event.get("evidence_links", ()):
        label = "独立佐证" if evidence["relation"] == "佐证" else "转载来源"
        source = locations.resolve(evidence["source_path"], mapping)
        origin = f"（{plain(evidence['origin'])}）" if evidence.get("origin") else ""
        links.append(f"[[{source}|{label}]]{origin}")
    return " · ".join(links)


def _source_label(event):
    modified_ns = event.get("source_modified_ns")
    if modified_ns is None:
        return "原件修改时间待核"
    return source_day(modified_ns).strftime("%Y年%m月%d日")


def _source_order(event):
    modified_ns = event.get("source_modified_ns")
    return (modified_ns is None, modified_ns or 0)


def _headline(event):
    return event.get("reading_headline") or event.get("headline") or _short(re.split(r"[。！？]", event["claim"], maxsplit=1)[0], 90)


def _topic_overview(topic):
    return topic.get("overview") or f"跟踪{topic['object']}的{topic['dimension']}。"


def _record_details(event):
    lines = []
    if _normalized(_headline(event)) != _normalized(event["claim"]):
        lines.append(plain(event["claim"]))
    for key, label in (("context", "为什么关注"), ("estimate", "测算"), ("to_verify", "待核对")):
        if event.get(key):
            lines.append(f"**{label}**　{plain(event[key])}")
    return lines


def _card_height(text):
    # 以 540px 卡片估算换行，中文和宽字符按两格计算。
    height = 42
    for line in text.splitlines():
        display = re.sub(r"\[\[[^\]\n]+\|([^\]]+)\]\]", r"\1", line).replace("\\", "")
        columns = sum(2 if unicodedata.east_asian_width(ch) in {"F", "W", "A"} else 1 for ch in display)
        height += 17 if not display else ceil(columns / 48) * (42 if line.startswith("##") else 25)
    return max(180, height)


def _filename(title):
    safe = unicodedata.normalize("NFC", title).translate(str.maketrans({
        "/": "／", "\\": "＼", ":": "：", "*": "＊", "?": "？", '"': "＂",
        "<": "＜", ">": "＞", "|": "｜", "[": "［", "]": "］", "#": "＃",
        "^": "＾", "%": "％",
    }))
    safe = " ".join("".join(" " if unicodedata.category(char) == "Cc" else char for char in safe).split())
    return safe.encode("utf-8")[:180].decode("utf-8", "ignore").strip(" .") or "知识主题"


def _topic_name_key(name):
    return unicodedata.normalize("NFD", name).casefold()


def _topic_stems(topics):
    used = {_topic_name_key(name) for name in ("分类", "知识总结索引", "知识总结总白板")}
    stems = {}
    for identifier, topic in sorted(topics.items(), key=lambda entry: (entry[1]["color_index"], entry[0])):
        title = _filename(_title(topic))
        stem, number = title, 2
        while _topic_name_key(stem) in used:
            stem = f"{title}（{number}）"
            number += 1
        stems[identifier] = stem
        used.add(_topic_name_key(stem))
    return stems


def _topic_path_map(topics):
    return {identifier: f"{CATEGORIES[_category(topics[identifier])]}/{stem}"
            for identifier, stem in _topic_stems(topics).items()}


def _topic_paths(identifier, topic_paths=None):
    folder = identifier if topic_paths is None else topic_paths[identifier]
    title = folder.rsplit("/", 1)[-1]
    stem = f"{ROOT}/{folder}/{title}" if "/" in folder else f"{ROOT}/{folder}"
    return f"{stem}.md", f"{stem}.canvas"


def _topic_details_folder(canvas):
    stem = canvas.removesuffix(".canvas")
    parent, _, title = stem.rpartition("/")
    return parent if len(parent.split("/")) == 3 and parent.rsplit("/", 1)[-1] == title else stem


def _full_timeline_path(canvas):
    stem = canvas.removesuffix(".canvas")
    title = stem.rsplit("/", 1)[-1]
    if re.fullmatch(r"k-[a-z0-9-]+", title):
        return f"{stem}-完整.canvas"
    return f"{_topic_details_folder(canvas)}/时间线/{title} · 完整时间线.canvas"


def _argument_paths(canvas, events):
    folder = f"{_topic_details_folder(canvas)}/论述"
    names = set()
    paths = {}
    # 判断正文参与记录身份。卡片标题和分支措辞调整时，详情入口保持不变。
    for event in events:
        title = _filename(re.split(r"[。！？]", event["claim"], maxsplit=1)[0] or event["claim"])
        name, suffix = title, 1
        while _topic_name_key(name) in names:
            suffix += 1
            name = f"{title} · {suffix}"
        names.add(_topic_name_key(name))
        paths[event["id"]] = f"{folder}/{name}.canvas"
    return paths


def _branch_path(canvas, branch, label=None):
    if label is None:
        label = _filename(branch.get("label") or branch.get("match_title") or branch.get("title") or branch["id"])
    folder = _topic_details_folder(canvas)
    if folder != canvas.removesuffix(".canvas"):
        folder += "/分支"
    return f"{folder}/{label}.canvas"


def _branch_labels(topic):
    labels = dict(topic.get("branch_labels", {}))
    used = {_topic_name_key(label) for label in labels.values()}
    candidates = {
        branch.get("label", branch["id"]): branch.get("label", branch.get("match_title", branch["title"]))
        for branch in topic.get("reading_hierarchy", {}).get("branches", ())
    }
    for retired in topic.get("retired_reading_branches", ()):
        candidates.setdefault(retired, "历史论述" if re.fullmatch(r"[a-f0-9]{16}", retired) else retired)
    for identifier, title in candidates.items():
        if identifier in labels:
            continue
        title = _filename(title)
        label, number = title, 2
        while _topic_name_key(label) in used:
            label = f"{title}（{number}）"
            number += 1
        labels[identifier] = label
        used.add(_topic_name_key(label))
    return labels


def _related_record_link(event, paths):
    label = {"进展": "此前记录", "分歧": "对照另一判断"}.get(event.get("relation"))
    target = paths.get(event.get("related_event"))
    return f"[[{target}|{label}]]" if label and target else ""


def _argument_source_context(vault, event, cache):
    number = event.get("evidence_number")
    sha = event.get("sha256")
    if vault is None or not sha or type(number) is not int or number < 1:
        return ""
    if sha not in cache:
        try:
            _, evidence = evidence_for(vault, sha)
            cache[sha] = evidence["parts"]
        except (OSError, RequestError):
            cache[sha] = ()
    parts = cache[sha]
    if number > len(parts):
        return ""
    text = " ".join(parts[number - 1]["text"].split())
    quote = event["quote"]
    offset = text.find(quote)
    if offset < 0 or text == quote:
        return ""
    # 保留签发引句与所在段的连续上下文；超长原段可从段号展开。
    budget = 700
    start = min(max(0, offset - (budget - len(quote)) // 2), max(0, len(text) - budget))
    end = min(len(text), start + budget)
    if start:
        boundary = max(text.rfind(mark, start, offset) for mark in "。！？")
        if boundary >= start:
            start = boundary + 1
    if end < len(text):
        after_quote = offset + len(quote) - 1
        boundary = max(text.rfind(mark, after_quote, end) for mark in "。！？")
        if boundary >= after_quote:
            end = boundary + 1
    excerpt = text[start:end].strip()
    if excerpt == quote:
        return ""
    return ("…" if start else "") + excerpt + ("…" if end < len(text) else "")


def _argument_detail(event, overview, timeline, color, mapping, paths, contexts, replaced, source_context,
                     vault, fixed_cache):
    history = [_related_record_link(event, paths)]
    for target, label in ((event["supersedes"], "被修订的记录"),
                          (replaced.get(event["id"]), "查看后续修订")):
        if target in paths:
            history.append(f"[[{paths[target]}|{label}]]")
    return _json_bytes(reading_canvas.argument(
        event, overview, timeline, color, mapping, contexts, history, source_context,
        _headline(event), _source_label(event), _text_node, _card_height, vault, fixed_cache))


def _timeline_source(event, mapping):
    source = locations.resolve(event["source_path"], mapping)
    if event.get("reading_anchor"):
        source = source.partition("#")[0] + f"#^e{event['reading_anchor']}"
    return source


def _timeline_cue(event):
    if event.get("timeline_cue"):
        return event["timeline_cue"]
    text = re.split(r"[；。！？]", _headline(event).strip(), maxsplit=1)[0]
    text = re.sub(r"^(?:作者|讲者|材料|纪要|报告)(?:认为|提到|提出|推测)[，、 ]*", "", text)
    return _short(text or _headline(event), 49)


def _continuous_timeline(root, events, cards, paths, mapping, minimum_heights, branch_cards=()):
    """主题的每个月按两列连续展开，沿同一条纵向阅读路径。"""
    width, gutter, inset = 524, 48, 34
    span = width * 2 + gutter + inset * 2
    sections = {}
    for event, card in zip(events, cards, strict=True):
        stamp = event.get("source_modified_ns")
        month = source_day(stamp).strftime("%Y年%m月") if stamp is not None else "材料时间待核"
        sections.setdefault(month, []).append((event, card))
    picks = {month: max(items, key=lambda entry: (
        entry[0]["kind"] in ("更正", "撤回"), entry[0].get("timeline_pick", 0),
        entry[0].get("relation") in ("进展", "分歧"), entry[0]["kind"] == "事实"))[0]
        for month, items in sections.items()}

    def pick_link(event):
        return f"[[{paths[event['id']]}|{plain(_headline(event))}]] · [[{_timeline_source(event, mapping)}|固定原文]]"

    shortcuts = [f"**{month} · {len(sections[month])} 条**　{pick_link(pick)}" for month, pick in picks.items()]
    intro = root["text"] + "\n\n### 按月选读\n\n" + "\n\n".join(shortcuts)
    root_height = max(170, ceil(_card_height(intro) * 0.82) + 54)
    header = {"id": "choose-month", "type": "group", "label": "按月选读", "x": 0, "y": 0,
              "width": span, "height": root_height + 56, "color": ROOT_COLOR}
    result = [header, {**root, "x": 28, "y": 28, "width": span - 56, "height": root_height, "text": intro}]
    top = header["height"] + 100
    if branch_cards:
        label = _text_node("outline", "## 分论点", ROOT_COLOR, 0, top, span, 82)
        result.append(label)
        top += label["height"] + 30
        for start in range(0, len(branch_cards), 2):
            row = [{**card, "x": inset + column * (width + gutter), "y": top, "width": width}
                   for column, card in enumerate(branch_cards[start:start + 2])]
            result.extend(row)
            top += max(card["height"] for card in row) + 44
        top += 56
    for month, items in sections.items():
        pick = picks[month]
        section = {"id": "month-" + month, "type": "group",
                   "label": f"{month} · {len(items)} 条　{_timeline_cue(pick)}",
                   "x": 0, "y": top, "width": span, "color": ROOT_COLOR}
        result.append(section)
        strip = _text_node("pick-" + month, f"**本月选读**　{pick_link(pick)}", ROOT_COLOR, inset,
                           top + inset, span - 2 * inset, max(84, 42 + 28 * ceil(len(_headline(pick)) / 44)))
        result.append(strip)
        y = strip["y"] + strip["height"] + 32
        for start in range(0, len(items), 2):
            row = [{**card, "x": inset + column * (width + gutter), "y": y, "width": width,
                    "height": max(210, ceil(_card_height(card["text"]) * 1.10) + 16,
                                  minimum_heights.get(card["id"], 0))}
                   for column, (_, card) in enumerate(items[start:start + 2])]
            result.extend(row)
            y += max(card["height"] for card in row) + 40
        section["height"] = y - top
        top = y + 110
    return _json_bytes({"nodes": result, "edges": []})


def _render_topic(identifier, topic, mapping, topic_paths=None, *, vault=None, source_cache=None):
    page, canvas = _topic_paths(identifier, topic_paths)
    title = _title(topic)
    events = sorted(topic["events"], key=_source_order)
    replaced = {event["supersedes"]: event["id"] for event in events if event["supersedes"]}
    hierarchy = topic.get("reading_hierarchy")
    paths = _argument_paths(canvas, topic["events"])
    minimum_heights = topic.get("timeline_minimum_heights", {})
    contexts = defaultdict(list)
    colors = {}
    if hierarchy:
        for index, branch in enumerate(hierarchy["branches"]):
            branch_path = canvas
            for argument in branch["arguments"]:
                contexts[argument["event"]].append((branch_path, branch["title"], argument))
                colors.setdefault(argument["event"], READING_BRANCH_COLORS[index % len(READING_BRANCH_COLORS)])
    lines = [f"[[{canvas}|打开问题白板（{len(events)} 条）]]"]
    intro = f"# {plain(title)}\n\n{plain(_topic_overview(topic))}\n\n"
    intro += f"[[{reading_canvas.HOME}|阅读首页]] · [[{OVERVIEW}|知识总白板]]"
    if hierarchy:
        intro += f"\n\n{plain(hierarchy['reason'])}"
    root = _text_node("root", intro, ROOT_COLOR, 0, 0, 540, 0)
    cards = []
    color = branch_color(topic["color_index"])
    for event in events:
        identifier = event["id"]
        heading = plain(_headline(event))
        stage = event["stage"]
        kind = (f"{stage} · " if stage not in ("其他", "未知") else "") + event["kind"]
        label = f"**{_source_label(event)}**　{plain(event['origin'] or event['article_title'])} · {plain(kind)}"
        details = _record_details(event)
        links = _event_links(event, mapping)
        link = f"[[{paths[identifier]}|{heading}]]"
        lines.extend(["", f"### {link}", "", label, "", *[part for detail in details for part in (detail, "")],
                      f"> {plain(event['quote'])}", "",
                      f"- {plain(event['article_title'])} · {links}",
                      f"- 来源发表 {plain(event['published_at'] or '未知')} · "
                      f"收录 {plain(event['admitted_at'] or '未知')} · 知识更新 {plain(event['recorded_at'])}"])
        if event["date"]:
            lines.append(f"- 材料中提到的事件时间 {plain(event['date'])}")
        related = _related_record_link(event, paths)
        if related:
            lines.append(f"- {related}")
        for target, correction_label in ((event["supersedes"], "被修订的记录"),
                                         (replaced.get(identifier), "查看后续修订")):
            if target in paths:
                lines.append(f"- [[{paths[target]}|{correction_label}]]")
        lines.extend(["", f"^{identifier}"])
        source = _timeline_source(event, mapping)
        steps = event.get("reading_reason", {}).get("steps", ())
        if steps:
            lead = steps[0]["body"]
        else:
            lead = event.get("context") or (event["claim"] if _normalized(event["claim"]) != _normalized(_headline(event))
                                            else event["quote"])
        references = " · ".join(filter(None, (f"[[{paths[identifier]}|读作者的理由]]",
                    _related_record_link(event, paths), f"[[{source}|固定原文]]")))
        text = "\n\n".join([f"## {link}", plain(_short(lead, 185)), label, references])
        cards.append(_text_node(identifier, text, colors.get(identifier, color), 0, 0, 540, 0))

    branch_cards = _question_branches(hierarchy, paths) if hierarchy else ()
    timeline = _continuous_timeline(root, events, cards, paths, mapping, minimum_heights, branch_cards)
    outputs = {page: _markdown(title, lines), canvas: timeline}

    source_cache = {} if source_cache is None else source_cache
    fixed_cache = source_cache.setdefault("fixed_source_blocks", {})
    for event in events:
        event_id = event["id"]
        fallback = _argument_source_context(vault, event, source_cache)
        outputs[paths[event_id]] = _argument_detail(event, canvas, canvas,
                            colors.get(event_id, color), mapping, paths, contexts[event_id],
                            replaced, fallback, vault, fixed_cache)
    return outputs


def _reading_node(identifier, title, body, x, y, width, height, color):
    text = "\n\n".join([f"## {plain(title)}", *body])
    return _text_node(identifier, text, color, x, y, width, height)


def _question_branches(hierarchy, paths):
    cards = []
    for index, branch in enumerate(hierarchy["branches"]):
        arguments = [f"- [[{paths[item['event']]}|{plain(item['title'])}]]"
                     for item in branch["arguments"] if item["event"] in paths]
        text = "\n\n".join([f"## {plain(branch['title'])}", plain(branch["reason"]), "\n".join(arguments)])
        color = READING_BRANCH_COLORS[index % len(READING_BRANCH_COLORS)]
        cards.append(_text_node(f"branch-{branch['id']}", text, color, 0, 0, 524, _card_height(text) + 40))
    return cards


def superseded_topic_boards(topics):
    """旧独立时间线及分支回到同一题的主白板。"""
    paths = _topic_path_map(topics)
    aliases = {}
    for key, topic in topics.items():
        _, canvas = _topic_paths(key, paths)
        aliases[_full_timeline_path(canvas)] = canvas
        labels = _branch_labels(topic)
        branches = topic.get("reading_hierarchy", {}).get("branches", ())
        for branch in branches:
            label = labels[branch.get("label", branch["id"])]
            aliases[_branch_path(canvas, branch, label)] = canvas
        for label in topic.get("retired_reading_branches", ()):
            aliases[_branch_path(canvas, {"id": label}, labels[label])] = canvas
    return aliases


def _category_path(key):
    return f"{ROOT}/{CATEGORIES[key]}/{CATEGORIES[key]}总览.canvas"


def _topic_card(identifier, topic, x, y, topic_paths=None):
    _, canvas = _topic_paths(identifier, topic_paths)
    paths = _argument_paths(canvas, topic["events"])
    superseded = {event["supersedes"] for event in topic["events"] if event["supersedes"]}
    events = [event for event in reversed(topic["events"]) if event["id"] not in superseded]
    events.sort(key=lambda event: (event.get("source_modified_ns") is None,
                                   -(event.get("source_modified_ns") or 0)))
    card = topic.get("category_card", {})
    by_id = {event["id"]: event for event in events}
    picked = [(by_id[item["event"]], item["text"]) for item in card.get("picks", ()) if item["event"] in by_id]
    used = {event["id"] for event, _ in picked}
    new = [event for event in events if card and event.get("recorded_at", "") > card["through"]
           and event["id"] not in used]
    if new:
        picked = picked[:1] + [(new[0], _headline(new[0]))]
    if not picked:
        picked = [(event, _headline(event)) for event in events[:2]]
    title = card.get("title") or topic.get("main_judgment") or _title(topic)
    scope = card.get("scope") or _short(_topic_overview(topic), 85)
    lines = [f"## [[{canvas}|{plain(title)}]]", plain(scope)]
    if picked:
        rows = []
        for event, summary in picked:
            summary = plain(_short(summary, 86))
            rows.append(f"- [[{paths[event['id']]}|{summary}]]")
        lines.append("\n".join(rows))
    lines.append(f"[[{canvas}|打开问题白板]] · {len(topic['events'])} 条记录")
    text = "\n\n".join(lines)
    return _text_node(identifier, text, branch_color(topic["color_index"]), x, y, 524,
                      max(318, ceil(_card_height(text) * 1.17) + 48))


def _text_node(identifier, text, color, x, y, width, height):
    return {"id": identifier, "type": "text", "text": text, "color": color,
            "x": x, "y": y, "width": width, "height": height}


def _projections(vault, topics, selected=None, categories=()):
    if not topics or selected is not None and not selected:
        return {}
    mapping = locations.load(vault)
    topic_paths = _topic_path_map(topics)
    source_cache = {}
    generated = {}
    groups = defaultdict(list)
    category_filter = None if selected is None else set(categories)
    for identifier, topic in sorted(topics.items(), key=lambda entry: entry[1]["color_index"]):
        category = _category(topic)
        groups[category].append((identifier, topic))
        if selected is None or identifier in selected:
            generated.update(_render_topic(identifier, topic, mapping, topic_paths,
                                           vault=vault, source_cache=source_cache))
            if category_filter is not None:
                category_filter.add(category)
    lines = [f"[[{OVERVIEW}|打开知识总结总白板]]", ""]
    nodes = [_text_node("root", f"# 知识总结\n\n{len(topics)} 个主题 · [[{reading_canvas.HOME}|返回阅读首页]]",
                        ROOT_COLOR, 0, 0, 440, 132)]
    column_tops = [210, 210, 210]
    entry_height, entry_step = 88, 106
    for position, (domain, name, color, entries) in enumerate(DOMAINS):
        keys = [key for key, _ in entries if key in groups]
        if not keys:
            continue
        column = position % 3
        x = column * 504
        y = column_tops[column]
        height = 46 + entry_step * len(keys)
        nodes.append({"id": "domain-" + domain, "type": "group", "label": name,
                      "color": color, "x": x, "y": y, "width": 450, "height": height})
        lines.extend([f"## {name}", ""])
        for index, key in enumerate(keys):
            label, path, members = CATEGORIES[key], _category_path(key), groups[key]
            nodes.append(_text_node("category-" + key, f"[[{path}|{label}]]　·　{len(members)} 个主题",
                                    color, x + 22, y + 24 + entry_step * index, 406, entry_height))
            lines.extend([f"### [[{path}|{label}]]", ""])
            if category_filter is None or key in category_filter:
                category_nodes = [_text_node("root", f"# {label}总览\n\n{len(members)} 个主题 · [[{OVERVIEW}|返回知识总结总白板]]",
                                             color, 0, 0, 1100, 138)]
                card_y = 208
                for offset in range(0, len(members), 2):
                    cards = [_topic_card(identifier, topic, 576 * slot, card_y, topic_paths)
                             for slot, (identifier, topic) in enumerate(members[offset:offset + 2])]
                    row_height = max(card["height"] for card in cards)
                    category_nodes.extend({**card, "height": row_height} for card in cards)
                    card_y += row_height + 42
                generated[path] = _json_bytes({"nodes": category_nodes, "edges": []})
            for identifier, topic in members:
                page, canvas = _topic_paths(identifier, topic_paths)
                lines.append(f"- [[{canvas}|{plain(_title(topic))}]]")
            lines.append("")
        column_tops[column] += height + 78
    generated[OVERVIEW] = _json_bytes({"nodes": nodes, "edges": []})
    generated[INDEX] = _markdown("知识总结索引", lines)
    return generated


def _generated_markdown(original):
    begin, end = (token.encode() + b"\n" for token in (BEGIN, END))
    if original.count(begin) != 1 or original.count(end) != 1:
        raise RequestError("user_edit_conflict")
    start = original.index(begin)
    stop = original.index(end) + len(end)
    if stop <= start:
        raise RequestError("user_edit_conflict")
    return original[:start], original[start:stop], original[stop:]


def _markdown_boundary(content):
    return {"bytes": len(content), "opening": content.split(b"\n", 1)[0].decode("utf-8") + "\n"}


def _checked_markdown(original, ledger, relative):
    """以账本记录的正文摘要和边界定位，手写前后区保持原字节。"""
    expected = ledger["projections"].get(relative)
    metadata = ledger.get("markdown_boundaries", {}).get(relative, {})
    current = metadata.get("current")
    if expected and current is None:
        begin, end = (token.encode() + b"\n" for token in (BEGIN, END))
        starts = (match.start() for match in re.finditer(re.escape(begin), original))
        ranges = []
        for start in starts:
            ending = original.find(end, start + len(begin))
            if ending != -1:
                stop = ending + len(end)
                if digest(original[start:stop]) == expected:
                    ranges.append((start, stop))
        if len(ranges) == 1:
            start, stop = ranges[0]
            return original[:start], original[start:stop], original[stop:]
    elif expected and current:
        opening = current["opening"].encode("utf-8")
        starts = list(re.finditer(re.escape(opening), original))
        if len(starts) == 1:
            start = starts[0].start()
            stop = start + current["bytes"]
            body = original[start:stop]
            if len(body) == current["bytes"] and digest(body) == expected:
                return original[:start], body, original[stop:]
    # 中断在文件写入和账本确认之间时，只接续已登记的完整文件。
    pending = metadata.get("pending")
    if pending and digest(original) == pending["file_sha256"]:
        start = pending["start"]
        stop = start + pending["bytes"]
        return original[:start], original[start:stop], original[stop:]
    raise RequestError("user_edit_conflict")


def _prepare_markdown(path, content, ledger, relative):
    try:
        original = source_bytes(path)
    except FileNotFoundError:
        if ledger["projections"].get(relative):
            raise RequestError("user_edit_conflict") from None
        return None, content + "\n## 自由笔记\n".encode("utf-8"), 0
    prefix, _, suffix = _checked_markdown(original, ledger, relative)
    return original, prefix + content + suffix, len(prefix)


def _canvas_by_id(content):
    try:
        canvas = json.loads(content)
        if not isinstance(canvas, dict) or set(canvas) != {"nodes", "edges"}:
            return None
        indexed = {field: {item["id"]: item for item in canvas[field]} for field in ("nodes", "edges")}
        if any(len(indexed[field]) != len(canvas[field]) for field in indexed):
            return None
        return indexed
    except (ValueError, TypeError, KeyError):
        return None


def _retain_canvas_order(current, content):
    actual, proposed = json.loads(current), json.loads(content)
    for field in ("nodes", "edges"):
        positions = {item["id"]: index for index, item in enumerate(actual[field])}
        proposed[field].sort(key=lambda item: -1 if item["id"] == "choose-month"
                             else positions.get(item["id"], len(positions)))
    return _json_bytes(proposed)


def _publish_projection(path, content, expected, previous=None):
    try:
        current = source_bytes(path)
    except FileNotFoundError:
        if expected:
            # 文件可能由用户移走。保留住现有意图。
            raise RequestError("user_edit_conflict") from None
        _write_projection(path, None, content)
        return digest(content)
    actual = _canvas_by_id(current)
    if actual is not None and actual == _canvas_by_id(content):
        return digest(current)
    if current != content and digest(current) != expected:
        # Obsidian 可重排同一组节点或重写 JSON。只接受与上版逐 ID 完全一致的板。
        if actual is None or previous is None or actual != _canvas_by_id(previous):
            raise RequestError("user_edit_conflict")
    if actual is not None:
        content = _retain_canvas_order(current, content)
    _write_projection(path, current, content)
    return digest(content)


def _save(path, state):
    try:
        original = source_bytes(path)
    except FileNotFoundError:
        original = None
    _write_projection(path, original, _json_bytes(state))


def _sync_source_times(vault, topics):
    events = [event for topic in topics.values() for event in topic["events"]]
    intake_db = inside(vault, ".afr/intake.sqlite")
    if not events or not intake_db.is_file():
        return False
    with closing(sqlite3.connect(intake_db.as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_times'").fetchone():
            return False
        dates = dict(db.execute("SELECT sha, modified_ns FROM source_times"))
    changed = False
    for event in events:
        modified = dates.get(event["sha256"])
        if modified is not None and event.get("source_modified_ns") != modified:
            event["source_modified_ns"] = modified
            changed = True
    return changed


def _legacy_projection_moves(state):
    topics = state["topics"]
    stems = _topic_stems(topics)
    names = _topic_path_map(topics)
    tracked = state["projections"]
    moves = {old: _category_path(key) for key, label in CATEGORIES.items()
             for old in (f"{ROOT}/分类/{label}.canvas", f"{ROOT}/{label}/{label}.canvas")
             if old in tracked}
    for identifier, topic in topics.items():
        title = stems[identifier]
        note, canvas = _topic_paths(identifier, names)
        current_folder = _topic_details_folder(canvas)
        branch_labels = _branch_labels(topic)
        origins = [identifier, title, f"{CATEGORIES['unclassified']}/{title}"]
        for origin in origins:
            old_note, old_canvas = _topic_paths(identifier, {identifier: origin})
            if old_canvas == canvas:
                continue
            old_folder = _topic_details_folder(old_canvas)
            destinations = {old_note: note, old_canvas: canvas,
                            _full_timeline_path(old_canvas): _full_timeline_path(canvas)}
            for relative in tracked:
                if not relative.startswith(f"{old_folder}/") or relative in destinations or not relative.endswith(".canvas"):
                    continue
                child = relative.removeprefix(f"{old_folder}/")
                if "/" not in child:
                    branch = child.removesuffix(".canvas")
                    child = f"分支/{branch_labels.get(branch, _filename(branch))}.canvas"
                destinations[relative] = f"{current_folder}/{child}"
            moves.update({old: new for old, new in destinations.items()
                          if old != new and old in tracked})
    return moves


def _move_projection(vault, old, new, expected):
    source, target = inside(vault, old), inside(vault, new)
    try:
        original = source_bytes(source)
    except FileNotFoundError:
        try:
            current = source_bytes(target)
        except FileNotFoundError:
            return
        if expected != digest(current):
            raise RequestError("user_edit_conflict") from None
        return
    if digest(original) != expected:
        raise RequestError("user_edit_conflict")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError:
        if not target.samefile(source):
            raise RequestError("user_edit_conflict") from None
    if source_bytes(source) != original or not target.samefile(source):
        raise RequestError("user_edit_conflict")
    source.unlink()
    for folder in source.parents:
        if folder == vault / ROOT:
            break
        try:
            folder.rmdir()
        except OSError:
            break


def _migrate_topic_paths(vault, path, state, moves):
    if not moves:
        return []
    protected = []
    pending = state.get("pending_topic_moves", {})
    duplicates = Counter(moves.values())
    # 先核对整批目标，保留已有文件，也避免因为重名先移动一半入口。
    for old, new in moves.items():
        try:
            source, target = inside(vault, old), inside(vault, new)
            if duplicates[new] > 1 or new in state["projections"]:
                protected.append(new)
            elif target.exists():
                if source.exists():
                    if not target.samefile(source):
                        protected.append(new)
                elif old not in pending or digest(source_bytes(target)) != pending[old]:
                    protected.append(new)
        except RequestError:
            protected.append(new)
    if protected:
        return list(dict.fromkeys(protected))
    state["topic_view_pending"] = True
    boundaries = state.setdefault("markdown_boundaries", {})
    pending = state.setdefault("pending_topic_moves", {})
    for old, new in moves.items():
        try:
            try:
                current = digest(source_bytes(inside(vault, old)))
            except FileNotFoundError:
                current = pending.get(old)
            if old not in pending or pending[old] != current:
                pending[old] = current
                _save(path, state)
            _move_projection(vault, old, new, pending[old])
        except RequestError as exc:
            if str(exc) not in ("user_edit_conflict", "invalid_path"):
                raise
            protected.append(new)
            continue
        state["projections"][new] = state["projections"].pop(old)
        if old in boundaries:
            boundaries[new] = boundaries.pop(old)
        del pending[old]
        _save(path, state)
    if not pending:
        state.pop("pending_topic_moves")
    return protected


def _publish(vault, path, state, previous=None, *, selected=None, categories=()):
    previous = previous or {}
    moves = _legacy_projection_moves(state)
    protected = _migrate_topic_paths(vault, path, state, moves)
    if protected:
        return protected
    if state.get("topic_view_pending"):
        selected = None
    topics = state["topics"]
    affected = topics if selected is None else {key: topics[key] for key in selected if key in topics}
    _sync_source_times(vault, affected)
    projections = _projections(vault, topics, selected, categories)
    live_categories = {_category(topic) for topic in topics.values()}
    for key, label in CATEGORIES.items():
        if key in live_categories or selected is not None and key not in categories:
            continue
        relative = _category_path(key)
        if relative in state["projections"] and relative not in projections:
            projections[relative] = _json_bytes({"nodes": [
                _text_node("root", f"# {label}\n\n暂无主题 · [[{OVERVIEW}|返回知识总结总白板]]",
                           ROOT_COLOR, 0, 0, 540, 148)
            ], "edges": []})
    boundaries = state.setdefault("markdown_boundaries", {})
    prepared = {}
    changed_pending = False
    for relative, content in projections.items():
        if not relative.endswith(".md"):
            continue
        try:
            target = inside(vault, relative)
            original, updated, start = _prepare_markdown(target, content, state, relative)
        except RequestError as exc:
            if str(exc) not in ("user_edit_conflict", "invalid_path"):
                raise
            protected.append(relative)
            continue
        prepared[relative] = (target, original, updated)
        if original != updated:
            pending = {"file_sha256": digest(updated), "start": start, "bytes": len(content)}
            boundaries.setdefault(relative, {})["pending"] = pending
            changed_pending = True
    if changed_pending:
        _save(path, state)

    for relative, content in projections.items():
        try:
            if relative.endswith(".md"):
                if relative not in prepared:
                    continue
                _write_projection(*prepared[relative])
                boundaries[relative] = {"current": _markdown_boundary(content)}
                state["projections"][relative] = digest(content)
                continue
            saved = _publish_projection(inside(vault, relative), content, state["projections"].get(relative),
                                        previous.get(relative))
        except RequestError as exc:
            if str(exc) not in ("user_edit_conflict", "invalid_path"):
                raise
            protected.append(relative)
        else:
            state["projections"][relative] = saved
    state.pop("topic_view_pending", None)
    _save(path, state)
    return protected


def _remember_equivalent_canvases(vault, state, previous):
    # 发布前保存可安全续接的旧字节摘要，避免中断后把旧版排版识别成用户编辑。
    for relative, content in previous.items():
        if not relative.endswith(".canvas"):
            continue
        try:
            current = source_bytes(inside(vault, relative))
        except (OSError, RequestError):
            continue
        if current != content and _canvas_by_id(current) == _canvas_by_id(content):
            state["projections"][relative] = digest(current)


def refresh_views(vault, *, source_dates_changed=False, force=False, selected=None):
    """启动监听器时用本机证据更新历史展示，保留用户手写内容。"""
    path = inside(vault, f"{STATE}/ledger.json")
    if not path.is_file():
        return
    with inside(vault, f"{STATE}/lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if source_dates_changed else 0))
        except BlockingIOError:
            # 正在处理的知识批次发布时也会读取已核验的原件修改时间。
            return
        state = _state(path)
        topics = state["topics"]
        if selected is not None:
            selected = set(selected)
            if not selected or not selected.issubset(topics):
                raise RequestError("unknown_knowledge_topic")
        affected = topics if selected is None else {key: topics[key] for key in selected}
        previous = _projections(vault, topics, selected) if source_dates_changed else None
        updated_times = source_dates_changed and _sync_source_times(vault, affected)
        pending_markdown = any("pending" in entry for entry in state.get("markdown_boundaries", {}).values())
        display_pending = state.get("topic_view_pending") or _legacy_projection_moves(state)
        if (state.get("view_revision") == VIEW_REVISION and not updated_times
                and not force and not pending_markdown and not display_pending):
            return
        publications = {}
        for topic in affected.values():
            for event in topic["events"]:
                sha = event["sha256"]
                if event["published_at"]:
                    continue
                if sha not in publications:
                    try:
                        _, evidence = evidence_for(vault, sha)
                    except (OSError, RequestError):
                        continue
                    publications[sha] = _publication_date(evidence)
                event["published_at"] = publications[sha]
        if selected is None:
            state["view_revision"] = VIEW_REVISION
        return _publish(vault, path, state, previous, selected=selected)


def _scope(version_ids, new_version_ids):
    versions = (list, tuple, set, frozenset)
    if (not isinstance(version_ids, versions) or not 1 <= len(version_ids) <= 10
            or any(not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) for sha in version_ids)):
        raise RequestError("invalid_request")
    scope = frozenset(version_ids)
    if new_version_ids is None:
        return scope, scope
    if (not isinstance(new_version_ids, versions) or not new_version_ids
            or any(not isinstance(sha, str) or sha not in scope for sha in new_version_ids)):
        raise RequestError("invalid_request")
    return scope, frozenset(new_version_ids)


def _state(path):
    if not path.is_file():
        return {"contract": CONTRACT, "view_revision": VIEW_REVISION,
                "topics": {}, "batches": {}, "projections": {}}
    state = semantic.read_json(path)
    if (not isinstance(state, dict) or state.get("contract") != CONTRACT
            or any(not isinstance(state.get(key), dict) for key in ("topics", "batches", "projections"))):
        raise RequestError("invalid_knowledge_state")
    assigned = {topic["color_index"] for topic in state["topics"].values() if "color_index" in topic}
    next_color = max(assigned, default=-1) + 1
    for topic in state["topics"].values():
        if "color_index" not in topic:
            topic["color_index"] = next_color
            next_color += 1
    return state


def _model_payload(payload):
    if len(json.dumps(payload, ensure_ascii=False)) > MAX_PAYLOAD:
        raise RequestError("knowledge_scope_too_large")
    return payload


def _review_identity(topic):
    if not isinstance(topic, dict):
        return None
    subject, dimension, identifier = (topic.get(key) for key in ("object", "dimension", "topic_id"))
    if not all(isinstance(value, str) for value in (subject, dimension, identifier)):
        return None
    return identifier or _topic_id(subject, dimension)


def _compact_review_topic(topic):
    # 只有完整单主题装不下才让校读重写可见卡文案。出处、主张和修订关系逐项保留。
    if _review_identity(topic) is None or not isinstance(topic.get("events"), list):
        return None
    return {key: value for key, value in topic.items()
            if key in TOPIC["properties"] and key not in TOPIC_CARD_FIELDS and key not in {"reading_branches", "events"}} | {
        "events": [{key: value for key, value in event.items()
                    if key in EVENT["properties"] and key not in EVENT_CARD_FIELDS}
                   for event in topic["events"] if isinstance(event, dict)],
    }


def _review_parts(payload, draft):
    """每段保留完整主题的证据和关系，原提案已独立留档。"""
    base = {"topic_numbers": [], "regenerate_presentation_for": [], "recheck_unreadable": [],
            "draft_topic_count": len(draft["topics"]), "discovery_slots": MAX_TOPICS - len(draft["topics"])}
    parts = []
    topics = []
    scope = dict(base)

    def review_payload():
        return {**payload, "draft": {"topics": topics}, "draft_review_scope": scope}

    for number, topic in enumerate(draft["topics"], 1):
        proposed = {**review_payload(), "draft": {"topics": [*topics, topic]},
                    "draft_review_scope": {**scope, "topic_numbers": [*scope["topic_numbers"], number]}}
        if _payload_size(proposed) <= MAX_PAYLOAD:
            topics, scope = proposed["draft"]["topics"], proposed["draft_review_scope"]
            continue
        if topics or scope["recheck_unreadable"]:
            parts.append(review_payload())
            topics, scope = [], {**base, "discovery_slots": 0}
        scope["topic_numbers"] = [number]
        topics = [topic]
        if _payload_size(review_payload()) <= MAX_PAYLOAD:
            continue
        compact = _compact_review_topic(topic)
        topics = [compact] if compact is not None else []
        scope["regenerate_presentation_for"] = [number]
        if compact is None or _payload_size(review_payload()) > MAX_PAYLOAD:
            # 畸形超限草稿不能代表已校验的依据。标号并请校读直接读同一批证据。
            topics = []
            scope["topic_numbers"] = []
            scope["regenerate_presentation_for"] = []
            scope["recheck_unreadable"] = [number]
    parts.append(review_payload())
    return parts


def _combine_review_timing(items):
    metrics = {key: sum(item.get(key, 0) for item in items) for key in ("elapsed_ms", "input_bytes")}
    models = {item["model"] for item in items if item.get("model")}
    if len(models) == 1:
        metrics["model"] = models.pop()
    usage = [item.get("usage") for item in items]
    if all(isinstance(item, dict) for item in usage):
        metrics["usage"] = {key: sum(item.get(key, 0) for item in usage)
                            for key in set().union(*usage)}
    return metrics


def _review_proposal(vault, receipt_key, runner, instructions, payload, draft, authorized, validate):
    ordinary = {**payload, "draft": draft}
    if _payload_size(ordinary) <= MAX_PAYLOAD:
        authorized()
        review, timing = measured(runner, instructions, ordinary, SCHEMA)
        validate(review)
        return {"output": review, "timing": timing}

    instructions += (
        "\n\n本次按提案主题分段校读。每段材料和历史范围一致。请仅校读该段提案，保持主题对象、"
        "维度和已有主题 ID，独立核对原文并补足主题内遗漏。标记须重写展示的主题已有完整证据，"
        "按校读结果重写卡文案和阅读分支。无法附上草案的编号可直接根据原文重新归纳。"
        "只有 discovery_slots 大于零时才另查漏掉的主题，额外主题数不得超过该值。"
    )
    all_ids = {_review_identity(topic) for topic in draft["topics"]} - {None}
    reviews = {}
    discovered = {}
    timing = []
    checkpoints = []
    for part in _review_parts(payload, draft):
        authorized()
        key = semantic.fingerprint([instructions, part])
        path = inside(vault, f"{STATE}/{receipt_key}-review-{key}.json")
        if path.is_file():
            saved = semantic.read_json(path)
        else:
            review, elapsed = measured(runner, instructions, _model_payload(part), SCHEMA)
            validate(review)
            saved = {"output": review, "timing": elapsed}
            exclusive_json(path, saved)
        checkpoints.append(key)
        timing.append(saved["timing"])
        scope = part["draft_review_scope"]
        assigned = {_review_identity(draft["topics"][number - 1])
                    for number in [*scope["topic_numbers"], *scope["recheck_unreadable"]]} - {None}
        discovery_slots = scope["discovery_slots"] + sum(
            _review_identity(draft["topics"][number - 1]) is None for number in scope["recheck_unreadable"])
        for topic in saved["output"]["topics"]:
            identifier = _review_identity(topic)
            if identifier in assigned:
                reviews.setdefault(identifier, topic)
            elif identifier and identifier not in all_ids and discovery_slots > 0:
                if identifier not in discovered:
                    discovered[identifier] = topic
                    discovery_slots -= 1
    topics = [*reviews.values(), *discovered.values()]
    review = {"topics": topics[:MAX_TOPICS]}
    validate(review)
    return {"output": review, "timing": _combine_review_timing(timing), "segments": checkpoints}


def run(vault, version_ids, approvals, runner=None, *, new_version_ids=None):
    """校读已许可的固定版本，追加知识历史并返回本批 Obsidian 发布状态。"""
    vault = Path(vault).resolve()
    scope, new_sources = _scope(version_ids, new_version_ids)
    runner = runner or run_structured

    def authorized():
        if not scope.issubset(semantic.approved_versions(approvals)):
            raise RequestError("local_only")

    authorized()
    articles, pointers, candidates = _article_payload(vault, scope)
    try:
        standard = STANDARD.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise RequestError("knowledge_standard_unavailable") from None
    if not standard:
        raise RequestError("knowledge_standard_unavailable")
    standard += "\n\n分类代码如下。\n" + "\n".join(
        f"- {domain}　" + "；".join(f"{key} = {label}" for key, label in entries)
        for _, domain, _, entries in DOMAINS)
    batch = semantic.fingerprint([CONTRACT, PROPOSE, REVIEW, standard, SCHEMA, articles, candidates, sorted(new_sources)])
    application_batch = semantic.fingerprint([batch, APPLICATION_REVISION])
    directory = inside(vault, STATE)
    directory.mkdir(parents=True, exist_ok=True)
    path = inside(vault, f"{STATE}/ledger.json")
    with inside(vault, f"{STATE}/lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _state(path)
        added = 0
        added_evidence_links = 0
        previous = {}
        receipt = state["batches"].get(application_batch)
        if receipt is None:
            legacy = state["batches"].get(batch)
            if legacy and (legacy["topic_ids"] or not legacy["deferred_topics"]):
                receipt = legacy
        if receipt is None:
            digest_scope = _topic_digest(state["topics"], articles, scope, new_sources)
            catalog = {item["id"]: item for item in digest_scope}
            payload = _model_payload({"articles": articles, "new_versions": sorted(new_sources),
                                      "existing_topics": digest_scope, "cross_card_candidates": candidates})
            checkpoint = semantic.fingerprint(payload)
            proposal_path = inside(vault, f"{STATE}/{batch}-{checkpoint}-proposal.json")
            review_path = inside(vault, f"{STATE}/{batch}-{checkpoint}-review.json")
            authorized()
            if proposal_path.is_file():
                saved = semantic.read_json(proposal_path)
                draft = saved.get("output", saved)
            else:
                draft, timing = measured(runner, PROPOSE + "\n" + standard, payload, SCHEMA)
                _topic_updates(draft, pointers, state["topics"], catalog, new_sources, reviewed=False)
                exclusive_json(proposal_path, {"output": draft, "timing": timing})
            authorized()
            if review_path.is_file():
                saved = semantic.read_json(review_path)
                review = saved.get("output", saved)
            else:
                def validate_review(output):
                    _topic_updates(output, pointers, state["topics"], catalog, new_sources, reviewed=True)

                saved = _review_proposal(vault, f"{batch}-{checkpoint}", runner, REVIEW + "\n" + standard,
                                         payload, draft, authorized, validate_review)
                review = saved["output"]
                exclusive_json(review_path, saved)
            updates, deferred = _topic_updates(review, pointers, state["topics"], catalog, new_sources, reviewed=True)
            authorized()
            categories = {_category(item) for item in updates.values()}
            categories.update(_category(state["topics"][key]) for key in updates if key in state["topics"])
            previous = _projections(vault, state["topics"], set(updates), categories)
            _remember_equivalent_canvases(vault, state, previous)
            identifiers, added, event_ids, evidence_links = _apply(vault, state, updates, new_sources)
            added_evidence_links = len(evidence_links)
            receipt = {"topic_ids": identifiers, "deferred_topics": deferred + len(updates) - len(identifiers),
                       "model_batch": batch, "model_checkpoint": checkpoint, "event_ids": event_ids,
                       "evidence_links": evidence_links, "view_categories": sorted(categories)}
            state["batches"][application_batch] = receipt
            _save(path, state)
        protected = _publish(vault, path, state, previous, selected=set(receipt["topic_ids"]),
                             categories=receipt.get("view_categories", ()))
    return {"topics": len(receipt["topic_ids"]), "topic_ids": receipt["topic_ids"],
            "deferred_topics": receipt["deferred_topics"], "added_events": added,
            "added_evidence_links": added_evidence_links, "evidence_links": receipt.get("evidence_links", []),
            "index_path": INDEX if state["topics"] else None, "protected_paths": protected,
            "event_ids": receipt.get("event_ids"), "model_batch": receipt.get("model_batch"),
            "model_checkpoint": receipt.get("model_checkpoint")}
