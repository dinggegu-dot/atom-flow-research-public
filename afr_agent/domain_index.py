"""Link signed reports from the owner's existing Obsidian domain indexes."""

import fcntl
from pathlib import PurePosixPath
import re

from . import locations, questions, semantic
from .filing import DOMAIN_TERMS, PENDING_DOMAIN
from .intake import inside, source_bytes
from .library import RequestError


_BEGIN = "<!-- AFR:DOMAIN-INDEX:BEGIN -->"
_END = "<!-- AFR:DOMAIN-INDEX:END -->"


def index_path(vault, domain, root=locations.ARCHIVE_ROOT):
    name = "缺少既有分类.md" if root == locations.LEGACY_ROOT and domain == PENDING_DOMAIN else "本领域资料索引.md"
    return inside(vault, f"{root}/{domain}/{name}")


def initialize(vault):
    """Create missing index notes without changing existing notes."""
    if not vault.is_dir():
        raise RequestError("invalid_vault")
    links = []
    for domain in locations.ARCHIVE_DOMAINS:
        index = index_path(vault, domain)
        if not index.exists():
            questions._write_projection(index, None, f"# {domain}\n".encode("utf-8"))
        links.append(f"- [[{index.relative_to(vault).as_posix()}|{domain}]]")
    overview = inside(vault, f"{locations.ARCHIVE_ROOT}/资料索引.md")
    if not overview.exists():
        text = "# 资料索引\n\n" + "\n".join(links) + "\n"
        questions._write_projection(overview, None, text.encode("utf-8"))


def ensure_entry(vault, folder, sha):
    """Append links once. An absent index returns None so the workflow can retry."""
    paths = semantic.publication_paths(vault, sha, folder)
    article = PurePosixPath(paths["reading"]).parent
    if len(article.parts) != 3:
        return False
    root, domain, _ = article.parts
    domains = (*DOMAIN_TERMS, PENDING_DOMAIN) if root == locations.LEGACY_ROOT else locations.ARCHIVE_DOMAINS
    if root not in {locations.ARCHIVE_ROOT, locations.LEGACY_ROOT} or domain not in domains:
        return False
    index = index_path(vault, domain, root)
    if not index.is_file():
        return None

    reading = paths["reading"]
    canvas = paths["canvas"]
    evidence = str(article / "固定原文.md")
    if not all(inside(vault, path).is_file() for path in (reading, canvas, evidence)):
        raise RequestError("domain_index_missing_article")
    title = re.sub(r" · [0-9a-f]{16}$", "", article.name)
    title = title.replace("|", "｜").replace("[", "［").replace("]", "］")
    line = f"- [[{reading}|{title}]] · [[{canvas}|文章白板]] · [[{evidence}|固定原文]]"

    inside(vault, ".afr").mkdir(exist_ok=True)
    with inside(vault, ".afr/domain-index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = source_bytes(index)
        try:
            text = before.decode("utf-8")
        except UnicodeError as exc:
            raise RequestError("domain_index_invalid_encoding") from exc
        # 旧索引手写链接可能省略 Markdown 扩展名。
        target = re.escape(reading.removesuffix(".md"))
        if re.search(rf"\[\[{target}(?:\.md)?(?=[|\]])", text):
            return False
        start = text.count(_BEGIN)
        end = text.count(_END)
        if start == end == 0:
            separator = "" if text.endswith("\n\n") or not text else ("\n" if text.endswith("\n") else "\n\n")
            updated = f"{text}{separator}{_BEGIN}\n## 自动归档\n\n{line}\n{_END}\n"
        elif start == end == 1 and text.index(_BEGIN) < text.index(_END):
            marker = text.index(_END)
            updated = f"{text[:marker]}{line}\n{text[marker:]}"
        else:
            raise RequestError("domain_index_invalid_section")
        questions._write_projection(index, before, updated.encode("utf-8"))
    return True
