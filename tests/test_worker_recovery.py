import json
from pathlib import Path
import sqlite3
import pytest
import time

from workbench import db, jobs, service


def _isolate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "VAR_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "workbench.sqlite3")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path / "runs")
    service.RUN_ROOT.mkdir(parents=True, exist_ok=True)
    db.init_db()


def test_schema_migrates_job_timestamps_and_editor_provenance_with_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "VAR_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "workbench.sqlite3")
    with sqlite3.connect(db.DB_PATH) as con:
        con.execute(
            """CREATE TABLE jobs(
               id TEXT PRIMARY KEY,status TEXT,created_at TEXT,updated_at TEXT,heartbeat_at TEXT)"""
        )
        con.execute(
            "INSERT INTO jobs VALUES('old-terminal','completed','2026-01-01','2026-01-02',NULL)"
        )
        con.execute("PRAGMA user_version=2")
    db.init_db()
    with db.connect() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
        version = con.execute("PRAGMA user_version").fetchone()[0]
        terminal_at = con.execute(
            "SELECT terminal_at FROM jobs WHERE id='old-terminal'"
        ).fetchone()[0]
    assert version == db.LATEST_SCHEMA_VERSION
    assert {"attempt_started_at", "terminal_at"} <= columns
    assert terminal_at == "2026-01-02"
    assert len(list((tmp_path / "backups").glob("workbench-v2-*.sqlite3"))) == 1


def test_orphan_job_is_retried_once_then_failed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    job_id = db.create_job("preview", "A", {"config": {}})
    first_attempt = db.claim_next_job()
    assert first_attempt["attempt_count"] == 1
    assert first_attempt["attempt_started_at"] is not None
    assert first_attempt["terminal_at"] is None
    assert db.recover_orphan_jobs() == {"requeued": 1, "failed": 0}
    recovered = db.get_job(job_id)
    assert recovered["attempt_started_at"] is None
    assert recovered["queue_position"] == 1
    assert db.claim_next_job()["attempt_count"] == 2
    assert db.recover_orphan_jobs() == {"requeued": 0, "failed": 1}
    terminal = db.get_job(job_id)
    assert terminal["status"] == "failed"
    assert terminal["terminal_at"] is not None
    retried = db.retry_failed_job(job_id)
    assert retried["id"] != job_id
    assert retried["status"] == "queued"
    with pytest.raises(ValueError, match="Only failed"):
        db.retry_failed_job(retried["id"])


