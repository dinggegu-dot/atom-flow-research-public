"""发表可回源的白板，保留初读页与用户编辑。"""
from collections import defaultdict
import colorsys
import contextlib
import fcntl
import math
import os
import sqlite3
import tempfile
import json
from pathlib import Path
import re
import unicodedata

from . import locations
from .board_style import ROOT_COLOR, branch_color as pastel_branch_color
from .intake import digest, inside, plain, exclusive_json, source_bytes
from .library import RequestError

ARCHIVE_BOARD_LAYOUT = "archive_columns_v1"
PASTEL_ARCHIVE_BOARD_LAYOUT = "archive_columns_v2"
CHAPTER_READING_LAYOUT = "chapters_v1"
CHAPTER_READING_MARKER = "<!-- AFR:READING:chapters_v1 -->"
MAX_READING_CHAPTERS = 7
ORIGINAL_ARGUMENT = "该组保留完整固定原文。"
OLD_CARD_PLACEHOLDER = "打开本话题阅读对应的完整固定原文。"

COLORS = ["#97b0c0","#a1b99e","#d0b397","#b7a5c5","#9cbdb6","#c3acba","#b6bd8e","#b1accc"]


def text_width(value):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in value)


def branch_color(index):
    if index < len(COLORS):
        return COLORS[index]
    hue = ((index - len(COLORS)) * 0.61803398875 + 0.12) % 1
    rgb = colorsys.hls_to_rgb(hue, 0.70, 0.27)
    return "#" + "".join(f"{round(component * 255):02x}" for component in rgb)


def card_body(evidence, group, *, preview=False):
    """老签发卡缺正文时，以同组固定原文补足可读内容。"""
    numbered = [(number, evidence["parts"][number - 1])
                for number in range(group["start"], group["end"] + 1)]
    body = [(number, " ".join(part["text"].split())) for number, part in numbered
            if part.get("kind") not in {"heading", "title"} and part["text"].strip()]
    first = body[0][0] if body else group["start"]
    summary = group["summary"]
    if not preview and group.get("viewpoints"):
        first = group["viewpoints"][0]["evidence"][0]["number"]
    if preview or not body or summary.strip() != OLD_CARD_PLACEHOLDER:
        return summary, first
    title = re.sub(r"[^\w]", "", group["title"])
    phrases = {title[index:index + 2] for index in range(len(title) - 1)}
    candidates = []
    for number, part in body:
        for sentence in re.split(r"(?<=[。！？])", part):
            sentence = sentence.strip()
            if len(sentence) >= 15:
                candidates.append((sum(word in sentence for word in phrases), number, sentence))
    if not candidates:
        candidates = [(0, number, text) for number, text in body]
    _, number, excerpt = max(candidates, key=lambda item: (item[0], min(len(item[2]), 120), -item[1]))
    if len(excerpt) > 195:
        excerpt = excerpt[:195].rstrip() + "…"
    return "固定原文摘录\n\n" + excerpt, number


def supplement_card_text(text, evidence, group, supplement, *, archive=False):
    """替换旧签发卡的摘录正文与回源位置，保留原有标题和分组。"""
    previous, prior_number = card_body(evidence, group)
    summary = supplement["summary"]
    if archive and len(summary) > 105 and "\n" not in summary:
        summary = re.sub(r"^(.+?[。！？]+[”’」』）】]*)(?=\S)", r"\1\n\n", summary, count=1)
    old_source = f"[[{evidence['folder']}/固定原文.md#^e{prior_number}|查看原文]]"
    new_source = f"[[{evidence['folder']}/固定原文.md#^e{supplement['evidence_number']}|查看原文]]"
    if text.count(plain(previous)) != 1 or text.count(old_source) != 1:
        raise RequestError("article_publication_changed")
    return text.replace(plain(previous), plain(summary), 1).replace(old_source, new_source, 1)


