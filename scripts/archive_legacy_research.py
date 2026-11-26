#!/usr/bin/env python3
"""在 Obsidian 库外保存旧研究问题，切换阅读导航。默认只预览。"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile


OLD_DIRS = {"研究话题", "待确认研究问题"}
OLD_ENTRIES = {"研究话题总览.canvas", "话题阅读入口.md", "文章与研究话题对照.md", "研究问题索引.md"}
OLD_NOTES = {"资料核验/待判断论述.md"}
SUMMARY = "[[知识总结/知识总结总白板.canvas|知识总结总白板]]"
VERIFY_NOTE = "资料核验/知识库验收状态.md"
USAGE_NOTE = "知识库使用与待确认.md"
WORKSPACE = ".obsidian/workspace.json"
WIKILINK = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")


def old_path(value):
    path = Path(value.removeprefix("./"))
    return (
        bool(path.parts)
        and (path.parts[0] in OLD_DIRS or path.as_posix() in OLD_NOTES
             or path.name in OLD_ENTRIES or path.name + ".md" in OLD_ENTRIES
             or path.name == "待判断论述" or path.name == "待判断论述.md")
    )


def update_usage(text):
    text = re.sub(r"(?m)^- \[\[话题阅读入口.*\n", "", text)
    text = re.sub(r"(?m)^- 查看 22 个候选问题.*\n", "", text)
    text = re.sub(r"(?ms)^## 历史话题记录\n.*?(?=^## )", "", text)
    return re.sub(r"7 个暂缓研究问题涉及 21 条论述，作为来源对照留存，单篇材料已纳入本次交付。", "", text)


def update_verification(text):
    if "历史验收记录。当前跨篇主题从" not in text:
        text = text.replace(
            "# 知识库验收状态\n", "# 知识库验收状态\n\n"
            f"历史验收记录。当前跨篇主题从{SUMMARY}进入，早期话题白板对应的验收结论保留在下文。\n", 1,
        )
    return re.sub(
        r"\[\[资料核验/待判断论述\.md\|[^\]]+\]\] · ", "", text,
    )


def update_workspace(text, archive_paths):
    workspace = json.loads(text)
    def clean(value):
        if isinstance(value, dict):
            state = value.get("state")
            if isinstance(state, dict):
                file = state.get("file")
                if isinstance(file, str) and (old_path(file) or file in archive_paths):
                    if value.get("type") in {"markdown", "canvas"}:
                        value.clear()
                        value["type"] = "empty"
                    else:
                        state.pop("file")
            for child in value.values():
                clean(child)
        elif isinstance(value, list):
            value[:] = [child for child in value if not isinstance(child, str)
                        or not (old_path(child) or child in archive_paths)]
            for child in value:
                clean(child)
    clean(workspace)
    return json.dumps(workspace, ensure_ascii=False, indent=2) + "\n"


def plan(vault):
    archive_paths = sorted(
        p for p in OLD_DIRS | OLD_ENTRIES | OLD_NOTES if (vault / p).exists() or (vault / p).is_symlink()
    )
    for path in (vault / "资料库").glob("*/*/AFR 自动归类.md"):
        if path.is_symlink():
            raise ValueError(f"无法归档符号链接：{path}")
        text = path.read_text()
        if text.startswith("# AFR 自动归类\n\n依据完整话题关联已有研究问题。"):
            archive_paths.append(path.relative_to(vault).as_posix())

    if not archive_paths:
        return [], {}
    updates = {}
    archive_set = set(archive_paths)
    for path in vault.rglob("*"):
        rel = path.relative_to(vault)
        if path.is_symlink() or any(part.startswith(".") for part in rel.parts):
            continue
        if any(rel.is_relative_to(archived) for archived in archive_paths):
            continue
        if path.suffix not in {".md", ".canvas", ".base"} or not path.is_file():
            continue
        if path.name == "固定原文.md":
            continue
        original = path.read_bytes()
        text = original.decode("utf-8")
        if rel.as_posix() == USAGE_NOTE:
            text = update_usage(text)
        elif rel.as_posix() == VERIFY_NOTE:
            text = update_verification(text)
        links = [match for match in WIKILINK.finditer(text)
                 if old_path(match[1]) or match[1] in archive_set]
        if links and rel.as_posix() not in {VERIFY_NOTE, USAGE_NOTE}:
            raise ValueError(f"发现现役页面的旧链接，需核对后再迁移：{rel} · {links[0][0]}")
        if links:
            text = WIKILINK.sub(lambda m: (m[2] or Path(m[1]).stem)
                               if old_path(m[1]) or m[1] in archive_set else m[0], text)
        if text.encode() != original:
            updates[rel.as_posix()] = (original, text.encode())

    workspace = vault / WORKSPACE
    if workspace.is_file():
        original = workspace.read_bytes()
        decoded = original.decode()
        updated = update_workspace(decoded, archive_set)
        if json.loads(decoded) != json.loads(updated):
            updates[WORKSPACE] = (original, updated.encode())

    for name in [*archive_paths, *updates]:
        original = vault / name
        for path in (original, *original.parents):
            if path == vault:
                break
            if path.is_symlink():
                raise ValueError(f"无法安全迁移符号链接：{path}")
        if original.is_dir() and any(p.is_symlink() for p in original.rglob("*")):
            raise ValueError(f"旧目录内有符号链接：{original}")
    return archive_paths, updates


def replace_file(path, data):
    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def apply(vault, backup, archive_paths, updates):
    if not archive_paths:
        return
    if backup.exists() or backup.is_symlink():
        raise ValueError("备份目录须为尚未创建的新目录")
    backup.mkdir(mode=0o700)
    if backup.stat().st_dev != vault.stat().st_dev:
        backup.rmdir()
        raise ValueError("备份须与原库在同一文件系统")
    (backup / "manifest.json").write_text(json.dumps({
        "vault": str(vault), "archived": archive_paths, "before": sorted(updates)
    }, ensure_ascii=False, indent=2) + "\n")
    for name, (original, _) in updates.items():
        path = backup / "before" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vault / name, path)
        if path.read_bytes() != original:
            raise ValueError(f"预览后文件已变化：{name}")

    moved = []
    edited = []
    try:
        for name in archive_paths:
            path = backup / "archived" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            (vault / name).rename(path)
            moved.append(name)
        for name, (original, updated) in updates.items():
            if (vault / name).read_bytes() != original:
                raise ValueError(f"预览后文件已变化：{name}")
            replace_file(vault / name, updated)
            edited.append(name)
    except (OSError, ValueError):
        for name in reversed(edited):
            replace_file(vault / name, updates[name][0])
        for name in reversed(moved):
            (backup / "archived" / name).rename(vault / name)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        vault = args.vault.expanduser().resolve(strict=True)
        if not (vault / "知识总结" / "知识总结索引.md").is_file():
            raise ValueError("指定库缺少现役知识总结索引")
        archive_paths, updates = plan(vault)
        if args.apply:
            if args.backup is None:
                raise ValueError("执行时须明确指定库外备份目录")
            backup = args.backup.expanduser().resolve()
            if backup.is_relative_to(vault) or vault.is_relative_to(backup):
                raise ValueError("备份须位于 Obsidian 库外")
            apply(vault, backup, archive_paths, updates)
        print(json.dumps({"archived": archive_paths, "updated": sorted(updates),
                          "applied": args.apply}, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