def test_interrupted_auto_commit_becomes_failed_once_and_updates_batch(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    batch_id, job_ids = db.create_batch("Recovery", ["A"], {"config": {}})
    job_id = job_ids[0]
    with db.connect() as con:
        con.execute(
            "UPDATE jobs SET status='ready',stage='ready' WHERE id=?", (job_id,)
        )

    assert db.recover_incomplete_auto_commits() == 1
    assert db.get_job(job_id)["status"] == "failed"
    batch = db.get_batch(batch_id)
    assert batch["failed"] == 1
    assert batch["status"] == "completed"
    assert db.recover_incomplete_auto_commits() == 0
    assert db.get_batch(batch_id)["failed"] == 1


def test_cancel_is_safe_for_queued_and_running_jobs(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    queued = db.create_job("preview", "A", {"config": {}})
    queued_result = db.request_cancel(queued)
    assert queued_result["status"] == "canceled"
    assert queued_result["terminal_at"] is not None
    assert db.list_events(queued)[-1]["message"] == "Cancel requested"
    running = db.create_job("preview", "B", {"config": {}})
    db.claim_next_job()
    canceled = db.request_cancel(running)
    assert canceled["status"] == "running"
    assert canceled["cancel_requested"] == 1
    assert db.fail_or_retry(running, "Canceled by user") == "canceled"
    assert db.get_job(running)["terminal_at"] is not None
    assert db.list_events(running)[-1]["level"] == "warning"


def test_queue_position_tracks_single_worker_order(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    first = db.create_job("simulation", "A", {"parent_run_id": "one"})
    second = db.create_job("simulation", "B", {"parent_run_id": "two"})
    assert db.get_job(first)["queue_position"] == 1
    assert db.get_job(second)["queue_position"] == 2
    db.claim_next_job()
    assert db.get_job(second)["queue_position"] == 1


def test_preview_recovery_ledger_verifies_artifacts_and_explains_shared_queue(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    active_id = db.create_job("lhs", "4252702YJ2745A", {"study": "N50"})
    assert db.claim_next_job()["id"] == active_id
    queued_id = db.create_job("preview", "4252702YJ2745A", {
        "config": {"provenance": {"scenario_name": "Queued pilot", "baseline_profile": "pilot"}},
        "geometry_actions": [],
    })
    ready_id = db.create_job("preview", "READY-BUILDING", {
        "config": {"provenance": {"scenario_name": "Ready pilot", "baseline_profile": "pilot"}},
        "geometry_actions": [{"action": "simplify", "approved": True}],
    })
    ready_dir = tmp_path / "previews" / ready_id
    ready_dir.mkdir(parents=True)
    evidence = ready_dir / "config.json"
    evidence.write_text('{"provenance":{"scenario_name":"Ready pilot"}}', encoding="utf-8")
    service.write_json(ready_dir / "manifest.json", {
        "schema_version": 1,
        "job_id": ready_id,
        "refparcela": "READY-BUILDING",
        "artifacts": [{
            "name": evidence.name,
            "sha256": service.sha256_file(evidence),
            "size_bytes": evidence.stat().st_size,
        }],
    })
    db.update_job(ready_id, "ready", result_path=str(ready_dir))

    detail = service.preview_detail(queued_id)
    assert detail["artifact_state"] == {"status": "PENDING", "recoverable": True, "issues": []}
    assert detail["queue_context"]["position"] == 1
    assert detail["queue_context"]["active_job"]["kind"] == "lhs"
    assert detail["queue_context"]["worker_busy"] is True

    ledger = service.recoverable_previews()
    assert ledger["active"]["id"] == queued_id
    ready = next(item for item in ledger["items"] if item["id"] == ready_id)
    assert ready["artifact_state"]["status"] == "AVAILABLE"
    assert ledger["ready_count"] == 1

    evidence.write_text("tampered", encoding="utf-8")
    tampered = service.preview_detail(ready_id)
    assert tampered["artifact_state"]["status"] == "TAMPERED"
    assert tampered["scene"] is None
    with pytest.raises(ValueError, match="Preview artifact integrity failed"):
        service.commit_preview(ready_id)


def test_current_simulation_job_prefers_running_and_supports_parent_filter(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    running_id = db.create_job("simulation", "A", {"parent_run_id": "parent-a"})
    queued_id = db.create_job("simulation", "B", {"parent_run_id": "parent-b"})
    db.create_job("preview", "C", {"config": {}})
    assert db.claim_next_job()["id"] == running_id
    assert db.find_current_simulation_job()["id"] == running_id
    assert db.find_current_simulation_job("parent-b")["id"] == queued_id
    assert db.find_active_simulation_job("parent-a")["id"] == running_id
    assert db.find_current_simulation_job("missing") is None


def test_fail_or_retry_records_reason_and_resets_attempt_clock(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    job_id = db.create_job("simulation", "A", {"parent_run_id": "one"})
    db.claim_next_job()
    assert db.fail_or_retry(job_id, "temporary runtime failure") == "queued"
    job = db.get_job(job_id)
    assert job["attempt_started_at"] is None
    assert job["terminal_at"] is None
    retry_event = db.list_events(job_id)[-1]
    assert retry_event["level"] == "warning"
    assert "temporary runtime failure" in retry_event["message"]


def test_log_chunk_cursor_reads_only_appended_bytes(tmp_path):
    path = tmp_path / "worker.log"
    path.write_text("first\n", encoding="utf-8")
    offset, first = jobs.read_log_chunk(path, 0)
    assert first == "first\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("second\n")
    next_offset, second = jobs.read_log_chunk(path, offset)
    assert second == "second\n"
    assert next_offset > offset
    reset_offset, reset = jobs.read_log_chunk(path, next_offset + 100)
    assert reset == "first\nsecond\n"
    assert reset_offset == next_offset


def test_canceled_batch_is_reconciled_without_double_counting(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    batch_id, job_ids = db.create_batch("Cancel", ["A"], {"config": {}})
    db.request_cancel(job_ids[0])
    batch = db.get_batch(batch_id)
    assert batch["status"] == "completed"
    assert batch["completed"] == 0
    assert batch["failed"] == 1
    updated_at = batch["updated_at"]
    db.reconcile_all_batches()
    reconciled = db.get_batch(batch_id)
    assert reconciled["failed"] == 1
    assert reconciled["updated_at"] == updated_at


def test_interrupted_commit_recovers_from_staging(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    job_id = db.create_job("preview", "A", {"config": {}})
    run_id = "run-recovery"
    staging = service.RUN_ROOT / f".staging-{run_id}"
    staging.mkdir()
    artifact = staging / "stats.json"
    artifact.write_text("{}", encoding="utf-8")
    manifest = staging / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 2, "artifacts": []}), encoding="utf-8")
    artifacts = service.artifact_manifest(staging)
    final = service.RUN_ROOT / run_id
    for item in artifacts:
        item["path"] = str(final / item["name"])
    db.insert_run({
        "id": run_id, "job_id": job_id, "refparcela": "A", "scenario_name": "Recovery",
        "config": {}, "stats": {}, "qa": {}, "artifact_dir": str(staging),
        "verification_status": "COMMITTING", "manifest_sha256": service.sha256_file(manifest),
    }, artifacts)
    service.recover_committing_runs()
    recovered = db.get_run(run_id)
    assert recovered["verification_status"] == "VERIFIED"
    assert Path(recovered["artifact_dir"]) == final
    assert final.exists() and not staging.exists()


def test_run_without_integrity_metadata_is_preserved_as_legacy(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    job_id = db.create_job("preview", "A", {"config": {}})
    with db.connect() as con:
        con.execute(
            """INSERT INTO runs(id,job_id,refparcela,scenario_name,config_json,stats_json,
               qa_json,artifact_dir,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("legacy", job_id, "A", "Historical", "{}", "{}", "{}", str(tmp_path), db.utcnow()),
        )
    assert db.get_run("legacy")["verification_status"] == "LEGACY"
    assert service.verify_run_artifacts("legacy")["status"] == "LEGACY"


def _fake_worker_project(tmp_path: Path, source: str) -> Path:
    project = tmp_path / "fake-project"
    package = project / "src/workbench"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "worker.py").write_text(source, encoding="utf-8")
    return project


def _wait_for_status(job_id: str, status: str, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = db.get_job(job_id)
        if job and job["status"] == status:
            return job
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not reach {status}: {db.get_job(job_id)}")


def test_manager_drains_large_child_output_without_pipe_deadlock(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    project = _fake_worker_project(tmp_path, "import sys\nprint('x' * 200000)\nsys.exit(1)\n")
    monkeypatch.setattr(jobs, "PROJECT", project)
    job_id = db.create_job("preview", "A", {"config": {}}, timeout_seconds=5)
    manager = jobs.JobManager()
    manager.start()
    manager.notify()
    failed = _wait_for_status(job_id, "failed")
    manager.stop()
    stdout_path, _ = jobs.log_paths(job_id)
    assert failed["attempt_count"] == 2
    assert stdout_path.stat().st_size > 200000


def test_manager_timeout_terminates_and_retries_once(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    project = _fake_worker_project(tmp_path, "import time\ntime.sleep(10)\n")
    monkeypatch.setattr(jobs, "PROJECT", project)
    job_id = db.create_job("preview", "A", {"config": {}}, timeout_seconds=1)
    manager = jobs.JobManager()
    manager.start()
    manager.notify()
    failed = _wait_for_status(job_id, "failed", timeout=6)
    manager.stop()
    assert failed["attempt_count"] == 2
    assert "timeout" in (failed["error"] or "").lower()
