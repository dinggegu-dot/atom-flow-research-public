"""本地投放、证据冻结与可重试的首次发布。"""
import contextlib
from collections import defaultdict
import ctypes
from datetime import date, datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import zlib
from zoneinfo import ZoneInfo

from .library import RequestError
from .filing import classify_article, is_generic_title
from . import locations

SUPPORTED = {".docx", ".pdf", ".md", ".txt"}
MAX_BYTES = 100 * 1024 * 1024
SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATED_STEM = re.compile(r"(\d{8}|\d{4}-\d{2}-\d{2}|\d{4}年\d{2}月\d{2}日)"
                         r"(?:-(\d{2,})|[（(](\d+)[）)])?")


def source_day(modified_ns):
    return datetime.fromtimestamp(modified_ns // 1_000_000_000, SHANGHAI).date()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_bytes(path, *, dir_fd=None):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise RequestError("unsupported_file")
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(data) != before.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise RequestError("file_in_progress")
        return data


def private_json(path, limit):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("private_file_required")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("file_too_large")
        return json.loads(raw)


def frozen(db, sha):
    if not db:
        return None
    # 只接受指向同一字节版本原件的旧解析记录。
    with contextlib.closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("""
            SELECT r.payload_json FROM document_representations r
            JOIN evidence_snapshots s ON s.id=r.snapshot_id AND s.source_id=r.source_id
            WHERE EXISTS (SELECT 1 FROM json_each(s.artifact_manifest_json) a
              WHERE json_extract(a.value,'$.object_sha256')=?)
            ORDER BY r.created_at DESC LIMIT 1""", (sha,)).fetchone()
    return json.loads(row[0]) if row else None


def units(path, data, sha, db):
    rep = frozen(db, sha)
    if not rep and path.suffix.lower() == ".docx":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        from app.document_representation.adapters.native_docx import NativeDocxAdapter
        from app.document_representation.domain import ParserInput, ParseOptions
        source = ParserInput(sha, sha, "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", path.name, "", data)
        rep = NativeDocxAdapter().parse(source, ParseOptions()).model_dump()
        # 新文档定位由固定原件和原序确定，崩溃后的重新提取应产生相同引用。
        for number, element in enumerate(rep["elements"], 1):
            element["id"] = f"{sha}:e{number}"
    if rep:
        parts = [dict(kind=e["type"], text=e["content"], locator=e["locator"], element=e["id"])
                 for e in rep["elements"] if e["content"].strip()]
        return parts, rep["warnings"], rep["source_id"], rep["snapshot_id"]
    parts, gaps = [], []
    if path.suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise RequestError("encrypted_pdf")
            for n, page in enumerate(doc, 1):
                blocks = page.get_text("blocks", sort=True)
                content = [b for b in blocks if b[6] == 0 and b[4].strip()]
                parts.extend(dict(kind="paragraph", text=b[4].strip(), locator={"page": n, "bbox":list(b[:4])}) for b in content)
                if not content:
                    gaps.append(f"第 {n} 页需要 OCR")
            gaps.append("仅提取 PDF 文字，图表与版面仍需核对")
    else:
        text = data.decode("utf-8-sig")
        offset = 0
        paragraph = None
        for line in text.splitlines(keepends=True):
            raw = line.rstrip("\r\n")
            heading = re.match(r"^ {0,3}#{1,6}[ \t]+", raw)
            if paragraph is not None and (heading or not raw.strip()):
                parts.append(dict(kind="paragraph", text=text[paragraph:offset].rstrip("\r\n"), locator={"offset": paragraph}))
                paragraph = None
            if heading:
                parts.append(dict(kind="heading", text=raw, locator={"offset": offset}))
            elif raw.strip() and paragraph is None:
                paragraph = offset
            offset += len(line)
        if paragraph is not None:
            parts.append(dict(kind="paragraph", text=text[paragraph:].rstrip("\r\n"), locator={"offset": paragraph}))
    return parts, gaps, sha, sha


def plain(value):
    # 原文另存原件。阅读页使用文字显示，禁用材料自带的嵌入、HTML 和外链。
    import html
    return re.sub(r'([\\`*_{}\[\]()#+!|^~=\->$])', r'\\\1', html.escape(value))


def safe_name(title):
    cleaned = re.sub(r'[\x00-\x1f\[\]#^|/\\:*?"<>]', " ", title).strip(" .")
    # 同时为版本后缀与文件扩展名留出空间。
    return cleaned[:65].encode()[:180].decode(errors="ignore").strip() or "研报"


def inside(root, relative):
    target = root / relative
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        raise RequestError("invalid_path") from None
    if any(p.is_symlink() for p in [target, *target.parents] if p != root and root in p.parents):
        raise RequestError("invalid_path")
    return target


def exclusive_json(path, obj):
    # 同一文件系统中原子发布；已存在的版本与用户修改永不被覆盖。
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as f:
        tmp = Path(f.name)
        try:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink()


def initial_files(evidence, original):
    relative = evidence["folder"]
    suffix = evidence["suffix"]
    parts = evidence["parts"]
    gaps = evidence["gaps"]
    text = [f"# {plain(evidence['title'])}", f"[[{relative}/原件{suffix}|打开原件]]",
            "读取范围　" + "、".join(map(plain, gaps)) if gaps else "已提取可读文字"]
    for i, unit in enumerate(parts, 1):
        page = unit["locator"].get("page")
        line = f"[[{relative}/原件.pdf#page={page}|原件第 {page} 页]]" if page and suffix == ".pdf" else ""
        text.extend([f"## 第 {i} 段", plain(unit["text"]), f"^e{i}", line])
    return {
        "原件" + suffix: original,
        "固定原文.md": "\n\n".join(text).encode(),
        "证据索引.json": json.dumps(evidence, ensure_ascii=False).encode(),
    }


def publish_first(conn, vault, path, data, sha, legacy, receipt, moved):
    if receipt:
        evidence = json.loads(zlib.decompress(receipt[0]))
        relative = locations.resolve(evidence["folder"], moved)
    else:
        parts, gaps, source_id, snapshot_id = units(path, data, sha, legacy)
        if not parts:
            gaps = list(gaps) + ["没有可读文字，原件可打开"]
        title, domain = classify_article(path.stem, parts)
        relative = locations.archive_folder(domain, f"{safe_name(title)} · {sha[:16]}")
        evidence = dict(version=1, sha256=sha, title=title, source_id=source_id, snapshot_id=snapshot_id,
                        gaps=gaps, parts=parts, folder=relative, suffix=path.suffix.lower())
    target = inside(vault, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    outputs = initial_files(evidence, data)
    reading = outputs["固定原文.md"]
    if relative != evidence["folder"]:
        outputs["固定原文.md"] = locations.rewrite(reading.decode(), moved).encode()
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".afr-") as stage:
        out = Path(stage)
        for name, content in outputs.items():
            (out / name).write_bytes(content)
        if not target.exists():
            out.rename(target)
        else:
            for name, content in outputs.items():
                saved = inside(vault, relative + "/" + name)
                if not saved.exists():
                    os.link(out / name, saved)
                elif digest(source_bytes(saved)) != digest(content):
                    raise RequestError("evidence_changed")
    encoded = outputs["证据索引.json"]
    conn.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?)",
                 (sha, zlib.compress(encoded), digest(encoded), digest(reading), bool(evidence["parts"])))
    return relative


