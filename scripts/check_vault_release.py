#!/usr/bin/env python3
"""只读检查 AFR 上线副本及 Obsidian 内部链接。"""

import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

# 口径沿用发布器的账本命名和 Markdown 转义，其余检查直接读取产物。
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afr_agent import article_argument_boards as article, knowledge, locations

WIKI = re.compile(r"(?<!\\)\[\[([^\[\]\n]+)\]\]")
OPEN_URI = re.compile(r"obsidian://open\?[^\s<)>\]]+")
BLOCK = re.compile(r"(?m)(?:^|[ \t])\^([\w-]+)[ \t]*$")
HEADING = re.compile(r"(?m)^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
CARD = re.compile(r"(?m)^#{1,6} \[\[([^\[\]|\n]*(?:/篇内论述|\./篇内论述)[^\[\]|\n]*\.canvas)\|([^\]\n]+)\]\]")
ARTICLE_DIRS = ("资料档案", "资料库", "新收研报")
OLD_ROOTS = ("研究话题", "待确认研究问题", "研究话题总览.canvas",
             "话题阅读入口.md", "文章与研究话题对照.md", "研究问题索引.md",
             "资料核验/待判断论述.md")


def normalized(value):
    return unicodedata.normalize("NFC", value).casefold()


def target(value):
    return value.split("|", 1)[0].removesuffix("\\").strip()


def refs(text):
    for match in WIKI.finditer(text):
        yield target(match[1])
    for match in OPEN_URI.finditer(text):
        yield match[0]


def split_ref(value):
    path, _, fragment = value.partition("#")
    return path, fragment


class Vault:
    def __init__(self, root, aliases=()):
        self.root = root
        self.aliases = (root, *aliases)
        self.files = {}
        self.names = defaultdict(list)
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or any(part.startswith(".") for part in PurePosixPath(relative).parts):
                continue
            if path.is_file():
                self.files[normalized(relative)] = relative
                names = {path.name, path.stem} if path.suffix == ".md" else {path.name}
                for name in names:
                    self.names[normalized(name)].append(relative)
        self.documents = sorted(path for path in self.files.values()
                                if PurePosixPath(path).suffix in {".md", ".canvas", ".base"})

    @lru_cache(maxsize=None)
    def content(self, relative):
        return (self.root / relative).read_text(encoding="utf-8")

    @lru_cache(maxsize=None)
    def canvas(self, relative):
        data = json.loads(self.content(relative))
        if not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
            raise ValueError(f"{relative} 缺少 Canvas 节点或连线")
        return data

    @lru_cache(maxsize=None)
    def fragments(self, relative):
        path = PurePosixPath(relative)
        if path.suffix == ".md":
            text = self.content(relative)
            return (frozenset(BLOCK.findall(text)), frozenset(
                normalized(unquote(heading.strip())) for heading in HEADING.findall(text)))
        if path.suffix == ".canvas":
            return frozenset(node["id"] for node in self.canvas(relative)["nodes"]), frozenset()
        return frozenset(), frozenset()

    def locate(self, source, raw):
        encoded = not raw.startswith("obsidian://")
        uri_fragment = ""
        if not encoded:
            uri = urlsplit(raw)
            query = parse_qs(uri.query)
            raw = query.get("path", query.get("file", [""]))[0]
            uri_fragment = unquote(uri.fragment)
            if not raw:
                return None, "", "Obsidian URL 无目标"
        path, fragment = split_ref(raw)
        if encoded:
            path, fragment = unquote(path), unquote(fragment)
        fragment = fragment or uri_fragment
        path = path.replace("\\ ", " ")
        if path.startswith("/"):
            for alias in self.aliases:
                if path.startswith(str(alias) + "/"):
                    path = path[len(str(alias)) + 1:]
                    break
            else:
                path = path.lstrip("/")
        parent = posixpath.dirname(source)
        if not path:
            candidates = [source]
        elif path.startswith(("./", "../")):
            candidates = [posixpath.normpath(posixpath.join(parent, path))]
        elif "/" in path:
            candidates = [posixpath.normpath(path), posixpath.normpath(posixpath.join(parent, path))]
        else:
            candidates = [posixpath.join(parent, path), path]

        for candidate in candidates:
            for spelling in (candidate, candidate + ".md"):
                if real := self.files.get(normalized(spelling)):
                    return real, fragment, None
        if path and not path.startswith(("./", "../")):
            matches = self.names[normalized(posixpath.basename(path))]
            if "/" in path:
                endings = (normalized("/" + path), normalized("/" + path + ".md"))
                matches = [item for item in matches if normalized("/" + item).endswith(endings)]
            if len(matches) == 1:
                return matches[0], fragment, None
            if len(matches) > 1:
                return None, fragment, "简称指向多份文件"
        return None, fragment, "缺文件"

    def check(self, source, raw):
        relative, fragment, error = self.locate(source, raw)
        if error or not fragment:
            return error
        suffix = PurePosixPath(relative).suffix
        if suffix == ".pdf" and re.fullmatch(r"page=[1-9]\d*(?:&.*)?", fragment):
            return None
        if suffix in {".md", ".canvas"}:
            blocks, headings = self.fragments(relative)
            if fragment.startswith("^"):
                if fragment[1:] in blocks:
                    return None
            elif normalized(fragment) in headings or fragment in blocks:
                return None
            return "缺锚点"
        return None

    def document_refs(self, relative):
        if relative.endswith(".canvas"):
            for node in self.canvas(relative)["nodes"]:
                yield from refs(node.get("text", ""))
                if node.get("type") == "file":
                    yield node["file"] + node.get("subpath", "")
        else:
            yield from refs(self.content(relative))


def scan(vault):
    broken = {}
    count = 0
    for relative in vault.documents:
        try:
            for raw in vault.document_refs(relative):
                count += 1
                if error := vault.check(relative, raw):
                    broken[(relative, raw)] = error
        except (OSError, ValueError, KeyError) as exc:
            broken[(relative, "")] = f"文件无法解析：{exc}"
    return count, broken


def knowledge_checks(vault, args, errors, counts):
    ledger = json.loads((vault.root / knowledge.STATE / "ledger.json").read_bytes())
    topics = ledger["topics"]
    mapping = locations.load(vault.root)
    names = knowledge._topic_path_map(topics)
    index_path = f"{knowledge.ROOT}/知识总结索引.md"
    index = vault.content(index_path)
    indexed = set(refs(index))
    expected_paths = set()
    counts["topics"] = len(topics)
    counts["events"] = sum(len(topic["events"]) for topic in topics.values())
    if args.expected_topics is not None and counts["topics"] != args.expected_topics:
        errors.append(f"知识账本应有 {args.expected_topics} 个主题，实际 {counts['topics']}")
    if args.expected_events is not None and counts["events"] != args.expected_events:
        errors.append(f"知识账本应有 {args.expected_events} 条事件，实际 {counts['events']}")

    category_paths = {key: knowledge._category_path(key)
                      for key in {knowledge._category(topic) for topic in topics.values()}}
    counts["categories"] = len(category_paths)
    for category, relative in category_paths.items():
        if not (vault.root / relative).is_file() or relative not in indexed:
            errors.append(f"知识总结缺少分类总览：{relative}")
            continue
        for topic in (identifier for identifier, record in topics.items() if knowledge._category(record) == category):
            _, board = knowledge._topic_paths(topic, names)
            if not any(target == board for target in vault.document_refs(relative)):
                errors.append(f"分类总览缺主白板入口：{board}")
    for identifier, topic in topics.items():
        _, canvas = knowledge._topic_paths(identifier, names)
        arguments = knowledge._argument_paths(canvas, topic["events"])
        expected_paths.update(arguments.values())
        if canvas not in indexed:
            errors.append(f"主题索引缺少主白板：{identifier} · {canvas}")
        try:
            chronology = vault.canvas(canvas)
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"主题主白板不可读：{identifier} · {exc}")
            continue
        timeline_nodes = {node["id"]: node for node in chronology["nodes"]}
        for event in topic["events"]:
            event_id = event["id"]
            detail = arguments[event_id]
            title = knowledge.plain(knowledge._headline(event))
            source = locations.resolve(event["source_path"], mapping)
            source_path, source_fragment = split_ref(source)
            label = f"{identifier} / {event_id} ({detail})"
            card = timeline_nodes.get(event_id, {}).get("text", "")
            if f"[[{detail}|{title}]]" not in card:
                errors.append(f"时间线该事件没有具体标题和对应入口：{label}")
            try:
                texts = [node.get("text", "") for node in vault.canvas(detail)["nodes"]]
                body = "\n\n".join(texts)
            except (OSError, ValueError, KeyError) as exc:
                errors.append(f"论述白板不可读：{label} · {exc}")
                continue
            if not title.strip() or not event["claim"].strip() or not event["quote"].strip():
                errors.append(f"账本事件缺标题、判断或引段：{label}")
            if not any(text.startswith(f"## {title}\n") for text in texts) or canvas not in set(refs(body)):
                errors.append(f"论述白板缺具体标题或问题主白板回链：{label}")
            if source not in set(refs(body)) or not source_fragment.startswith("^") or vault.check(detail, source):
                errors.append(f"论述白板未指向固定原文锚点：{label} · {source_path}#{source_fragment}")
            else:
                counts["knowledge_events_with_source"] += 1

    actual = {path for path in vault.documents if path.startswith(f"{knowledge.ROOT}/")
              and PurePosixPath(path).parent.name == "论述" and path.endswith(".canvas")}
    for path in sorted(actual - expected_paths):
        errors.append(f"知识论述白板无账本事件：{path}")


