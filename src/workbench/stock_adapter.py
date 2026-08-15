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

import results_layer as rl
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

# Fallback only, for an installation that has never finished a run: the real
# figure is read from the runs on disk (`_seconds_per_building`).
#
# It is a MEAN, not a median.  The workers pull from one queue, so the wall
# clock is (buildings x mean) / workers; the per-building times are heavily
# right-skewed - Benicalap v8 had a median of 40.2 s against a mean of 60.1 s
# and a slowest building of 1130 s - so estimating from the median understates
# a long run by a third.  Checked against v8: 968 x 60.1 / 3 workers = 323 min,
# and the run took 323.47.
SECONDS_PER_BUILDING = 60.1
BYTES_PER_BUILDING_FULL = 5.8 * 1024 ** 2
BYTES_PER_BUILDING_SUMMARY = 1.05 * 1024 ** 2
_EXPORT_LOCK = threading.Lock()


@dataclass(frozen=True)
class InputSet:
    """The files a stock run reads.  Resolved once, reported as one thing.

    Two shapes of building input are supported and they demand different
    companions:

    * `gis` - a raw cadastre, which cannot say how much of a building is
      housing, so a Tipo15 ledger is **required** alongside it and the input
      policy derives the stock from the pair.  This is the Valencia path and it
      behaves exactly as it always has.
    * `stock` - a prepared, self-describing file that already carries the fields
      the engine reads.  There is no companion to require, and the policy step
      is skipped: the file already is the prepared stock.

    Whichever is active, `climate` and `template` are always required.  That is
    not a convenience: with no climate the frozen engine falls back to Valencia's
    design days, barometric pressure, ground temperature and mains temperature
    (`deep_building` lines 919-923), which would put Valencia's site inside
    another city's results without anybody being told.
    """

    gis: Path | None = None
    tipo15: Path | None = None
    climate: Path | None = None
    template: Path | None = None
    stock: Path | None = None
    microclimate: Path | None = None

    @property
    def prepared(self) -> bool:
        """Is the building input already in the shape the engine reads?"""
        return self.stock is not None

    def missing(self) -> list[str]:
        required: list[tuple[str, Path | None]] = [
            ("climate", self.climate), ("template", self.template),
        ]
        if self.prepared:
            required.append(("stock", self.stock))
        else:
            required += [("gis", self.gis), ("tipo15", self.tipo15)]
        return [label for label, path in required
                if path is None or not Path(path).exists()]


@lru_cache(maxsize=8)
def _managed_climate(epw: str, ddy: str, climate_root: str,
                     ground_c: float | None, mains_c: float | None) -> Path:
    """Build once per immutable input pair; managed uploads are content-addressed.

    The two declared site temperatures are part of the cache key because they
    are part of the bundle, and therefore of `climate_fingerprint`: changing a
    declaration must produce a different climate identity, not silently reuse
    the one built from the previous answer.
    """
    path, _ = file_inputs.build_climate_bundle(
        Path(epw), Path(ddy), Path(climate_root),
        ground_temperature_c=ground_c, water_mains_temperature_c=mains_c)
    return path


