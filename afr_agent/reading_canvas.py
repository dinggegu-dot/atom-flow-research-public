"""正式阅读白板共用的布局、证据和库内入口。"""
from math import ceil
from pathlib import PurePosixPath
import re

from . import locations, semantic
from .board_style import ROOT_COLOR, branch_color
from .intake import inside, plain, source_bytes
from .library import RequestError

HOME = "AFR 阅读首页.canvas"
ARCHIVE = "资料档案/资料档案.canvas"
AREAS = ("科技与产业", "宏观与金融", "企业与投资", "治理与地缘", "社会与劳动", "认知与方法", "综合研究")
PARAGRAPHS = re.compile(r"(?ms)^##[ \t]+e(\d+)\s*\n(.*?)\n[ \t]*\^e\1[ \t]*$")


def fixed_blocks(vault, path, cache):
    if vault is None:
        return {}
    if path not in cache:
        try:
            body = source_bytes(inside(vault, path)).decode("utf-8")
            cache[path] = {int(number): " ".join(text.split()) for number, text in PARAGRAPHS.findall(body)}
        except (OSError, UnicodeError, RequestError):
            cache[path] = {}
    return cache[path]


def _excerpt(text, quote="", limit=300):
    if len(text) <= limit:
        return text
    offset = text.find(quote) if 0 < len(quote) <= limit else -1
    start = min(max(0, offset - 45), len(text) - limit)
    if start:
        boundary = max(text.rfind(mark, 0, start + 1) for mark in "。！？")
        if 0 <= start - boundary < 45:
            start = boundary + 1
    end = min(len(text), start + limit)
    if end < len(text):
        boundary = max(text.rfind(mark, start + limit // 2, end) for mark in "。！？")
        if boundary >= 0:
            end = boundary + 1
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _explicit_reason(headline, claim, candidates):
    """选择原文已经说出的解释，保留原句和所在段。"""
    def phrases(text):
        parts = re.findall(r"[一-鿿]{3,}|[A-Za-z]{3,}\d*", text)
        return {part[i:i+3].casefold() for part in parts for i in range(len(part) - 2)}

    keywords = phrases(headline) | phrases(claim)
    marker = re.compile(r"因为|由于|原因|理由|所以|因此|才会|导致|取决于|源于|带动|从而|使得|用来说明|解释为")
    best = None
    for text, target in candidates:
        if not text or len(text) < 24:
            continue
        for sentence in re.findall(r"[^。！？]+[。！？]?", text):
            sentence = sentence.strip(" •")
            overlap = len(phrases(sentence) & keywords)
            if overlap < 3 or not marker.search(sentence) or len(sentence) < 22:
                continue
            score = (overlap, min(len(sentence), 160))
            if best is None or best[0] < score:
                best = (score, _excerpt(sentence, limit=220), target)
    return best[1:] if best else None


def archive_entries(vault, categories, signed_boards):
    """按现有归档和已签发主题提供导航。材料不移动。"""
    catalog = {}
    for top in ("资料档案", "资料库"):
        root = vault / top
        if not root.is_dir():
            continue
        for item in sorted(root.iterdir()):
            if not item.is_dir() or item.is_symlink():
                continue
            if (item / "固定原文.md").is_file():
                folders = (item,)
            else:
                folders = (entry for entry in sorted(item.iterdir()) if entry.is_dir()
                           and not entry.is_symlink() and (entry / "固定原文.md").is_file())
            for folder in folders:
                relative = folder.relative_to(vault).as_posix()
                identity = folder.name.rsplit(" · ", 1)[-1]
                area = item.name if item.name in AREAS else categories.get(relative, "综合研究")
                board = signed_boards.get(relative, f"{relative}/文章白板.canvas")
                entry = (area, relative, folder.name.rsplit(" · ", 1)[0], board if (vault / board).is_file() else None)
                if identity not in catalog or top == "资料档案":
                    catalog[identity] = entry
    return tuple(catalog.values())


def archive_pages(vault, categories, node, dump, height, *, signed_boards=None):
    entries = archive_entries(vault, categories, signed_boards or {})
    cards = [node("root", f"# 资料档案\n\n[[{HOME}|返回阅读首页]]", ROOT_COLOR, 0, 0, 1080, 134)]
    pages = {}
    for index, area in enumerate(AREAS):
        group = sorted((entry for entry in entries if entry[0] == area), key=lambda item: item[2])
        if not group:
            continue
        relative = f"资料档案/{area}/{area}.canvas"
        color = branch_color(index)
        cards.append(node(f"area-{index}", f"## [[{relative}|{area}]]\n\n{len(group)} 篇材料",
                          color, len(pages) % 2 * 554, 176 + len(pages) // 2 * 152, 526, 116))
        group_cards = [node("root", f"# {area}\n\n[[{ARCHIVE}|返回资料档案]]", color, 0, 0, 1080, 134)]
        y = 180
        for start in range(0, len(group), 2):
            row = []
            for column, (_, folder, title, board) in enumerate(group[start:start + 2]):
                links = f"[[{folder}/固定原文.md|固定原文]]"
                if board:
                    links = f"[[{board}|文章白板]] · {links}"
                text = f"## {plain(title)}\n\n{links}"
                row.append(node(f"article-{start+column}", text, color, column * 554, y, 526,
                                max(128, ceil(height(text) * 0.86) + 26)))
            group_cards.extend(row)
            y += max(card["height"] for card in row) + 36
        pages[relative] = dump({"nodes": group_cards, "edges": []})
    checks = "资料档案/读取核验/读取核验.canvas"
    if (vault / checks).is_file():
        cards[0]["text"] += f"\n\n[[{checks}|读取核验]]"
        cards[0]["height"] += 32
    pages[ARCHIVE] = dump({"nodes": cards, "edges": []})
    return pages


def home_page(sections, node, dump, height):
    links = f"[[{ARCHIVE}|资料档案]]　·　[[知识总结/知识总结总白板.canvas|知识总结]]"
    cards = [node("root", f"# AFR 阅读首页\n\n{links}", ROOT_COLOR, 0, 0, 1080, 152)]
    y = 194
    for index, (title, lines) in enumerate(sections):
        if not lines:
            continue
        text = f"## {title}\n\n" + "\n".join(lines)
        card_height = max(146, ceil(height(text) * 0.85) + 48)
        cards.append(node(f"section-{index}", text, branch_color(index), 0, y, 1080, card_height))
        y += card_height + 46
    return dump({"nodes": cards, "edges": []})


def argument(event, overview, timeline, color, mapping, contexts, history, source_context,
             headline, date, node, height, vault, cache):
    """同一条纵线展示作者的理由以及紧邻的固定原文。"""
    source = locations.resolve(event["source_path"], mapping)
    filename, _, _ = source.partition("#")
    paragraphs = fixed_blocks(vault, filename, cache)
    number = event.get("evidence_number")
    identifier = event["id"]
    nodes, y = [], 0

    def add(part, blocks, shade=color):
        nonlocal y
        text = "\n\n".join(blocks)
        card_height = max(112, ceil(height(text) * 0.82) + 44)
        node_id = semantic.fingerprint([identifier, part])[:16]
        nodes.append(node(node_id, text, shade, 0, y, 740, card_height))
        y += card_height + 24

    def paragraph_link(n):
        return f"[[{filename}#^e{n}|原文第 {n} 段]]"

    material = plain(event["article_title"])
    origin = plain(event["origin"] or event["article_title"])
    metadata = f"{origin} · {date} · {plain(event['kind'])}"
    header = [f"## {plain(headline)}", f"*{metadata}*"]
    if event["article_title"] != event["origin"]:
        header.append(f"材料　{material}")
    if event.get("date"):
        header.append(f"材料提到的时间　{plain(event['date'])}")
    add("argument", header)

    support_blocks = []
    for path in event.get("support_paths", ()):
        target = locations.resolve(path, mapping)
        support_file, _, anchor = target.partition("#^e")
        if anchor.isdecimal():
            text = fixed_blocks(vault, support_file, cache).get(int(anchor), "")
            support_blocks.append((text, target, anchor))
    primary = paragraphs.get(number) or source_context or event["quote"]

    curated = event.get("reading_reason", {})
    if curated.get("steps"):
        for index, step in enumerate(curated["steps"]):
            cited = [paragraph_link(n) for n in step["anchors"] if n in paragraphs]
            add(f"reason-{index}", [f"### {plain(step['title'])}", plain(step["body"]),
                                    " · ".join(cited) or f"[[{source}|固定原文]]"])
        if curated.get("limit"):
            add("condition", ["### 判断适用到哪里", plain(curated["limit"])])

    elif event.get("context") or contexts:
        if event.get("context"):
            add("why", ["### 作者给出的理由", plain(event["context"]), f"[[{source}|依据原文]]"])
        for index, (_, branch, item) in enumerate(contexts):
            anchor = item.get("anchor")
            link = filename + "#^" + anchor if anchor else source
            details = [f"### {plain(branch)}", plain(item["reason"]), f"[[{link}|依据原文]]"]
            if item.get("scope"):
                details.append(plain(item["scope"]))
            add(f"branch-reason-{index}", details)
    else:
        if " ".join(event["claim"].split()) != " ".join(headline.split()):
            add("signed-claim", ["### 材料中的论述", plain(event["claim"]), f"[[{source}|定位原文]]"])
        candidates = [(primary, source), *((text, target) for text, target, _ in support_blocks)]
        if reason := _explicit_reason(headline, event["claim"], candidates):
            text, target = reason
            add("reason-excerpt", ["### 作者的解释摘录", f"> {plain(text)}", f"[[{target}|定位这一段]]"])

    if not curated.get("steps"):
        if event.get("estimate"):
            add("estimate", ["### 测算与假设", plain(event["estimate"]), f"[[{source}|依据原文]]"])
        if event.get("to_verify"):
            add("scope", ["### 判断边界", plain(event["to_verify"])])

    evidence = ["### 固定原文依据", f"> {plain(_excerpt(primary, event['quote']))}",
                f"[[{source}|原文第 {number} 段]]" if number else f"[[{source}|固定原文]]"]
    if not curated.get("steps"):
        raw_parts = 0
        duplicate_text = {primary}
        for support, evidence_path, anchor in support_blocks:
            if len(support) < 18 or support in duplicate_text or support.startswith(("发言人", "说话人", "（保留元素位置）")):
                continue
            duplicate_text.add(support)
            if raw_parts < 3:
                evidence.extend([f"> {plain(_excerpt(support))}", f"[[{evidence_path}|原文第 {anchor} 段]]"])
                raw_parts += 1
        if raw_parts == 0 and len(primary) < 25 and not event.get("context") and not contexts:
            evidence.append("这段材料给出的解释有限。可打开固定原文继续核对。")
    remaining = [f"[[{locations.resolve(path, mapping)}|原文 {path.rsplit('#^', 1)[-1]}]]"
                 for path in event.get("support_paths", ())]
    if remaining:
        evidence.append(" · ".join(remaining))
    add("sources", evidence, ROOT_COLOR)

    for index, link in enumerate(event.get("evidence_links", ())):
        target = locations.resolve(link["source_path"], mapping)
        add(f"extra-source-{index}", [f"### {plain(link['relation'])} · {plain(link['origin'] or link['article_title'])}",
                                     f"> {plain(link['quote'])}", f"[[{target}|原文第 {link['evidence_number']} 段]]"],
            ROOT_COLOR)

    nav = [f"[[{timeline}|回到问题白板]]"]
    if overview != timeline:
        nav.append(f"[[{overview}|回到主题论证]]")
    nav.extend(f"[[{path}|回到{plain(title)}]]" for path, title, _ in contexts if path not in (overview, timeline))
    links = [f"[[{filename}|完整固定原文]]"]
    article = str(PurePosixPath(filename).with_name("文章白板.canvas"))
    if vault is not None and (vault / article).is_file():
        links.append(f"[[{article}|文章白板]]")
    add("navigation", [" · ".join(links), " · ".join([*nav, *filter(None, history)])], ROOT_COLOR)
    return {"nodes": nodes, "edges": []}
