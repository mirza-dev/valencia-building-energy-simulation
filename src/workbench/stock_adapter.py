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
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import geopandas as gpd

import results_layer as rl
import stock_input_policy as sip
import stock_runner as sr
import verified_model as vm
from workbench import building_report as building_report_view
from workbench import db, file_inputs, integrity
from workbench.scene import extract_scene_from_path

PROJECT = Path(__file__).resolve().parents[2]
RUNNER_PATH = PROJECT / "src/stock_runner.py"
STOCK_ROOT = PROJECT / "out/stock"
PUBLICATION_RESULTS_ROOT = PROJECT / "docs/results"

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


class RunActiveError(RuntimeError):
    """A destructive run operation was refused because its process is live."""


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


def _was_a_hand_picked_list(run_dir: Path) -> bool:
    """Whether this run's scope was a list of references someone chose.

    A rate has to come from a run of the same shape as the one being estimated.
    `ALL-VALENC-A_unfinished` retried the 1 406 buildings the full city run had
    not finished - the hardest ones, under contention - and averaged 210.2 s
    where the city itself averaged 96.8 s.  Newest by date, it would have told an
    operator that the next full city needs 10.7 days when the city's own ledger
    says 4.9.  A run states which it was, so this asks rather than guesses; an
    absent `run_config.json` predates the field, and every run that old covered a
    whole scope.
    """
    try:
        config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(config, dict) and config.get("scope") == "references"


