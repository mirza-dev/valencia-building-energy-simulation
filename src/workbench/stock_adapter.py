"""Thin adapter between the Workbench UI and the per-building stock runner.

Orchestration only.  Every count, every exclusion reason and every number comes
from `stock_runner` itself - this module never re-derives a threshold, a scope
rule or an energy figure.  When something here needs to know whether a building
can run, it calls `stock_runner.screen_geometry`; the day the engine's gate
changes, this file needs no edit and cannot disagree with it.

The run itself is a **subprocess**, not an in-process call.  A full-city run is
20+ hours and already spawns its own six worker processes; nesting that inside
the API's worker thread would make cancellation unreliable and let a crash take
the interface down with it.  As a subprocess:

  * stop = terminate the process, and `stock_runner`'s ledger is flushed and
    fsynced per building, so resume picks up exactly where it stopped
  * a crash is isolated and visible in the log file
  * progress is read from the ledger, which is the same durable record the
    resume logic trusts
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd

import stock_input_policy as sip
import stock_runner as sr
import verified_model as vm

PROJECT = Path(__file__).resolve().parents[2]
RUNNER_PATH = PROJECT / "src/stock_runner.py"
STOCK_ROOT = PROJECT / "out/stock"

# Entry points this adapter depends on.  Listed so a capability check can prove
# the engine still offers them rather than discovering it mid-run.
REQUIRED_ENTRYPOINTS = (
    "run_stock", "prepare_stock_file", "screen_geometry", "select_scope",
    "aggregate", "read_ledger", "latest_per_reference",
)

# Only these may ever be served out of a run directory.  The list mirrors what
# `stock_runner.KEPT_ARTIFACTS` preserves; anything else in there is an
# EnergyPlus intermediate nobody should be handed by filename.
SERVABLE_ARTIFACTS = {
    "eplustbl.htm": "text/html",
    "eplusout.err": "text/plain",
    "model_python.osm": "text/plain",
    "deep_layers.json": "application/json",
    "verified_profile.json": "application/json",
    "qa_report.txt": "text/plain",
}

# Measured on Benicalap v6: 967 buildings, 6 workers, median 40.6 s each.
SECONDS_PER_BUILDING = 40.6
BYTES_PER_BUILDING_FULL = 5.8 * 1024 ** 2
BYTES_PER_BUILDING_SUMMARY = 1.05 * 1024 ** 2


@dataclass(frozen=True)
class InputSet:
    """The four files a stock run reads.  Resolved once, reported as one thing."""

    gis: Path
    tipo15: Path
    climate: Path | None = None
    template: Path | None = None

    def missing(self) -> list[str]:
        absent = []
        for label, path in (("gis", self.gis), ("tipo15", self.tipo15)):
            if path is None or not Path(path).exists():
                absent.append(label)
        for label, path in (("climate", self.climate), ("template", self.template)):
            if path is not None and not Path(path).exists():
                absent.append(label)
        return absent


def default_inputs() -> InputSet:
    """The files the CLI defaults to, so the UI opens on a working configuration."""
    import model_builder as mb
    root = Path(mb._project_root())
    return InputSet(gis=root / "data/gis/DatosRai_ciudadValencia.shp",
                    tipo15=root / "data/reference/Tipo15_soloV(in).csv")


def entrypoints_present() -> dict[str, bool]:
    return {name: callable(getattr(sr, name, None)) for name in REQUIRED_ENTRYPOINTS}


def profile() -> dict[str, Any]:
    """Which verified model the interface is about to run, stated up front."""
    record = vm.profile_record()
    return {"fingerprint": record.get("fingerprint"),
            "source_hashes": record.get("source_hashes", {})}


# ---------------------------------------------------------------------------
# Preflight - what a run would do, before it does any of it
# ---------------------------------------------------------------------------
def district_options(inputs: InputSet | None = None) -> list[str]:
    inputs = inputs or default_inputs()
    stock = gpd.read_file(inputs.gis)
    return sorted(stock["nombre"].dropna().astype(str).unique().tolist())


def preflight(scope: str, *, district: str | None = None,
              references: list[str] | None = None,
              inputs: InputSet | None = None,
              policy: sip.StockInputPolicy | None = None,
              keep: str = "full",
              workers: int = 6,
              var_dir: Path | None = None) -> dict[str, Any]:
    """How many buildings would run, how many would not and why, and what it costs.

    Uses the engine's own scope selection and geometry screen, so the numbers a
    person approves are the numbers the run will produce.
    """
    inputs = inputs or default_inputs()
    absent = inputs.missing()
    if absent:
        return {"ok": False, "missing_inputs": absent}

    policy = policy or sip.StockInputPolicy()
    var_dir = Path(var_dir or (PROJECT / "var"))
    _, stock, counters = sr.prepare_stock_file(
        Path(inputs.gis), Path(inputs.tipo15), policy, var_dir)

    scoped = sr.select_scope(stock, scope, district=district, references=references)
    runnable, exclusions = sr.screen_geometry(scoped)

    reasons: dict[str, int] = {}
    for item in exclusions:
        reason = str(item.get("reason", "unknown")).split(":")[0]
        reasons[reason] = reasons.get(reason, 0) + 1

    per_building = (BYTES_PER_BUILDING_FULL if keep == "full"
                    else BYTES_PER_BUILDING_SUMMARY)
    seconds = len(runnable) * SECONDS_PER_BUILDING / max(1, workers)
    return {
        "ok": True,
        "scope": scope,
        "district": district,
        "buildings_in_scope": int(len(scoped)),
        "runnable": len(runnable),
        "excluded": len(exclusions),
        "exclusion_reasons": reasons,
        "estimated_minutes": round(seconds / 60.0, 1),
        "estimated_bytes": int(len(runnable) * per_building),
        "policy_fingerprint": counters.get("policy_fingerprint"),
        "stock_source_fingerprint": counters.get("stock_source_fingerprint"),
        "profile": profile(),
    }


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def run_directory(name: str) -> Path:
    """A run name may never escape the stock root.

    Anything that looks like a path is refused outright rather than reduced to
    its last component: quietly turning "../../etc" into "etc" cannot escape,
    but it does hand back a directory nobody asked for.
    """
    text = str(name)
    if not text or text.startswith(".") or text != Path(text).name:
        raise ValueError(f"invalid run name: {name!r}")
    if any(sep in text for sep in ("/", "\\", "\x00")):
        raise ValueError(f"invalid run name: {name!r}")
    return (STOCK_ROOT / text).resolve()


def start_run(name: str, scope: str, *, district: str | None = None,
              references: list[str] | None = None,
              inputs: InputSet | None = None,
              workers: int = 6, keep: str = "full",
              resume: bool = False,
              log_dir: Path | None = None) -> dict[str, Any]:
    """Launch the runner as a subprocess and return what is needed to follow it."""
    inputs = inputs or default_inputs()
    out_dir = run_directory(name)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(log_dir or (PROJECT / "var"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"stock_{out_dir.name}.log"

    argv = [sys.executable, str(RUNNER_PATH),
            "--scope", scope,
            "--out-dir", str(out_dir),
            "--workers", str(workers),
            "--keep", keep,
            "--gis", str(inputs.gis),
            "--tipo15", str(inputs.tipo15)]
    if district:
        argv += ["--district", district]
    if references:
        argv += ["--references", *references]
    if resume:
        argv.append("--resume")

    handle = log_path.open("ab")
    # Own process group so a stop signal reaches the worker pool too, not just
    # the parent that spawned it.
    popen_kwargs: dict[str, Any] = {"stdout": handle, "stderr": subprocess.STDOUT,
                                    "cwd": str(PROJECT)}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:                                    # Windows has no process groups here
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(argv, **popen_kwargs)
    record = {"run": out_dir.name, "pid": process.pid, "log": str(log_path),
              "argv": argv, "started_at": time.time()}
    # On disk, not in memory: a 20-hour run must still be stoppable after the
    # interface itself has been restarted.
    (out_dir / "run_process.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8")
    return record


def active_process(name: str) -> dict[str, Any] | None:
    """The run's process record, if it is still alive."""
    marker = run_directory(name) / "run_process.json"
    if not marker.exists():
        return None
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or not is_running(pid):
        return None
    return record