def baseline_nodes(baseline, board):
    if not baseline or board not in baseline.files.values():
        return {}
    return {node["id"]: article.TITLE.search(node.get("text", "")) for node in baseline.canvas(board)["nodes"]}


def article_checks(vault, baseline, errors, counts):
    for board in vault.documents:
        path = PurePosixPath(board)
        if not (path.parts[0] in ARTICLE_DIRS and path.name.startswith("文章白板") and path.suffix == ".canvas"):
            continue
        counts["article_boards"] += 1
        old_nodes = baseline_nodes(baseline, board)
        data = vault.canvas(board)
        nodes = {node["id"]: node for node in data["nodes"]}
        for node_id in sorted(set(old_nodes) - set(nodes)):
            if old_nodes[node_id]:
                errors.append(f"文章白板原有论点卡消失：{board} · {node_id}")
        for node_id, node in nodes.items():
            text = node.get("text", "")
            old = old_nodes.get(node_id)
            card = CARD.search(text)
            if not (old or card or article.TITLE.search(text)):
                continue
            counts["article_argument_cards"] += 1
            label = f"{board} · {node_id}"
            if not card:
                errors.append(f"篇内论点标题仍未打开论述白板：{label}")
                continue
            pointer, title = card.groups()
            detail, _, error = vault.locate(board, pointer)
            if error or PurePosixPath(detail).parent.parent != path.parent:
                errors.append(f"篇内论述未落在同篇文章：{label} · {pointer}")
                continue
            try:
                details = [node.get("text", "") for node in vault.canvas(detail)["nodes"]]
            except (OSError, ValueError, KeyError):
                errors.append(f"篇内论述白板不可读：{label} · {detail}")
                continue
            if not details or not details[0].startswith(f"## {title}\n") or len(details) < 2:
                errors.append(f"篇内论述缺标题或正文：{label} · {detail}")
                continue
            joined = "\n\n".join(details)
            back = [raw for raw in refs(joined) if vault.locate(detail, raw)[:2] == (board, "")]
            exact = [(p, anchor) for raw in refs(details[0]) for p, anchor, err in [vault.locate(detail, raw)]
                     if not err and PurePosixPath(p).parent == path.parent
                     and PurePosixPath(p).name.startswith("完整阅读") and anchor.startswith("^topic-")]
            if len(set(exact)) != 1 or not back:
                errors.append(f"篇内论述缺唯一的阅读锚点或返回主板入口：{label} · {detail}")
                continue
            reading, anchor = exact[0]
            if old and (reading, anchor, title) != (old[2], "^" + old[3], old[4]):
                errors.append(f"篇内论述跳回的原阅读段与迁移前不一致：{label}")
                continue
            if not detail.endswith(f" · {anchor.removeprefix('^topic-')}.canvas"):
                errors.append(f"篇内论述文件对应的锚点与实际阅读段不同：{label}")
            prose = article_sections(vault, reading).get(anchor[1:], "")
            # 归档后链接可能用同篇相对地址或库内完整地址。比较解析后的目标、锚点和别名。
            expected = normalized_body(vault, reading, prose)
            actual = normalized_body(vault, detail, "\n\n".join(details[1:]).removeprefix("### 具体论述\n\n"))
            if not expected or expected != actual or vault.check(detail, f"../{PurePosixPath(reading).name}#{anchor}"):
                errors.append(f"篇内论述正文或锚点与完整阅读不一致：{label}")
            else:
                counts["article_arguments_with_reading"] += 1