def initial_state(conn, vault, sha, folder, receipt, moved):
    if not receipt:
        return "missing"
    folder = locations.resolve(folder, moved)
    original_folder = folder
    if folder in moved.values():
        original_folder = json.loads(zlib.decompress(receipt[0]))["folder"]
        if locations.resolve(original_folder, moved) != folder:
            raise RequestError("version_conflict")
    for name, expected in (("证据索引.json", receipt[1]), ("固定原文.md", receipt[2])):
        path = inside(vault, folder + "/" + name)
        if not path.is_file():
            return "missing"
        if name == "固定原文.md" and original_folder != folder:
            text = source_bytes(path).decode()
            mapping = {original_folder: folder}
            normalized = locations.rewrite(text, mapping, reverse=True)
            if locations.rewrite(normalized, mapping) != text:
                raise RequestError("evidence_changed")
            actual = digest(normalized.encode())
        else:
            actual = remember(conn, path)
        if actual != expected:
            raise RequestError("evidence_changed")
    files = list(inside(vault, folder).glob("原件.*"))
    if not files:
        return "missing"
    if len(files) != 1 or remember(conn, inside(vault, str(files[0].relative_to(vault)))) != sha:
        raise RequestError("evidence_changed")
    return "ready"


def library_originals(vault, moved=None):
    manifest = inside(vault, ".afr/legacy-import-manifest.json")
    if not manifest.exists():
        manifest = inside(vault, "资料导入清单.json")
        if not manifest.exists():
            return
    if moved is None:
        moved = locations.load(vault)
    rows = json.loads(manifest.read_text())
    if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get("originals"), list)
            and all(isinstance(name, str) for name in row["originals"]) for row in rows):
        raise RequestError("invalid_manifest")
    for row in rows:
        for original in row["originals"]:
            yield inside(vault, locations.resolve(original, moved))


