"""本地研究问题、固定论述与可重建的 Obsidian 入口。"""
import contextlib
from datetime import datetime, timezone
import fcntl
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unicodedata
from urllib.parse import quote

from . import locations
from .intake import digest, inside, plain, source_bytes
from .library import RequestError
from .presentation import (CHAPTER_READING_MARKER, board, chapter_topic_section, evidence_for,
                           reading_chapters, supplement_card_text, topic_section)

RELATIONS = {
    "SUPPORTS": "支持",
    "CONTRADICTS": "反驳",
    "QUALIFIES": "口径不同",
    "BACKGROUND": "背景",
    "UNVERIFIED": "待证",
}
ROOT = "研究话题/研究问题"
INDEX = ROOT + "/研究问题索引.md"
ARCHIVE = "待确认研究问题/AFR 自动草稿存档"
BEGIN = "<!-- AFR:BEGIN -->\n"
END = "<!-- AFR:END -->\n"
MANUAL = "\n## 自由笔记\n\n"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _question_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"q-[0-9a-f]{32}", value):
        raise RequestError("invalid_request")
    return value


def _positive(value):
    return type(value) is int and value >= 1


def read_topic(vault, sha, topic_id):
    from .semantic import card_supplements, publication_paths, reviewed_plan

    if not _positive(topic_id) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise RequestError("invalid_request")
    folder, evidence = evidence_for(vault, sha)
    plan = reviewed_plan(vault, sha)
    if topic_id > len(plan["groups"]):
        raise RequestError("not_found")
    group = plan["groups"][topic_id - 1]
    expected = topic_section(evidence, topic_id, group, include_source_text=plan.get("includes_source_text", False))
    actual_folder = folder.relative_to(vault).as_posix()
    mapping = {evidence["folder"]: actual_folder} if evidence["folder"] != actual_folder else {}
    display = {**evidence, "folder": actual_folder}
    citations = []
    for number in range(group["start"], group["end"] + 1):
        part = evidence["parts"][number - 1]
        path = f"{actual_folder}/固定原文.md#^e{number}"
        citations.append(dict(number=number, path=path, text=part["text"],
                              locator=part["locator"], element=part.get("element")))
    paths = publication_paths(vault, sha, evidence["folder"], mapping=mapping)
    try:
        reading = source_bytes(inside(vault, paths["reading"])).decode("utf-8")
        canvas = json.loads(source_bytes(inside(vault, paths["canvas"])))
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError) as exc:
        raise RequestError("article_publication_changed") from exc
    path = f"{paths['reading']}#^topic-{topic_id}"
    if reading.startswith(CHAPTER_READING_MARKER + "\n"):
        chapter = reading_chapters(evidence, plan["groups"]).get(topic_id)
        projected = chapter_topic_section(display, topic_id, group, chapter=chapter, canvas_path=paths["canvas"])
        matches = reading.count(projected) == 1
    else:
        sections = re.split(r"(?m)(?=^## )", reading)
        matches = (len(sections) > topic_id
                   and sections[topic_id].rstrip("\n") == locations.rewrite(expected, mapping))
    anchors = re.findall(rf"(?m)(?:^|[ \t])\^topic-{topic_id}[ \t]*$", reading)
    nodes = canvas.get("nodes") if isinstance(canvas, dict) else None
    if not matches or len(anchors) != 1 or not isinstance(nodes, list):
        raise RequestError("article_publication_changed")
    node_id = f"t{topic_id}"
    expected_board = board(display, plan["groups"], layout=plan.get("board_layout"))
    expected_node = next(node["text"] for node in expected_board["nodes"] if node["id"] == node_id)
    expected_node = expected_node.replace(f"[[{actual_folder}/完整阅读.md#^topic-{topic_id}|", f"[[{path}|", 1)
    valid_text = {expected_node}
    supplement = card_supplements(vault, sha, evidence, plan).get(topic_id)
    if supplement:
        updated = supplement_card_text(expected_node, display, group, supplement,
                                       archive=plan.get("board_layout") is not None)
        valid_text = {updated}
    if plan.get("board_layout") is None:
        # 旧签发白板重排时可在卡首补同一已签发主线或续页标题。
        branch = plain(" ".join(group["branch"].split()))
        captions = {plain(group["branch"]), branch, branch + "（续）"}
        valid_text.update(f"**{caption}**\n\n{item}" for item in tuple(valid_text) for caption in captions)
    from .article_argument_boards import detail_relative, linked_text
    valid_text.update(linked_text(item) for item in tuple(valid_text))
    matching_nodes = [node for node in nodes if isinstance(node, dict) and node.get("id") == node_id]
    if (len(matching_nodes) != 1
            or matching_nodes[0].get("type") != "text"
            or matching_nodes[0].get("text") not in valid_text):
        raise RequestError("article_publication_changed")
    if detail := detail_relative(matching_nodes[0]["text"]):
        if not inside(vault, (folder / detail).relative_to(vault)).is_file():
            raise RequestError("article_publication_changed")
    return dict(sha256=sha, topic_id=topic_id, title=group["title"], text=group["argument"],
                path=path, citations=citations, article_title=evidence["title"],
                article_published_at=evidence.get("published_at"),
                revision=digest(expected.encode("utf-8")))


