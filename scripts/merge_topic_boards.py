#!/usr/bin/env python3
"""将可核验的旧主题时间线和分支移到库外恢复点。"""
import argparse
import fcntl
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afr_agent import knowledge
from check_vault_release import Vault


def candidates(vault, state):
    return {old: main for old, main in knowledge.superseded_topic_boards(state["topics"]).items()
            if old in state["projections"] and (vault / old).is_file() and not (vault / main).is_symlink()}


def eligible(vault, state, paths):
    protected = {old for old, main in paths.items()
                 if not (vault / main).is_file() or (vault / old).is_symlink()
                 or knowledge.digest((vault / old).read_bytes()) != state["projections"][old]}
    lookup = Vault(vault)
    links_to_old = {}
    if paths:
        for document in lookup.documents:
            links_to_old[document] = {target for link in lookup.document_refs(document)
                                      for target, _, error in [lookup.locate(document, link)]
                                      if not error and target in paths}
    previous = None
    while protected != previous:
        previous = set(protected)
        protected.update(target for source, targets in links_to_old.items()
                         if source not in paths or source in previous for target in targets)
    return paths.keys() - protected, protected


def apply(vault, backup):
    protected = knowledge.refresh_views(vault, force=True)
    if protected:
        raise SystemExit("以下视图有受保护内容。旧白板继续保留。\n" + "\n".join(protected))
    ledger = vault / knowledge.STATE / "ledger.json"
    with (ledger.parent / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = knowledge._state(ledger)
        ready, protected = eligible(vault, state, candidates(vault, state))
        if not ready:
            return 0, protected
        backup.mkdir(parents=True, exist_ok=True)
        with (backup / "ledger.before.json").open("xb") as saved:
            saved.write(ledger.read_bytes())
        for path in sorted(ready):
            destination = backup / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as saved:
                saved.write((vault / path).read_bytes())
        removed = []
        try:
            for path in sorted(ready):
                file = vault / path
                if file.read_bytes() != (backup / path).read_bytes():
                    raise RuntimeError(f"发布后有并发编辑：{path}")
                file.unlink()
                removed.append(path)
                state["projections"].pop(path)
            knowledge._save(ledger, state)
        except BaseException:
            for path in removed:
                shutil.copy2(backup / path, vault / path)
            raise
        for path in sorted(ready):
            directory = (vault / path).parent
            while directory != vault / knowledge.ROOT:
                try:
                    directory.rmdir()
                except OSError:
                    break
                directory = directory.parent
        return len(ready), protected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    vault = args.vault.resolve(strict=True)
    if args.apply:
        if args.backup is None or args.backup.resolve().is_relative_to(vault):
            parser.error("--apply 需要位于知识库之外的 --backup")
        count, protected = apply(vault, args.backup.resolve())
        print(f"已归档 {count} 张旧白板")
    else:
        state = knowledge._state(vault / knowledge.STATE / "ledger.json")
        ready, protected = eligible(vault, state, candidates(vault, state))
        print(f"当前可归档 {len(ready)} 张。执行时先刷新现有白板，再检查旧路径。")
    if protected:
        print("保留仍被引用、主白板缺失或有手工修改的旧路径")
        for path in sorted(protected):
            print(path)


if __name__ == "__main__":
    main()
