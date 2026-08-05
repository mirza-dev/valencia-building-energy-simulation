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
import hashlib
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import geopandas as gpd

import stock_input_policy as sip
import stock_runner as sr
import verified_model as vm
from workbench import db, file_inputs, integrity

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
_EXPORT_LOCK = threading.Lock()


@dataclass(frozen=True)
class InputSet:
    """The four files a stock run reads.  Resolved once, reported as one thing."""

    gis: Path | None
    tipo15: Path | None
    climate: Path | None = None
    template: Path | None = None

    def missing(self) -> list[str]:
        absent = []
        for label, path in (
            ("gis", self.gis), ("tipo15", self.tipo15),
            ("climate", self.climate), ("template", self.template),
        ):
            if path is None or not Path(path).exists():
                absent.append(label)
        return absent


@lru_cache(maxsize=8)
def _managed_climate(epw: str, ddy: str, climate_root: str) -> Path:
    """Build once per immutable input pair; managed uploads are content-addressed."""
    path, _ = file_inputs.build_climate_bundle(
        Path(epw), Path(ddy), Path(climate_root))
    return path


def default_inputs() -> InputSet:
    """Resolve the six active dataset records into the runner's four inputs."""
    import model_builder as mb
    root = Path(mb._project_root())
    try:
        settings = db.project_settings()

        def selected(field: str) -> Path | None:
            dataset_id = settings.get(field)
            item = db.get_dataset(str(dataset_id)) if dataset_id else None
            return Path(item["path"]) if item else None

        gis = selected("building_dataset_id")
        tipo15 = selected("tipo15_dataset_id")
        template = selected("template_dataset_id")
        epw = selected("weather_dataset_id")
        ddy = selected("ddy_dataset_id")
        climate_path = None
        if epw is not None and ddy is not None and epw.exists() and ddy.exists():
            var_root = Path(os.environ.get("WORKBENCH_VAR_DIR", PROJECT / "var"))
            climate_path = _managed_climate(
                str(epw.resolve()), str(ddy.resolve()), str((var_root / "climates").resolve()))
        return InputSet(gis=gis, tipo15=tipo15, climate=climate_path, template=template)
    except (RuntimeError, OSError, sqlite3.Error):
        # Direct CLI/import use before Workbench bootstrap keeps the historical
        # project defaults.  The UI path always has a database and never guesses.
        return InputSet(
            gis=root / "data/gis/DatosRai_ciudadValencia.shp",
            tipo15=root / "data/reference/Tipo15_soloV(in).csv",
            climate=root / "climates/valencia_iwec.json",
            template=root / "data/templates/PlantillaOS_v2.osm",
        )


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
    if inputs.climate is not None:
        argv += ["--climate", str(inputs.climate)]
    if inputs.template is not None:
        argv += ["--template", str(inputs.template)]
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


def log_tail(name: str, max_bytes: int = 160_000) -> str:
    """Return a bounded UTF-8 tail; the process marker is the only log authority."""
    directory = run_directory(name)
    marker = directory / "run_process.json"
    if not marker.exists():
        return ""
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
        path = Path(str(record["log"])).resolve()
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid process record for {name}") from exc
    allowed_root = Path(os.environ.get("WORKBENCH_VAR_DIR", PROJECT / "var")).resolve()
    try:
        path.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("stock log escaped the Workbench var root") from exc
    if not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - max_bytes))
        return handle.read(max_bytes).decode("utf-8", errors="replace")