def _seconds_per_building() -> tuple[float, str]:
    """How long a building takes here, measured rather than remembered.

    The estimate a person plans a multi-day run around should come from this
    machine's own finished runs, not from a figure someone measured once: the
    engine has since capped storey counts and scaled the top storey, and every
    such change moves the time per building.  The newest finished run wins, and
    the basis is returned so the screen can say where the number came from.

    Newest *comparable* run, though.  A microclimate event run simulates eight
    days per building where an annual run simulates a year, so its rate is not
    a slower or faster version of the same work - it is a different unit of it,
    and the run that carried it (`LECCO_1`, 9.6 s) would have told an operator
    that the full city needs 11 hours when the annual basis says 65.  That is
    the difference between checking back after lunch and leaving a machine
    running for three days.  `energy_period` exists precisely to make this
    distinction legible; an absent block means a run from before it was
    introduced, and those were all annual.

    Comparable in scope as well as in unit: a run of a hand-picked reference
    list is a repair of the last run, not a sample of the next one, so its rate
    describes repairs.  `_was_a_hand_picked_list` carries that measurement.
    """
    newest: tuple[float, float, str] | None = None
    if STOCK_ROOT.exists():
        for path in STOCK_ROOT.iterdir():
            summary = path / "aggregate.json"
            if not summary.is_file():
                continue
            try:
                report = json.loads(summary.read_text(encoding="utf-8"))
                mean = (report.get("seconds_per_building") or {}).get("mean")
                period = (report.get("energy_period") or {}).get("period", "annual")
                stamp = summary.stat().st_mtime
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            if not isinstance(mean, (int, float)) or mean <= 0:
                continue
            if period != "annual":
                continue
            if _was_a_hand_picked_list(path):
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
    root = STOCK_ROOT.resolve()
    candidate = STOCK_ROOT / text
    # A direct-child symlink still resolves outside the stock root.  Refuse it
    # explicitly: callers use this helper as their containment boundary.
    if candidate.is_symlink():
        raise ValueError(f"run directory may not be a symlink: {name!r}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"run directory escaped the stock root: {name!r}") from exc
    return resolved


def _discard_root() -> Path:
    """Where a deleted run waits while its bytes are reclaimed.

    A sibling of the runs themselves, so the move is always within one
    filesystem and therefore atomic.  The leading dot keeps it unreachable as a
    run name - `run_directory` refuses those outright - and it holds no
    `ledger.jsonl` of its own, so `list_runs` never sees it.
    """
    return STOCK_ROOT / ".discarded"


def reclaim_discarded_runs() -> int:
    """Delete what earlier removals moved aside, including across a restart."""
    root = _discard_root()
    if not root.is_dir():
        return 0
    reclaimed = 0
    for path in list(root.iterdir()):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            reclaimed += 1
        except OSError:
            # A partly reclaimed directory is not an error worth failing a
            # request over: the next sweep picks up whatever is left.
            continue
    return reclaimed


def delete_run(name: str) -> dict[str, Any]:
    """Permanently remove one inactive run and only its exact cached exports."""
    directory = run_directory(name)
    if not directory.is_dir() or not (directory / "ledger.jsonl").is_file():
        raise FileNotFoundError(name)

    # Package creation and deletion may not observe each other halfway through.
    with _EXPORT_LOCK:
        if active_process(name) is not None:
            raise RunActiveError(f"run {name!r} is still running")
        ledger_key = str((directory / "ledger.jsonl").resolve())
        # A full-city run is ~133 GB over ~26 000 directories; unlinking that
        # inside the request took minutes with no response, which reads as a
        # broken button rather than a slow one.  The rename is what makes the
        # run gone - atomic, same filesystem, constant time - and the bytes are
        # reclaimed behind it.  If the move cannot be done the old behaviour
        # still applies, so a deletion never silently fails to delete.
        discarded: Path | None = _discard_root() / f"{name}.{uuid.uuid4().hex}"
        try:
            discarded.parent.mkdir(parents=True, exist_ok=True)
            directory.rename(discarded)
        except OSError:
            discarded = None
            shutil.rmtree(directory)
        _TALLY_CACHE.pop(ledger_key, None)

        export_root = Path(os.environ.get(
            "WORKBENCH_EXPORT_ROOT", PROJECT / "var/exports"))
        removed_exports = 0
        if export_root.is_dir():
            pattern = re.compile(
                rf"^stock_{re.escape(name)}_(?:full|selected-[0-9a-f]{{12}})\.zip$")
            for path in export_root.iterdir():
                if path.is_file() and not path.is_symlink() and pattern.fullmatch(path.name):
                    path.unlink()
                    removed_exports += 1
    if discarded is not None:
        # Daemon: the sweep on the next deletion, and on service start, finishes
        # anything an exit interrupts.  Nothing reads what is in there.
        threading.Thread(target=reclaim_discarded_runs, name="reclaim-discarded",
                         daemon=True).start()
    return {"deleted": True, "run": name, "removed_exports": removed_exports}


def start_run(name: str, scope: str, *, district: str | None = None,
              references: list[str] | None = None,
              inputs: InputSet | None = None,
              workers: int = 6, keep: str = "full",
              resume: bool = False,
              retry_failed: bool = False,
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
    if retry_failed:
        # Not a variant of resume: `--resume` skips every terminal row, while
        # this one deliberately picks the failures back up.  The runner treats
        # it as its own continuation mode (`stock_runner.py:1403`), so it is
        # passed as its own flag rather than folded into `--resume`.
        argv.append("--retry-failed")

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


def unfinished_references(name: str) -> dict[str, Any]:
    """The references in a run that produced no result, split by why.

    Two lists, not one, because the two cases need different actions and a
    single "retry" button over both would quietly do nothing for half of them:

    * ``failed`` reached the engine and raised.  `--retry-failed` picks exactly
      these back up into the same ledger (`stock_runner.completed_references`
      counts only `ok` and `excluded`, so a failure is never treated as done).
    * ``excluded`` never reached the engine.  A screening gate refused the
      geometry, and that gate is deterministic under one profile - re-running
      them unchanged returns the same answer.  They move only when the build
      config changes, and that changes `profile_fingerprint`, which is one of
      `IDENTITY_FIELDS`, so the same ledger can no longer be resumed at all
      (`stock_runner.assert_ledger_matches_inputs`).  A fresh run directory is
      then the only correct home for them.

    Counted from `latest_per_reference`, so a reference that failed on one
    attempt and succeeded on a retry is reported once, as done.
    """
    failed: list[str] = []
    excluded: list[dict[str, Any]] = []
    for row in ledger_rows(name):
        reference = str(row.get("refparcela") or "")
        if not reference:
            continue
        status = str(row.get("status") or "")
        if status in ("failed", "failed_qa"):
            failed.append(reference)
        elif status == "excluded":
            excluded.append({"refparcela": reference,
                             "reason": row.get("reason")})
    reasons: dict[str, int] = {}
    for item in excluded:
        key = str(item["reason"] or "unrecorded")
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "run": name,
        "failed": sorted(failed),
        "excluded": sorted(item["refparcela"] for item in excluded),
        "exclusion_reasons": dict(sorted(reasons.items(),
                                         key=lambda pair: -pair[1])),
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
        aggregate = json.loads(cached.read_text(encoding="utf-8"))
        publication = _publication_heatmap_block(name)
        if publication is not None:
            aggregate.setdefault("results_layer", {})["heatmap"] = publication
        return aggregate
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

    `scope_total` is preferred over `runnable + excluded` because a resume
    rewrites this file with only the work it has left.  On ALL-VALENC-A that
    left 6 251 + 1 351 against 26 558 cumulative ledger rows - a 268 % bar,
    visible only as a permanent 100 % because the frontend clamps it.  Runs
    written before the field existed fall back to the old pair, which is
    correct for them: they were never resumed into a narrowed scope, or the
    number they carry is the best record that exists.
    """
    config_path = run_directory(name) / "run_config.json"
    if not config_path.is_file():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        runnable = int(config["runnable"])
        excluded = int(config["excluded"])
        total = int(config.get("scope_total", runnable + excluded))
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if runnable < 0 or excluded < 0 or total < 0:
        return None
    return {"runnable": runnable, "excluded": excluded,
            "total": max(total, runnable + excluded)}


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

    if active_process(name) is not None or summary_is_partial(name):
        raise RunActiveError(
            f"run {name!r} has not settled; the final heat map is unavailable")
    publication = _publication_heatmap_paths(name)
    if publication is not None:
        return publication[0]
    return _result_artifact(name, rm.MAP_FILENAME)


def _publication_heatmap_paths(name: str) -> tuple[Path, Path] | None:
    """Return a verified report derivative without touching raw run evidence."""
    import results_maps as rm

    run_directory(name)  # central name/containment validation
    directory = (PUBLICATION_RESULTS_ROOT / name).resolve()
    try:
        directory.relative_to(PUBLICATION_RESULTS_ROOT.resolve())
    except ValueError:
        return None
    image = directory / rm.MAP_FILENAME
    metadata = directory / rm.METADATA_FILENAME
    if not image.is_file() or not metadata.is_file():
        return None
    try:
        evidence = json.loads(metadata.read_text(encoding="utf-8"))
        if evidence.get("schema") != rm.HEATMAP_EVIDENCE_VERSION:
            return None
        if evidence.get("run") != name:
            return None
        recorded = evidence.get("png") or {}
        if recorded.get("file") != rm.MAP_FILENAME:
            return None
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        if recorded.get("sha256") != digest:
            return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return image, metadata


def _publication_heatmap_block(name: str) -> dict[str, Any] | None:
    paths = _publication_heatmap_paths(name)
    if paths is None:
        return None
    image, metadata = paths
    evidence = json.loads(metadata.read_text(encoding="utf-8"))
    geography = evidence.get("geography") or {}
    status = evidence.get("status_classes") or {}
    return {
        "written": True,
        "publication_derivative": True,
        "image": image.name,
        "metadata": metadata.name,
        "unit": evidence.get("unit"),
        "denominator": evidence.get("denominator"),
        "panels": list((evidence.get("panels") or {}).keys()),
        "panel_statistics": evidence.get("panels") or {},
        "status_classes": status,
        "buildings_drawn": sum(int(status.get(key) or 0)
                               for key in ("successful", "excluded", "failed")),
        "buildings_without_result": (int(status.get("excluded") or 0)
                                     + int(status.get("failed") or 0)),
        "outside_frame": geography.get("outside_frame"),
        "views": geography.get("views") or [],
        "crs": evidence.get("crs"),
        "profile_fingerprint": evidence.get("profile_fingerprint"),
        "bytes": int(image.stat().st_size),
        "sha256": (evidence.get("png") or {}).get("sha256"),
    }


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



# ---------------------------------------------------------------------------
# The user-facing Building CSV
#
# `/ledger.csv` streams the engineering record: columns in first-seen order,
# rows in worker-completion order, 24 983 absolute `model_osm` paths and 111
# truncated tracebacks.  Every cell in it is correct - it was checked against
# the ledger and matched string for string - but it is the runner's own log,
# not a table anyone can read, cite or diff.
#
# This is the published view of the same finished ledger.  Nothing is
# recomputed: values are selected, renamed and formatted.  The order below is
# the publication order of the User Guide's Appendix A, and
# `verify_product_guide_contract.py` compares the two mechanically, so a field
# cannot be published undocumented or documented unpublished.
#
# What is absent is as much of the contract as what is present.  `model_osm` is
# an absolute path into one machine, `traceback` is a truncated multi-line dump
# that begins mid-frame, and `run_identity` repeats a 64-character hash on
# every row.  They stay in the raw ledger and the technical evidence.
CURATED_BUILDING_CSV_FIELDS = (
    "refparcela", "status", "reason", "message", "cluster", "run_mode",
    "event_days", "event_window", "delta_peak_k", "delta_base_k", "seconds",
    "pruned_bytes", "total_site_kwh", "space_heating_kwh", "cooling_kwh",
    "dhw_kwh", "total_site_kwh_m2", "total_site_kwh_m2_conditioned",
    "space_heating_kwh_m2", "cooling_kwh_m2", "dhw_kwh_m2", "fans_kwh_m2",
    "pumps_kwh_m2", "lighting_kwh_m2", "equipment_kwh_m2",
    "space_heating_kwh_m2_conditioned", "cooling_kwh_m2_conditioned",
    "site_gas_kwh_m2", "site_elec_kwh_m2", "residential_site_kwh",
    "terciario_site_kwh", "residential_total_site_kwh_m2", "dhw_share_pct",
    "terciario_share_pct", "total_site_kwh_per_person",
    "total_site_kwh_per_dwelling", "total_site_co2_kg_m2", "hvac_co2_kg_m2",
    "total_site_co2_t", "hvac_co2_t", "footprint_m2", "res_area_m2",
    "tipo15_res_area_m2", "res_area_source", "total_conditioned_area_m2",
    "conditioned_to_cadastral_ratio", "altura_max", "n_floors_total",
    "n_floors_residential", "residential_storeys_effective", "built_storeys",
    "top_storey_fraction", "storey_cap_applied",
    "mixed_use_storeys_converted", "mixed_use_basis",
    "large_footprint_single_zone", "footprint_fidelity",
    "simplify_tolerance_used_m", "storey_rule_margin", "storey_rule_snapped",
    "n_party_surfaces", "n_shading_surfaces", "n_windows", "window_area_m2",
    "pob_total", "num_vivend", "dwelling_area_m2", "padron_occupants",
    "occupants_applied", "occupants_source", "occupancy_plausibility",
    "ground_use", "ground_use_source", "wall_construction",
    "roof_construction", "qa_all_passed", "warnings", "severes",
    "severes_benign_shading_ems", "severes_unexplained", "fatals",
    "profile_fingerprint", "climate_fingerprint", "template_fingerprint",
    "policy_fingerprint", "stock_source_fingerprint", "runner_schema",
    "zero_policy"
)


# Published period-neutral.  The ledger keeps its `_yr` keys for backward
# compatibility, but an eight-day event total must never reach a reader under a
# per-year label, so the rename happens at the publication boundary.
CURATED_FIELD_SOURCES = {
    "total_site_co2_t": "total_site_co2_t_yr",
    "hvac_co2_t": "hvac_co2_t_yr",
}

# Descriptive columns the ledger never carried; the same read-only enrichment
# the results layer performs, using its rule for which of them may be summed
# across a reference's footprints and which may only be repeated.
_CURATED_CONTEXT = ("cluster", "pob_total", "num_vivend", "altura_max")


@lru_cache(maxsize=16)
def _read_stock_context(path_text: str, mtime_ns: int) -> dict[str, dict[str, Any]]:
    """Per-reference stock context, collapsed the way the results layer does."""
    del mtime_ns  # part of the cache key; content is read from path_text
    path = Path(path_text).resolve()
    allowed_root = (PROJECT / "var").resolve()
    if path != allowed_root and allowed_root not in path.parents:
        return {}
    wanted = [name for name in _CURATED_CONTEXT]
    try:
        frame = gpd.read_file(path, columns=["refparcela", *wanted],
                              ignore_geometry=True)
    except (OSError, RuntimeError, ValueError, KeyError):
        return {}
    available = [name for name in wanted if name in frame.columns]
    if "refparcela" not in frame.columns or not available:
        return {}
    context: dict[str, dict[str, Any]] = {}
    for record in frame[["refparcela", *available]].to_dict("records"):
        reference = str(record.get("refparcela") or "").strip()
        if not reference:
            continue
        held = context.setdefault(reference, {})
        for name in available:
            value = record.get(name)
            if value is None:
                continue
            # A reference may carry several footprint rows.  Population is split
            # across them and must be added back up; dwellings and height are
            # repeated identically and must not be.  This is `results_layer`'s
            # rule, imported rather than restated so the two cannot disagree.
            if name in rl.CONTEXT_SUMMED:
                try:
                    held[name] = float(held.get(name) or 0.0) + float(value)
                except (TypeError, ValueError):
                    continue
            elif name not in held:
                held[name] = value
    return context


def _stock_context_for_run(out_dir: Path) -> dict[str, dict[str, Any]]:
    config_path = out_dir / "run_config.json"
    if not config_path.is_file():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        prepared = Path(str(config["worker_config"]["prepared_gis"])).resolve()
        return _read_stock_context(str(prepared), prepared.stat().st_mtime_ns)
    except (KeyError, OSError, RuntimeError, ValueError, TypeError,
            json.JSONDecodeError):
        return {}


def _is_numeric_text(text: str) -> bool:
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


def _curated_cell(value: Any) -> str:
    """One published cell.

    A measured zero prints as `0`; something the record never carried stays
    blank.  The two must never look alike, which is why `None` is not coerced.
    A boolean is written as a word rather than `True`/`1`, so a spreadsheet
    cannot average a flag, and a non-finite float is not a measurement and is
    left blank rather than printed as `nan`.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        if value.is_integer():
            return str(int(value))
        text = repr(value)
        if "e" in text or "E" in text:
            # `repr` switches to an exponent below 1e-4, which put 583 cells of
            # `footprint_fidelity` into scientific notation and left that column
            # holding two visibly different kinds of number.  Decimal renders
            # the same value in full without padding it with zeros.
            text = format(Decimal(text), "f")
        return text
    text = str(value)
    if not text:
        return ""
    # Excel and LibreOffice execute a cell that opens with one of these.  Only
    # text is guarded: a negative number must stay a number.
    if text[0] in "=+-@" and not _is_numeric_text(text):
        return "'" + text
    return text


# An exclusion's code carries its own count (`interior_rings_2`,
# `duplicate_refparcela_3_rows`) but the runner records no sentence for it, and
# a reader of the published table should not have to decode an identifier.  The
# text below states what the code already says - it adds no fact the record
# does not carry - and the machine code stays in `reason` beside it.
_EXCLUSION_SENTENCES = (
    (re.compile(r"^duplicate_refparcela_(\d+)_rows$"),
     "The stock carries {0} rows under this reference, so it cannot be "
     "resolved to one building."),
    (re.compile(r"^interior_rings_(\d+)$"),
     "The footprint has {0} interior ring(s); a courtyard outline is not "
     "modelled as a single mass."),
    (re.compile(r"^unmodellable_geometry$"),
     "The footprint could not be turned into a closed, simple polygon."),
)


def _explain_exclusion(reason: str) -> str:
    for pattern, sentence in _EXCLUSION_SENTENCES:
        match = pattern.match(reason or "")
        if match:
            return sentence.format(*match.groups())
    return ""


def _without_local_paths(text: str, out_dir: Path) -> str:
    """Keep an engine diagnostic, drop the machine it happened to run on.

    A failure message can quote the absolute path of the `.err` file it wants
    the reader to open.  The sentence is worth publishing; the path is this
    installation's directory layout and means nothing on another computer, so
    it becomes the run-relative path the evidence endpoints already use.
    """
    if "/" not in text:
        return text
    # The project lives under a path with spaces in it, so a pattern that ends
    # at whitespace cuts the path in half and leaves the remainder in place.
    # The two roots that can legitimately appear are known strings, so they are
    # removed as strings; only what is left goes to a pattern.
    cleaned = text.replace(str(out_dir) + "/", "").replace(str(out_dir), "")
    cleaned = cleaned.replace(str(PROJECT) + "/", "").replace(str(PROJECT), "")
    return re.sub(r"(?:/(?:Volumes|Users|home|private)/)[^\s,;]*", "", cleaned)


def curated_building_rows(name: str) -> list[dict[str, str]]:
    """The finished run's ledger as the published Building CSV.

    Refuses a run whose totals are still moving: the partial and the final
    record carry the same field names and differ by the unfinished remainder,
    so a file downloaded mid-run would look final and be a fraction of the
    stock.
    """
    out_dir = run_directory(name)
    if not (out_dir / "ledger.jsonl").is_file():
        raise FileNotFoundError(name)
    if active_process(name) is not None or summary_is_partial(name):
        raise RunActiveError(
            f"run {name!r} has not finished: the Building CSV is published from "
            "the settled ledger, not from a running tally")
    rows = ledger_rows(name)
    context = _stock_context_for_run(out_dir)

    published: list[dict[str, str]] = []
    for row in rows:
        source = dict(row)
        for key, value in context.get(str(source.get("refparcela")), {}).items():
            if source.get(key) in (None, ""):
                source[key] = value
        # Energy per resident and per dwelling are not in the ledger, which
        # reports intensity per floor area.  Both stay blank where the
        # denominator is zero: "nobody to divide by" is not "uses nothing".
        total = source.get("total_site_kwh")
        for field, divisor in (("total_site_kwh_per_person", "pob_total"),
                               ("total_site_kwh_per_dwelling", "num_vivend")):
            try:
                bottom = float(source.get(divisor) or 0.0)
                source[field] = round(float(total) / bottom, 1) if (
                    total is not None and bottom > 0) else None
            except (TypeError, ValueError):
                source[field] = None
        # An exclusion records its explanation in `detail`, a failure in
        # `message`.  `detail` is not published, so the one human-readable
        # column must carry both or the 38 exclusions arrive unexplained.
        if not source.get("message") and source.get("detail"):
            source["message"] = source["detail"]
        if not source.get("message") and source.get("status") != "ok":
            source["message"] = _explain_exclusion(str(source.get("reason") or ""))
        if source.get("message"):
            source["message"] = _without_local_paths(str(source["message"]), out_dir)
        published.append({
            field: _curated_cell(
                source.get(CURATED_FIELD_SOURCES.get(field, field)))
            for field in CURATED_BUILDING_CSV_FIELDS
        })
    # Deterministic, so two runs of the same city diff cleanly instead of in
    # worker-completion order.
    published.sort(key=lambda item: (item.get("cluster", ""),
                                     item.get("refparcela", "")))
    return published


def curated_building_dictionary() -> list[dict[str, str]]:
    """The companion dictionary, read out of the guide that defines it.

    Appendix A is the publication contract; writing a second description here
    would create a second definition that could drift from it.
    """
    appendix = PROJECT / "docs/guides/source/user-guide.md"
    entries: dict[str, tuple[str, str]] = {}
    if appendix.is_file():
        text = appendix.read_text(encoding="utf-8")
        section = text.split("# Appendix A.", 1)[-1].split("## A.8", 1)[0]
        for line in section.splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            # A.1-A.5 carry a middle column (unit and period, or which output
            # the field applies to); A.6 and A.7 are two columns.  Both are
            # read, or the diagnostic and provenance fields arrive undefined.
            if len(cells) < 2 or cells[0].startswith("---"):
                continue
            qualifier = cells[1] if len(cells) >= 3 else ""
            for token in re.findall(r"`([^`]+)`", cells[0]):
                entries.setdefault(token.strip(), (qualifier, cells[-1]))
    return [
        {"field": field,
         "ledger_source": CURATED_FIELD_SOURCES.get(field, field),
         "unit_or_scope": entries.get(field, ("", ""))[0],
         "meaning": entries.get(field, ("", ""))[1]}
        for field in CURATED_BUILDING_CSV_FIELDS
    ]

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


def building_report(name: str, refparcela: str) -> str:
    """The preserved evidence for one building, rendered as a readable page.

    Derived like `building_scene`: composed from `deep_layers.json` on every
    request and never written into the run directory, which is signed evidence
    that `_package_sources` collects wholesale with `rglob`.

    The ledger row is looked up but not required.  It only supplies the run's
    own status wording, and a missing row must not withhold a report whose
    evidence is sitting on disk.
    """
    layers_path = artifact_path(name, refparcela, "deep_layers.json")
    try:
        layers = json.loads(layers_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportUnavailable(
            f"{layers_path.name} for {refparcela} could not be read: {exc}") from exc
    if not isinstance(layers, dict):
        raise ReportUnavailable(
            f"{layers_path.name} for {refparcela} is not a layer record")
    reference = str(Path(str(refparcela)).name)
    row = next((item for item in ledger_rows(name)
                if str(item.get("refparcela")) == reference), None)
    metadata: dict[str, Any] = {}
    try:
        profile_path = artifact_path(name, reference, "verified_profile.json")
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        if isinstance(profile, dict):
            metadata["verified_profile"] = profile
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        # Older and failed records may not preserve this optional companion.
        # Its absence is rendered honestly; it must not hide deep_layers.json.
        pass
    try:
        error_path = artifact_path(name, reference, "eplusout.err")
        with error_path.open(encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline(512)
        match = re.search(
            r"EnergyPlus,\s*Version\s+([^,\r\n]+)", first_line, re.IGNORECASE)
        if match:
            metadata["energyplus_version"] = match.group(1).strip()
    except (FileNotFoundError, OSError, ValueError):
        pass
    try:
        return building_report_view.render(
            layers, run=run_directory(name).name, reference=reference,
            ledger_row=row, metadata=metadata)
    except building_report_view.EvidenceIdentityError as exc:
        raise ReportUnavailable(str(exc)) from exc


class ReportUnavailable(RuntimeError):
    """The evidence is there but cannot be turned into a page.

    Like `SceneUnavailable`, deliberately not a `ValueError`: the caller asked
    for a building that exists, so this is unreadable evidence rather than a
    bad request, and the two must not reach the operator wearing the same code.
    """


class SceneUnavailable(RuntimeError):
    """The preserved model is there but cannot be turned into browser geometry.

    Deliberately not a `ValueError`: the caller asked for a building that
    exists, so this is unreadable evidence rather than a bad request, and the
    two must not arrive at the operator wearing the same status code.
    """


def building_scene(name: str, refparcela: str) -> dict[str, Any]:
    """Rebuild the browser geometry of one preserved building model.

    Derived, never preserved.  The scene is recomputed from `model_python.osm`
    on every request and no file is written into the run directory: that tree
    is signed evidence and `_package_sources` collects it wholesale, so a
    cached `scene.json` dropped beside the model would silently change what a
    signed package contains.  A warm extraction costs about 0.2 s, which is
    cheap enough that the honest arrangement is also the affordable one.

    The origin and reference come from the building's own `deep_layers.json`,
    not from a stub.  A synthetic origin would render identically and be a
    quiet lie about where the building stands.
    """
    osm = artifact_path(name, refparcela, "model_python.osm")
    layers = artifact_path(name, refparcela, "deep_layers.json")
    try:
        summary = json.loads(layers.read_text(encoding="utf-8"))["summary"]
        stats = {
            "refparcela": str(summary["refparcela"]),
            "origin_x": float(summary["origin_x"]),
            "origin_y": float(summary["origin_y"]),
        }
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise SceneUnavailable(f"{layers.name} does not carry the model's origin") from exc

    try:
        scene = extract_scene_from_path(osm, stats)
    except Exception as exc:  # OpenStudio raises broadly on a damaged model
        raise SceneUnavailable(f"{osm.name} could not be opened") from exc

    if not scene.get("surfaces"):
        # Zero surfaces is "this model has no geometry", not "an empty model":
        # the viewer's bounding box would collapse to NaN and draw a blank
        # canvas that looks like a rendering bug rather than a missing one.
        raise SceneUnavailable(f"{osm.name} contains no surfaces")
    return scene


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