def stop_run(pid: int) -> bool:
    """Stop a run.  The ledger is durable, so this is always resumable."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            os.kill(pid, signal.CTRL_BREAK_EVENT)   # type: ignore[attr-defined]
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Reading results
# ---------------------------------------------------------------------------
def progress(name: str) -> dict[str, Any]:
    """Live counts, read from the same ledger the resume logic trusts."""
    out_dir = run_directory(name)
    ledger = out_dir / "ledger.jsonl"
    if not ledger.exists():
        return {"run": out_dir.name, "started": False}
    counts: dict[str, int] = {}
    seconds = 0.0
    for row in sr.read_ledger(ledger):
        status = str(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
        seconds += float(row.get("seconds") or 0.0)
    done = counts.get("ok", 0) + counts.get("failed", 0)
    return {"run": out_dir.name, "started": True, "counts": counts,
            "completed": done, "cpu_seconds": round(seconds, 1)}


def list_runs() -> list[dict[str, Any]]:
    if not STOCK_ROOT.exists():
        return []
    runs = []
    # newest first: a person opens this to find what they just ran, not to read
    # an alphabet
    candidates = sorted(STOCK_ROOT.iterdir(),
                        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
                        reverse=True)
    for path in candidates:
        if not path.is_dir() or not (path / "ledger.jsonl").exists():
            continue
        summary = path / "aggregate.json"
        runs.append({
            "run": path.name,
            "modified": path.stat().st_mtime,
            "has_summary": summary.exists(),
            "buildings": progress(path.name).get("counts", {}).get("ok", 0),
        })
    return runs


def summary(name: str) -> dict[str, Any]:
    """The run's own aggregate, recomputed if it was never written."""
    out_dir = run_directory(name)
    cached = out_dir / "aggregate.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    rows = sr.read_ledger(out_dir / "ledger.jsonl")
    return sr.aggregate(rows)


def ledger_rows(name: str) -> list[dict[str, Any]]:
    out_dir = run_directory(name)
    return sr.latest_per_reference(sr.read_ledger(out_dir / "ledger.jsonl"))


def artifact_path(name: str, refparcela: str, filename: str) -> Path:
    """Resolve one evidence file for one building, or refuse.

    Three gates, all of them necessary: the filename must be on the allowlist,
    the reference must be a bare name, and the resolved path must still be
    inside the run directory after symlinks are followed.
    """
    if filename not in SERVABLE_ARTIFACTS:
        raise ValueError(f"not a servable artifact: {filename!r}")
    reference = Path(str(refparcela)).name
    if not reference or reference.startswith("."):
        raise ValueError(f"invalid refparcela: {refparcela!r}")
    out_dir = run_directory(name)
    candidate = (out_dir / "runs" / f"{reference}_deep" / filename).resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"{filename} not found for {reference}")
    if out_dir not in candidate.parents:
        raise ValueError("resolved outside the run directory")
    return candidate