def raise_walk_error(error):
    raise error


def inbox_files(inbox, *, strict=False):
    for parent, dirs, names in os.walk(inbox, onerror=raise_walk_error if strict else None):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not Path(parent, d).is_symlink())
        for name in sorted(names):
            path = Path(parent, name)
            if name.startswith((".", "~$")) or path.is_symlink():
                continue
            if strict:
                regular = stat.S_ISREG(path.lstat().st_mode)
            else:
                regular = path.is_file()
            if regular:
                yield path


def check_intake_roots(vault, inbox):
    if not vault.is_dir() or not inbox.is_dir() or vault == inbox or vault in inbox.parents or inbox in vault.parents:
        raise RequestError("invalid_path")


class InboxChanged(RequestError):
    pass


def inbox_identity(inbox, metadata=None):
    st = inbox.stat() if metadata is None else metadata
    return [str(inbox.resolve()), st.st_dev, st.st_ino]


def check_inbox_identity(inbox, expected):
    if expected is None:
        return
    try:
        current = inbox_identity(inbox)
    except OSError as exc:
        raise InboxChanged("inbox_changed") from exc
    if current != expected:
        raise InboxChanged("inbox_changed")


@contextlib.contextmanager
def open_inbox(inbox, expected):
    check_inbox_identity(inbox, expected)
    try:
        fd = os.open(inbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if expected is not None:
            raise InboxChanged("inbox_changed") from exc
        raise
    try:
        if expected is not None and inbox_identity(inbox, os.fstat(fd)) != expected:
            raise InboxChanged("inbox_changed")
        yield fd
        check_inbox_identity(inbox, expected)
    finally:
        os.close(fd)


def open_inbox_files(inbox, fd):
    # 通过已经验证的目录描述符读取，路径在扫描期间换名也不会切换到新目录。
    for parent, dirs, names, parent_fd in os.fwalk(".", dir_fd=fd):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            if name.startswith((".", "~$")):
                continue
            yield inbox / parent / name, parent_fd


def _rename_exclusive(source, target, dir_fd):
    # Python 的 rename 会覆盖同名文件。macOS 和 Linux 都提供原子不覆盖移动。
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename, flags = libc.renameatx_np, 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename, flags = libc.renameat2, 1  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(dir_fd, os.fsencode(source), dir_fd, os.fsencode(target), flags) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def dated_names(inbox, inbox_fd):
    """同目录同日按修改时间编号，已定下的日期与序号继续占位。"""
    numbered = defaultdict(set)
    unnamed = defaultdict(list)
    for path, parent_fd in open_inbox_files(inbox, inbox_fd):
        if path.suffix.lower() not in SUPPORTED:
            continue
        try:
            metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        match = _DATED_STEM.fullmatch(path.stem)
        if match:
            try:
                day = date.fromisoformat(re.sub(r"\D", "", match[1]))
            except ValueError:
                continue
            ordinal = int(match[2]) if match[2] else int(match[3]) + 1 if match[3] else 1
            numbered[path.parent, day].add(ordinal)
        elif is_generic_title(path.stem):
            unnamed[path.parent, source_day(metadata.st_mtime_ns)].append((metadata.st_mtime_ns, path))
    targets = {}
    for key, files in unnamed.items():
        _, day = key
        label = day.strftime("%Y年%m月%d日")
        ordinal = max(numbered[key], default=0)
        single = not ordinal and len(files) == 1
        for modified_ns, path in sorted(files):
            ordinal += 1
            name = label if single else f"{label}-{ordinal:02d}"
            targets[path] = (name + path.suffix, modified_ns)
    return targets


def rename_dated(path, target, sha, modified_ns, dir_fd):
    def matches(name):
        metadata = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return metadata.st_mtime_ns == modified_ns and digest(source_bytes(name, dir_fd=dir_fd)) == sha

    if not matches(path.name):
        raise RequestError("file_in_progress")
    try:
        _rename_exclusive(path.name, target, dir_fd)
    except FileExistsError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}:
            return None
        raise
    try:
        if matches(target):
            return path.with_name(target)
    except (OSError, RequestError):
        pass
    # 若投放者已提交替换文件，尝试恢复旧名。两处都有文件时保留两份。
    with contextlib.suppress(FileExistsError, FileNotFoundError):
        _rename_exclusive(target, path.name, dir_fd)
    raise RequestError("file_in_progress")