def normalized_body(vault, source, text):
    def canonical(match):
        raw, separator, label = match[1].partition("|")
        file, fragment, error = vault.locate(source, raw)
        if error:
            return match[0]
        return "[[" + file + (f"#{fragment}" if fragment else "") + separator + label + "]]"

    return WIKI.sub(canonical, text).split()


@lru_cache(maxsize=None)
def article_sections(vault, reading):
    return article._sections(vault.content(reading))


def old_entry_checks(vault, errors):
    for old in OLD_ROOTS:
        if (vault.root / old).exists():
            errors.append(f"仍有旧入口：{old}")
    for path in vault.documents:
        if path.startswith("资料库/") and path.endswith("/AFR 自动归类.md"):
            if "依据完整话题关联已有研究问题。" in vault.content(path):
                errors.append(f"仍有旧自动归类入口：{path}")
    workspace = vault.root / ".obsidian/workspace.json"
    if workspace.is_file():
        found_refs = set(workspace_refs(json.loads(workspace.read_bytes())))
        for found in sorted(found_refs):
            if any(found == old or found.startswith(old + "/") for old in OLD_ROOTS):
                errors.append(f"Obsidian 工作区仍引用旧入口：{found}")
            elif re.match(r"^(?:知识总结/)?k-[a-z\d-]+(?:[./]|$)", found):
                errors.append(f"Obsidian 工作区仍引用旧知识主题文件名或标签：{found}")