def default_inputs() -> InputSet:
    """Resolve the active dataset records into the inputs a run reads.

    A prepared stock, when one is active, replaces the cadastre+Tipo15 pair
    rather than joining it: it is already the shape the engine reads.
    """
    import model_builder as mb
    root = Path(mb._project_root())
    try:
        settings = db.project_settings()

        def selected(field: str) -> Path | None:
            dataset_id = settings.get(field)
            item = db.get_dataset(str(dataset_id)) if dataset_id else None
            return Path(item["path"]) if item else None

        def selected_slice() -> Path | None:
            """Hand the runner the directory the loader reads, not the archive.

            A slice registered before uploads unpacked them carries only the
            zip, so the directory is produced on first use here.  The
            fingerprint recorded at acceptance is passed down, which means the
            healed path is checked exactly as the freshly uploaded one.
            """
            dataset_id = settings.get("microclimate_dataset_id")
            item = db.get_dataset(str(dataset_id)) if dataset_id else None
            if item is None:
                return None
            metadata = item.get("metadata") or {}
            stored = metadata.get("slice_dir")
            if stored and Path(str(stored)).is_dir():
                return Path(str(stored))
            archive = Path(item["path"])
            if archive.is_dir():
                return archive
            return file_inputs.materialise_slice(
                archive, archive.parent / "slice",
                expected_fingerprint=metadata.get("slice_fingerprint"))

        stock = selected("stock_dataset_id")
        gis = selected("building_dataset_id")
        tipo15 = selected("tipo15_dataset_id")
        template = selected("template_dataset_id")
        microclimate = selected_slice()
        epw = selected("weather_dataset_id")
        ddy = selected("ddy_dataset_id")
        climate_path = None
        if epw is not None and ddy is not None and epw.exists() and ddy.exists():
            var_root = Path(os.environ.get("WORKBENCH_VAR_DIR", PROJECT / "var"))
            ground = settings.get("ground_temperature_c")
            mains = settings.get("water_mains_temperature_c")
            climate_path = _managed_climate(
                str(epw.resolve()), str(ddy.resolve()),
                str((var_root / "climates").resolve()),
                None if ground is None else float(ground),
                None if mains is None else float(mains))
        if stock is not None:
            # A prepared stock is self-describing; the cadastre pair it replaces
            # is deliberately dropped so a leftover Valencia registration cannot
            # travel into another city's run.
            return InputSet(stock=stock, climate=climate_path, template=template,
                            microclimate=microclimate)
        return InputSet(gis=gis, tipo15=tipo15, climate=climate_path,
                        template=template, microclimate=microclimate)
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
    """Administrative areas this stock can be scoped by, if it names any.

    A district column is Valencia's; Lecco's stock has none.  Reading it
    unconditionally crashed the whole preflight for such a city, so its absence
    is answered with "no districts to choose from" - which is the truth - and
    the scope option disappears rather than the page failing.
    """
    inputs = inputs or default_inputs()
    source = inputs.stock or inputs.gis
    if source is None:
        return []
    stock = gpd.read_file(source)
    if "nombre" not in stock.columns:
        return []
    return sorted(stock["nombre"].dropna().astype(str).unique().tolist())


