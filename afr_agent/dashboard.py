"""把本机签发与知识发布回执投影到 Obsidian。"""
import contextlib
import fcntl
from datetime import datetime
import json
import re
import time
from zoneinfo import ZoneInfo

from . import knowledge, locations, reading_canvas, semantic
from .intake import digest, inside, plain, source_bytes
from .library import RequestError
from .questions import _write_projection

HOME = "AFR 阅读首页.md"
HOME_STATE = ".afr/reading-home.json"
CHINA = ZoneInfo("Asia/Shanghai")
ERRORS = {
    "codex_timeout": "模型调用超时",
    "codex_authentication": "Codex 登录未通过",
    "codex_failed": "Codex 调用失败",
    "review_requires_human": "校读后仍有内容需要判断",
    "local_io_failed": "读写失败",
}


def _alias(value):
    # Obsidian 的链接别名按原样显示，替换会截断链接的字符。
    return " ".join(value.translate(str.maketrans("[]|", "［］｜")).split())


def _clock(at):
    return datetime.fromtimestamp(at, CHINA).strftime("%m月%d日 %H:%M")


def _duration(start, end):
    if start is None or end is None or end < start:
        return "缺少计时回执"
    seconds = round(end - start)
    minutes, seconds = divmod(seconds, 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分 {seconds} 秒" if minutes else f"{seconds} 秒"


def _phase(row, moments, active, now):
    if row["phase"] == "ready":
        return "白板可读"
    if row["sha256"] in active:
        return "正在整理或校读"
    error = row["error"]
    if error == "local_only":
        return "已收录原文，等待研读许可"
    if error:
        reason = ERRORS.get(error, f"处理遇到问题（{plain(error)}）")
        retry = row["next_at"]
        if retry and row["attempts"] < 3:
            return f"{reason}，原文已保留，" + (f"{_clock(retry)}后重试" if retry > now else "等待重试")
        return f"{reason}，原文已保留，待处理"
    if moments.get("article_started") is not None:
        return "等待续跑"
    return "排队中"


def _changes(vault, result, topics, mapping, protected, topic_paths, as_canvas=False):
    recorded_ids = result.get("event_ids")
    sources = result.get("new_versions", ())
    proofs = result.get("evidence_links", ())
    for identifier in result.get("topic_ids", ()):
        topic = topics.get(identifier)
        if not topic:
            continue
        note, canvas = knowledge._topic_paths(identifier, topic_paths)
        if note in protected:
            continue
        try:
            contents = source_bytes(inside(vault, note)).decode("utf-8")
        except (FileNotFoundError, UnicodeError, RequestError):
            continue
        detail_paths = knowledge._argument_paths(canvas, topic["events"]) if as_canvas else {}
        anchors = list(re.finditer(r"(?m)(?:^|[ \t])\^(e-[0-9a-f]{24})[ \t]*$", contents))
        sections = {anchor.group(1): contents[start:anchor.start()]
                    for start, anchor in zip([0, *(item.end() for item in anchors)], anchors)}
        for event in topic["events"]:
            event_id = event["id"]
            if event_id not in sections:
                continue
            title = plain(knowledge._title(topic))
            if as_canvas:
                detail = detail_paths[event_id]
                if not inside(vault, detail).is_file():
                    continue
                target = f"[[{detail}|{_alias(knowledge._headline(event))}]]"
            else:
                target = f"[[{note}#^{event_id}|{_alias(knowledge._headline(event))}]]"
            is_new = event_id in recorded_ids if recorded_ids is not None else event["sha256"] in sources
            if is_new:
                source = locations.resolve(event["source_path"], mapping)
                yield (f"- **{title}**　{plain(event['kind'])} · {target} · [[{source}|原文]]", 1, 0)
            links = _published_evidence(identifier, event, proofs, sections[event_id], mapping)
            if links:
                yield (f"- **{title}**　补充出处 {len(links)} 条 · {target} · " + " · ".join(links), 0, len(links))


def _published_evidence(identifier, event, proofs, section, mapping):
    receipts = [proof for proof in proofs if proof["topic_id"] == identifier and proof["event_id"] == event["id"]]
    recorded_ids = {proof["evidence_id"] for proof in receipts if "evidence_id" in proof}
    recorded_pointers = {(proof["sha256"], proof["card_id"], proof["evidence_number"], proof["relation"])
                         for proof in receipts if "evidence_id" not in proof}
    links = []
    for proof in event.get("evidence_links", ()):
        pointer = (proof["sha256"], proof["topic_id"], proof["evidence_number"], proof["relation"])
        if proof.get("id") in recorded_ids or pointer in recorded_pointers:
            source = locations.resolve(proof["source_path"], mapping)
            label = "独立佐证" if proof["relation"] == "佐证" else "转载来源"
            link = f"[[{source}|{label}]]"
            if link in section:
                display = f"{label}（{_alias(proof['origin'])}）" if proof.get("origin") else label
                links.append(f"[[{source}|{display}]]")
    return links


def _protected_entries(vault, result, ledger, mapping, topic_paths):
    legacy = knowledge._legacy_projection_moves(ledger)
    protected = {legacy.get(relative, relative) for relative in result.get("protected_paths", ())
                 if relative in ledger["projections"] or relative in legacy}
    projected = ledger["projections"]
    for identifier in result.get("topic_ids", ()):
        topic = ledger["topics"].get(identifier)
        if not topic:
            continue
        note, canvas = knowledge._topic_paths(identifier, topic_paths)
        stem = knowledge._topic_details_folder(canvas)
        paths = [note, canvas]
        paths.extend(path for path in projected if path.startswith(f"{stem}/") and path.endswith(".canvas"))
        rendered = None
        for relative in paths:
            expected = projected.get(relative)
            if not expected or relative in protected:
                continue
            try:
                actual = source_bytes(inside(vault, relative))
                if relative == note:
                    _, actual, _ = knowledge._checked_markdown(actual, ledger, relative)
            except FileNotFoundError:
                continue
            except RequestError:
                protected.add(relative)
                continue
            if digest(actual) == expected:
                continue
            if relative != note:
                # Obsidian 的序列化或节点次序变化保留为等价白板。
                if rendered is None:
                    rendered = knowledge._render_topic(identifier, topic, mapping, topic_paths, vault=vault)
                existing = knowledge._canvas_by_id(actual)
                if existing is not None and existing == knowledge._canvas_by_id(rendered.get(relative, b"")):
                    continue
            protected.add(relative)
    return protected


def _receipt_line(vault, result):
    batch = result.get("model_batch")
    if not isinstance(batch, str) or not re.fullmatch(r"[a-f0-9]{64}", batch):
        return "模型和 token 用量缺少回执"
    checkpoint = result.get("model_checkpoint")
    if not isinstance(checkpoint, str) or not re.fullmatch(r"[a-f0-9]{64}", checkpoint):
        return "知识总结模型与 token 用量缺少可唯一对应的回执"
    paths = [inside(vault, f"{knowledge.STATE}/{batch}-{checkpoint}-{stage}.json")
             for stage in ("proposal", "review")]
    try:
        receipts = [json.loads(source_bytes(path)) for path in paths]
    except FileNotFoundError:
        return "知识总结模型与 token 用量缺少回执"
    timings = [receipt.get("timing") if isinstance(receipt, dict) else None for receipt in receipts]
    timings = [timing if isinstance(timing, dict) else {} for timing in timings]
    durations = [timing.get("elapsed_ms") for timing in timings]
    elapsed = (_duration(0, sum(durations) / 1000)
               if all(type(item) is int and item >= 0 for item in durations) else "缺少计时回执")
    models = {timing["model"] for timing in timings if timing.get("model")}
    usages = [timing["usage"] if isinstance(timing.get("usage"), dict) else {} for timing in timings]
    total = "未取得用量"
    if all(type(u.get("input_tokens")) is int and type(u.get("output_tokens")) is int for u in usages):
        total = f"{sum(u['input_tokens'] + u['output_tokens'] for u in usages):,}（输入含缓存）"
    model = "、".join(sorted(models)) or "历史回执未记录实际模型"
    return f"知识总结模型 · {model} · 两轮调用合计 {elapsed} · token {total}"


def render(vault, db, active=(), warning=None, *, as_canvas=False):
    posted = db.execute("""SELECT b.*,e.at AS published FROM workflow_events e
        JOIN knowledge_batches b ON b.scope=e.scope WHERE e.kind='knowledge_finished'
        ORDER BY e.at DESC,e.id DESC LIMIT 1""").fetchone()
    current = db.execute("""SELECT * FROM knowledge_batches WHERE state IN ('working','pending','needs_retry')
        ORDER BY CASE WHEN state='working' THEN 0 ELSE 1 END, rowid DESC LIMIT 1""").fetchone()
    result = json.loads(posted["result"]) if posted else {}
    active_result = json.loads(current["result"]) if current else {}
    scopes = set(result.get("new_versions", ()))
    current_scopes = set(active_result.get("new_versions", ()))
    completed = posted["published"] if posted else None
    articles = db.execute("""SELECT w.*,s.scope AS knowledge_scope,
        EXISTS (SELECT 1 FROM domain_navigation_verified n WHERE n.sha256=w.sha256) AS indexed,
        (SELECT max(at) FROM workflow_events e WHERE e.scope=w.sha256 AND e.kind='article_signed') AS signed
        FROM workflow w LEFT JOIN knowledge_sources s USING(sha256) ORDER BY admitted,w.rowid""").fetchall()
    newest_batch_intake = max((row["admitted"] for row in articles if row["sha256"] in scopes), default=0)
    tail = [row for row in articles if row["sha256"] in current_scopes or row["sha256"] in active or (
        row["knowledge_scope"] is None and row["sha256"] not in scopes and completed is not None and (
            (row["signed"] is not None and row["signed"] > completed)
            or row["admitted"] >= int(completed) + 1
            or row["admitted"] >= newest_batch_intake and row["phase"] != "ready"))]
    newer = bool(tail)
    if newer:
        articles = tail
    elif scopes:
        articles = [row for row in articles if row["sha256"] in scopes]
    elif articles:
        last = articles[-1]["admitted"]
        articles = [row for row in articles if row["admitted"] >= last - 120]

    ledger = knowledge._state(inside(vault, f"{knowledge.STATE}/ledger.json"))
    mapping = locations.load(vault)
    topic_paths = knowledge._topic_path_map(ledger["topics"])
    protected = _protected_entries(vault, result, ledger, mapping, topic_paths)
    published = list(_changes(vault, result, ledger["topics"], mapping, protected, topic_paths, as_canvas)) if completed else []
    topic_links = [line for line, _, _ in published]
    visible_events = sum(events for _, events, _ in published)
    visible_evidence = sum(evidence for _, _, evidence in published)
    expected_events = len(result.get("event_ids") or ())
    expected_evidence = len(result.get("evidence_links") or ())
    missing_updates = visible_events < expected_events or visible_evidence < expected_evidence
    changes = []
    if visible_events:
        changes.append(f"新增记录 {visible_events} 条")
    if visible_evidence:
        changes.append(f"补充出处 {visible_evidence} 条")
    no_topic = posted and (posted["state"] == "no_common_topic" or result.get("topics") == 0)
    if missing_updates and not protected:
        changes.append("有内容的入口待核对")
    if posted and not changes and not missing_updates:
        changes.append("已校读，本批没有形成可跟踪主题" if no_topic else "已校读，本批未产生知识增量")
    if protected:
        changes.append(f"{len(protected)} 个入口保留手改")
    now = time.time()
    readable = []
    progress = []
    visible_times = []
    indexed_times = []
    pending = blocked = 0
    for row in articles:
        sha = row["sha256"]
        folder = locations.resolve(row["folder"], mapping)
        name = folder.rsplit("/", 1)[-1].rsplit(" · ", 1)[0]
        board = f"{folder}/文章白板.canvas"
        if as_canvas and row["phase"] == "ready":
            board = _signed_board(vault, sha, row["folder"], board)
        moments = dict(db.execute("SELECT kind,min(at) FROM workflow_events WHERE scope=? GROUP BY kind", (sha,)))
        status = _phase(row, moments, active, now)
        if row["phase"] == "ready" and inside(vault, board).is_file():
            signed = moments.get("article_signed")
            indexed = moments.get("article_indexed") if row["indexed"] else None
            at = signed if signed is not None else indexed
            visible_times.append(at)
            indexed_times.append(indexed)
            readable.append((at or 0, f"- [[{board}|{_alias(name)}]]" + (f" · {_clock(at)}可读" if at else "")
                             + (" · 资料档案索引待更新" if not row["indexed"] else "")))
        elif row["phase"] == "ready":
            status = "已签发，白板文件待核对"
            blocked += 1
        elif (sha in active or not row["error"]
              or row["next_at"] and row["attempts"] < 3 and row["error"] != "local_only"):
            pending += 1
        else:
            blocked += 1
        start = moments.get("article_started")
        wait = (_duration(row["admitted"], now) + "（仍在排队）" if status == "排队中"
                else _duration(row["admitted"], start))
        progress.append(f"- **{plain(name)}**　{status} · 排队 {wait} · 返工与重试记录 {row['attempts']} 次")
    if warning:
        label = "投放目录需要重新核对，当前只收录原文"
    elif current and current["state"] == "working":
        label = "正在整理知识总结"
    elif current and current["state"] == "pending" and current["attempts"] < 3:
        next_at = current["next_at"]
        reason = ERRORS.get(current["error"], "知识总结暂未完成")
        label = f"{reason} · {_clock(next_at)}后重试" if next_at > now else f"{reason} · 等待重试"
    elif current:
        label = "知识总结待处理"
    elif newer or not posted:
        label = "等待可总结的签发材料"
    else:
        label = " · ".join(changes)

    begin = min((row["admitted"] for row in articles), default=None)
    observed = [at for at in visible_times if at is not None]
    first = min(observed) if observed and len(observed) == len(visible_times) else None
    first_index = min(indexed_times) if indexed_times and None not in indexed_times else None
    all_readable = max(observed) if first is not None and len(readable) == len(articles) else None
    recent = "上一批的主题变化" if newer else "这批有哪些变化"
    if completed and topic_links:
        recent += f" · {_clock(completed)}发布"
    if newer and not topic_links:
        recent = "主题变化"
    details = ["> [!afr-progress]- 逐篇进度与批次耗时",
               f"> 从入队起 · 首篇可读 {_duration(begin, first)} · 全部可读 {_duration(begin, all_readable)}"
               f" · 首篇进入索引 {_duration(begin, first_index)}"
               f" · 知识发布 {_duration(begin, completed if topic_links and not newer else None)}"]
    if posted:
        context = "上一批" if newer else ""
        details.append("> " + context + _receipt_line(vault, result))
    details.extend([">", *[f"> {line}" for line in progress]])
    archive = "资料档案/资料索引.md"
    checks = "资料档案/读取核验/读取核验.md"
    links = [f"[[{archive}|按领域读文章]]", f"[[{knowledge.OVERVIEW}|按分类看知识]]"]
    if inside(vault, checks).is_file():
        links.append(f"[[{checks}|读取核验]]")
    lines = ["# AFR 阅读首页", "", " · ".join(links), "",
             "> [!afr-status] 最近投放",
             f"> **{len(readable)} 篇可读** · {pending} 篇处理中 · {blocked} 篇待处理　　{label}", "",
             *details, "", "## 这批可以读的文章", ""]
    lines += [item for _, item in sorted(readable, reverse=True)] or ["签发后，文章白板会出现在这里。"]
    if topic_links:
        recent_lines = topic_links
    elif missing_updates:
        recent_lines = ["本批回执已保留，手改入口沿用当前内容。" if protected
                        else "本批回执已保留，相关入口待核对。"]
    elif no_topic and not newer:
        recent_lines = ["本批材料校读后没有形成可持续跟踪的主题。"]
    elif posted and not newer:
        recent_lines = ["本批没有新增判断或补充出处。"]
    else:
        recent_lines = ["签发并发布后的知识变化会出现在这里。"]
    if as_canvas:
        progress_detail = [line.removeprefix("> ") for line in details[1:] if line != ">"]
        sections = [
            ("最近投放", [f"**{len(readable)} 篇可读** · {pending} 篇处理中 · {blocked} 篇待处理",
                          label]),
            ("这批可以读的文章", [item for _, item in sorted(readable, reverse=True)]),
            (recent, recent_lines),
            ("逐篇进度与批次耗时", progress_detail),
        ]
        return reading_canvas.home_page(sections, knowledge._text_node, knowledge._json_bytes,
                                        knowledge._card_height)
    lines += ["", f"## {recent}", "", *recent_lines]
    if protected:
        lines += ["", "### 保留手改的入口", ""]
        for relative in sorted(protected):
            try:
                exists = inside(vault, relative).is_file()
            except RequestError:
                exists = False
            if exists:
                lines.append(f"- [[{relative}|{_alias(relative)}]]")
            else:
                lines.append(f"- {plain(relative)}（原位置不可打开）")
    return ("\n".join(lines) + "\n").encode("utf-8")


def refresh(vault, *, active=(), warning=None):
    from .workflow import database

    with contextlib.closing(database(vault)) as db:
        db.execute("BEGIN")
        content = render(vault, db, active, warning)
    path = inside(vault, HOME)
    receipt = inside(vault, HOME_STATE)
    with inside(vault, ".afr/reading-home.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            original = source_bytes(path)
        except FileNotFoundError:
            original = None
        try:
            old_receipt = source_bytes(receipt)
            saved = json.loads(old_receipt)
        except FileNotFoundError:
            old_receipt = None
            saved = {}
        checkpoints = saved.get("prefixes", [saved]) if isinstance(saved, dict) else []
        suffix = b""
        previous = None
        if original is not None:
            if original.startswith(content):
                suffix = original[len(content):]
            else:
                for checkpoint in checkpoints:
                    size = checkpoint.get("bytes") if isinstance(checkpoint, dict) else None
                    if type(size) is int and 0 < size <= len(original) and digest(original[:size]) == checkpoint.get("sha256"):
                        previous = checkpoint
                        suffix = original[size:]
                        break
                if previous is None:
                    prefix, _, previous_suffix = knowledge._generated_markdown(original)
                    suffix = prefix + previous_suffix
                    backup = inside(vault, ".afr/reading-home-legacy.md")
                    if not backup.exists():
                        _write_projection(backup, None, original)
        current = {"bytes": len(content), "sha256": digest(content)}
        prefixes = [current, previous] if previous else [current]
        # 先登记新旧两个可核验版本，写作中断后可从任一版本继续。
        _write_projection(receipt, old_receipt, knowledge._json_bytes({"prefixes": prefixes}))
        _write_projection(path, original, content + suffix)
    return {"path": HOME}



def refresh_canvas(vault, *, active=(), warning=None):
    from collections import Counter, defaultdict
    from .workflow import database

    manifest = inside(vault, ".afr/reading-canvas-home.json")
    with inside(vault, ".afr/reading-home.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with contextlib.closing(database(vault)) as db:
            db.execute("BEGIN")
            home = render(vault, db, active, warning, as_canvas=True)
            ready = db.execute("SELECT sha256,folder FROM workflow WHERE phase='ready'").fetchall()
        ledger = knowledge._state(inside(vault, f"{knowledge.STATE}/ledger.json"))
        mapping = locations.load(vault)
        labels = {key: label for _, label, _, entries in knowledge.DOMAINS for key, _ in entries}
        votes = defaultdict(Counter)
        for topic in ledger["topics"].values():
            area = labels.get(knowledge._category(topic))
            if area not in reading_canvas.AREAS:
                continue
            for event in topic["events"]:
                folder = locations.resolve(event["source_path"], mapping).partition("#")[0].rsplit("/", 1)[0]
                votes[folder][area] += 1
        categories = {folder: ("综合研究" if len(areas) > 1 and areas.most_common(2)[0][1] == areas.most_common(2)[1][1]
                               else areas.most_common(1)[0][0]) for folder, areas in votes.items()}
        signed_boards = {}
        for sha, prior in ready:
            folder = locations.resolve(prior, mapping)
            signed_boards[folder] = _signed_board(vault, sha, prior, f"{folder}/文章白板.canvas")
        pages = reading_canvas.archive_pages(vault, categories, knowledge._text_node,
                                             knowledge._json_bytes, knowledge._card_height, signed_boards=signed_boards)
        pages[reading_canvas.HOME] = home
        try:
            old = source_bytes(manifest)
            saved = json.loads(old)
        except FileNotFoundError:
            old, saved = None, {}
        protected = []
        for relative, content in pages.items():
            path = inside(vault, relative)
            previous = saved.get(relative, {})
            expected = previous.get("bytes")
            try:
                try:
                    current = source_bytes(path)
                except FileNotFoundError:
                    current = None
                if current and _canvas_digest(current) == previous.get("nodes"):
                    expected = digest(current)
                observed = knowledge._publish_projection(path, content, expected)
            except RequestError as exc:
                if str(exc) not in ("user_edit_conflict", "invalid_path"):
                    raise
                protected.append(relative)
                continue
            saved[relative] = {"bytes": observed, "nodes": _canvas_digest(content)}
        updated = knowledge._json_bytes(saved)
        if updated != old:
            _write_projection(manifest, old, updated)
    return {"path": reading_canvas.HOME, "protected_paths": protected}


def _canvas_digest(content):
    indexed = knowledge._canvas_by_id(content)
    return digest(json.dumps(indexed, sort_keys=True, ensure_ascii=False).encode()) if indexed is not None else None


def _signed_board(vault, sha, folder, fallback):
    try:
        return semantic.publication_paths(vault, sha, folder)["canvas"]
    except (RequestError, OSError):
        return fallback