def scan(vault,inbox,legacy=None,settle_seconds=0,*,rename_generic=False,expected_inbox=None):
    import fcntl
    import time
    if not 0 <= settle_seconds <= 3600:
        raise RequestError("invalid_request")
    check_inbox_identity(inbox, expected_inbox)
    check_intake_roots(vault, inbox)
    state = inside(vault,".afr")
    state.mkdir(exist_ok=True)
    with inside(vault,".afr/scan.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        moved = locations.load(vault)
        with open_inbox(inbox, expected_inbox) as inbox_fd, \
                contextlib.closing(sqlite3.connect(inside(vault,".afr/intake.sqlite"))) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS seen (path TEXT PRIMARY KEY, stamp TEXT, sha TEXT, folder TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS articles (sha TEXT PRIMARY KEY, folder TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS receipts (sha TEXT PRIMARY KEY, payload BLOB NOT NULL, evidence TEXT, reading TEXT, readable INTEGER)")
            conn.execute("CREATE TABLE IF NOT EXISTS source_times (sha TEXT PRIMARY KEY, modified_ns INTEGER NOT NULL)")
            dates_recorded = 0
            targets = dated_names(inbox, inbox_fd) if rename_generic else {}
            known = {remember(conn, original) for original in library_originals(vault, moved) if original.is_file()}
            added=[]; errors=[]; skipped=0
            renamed = 0
            present = set()
            settling = 0
            unsupported = []
            for p, parent_fd in open_inbox_files(inbox, inbox_fd):
                name = p.name
                try:
                    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    if p.suffix.lower() not in SUPPORTED:
                        unsupported.append(str(p.relative_to(inbox)))
                        continue
                    if time.time() - max(metadata.st_mtime, metadata.st_ctime) < settle_seconds:
                        settling += 1
                        continue
                    sha=remember(conn,p,dir_fd=parent_fd)
                    if _file_stamp(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _file_stamp(metadata):
                        raise RequestError("file_in_progress")
                    dates_recorded += conn.execute("INSERT OR IGNORE INTO source_times VALUES(?,?)",
                                                   (sha, metadata.st_mtime_ns)).rowcount
                    target, modified_ns = targets.get(p, (None, None))
                    if sha in known:
                        skipped+=1
                        check_inbox_identity(inbox, expected_inbox)
                        if target and rename_dated(p, target, sha, modified_ns, parent_fd):
                            renamed += 1
                        continue
                    row=conn.execute("SELECT folder FROM articles WHERE sha=?",(sha,)).fetchone()
                    receipt = conn.execute("SELECT payload,evidence,reading FROM receipts WHERE sha=?", (sha,)).fetchone()
                    if row and initial_state(conn,vault,sha,row[0],receipt,moved) == "ready":
                        present.add(sha)
                        check_inbox_identity(inbox, expected_inbox)
                        if target and rename_dated(p, target, sha, modified_ns, parent_fd):
                            renamed += 1
                        continue
                    data=source_bytes(name,dir_fd=parent_fd)
                    if digest(data)!=sha:
                        raise RequestError("file_in_progress")
                    check_inbox_identity(inbox, expected_inbox)
                    t=time.monotonic()
                    folder=publish_first(conn,vault,p,data,sha,legacy,receipt,moved)
                    conn.execute("INSERT OR IGNORE INTO articles VALUES(?,?)",(sha,folder))
                    conn.execute("UPDATE seen SET folder=? WHERE path=? AND sha=?",(folder,str(p),sha))
                    conn.commit()
                    present.add(sha)
                    added.append(dict(sha256=sha,folder=folder,publish_seconds=round(time.monotonic()-t,3)))
                    check_inbox_identity(inbox, expected_inbox)
                    if target and rename_dated(p, target, sha, modified_ns, parent_fd):
                        renamed += 1
                except InboxChanged:
                    raise
                except ImportError:
                    errors.append(dict(file=name,error="parser_unavailable"))
                except (RequestError, ValueError, OSError, RuntimeError) as exc:
                    errors.append(dict(file=name,error=str(exc) if isinstance(exc,RequestError) else type(exc).__name__))
            conn.commit()
            versions = [
                dict(sha256=sha, folder=locations.resolve(folder, moved), readable=bool(readable))
                for sha, folder, readable in conn.execute(
                    "SELECT a.sha,a.folder,r.readable FROM articles a JOIN receipts r ON r.sha=a.sha"
                )
                if sha in present and inside(vault, locations.resolve(folder, moved)).is_dir()
            ]
            pending = [
                dict(sha256=row["sha256"], folder=row["folder"]) for row in versions
                if row["readable"] and not all(inside(vault, row["folder"]+"/"+name).is_file()
                                               for name in ("文章白板.canvas", "完整阅读.md"))
            ]
            return dict(added=added,existing=skipped,errors=errors,pending=pending,settling=settling,renamed=renamed,
                        unsupported=unsupported,versions=versions,dates_recorded=dates_recorded)


def _file_stamp(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def remember(conn,path,*,dir_fd=None):
    name = path if dir_fd is None else path.name
    s=os.stat(name,dir_fd=dir_fd,follow_symlinks=False)
    stamp=":".join(map(str, _file_stamp(s)))
    row=conn.execute("SELECT sha FROM seen WHERE path=? AND stamp=?",(str(path),stamp)).fetchone()
    if row:
        return row[0]
    sha=digest(source_bytes(name,dir_fd=dir_fd))
    conn.execute("INSERT INTO seen(path,stamp,sha) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET stamp=excluded.stamp,sha=excluded.sha,folder=CASE WHEN seen.sha=excluded.sha THEN seen.folder END",
                (str(path),stamp,sha))
    return sha
