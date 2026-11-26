"""直接从当前知识库读取，不复制研究正文到外部检索服务。"""
from bisect import insort
from collections import Counter
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import quote

from . import locations


class RequestError(ValueError):
    pass


def notes(vault: Path):
    if not vault.is_dir():
        raise RequestError("invalid_vault")
    for parent, dirs, files in os.walk(vault):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not Path(parent, d).is_symlink())
        for name in sorted(files):
            path = Path(parent, name)
            if name.endswith(".md") and not name.startswith(".") and not path.is_symlink() and path.is_file():
                yield path


def sections(vault, path, skipped, mapping=None):
    relative = path.relative_to(vault).as_posix()
    if mapping is None:
        mapping = locations.load(vault)
    logical_path = locations.resolve(relative, mapping, reverse=True)
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        skipped.append(relative)
        return
    marks = list(re.finditer(r"^#{1,6}[ \t]+([^\r\n]+)", source, re.MULTILINE))
    starts = [0] + [m.start() for m in marks if m.start() > 0] + [len(source)]
    counts = {}
    titles = Counter(m[1].strip() for m in marks)
    block_pattern = r"(?:^|[ \t])\^([a-zA-Z0-9-]+)[ \t]*$"
    anchors = Counter(re.findall(block_pattern, source, re.MULTILINE))
    line = 1
    for start, end in zip(starts, starts[1:]):
        current_line = line
        line += source.count("\n", start, end)
        body = source[start:end].strip()
        if not body:
            continue
        title = re.match(r"^#{1,6}[ \t]+([^\n]+)", body)
        heading = title[1].strip() if title else ""
        ordinal = counts.get(heading, 0)
        counts[heading] = ordinal + 1
        identity = f"{logical_path}\0{heading}\0{ordinal}".encode()
        ref = hashlib.sha256(identity).hexdigest()
        revision = hashlib.sha256(body.encode()).hexdigest()
        anchor = ""
        if heading and titles[heading] == 1:
            anchor = "#" + heading
        else:
            block = re.search(block_pattern, body, re.MULTILINE)
            if block and anchors[block[1]] == 1:
                anchor = "#^" + block[1]
        target = relative + anchor
        uri = "obsidian://open?vault=" + quote(vault.name, safe="") + "&file=" + quote(target, safe="")
        yield dict(ref=ref, revision=revision, path=relative, heading=heading, line=current_line, uri=uri), body


def note_kind(vault, path):
    relative = path.relative_to(vault)
    if relative.parts[:1] in (("知识总结",), ("研究话题",), ("待确认研究问题",)):
        return "topic"
    if relative.parts[:1] in (("资料档案",), ("资料库",)):
        if path.stem == "固定原文":
            return "source"
        if path.stem in {"完整阅读", "完整阅读（本轮校读）"}:
            return "article"
    return "note"


def search(vault, query, limit, scope="all"):
    words = query.casefold().split()
    if not words or not 1 <= limit <= 100 or scope not in {"all","article","topic","source","note"}:
        raise RequestError("invalid_request")
    matches = []
    skipped = []
    mapping = locations.load(vault)
    for path in notes(vault):
        kind = note_kind(vault, path)
        if scope != "all" and kind != scope:
            continue
        for record, body in sections(vault, path, skipped, mapping):
            record["kind"] = kind
            title = record["heading"].casefold()
            label = path.stem.casefold()
            folded = body.casefold()
            if not all(w in folded or w in label for w in words):
                continue
            rank = sum(3 * (w in title) + 2 * (w in label) for w in words)
            where = next((folded.index(w) for w in words if w in folded), 0)
            record["excerpt"] = body[max(0, where - 40):where + 160]
            insort(matches, (-rank, record["path"], record["ref"], record))
            if len(matches) > limit:
                matches.pop()
    return {"hits": [item[-1] for item in matches], "skipped_notes": len(skipped)}


def read(vault, ref, revision):
    if not all(re.fullmatch(r"[0-9a-f]{64}", v) for v in (ref, revision)):
        raise RequestError("invalid_request")
    skipped = []
    mapping = locations.load(vault)
    for path in notes(vault):
        for record, body in sections(vault, path, skipped, mapping):
            if record["ref"] == ref:
                if record["revision"] != revision:
                    raise RequestError("revision_changed")
                return {**record, "text": body}
    raise RequestError("not_found")
