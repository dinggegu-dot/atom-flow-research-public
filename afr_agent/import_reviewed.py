"""Add an already reviewed, five-version AFR snapshot without replacing any file."""

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from afr_agent import questions, semantic
from afr_agent.intake import inside, source_bytes
from afr_agent.library import RequestError
from afr_agent.presentation import evidence_for

SUMMARY = "五篇文章总结.md"
QUESTION_DB = ".afr/questions.sqlite"
WORKFLOW_DB = ".afr/workflow.sqlite"
RECONCILIATION = ".afr/reconciliation"


def require(condition, label):
    if not condition:
        raise RequestError(label)


@contextlib.contextmanager
def readonly(vault, name):
    file = inside(vault, name)
    require(file.is_file() and not any(Path(str(file) + suffix).exists()
                for suffix in ("-wal", "-shm", "-journal")), "sqlite_snapshot_required")
    uri = file.as_uri() + "?mode=ro&immutable=1"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as connection:
        require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "sqlite_integrity")
        yield connection


def json_bytes(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()


def rewrite_links(text, mapping):
    if text is None:
        return None
    for old, new in mapping.items():
        pattern = r"(\[\[)" + re.escape(old) + r"(?=[#|\]])"
        text = re.sub(pattern, lambda match: match[1] + new, text)
    return text


def rewrite_path(path, mapping):
    base, separator, anchor = path.partition("#")
    return mapping.get(base, base) + separator + anchor


def assets(source, destination):
    blobs = {}
    mapping = {}
    plans = {}
    originals = set()
    reused = []
    renamed = 0

    def add(path, data):
        target = inside(destination, path)
        if target.is_file() and source_bytes(target) == data:
            reused.append(path)
            return
        require(not target.exists() and path not in blobs, "destination_file_conflict")
        for parent in target.parents:
            if parent == destination:
                break
            require(not parent.exists() or parent.is_dir(), "destination_parent_conflict")
        blobs[path] = data

    with readonly(source, WORKFLOW_DB) as db:
        workflow_rows = db.execute("SELECT sha256, folder, phase FROM workflow").fetchall()
        reviews = db.execute("SELECT state FROM cross_article_review").fetchall()
    require(len(workflow_rows) == 5 and reviews == [("ready",)], "expected_five_reviewed_articles")
    for sha, folder, phase in sorted(workflow_rows):
        require(bool(re.fullmatch(r"[0-9a-f]{64}", sha)) and phase == "ready", "invalid_workflow")
        _, evidence = evidence_for(source, sha)
        _, dest_evidence = evidence_for(destination, sha)
        require(evidence == dest_evidence and evidence["folder"] == folder, "official_evidence_mismatch")
        names = ["固定原文.md", "证据索引.json", "原件" + evidence["suffix"]]
        if inside(source, f"{folder}/原文结构.canvas").is_file():
            names.append("原文结构.canvas")
        for name in names:
            path = f"{folder}/{name}"
            require(source_bytes(inside(source, path)) == source_bytes(inside(destination, path)),
                    "official_original_mismatch")
            originals.add(path)

        plan = semantic.reviewed_plan(source, sha)
        plans[sha] = plan
        publication = semantic.receipt(source, sha, "publication")
        record = semantic.read_json(publication)
        require("paths" not in record, "source_requires_canonical_publications")
        paths = semantic.publication_paths(source, sha, folder, publication=record)
        data = {kind: source_bytes(inside(source, path)) for kind, path in paths.items()}
        for number in range(1, len(plan["groups"]) + 1):
            questions.read_topic(source, sha, number)
        for path in paths.values():
            existing = inside(destination, path)
            require(not existing.exists() or existing.is_file(), "destination_publication_conflict")
        if any(inside(destination, path).is_file() and source_bytes(inside(destination, path)) != data[kind]
               for kind, path in paths.items()):
            record["paths"] = {"reading": f"{folder}/完整阅读（本轮校读）.md",
                               "canvas": f"{folder}/文章白板（本轮校读）.canvas"}
            aliases = semantic.publication_paths(source, sha, folder, publication=record)
            links = {path: aliases[kind] for kind, path in paths.items()}
            mapping.update(links)
            renamed += 1
            data["reading"] = rewrite_links(data["reading"].decode(), links).encode()
            canvas = json.loads(data["canvas"])
            for node in canvas["nodes"]:
                if "text" in node:
                    node["text"] = rewrite_links(node["text"], links)
                if "file" in node:
                    node["file"] = rewrite_path(node["file"], links)
            data["canvas"] = json_bytes(canvas)
            paths = aliases
        for kind, path in paths.items():
            add(path, data[kind])
        for file in sorted(publication.parent.glob("*.json")):
            relative = file.relative_to(source).as_posix()
            add(relative, json_bytes(record) if file == publication and "paths" in record
                else source_bytes(inside(source, relative)))

    for file in sorted(inside(source, RECONCILIATION).glob("*.json")):
        relative = file.relative_to(source).as_posix()
        add(relative, source_bytes(inside(source, relative)))

    with readonly(source, QUESTION_DB) as db:
        revisions = db.execute("SELECT id, revision, payload FROM revisions ORDER BY id, revision").fetchall()
        projections = db.execute("SELECT path, current, previous FROM projections ORDER BY path").fetchall()
    new_revisions = []
    expected_questions = {questions.INDEX}
    for identifier, revision, payload in revisions:
        record = json.loads(payload)
        expected_questions.add(record["path"])
        require(record["id"] == identifier and record["revision"] == revision, "invalid_question_revision")
        for link in record["links"]:
            number, sha = link["topic_id"], link["sha256"]
            require(sha in plans and 1 <= number <= len(plans[sha]["groups"]), "invalid_question_topic")
            topic = questions.read_topic(source, sha, number)
            require(link["topic_revision"] == topic["revision"] and link["path"] == topic["path"],
                    "invalid_question_topic_revision")
            link["path"] = rewrite_path(link["path"], mapping)
        new_revisions.append((identifier, revision, json.dumps(record, ensure_ascii=False)))
    require(expected_questions == {p[0] for p in projections}, "question_projection_mismatch")
    new_projections = []
    for path, current, previous in projections:
        require(path.startswith(questions.ROOT + "/") and path.endswith(".md"), "invalid_question_projection")
        require(not inside(destination, path).exists(), "destination_question_projection_conflict")
        original = source_bytes(inside(source, path)).decode()
        first, stop = original.find(questions.BEGIN), original.find(questions.END)
        stop += len(questions.END)
        require(first >= 0 and stop > first and original[first:stop] in (current, previous),
                "question_projection_conflict")
        add(path, rewrite_links(original, mapping).encode())
        new_projections.append((path, rewrite_links(current, mapping), rewrite_links(previous, mapping)))

    require(not inside(destination, SUMMARY).exists(), "destination_summary_conflict")
    add(SUMMARY, rewrite_links(source_bytes(inside(source, SUMMARY)).decode(), mapping).encode())
    for path in (QUESTION_DB, WORKFLOW_DB):
        require(not inside(destination, path).exists(), "destination_database_conflict")
    report = dict(articles=len(plans), topics=sum(len(plan["groups"]) for plan in plans.values()),
                  renamed_article_pairs=renamed, preserved_fixed_files=len(originals),
                  imported_question_revisions=len(new_revisions), imported_projections=len(new_projections),
                  added_files=len(blobs) + 2, reused_files=len(reused))
    return blobs, plans, originals, new_revisions, new_projections, report


def database_bytes(source, name, staging, revisions=None, projections=None):
    target = staging / Path(name).name
    with readonly(source, name) as original, contextlib.closing(sqlite3.connect(target)) as db:
        original.backup(db)
        if revisions is not None:
            with db:
                db.executemany("UPDATE revisions SET payload=? WHERE id=? AND revision=?",
                               [(payload, identifier, rev) for identifier, rev, payload in revisions])
                db.executemany("UPDATE projections SET current=?, previous=? WHERE path=?",
                               [(current, previous, path) for path, current, previous in projections])
        require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "sqlite_integrity")
    return target.read_bytes()