def board(evidence, groups, preview=False, *, layout=None):
    if layout in (ARCHIVE_BOARD_LAYOUT, PASTEL_ARCHIVE_BOARD_LAYOUT):
        pastel = layout == PASTEL_ARCHIVE_BOARD_LAYOUT
        return _archive_columns_board(evidence, groups, preview,
                                      palette=pastel_branch_color if pastel else branch_color,
                                      root_color=ROOT_COLOR if pastel else COLORS[0])

    relative = evidence["folder"]
    caption = "原文结构 · 可先阅读" if preview else "文章思维导图"
    root_text = f"### {plain(evidence['title'])}\n\n{caption}"
    root_text += f"\n\n[[{relative}/固定原文.md|固定原文]] · [[{relative}/原件{evidence['suffix']}|原件]]"
    if evidence["gaps"]:
        root_text += "\n\n保留读取缺口，详见固定原文。"

    nodes, edges = [], []

    def card(key, text, x, y, width, height, color):
        nodes.append(dict(id=key, type="text", text=text, x=x, y=y, width=width, height=height, color=color))

    def connect(parent, child, color):
        edges.append(dict(id=parent + "-" + child, fromNode=parent, fromSide="right",
                          toNode=child, toSide="left", toEnd="none", color=color))

    branches = defaultdict(list)
    for number, group in enumerate(groups, 1):
        branches[group["branch"]].append((number, group))
    y = 0
    for index, (branch, children) in enumerate(branches.items()):
        color = branch_color(index)
        key = f"b{index}"
        start_y = y
        for number, group in children:
            summary, cite = card_body(evidence, group, preview=preview)
            source = f"{relative}/固定原文.md#^e{cite}"
            reading = source if preview else f"{relative}/完整阅读.md#^topic-{number}"
            label = group["title"].replace("|", "｜").replace("[", "［").replace("]", "］")
            text = f"### [[{reading}|{plain(label)}]]\n\n{plain(summary)}\n\n[[{source}|查看原文]]"
            lines = math.ceil(text_width(label) / 30) + math.ceil(text_width(summary) / 37)
            height = max(215, 145 + (lines + summary.count("\n")) * 38)
            child = f"t{number}"
            card(child, text, 920, y, 520, height, color)
            connect(key, child, color)
            y += height + 50
        branch_height = max(125, 70 + math.ceil(text_width(branch) / 15) * 36)
        card(key, "### " + plain(branch), 530, (start_y + y - branch_height - 50) // 2, 280, branch_height, color)
        connect("article", key, color)
        y += 60
    root_height = max(285, 200 + math.ceil(text_width(evidence["title"]) / 24) * 38)
    card("article", root_text, 0, max(0, (y - root_height) // 2), 425, root_height, COLORS[0])
    return {"nodes": nodes, "edges": edges}


def _wrapped_lines(value, width):
    return sum(max(1, math.ceil(text_width(line) / width)) for line in value.split("\n"))


def article_card_height(title, summary, width, *, chapter_label=""):
    # Canvas 的实际换行受正文内边距影响，再留出标题与回原文入口的空间。
    columns = max(14, (width - 52) // 10)
    height = 118 + 37 * _wrapped_lines(title, columns) + 32 * _wrapped_lines(summary, columns)
    if chapter_label:
        height += 18 + 30 * _wrapped_lines(chapter_label, columns)
    return max(220, height)


def _archive_columns_board(evidence, groups, preview, *, palette, root_color):
    # 章节按初次出现排列，同一章节的卡片保持原稿顺序。
    topics_by_branch = defaultdict(list)
    for number, group in enumerate(groups, 1):
        topics_by_branch[" ".join(group["branch"].split())].append((number, group))
    # 同一主线下的独立判断可并排阅读，背景和承上启下的卡仍随原序排列。
    fanout = {}
    lanes = []
    cards_per_lane = 4
    for index, (branch, topics) in enumerate(topics_by_branch.items()):
        judgments = [frozenset(" ".join(record["judgment"].split()) for record in group.get("viewpoints", []))
                     for _, group in topics]
        if not preview and 2 <= len(topics) <= 4 and all(judgments) and len(set(judgments)) == len(topics):
            fanout[index] = len(topics)
            lanes.extend((branch, [topic], index, False) for topic in topics)
        else:
            for start in range(0, len(topics), cards_per_lane):
                lanes.append((branch, topics[start:start + cards_per_lane], index, bool(start)))

    relative = evidence["folder"]
    fixed_source = f"{relative}/固定原文.md"
    full_reading = f"{relative}/完整阅读.md"
    card_width, column_gap, card_gap, row_gap, columns = 520, 70, 55, 100, 4
    nodes, edges = [], []
    rows = []
    row = []
    for lane in lanes:
        branch_index = lane[2]
        required = fanout.get(branch_index, 1) if not row or row[-1][2] != branch_index else 1
        if len(row) + required > columns:
            rows.append(row)
            row = []
        row.append(lane)
    if row:
        rows.append(row)

    def card(key, text, x, y, width, height, color):
        nodes.append(dict(id=key, type="text", text=text, x=x, y=y, width=width, height=height, color=color))

    def connect(parent, child, color):
        edges.append(dict(id=parent + "-" + child, fromNode=parent, fromSide="bottom",
                          toNode=child, toSide="top", toEnd="none", color=color))

    def route(row):
        return " · ".join(dict.fromkeys(plain(branch) for branch, _, _, _ in row))

    first = evidence["parts"][0] if evidence["parts"] else None
    title = evidence["title"]
    if first and (first["kind"] == "title" or (first["kind"] == "heading" and first["text"].startswith("# "))):
        title = " ".join(first["text"].lstrip("# ").split()) or title
    title = plain(title)
    caption = "原文结构 · 可先阅读" if preview else "文章思维导图"
    links = f"[[{fixed_source}|固定原文]] · [[{relative}/原件{evidence['suffix']}|原件]]"
    if not preview:
        links += f" · [[{full_reading}|完整阅读]]"
    root_text = f"### {title}\n\n{caption}\n\n{links}"
    root_height = 154 + 34 * _wrapped_lines(title, 47)
    if rows:
        first_route = route(rows[0])
        if len(rows) > 1:
            first_route = "第 1 行：" + first_route
        root_text += f"\n\n**阅读地图** · 从左向右选主题\n\n{first_route}"
        root_height += 47 + 28 * _wrapped_lines(first_route, 58)
        if len(rows) > 1:
            root_text += f"\n\n共 {len(rows)} 行章节，向下查看后续章节。每列从上往下读。"
            root_height += 58
    if evidence["gaps"]:
        root_text += "\n\n保留读取缺口，详见固定原文。"
        root_height += 44
    card("article", root_text, 0, 0, 560, root_height, root_color)

    row_top = 0
    for row_number, row in enumerate(rows, 1):
        guide = "article"
        guide_height = root_height
        if row_number > 1:
            guide = f"row{row_number}"
            row_route = route(row)
            guide_height = 82 + 28 * _wrapped_lines(row_route, 54)
            card(guide, f"### 阅读地图 · 第 {row_number} 行\n\n{row_route}", 0, row_top, card_width,
                 guide_height, root_color)
        first_top = row_top + guide_height + row_gap
        row_bottom = first_top
        branch_nodes = {}
        for column, (branch, _, branch_index, _) in enumerate(row):
            if branch_index not in fanout or branch_index in branch_nodes:
                continue
            lane_count = fanout[branch_index]
            x = column * (card_width + column_gap)
            width = lane_count * card_width + (lane_count - 1) * column_gap
            branch_key = f"branch-{branch_index}"
            height = 48 + 34 * _wrapped_lines(branch, (width - 52) // 10)
            card(branch_key, f"### {plain(branch)}", x, first_top, width, height, palette(branch_index))
            connect(guide, branch_key, palette(branch_index))
            branch_nodes[branch_index] = (branch_key, first_top + height + card_gap)
        for column, (branch, topics, branch_index, continued) in enumerate(row):
            x = column * (card_width + column_gap)
            color = palette(branch_index)
            parent, y = branch_nodes.get(branch_index, (guide, first_top))
            for number, group in topics:
                summary, cite = card_body(evidence, group, preview=preview)
                source = f"{fixed_source}#^e{cite}"
                reading = source if preview else f"{full_reading}#^topic-{number}"
                label = plain(group["title"].replace("|", "｜").replace("[", "［").replace("]", "］"))
                # 较长摘要把第一句单独成段，保留其余原句与模型已有的段落。
                if len(summary) > 105 and "\n" not in summary:
                    summary = re.sub(r"^(.+?[。！？]+[”’」』）】]*)(?=\S)", r"\1\n\n", summary, count=1)
                topic_text = f"### [[{reading}|{label}]]\n\n{plain(summary)}\n\n[[{source}|查看原文]]"
                chapter = ""
                if parent == guide and (continued or plain(branch).strip() != label.strip()):
                    chapter = plain(branch) + ("（续）" if continued else "")
                    topic_text = f"**{chapter}**\n\n{topic_text}"
                key = f"t{number}"
                height = article_card_height(group["title"], summary, card_width, chapter_label=chapter)
                card(key, topic_text, x, y, card_width, height, color)
                connect(parent, key, color)
                parent = key
                row_bottom = max(row_bottom, y + height)
                y += height + card_gap
        row_top = row_bottom + row_gap
    return {"nodes": nodes, "edges": edges}


def evidence_for(vault, identifier):
    if not re.fullmatch(r"[0-9a-f]{64}",identifier):
        raise RequestError("invalid_request")
    db=inside(vault,".afr/intake.sqlite")
    if not db.exists():
        raise RequestError("not_found")
    with contextlib.closing(sqlite3.connect(db.as_uri()+"?mode=ro",uri=True)) as conn:
        row=conn.execute("SELECT a.folder,r.evidence,r.reading FROM articles a JOIN receipts r ON r.sha=a.sha WHERE a.sha=?",(identifier,)).fetchone()
    if not row:
        raise RequestError("not_found")
    moved = locations.load(vault)
    actual = locations.resolve(row[0], moved)
    path = inside(vault, actual)
    raw = source_bytes(inside(vault, actual + "/证据索引.json"))
    if digest(raw) != row[1]:
        raise RequestError("evidence_changed")
    evidence = json.loads(raw)
    original = evidence["folder"]
    if evidence["sha256"] != identifier or locations.resolve(original, moved) != actual:
        raise RequestError("version_conflict")
    fixed = source_bytes(inside(vault, actual + "/固定原文.md"))
    if original != actual:
        mapping = {original: actual}
        try:
            normalized = locations.rewrite(fixed.decode("utf-8"), mapping, reverse=True)
        except UnicodeDecodeError:
            raise RequestError("evidence_changed") from None
        if locations.rewrite(normalized, mapping).encode() != fixed:
            raise RequestError("evidence_changed")
        fixed = normalized.encode()
    if digest(fixed) != row[2]:
        raise RequestError("evidence_changed")
    if digest(source_bytes(inside(vault, actual + "/原件"+evidence["suffix"]))) != identifier:
        raise RequestError("evidence_changed")
    return path,evidence


def publish(vault, identifier, plan):
    """外部计划作为本地草稿留存，正式页由独立校读流程签发。"""
    _, evidence = evidence_for(vault, identifier)
    from .semantic import fingerprint, receipt, validate

    if not isinstance(plan, dict) or plan.get("sha256") != identifier:
        raise RequestError("invalid_plan")
    groups = plan.get("groups")
    validate(groups, 1, len(evidence["parts"]))
    draft = {"sha256": identifier, "groups": groups}
    draft_sha = fingerprint(draft)
    path = receipt(vault, identifier, f"draft-{draft_sha}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        exclusive_json(path, draft)
    except FileExistsError:
        if json.loads(source_bytes(path)) != draft:
            raise RequestError("invalid_plan") from None
    return {"sha256": identifier, "state": "draft", "draft_sha256": draft_sha, "topics": len(groups)}


def topic_section(evidence, number, group, *, include_source_text=False):
    lines = [f"## {plain(group['title'])}", plain(group["argument"]), f"^topic-{number}", "### 原文依据"]
    for index in range(group["start"], group["end"] + 1):
        lines.append(f"[[{evidence['folder']}/固定原文.md#^e{index}|原文第 {index} 段]]")
        if include_source_text:
            original = plain(evidence["parts"][index - 1]["text"])
            lines.append("\n".join("> " + line for line in original.splitlines()))
    return "\n\n".join(lines)


def _source_heading(text):
    return " ".join(re.sub(r"^#{1,6}\s+", "", text.strip()).split())


def _reading_title(evidence):
    title = evidence["title"]
    first = next(iter(evidence["parts"]), None)
    if first and (first["kind"] == "title" or (first["kind"] == "heading" and (
            first["text"].startswith("# ") or re.fullmatch(r"export[_-]\d+", title, re.I)))):
        return _source_heading(first["text"]) or title
    return title


def reading_chapters(evidence, groups):
    """用白板主线组织章节，保留原文与各张卡片的顺序。"""
    branches = {}
    previous = None
    for number, group in enumerate(groups, 1):
        branch = " ".join(group["branch"].split())
        if branch != previous:
            branches[number] = branch
            previous = branch
    if len(branches) <= MAX_READING_CHAPTERS:
        return branches

    # 卡片主题过密时，沿用作者的章节边界。段内的小标题仍随正文阅读。
    outline = {}
    article_title = _source_heading(_reading_title(evidence))
    for number, group in enumerate(groups, 1):
        for part in evidence["parts"][group["start"] - 1:group["end"]]:
            if part["kind"] != "heading":
                continue
            title = _source_heading(part["text"])
            if title and title != article_title:
                outline[number] = title
                break
    if len(outline) < 2:
        outline = branches
    if 1 not in outline:
        outline = {1: branches[1], **outline}
    if len(outline) <= MAX_READING_CHAPTERS:
        return outline

    starts = list(outline)
    return {number: outline[number] for number in
            (starts[index * len(starts) // MAX_READING_CHAPTERS] for index in range(MAX_READING_CHAPTERS))}


def chapter_topic_section(evidence, number, group, *, chapter=None, canvas_path=None):
    if group["argument"] == ORIGINAL_ARGUMENT:
        parts = evidence["parts"][group["start"] - 1:group["end"]]
        body = []
        article_title = _source_heading(_reading_title(evidence))
        for part in parts:
            text = part["text"].strip()
            if part["kind"] in {"heading", "title"}:
                text = _source_heading(text)
                if not body and text in {chapter, article_title}:
                    continue
            if text:
                body.append(text)
        prose = "\n\n".join(body) or group["title"]
    else:
        prose = group["argument"].strip()
    prose = prose.replace("\r\n", "\n").replace("\r", "\n")
    first, separation, remaining = plain(prose).partition("\n\n")
    anchored = f"{first.rstrip()} ^topic-{number}{separation}{remaining}"
    if group.get("viewpoints"):
        source = f"{evidence['folder']}/固定原文.md"
        canvas = canvas_path or f"{evidence['folder']}/文章白板.canvas"
        for record in group["viewpoints"]:
            origin = {"reported_fact": "来源陈述", "interpretation": "解释", "forecast": "预测",
                      "recommendation": "建议", "uncertainty": "不确定性"}[record["kind"]]
            anchored += f"\n\n**{plain(record['judgment'])}** · {plain(record['speaker'])}（{origin}）"
            for label, field in (("依据", "grounds"), ("适用条件", "conditions")):
                if record[field]:
                    items = "；".join(plain(item) for item in record[field])
                    anchored += f"\n\n**{label}**　{items}"
            for citation in record["evidence"]:
                anchor = citation["number"]
                quote = plain(" ".join(citation["quote"].split()))
                anchored += f"\n\n> {quote} · [[{source}#^e{anchor}|原文第 {anchor} 段]]"
        anchored += f"\n\n[[{canvas}|返回文章白板]]"
    return f"<!-- AFR:TOPIC:{number} -->\n\n{anchored}\n\n<!-- /AFR:TOPIC:{number} -->"


def reading_markdown(evidence, groups, *, canvas_path=None, include_source_text=False, layout=None):
    folder = evidence["folder"]
    canvas_path = canvas_path or f"{folder}/文章白板.canvas"
    title = _reading_title(evidence) if layout == CHAPTER_READING_LAYOUT else evidence["title"]
    body = [f"# {plain(title)}", f"[[{canvas_path}|文章白板]]"]
    if layout != CHAPTER_READING_LAYOUT:
        body.extend(topic_section(evidence, number, group, include_source_text=include_source_text)
                    for number, group in enumerate(groups, 1))
        return "\n\n".join(body) + "\n"

    body.insert(0, CHAPTER_READING_MARKER)
    body[2] += f" · [[{folder}/固定原文.md|固定原文]]"
    chapters = reading_chapters(evidence, groups)
    for number, group in enumerate(groups, 1):
        chapter = chapters.get(number)
        if chapter:
            body.append(f"## {plain(chapter)}")
        body.append(chapter_topic_section(evidence, number, group, chapter=chapter, canvas_path=canvas_path))
        if number == len(groups) or number + 1 in chapters:
            first = max(start for start in chapters if start <= number)
            source = groups[first - 1]["start"]
            body.append(f"[[{folder}/固定原文.md#^e{source}|本节原文]]")
    return "\n\n".join(body) + "\n"


def _publish_reviewed(vault, identifier):
    from .semantic import missing_card_bodies, receipt, signed_plan

    folder, evidence = evidence_for(vault, identifier)
    plan = signed_plan(vault, identifier, evidence)
    groups = plan["groups"]
    if list(missing_card_bodies(groups)):
        raise RequestError("review_requires_human")
    display_evidence = {**evidence, "folder": folder.relative_to(vault).as_posix()}
    text = reading_markdown(display_evidence, groups, include_source_text=plan.get("includes_source_text", False),
                            layout=plan.get("reading_layout"))
    # 只新建；部分发布可用同一计划安全重试。人工改动通过冲突回执保留。
    with inside(vault,".afr/publish.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        reading=inside(vault,display_evidence["folder"]+"/完整阅读.md")
        canvas=inside(vault,display_evidence["folder"]+"/文章白板.canvas")
        content=board(display_evidence, groups, layout=plan.get("board_layout"))
        from .article_argument_boards import linked_text, project
        from .questions import _write_projection

        if reading.exists() and source_bytes(reading).decode("utf-8") != text:
            raise RequestError("user_edit_conflict")
        saved_canvas = canvas_bytes = None
        if canvas.exists():
            try:
                canvas_bytes = source_bytes(canvas)
                saved_canvas = json.loads(canvas_bytes)
            except (ValueError, UnicodeError):
                raise RequestError("user_edit_conflict") from None
            linked_nodes = [{**node, "text": linked_text(node["text"])} if "text" in node else node
                            for node in content["nodes"]]
            if saved_canvas not in (content, {**content, "nodes": linked_nodes}):
                raise RequestError("user_edit_conflict")
        project(vault, canvas, content, {reading.relative_to(vault).as_posix(): text})
        if not reading.exists():
            with tempfile.NamedTemporaryFile(dir=folder,delete=False) as tmp:
                tmp.write(text.encode()); tmp.flush(); os.fsync(tmp.fileno())
            try:
                os.link(tmp.name,reading)
            finally:
                Path(tmp.name).unlink()
        if canvas_bytes is None:
            exclusive_json(canvas,content)
        elif saved_canvas != content:
            # 旧进程可能在主板写入后、发布回执前退出。已核对旧主板完整一致。
            _write_projection(canvas, canvas_bytes, json.dumps(content, ensure_ascii=False, indent=2).encode())
        exclusive_json(receipt(vault, identifier, "publication"),
                       {"sha256": identifier, "plan_sha256": plan["plan_sha256"], "state": "published"})
    return {"sha256": identifier, "state": "published", "review": "passed",
            "board": canvas.relative_to(vault).as_posix(), "topics": len(groups)}
