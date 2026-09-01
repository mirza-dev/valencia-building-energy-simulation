"""Single-file local queue; every OpenStudio job runs in an isolated process."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from workbench import db, storage


PROJECT = Path(__file__).resolve().parents[2]
def log_paths(job_id: str) -> tuple[Path, Path]:
    root = db.VAR_DIR / "job_logs"
    return root / f"{job_id}.stdout.log", root / f"{job_id}.stderr.log"


def read_log_chunk(path: Path, offset: int, limit_bytes: int = 16384) -> tuple[int, str]:
    """Read an append-only log from a byte cursor without duplicating content."""
    if not path.exists():
        return 0 if offset else offset, ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        safe_offset = 0 if offset < 0 or offset > size else offset
        handle.seek(safe_offset)
        payload = handle.read(max(1, limit_bytes))
        next_offset = handle.tell()
    return next_offset, payload.decode("utf-8", errors="replace")


def _tail(path: Path, limit: int = 4000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit * 2))
        return handle.read().decode("utf-8", errors="replace")[-limit:].strip()


class JobManager:
    def __init__(self) -> None:
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active: subprocess.Popen[str] | None = None
        self._active_job_id: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # `stop()` sets this and nothing used to clear it, so a manager that had
        # been stopped once started a thread that immediately fell out of its
        # own loop: `status()` then reported `running: false` for the life of
        # the process and `/api/health` stayed BLOCKED with every input valid.
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="workbench-queue", daemon=True)
        self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)

    def status(self) -> dict[str, object]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "active_job_id": self._active_job_id,
            "active_pid": self._active.pid if self._active else None,
        }

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = db.claim_next_job()
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            try:
                storage.verify_worker_capacity(job)
            except storage.StorageAdmissionError as exc:
                db.add_event(job["id"], str(exc), 0.0, "error")
                db.update_job(job["id"], "failed", error=str(exc))
                time.sleep(0.05)
                continue
            env = os.environ.copy()
            src = str(PROJECT / "src")
            env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
            env.setdefault("MPLCONFIGDIR", str(db.VAR_DIR / "matplotlib"))
            started = time.monotonic()
            stdout_path, stderr_path = log_paths(job["id"])
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            with stdout_path.open("a", encoding="utf-8") as stdout_handle, \
                    stderr_path.open("a", encoding="utf-8") as stderr_handle:
                stdout_handle.write(f"\n--- attempt {job['attempt_count']} ---\n")
                stderr_handle.write(f"\n--- attempt {job['attempt_count']} ---\n")
                stdout_handle.flush()
                stderr_handle.flush()
                process = subprocess.Popen(
                    [sys.executable, "-m", "workbench.worker", job["id"]],
                    cwd=PROJECT,
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    start_new_session=True,
                )
                self._active = process
                self._active_job_id = job["id"]
                forced_error: str | None = None
                while process.poll() is None:
                    latest = db.get_job(job["id"])
                    if self._stop.is_set():
                        forced_error = "Worker manager stopped"
                        self._terminate(process)
                        break
                    if latest and latest.get("cancel_requested"):
                        forced_error = "Canceled by user"
                        self._terminate(process)
                        break
                    if time.monotonic() - started > int(job.get("timeout_seconds", 180)):
                        forced_error = f"Job exceeded timeout of {job.get('timeout_seconds', 180)} seconds"
                        self._terminate(process)
                        break
                    db.heartbeat_job(job["id"])
                    time.sleep(0.5)
                process.wait()
            latest = db.get_job(job["id"])
            incomplete = latest and (
                latest["status"] == "running"
                or bool(job.get("auto_commit") and latest["status"] != "completed")
            )
            failed_process = process.returncode != 0 or forced_error is not None
            terminal = latest and latest["status"] in {"completed", "failed", "canceled"}
            if latest and not terminal and (failed_process or incomplete):
                message = forced_error or _tail(stderr_path) or _tail(stdout_path) or \
                    "Worker exited before reaching its required terminal state"
                db.fail_or_retry(job["id"], message[-4000:])
                self._wake.set()
            self._active = None
            self._active_job_id = None
            time.sleep(0.05)


manager = JobManager()
