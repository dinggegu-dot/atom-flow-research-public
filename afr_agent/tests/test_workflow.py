"""合成材料的收录、研究问题和恢复路径。"""
import hashlib
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import Mock, patch

from afr_agent import intake, knowledge, questions, semantic, workflow
from afr_agent.presentation import CHAPTER_READING_MARKER


class WorkflowTests(unittest.TestCase):
    def test_watch_indexes_fast_article_and_resumes_due_retry_while_long_article_runs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inbox, vault, policy = root / "inbox", root / "vault", root / "policy.json"
            inbox.mkdir()
            vault.mkdir()
            policy.touch(mode=0o600)
            slow, retry, fast = [f"{n:064x}" for n in range(1, 4)]
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=[slow, retry, fast])))
            with workflow.database(vault) as db:
                db.executemany("INSERT INTO workflow(sha256,folder,admitted) VALUES (?,'ignored',?)",
                               [(slow, 1), (retry, 2), (fast, 3)])
            slow_running, finish_slow = Event(), Event()
            retry_calls = []

            def process(_vault, sha, _approvals, **_options):
                if sha == slow:
                    slow_running.set()
                    if not finish_slow.wait(5):
                        raise AssertionError("the test did not release the long article")
                if sha == retry:
                    retry_calls.append(sha)
                    if len(retry_calls) == 1:
                        raise workflow.RequestError("codex_failed")
                return 1

            with workflow.WatchJobs() as jobs, patch.object(workflow, "process_article", side_effect=process), \
                    patch("afr_agent.domain_index.ensure_entry", return_value=True) as index, \
                    patch.object(workflow, "_knowledge_worker"):
                try:
                    workflow.cycle(vault, inbox, policy, jobs=jobs, settle_seconds=0)
                    self.assertTrue(slow_running.wait(2))
                    jobs.wait(2)
                    workflow.cycle(vault, inbox, policy, jobs=jobs, settle_seconds=0)
                    jobs.wait(2)
                    workflow.cycle(vault, inbox, policy, jobs=jobs, settle_seconds=0)
                    index.assert_called_once_with(vault, "ignored", fast)
                    self.assertEqual(len(retry_calls), 1)
                    with workflow.database(vault) as db:
                        self.assertEqual(db.execute("SELECT phase FROM workflow WHERE sha256=?", (slow,)).fetchone()[0],
                                         "admitted")
                        db.execute("UPDATE workflow SET next_at=0 WHERE sha256=?", (retry,))
                    workflow.cycle(vault, inbox, policy, jobs=jobs, settle_seconds=0)
                    jobs.wait(2)
                    workflow.cycle(vault, inbox, policy, jobs=jobs, settle_seconds=0)
                    self.assertEqual(len(retry_calls), 2)
                    self.assertFalse(finish_slow.is_set())
                    index.assert_called_with(vault, "ignored", retry)
                finally:
                    finish_slow.set()

    def test_knowledge_aggregates_ready_sources_and_adds_late_article_separately(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            vault, policy = root / "vault", root / "policy.json"
            vault.mkdir()
            policy.touch(mode=0o600)
            sources = [f"{n:064x}" for n in range(1, 4)]
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=sources)))
            with workflow.database(vault) as db, patch.object(knowledge, "run", return_value={"topics": 1}) as run:
                with db:
                    db.executemany("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',1,'ready')",
                                   [(sha,) for sha in sources[:2]])
                    db.execute("INSERT INTO workflow(sha256,folder,admitted) VALUES (?,'',1)", (sources[2],))
                    for sha in sources[:2]:
                        workflow._event(db, sha, "article_signed")
                workflow.supplement_knowledge(vault, db, policy)
                run.assert_not_called()
                with db:
                    db.execute("UPDATE workflow_events SET at=at-? WHERE kind='article_signed'",
                               (workflow.KNOWLEDGE_MAX_WAIT_SECONDS + 1,))
                workflow.supplement_knowledge(vault, db, policy)
                run.assert_called_once_with(vault, sources[:2], policy, new_version_ids=sources[:2])
                with db:
                    db.execute("UPDATE workflow SET phase='ready' WHERE sha256=?", (sources[2],))
                    workflow._event(db, sources[2], "article_signed")
                workflow.supplement_knowledge(vault, db, policy)
                self.assertEqual(run.call_args.args[1], sources[2:])
                self.assertEqual(run.call_args.kwargs["new_version_ids"], sources[2:])

    def test_bound_model_gate_checks_directory_again_for_running_jobs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inbox, vault, policy = root / "inbox", root / "vault", root / "policy.json"
            inbox.mkdir()
            vault.mkdir()
            policy.touch(mode=0o600)
            sha = "a" * 64
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=[sha])))
            workflow.seed_baseline(vault, inbox)
            baseline = vault / ".afr/automatic-baseline.json"
            stop = Event()
            approvals = semantic.WatchApprovals(policy, frozenset({sha}), frozenset(), baseline,
                                                tuple(intake.inbox_identity(inbox)), stop)
            self.assertTrue(semantic.allowed(sha, approvals))
            stop.set()
            self.assertFalse(semantic.allowed(sha, approvals))
            stop.clear()
            inbox.rename(root / "old-inbox")
            inbox.mkdir()
            self.assertFalse(semantic.allowed(sha, approvals))

    def test_due_knowledge_retry_does_not_wait_for_unrelated_long_article(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy.json"
            policy.touch(mode=0o600)
            sources = [f"{n:064x}" for n in range(1, 4)]
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=sources)))
            with workflow.database(root) as db, patch.object(knowledge, "run",
                    side_effect=[OSError, {"topics": 1}]) as run:
                with db:
                    db.executemany("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',0,'ready')",
                                   [(sha,) for sha in sources[:2]])
                workflow.supplement_knowledge(root, db, policy)
                with db:
                    db.execute("UPDATE knowledge_batches SET next_at=0")
                    db.execute("INSERT INTO workflow(sha256,folder,admitted) VALUES (?,'',1)", (sources[2],))
                workflow.supplement_knowledge(root, db, policy)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.args[1], sources[:2])
                self.assertEqual(workflow.status(root)["knowledge_summary"]["state"], "ready")

    def test_bound_watch_rechecks_settling_files_before_article_processing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inbox, vault = root / "inbox", root / "vault"
            inbox.mkdir()
            vault.mkdir()
            workflow.seed_baseline(vault, inbox)
            policy = root / "approvals.json"
            policy.touch(mode=0o600)
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=[])))
            first, second = inbox / "甲.md", inbox / "乙.md"
            first.write_text("第一份资料")
            second.write_text("第二份资料")
            first_sha, second_sha = [intake.digest(path.read_bytes()) for path in (first, second)]
            common = dict(renamed=0, errors=[], unsupported=[])
            first_version, second_version = [
                dict(sha256=sha, folder=path.stem) for sha, path in ((first_sha, first), (second_sha, second))]
            scans = [{**common, "added": [first_version], "versions": [first_version], "settling": 1},
                     {**common, "added": [second_version], "versions": [first_version, second_version], "settling": 0}]
            with patch.object(workflow, "scan", side_effect=scans) as scan, \
                    patch.object(workflow.time, "sleep") as wait:
                result = workflow.cycle(vault, inbox, policy, new_articles=True)
            self.assertEqual(scan.call_count, 2)
            wait.assert_called_once_with(2)
            self.assertEqual(result["collected"], 2)
            self.assertEqual({row["sha256"] for row in result["articles"]}, {first_sha, second_sha})

    def test_broken_review_receipt_does_not_stop_other_articles(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inbox, vault = root / "inbox", root / "vault"
            inbox.mkdir()
            vault.mkdir()
            broken, good = (b"First synthetic article.\n", b"Second synthetic article.\n")
            broken_sha, good_sha = (intake.digest(raw) for raw in (broken, good))
            for name, content in (("broken.md", broken), ("good.md", good)):
                (inbox / name).write_bytes(content)
            intake.scan(vault, inbox)
            policy = root / "approvals.json"
            policy.touch(mode=0o600)
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=[broken_sha, good_sha])))
            receipt = semantic.receipt(vault, broken_sha, "review")
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{incomplete json")
            groups = [dict(title="论述", branch="主题", start=1, end=1, summary="概要", argument="论述")]
            with patch.object(semantic.codex, "run_structured",
                              side_effect=[{"groups": groups}, {"passed": True, "issues": []}]) as run:
                state = workflow.cycle(vault, inbox, policy, settle_seconds=0)
            articles = {row["sha256"]: row for row in state["articles"]}
            self.assertEqual(articles[broken_sha]["error"], "invalid_review_receipt")
            self.assertEqual(articles[good_sha]["phase"], "ready")
            self.assertEqual(run.call_count, 2)

    def test_two_articles_trigger_knowledge_and_restart_without_reprocessing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inbox, vault = root / "inbox", root / "vault"
            inbox.mkdir()
            vault.mkdir()
            policy = root / "approvals.json"
            policy.touch(mode=0o600)
            first = b"July applied to 10 sites, of which 4 saw improvement.\n"
            second = b"August review used 20 sites and saw 4 improvements.\n"
            allowed = [hashlib.sha256(body).hexdigest() for body in (first, second)]
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=allowed)))

            parallel = Barrier(2, timeout=5)
            standard = semantic.load_article_board_standard()

            def article_call(instructions, payload, schema):
                if instructions == f"{semantic.REVIEW_INSTRUCTIONS}\n\n{standard}":
                    return {"passed": True, "issues": []}
                self.assertEqual(instructions, f"{semantic.SOURCE_TOPIC_INSTRUCTIONS}\n\n{standard}")
                parallel.wait()
                source = payload["evidence"][0]["text"]
                return {"groups": [{"title": "样本与统计口径", "branch": "样本",
                                    "start": 1, "end": 1, "summary": source, "argument": source}]}

            def summarize(vault, versions, approvals, *, new_version_ids):
                self.assertEqual(set(versions), set(allowed))
                self.assertEqual(set(new_version_ids), set(allowed))
                self.assertEqual(semantic.approved_versions(approvals), set(allowed))
                # 只有通过独立校读并签发的白板进入知识总结。
                for sha in versions:
                    plan = semantic.reviewed_plan(vault, sha)
                    self.assertEqual(plan["board_layout"], "archive_columns_v2")
                return {"topics": 1, "added_events": 2}

            with patch.object(semantic.codex, "run_structured", side_effect=article_call) as article_model, \
                    patch.object(knowledge, "run", side_effect=summarize) as knowledge_agent:
                with workflow.watcher_lock(vault):
                    (inbox / "甲.md").write_bytes(first)
                    (inbox / "乙.md").write_bytes(second)
                    (inbox / "同一文件副本.md").write_bytes(second)
                    (inbox / "未授权.md").write_text("未获准的私人材料。")
                    state = workflow.cycle(vault, inbox, policy, settle_seconds=0, preserve_original=True)
                self.assertEqual(state["collected"], 3)
                self.assertEqual([row["error"] for row in state["articles"]].count("local_only"), 1)
                self.assertEqual([row["phase"] for row in state["articles"]].count("ready"), 2)
                self.assertIsNone(state["cross_article_review"])
                self.assertEqual(state["knowledge_summary"]["state"], "ready")
                self.assertEqual(state["knowledge_summary"]["result"]["added_events"], 2)
                self.assertEqual(questions.catalog(vault), [])
                self.assertEqual(len(list(vault.rglob("文章白板.canvas"))), 2)
                self.assertEqual(list(vault.rglob("原文结构.canvas")), [])
                for row in state["articles"]:
                    if row["phase"] == "ready":
                        reading = (vault / row["folder"] / "完整阅读.md").read_text()
                        self.assertTrue(reading.startswith(CHAPTER_READING_MARKER))
                        self.assertIn("\n## 样本\n", reading)
                        self.assertNotIn("### 原文依据", reading)
                        self.assertEqual(len(questions.read_topic(vault, row["sha256"], 1)["citations"]), 1)
                        canvas = json.loads((vault / row["folder"] / "文章白板.canvas").read_text())
                        topic, = (node for node in canvas["nodes"] if node["id"] == "t1")
                        self.assertRegex(topic["text"], r"(July|August) review|July applied")
                self.assertEqual(workflow.cycle(vault, inbox, policy, settle_seconds=0)["collected"], 0)
                self.assertEqual(article_model.call_count, 4)
                knowledge_agent.assert_called_once()

    def test_automatic_policy_protects_baseline_and_reloads_permission(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inbox, vault, older = (root / name for name in ("inbox", "vault", "older"))
            for folder in (inbox, vault, older):
                folder.mkdir()
            old = ["old inbox", "old manifest", "old snapshot", "old directory", "old indexed"]
            hashes = [hashlib.sha256(value.encode()).hexdigest() for value in old]
            (inbox / "old.md").write_text(old[0])
            (vault / "original.md").write_text(old[1])
            (vault / "资料导入清单.json").write_text(json.dumps([{"originals": ["original.md"]}]))
            legacy_db = root / "legacy.sqlite"
            with sqlite3.connect(legacy_db) as db:
                db.execute("CREATE TABLE evidence_snapshots (id TEXT, source_id TEXT, artifact_manifest_json TEXT)")
                db.execute("CREATE TABLE document_representations (snapshot_id TEXT, source_id TEXT, "
                           "payload_json TEXT, created_at TEXT)")
                db.execute("INSERT INTO evidence_snapshots VALUES ('legacy', 'source', ?)",
                           (json.dumps([{"object_sha256": hashes[2]}]),))
            (older / "old.md").write_text(old[3])
            with workflow.database(vault) as db:
                db.execute("INSERT INTO workflow (sha256,folder,admitted,phase) VALUES (?, '',0,'ready')",
                           (hashes[4],))

            policy = root / "approvals.json"
            policy.touch(mode=0o600)
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", allow_new_articles=True, sha256=hashes)))
            args = [sys.executable, "-m", "afr_agent"]
            paths = ["--vault", str(vault), "--inbox", str(inbox)]
            watch = ["watch", *paths, "--new-articles", "--approvals", str(policy), "--once"]
            before = subprocess.run(args + watch, capture_output=True, check=False)
            self.assertEqual(before.returncode, 0)
            self.assertEqual(json.loads(before.stdout)["warning"],
                             {"code": "baseline_required", "mode": "local_intake_only"})
            seed = ["seed-baseline", *paths, "--legacy-db", str(legacy_db), "--legacy-root", str(older)]
            seeded = subprocess.run(args + seed, capture_output=True, check=True)
            self.assertEqual(json.loads(seeded.stdout)["historical_versions"], 5)
            duplicate = subprocess.run(args + seed, capture_output=True, check=False)
            self.assertEqual(json.loads(duplicate.stdout)["error"], "baseline_exists")
            self.assertEqual(semantic.approved_versions(policy), set(hashes))

            for index, value in enumerate(old):
                (inbox / f"copy-{index}.md").write_text(value)
            new = ["new article first", "new article second"]
            new_hashes = {hashlib.sha256(value.encode()).hexdigest() for value in new}
            for index, value in enumerate(new):
                (inbox / f"new-{index}.md").write_text(value)

            def process_article(vault, sha, approvals, *, preserve_original):
                self.assertIn(sha, new_hashes)
                self.assertTrue(semantic.allowed(sha, approvals))
                self.assertTrue(all(not semantic.allowed(old, approvals) for old in hashes))
                return {"state": "published", "topics": 1}

            def knowledge_scope(vault, versions, approvals, *, new_version_ids):
                self.assertEqual(set(versions), new_hashes)
                self.assertEqual(semantic.approved_versions(approvals), new_hashes)
                policy.chmod(0o644)
                self.assertFalse(semantic.approved_versions(approvals))
                return {"clusters": 0}

            with patch.object(semantic, "process", side_effect=process_article) as process, \
                    patch.object(knowledge, "run", side_effect=knowledge_scope) as cross:
                policy.chmod(0o644)
                paused = workflow.cycle(vault, inbox, policy, settle_seconds=0, new_articles=True, legacy_db=legacy_db)
                self.assertTrue(all(row["error"] == "local_only"
                                    for row in paused["articles"] if row["phase"] != "ready"))
                process.assert_not_called()
                cross.assert_not_called()
                policy.chmod(0o600)
                workflow.cycle(vault, inbox, policy, settle_seconds=0, new_articles=True, legacy_db=legacy_db)
                self.assertEqual(process.call_count, 2)
                cross.assert_called_once()

    def test_device_rebind_recovers_local_intake_without_authorizing_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inbox, vault = root / "inbox", root / "vault"
            inbox.mkdir()
            vault.mkdir()
            (inbox / "旧研报.md").write_text("Historical article")
            workflow.seed_baseline(vault, inbox)
            path = vault / ".afr" / "automatic-baseline.json"
            baseline = json.loads(path.read_text())
            device = baseline["inbox"][1]
            old_device = device + 1
            baseline["inbox"][1] = old_device
            path.write_text(json.dumps(baseline))
            (inbox / "新研报.md").write_text("New report")
            (inbox / "暂存研报.md").write_text("Withdrawn report")
            new_sha = hashlib.sha256(b"New report").hexdigest()
            policy = root / "approval.json"
            policy.touch(mode=0o600)
            policy.write_text(json.dumps({"destination": "OpenAI Codex", "allow_new_articles": True,
                                          "sha256": [*baseline["sha256"], semantic.DEMO_SHA256]}))
            with patch.object(workflow, "process_article", return_value=1) as process, \
                    patch.object(workflow, "supplement_knowledge") as cross:
                state = workflow.cycle(vault, inbox, policy, settle_seconds=0, new_articles=True)
                self.assertEqual(state["warning"], {"code": "invalid_baseline", "mode": "local_intake_only"})
                self.assertEqual(state["collected"], 3)
                self.assertEqual(state["articles"], [])
                process.assert_not_called()
                cross.assert_not_called()
                (inbox / "暂存研报.md").unlink()

                args = [sys.executable, "-m", "afr_agent", "rebind-baseline", "--vault", str(vault),
                        "--inbox", str(inbox), "--new-device", str(device)]
                wrong = subprocess.run(args + ["--old-device", str(old_device + 1)],
                                       capture_output=True, check=False)
                self.assertEqual(json.loads(wrong.stdout)["error"], "invalid_baseline")
                self.assertEqual(json.loads(path.read_text()), baseline)
                rebound = subprocess.run(args + ["--old-device", str(old_device)], capture_output=True, check=True)
                self.assertTrue(json.loads(rebound.stdout)["rebound"])
                updated = json.loads(path.read_text())
                self.assertEqual(updated, {**baseline, "inbox": workflow.inbox_identity(inbox)})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                again = subprocess.run(args + ["--old-device", str(old_device)], capture_output=True, check=True)
                self.assertFalse(json.loads(again.stdout)["rebound"])

                state = workflow.cycle(vault, inbox, policy, settle_seconds=0, new_articles=True)
                self.assertNotIn("warning", state)
                self.assertEqual(state["collected"], 0)
                process.assert_called_once()
                self.assertEqual(process.call_args.args[1], new_sha)
                self.assertEqual(semantic.approved_versions(process.call_args.args[2]), {new_sha})
                cross.assert_called_once()

    def test_alternate_manual_watch_does_not_admit_automatic_versions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            vault, inbox, alternate = (root / name for name in ("vault", "inbox", "alternate"))
            for folder in (vault, inbox, alternate):
                folder.mkdir()
            policy = root / "approval.json"
            policy.touch(mode=0o600)
            policy.write_text('{"destination":"OpenAI Codex","allow_new_articles":true}')
            workflow.seed_baseline(vault, inbox)
            (alternate / "alternate.md").write_text("Synthetic alternate source.")
            result = workflow.cycle(vault, alternate, settle_seconds=0)
            sha = result["articles"][0]["sha256"]
            self.assertEqual(result["articles"][0]["error"], "local_only")

            with patch.object(workflow, "process_article", return_value=0) as process:
                workflow.cycle(vault, inbox, policy, new_articles=True, settle_seconds=0)
                process.assert_not_called()
                (inbox / "explicitly-dropped.md").write_bytes((alternate / "alternate.md").read_bytes())
                workflow.cycle(vault, inbox, policy, new_articles=True, settle_seconds=0)
                process.assert_called_once()
                self.assertEqual(process.call_args.args[1], sha)

    def test_replaced_root_during_scan_cannot_admit_its_contents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            vault, inbox, alternate, parked = (root / name for name in ("vault", "inbox", "alternate", "parked"))
            for folder in (vault, inbox, alternate):
                folder.mkdir()
            policy = root / "approval.json"
            policy.touch(mode=0o600)
            policy.write_text('{"destination":"OpenAI Codex","allow_new_articles":true}')
            workflow.seed_baseline(vault, inbox)
            (alternate / "export_1.md").write_text("# Synthetic report\n\nSynthetic evidence.")
            scan = workflow.scan

            def swap_for_scan(*args, **kwargs):
                inbox.rename(parked)
                alternate.rename(inbox)
                try:
                    return scan(*args, **kwargs)
                finally:
                    inbox.rename(alternate)
                    parked.rename(inbox)

            with patch.object(workflow, "process_article", return_value=0) as process:
                with patch.object(workflow, "scan", side_effect=swap_for_scan):
                    state = workflow.cycle(vault, inbox, policy, new_articles=True, settle_seconds=0,
                                           rename_generic=True)
                self.assertEqual(state["warning"], {"code": "inbox_changed", "mode": "local_intake_only"})
                self.assertEqual(state["articles"], [])
                self.assertTrue((alternate / "export_1.md").is_file())
                self.assertEqual(list(vault.rglob("原件.md")), [])
                workflow.cycle(vault, inbox, policy, new_articles=True, settle_seconds=0)
                process.assert_not_called()

    def test_generic_rename_preserves_a_concurrent_producer_replacement(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            vault, inbox = root / "vault", root / "inbox"
            vault.mkdir()
            inbox.mkdir()
            source = inbox / "export_1.md"
            source.write_text("# Synthetic report\n\nFirst version.")
            replacement = inbox / ".replacement"
            replacement.write_text("# Updated synthetic report\n\nSecond version.")
            date_name = intake.source_day(replacement.stat().st_mtime_ns).strftime("%Y年%m月%d日") + ".md"
            rename = intake._rename_exclusive

            def commit_replacement(old, new, fd):
                if old == source.name:
                    replacement.replace(source)
                return rename(old, new, fd)

            with patch.object(intake, "_rename_exclusive", side_effect=commit_replacement):
                first = intake.scan(vault, inbox, rename_generic=True)
            self.assertEqual(first["renamed"], 0)
            self.assertEqual(first["errors"], [{"file": source.name, "error": "file_in_progress"}])
            self.assertIn("Second version", source.read_text())
            self.assertIn("First version", (vault / first["added"][0]["folder"] / "原件.md").read_text())
            second = intake.scan(vault, inbox, rename_generic=True)
            self.assertEqual(second["renamed"], 1)
            self.assertIn("Second version", (inbox / date_name).read_text())

    def test_legacy_domain_navigation_marker_retries_when_index_returns(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            vault, inbox = root / "vault", root / "inbox"
            vault.mkdir()
            inbox.mkdir()
            sha = "a" * 64
            article = f"资料库/科技与产业/机器人产线 · {sha[:16]}"
            folder = vault / article
            folder.mkdir(parents=True)
            for name in ("完整阅读.md", "文章白板.canvas", "固定原文.md"):
                (folder / name).write_text("合成签发材料")
            db = workflow.database(vault)
            with db:
                db.execute("DROP TABLE domain_navigation")
                db.execute("CREATE TABLE domain_navigation (sha256 TEXT PRIMARY KEY)")
                db.execute("INSERT INTO domain_navigation VALUES (?)", (sha,))
                db.execute("INSERT INTO workflow (sha256,folder,admitted,phase) VALUES (?,?,0,'ready')",
                           (sha, article))
            db.close()
            index = folder.parent / "本领域资料索引.md"

            workflow.cycle(vault, inbox, settle_seconds=0)
            self.assertFalse(index.exists())
            index.write_text("# 科技与产业\n")
            workflow.cycle(vault, inbox, settle_seconds=0)
            workflow.cycle(vault, inbox, settle_seconds=0)
            self.assertEqual(index.read_text().count(f"[[{article}/完整阅读.md|"), 1)
            db = workflow.database(vault)
            with db:
                self.assertEqual(db.execute("SELECT sha256 FROM domain_navigation").fetchone()[0], sha)
                self.assertEqual(db.execute("SELECT sha256 FROM domain_navigation_verified").fetchone()[0], sha)
                # 1173c07 的原写法在降级后仍可处理新的签发记录。
                db.execute("INSERT OR IGNORE INTO domain_navigation VALUES (?)", ("b" * 64,))
            db.close()

    def test_ten_articles_share_one_knowledge_batch_and_leave_legacy_state_readable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            vault, policy = root / "vault", root / "policy.json"
            vault.mkdir()
            policy.touch(mode=0o600)
            sources = [f"{n:064x}" for n in range(1, 12)]
            unapproved = "f" * 64
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=sources)))
            with workflow.database(vault) as db:
                db.execute("INSERT INTO cross_article_review(scope,state) VALUES (?,'ready')", ("old",))
                db.executemany("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',?,'ready')",
                               zip([*sources, unapproved], range(12)))
            with workflow.database(vault) as db, patch.object(knowledge, "run", return_value={}) as run:
                workflow.supplement_knowledge(vault, db, policy)
                run.assert_called_once_with(vault, sources[:10], policy, new_version_ids=sources[:10])
                workflow.supplement_knowledge(vault, db, policy)
                self.assertEqual(run.call_args.args[1], sources[10:])
                self.assertEqual(run.call_args.kwargs["new_version_ids"], sources[10:])
                workflow.supplement_knowledge(vault, db, policy)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(db.execute("SELECT scope,state FROM cross_article_review").fetchone()[:],
                                 ("old", "ready"))

    def test_incremental_batch_prefers_signed_related_sources_and_reuses_manifest(self):
        with TemporaryDirectory() as directory:
            vault = Path(directory)
            policy = vault / "policy.json"
            policy.touch(mode=0o600)
            oldest, related, latest, revoked, unsigned, fresh, later = [f"{n:064x}" for n in range(1, 8)]

            def approve(sources):
                policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=sources)))

            def signed_metadata(source, title, branch, summary):
                plan = {"sha256": source, "groups": [dict(title=title, branch=branch, summary=summary)]}
                revision = semantic.fingerprint(plan)
                review = semantic.receipt(vault, source, "review")
                review.parent.mkdir(parents=True, exist_ok=True)
                review.write_text(json.dumps(dict(plan=plan, plan_sha256=revision, version=semantic.REVIEW_VERSION)))
                semantic.receipt(vault, source, "publication").write_text(json.dumps(dict(
                    sha256=source, plan_sha256=revision, state="published")))

            signed_metadata(oldest, "水电防汛", "降雨监测", "流量水位")
            signed_metadata(related, "海运费调查纪要", "集运运价", "航线运力变化")
            signed_metadata(latest, "医疗器械审批", "产品目录", "器械上市")
            signed_metadata(revoked, "红海航线集运运价", "航线运力", "运价下跌")
            signed_metadata(fresh, "红海航线集运运价", "航线运力", "保险和运价影响")
            signed_metadata(later, "红海航线绕行", "集运运价", "航线运力上升")
            approve([oldest, related, latest, revoked, unsigned, fresh, later])
            with workflow.database(vault) as db, patch.object(
                    knowledge, "run", side_effect=[{"topics": 1}, OSError, {"topics": 1}, {"topics": 1}]) as run:
                with db:
                    db.executemany("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',?,'ready')",
                                   zip([oldest, related, latest, revoked, unsigned], range(5)))
                workflow.supplement_knowledge(vault, db, policy)
                approve([oldest, related, latest, unsigned, fresh, later])
                with db:
                    db.execute("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',6,'ready')", (fresh,))
                workflow.supplement_knowledge(vault, db, policy)
                run.assert_called_with(vault, [related, fresh], policy, new_version_ids=[fresh])
                cached = {row[0] for row in db.execute("SELECT sha256 FROM knowledge_article_terms")}
                self.assertEqual(cached, {oldest, related, latest, fresh})
                with db:
                    db.execute("UPDATE knowledge_batches SET next_at=0 WHERE state='pending'")
                with patch.object(workflow, "_signed_knowledge_terms", side_effect=AssertionError("retry reranked")):
                    workflow.supplement_knowledge(vault, db, policy)
                run.assert_called_with(vault, [related, fresh], policy, new_version_ids=[fresh])
                with db:
                    db.execute("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',7,'ready')", (later,))
                with patch.object(semantic, "read_json", wraps=semantic.read_json) as read:
                    workflow.supplement_knowledge(vault, db, policy)
                    read_paths = {call.args[0] for call in read.call_args_list}
                self.assertIn(semantic.receipt(vault, later, "review"), read_paths)
                self.assertNotIn(semantic.receipt(vault, related, "review"), read_paths)
                run.assert_called_with(vault, [related, fresh, later], policy, new_version_ids=[later])

    def test_exhausted_batch_does_not_block_new_sources_and_retries_exact_members(self):
        with TemporaryDirectory() as directory:
            vault = Path(directory)
            policy = vault / "policy.json"
            policy.touch(mode=0o600)
            sources = [f"{n:064x}" for n in range(1, 5)]
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=sources)))
            with workflow.database(vault) as db:
                db.executemany("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',?,'ready')",
                               zip(sources[:2], range(2)))
            with workflow.database(vault) as db, patch.object(knowledge, "run", side_effect=OSError):
                workflow.supplement_knowledge(vault, db, policy)
                scope = db.execute("SELECT scope FROM knowledge_batches").fetchone()["scope"]
                db.execute("UPDATE knowledge_batches SET attempts=3,next_at=0 WHERE scope=?", (scope,))
                db.executemany("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',?,'ready')",
                               zip(sources[2:], range(2, 4)))
            with workflow.database(vault) as db, patch.object(knowledge, "run", return_value={}) as run:
                workflow.supplement_knowledge(vault, db, policy)
                run.assert_called_once_with(vault, sources[2:], policy, new_version_ids=sources[2:])
                workflow.retry(vault, sources[0])
                workflow.supplement_knowledge(vault, db, policy)
                self.assertEqual(run.call_args.args[1], sources[:2])
                self.assertEqual(run.call_args.kwargs["new_version_ids"], sources[:2])

    def test_exhausted_article_review_does_not_replay_or_hold_ready_articles(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inbox, vault, policy = root / "inbox", root / "vault", root / "policy.json"
            inbox.mkdir()
            vault.mkdir()
            sources = [f"{n:064x}" for n in range(1, 4)]
            policy.touch(mode=0o600)
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=sources)))
            with workflow.database(vault) as db:
                db.executemany("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',0,'ready')",
                               [(sha,) for sha in sources[:2]])
                db.execute("INSERT INTO workflow(sha256,folder,admitted) VALUES (?,'',0)", (sources[2],))
            with patch.object(workflow, "process_article", side_effect=workflow.RequestError(
                    "review_requires_human")) as process, patch.object(knowledge, "run", return_value={}) as run:
                workflow.cycle(vault, inbox, policy, settle_seconds=0)
                # 许可临时撤回及恢复不能清除需要显式重试的校读状态。
                snapshot = policy.read_text()
                policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=sources[:2])))
                workflow.cycle(vault, inbox, policy, settle_seconds=0)
                policy.write_text(snapshot)
                state = workflow.cycle(vault, inbox, policy, settle_seconds=0)
                process.assert_called_once()
                run.assert_called_once_with(vault, sources[:2], policy, new_version_ids=sources[:2])
                blocked = next(row for row in state["articles"] if row["sha256"] == sources[2])
                self.assertEqual((blocked["attempts"], blocked["error"]), (1, "review_requires_human"))
                workflow.retry(vault, sources[2])
                workflow.cycle(vault, inbox, policy, settle_seconds=0)
                self.assertEqual(process.call_count, 2)

    def test_unverified_incremental_knowledge_preserves_scope_on_retry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            vault, policy = root / "vault", root / "policy.json"
            vault.mkdir()
            policy.touch(mode=0o600)
            sources = [f"{n:064x}" for n in range(1, 4)]
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=sources)))
            with workflow.database(vault) as db:
                db.executemany("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',0,'ready')",
                               [(sha,) for sha in sources[:2]])
            good = {"topics": 1, "index_path": knowledge.INDEX}
            deferred = {"topics": 0, "deferred_topics": 1, "index_path": knowledge.INDEX}
            with workflow.database(vault) as db, patch.object(knowledge, "run",
                                                              side_effect=[good, deferred, good]) as run:
                workflow.supplement_knowledge(vault, db, policy)
                with db:
                    db.execute("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',1,'ready')", (sources[2],))
                workflow.supplement_knowledge(vault, db, policy)
                self.assertEqual(workflow.status(vault)["knowledge_summary"]["state"], "needs_retry")
                membership = db.execute("SELECT * FROM knowledge_sources ORDER BY sha256").fetchall()
                workflow.retry(vault, sources[2])
                workflow.supplement_knowledge(vault, db, policy)
                self.assertEqual(run.call_args.args[1], sources[2:])
                self.assertEqual(run.call_args.kwargs["new_version_ids"], sources[2:])
                self.assertEqual(db.execute("SELECT * FROM knowledge_sources ORDER BY sha256").fetchall(), membership)
                self.assertEqual(workflow.status(vault)["knowledge_summary"]["state"], "ready")

    def test_empty_knowledge_batch_is_reported_and_can_be_redone_with_new_sources(self):
        with TemporaryDirectory() as directory:
            vault = Path(directory)
            policy = vault / "policy.json"
            policy.touch(mode=0o600)
            sources = [f"{n:064x}" for n in range(1, 4)]
            policy.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=sources)))
            with workflow.database(vault) as db:
                db.executemany("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',0,'ready')",
                               [(sha,) for sha in sources[:2]])
            with workflow.database(vault) as db, patch.object(
                    knowledge, "run", side_effect=[{"topics": 0, "index_path": None}, {"topics": 1}]) as run:
                workflow.supplement_knowledge(vault, db, policy)
                self.assertEqual(workflow.status(vault)["knowledge_summary"]["state"], "no_common_topic")
                workflow.retry(vault, sources[0])
                with db:
                    db.execute("INSERT INTO workflow(sha256,folder,admitted,phase) VALUES (?,'',1,'ready')",
                               (sources[2],))
                workflow.supplement_knowledge(vault, db, policy)
                self.assertEqual(run.call_args.kwargs["new_version_ids"], sources)
                self.assertEqual([x[0] for x in db.execute("SELECT state FROM knowledge_batches ORDER BY rowid")],
                                 ["superseded", "ready"])