@contextlib.contextmanager
def _reader(vault):
    if not vault.is_dir():
        raise RequestError("invalid_vault")
    path = inside(vault, ".afr/questions.sqlite")
    if not path.exists():
        yield None
        return
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        yield db


@contextlib.contextmanager
def _writer(vault):
    if not vault.is_dir():
        raise RequestError("invalid_vault")
    inside(vault, ".afr").mkdir(exist_ok=True)
    with inside(vault, ".afr/questions.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with contextlib.closing(sqlite3.connect(inside(vault, ".afr/questions.sqlite"))) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (id, revision)
                );
                CREATE TABLE IF NOT EXISTS projections (
                    path TEXT PRIMARY KEY, current TEXT NOT NULL, previous TEXT
                );
                CREATE TABLE IF NOT EXISTS archived_questions (
                    id TEXT PRIMARY KEY, batch TEXT NOT NULL
                );
            """)
            _upgrade_projection_queue(vault, db)
            _replay_projections(vault, db)
            yield db


def _record(db, identifier, revision=None):
    if db is None:
        raise RequestError("not_found")
    sql = "SELECT payload FROM revisions WHERE id=?"
    args = (identifier,)
    if revision is None:
        sql += " ORDER BY revision DESC LIMIT 1"
    else:
        sql += " AND revision=?"
        args += (revision,)
    row = db.execute(sql, args).fetchone()
    if row is None:
        raise RequestError("not_found")
    return json.loads(row[0])


def _all(db, *, include_archived=False):
    if db is None:
        return []
    archived = set()
    if not include_archived and db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archived_questions'"
    ).fetchone():
        archived = {row[0] for row in db.execute("SELECT id FROM archived_questions")}
    rows = db.execute("""
        SELECT id, payload FROM revisions JOIN
        (SELECT id, max(revision) AS revision FROM revisions GROUP BY id) USING (id, revision)
        ORDER BY id
    """)
    return [json.loads(payload) for identifier, payload in rows if identifier not in archived]


def _view(record):
    return {key: record[key] for key in ("id", "revision", "title", "path", "links")}


def catalog(vault):
    with _reader(vault) as db:
        records = _all(db)
    return [
        dict(id=record["id"], revision=record["revision"], title=record["title"],
             links=[{key: item[key] for key in ("sha256", "topic_id")} for item in record["links"]])
        for record in records
    ]


def search(vault, query, limit, *, visible=None):
    if not isinstance(query, str) or not query.strip() or not _positive(limit) or limit > 100:
        raise RequestError("invalid_request")
    words = query.casefold().split()
    with _reader(vault) as db:
        records = [_view(record) for record in _all(db)]
    matches = [
        record for record in records
        if (visible is None or visible(record))
        and all(word in (record["title"] + " " + record["id"]).casefold() for word in words)
    ]
    return {"hits": sorted(matches, key=lambda item: (item["title"], item["id"]))[:limit]}


def read(vault, id, revision=None):
    identifier = _question_id(id)
    if revision is not None and not _positive(revision):
        raise RequestError("invalid_request")
    with _reader(vault) as db:
        record = _record(db, identifier, revision)
    return {**record, "uri": "obsidian://open?vault=" + quote(vault.name, safe="")
            + "&file=" + quote(record["path"], safe="")}


def _normalize(title):
    return " ".join(unicodedata.normalize("NFKC", title).casefold().split())


def _assignments(vault, sha, assignments):
    if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or not isinstance(assignments, list)):
        raise RequestError("invalid_plan")
    topics = {}
    results = []
    for item in assignments:
        base = {"topic_id", "relation", "reason"}
        if not isinstance(item, dict) or set(item) not in (base | {"question_id"}, base | {"new_question"}):
            raise RequestError("invalid_plan")
        number, relation, reason = item["topic_id"], item["relation"], item["reason"]
        if (not _positive(number) or not isinstance(relation, str) or relation not in RELATIONS
                or not isinstance(reason, str) or not reason.strip()):
            raise RequestError("invalid_plan")
        if "question_id" in item:
            identifier = _question_id(item["question_id"])
            title = None
        else:
            title = item["new_question"]
            if not isinstance(title, str) or not title.strip() or len(title) > 500 or any(ord(c) < 32 for c in title):
                raise RequestError("invalid_plan")
            title = title.strip()
            identifier = "q-" + digest(_normalize(title).encode("utf-8"))[:32]
        if number not in topics:
            topics[number] = read_topic(vault, sha, number)
        topic = topics[number]
        link = dict(sha256=sha, topic_id=number, relation=relation, reason=reason.strip(),
                    topic_revision=topic["revision"], topic_title=topic["title"],
                    article_title=topic["article_title"], path=topic["path"],
                    article_published_at=topic["article_published_at"])
        results.append((identifier, title, link))
    return results


def _alias(text):
    return html.escape(text.replace("[", "［").replace("]", "］").replace("|", "｜").replace("\n", " "))


def _render_question(record):
    lines = [f"# {plain(record['title'])}", "",
             f"第 {record['revision']} 版 · 更新于 {record['updated_at']}",
             "", f"[[{INDEX}|研究问题索引]]", "", "## 材料观点"]
    for link in record["links"]:
        published = link["article_published_at"] or "未知"
        title = _alias(link["article_title"] + " · " + link["topic_title"])
        lines.extend([
            "",
            f"- {RELATIONS[link['relation']]} · [[{link['path']}|{title}]]",
            f"  - 发表时间 {published} · 归入问题 {link['linked_at']}",
            "  - " + plain(link["reason"]).replace("\n", "  \n    "),
        ])
    if record["note"]:
        lines.extend(["", "## 保存的讨论", "", record["note"].rstrip("\n")])
    discussions = record.get("discussions", [])
    if discussions:
        lines.extend(["", "## 通过 Codex 保存的讨论"])
    for discussion in discussions:
        lines.extend([
            "", f"### {discussion['id']} · 第 {discussion['revision']} 版",
            "", f"更新于 {discussion['updated_at']}",
            "", discussion["body"].rstrip("\n"),
        ])
    return "\n".join(lines) + "\n"


def _render_index(db):
    lines = ["# 研究问题索引", ""]
    for record in sorted(_all(db), key=lambda item: (item["title"], item["id"])):
        lines.append(f"- [[{record['path']}|{_alias(record['title'])}]] · 第 {record['revision']} 版")
    return "\n".join(lines) + "\n"


def _projection_contents(path, target, accepted, *, allow_missing):
    try:
        original = source_bytes(path)
    except FileNotFoundError:
        if not allow_missing:
            raise RequestError("user_edit_conflict")
        return None, None, (target + MANUAL).encode("utf-8")
    try:
        text = original.decode("utf-8")
    except UnicodeError as exc:
        raise RequestError("user_edit_conflict") from exc
    start = text.find(BEGIN)
    end = text.find(END, start + len(BEGIN))
    stop = end + len(END)
    previous = text[start:stop]
    if (start == -1 or end == -1 or text.count(BEGIN) != 1 or text.count(END) != 1
            or previous not in accepted):
        raise RequestError("user_edit_conflict")
    return original, previous, (text[:start] + target + text[stop:]).encode("utf-8")


def _projection(vault, db, relative, body):
    path = inside(vault, relative)
    row = db.execute("SELECT current,previous FROM projections WHERE path=?", (relative,)).fetchone()
    target = BEGIN + body + END
    original, previous, result = _projection_contents(path, target, row or (), allow_missing=row is None)
    db.execute("""
        INSERT INTO projections(path, current, previous) VALUES(?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET current=excluded.current, previous=excluded.previous
    """, (relative, target, previous))
    if original != result:
        db.execute("INSERT INTO pending_projections(path) VALUES (?)", (relative,))


def _upgrade_projection_queue(vault, db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='pending_projections'").fetchone():
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE pending_projections (path TEXT PRIMARY KEY)")
        index = db.execute("SELECT current,previous FROM projections WHERE path=?", (INDEX,)).fetchone()
        if (not index or index[1] is None or index[0] == index[1]
                or index[0] != BEGIN + _render_index(db) + END):
            return
        current, previous = index
        try:
            _projection_contents(inside(vault, INDEX), current, (previous,), allow_missing=False)
        except RequestError as exc:
            if str(exc) != "user_edit_conflict":
                raise
            return

        # 旧索引仍为上一版时，只补本轮新进入索引且已保存首版的问题。
        # 已完成索引中后来缺失的文件无法判断删除意图，保留现场。
        pending = [(INDEX,)]
        for record in _all(db):
            relative = record["path"]
            link = f"[[{relative}|"
            expected = BEGIN + _render_question(record) + END
            saved = db.execute("SELECT current,previous FROM projections WHERE path=?", (relative,)).fetchone()
            if not saved or saved[0] != expected:
                return
            may_recover_new_file = saved[1] is None and record["revision"] == 1 and link not in previous
            try:
                original, _, result = _projection_contents(
                    inside(vault, relative), expected, saved, allow_missing=may_recover_new_file,
                )
            except RequestError as exc:
                if str(exc) != "user_edit_conflict":
                    raise
                return
            if original != result:
                pending.append((relative,))
        db.executemany("INSERT INTO pending_projections(path) VALUES (?)", pending)


def _replay_projections(vault, db):
    # 问题页先落盘，索引随后更新。每次重放都重新读取手写区。
    pending = db.execute("""
        SELECT path,current,previous FROM pending_projections JOIN projections USING(path)
        ORDER BY path = ?, path
    """, (INDEX,)).fetchall()
    for relative, target, previous in pending:
        path = inside(vault, relative)
        original, _, result = _projection_contents(
            path, target, (target, previous), allow_missing=previous is None,
        )
        _write_projection(path, original, result)
        db.execute("DELETE FROM pending_projections WHERE path=?", (relative,))
        db.commit()


def _write_projection(path, original, target):
    if original == target:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(target)
            stream.flush()
            os.fsync(stream.fileno())
            if original is None:
                try:
                    os.link(staged, path)
                except FileExistsError as exc:
                    raise RequestError("user_edit_conflict") from exc
            else:
                try:
                    if source_bytes(path) != original:
                        raise RequestError("user_edit_conflict")
                except FileNotFoundError as exc:
                    raise RequestError("user_edit_conflict") from exc
                os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


def _store(db, record):
    db.execute("INSERT INTO revisions VALUES(?, ?, ?)",
               (record["id"], record["revision"], _json(record)))


def _finish(vault, db, records):
    for record in records:
        _projection(vault, db, record["path"], _render_question(record))
    _projection(vault, db, INDEX, _render_index(db))
    # SQLite 原子保留版本和待写投影，后续写入可恢复中断。
    db.commit()
    _replay_projections(vault, db)


def _archive_batch(source_shas):
    if not isinstance(source_shas, (list, tuple, set, frozenset)) or not source_shas or not all(
        isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) for sha in source_shas
    ):
        raise RequestError("invalid_request")
    sources = set(source_shas)
    return sources, digest(_json(sorted(sources)).encode())


def _unedited(vault, db, record):
    if record.get("note") or record.get("discussions"):
        return False
    relative = record["path"]
    row = db.execute("SELECT current FROM projections WHERE path=?", (relative,)).fetchone()
    expected = BEGIN + _render_question(record) + END
    if row != (expected,):
        return False
    try:
        return source_bytes(inside(vault, relative)) == (expected + MANUAL).encode()
    except FileNotFoundError:
        return False


def archive_unedited_batch(vault, source_shas):
    """移出主动导航，保留正式记录、原笔记以及版本链接。"""
    sources, batch = _archive_batch(source_shas)
    with _writer(vault) as db:
        archived = dict(db.execute("SELECT id,batch FROM archived_questions"))
        records = _all(db, include_archived=True)
        selected = [record for record in records if archived.get(record["id"]) == batch]
        skipped = 0
        for record in records:
            if record["id"] in archived or not record["links"] or any(
                link["sha256"] not in sources for link in record["links"]
            ):
                continue
            if not _unedited(vault, db, record):
                skipped += 1
                continue
            selected.append(record)
            db.execute("INSERT INTO archived_questions(id,batch) VALUES (?,?)", (record["id"], batch))

        archive_path = None
        if selected:
            # 先核对正式索引中受保护的生成区，再创建可追踪的独占归档页。
            _projection(vault, db, INDEX, _render_index(db))
            fingerprint = digest(_json(sorted(record["id"] for record in selected)).encode())
            archive_path = f"{ARCHIVE}/批次-{fingerprint}.md"
            archive = inside(vault, archive_path)
            if not archive.exists():
                lines = ["# 自动草稿存档", "", "原研究问题及历史版本可继续打开。", ""]
                for record in sorted(selected, key=lambda item: (item["title"], item["id"])):
                    lines.append(f"- [[{record['path']}|{_alias(record['title'])}]]")
                _write_projection(archive, None, ("\n".join(lines) + "\n").encode())
            db.commit()
            _replay_projections(vault, db)
        return {"archived": len(selected), "skipped": skipped, "archive_path": archive_path}


def restore_archived_batch(vault, source_shas):
    """把指定批次恢复到正式导航，保留归档清单。"""
    _, batch = _archive_batch(source_shas)
    with _writer(vault) as db:
        restored = db.execute("DELETE FROM archived_questions WHERE batch=?", (batch,)).rowcount
        if restored or inside(vault, INDEX).exists():
            _finish(vault, db, [])
        return {"restored": restored}


def apply(vault, sha, assignments, confirm_new=False, *, expected_revisions=None):
    if type(confirm_new) is not bool:
        raise RequestError("invalid_plan")
    if expected_revisions is not None:
        if not isinstance(expected_revisions, dict) or any(not _positive(value) for value in expected_revisions.values()):
            raise RequestError("invalid_plan")
        for identifier in expected_revisions:
            _question_id(identifier)
    operations = _assignments(vault, sha, assignments)
    if not operations:
        return {"questions": [], "pending": []}
    pending = []
    changed = set()
    records = {}
    prior = {}
    now = _now()
    with _writer(vault) as db:
        for identifier, title, link in operations:
            record = records.get(identifier)
            if record is None:
                row = db.execute("SELECT payload FROM revisions WHERE id=? ORDER BY revision DESC LIMIT 1",
                                 (identifier,)).fetchone()
                if row:
                    record = json.loads(row[0])
                    prior[identifier] = record["revision"]
                elif title is None:
                    raise RequestError("not_found")
                elif not confirm_new:
                    suggestion = {**link, "new_question": title}
                    if suggestion not in pending:
                        pending.append(suggestion)
                    continue
                else:
                    record = dict(id=identifier, title=title, path=f"{ROOT}/{identifier}.md", revision=0,
                                  created_at=now, updated_at=now, note="", links=[])
                records[identifier] = record
            existing = next((item for item in record["links"]
                             if (item["sha256"], item["topic_id"]) == (sha, link["topic_id"])), None)
            if existing is not None:
                if any(existing[key] != value for key, value in link.items()):
                    raise RequestError("assignment_conflict")
                continue
            if title is not None and identifier in prior:
                raise RequestError("existing_question_required")
            if (title is None and expected_revisions is not None
                    and expected_revisions.get(identifier) != prior.get(identifier)):
                raise RequestError("revision_changed")
            record["links"].append({**link, "linked_at": now})
            changed.add(identifier)
        for identifier, record in records.items():
            if identifier not in changed:
                continue
            record["revision"] += 1
            record["updated_at"] = now
            _store(db, record)
        if records:
            _finish(vault, db, records.values())
    return {"questions": [_view(record) for record in records.values()], "pending": pending}


def adopt_candidate(vault, batch, position, title, sources):
    operations = [
        _assignments(vault, item["sha256"], [{**{key: item[key] for key in ("topic_id", "relation", "reason")},
                                             "new_question": title}])[0]
        for item in sources
    ]
    identifier, title, _ = operations[0]
    links = [link for _, _, link in operations]
    if len({link["sha256"] for link in links}) < 2:
        raise RequestError("invalid_review_receipt")
    origin = {"batch": batch, "position": position}
    with _writer(vault) as db:
        for existing in _all(db, include_archived=True):
            if _normalize(existing["title"]) != _normalize(title):
                continue
            first = _record(db, existing["id"], 1)
            if (first.get("candidate") != origin
                    or [{key: item[key] for key in links[0]} for item in first["links"]] != links):
                raise RequestError("existing_question_required")
            return {"question": _view(existing), "adopted": False}
        now = _now()
        record = dict(id=identifier, title=title, path=f"{ROOT}/{identifier}.md", revision=1,
                      created_at=now, updated_at=now, note="", candidate=origin,
                      links=[{**link, "linked_at": now} for link in links])
        _store(db, record)
        _finish(vault, db, [record])
    return {"question": _view(record), "adopted": True}


def update_note(vault, id, expected_revision, body):
    identifier = _question_id(id)
    if (not _positive(expected_revision) or not isinstance(body, str)
            or BEGIN.strip() in body or END.strip() in body):
        raise RequestError("invalid_request")
    with _writer(vault) as db:
        record = _record(db, identifier)
        if record["revision"] != expected_revision:
            if (record["revision"] != expected_revision + 1 or record["note"] != body
                    or _record(db, identifier, expected_revision)["links"] != record["links"]):
                raise RequestError("revision_changed")
        elif record["note"] != body:
            record.update(note=body, revision=expected_revision + 1, updated_at=_now())
            _store(db, record)
        _finish(vault, db, [record])
    return read(vault, identifier, record["revision"])


def save_discussion(vault, id, expected_revision, body, *, discussion_id=None):
    """保存独立条目，修订前后的内容随研究问题版本留存。"""
    identifier = _question_id(id)
    if (not _positive(expected_revision) or not isinstance(body, str) or not body.strip()
            or BEGIN.strip() in body or END.strip() in body):
        raise RequestError("invalid_request")
    if discussion_id is not None and (
            not isinstance(discussion_id, str) or not re.fullmatch(r"d-[0-9a-f]{32}", discussion_id)):
        raise RequestError("invalid_request")

    with _writer(vault) as db:
        current = _record(db, identifier)
        if current["revision"] not in (expected_revision, expected_revision + 1):
            raise RequestError("revision_changed")
        previous = current if current["revision"] == expected_revision else _record(db, identifier, expected_revision)
        discussions = list(previous.get("discussions", []))
        now = _now()
        if discussion_id is None:
            discussion_id = "d-" + digest(_json([identifier, expected_revision]).encode("utf-8"))[:32]
            item = {"id": discussion_id, "revision": 1, "created_at": now}
            position = len(discussions)
        else:
            position = next((i for i, entry in enumerate(discussions) if entry["id"] == discussion_id), None)
            if position is None:
                raise RequestError("not_found")
            entry = discussions[position]
            item = {**entry, "revision": entry["revision"] + 1}

        sources = [{key: link[key] for key in ("sha256", "topic_id", "topic_revision")} for link in previous["links"]]
        item.update(body=body, sources=sources, updated_at=now)
        discussions[position:position + 1] = [item]
        updated = {**previous, "revision": expected_revision + 1, "updated_at": now, "discussions": discussions}
        if current["revision"] == expected_revision:
            _store(db, updated)
            current = updated
        else:
            # 复用已提交的同一修改来恢复写盘，时间字段沿用第一次保存。
            existing = current.get("discussions", [])
            if position >= len(existing):
                raise RequestError("revision_changed")
            item.update(created_at=existing[position]["created_at"], updated_at=existing[position]["updated_at"])
            updated["updated_at"] = current["updated_at"]
            if current != updated:
                raise RequestError("revision_changed")
        _finish(vault, db, [current])
    return {"id": identifier, "revision": current["revision"], "discussion_id": discussion_id,
            "discussion_revision": item["revision"]}