def _seconds_per_building() -> tuple[float, str]:
    """How long a building takes here, measured rather than remembered.

    The estimate a person plans a multi-day run around should come from this
    machine's own finished runs, not from a figure someone measured once: the
    engine has since capped storey counts and scaled the top storey, and every
    such change moves the time per building.  The newest finished run wins, and
    the basis is returned so the screen can say where the number came from.
    """
    newest: tuple[float, float, str] | None = None
    if STOCK_ROOT.exists():
        for path in STOCK_ROOT.iterdir():
            summary = path / "aggregate.json"
            if not summary.is_file():
                continue
            try:
                mean = json.loads(summary.read_text(encoding="utf-8")) \
                    .get("seconds_per_building", {}).get("mean")
                stamp = summary.stat().st_mtime
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            if not isinstance(mean, (int, float)) or mean <= 0:
                continue
            if newest is None or stamp > newest[0]:
                newest = (stamp, float(mean), path.name)
    if newest is None:
        return SECONDS_PER_BUILDING, "default"
    return newest[1], f"measured on {newest[2]}"


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

    var_dir = Path(var_dir or (PROJECT / "var"))
    if inputs.prepared:
        # Already the shape the engine reads: running the cadastre policy over it
        # would be re-deriving fields it was built to state.
        stock = gpd.read_file(inputs.stock)
        counters = {"stock_source_fingerprint": sr.file_sha256(Path(inputs.stock))}
    else:
        policy = policy or sip.StockInputPolicy()
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
    rate, rate_basis = _seconds_per_building()
    seconds = len(runnable) * rate / max(1, workers)
    return {
        "ok": True,
        "scope": scope,
        "district": district,
        "buildings_in_scope": int(len(scoped)),
        "runnable": len(runnable),
        "excluded": len(exclusions),
        "exclusion_reasons": reasons,
        "estimated_minutes": round(seconds / 60.0, 1),
        "estimated_seconds_per_building": round(rate, 1),
        "estimated_rate_basis": rate_basis,
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
              run_mode: str = "annual",
              log_dir: Path | None = None) -> dict[str, Any]:
    """Launch the runner as a subprocess and return what is needed to follow it."""
    inputs = inputs or default_inputs()
    absent = inputs.missing()
    if absent:
        # Refuse rather than launch a run that would fill in the gap from the
        # project's own Valencia defaults.  A missing climate is the worst of
        # these: the engine would silently use Valencia's design days, pressure
        # and site temperatures for whatever city this is.
        raise ValueError(
            f"cannot start a run without {', '.join(absent)}: the engine would "
            "fall back to the values this project was verified on, which belong "
            "to Valencia and not to the city being run"
        )
    out_dir = run_directory(name)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(log_dir or (PROJECT / "var"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"stock_{out_dir.name}.log"

    argv = [sys.executable, str(RUNNER_PATH),
            "--scope", scope,
            "--out-dir", str(out_dir),
            "--workers", str(workers),
            "--keep", keep]
    if inputs.prepared:
        argv += ["--stock", str(inputs.stock)]
    else:
        argv += ["--gis", str(inputs.gis), "--tipo15", str(inputs.tipo15)]
    if inputs.climate is not None:
        argv += ["--climate", str(inputs.climate)]
    if inputs.template is not None:
        argv += ["--template", str(inputs.template)]
    if run_mode == "microclimate_event":
        if inputs.microclimate is None:
            raise ValueError(
                "a microclimate event run needs an activated slice; without one "
                "there is no offset to apply and the run would just be annual"
            )
        argv += ["--microclimate", str(inputs.microclimate)]
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


def _process_state(pid: int) -> str:
    """The kernel's one-letter state for a pid, or "" when it is unknown."""
    try:
        result = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()[:1]


def is_running(pid: int) -> bool:
    """True only for a process that can still do work.

    `os.kill(pid, 0)` succeeds for a *zombie* - a child that has exited but
    whose parent has not reaped it.  A stopped run leaves exactly that: the
    subprocess dies, the API process that spawned it never calls wait(), and
    the pid stays in the process table.  Treating that as alive made every
    stopped run refuse its own resume with "run is already going" until the
    service was restarted - which is the one thing the durable ledger exists to
    make unnecessary.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # When this process is the parent, reaping clears the zombie for good
    # instead of re-detecting it on every poll.  A restarted service is not the
    # parent any more, so ChildProcessError is the normal path there.
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except (ChildProcessError, OSError):
        pass
    return _process_state(pid) != "Z"


# ---------------------------------------------------------------------------
# Reading results
# ---------------------------------------------------------------------------
# Counting a ledger means parsing it, and the interface asks for the counts
# every two seconds while a run is live - `list_runs` once per run directory on
# top of that.  On a district that is cheap.  On the full city the ledger
# reaches ~60 MB, a full parse costs ~1.5 s, and the polling would spend more
# than a core re-reading bytes that have not changed, competing with the
# EnergyPlus workers for the same machine.  The ledger is append-only by
# construction (opened "a", flushed and fsynced per row), so the tally can be
# carried forward and only the newly appended bytes parsed.
_TALLY_CACHE: dict[str, dict[str, Any]] = {}
_TALLY_LOCK = threading.Lock()


def _tally(ledger: Path) -> tuple[dict[str, int], float]:
    """Statuses and CPU seconds, parsing only what was appended since last time."""
    try:
        stat = ledger.stat()
    except OSError:
        return {}, 0.0
    key = str(ledger)
    with _TALLY_LOCK:
        state = _TALLY_CACHE.get(key)
        # A different inode, or a file that has grown shorter, is a different
        # ledger under the same name - a re-run that reused the directory.
        # Carrying the old tally forward would report buildings that this run
        # never simulated, so the count starts again from nothing.
        if (state is None or state["inode"] != stat.st_ino
                or stat.st_size < state["offset"]):
            state = {"inode": stat.st_ino, "offset": 0, "counts": {},
                     "seconds": 0.0}
        if stat.st_size > state["offset"]:
            with ledger.open("rb") as handle:
                handle.seek(state["offset"])
                chunk = handle.read(stat.st_size - state["offset"])
            # Stop at the last newline.  A row is fsynced whole, but a read can
            # still land between the write and the newline; counting half a line
            # now and the rest on the next call would double-count the row.
            end = chunk.rfind(b"\n")
            if end >= 0:
                counts = dict(state["counts"])
                seconds = state["seconds"]
                for line in chunk[:end].decode("utf-8", "replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    status = str(row.get("status"))
                    counts[status] = counts.get(status, 0) + 1
                    seconds += float(row.get("seconds") or 0.0)
                state = {"inode": stat.st_ino, "offset": state["offset"] + end + 1,
                         "counts": counts, "seconds": seconds}
        _TALLY_CACHE[key] = state
        return dict(state["counts"]), state["seconds"]


def progress(name: str) -> dict[str, Any]:
    """Live counts, read from the same ledger the resume logic trusts."""
    out_dir = run_directory(name)
    ledger = out_dir / "ledger.jsonl"
    if not ledger.exists():
        return {"run": out_dir.name, "started": False}
    counts, seconds = _tally(ledger)
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


def summary_is_partial(name: str) -> bool:
    """Whether the totals above are a running tally rather than a final one.

    `summary()` recomputes from the partial ledger when the run has not written
    its aggregate yet.  That is useful - a person watching a live run wants to
    see it - but the partial and the final total carry the *same field names*
    and differ by the whole unfinished remainder, so a caller that cannot tell
    them apart will print a fraction of the stock under a headline that claims
    the whole of it.
    """
    return not (run_directory(name) / "aggregate.json").is_file()


def scope_size(name: str) -> dict[str, int] | None:
    """The scope the run was started with, from its own `run_config.json`.

    The ledger cannot answer this while a run is live: `aggregate()` derives
    `buildings_in_scope` from the rows written so far, which mid-run is the
    same set as the rows completed - so a progress bar built on it reads 100%
    from the first row.  The runner records the real figure once, at start,
    and that is the only honest denominator until the run ends.

    Returns `None` rather than raising, and rather than guessing a number: a
    run directory written before this field existed has no scope on record,
    and "unknown" must not be reported as a count.
    """
    config_path = run_directory(name) / "run_config.json"
    if not config_path.is_file():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        runnable = int(config["runnable"])
        excluded = int(config["excluded"])
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if runnable < 0 or excluded < 0:
        return None
    return {"runnable": runnable, "excluded": excluded,
            "total": runnable + excluded}


def results_layer(name: str) -> Path:
    """The run's GIS layer, or a refusal that says which case this is.

    A run finished before the layer existed simply has no file, and telling the
    reader that is more useful than a bare 404: the layer can be produced from
    the ledger it already has, without simulating anything again.
    """
    return _result_artifact(name, rl.LAYER_FILENAME)


def results_heatmap(name: str) -> Path:
    """The run's printable heat map, or the same refusal.

    A separate artefact rather than a rendering of the layer on request: it is
    written once beside the ledger, so what a reader downloads is the same
    picture that went into the signed package.
    """
    import results_maps as rm

    return _result_artifact(name, rm.MAP_FILENAME)


def _result_artifact(name: str, filename: str) -> Path:
    path = run_directory(name) / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"{name} has no {filename}; runs finished before this file existed "
            f"can produce it with "
            f"`stock_runner.py --aggregate <ledger> --stock <prepared.gpkg>`")
    return path


def ledger_rows(name: str) -> list[dict[str, Any]]:
    out_dir = run_directory(name)
    rows = sr.latest_per_reference(sr.read_ledger(out_dir / "ledger.jsonl"))
    # Runner schema 2 did not persist the task's cluster on the ledger row,
    # even though the worker already received it and the aggregate joined it
    # from the prepared stock.  Keep the immutable ledger untouched and enrich
    # the API/CSV view from that exact prepared input.  New rows carry the
    # cluster directly; this compatibility path makes historical runs equally
    # auditable, including failures that never produced a model path.
    lookup = _cluster_lookup_for_run(out_dir)
    return [
        row if row.get("cluster") else row | {"cluster": lookup.get(str(row.get("refparcela"))) }
        for row in rows
    ]


@lru_cache(maxsize=16)
def _read_cluster_lookup(path_text: str, mtime_ns: int) -> dict[str, str]:
    del mtime_ns  # part of the cache key; content is read from path_text
    path = Path(path_text).resolve()
    allowed_root = (PROJECT / "var").resolve()
    if path != allowed_root and allowed_root not in path.parents:
        return {}
    frame = gpd.read_file(
        path, columns=["refparcela", "cluster"], ignore_geometry=True,
    )
    if not {"refparcela", "cluster"} <= set(frame.columns):
        return {}
    return {
        str(reference): str(cluster)
        for reference, cluster in zip(frame["refparcela"], frame["cluster"], strict=False)
        if reference is not None and cluster is not None and str(cluster).strip()
    }


def _cluster_lookup_for_run(out_dir: Path) -> dict[str, str]:
    config_path = out_dir / "run_config.json"
    if not config_path.is_file():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        prepared = Path(str(config["worker_config"]["prepared_gis"])).resolve()
        return _read_cluster_lookup(str(prepared), prepared.stat().st_mtime_ns)
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return {}


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