def stage_and_verify(source, destination, blobs, plans, originals, revisions, projections):
    with tempfile.TemporaryDirectory(prefix="afr-five-stage-") as temporary:
        staging = Path(temporary)
        blobs[QUESTION_DB] = database_bytes(source, QUESTION_DB, staging, revisions, projections)
        blobs[WORKFLOW_DB] = database_bytes(source, WORKFLOW_DB, staging)
        overlay = staging / "vault"
        overlay.mkdir()
        for path in sorted(originals | {".afr/intake.sqlite"} | blobs.keys()):
            file = inside(overlay, path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(blobs[path] if path in blobs else source_bytes(inside(destination, path)))
        checked_topics = {}
        for sha, plan in plans.items():
            require(semantic.reviewed_plan(overlay, sha) == plan, "signed_plan_changed")
            for number in range(1, len(plan["groups"]) + 1):
                topic = questions.read_topic(overlay, sha, number)
                checked_topics[sha, number] = (topic["path"], topic["revision"])
        for identifier, revision, _ in revisions:
            record = questions.read(overlay, identifier, revision)
            for link in record["links"]:
                require(checked_topics[link["sha256"], link["topic_id"]] ==
                        (link["path"], link["topic_revision"]), "staged_question_reference_mismatch")


def install(destination, blobs):
    created = []
    created_dirs = []
    try:
        for relative, data in sorted(blobs.items(), key=lambda item: item[0] in (QUESTION_DB, WORKFLOW_DB)):
            path = inside(destination, relative)
            parents = []
            parent = path.parent
            while not parent.exists():
                parents.append(parent)
                parent = parent.parent
            for parent in reversed(parents):
                parent.mkdir()
                created_dirs.append(parent)
            with tempfile.NamedTemporaryFile(dir=path.parent) as temp:
                temp.write(data)
                temp.flush()
                os.fsync(temp.fileno())
                os.link(temp.name, path)
                created.append((path, os.fstat(temp.fileno()).st_ino, hashlib.sha256(data).digest()))
        return
    except BaseException:
        for path, inode, expected in reversed(created):
            if (path.is_file() and path.stat().st_ino == inode
                    and hashlib.sha256(source_bytes(path)).digest() == expected):
                path.unlink()
        for path in reversed(created_dirs):
            with contextlib.suppress(OSError):
                path.rmdir()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    try:
        require(source.is_dir() and destination.is_dir() and source != destination
                and not source.is_relative_to(destination) and not destination.is_relative_to(source),
                "independent_vaults_required")
        blobs, plans, originals, revisions, projections, report = assets(source, destination)
        if args.apply:
            stage_and_verify(source, destination, blobs, plans, originals, revisions, projections)
            install(destination, blobs)
        print(json.dumps({"mode": "applied" if args.apply else "planned", **report}))
    except (RequestError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        # Avoid serializing exception details which may include private names or payloads.
        code = str(error) if isinstance(error, RequestError) else type(error).__name__
        parser.exit(1, f"Import stopped: {code}\n")


if __name__ == "__main__":
    main()