def workspace_refs(value):
    if isinstance(value, dict):
        for child in value.values():
            if isinstance(child, str):
                yield child
            else:
                yield from workspace_refs(child)
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, str):
                yield child
            else:
                yield from workspace_refs(child)


def digest(vault, relative):
    return hashlib.sha256(vault.content(relative).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--vault-alias", type=Path, action="append", default=[],
                        help="副本链接仍使用正式库绝对路径时，指定正式库根路径")
    parser.add_argument("--expected-topics", type=int)
    parser.add_argument("--expected-events", type=int)
    args = parser.parse_args()
    vault = Vault(args.vault.resolve(strict=True), args.vault_alias)
    baseline = Vault(args.baseline.resolve(strict=True), [vault.root, *args.vault_alias]) if args.baseline else None
    if baseline is not None:
        vault.aliases += (baseline.root,)
    errors = []
    counts = Counter()
    old_entry_checks(vault, errors)
    for title, check in (("知识总结", lambda: knowledge_checks(vault, args, errors, counts)),
                         ("文章白板", lambda: article_checks(vault, baseline, errors, counts))):
        try:
            check()
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"{title}检查无法继续：{exc}")
    checked, broken = scan(vault)
    counts["links_checked"] = checked
    before_count, before = scan(baseline) if baseline else (0, {})
    result = {"counts": dict(counts), "checks_failed": errors}
    rows = lambda issues: [dict(file=path, target=raw, issue=issues[path, raw]) for path, raw in sorted(issues)]
    result["existing_link_issues"] = rows({key: error for key, error in broken.items() if before.get(key) == error})
    result["new_link_issues"] = rows({key: error for key, error in broken.items() if before.get(key) != error})
    if baseline:
        counts = result["counts"]
        counts["baseline_links_checked"] = before_count
        counts["baseline_link_issues"] = len(before)
        result["resolved_link_issues"] = rows({key: error for key, error in before.items()
                                               if key not in broken and normalized(key[0]) in vault.files})
        common = vault.files.keys() & baseline.files.keys()
        documents = {normalized(path) for path in vault.documents}
        result["added_documents"] = sorted(vault.files[key] for key in documents - baseline.files.keys())
        result["rewritten_documents"] = sorted(vault.files[key] for key in documents & common
                                               if digest(vault, vault.files[key]) != digest(baseline, baseline.files[key]))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors or result["new_link_issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