def ledger_page(name: str, *, query: str = "", status: str = "",
                offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """A bounded view over the durable ledger for the Outputs table."""
    rows = ledger_rows(name)
    needle = query.strip().casefold()
    wanted_status = status.strip().casefold()
    filtered = []
    for row in rows:
        if wanted_status and str(row.get("status", "")).casefold() != wanted_status:
            continue
        if needle:
            haystack = " ".join(str(row.get(key, "")) for key in (
                "refparcela", "cluster", "status", "error", "occupancy_plausibility",
            )).casefold()
            if needle not in haystack:
                continue
        filtered.append(row)
    return {
        "run": name,
        "total": len(filtered),
        "offset": offset,
        "limit": limit,
        "items": filtered[offset:offset + limit],
    }


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


# ---------------------------------------------------------------------------
# Signed packages - generated on demand, never folded back into the run
# ---------------------------------------------------------------------------
def _package_sources(name: str, references: list[str] | None = None) -> tuple[list[tuple[str, Path]], bytes | None]:
    out_dir = run_directory(name)
    if not (out_dir / "ledger.jsonl").is_file():
        raise FileNotFoundError(name)

    selected_ledger: bytes | None = None
    candidates: list[Path] = []
    if references is None:
        candidates = [
            path for path in out_dir.rglob("*")
            if path.is_file() and not path.is_symlink() and path.name != "run_process.json"
        ]
    else:
        requested = list(dict.fromkeys(str(item).strip() for item in references if str(item).strip()))
        if not requested:
            raise ValueError("at least one refparcela is required")
        rows = {str(row.get("refparcela")): row for row in ledger_rows(name)}
        missing = [reference for reference in requested if reference not in rows]
        if missing:
            raise ValueError(f"unknown refparcela: {', '.join(missing[:8])}")
        for reference in requested:
            if reference != Path(reference).name or any(sep in reference for sep in ("/", "\\", "\x00")):
                raise ValueError(f"invalid refparcela: {reference!r}")
            row = rows[reference]
            model_value = row.get("model_osm")
            if model_value:
                model = (PROJECT / str(model_value)).resolve()
                if model.is_file():
                    candidates.append(model)
            deep = out_dir / "runs" / f"{reference}_deep"
            if deep.is_dir():
                candidates.extend(path for path in deep.rglob("*") if path.is_file() and not path.is_symlink())
        for common in ("aggregate.json", "run_config.json"):
            path = out_dir / common
            if path.is_file():
                candidates.append(path)
        selected_ledger = integrity.canonical_json_bytes({
            "schema_version": 1, "run": name,
            "rows": [rows[reference] for reference in requested],
        })

    sources: dict[str, Path] = {}
    for path in candidates:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(out_dir)
        except ValueError as exc:
            raise ValueError(f"package source escaped run directory: {path}") from exc
        sources[relative.as_posix()] = resolved
    return sorted(sources.items()), selected_ledger


def package_plan(name: str, references: list[str] | None = None) -> dict[str, Any]:
    sources, selected_ledger = _package_sources(name, references)
    size = sum(path.stat().st_size for _, path in sources) + len(selected_ledger or b"")
    return {
        "run": name,
        "scope": "full" if references is None else "selection",
        "references": None if references is None else len(set(references)),
        "files": len(sources) + (1 if selected_ledger is not None else 0),
        "uncompressed_bytes": size,
        "signed": True,
    }


def export_package(name: str, references: list[str] | None = None) -> Path:
    """Create one Ed25519-signed ZIP while hashing each source in a single pass."""
    sources, selected_ledger = _package_sources(name, references)
    export_root = Path(os.environ.get("WORKBENCH_EXPORT_ROOT", PROJECT / "var/exports"))
    export_root.mkdir(parents=True, exist_ok=True)
    if references is None:
        suffix = "full"
    else:
        identity = integrity.canonical_json_bytes(sorted(set(references)))
        suffix = f"selected-{hashlib.sha256(identity).hexdigest()[:12]}"
    output = export_root / f"stock_{name}_{suffix}.zip"
    temporary = export_root / f".{output.name}.{uuid.uuid4().hex}.tmp"

    with _EXPORT_LOCK:
        try:
            package_files: list[dict[str, Any]] = []
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                                 allowZip64=True) as archive:
                for arcname, source in sources:
                    digest = hashlib.sha256()
                    size = 0
                    with source.open("rb") as reader, archive.open(arcname, "w", force_zip64=True) as writer:
                        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                            digest.update(chunk)
                            size += len(chunk)
                            writer.write(chunk)
                    package_files.append({"path": arcname, "sha256": digest.hexdigest(), "size_bytes": size})
                if selected_ledger is not None:
                    archive.writestr("selected_ledger.json", selected_ledger)
                    package_files.append({
                        "path": "selected_ledger.json",
                        "sha256": hashlib.sha256(selected_ledger).hexdigest(),
                        "size_bytes": len(selected_ledger),
                    })
                manifest = {
                    "schema_version": 1,
                    "run": name,
                    "scope": "full" if references is None else "selection",
                    "references": None if references is None else sorted(set(references)),
                    "created_at": integrity.utcnow(),
                    "files": package_files,
                }
                signature = integrity.sign_manifest(manifest)
                archive.writestr("export_manifest.json", integrity.canonical_json_bytes(manifest))
                archive.writestr("export_manifest.sig.json", integrity.canonical_json_bytes(signature))
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    return output
