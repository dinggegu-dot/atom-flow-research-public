"""保留已签发文章的路径身份，同时解析正式归档位置。"""
import json
import os
import re
import stat
import unicodedata

from .filing import DOMAIN_TERMS, PENDING_DOMAIN


ARCHIVE_ROOT = "资料档案"
LEGACY_ROOT = "资料库"
GENERAL_DOMAIN = "综合研究"
ARCHIVE_DOMAINS = (*DOMAIN_TERMS, GENERAL_DOMAIN)
MANIFEST = ".afr/article-locations.json"
_DOMAINS = {*DOMAIN_TERMS, PENDING_DOMAIN}
_LIMIT = 1024 * 1024
_HASH_SUFFIX = re.compile(r".+ · ([0-9a-f]{8}|[0-9a-f]{16})$")
_INVALID_NAME = re.compile(r"[\x00-\x1f\x7f\\\[\]#^|:*?\"<>]")
_WIKI_LINK = re.compile(r"(?<!\\)\[\[([^\[\]\r\n|#]+)(?=(?:[#|][^\[\]\r\n]*)?\]\])")


def _unique_object(pairs):
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("duplicate_location_key")
    return values


def _folder(relative):
    if not isinstance(relative, str):
        return None
    parts = relative.split("/")
    if len(parts) == 2 and parts[0] == ARCHIVE_ROOT:
        root, name = parts
        domain = None
    elif len(parts) == 3 and parts[0] in {LEGACY_ROOT, ARCHIVE_ROOT}:
        root, domain, name = parts
    else:
        return None
    match = _HASH_SUFFIX.fullmatch(name)
    if match is None or name.startswith(".") or _INVALID_NAME.search(name):
        return None
    return root, domain, match[1]


def archive_folder(domain, name):
    if domain not in ARCHIVE_DOMAINS:
        domain = GENERAL_DOMAIN
    return f"{ARCHIVE_ROOT}/{domain}/{name}"


def _valid_move(old, new):
    if old is None or new is None or old[2] != new[2]:
        return False
    if new[0] == LEGACY_ROOT:
        return old[:2] == (LEGACY_ROOT, "新收研报") and new[1] in _DOMAINS and len(new[2]) == 16
    return new[1] in ARCHIVE_DOMAINS and (
        old[0] == LEGACY_ROOT and old[1] in _DOMAINS | {"新收研报"} and len(old[2]) == 16
        or old[:2] == (ARCHIVE_ROOT, None)
    )


def load(vault):
    """只接受同一固定原件按历史目录顺序迁入资料档案的映射。"""
    from .library import RequestError

    path = vault / MANIFEST
    if path.parent.is_symlink():
        raise RequestError("invalid_article_locations")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT:
                raise ValueError("invalid_location_file")
            data = stream.read(_LIMIT + 1)
            if len(data) != info.st_size or os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns:
                raise ValueError("invalid_location_file")
            manifest = json.loads(data, object_pairs_hook=_unique_object)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise RequestError("invalid_article_locations") from None
    if (not isinstance(manifest, dict) or set(manifest) != {"version", "folders"}
            or type(manifest["version"]) is not int or manifest["version"] != 1
            or not isinstance(manifest["folders"], dict)):
        raise RequestError("invalid_article_locations")

    mapping = manifest["folders"]
    destinations = set()
    for source, target in mapping.items():
        old, new = _folder(source), _folder(target)
        if not _valid_move(old, new):
            raise RequestError("invalid_article_locations")
        destination = unicodedata.normalize("NFC", target).casefold()
        if destination in destinations:
            raise RequestError("invalid_article_locations")
        destinations.add(destination)
    return mapping


def _resolve(relative, paths):
    path, anchor, fragment = relative.partition("#")
    candidate = path
    while candidate not in paths:
        candidate, separator, _ = candidate.rpartition("/")
        if not separator:
            return relative
    current = candidate
    visited = set()
    while current in paths:
        if current in visited:
            raise ValueError("cyclic_article_locations")
        visited.add(current)
        current = paths[current]
    return current + path[len(candidate):] + anchor + fragment


def resolve(relative, mapping, *, reverse=False):
    if not mapping:
        return relative
    paths = {destination: source for source, destination in mapping.items()} if reverse else mapping
    return _resolve(relative, paths)


def rewrite(text, mapping, *, reverse=False):
    """重写 Obsidian wikilink 的归档路径，保留原句、标题、锚点与别名。"""
    if not mapping or "[[" not in text:
        return text
    paths = {destination: source for source, destination in mapping.items()} if reverse else mapping
    return _WIKI_LINK.sub(lambda match: "[[" + _resolve(match[1], paths), text)
