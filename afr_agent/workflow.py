"""AFR-owned file intake, article processing, and batch knowledge updates."""
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import contextlib
import fcntl
import json
import os
import re
import sqlite3
import stat
import tempfile
from threading import Event
import time

from . import locations, semantic
from .intake import (SUPPORTED, InboxChanged, check_inbox_identity, check_intake_roots, digest,
                     exclusive_json, inbox_files, inbox_identity, inside, library_originals,
                     private_json, scan, source_bytes)
from .library import RequestError

RETRY_LIMIT = 3
KNOWLEDGE_BATCH_SIZE = 10
KNOWLEDGE_PEER_LIMIT = 4
ARTICLE_WORKERS = 2
KNOWLEDGE_MAX_WAIT_SECONDS = 120
ARTICLE_NEEDS_RETRY = frozenset({"review_requires_human"})


def database(vault):
    state = inside(vault, ".afr")
    state.mkdir(exist_ok=True)
    db = sqlite3.connect(inside(vault, ".afr/workflow.sqlite"))
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS workflow (
        sha256 TEXT PRIMARY KEY, folder TEXT NOT NULL, admitted INTEGER NOT NULL,
        phase TEXT NOT NULL DEFAULT 'admitted', error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
        plan TEXT, deferred TEXT, question_count INTEGER
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS cross_article_review (
        scope TEXT PRIMARY KEY, state TEXT NOT NULL, error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
        result TEXT
    )""")
    db.execute("CREATE TABLE IF NOT EXISTS domain_navigation (sha256 TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS domain_navigation_verified (sha256 TEXT PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS automatic_admission (
        sha256 TEXT NOT NULL, inbox_path TEXT NOT NULL, inbox_inode INTEGER NOT NULL,
        PRIMARY KEY (sha256, inbox_path, inbox_inode)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS knowledge_batches (
        scope TEXT PRIMARY KEY, state TEXT NOT NULL, error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
        result TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS knowledge_sources (
        sha256 TEXT PRIMARY KEY, scope TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS knowledge_article_terms (
        sha256 TEXT PRIMARY KEY, revision TEXT NOT NULL, focus TEXT NOT NULL, context TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_events (
        id INTEGER PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL,
        error TEXT, retry_at INTEGER
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS workflow_events_scope ON workflow_events(kind,scope,id)")
    return db


def status(vault):
    with contextlib.closing(database(vault)) as db:
        articles = [
            dict(row) for row in db.execute(
                "SELECT sha256,folder,admitted,phase,error,attempts,next_at,question_count,deferred "
                "FROM workflow ORDER BY admitted, rowid"
            )
        ]
        columns = "state,error,attempts,result"
        review = db.execute(f"SELECT {columns} FROM cross_article_review ORDER BY rowid DESC LIMIT 1").fetchone()
        knowledge = db.execute(f"SELECT {columns} FROM knowledge_batches ORDER BY rowid DESC LIMIT 1").fetchone()
    mapping = locations.load(vault)
    for row in articles:
        row["folder"] = locations.resolve(row["folder"], mapping)
        row["deferred"] = json.loads(row["deferred"]) if row["deferred"] is not None else []

    def details(row):
        if row is None:
            return None
        value = dict(row)
        value["result"] = json.loads(value["result"]) if value["result"] else None
        return value

    return {"articles": articles, "cross_article_review": details(review), "knowledge_summary": details(knowledge)}


def process_article(vault, identifier, approvals, *, preserve_original):
    result = semantic.process(vault, identifier, approvals, preserve_original=preserve_original)
    if result["state"] == "existing_publication":
        raise RequestError("legacy_publication_requires_review")
    count = result.get("topics")
    if count is None:
        count = len(semantic.reviewed_plan(vault, identifier)["groups"])
    return count


def _event(db, scope, kind, error=None, retry_at=None):
    db.execute("INSERT INTO workflow_events(scope,kind,at,error,retry_at) VALUES (?,?,?,?,?)",
               (scope, kind, time.time(), error, retry_at))


def record_failure(db, row, exc):
    error = str(exc) if isinstance(exc, RequestError) else "local_io_failed"
    attempts = 0 if error == "local_only" else row["attempts"] + 1
    next_at = 0 if error in ARTICLE_NEEDS_RETRY else int(time.time()) + 30 * attempts
    with db:
        if error in {"revision_changed", "existing_question_required"}:
            db.execute("UPDATE workflow SET plan=NULL WHERE sha256=?", (row["sha256"],))
        db.execute("UPDATE workflow SET error=?,attempts=?,next_at=? WHERE sha256=?",
                   (error, attempts, next_at, row["sha256"]))
        _event(db, row["sha256"], "article_failed", error, next_at)


def _signed_knowledge_terms(vault, db, versions, folders):
    from .knowledge import _words

    records = {}
    missing = []
    for sha in versions:
        review = semantic.receipt(vault, sha, "review")
        publication = semantic.receipt(vault, sha, "publication")
        try:
            files = (review.stat(), publication.stat())
            revision = ":".join(f"{item.st_ino}-{item.st_size}-{item.st_mtime_ns}" for item in files)
            previous = db.execute("SELECT * FROM knowledge_article_terms WHERE sha256=?", (sha,)).fetchone()
            if previous and previous["revision"] == revision:
                records[sha] = (set(json.loads(previous["focus"])), set(json.loads(previous["context"])))
                continue
            signed = semantic.read_json(review)
            published = semantic.read_json(publication)
            plan = signed.get("plan", {})
            if (plan.get("sha256") != sha or signed.get("version") != semantic.REVIEW_VERSION
                    or published.get("sha256") != sha or published.get("state") != "published"
                    or signed.get("plan_sha256") != published.get("plan_sha256")
                    or signed.get("plan_sha256") != semantic.fingerprint(plan)):
                continue
            title = re.sub(r"(?<![a-f0-9])[a-f0-9]{8,64}(?![a-f0-9])", "",
                           folders.get(sha, "").rsplit("/", 1)[-1])
            groups = plan["groups"]
            focus = _words(" ".join([title, *(group["title"] + " " + group["branch"] for group in groups)]))
            context = _words(" ".join(group["summary"][:200] for group in groups))
        except (OSError, RequestError, KeyError, TypeError, AttributeError, ValueError):
            continue
        records[sha] = (focus, context)
        missing.append((sha, revision, json.dumps(sorted(focus), ensure_ascii=False),
                        json.dumps(sorted(context), ensure_ascii=False)))
    if missing:
        with db:
            db.executemany("""INSERT INTO knowledge_article_terms VALUES (?,?,?,?)
                ON CONFLICT(sha256) DO UPDATE SET revision=excluded.revision,
                focus=excluded.focus,context=excluded.context""", missing)
    return records


def _related_knowledge_sources(vault, db, new, candidates, folders, limit):
    from .knowledge import STATE, _words

    if not candidates or not limit:
        return []
    incoming = _signed_knowledge_terms(vault, db, new, folders)
    if not incoming:
        return []
    terms = _signed_knowledge_terms(vault, db, candidates, folders)
    labels = defaultdict(list)
    try:
        ledger = semantic.read_json(inside(vault, f"{STATE}/ledger.json"))
    except (OSError, RequestError):
        ledger = {}
    eligible = set(candidates) & terms.keys()
    for topic in ledger.get("topics", {}).values():
        subjects = _words(topic["object"])
        dimensions = _words(topic["dimension"])
        for event in topic["events"]:
            if event["sha256"] in eligible:
                labels[event["sha256"]].append((subjects, dimensions))
    frequency = Counter(term for sha in eligible for term in terms[sha][0])
    ranked = []
    for sha in candidates:
        if sha not in eligible:
            continue
        focus, context = terms[sha]
        best = 0.0
        for title, summary in incoming.values():
            shared = (focus & title) | (focus & summary) | (context & title)
            focused = focus & title
            # 必须存在具体标题/主线的交集或对象与维度同时吻合。通用摘要词不能独自匹配。
            direct = (sum(1 / frequency.get(term, 1) for term in shared)
                      if len(shared) >= 2 and focused else 0)
            known = max((2 + len(title & subjects) + len((title | summary) & dimensions)
                         for subjects, dimensions in labels[sha]
                         if subjects and len(title & subjects) >= (len(subjects) + 1) // 2
                         and dimensions and len((title | summary) & dimensions) >= (len(dimensions) + 1) // 2),
                        default=0)
            best = max(best, direct, known)
        if best:
            ranked.append((best, sha))
    return [sha for _, sha in sorted(ranked, key=lambda match: (-match[0], match[1]))[:limit]]


def _knowledge_batch(vault, db, versions, folders, *, allow_new=True):
    authorized = set(versions)
    now = time.time()
    ready = db.execute("SELECT scope FROM knowledge_batches WHERE state IN ('ready','no_common_topic')").fetchall()
    ready_scopes = {scope for (scope,) in ready}
    pending = db.execute("""SELECT * FROM knowledge_batches
        WHERE state IN ('working','pending') ORDER BY rowid""").fetchall()
    for row in pending:
        manifest = json.loads(row["result"])
        if not set(manifest["versions"]).issubset(authorized):
            with db:
                db.execute("UPDATE knowledge_batches SET state='superseded',error='local_only' WHERE scope=?",
                           (row["scope"],))
                db.execute("DELETE FROM knowledge_sources WHERE scope=?", (row["scope"],))
        elif row["attempts"] < RETRY_LIMIT and row["next_at"] <= now:
            return row, manifest
    if not allow_new:
        return None, None

    observed = dict(db.execute("SELECT sha256,scope FROM knowledge_sources"))
    completed = [sha for sha in versions if observed.get(sha) in ready_scopes]
    # 第一批可一并比较十篇。续批为允许使用的既有出处保留位置。
    new = [sha for sha in versions if sha not in observed]
    if not new:
        return None, None
    comparison_count = min(len(new), KNOWLEDGE_BATCH_SIZE - 2)
    peer_limit = min(KNOWLEDGE_PEER_LIMIT, KNOWLEDGE_BATCH_SIZE - comparison_count)
    peers = _related_knowledge_sources(vault, db, new[:comparison_count], completed, folders, peer_limit)
    new = new[:KNOWLEDGE_BATCH_SIZE - len(peers)] if not peers else new[:comparison_count]
    manifest = {"versions": sorted(new + peers), "new_versions": sorted(new)}
    scope = semantic.fingerprint(manifest)
    with db:
        db.execute("""INSERT INTO knowledge_batches (scope,state,result) VALUES (?,'working',?)
                   ON CONFLICT(scope) DO UPDATE SET state='working',attempts=0,next_at=0,
                   error=NULL,result=excluded.result""", (scope, json.dumps(manifest)))
        db.executemany("INSERT INTO knowledge_sources VALUES (?,?)", ((sha, scope) for sha in new))
    return db.execute("SELECT * FROM knowledge_batches WHERE scope=?", (scope,)).fetchone(), manifest


def supplement_knowledge(vault, db, approvals):
    from . import knowledge

    rows = db.execute("""SELECT w.sha256,w.folder,w.phase,w.error,w.attempts, s.scope,
        COALESCE((SELECT e.at FROM workflow_events e WHERE e.kind='article_signed' AND e.scope=w.sha256
                  ORDER BY e.id DESC LIMIT 1), w.admitted) AS signed_at
        FROM workflow w LEFT JOIN knowledge_sources s USING(sha256) ORDER BY w.admitted,w.rowid""").fetchall()
    allowed = semantic.approved_versions(approvals)
    approved = [row for row in rows if row["sha256"] in allowed]
    # 同批正常签发时合并总结。已有可比证据等候满窗口后，可在长文继续处理时先更新。
    pending_articles = any(row["phase"] != "ready" and row["error"] not in ARTICLE_NEEDS_RETRY
                           and row["attempts"] < RETRY_LIMIT for row in approved)
    cutoff = time.time() - KNOWLEDGE_MAX_WAIT_SECONDS
    overdue = any(row["phase"] == "ready" and row["scope"] is None and row["signed_at"] <= cutoff
                  for row in approved)
    versions = [row["sha256"] for row in approved if row["phase"] == "ready"]
    state, manifest = _knowledge_batch(vault, db, versions, {row["sha256"]: row["folder"] for row in approved},
                                       allow_new=not pending_articles or overdue)
    if state is None:
        return
    scope = state["scope"]
    with db:
        _event(db, scope, "knowledge_started")
    try:
        result = knowledge.run(vault, manifest["versions"], approvals, new_version_ids=manifest["new_versions"])
    except (RequestError, OSError, sqlite3.Error) as exc:
        error = str(exc) if isinstance(exc, RequestError) else "local_io_failed"
        attempts = state["attempts"] + 1
        next_at = int(time.time()) + 30 * attempts
        with db:
            db.execute("UPDATE knowledge_batches SET state='pending',error=?,attempts=?,next_at=? WHERE scope=?",
                       (error, attempts, next_at, scope))
            _event(db, scope, "knowledge_failed", error, next_at)
        return
    phase = "ready"
    if result.get("topics") == 0:
        if result.get("deferred_topics"):
            phase = "needs_retry"
        else:
            phase = "no_common_topic"
    with db:
        db.execute("UPDATE knowledge_batches SET state=?,error=NULL,next_at=0,result=? WHERE scope=?",
                   (phase, json.dumps({**manifest, **result}, ensure_ascii=False), scope))
        _event(db, scope, "knowledge_finished")


def seed_baseline(vault, inbox, *, legacy_roots=(), legacy_db=None):
    from . import domain_index

    check_intake_roots(vault, inbox)
    with watcher_lock(vault):
        path = inside(vault, ".afr/automatic-baseline.json")
        if path.exists():
            raise RequestError("baseline_exists")
        identity = inbox_identity(inbox)
        historical = {digest(source_bytes(file)) for file in library_originals(vault)}
        for root in (inbox, *legacy_roots):
            if not root.is_dir():
                raise RequestError("invalid_path")
            historical.update(digest(source_bytes(file)) for file in inbox_files(root, strict=True)
                              if file.suffix.lower() in SUPPORTED)
        for filename, queries in (
            ("intake.sqlite", {"articles": "SELECT sha FROM articles",
                               "seen": "SELECT sha FROM seen"}),
            ("workflow.sqlite", {"workflow": "SELECT sha256 FROM workflow"}),
        ):
            db_path = inside(vault, f".afr/{filename}")
            if db_path.is_file():
                with contextlib.closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table, query in queries.items():
                        if table in tables:
                            historical.update(sha for (sha,) in db.execute(query))
        if legacy_db is not None:
            with contextlib.closing(sqlite3.connect(legacy_db.as_uri() + "?mode=ro", uri=True)) as db:
                historical.update(sha for (sha,) in db.execute("""
                    SELECT json_extract(a.value, '$.object_sha256')
                    FROM evidence_snapshots s, json_each(s.artifact_manifest_json) a
                """))
        if any(not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha) for sha in historical):
            raise RequestError("invalid_baseline")
        if inbox_identity(inbox) != identity:
            raise RequestError("inbox_changed")
        if (all(entry.name in {".afr", ".obsidian", ".DS_Store"} for entry in vault.iterdir())
                and all(entry.name == "workflow.lock" for entry in path.parent.iterdir())
                and not legacy_roots and legacy_db is None):
            domain_index.initialize(vault)
        exclusive_json(path, {"version": 1, "inbox": identity, "sha256": sorted(historical)})
    return {"historical_versions": len(historical)}


class BaselineError(RequestError):
    """自动授权基线不可用，本次只收录本地原文。"""


def read_baseline(path):
    try:
        state = private_json(path, 100 * 1024 * 1024)
    except FileNotFoundError as exc:
        raise BaselineError("baseline_required") from exc
    except (OSError, ValueError) as exc:
        raise BaselineError("invalid_baseline") from exc
    versions = semantic.approved_hashes(state, "sha256") if isinstance(state, dict) else frozenset()
    binding = state.get("inbox") if isinstance(state, dict) else None
    if (not isinstance(binding, list) or len(binding) != 3 or not isinstance(binding[0], str)
            or any(type(value) is not int or value < 0 for value in binding[1:])
            or state.get("version") != 1 or not isinstance(state.get("sha256"), list)
            or len(versions) != len(state["sha256"])):
        raise BaselineError("invalid_baseline")
    return state, versions


def load_baseline(vault, inbox, approvals):
    check_intake_roots(vault, inbox)
    if approvals is None:
        raise RequestError("approval_path_required")
    for root in (vault, inbox):
        if approvals.resolve().is_relative_to(root.resolve()) or approvals.absolute().is_relative_to(root.absolute()):
            raise RequestError("invalid_approval_path")
    state, versions = read_baseline(inside(vault, ".afr/automatic-baseline.json"))
    if state["inbox"] != inbox_identity(inbox):
        raise BaselineError("invalid_baseline")
    return state["inbox"], versions


def rebind_baseline(vault, inbox, *, old_device, new_device):
    check_intake_roots(vault, inbox)
    if min(old_device, new_device) < 0 or old_device == new_device:
        raise RequestError("invalid_request")
    with watcher_lock(vault):
        path = inside(vault, ".afr/automatic-baseline.json")
        try:
            original = path.lstat()
        except FileNotFoundError as exc:
            raise BaselineError("baseline_required") from exc
        state, versions = read_baseline(path)
        canonical, current_device, inode = inbox_identity(inbox)
        if current_device != new_device:
            raise RequestError("inbox_changed")
        result = {"historical_versions": len(versions), "rebound": False}
        if state["inbox"] == [canonical, new_device, inode]:
            return result
        if state["inbox"] != [canonical, old_device, inode]:
            raise BaselineError("invalid_baseline")

        expected = [canonical, new_device, inode]
        state["inbox"][1] = new_device
        # 暂存到同一目录，仅替换设备号，保留历史版本与私有文件权限。
        with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as stream:
            temporary = stream.name
            try:
                fd = stream.fileno()
                if os.fstat(fd).st_gid != original.st_gid:
                    os.fchown(fd, -1, original.st_gid)
                os.fchmod(fd, stat.S_IMODE(original.st_mode))
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(fd)
                if inbox_identity(inbox) != expected or path.lstat() != original:
                    raise RequestError("inbox_changed")
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {**result, "rebound": True}


def _index_ready_articles(vault, db, errors):
    from .domain_index import ensure_entry

    rows = db.execute("""SELECT sha256,folder FROM workflow WHERE phase='ready' AND NOT EXISTS
        (SELECT 1 FROM domain_navigation_verified WHERE sha256=workflow.sha256)""").fetchall()
    for sha, folder in rows:
        try:
            indexed = ensure_entry(vault, folder, sha)
        except (RequestError, OSError) as exc:
            errors.append({"file": folder, "error": str(exc) if isinstance(exc, RequestError) else type(exc).__name__})
            continue
        if indexed is not None:
            with db:
                # 旧监听器仍可读取原来的单列标记。
                db.execute("INSERT OR IGNORE INTO domain_navigation VALUES (?)", (sha,))
                db.execute("INSERT OR IGNORE INTO domain_navigation_verified VALUES (?)", (sha,))
                _event(db, sha, "article_indexed")


def _knowledge_worker(vault, approvals):
    with contextlib.closing(database(vault)) as db:
        supplement_knowledge(vault, db, approvals)


class WatchJobs:
    """监听器跨扫描保留正在运行的任务，所有篇内状态仍由监听主线程写入。"""

    def __init__(self):
        self._article_pool = ThreadPoolExecutor(max_workers=ARTICLE_WORKERS)
        self._knowledge_pool = ThreadPoolExecutor(max_workers=1)
        self.articles = {}
        self._knowledge = None
        self._next_retry = None
        self.stopping = Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stopping.set()
        self._article_pool.shutdown(cancel_futures=True)
        self._knowledge_pool.shutdown(cancel_futures=True)

    def complete_articles(self, db):
        for future, row in list(self.articles.items()):
            if not future.done():
                continue
            del self.articles[future]
            try:
                count = future.result()
            except (RequestError, OSError, sqlite3.Error) as exc:
                record_failure(db, row, exc)
                continue
            with db:
                db.execute("""UPDATE workflow SET phase='ready',error=NULL,next_at=0,deferred=?,question_count=0
                    WHERE sha256=?""", (json.dumps(list(range(1, count + 1))), row["sha256"]))
                _event(db, row["sha256"], "article_signed")

    def schedule_articles(self, vault, db, approvals, preserve_original, attempted=None):
        running = {row["sha256"] for row in self.articles.values()}
        allowed = semantic.approved_versions(approvals)
        now = time.time()
        self._next_retry = None
        rows = db.execute("SELECT * FROM workflow WHERE phase!='ready' ORDER BY admitted,rowid").fetchall()
        for row in rows:
            sha, error = row["sha256"], row["error"]
            if sha in running or (attempted is not None and sha in attempted) or error in ARTICLE_NEEDS_RETRY:
                continue
            if sha not in allowed:
                if error != "local_only":
                    with db:
                        db.execute("UPDATE workflow SET error='local_only' WHERE sha256=?", (sha,))
                continue
            if error != "local_only" and row["attempts"] >= RETRY_LIMIT:
                continue
            if len(self.articles) >= ARTICLE_WORKERS:
                continue
            if error != "local_only" and row["next_at"] > now:
                if self._next_retry is None or row["next_at"] < self._next_retry:
                    self._next_retry = row["next_at"]
                continue
            with db:
                _event(db, sha, "article_started")
            if attempted is not None:
                attempted.add(sha)
            future = self._article_pool.submit(process_article, vault, sha, approvals, preserve_original=preserve_original)
            self.articles[future] = row

    def schedule_knowledge(self, vault, approvals):
        if self._knowledge is None or self._knowledge.done():
            if self._knowledge is not None:
                self._knowledge.result()
            self._knowledge = self._knowledge_pool.submit(_knowledge_worker, vault, approvals)

    def wait(self, seconds):
        futures = list(self.articles)
        if futures or self._next_retry is not None:
            seconds = min(seconds, 3)
        if len(self.articles) < ARTICLE_WORKERS and self._next_retry is not None:
            seconds = min(seconds, max(0, self._next_retry - time.time()))
        if futures:
            wait(futures, timeout=seconds, return_when=FIRST_COMPLETED)
        else:
            time.sleep(seconds)


def cycle(vault, inbox, approvals=None, *, settle_seconds=2, preserve_original=False, new_articles=False,
          legacy_db=None, rename_generic=False, jobs=None):
    binding = None
    historical = frozenset()
    warning = None
    if new_articles:
        try:
            binding, historical = load_baseline(vault, inbox, approvals)
        except BaselineError as exc:
            warning = {"code": str(exc), "mode": "local_intake_only"}
    try:
        scan_options = dict(legacy=legacy_db, settle_seconds=settle_seconds,
                            rename_generic=rename_generic and warning is None, expected_inbox=binding)
        collected = scan(vault, inbox, **scan_options)
        if binding is not None and collected["added"] and collected["settling"]:
            # 同一批在写稳窗口两侧到达时，先补扫一次，避免等长文校读完才收下剩余材料。
            time.sleep(min(settle_seconds, 2))
            next_scan = scan(vault, inbox, **scan_options)
            next_scan["added"] = collected["added"] + next_scan["added"]
            next_scan["errors"] = collected["errors"] + next_scan["errors"]
            next_scan["renamed"] += collected["renamed"]
            next_scan["dates_recorded"] = next_scan.get("dates_recorded", 0) + collected.get("dates_recorded", 0)
            collected = next_scan
        check_inbox_identity(inbox, binding)
    except InboxChanged:
        return {"collected": 0, "settling": 0, "renamed": 0, "errors": [], "unsupported": [],
                **status(vault), "warning": {"code": "inbox_changed", "mode": "local_intake_only"}}
    result = {
        "collected": len(collected["added"]), "settling": collected["settling"], "renamed": collected["renamed"],
        "errors": collected["errors"], "unsupported": collected["unsupported"],
    }
    if warning is not None:
        # 只冻结本地原文。待识别到获准投放目录后，再把仍在其中的版本纳入处理队列。
        return {**result, **status(vault), "warning": warning}
    if collected.get("dates_recorded"):
        from . import knowledge
        try:
            knowledge.refresh_views(vault, source_dates_changed=True)
        except (RequestError, OSError, sqlite3.Error) as exc:
            result["errors"].append({"file": knowledge.OVERVIEW,
                                     "error": str(exc) if isinstance(exc, RequestError) else "local_io_failed"})
    admitted = {row["sha256"]: row["folder"] for row in collected["versions"]}
    now = int(time.time())
    with contextlib.closing(database(vault)) as db:
        with db:
            db.executemany("INSERT OR IGNORE INTO workflow (sha256,folder,admitted) VALUES (?,?,?)",
                           ((sha, folder, now) for sha, folder in admitted.items()))
            if binding is not None:
                # 设备号可由 rebind 更新。只延续同一路径、同一目录 inode 的入队记录。
                inbox_path, _, inbox_inode = binding
                db.executemany("INSERT OR IGNORE INTO automatic_admission VALUES (?,?,?)",
                               ((sha, inbox_path, inbox_inode) for sha in admitted if sha not in historical))
        if binding is not None:
            admitted_hashes = frozenset(sha for (sha,) in db.execute(
                "SELECT sha256 FROM automatic_admission WHERE inbox_path=? AND inbox_inode=?",
                (inbox_path, inbox_inode)))
            approvals = semantic.WatchApprovals(approvals, admitted_hashes, historical,
                                                 inside(vault, ".afr/automatic-baseline.json"), tuple(binding),
                                                 jobs.stopping if jobs is not None else None)
        if jobs is None:
            # 单次运行仍同步完成当前可执行的文章，供重放和现有调用方使用。
            with WatchJobs() as current:
                attempted = set()
                _index_ready_articles(vault, db, collected["errors"])
                current.schedule_articles(vault, db, approvals, preserve_original, attempted)
                while current.articles:
                    wait(list(current.articles), return_when=FIRST_COMPLETED)
                    current.complete_articles(db)
                    _index_ready_articles(vault, db, collected["errors"])
                    current.schedule_articles(vault, db, approvals, preserve_original, attempted)
            supplement_knowledge(vault, db, approvals)
        else:
            jobs.complete_articles(db)
            _index_ready_articles(vault, db, collected["errors"])
            jobs.schedule_articles(vault, db, approvals, preserve_original)
            jobs.schedule_knowledge(vault, approvals)
    return {**result, **status(vault)}


@contextlib.contextmanager
def watcher_lock(vault):
    if not vault.is_dir():
        raise RequestError("invalid_vault")
    state = inside(vault, ".afr")
    state.mkdir(exist_ok=True)
    with inside(vault, ".afr/workflow.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RequestError("watcher_running") from exc
        yield


def retry(vault, identifier):
    with watcher_lock(vault), contextlib.closing(database(vault)) as db, db:
        changed = db.execute(
            "UPDATE workflow SET error=NULL,attempts=0,next_at=0 WHERE sha256=? AND phase!='ready'",
            (identifier,),
        ).rowcount
        if not changed:
            changed = db.execute("""UPDATE knowledge_batches SET error=NULL,attempts=0,next_at=0
                WHERE state='pending' AND scope=(
                    SELECT scope FROM knowledge_sources WHERE sha256=?
                )""", (identifier,)).rowcount
        if not changed:
            changed = db.execute("""UPDATE knowledge_batches SET state='pending',error=NULL,attempts=0,next_at=0
                WHERE state IN ('ready','needs_retry') AND json_extract(result,'$.topics')=0
                  AND json_extract(result,'$.deferred_topics')>0 AND scope=(
                    SELECT scope FROM knowledge_sources WHERE sha256=?
                )""", (identifier,)).rowcount
        if not changed:
            empty = db.execute("""SELECT b.scope FROM knowledge_batches AS b
                  JOIN knowledge_sources AS s USING(scope) WHERE s.sha256=?
                  AND b.state IN ('ready','no_common_topic') AND json_extract(b.result,'$.topics')=0
                  AND json_extract(b.result,'$.index_path') IS NULL""", (identifier,)).fetchone()
            if empty is not None:
                scope = empty["scope"]
                db.execute("UPDATE knowledge_batches SET state='superseded' WHERE scope=?", (scope,))
                db.execute("DELETE FROM knowledge_sources WHERE scope=?", (scope,))
                changed = 1
    if not changed:
        raise RequestError("not_found")
    return {"sha256": identifier, "state": "queued"}
