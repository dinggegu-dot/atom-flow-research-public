import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import sqlite3
import time

from . import library


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise library.RequestError("invalid_request")


def main():
    parser = Parser(prog="afr_agent")
    parser.add_argument("--version", action="version", version="afr_agent 1")
    actions = parser.add_subparsers(dest="action", required=True)
    search = actions.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--scope", choices=("all","article","topic","source","note"), default="all")
    read = actions.add_parser("read")
    read.add_argument("--ref", required=True)
    read.add_argument("--revision", required=True)
    scan = actions.add_parser("scan")
    scan.add_argument("--inbox", type=Path, required=True)
    scan.add_argument("--legacy-db", type=Path)
    scan.add_argument("--settle-seconds", type=float, default=0)
    evidence = actions.add_parser("evidence")
    publish = actions.add_parser("publish")
    process = actions.add_parser("process")
    process.add_argument("--approvals", type=Path)
    process.add_argument("--preserve-original", action="store_true", help="按独立审核的主题组织完整固定原文")
    process.add_argument("--draft", help="校读指定的本地草稿摘要")
    for cmd in (evidence, publish, process):
        cmd.add_argument("--sha256", required=True)
    publish.add_argument("--plan", type=Path, required=True)
    watch = actions.add_parser("watch", help="收录文件并用 Codex 处理许可版本")
    watch.add_argument("--inbox", type=Path, required=True)
    watch.add_argument("--approvals", type=Path)
    watch.add_argument("--legacy-db", type=Path)
    watch.add_argument("--settle-seconds", type=float, default=2)
    watch.add_argument("--interval", type=float, default=3)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--new-articles", action="store_true", help="按首次启用后的新投放自动授权")
    watch.add_argument("--rename-generic", action="store_true", help="按原件修改日期为投放目录的未命名研报补名")
    baseline = actions.add_parser("seed-baseline", help="记录已有版本，禁止新投放自动授权覆盖历史资料")
    baseline.add_argument("--inbox", type=Path, required=True)
    baseline.add_argument("--legacy-root", type=Path, action="append", default=[])
    baseline.add_argument("--legacy-db", type=Path)
    rebind = actions.add_parser("rebind-baseline", help="原投放目录设备号变化后恢复自动授权基线")
    rebind.add_argument("--inbox", type=Path, required=True)
    rebind.add_argument("--old-device", type=int, required=True)
    rebind.add_argument("--new-device", type=int, required=True)
    watch.add_argument("--interpret", action="store_true", help="试用经独立校读的整理论述")
    status = actions.add_parser("status", help="查看文章签发和知识总结状态")
    dashboard = actions.add_parser("dashboard", help="更新 Obsidian 阅读首页")
    views = actions.add_parser("refresh-views", help="从本机知识记录重绘知识白板和时间线")
    views.add_argument("--topic", action="append", help="仅重绘指定知识主题和相关导航，可重复")
    retry = actions.add_parser("retry", help="重排待处理版本")
    retry.add_argument("--sha256", required=True)
    adopt = actions.add_parser("adopt-candidate", help="采纳已校读的跨篇候选研究问题")
    adopt.add_argument("--batch", required=True)
    adopt.add_argument("--position", required=True, type=int)
    for cmd in (read, search, scan, evidence, publish, process, watch, baseline, rebind, status, dashboard, views, retry, adopt):
        cmd.add_argument("--vault", type=Path, required=True)
    try:
        args = parser.parse_args()
        vault = args.vault.expanduser().resolve()
        if args.action == "search":
            data = library.search(vault, args.query, args.limit, args.scope)
        elif args.action == "scan":
            from .intake import scan as collect
            data = collect(vault, args.inbox.expanduser().resolve(), args.legacy_db,args.settle_seconds)
        elif args.action == "evidence":
            from .presentation import evidence_for
            _, data = evidence_for(vault, args.sha256)
        elif args.action == "process":
            from .semantic import process as understand
            data = understand(vault, args.sha256, args.approvals, draft_sha=args.draft,
                              preserve_original=args.preserve_original)
        elif args.action == "publish":
            from .presentation import publish as save
            data = save(vault, args.sha256, json.loads(args.plan.read_text()))
        elif args.action == "seed-baseline":
            from .workflow import seed_baseline
            data = seed_baseline(vault, args.inbox.expanduser().resolve(),
                                 legacy_roots=[p.expanduser().resolve() for p in args.legacy_root],
                                 legacy_db=args.legacy_db.expanduser().resolve() if args.legacy_db else None)
        elif args.action == "rebind-baseline":
            from .workflow import rebind_baseline
            data = rebind_baseline(vault, args.inbox.expanduser().resolve(),
                                   old_device=args.old_device, new_device=args.new_device)
        elif args.action == "watch":
            from . import dashboard, knowledge, workflow
            if not 0.2 <= args.interval <= 3600:
                raise library.RequestError("invalid_request")
            legacy_db = args.legacy_db.expanduser().resolve() if args.legacy_db else None
            with workflow.watcher_lock(vault), (nullcontext(None) if args.once else workflow.WatchJobs()) as jobs:
                view_error = None
                try:
                    knowledge.refresh_views(vault)
                except (OSError, ValueError, sqlite3.Error) as exc:
                    view_error = str(exc) if isinstance(exc, library.RequestError) else "local_io_failed"
                last = dashboard_state = None
                while True:
                    data = workflow.cycle(vault, args.inbox.expanduser().resolve(), args.approvals,
                                          settle_seconds=args.settle_seconds,
                                          preserve_original=not args.interpret,
                                          new_articles=args.new_articles, legacy_db=legacy_db,
                                          rename_generic=args.rename_generic, jobs=jobs)
                    if view_error:
                        data["knowledge_view_error"] = view_error
                    active = frozenset(row["sha256"] for row in jobs.articles.values()) if jobs else frozenset()
                    snapshot = (dict(data), active)
                    if snapshot != dashboard_state:
                        try:
                            dashboard.refresh_canvas(vault, active=active, warning=data.get("warning"))
                        except (OSError, ValueError, sqlite3.Error) as exc:
                            error = str(exc) if isinstance(exc, library.RequestError) else "local_io_failed"
                            data["dashboard_error"] = error
                        else:
                            dashboard_state = snapshot
                    if args.once:
                        break
                    if data != last:
                        print(json.dumps({**data, "version": 1, "ok": True}, ensure_ascii=False), flush=True)
                        last = data
                    if data.get("warning"):
                        time.sleep(args.interval)
                    else:
                        jobs.wait(args.interval)
        elif args.action == "refresh-views":
            from .knowledge import refresh_views
            protected = refresh_views(vault, force=True, selected=args.topic)
            data = {"state": "no_knowledge" if protected is None else "protected" if protected else "refreshed",
                    "protected_paths": protected or []}
        elif args.action == "dashboard":
            from .dashboard import refresh_canvas
            data = refresh_canvas(vault)
        elif args.action == "status":
            from .workflow import status as progress
            data = progress(vault)
        elif args.action == "retry":
            from .workflow import retry as requeue
            data = requeue(vault, args.sha256)
        elif args.action == "adopt-candidate":
            from .reconcile import adopt_candidate
            data = adopt_candidate(vault, args.batch, args.position)
        else:
            data = library.read(vault, args.ref, args.revision)
        result = {**data, "version": 1, "ok": True}
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error, ImportError, KeyError, TypeError, AttributeError) as exc:
        code = str(exc) if isinstance(exc, library.RequestError) else "io_error"
        result = dict(version=1, ok=False, error=code)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
