"""从已有篇内卡及完整阅读投影论述白板，保留原文与文章主板布局。"""
import json
from pathlib import Path
import re

from .intake import inside, safe_name, source_bytes
from .library import RequestError

TITLE = re.compile(r"(?m)^(#{1,6} )\[\[([^\[\]|\n]+\.md)#\^(topic-[\w-]+)\|((?:\\.|[^\]\\\n])+)\]\]")
DETAIL = re.compile(r"(?m)^#{1,6} \[\[(\./篇内论述[^\[\]|\n]+\.canvas)\|")
ANCHOR = re.compile(r"(?m)(?:^|[ \t])\^(topic-[\w-]+)[ \t]*$")
MARKED_SECTION = re.compile(r"(?ms)^<!-- AFR:TOPIC:(\d+) -->[ \t]*\n(.*?)^<!-- /AFR:TOPIC:\1 -->[ \t]*$")


def _target(match):
    _, reading, anchor, title = match.groups()
    variant = Path(reading).stem.removeprefix("完整阅读")
    label = safe_name(re.sub(r"\\([^\w])", r"\1", title))
    return f"./篇内论述{variant}/{label} · {anchor.removeprefix('topic-')}.canvas"


def detail_relative(text):
    if match := DETAIL.search(text):
        return match[1]


def linked_text(text):
    if match := TITLE.search(text):
        return text[:match.start(2)] + _target(match) + text[match.end(3):]
    return text


def _sections(text):
    if text.startswith("<!-- AFR:READING:chapters_v1 -->\n"):
        sections = {}
        for mark in MARKED_SECTION.finditer(text):
            anchor = f"topic-{mark[1]}"
            block = mark[2]
            if [match[1] for match in ANCHOR.finditer(block)] == [anchor] and anchor not in sections:
                sections[anchor] = ANCHOR.sub("", block).strip()
        return sections

    headings = list(re.finditer(r"(?m)^## (?!#).+$", text))
    bounds = [heading.start() for heading in headings] + [len(text)]
    sections = {}
    for start, end in zip(bounds, bounds[1:]):
        block = text[start:end]
        anchors = list(ANCHOR.finditer(block))
        for idx, anchor in enumerate(anchors):
            if len(anchors) > 1:
                begin = anchors[idx-1].end() if idx else 0
                part = block[begin:anchor.end()]
            else:
                part = block
            part = re.sub(r"(?m)^#{1,6} .*?$|^<!-- AFR[^\n]*-->$|[ \t]*\^topic-[\w-]+\s*$", "", part)
            sections[anchor[1]] = part.strip()
    return sections


def project(vault, canvas_path, canvas, readings=None):
    """先写完整论述，再由调用方发布主板入口。旧文章和别名阅读稿共用同一流程。"""
    from .questions import _write_projection
    readings = {} if readings is None else readings
    folder = canvas_path.parent
    sections_by_reading = {}
    total = 0
    for node in canvas["nodes"]:
        text = node.get("text", "")
        m = TITLE.search(text)
        if not m:
            continue
        _, reading, anchor, label = m.groups()
        source = inside(vault, reading)
        if source.parent != folder:
            raise RequestError("article_argument_source_mismatch")
        if reading not in sections_by_reading:
            source_text = readings.get(reading)
            if source_text is None:
                source_text = source_bytes(source).decode("utf-8")
            sections_by_reading[reading] = _sections(source_text)
        if anchor not in sections_by_reading[reading]:
            raise RequestError("article_argument_anchor_missing")
        prose = sections_by_reading[reading][anchor]
        if not prose:
            raise RequestError("article_argument_text_missing")
        target_text = linked_text(text)
        detail = inside(vault, (folder / _target(m)).relative_to(vault))
        inside_refs = folder.relative_to(vault).as_posix() + "/"
        body = text[m.end():].strip().replace("[["+inside_refs, "[[../")
        back = f"[[../{canvas_path.name}|返回文章白板]] · [[../{source.name}#^{anchor}|按原序读]]"
        title_text = f"## {label}\n\n{body}\n\n{back}"
        parts = [title_text]
        chunk = ""
        for paragraph in prose.replace("[["+inside_refs, "[[../").split("\n\n"):
            if chunk and len(chunk) + len(paragraph) > 800:
                parts.append(chunk)
                chunk = ""
            chunk += ("\n\n" if chunk else "") + paragraph
        if chunk:
            parts.append(chunk)
        items = []
        y = 0
        for n, part in enumerate(parts):
            visible = part if n == 0 else ("### 具体论述\n\n" if n == 1 else "") + part
            display = re.sub(r'\[\[[^\]\n]+\|([^\]\n]+)\]\]',r'\1',visible)
            height = 65 + sum(28 * max(1,(len(line)+32)//33) for line in display.splitlines())
            items.append({"id": f"argument-{n}", "type":"text", "x":0, "y":y, "width":680,
                          "height":height, "text":visible, "color":node.get("color","#a7b9bd")})
            y += height + 32
        data = {"nodes":items, "edges":[]}
        if detail.exists():
            try:
                saved = json.loads(source_bytes(detail))
            except (ValueError, UnicodeError):
                raise RequestError("user_edit_conflict") from None
            if saved != data:
                raise RequestError("user_edit_conflict")
        else:
            _write_projection(detail, None, json.dumps(data, ensure_ascii=False, indent=2).encode())
        node["text"] = target_text
        total += 1
    return total


def migrate(vault):
    from .questions import _write_projection
    totals = {"article_boards":0, "arguments":0}
    for name in ("资料档案","资料库","新收研报"):
        for board in sorted((vault / name).rglob("文章白板*.canvas")):
            original = board.read_bytes()
            data = json.loads(original)
            count = project(vault, board, data)
            if count:
                _write_projection(board, original, json.dumps(data, ensure_ascii=False, indent=2).encode())
                totals["article_boards"] += 1
                totals["arguments"] += count
    return totals
