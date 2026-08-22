"""Run the verified model over a stock of buildings.

This module is orchestration only.  It contains no physics and no energy
arithmetic: every building goes through ``verified_model.simulate_verified_building``
and every data-quality decision goes through ``stock_input_policy.prepare_stock``.
If either of those is ever bypassed here, the "one verified call site" guarantee
is gone, so keep it that way.

What it adds on top of running the CLI in a loop:

* **Prepared stock.**  ``prepare_stock`` imputes the ~1 600 invalid floor counts
  and resolves cluster mapping, and the result is written once to a GeoPackage
  that every worker reads.  The raw cadastre stays the shading context (see
  ``deep_building.resolve_neighbour_source``) so imputation cannot silently move
  anybody else's result.
* **Process parallelism.**  ``run_simulation.run_energyplus`` drives the
  in-process pyenergyplus API, which is not thread safe - workers are processes,
  each with its own TMPDIR.
* **A durable ledger.**  One JSONL line per building, flushed and fsynced as it
  lands.  A stock run gets interrupted (volume unmounts, laptop sleeps); resume
  reads the ledger and skips what is already done.
* **Failure isolation.**  A bad polygon records a typed failure and the run
  continues.  Nothing is ever silently skipped: every building ends up in the
  ledger as ``ok``, ``failed`` or ``excluded`` with a reason.
* **Artifact pruning.**  5.8 MB per building is 153 GB over the full stock.  By
  default the bulky EnergyPlus intermediates go once their numbers have been
  read; evidence (``eplustbl.htm``, ``eplusout.err``, the layer dump and the
  profile stamp) stays.  Failures and a sample keep everything.

Usage:

    python src/stock_runner.py --scope benicalap --workers 6
    python src/stock_runner.py --scope clusters --out-dir out/stock/stage1
    python src/stock_runner.py --scope all --resume
    python src/stock_runner.py --aggregate out/stock/benicalap/ledger.jsonl
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import pandas as pd

import climate as cl
import coverage_bias as cb
import deep_building as db
import eu_footprint_flags as euf
import floor_area_allocation as faa
import model_builder as mb
import results_layer as rl
import stock_input_policy as sip
import template_contract as tpl
import verified_model as vm

log = logging.getLogger("stock_runner")

# EnergyPlus intermediates that are fully reconstructible from model.idf + the
# weather file.  `eplustbl.htm` is the human-readable evidence and stays; so do
# the error log, the layer dump and the profile stamp.
#
# `model_python.osm` used to be pruned here.  It is not any more: it is the one
# artifact a person opens by hand, rebuilding it means re-running the whole
# chain, and at ~1 MB a building it is the cheapest thing in the directory.
PRUNABLE_ARTIFACTS = (
    "eplusout.sql", "model.idf", "epluszsz.csv", "eplusout.mdd", "eplusout.rdd",
    "eplusout.bnd", "eplusout.mtd", "eplusout.shd", "eplusout.audit",
    "eplusout.eio", "eplusout.end", "sqlite.err",
)
KEPT_ARTIFACTS = ("eplustbl.htm", "eplusout.err", "deep_layers.json",
                  "verified_profile.json", "model_python.osm")

# The OpenStudio model is the one artifact somebody opens by hand, so the run
# directory is not a good enough home for it: finding "a BlocPluriP04 model"
# would mean walking thousands of folders.  Every successful run also gets a
# hardlink under `models/<cluster>/<refparcela>.osm`.  On APFS a hardlink is the
# same bytes under a second name - the index costs no extra disk, and deleting
# either name leaves the other readable.
MODEL_ARTIFACT = "model_python.osm"

MODEL_INDEX_README = """# OpenStudio modelleri

`<kume>/<refparcela>.osm` -- OpenStudio Application ile dogrudan acilir.

Her dosya, o binanin simule edilen modelinin **kendisidir** (hardlink, kopya
degil): gercek kadastro footprint'i, gercek kat sayisi, gercek EPSG:25830
koordinatlari, 50 m yaricapindaki gercek komsular golgeleme yuzeyi olarak,
Rai'nin `PlantillaOS_v2.osm` kutuphanesinden gelen construction/schedule/
space type'lar ve uzerine eklenen alti katman (gercek occupancy, zemin rejimi,
camlama, cerceve, DHW, PTHP).

Ayni binanin tam ciktilari (`eplustbl.htm`, `model.idf`, `eplusout.err`,
`deep_layers.json`, `verified_profile.json`) `../runs/<refparcela>_deep/`
altindadir.

Hangi profille uretildigi `verified_profile.json` icindeki fingerprint ile
kanitlanir; ayni fingerprint ledger'in her satirinda da vardir.
"""

# Numbers worth carrying in the ledger.  Everything else stays in deep_layers.json
# next to the run; the ledger is meant to be loadable as a dataframe.
LEDGER_METRICS = (
    "space_heating_kwh_m2", "cooling_kwh_m2", "dhw_kwh_m2", "fans_kwh_m2",
    "pumps_kwh_m2", "lighting_kwh_m2", "equipment_kwh_m2",
    "total_site_kwh_m2", "total_site_kwh_m2_conditioned",
    "space_heating_kwh_m2_conditioned", "cooling_kwh_m2_conditioned",
    "site_gas_kwh_m2", "site_elec_kwh_m2", "dhw_share_pct",
    "hvac_co2_kg_m2", "total_site_co2_kg_m2", "hvac_co2_t_yr", "total_site_co2_t_yr",
    "total_site_kwh", "space_heating_kwh", "cooling_kwh", "dhw_kwh",
    "residential_total_site_kwh_m2", "residential_site_kwh", "terciario_site_kwh",
    "terciario_share_pct", "conditioned_to_cadastral_ratio",
    "res_area_m2", "tipo15_res_area_m2", "res_area_source",
    "total_conditioned_area_m2", "footprint_m2", "large_footprint_single_zone",
    "n_floors_total", "n_floors_residential", "mixed_use_storeys_converted",
    "mixed_use_basis", "residential_storeys_effective",
    "top_storey_fraction", "dwelling_area_m2",
    "built_storeys", "storey_cap_applied", "n_party_surfaces",
    "n_shading_surfaces", "n_windows", "window_area_m2",
    "padron_occupants", "occupants_applied", "occupants_source",
    "occupancy_plausibility", "ground_use", "ground_use_source",
    "zero_policy", "wall_construction", "roof_construction",
    "warnings", "severes", "severes_benign_shading_ems", "severes_unexplained",
    "fatals", "qa_all_passed",
)

def footprint_limits() -> tuple[float, float]:
    """The footprint window the frozen builder will actually accept.

    Read from the build config rather than restated here: a hardcoded guess of
    30 m2 let 30-50 m2 buildings through the screen only to fail inside the
    engine, and missed the upper bound entirely (Stage 2, 2026-07-28).
    """
    geometry = mb.DEFAULT_BUILD_CONFIG.geometry
    return float(geometry.footprint_min_m2), float(geometry.footprint_max_m2)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------
@dataclass
class Ledger:
    """Append-only JSONL record of every building the run touched."""

    path: Path
    _handle: object = field(default=None, repr=False)

    def open(self) -> "Ledger":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        return self

    def append(self, row: dict) -> None:
        assert self._handle is not None, "ledger is not open"
        self._handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        # a stock run WILL be interrupted; a buffered ledger loses the tail
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False


def read_ledger(path: Path) -> list[dict]:
    """Read a ledger, tolerating a half-written final line from a hard kill."""
    if not Path(path).exists():
        return []
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("[ledger] discarding truncated final line")
    return rows


def completed_references(rows: list[dict]) -> set[str]:
    """References that must not be run again on resume.

    Failures are deliberately NOT included: `--retry-failed` exists so a
    transient failure can be picked up without redoing the whole scope.
    """
    return {r["refparcela"] for r in rows if r.get("status") in ("ok", "excluded")}


# ---------------------------------------------------------------------------
# Stock preparation
# ---------------------------------------------------------------------------
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# Bump whenever prepare_stock_file changes what it writes or how it derives it.
# Without this the cache happily serves a file built by older code: on
# 2026-07-30 the Tipo15 area column was added and the very next run silently
# reused a GeoPackage that did not have it.
# v3 (2026-08-03): ground_use / ground_use_source columns - the ground-floor
# policy now actually reaches the engine.
PREPARED_STOCK_SCHEMA = 3

# Bump when the meaning of a ledger row changes, so an older ledger can never be
# resumed into by newer code that would write incompatible rows beside it.
RUNNER_SCHEMA = 2

# A shapefile is a set of files, and the attributes the whole pipeline runs on -
# altura_max, cluster, pob_total, num_vivend - live in the .dbf, not the .shp.
# Hashing only the .shp would miss an attribute edit entirely.
SHAPEFILE_SIDECARS = (".shp", ".dbf", ".shx", ".prj", ".cpg", ".sbn", ".sbx")


def shapefile_snapshot_sha256(gis_path: Path) -> dict[str, str]:
    """Hash every sidecar that travels with a shapefile."""
    gis_path = Path(gis_path)
    if gis_path.suffix.lower() != ".shp":
        return {gis_path.name: file_sha256(gis_path)}
    return {gis_path.with_suffix(suffix).name: file_sha256(gis_path.with_suffix(suffix))
            for suffix in SHAPEFILE_SIDECARS
            if gis_path.with_suffix(suffix).exists()}


def stock_source_fingerprint(gis_path: Path, tipo15_path: Path,
                             policy_fingerprint: str) -> str:
    """Identity of a prepared stock: the policy, the source files AND the code.

    Keying the cache on the policy alone was a silent-wrong-data bug: with the
    same policy and a different `--gis`, the correct stock was derived in memory
    but never written, and the workers went on reading the *previous* dataset's
    geometry for every building.  Scope came from one file, physics from another,
    and nothing anywhere looked broken.
    """
    identity = {
        "schema": PREPARED_STOCK_SCHEMA,
        "policy": policy_fingerprint,
        "gis": shapefile_snapshot_sha256(gis_path),
        "tipo15": file_sha256(Path(tipo15_path)),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


def prepared_stock_path(source_fingerprint: str, var_dir: Path) -> Path:
    return Path(var_dir) / f"stock_prepared_{source_fingerprint[:16]}.gpkg"


def prepare_stock_file(gis_path: Path, tipo15_path: Path,
                       policy: sip.StockInputPolicy, var_dir: Path,
                       *, force: bool = False) -> tuple[Path, gpd.GeoDataFrame, dict]:
    """Run the policy once and cache the corrected stock as a GeoPackage.

    The file is content-addressed on the policy *and* on the source datasets, so
    changing either produces a different file rather than reusing a stale one.
    """
    stock, resolved, counters = sip.prepare_stock(
        Path(gis_path), Path(tipo15_path), policy, duplicate_parcel_apportioning=True)
    # The policy's Tipo15 area travels under its own name, and it is renamed
    # HERE, on the frame, before anything else sees it - not on the copy being
    # written.  The engine derives its own `res_area_m2` from the geometry
    # (footprint x residential storeys) and that is the basis every energy
    # figure and the Rai comparison use - his own 954.80 m2 is 4 x 238.70, a
    # geometric storey area, not a net cadastral one.  Two different quantities
    # that differ by ~32 % city-wide must not share a column name.
    #
    # Renaming only the written copy left the returned frame carrying the
    # cadastral area under the geometric name, so the file and the frame
    # disagreed and a reader saw whichever the path handed it.  That is how the
    # coverage-bias measurement came out `measured: false` on a live run while
    # measuring fine when the same ledger was re-aggregated from the file
    # (found on Benicalap v9): the bias block looks for `tipo15_res_area_m2`,
    # the live run passes this frame, and the frame did not have it.  The bound
    # written to answer whether the excluded buildings skew the result would
    # simply not have been produced by the full-city run.
    if "res_area_m2" in stock.columns:
        stock = stock.rename(columns={"res_area_m2": "tipo15_res_area_m2"})
    fingerprint = sip.policy_fingerprint(resolved)
    source_fingerprint = stock_source_fingerprint(gis_path, tipo15_path, fingerprint)
    out = prepared_stock_path(source_fingerprint, var_dir)
    if force or not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        # This allowlist IS the contract between the policy and the engine: a
        # column the policy resolves but this list omits never reaches a
        # worker.  That is exactly how ground_use went missing on first wiring
        # (2026-08-03) - prepare_stock carried it, the file did not, and the
        # run silently fell back to terciario everywhere.  Caught by checking
        # the written file, and pinned by a test on the FILE, not the frame -
        # and by one that holds the FILE and the FRAME to the same names, which
        # is the gap the rename above closes.
        columns = [c for c in ("refparcela", "altura_max", "cluster", "family",
                               "period", "nombre", "pob_total", "num_vivend",
                               "footprint_area_m2", "imputed_floors",
                               "res_area_proxy", "dup_refparcela",
                               "ground_use", "ground_use_source",
                               "tipo15_res_area_m2", "geometry")
                   if c in stock.columns]
        prepared = stock[columns].copy()
        prepared.to_file(out, driver="GPKG")
        log.info("[stock] prepared file written: %s (%s buildings)", out.name, len(stock))
    else:
        log.info("[stock] prepared file reused: %s", out.name)
    counters = dict(counters)
    counters["policy_fingerprint"] = fingerprint
    counters["stock_source_fingerprint"] = source_fingerprint
    counters["gis_path"] = str(gis_path)
    counters["tipo15_path"] = str(tipo15_path)
    return out, stock, counters


def screen_geometry(stock: gpd.GeoDataFrame) -> tuple[list[str], list[dict]]:
    """Split the scope into runnable references and recorded exclusions.

    Screens exactly the shapes the frozen builder rejects outright, so the reason
    is recorded once and cheaply instead of arriving as a stack trace 26 000
    times.  Anything else is left to fail loudly inside the engine: a new failure
    mode must not be able to hide behind a silent filter.

    Iteration is over *references*, not rows.  A duplicated `refparcela` cannot
    be modelled at all (`load_building_row` demands exactly one row and says so),
    and screening per row used to file the same reference twice - once excluded,
    once failed.
    """
    low, high = footprint_limits()
    runnable: list[str] = []
    excluded: list[dict] = []

    for ref, group in stock.groupby(stock["refparcela"].astype(str), sort=False):
        ref = str(ref)
        if len(group) > 1:
            # matches the engine's own wording: it wants a multipart decision first
            excluded.append({"refparcela": ref,
                             "reason": f"duplicate_refparcela_{len(group)}_rows"})
            continue
        geom = group.geometry.iloc[0]
        if geom is None or geom.is_empty:
            excluded.append({"refparcela": ref, "reason": "empty_geometry"})
            continue
        # Ask the engine's own normaliser rather than restating its rule.  This
        # used to refuse every MultiPolygon outright, but `clean_polygon`
        # unwraps a single-part one and simulates it happily - and a source that
        # writes its footprints that way (the EU building database does) had its
        # whole stock excluded as "unsupported geometry".  Multi-part shapes,
        # holes and invalid rings are still refused; the difference is that the
        # decision is now taken where it is enforced.
        if geom.geom_type not in ("Polygon", "MultiPolygon"):
            excluded.append({"refparcela": ref,
                             "reason": f"unsupported_geometry_{geom.geom_type}"})
            continue
        if geom.geom_type == "Polygon" and len(geom.interiors) > 0:
            excluded.append({"refparcela": ref,
                             "reason": f"interior_rings_{len(geom.interiors)}"})
            continue
        # Run the builder's own footprint gate here rather than restating it.
        # It rejects two different things - an out-of-range area, and a polygon
        # whose detail is lost by simplification (3.0 % of the stock, measured) -
        # and both are geometry-quality decisions, not simulation failures.  They
        # belong in `excluded` with a reason, not in `failed` with a traceback.
        try:
            cleaned = mb.clean_polygon(geom)
            mb.prepare_footprint(cleaned)
        except ValueError as exc:
            message = str(exc)
            if "simplification changed area" in message:
                reason = "simplification_area_change_over_limit"
            elif "outside the expected range" in message:
                reason = "footprint_outside_range"
            else:
                reason = f"unmodellable_polygon_{type(exc).__name__}"
            excluded.append({"refparcela": ref, "reason": reason,
                             "footprint_m2": round(float(geom.area), 2),
                             "detail": message[:200]})
            continue
        except Exception as exc:                   # noqa: BLE001 - unmodellable shape
            excluded.append({"refparcela": ref,
                             "reason": f"unmodellable_polygon_{type(exc).__name__}",
                             "detail": str(exc)[:200]})
            continue
        runnable.append(ref)
    return runnable, excluded


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
_WORKER: dict = {}


def _init_worker(config: dict) -> None:
    """Give each worker its own scratch space and cache the shared config."""
    tmp = Path(config["tmp_root"]) / f"w{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    for key in ("TMPDIR", "TMP", "TEMP"):
        os.environ[key] = str(tmp)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    _WORKER.update(config)


def _prune_run_dir(run_dir: Path) -> int:
    freed = 0
    for name in PRUNABLE_ARTIFACTS:
        target = run_dir / name
        if target.exists():
            freed += target.stat().st_size
            target.unlink()
    return freed


def link_model(run_dir: Path, refparcela: str, cluster: str | None,
               models_root: Path) -> str | None:
    """Expose the run's .osm under `models/<cluster>/<refparcela>.osm`.

    Hardlink where the filesystem allows it (free), copy where it does not, so
    the index also works if the outputs ever land on a non-APFS volume.
    """
    source = Path(run_dir) / MODEL_ARTIFACT
    if not source.exists():
        return None
    folder = Path(models_root) / (str(cluster) if cluster else "_uncategorised")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{refparcela}.osm"
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return str(target)


def _config_for(refparcela: str):
    """This building's BuildConfig, with its own envelope pinned if it has one.

    `deep_building.config_for_building` resolves an unpinned envelope through
    `model_config.TABULA_ES` - the Spanish IVE table - and that is exactly how a
    building outside Spain would end up with Spanish walls.  The same function
    states the way out: "a caller that has already pinned the envelope means
    it".  A stock carrying measured U-values is such a caller.

    Returns the shared config untouched when the stock states no envelope, so
    the Valencia path is byte-identical to what it has always been.
    """
    shared = _WORKER.get("config")
    envelope = _WORKER.get("envelopes", {}).get(refparcela)
    if not envelope:
        return shared
    base = shared if shared is not None else mb.DEFAULT_BUILD_CONFIG
    return base.model_copy(deep=True).with_legacy_params({
        "wall_u": float(envelope["wall_u"]),
        "roof_u": float(envelope["roof_u"]),
        "window_u": float(envelope["window_u"]),
    })


EVENT_MODE = "microclimate_event"


def _event_stamp(plan: dict | None = None) -> dict:
    """What kind of run this is, for every row including the failed ones.

    Measured 2026-08-12: without this the ledger of an 8-day event run was
    schema-identical to an annual one.  The per-area columns held 1.02-1.26
    kWh/m2 instead of ~52, `total_site_co2_t_yr` held an 8-day figure in a
    field named per-year, and the slice fingerprint lived only in
    `run_config.json` - so `aggregate()` and the map layer, which read the
    ledger, could not tell the two apart and labelled the event as annual.
    A file has to say what it is.
    """
    # Callable from the parent as well as a worker: the excluded rows are
    # written before the pool starts, and an unstamped exclusion made the
    # ledger look like two runs mixed together (measured, first event run).
    plan = plan if plan is not None else _WORKER.get("event_plan")
    if plan is None:
        return {}
    window = [int(v) for v in plan["window"]]
    return {"run_mode": EVENT_MODE,
            "event_days": int(plan["days"]),
            "event_window": (f"{window[0]:02d}-{window[1]:02d}"
                             f"..{window[2]:02d}-{window[3]:02d}")}


def _event_for(refparcela: str) -> dict | None:
    """This building's slot in the microclimate plan, or None for an annual run.

    A building the slice does not reach is refused rather than run at a zero
    offset: zero is a measurement claiming the building sits exactly at the
    domain average, and "no data" is not that claim.
    """
    plan = _WORKER.get("event_plan")
    if plan is None:
        return None
    import microclimate as mcl

    entry = plan["deltas"].get(str(refparcela))
    if entry is None:
        raise ValueError(
            f"{refparcela} has no microclimate offset: the slice does not cover it")
    # Coerced at the deserialisation boundary: the plan crosses a process
    # boundary to reach this worker, and a date that arrives as text compares
    # unequal against every row in the weather file instead of failing loudly.
    window = tuple(int(v) for v in plan["window"])
    epw = mcl.write_event_epw(
        Path(plan["base_epw"]), Path(plan["epw_dir"]),
        delta_peak_k=entry["delta_peak_k"], delta_base_k=entry["delta_base_k"],
        window=window)
    return {"epw_path": epw, "window": window, "days": plan["days"],
            "slice_record": plan["slice_record"], **entry}


def run_one(task: tuple[str, str | None] | str) -> dict:
    """Simulate one building. Never raises: a failure is a ledger row too."""
    refparcela, cluster = task if isinstance(task, tuple) else (task, None)
    started = time.time()
    out_dir = Path(_WORKER["out_dir"])
    # the full identity travels on every row, so what produced it can always be
    # established from the ledger alone
    # The run mode rides on every row, the failed ones included: a building the
    # slice refused is part of the evidence about that slice's reach.
    base = {"refparcela": refparcela, "cluster": cluster,
            **_event_stamp(),
            **{key: _WORKER[key] for key in IDENTITY_FIELDS}}
    event = None
    try:
        envelope = _WORKER.get("envelopes", {}).get(refparcela)
        event = _event_for(refparcela)
        summary, qa_passed = vm.simulate_verified_building(
            refparcela, out_dir,
            zero_policy=_WORKER["zero_policy"],
            gis_path=Path(_WORKER["prepared_gis"]),
            neighbors_path=Path(_WORKER["context_gis"]),
            floor_u=None if envelope is None else envelope.get("floor_u"),
            event=event,
            climate=_WORKER.get("climate"),
            config=_config_for(refparcela),
        )
    except Exception as exc:                       # noqa: BLE001 - isolation is the point
        return {**base, "status": "failed",
                "reason": type(exc).__name__,
                "message": str(exc)[:400],
                "traceback": traceback.format_exc()[-1200:],
                "seconds": round(time.time() - started, 1)}

    run_dir = out_dir / f"{refparcela}_deep"
    model_path = None
    if _WORKER.get("models_root"):
        # index first: pruning in summary mode would take the .osm away
        model_path = link_model(run_dir, refparcela, cluster,
                                Path(_WORKER["models_root"]))
    freed = 0
    if _WORKER["keep"] == "summary" and qa_passed:
        freed = _prune_run_dir(run_dir)

    # A building whose QA cross-check failed is not a result.  It used to be
    # written as `ok` and its energy went into the totals, while `qa_failed` was
    # reported beside them as if it were a note - the one rule the whole chain
    # advertises ("a failed gate fails the run") was the one not enforced.
    status = "ok" if qa_passed else "failed_qa"
    row = {**base, "status": status, "seconds": round(time.time() - started, 1),
           "pruned_bytes": freed, "model_osm": model_path}
    row.update({key: summary.get(key) for key in LEDGER_METRICS if key in summary})
    # Geometric annotation, not a simulation result: it was computed before the
    # run started and reaches the row without passing through the model.  Off
    # unless --eu was given, so a ledger written without it is unchanged.
    row.update(_WORKER.get("eu_flags", {}).get(refparcela, {}))
    if event is not None:
        # The offset this building actually ran at.  Without it the map of an
        # event run can colour the consequence but not the cause.
        row.update({"delta_peak_k": event["delta_peak_k"],
                    "delta_base_k": event["delta_base_k"]})
    return row


# ---------------------------------------------------------------------------
# Scope selection
# ---------------------------------------------------------------------------
def select_scope(stock: gpd.GeoDataFrame, scope: str, *,
                 district: str | None = None,
                 references: list[str] | None = None) -> gpd.GeoDataFrame:
    if scope == "all":
        return stock
    if scope == "clusters":
        # one median-sized building per cluster: the cheapest way to touch every
        # construction path before committing hours to a district
        picks = []
        for cluster, group in stock.groupby("cluster"):
            areas = group.geometry.area
            target = areas.median()
            picks.append(group.loc[(areas - target).abs().idxmin()].name)
        return stock.loc[picks]
    if scope == "district":
        if not district:
            raise ValueError("--district is required for --scope district")
        match = stock["nombre"].astype(str).str.upper() == district.upper()
        if not match.any():
            raise ValueError(f"no buildings in district {district!r}")
        return stock[match]
    if scope == "references":
        if not references:
            raise ValueError("--references is required for --scope references")
        wanted = {str(r).strip() for r in references}
        return stock[stock["refparcela"].isin(wanted)]
    raise ValueError(f"unknown scope {scope!r}")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
# Rai's own per-cluster consumption intensities: one constant broadcast to every
# building of the cluster (proven 2026-07-28 - 13 distinct values city-wide).
#
# SOURCE (2026-08-04): the doctoral thesis, Ilustración 31 (reviewed version
# `2503 Tesis UPV Raimon Calabuig RW LReig.pdf`, printed p. 95).  That figure
# gives all 21 clusters over 8 orientations plus a `Media` row; the `ConsumE`
# column in `DatosRai_ciudadValencia.shp` is that `Media` rounded to integers.
# Cross-checked: 19 of 21 clusters agree within rounding.
#
# Two shapefile values are NOT used, because they disagree with the thesis and
# the thesis is self-consistent where they are not:
#   * EdiPluriP02  shapefile 0     vs thesis 52.02 - the shapefile zero
#     contradicts the thesis's own demand table for that cluster (15.0 GWh/yr,
#     1,404 buildings, 5,589 residents), so it is a data error.  The "no
#     residents -> zero consumption" rule (thesis p. 96) applies to EdiPluriP07.
#   * BlocPluriP02 shapefile 52    vs thesis 47.81 - stale.
#
# The 7 VivUni clusters used to be missing here entirely, so 5,262 buildings
# (19.9 % of the stock) were never compared against a reference at all.
#
# Basis: these are per m2 of cadastral dwelling area (thesis annex:
# `area = df.groupby('31_pc')['442_sfc'].sum()`), which is what `vs_rai_pct`
# divides by.
RAI_CLUSTER_CONSUME = {
    "BlocPluriP01": 47.81, "BlocPluriP02": 47.81, "BlocPluriP03": 51.71,
    "BlocPluriP04": 46.55, "BlocPluriP05": 46.59, "BlocPluriP06": 45.00,
    "BlocPluriP07": 45.00,
    "EdiPluriP01": 54.36, "EdiPluriP02": 52.02, "EdiPluriP03": 52.04,
    "EdiPluriP04": 51.51, "EdiPluriP05": 45.84, "EdiPluriP06": 45.81,
    "EdiPluriP07": 45.81,
    "VivUniP01": 62.57, "VivUniP02": 55.82, "VivUniP03": 54.56,
    "VivUniP04": 50.79, "VivUniP05": 42.77, "VivUniP06": 38.00,
    "VivUniP07": 38.00,
}

# What the shapefile column carries, kept as the historical record so the
# difference above is auditable rather than asserted.
RAI_CLUSTER_CONSUME_SHAPEFILE = {
    "BlocPluriP01": 48, "BlocPluriP02": 52, "BlocPluriP03": 52, "BlocPluriP04": 47,
    "BlocPluriP05": 47, "BlocPluriP06": 45, "BlocPluriP07": 45,
    "EdiPluriP01": 54, "EdiPluriP02": 0, "EdiPluriP03": 52, "EdiPluriP04": 52,
    "EdiPluriP05": 46, "EdiPluriP06": 46, "EdiPluriP07": 46,
    "VivUniP01": 63, "VivUniP02": 56, "VivUniP03": 55, "VivUniP04": 51,
    "VivUniP05": 43, "VivUniP06": 38, "VivUniP07": 38,
}


def latest_per_reference(rows: list[dict]) -> list[dict]:
    """Collapse the ledger to one row per building - the last one written.

    The ledger is append-only and `--retry-failed` deliberately re-runs a
    failure, so a building can legitimately hold several rows.  Counting rows
    instead of buildings inflates the failure count, and would double-count a
    building's energy if a retry succeeded after a failure.
    """
    latest: dict[str, dict] = {}
    for row in rows:
        reference = row.get("refparcela")
        if reference is not None:
            latest[str(reference)] = row
    return list(latest.values())


def coverage_block(rows: list[dict], ok: list[dict],
                   stock: gpd.GeoDataFrame | None) -> dict:
    """How much of the scope the totals actually stand for.

    A total is summed over the buildings that produced a result, so it is not
    the district's energy - it is the energy of the modellable part of it.

    The area-weighted intensity is NOT neutral either, and this block used to
    claim it was, then claimed the opposite for too long.  Until 2026-08-12 the
    note here was a constant: exclusions concentrate in large buildings,
    Spearman -0.552, subset biased upward.  That was true of the Benicalap v3
    ledger, when the footprint ceiling was 5 000 m2 and the gates refused 123
    large buildings.  The ceiling went to 20 000 m2 on 2026-08-03, those
    buildings run now, and what is left over are the small ones the simplifier
    cannot hold - so the skew reversed while the sentence did not.

    A remembered constant printed beside a number it no longer describes is the
    failure mode this project keeps meeting.  So the direction is no longer
    asserted: `coverage_bias.bias_block` measures it from the run's own rows and
    bounds the truncation, and the note is written from that measurement.
    """
    in_scope = {str(r.get("refparcela")) for r in rows if r.get("refparcela")}
    produced = {str(r["refparcela"]) for r in ok}
    missing = in_scope - produced

    block = {
        "buildings_in_scope": len(in_scope),
        "buildings_with_result": len(produced),
        "buildings_without_result": len(missing),
        "building_coverage_pct": (round(100.0 * len(produced) / len(in_scope), 2)
                                  if in_scope else 0.0),
        "bias": cb.bias_block(rows, ok, stock),
    }
    # The prose lives where a reader meets the number, but it is now written
    # from the measurement rather than carried forward as a constant.
    measured_note = (block["bias"] or {}).get("note")
    block["note"] = measured_note or (
        "totals are summed over buildings_with_result only, so an absolute GWh "
        "figure under-reports by the missing share, and the area-weighted "
        "intensity is the EUI of the modellable subset rather than an unbiased "
        "estimate of the whole scope. This run does not carry what is needed to "
        "size that skew - see coverage.bias.reason - so no direction is claimed."
    )

    if stock is None or not in_scope or "refparcela" not in stock.columns:
        return block

    frame = stock.copy()
    frame["refparcela"] = frame["refparcela"].astype(str).str.strip()
    scoped = frame[frame["refparcela"].isin(in_scope)]
    if scoped.empty:
        return block

    if "footprint_area_m2" in scoped.columns:
        area = scoped["footprint_area_m2"].astype(float)
    else:
        # a prepared stock carries the column; the raw cadastre passed by
        # `--aggregate` does not, and letting the footprint share silently
        # disappear is how a coverage caveat stops being read
        area = sip.geometry_area_25830(scoped)

    total_footprint = float(area.sum())
    if total_footprint > 0:
        covered = float(area[scoped["refparcela"].isin(produced).values].sum())
        block["footprint_coverage_pct"] = round(100.0 * covered / total_footprint, 2)
    return block


def zoning_block(frame: pd.DataFrame) -> dict:
    """How much of a total rests on the weaker single-zone assumption.

    Every storey gets one well-mixed thermal zone.  On a deep plan that averages
    an internally-driven core with an envelope-driven perimeter, so the result is
    softer than for a normal block - and those buildings are exactly the large
    ones, which carry area out of all proportion to their count.

    Until 2026-08-03 the chain simply refused them at 5 000 m2, which dropped
    11.55 % of Valencia's floor area over a threshold nothing had measured.  They
    are now simulated and counted here instead, so a city total can say what
    share of itself stands on the weaker assumption rather than implying none of
    it does.
    """
    block = {"threshold_m2": db.LARGE_FOOTPRINT_SINGLE_ZONE_M2,
             "scheme": "one_well_mixed_zone_per_storey"}
    if "large_footprint_single_zone" not in frame or frame.empty:
        return block

    flagged = frame["large_footprint_single_zone"].fillna(False).astype(bool)
    area = frame["res_area_m2"]
    energy = frame["total_site_kwh_m2"] * area
    total_area = float(area.sum())
    total_energy = float(energy.sum())

    block.update({
        "buildings": int(flagged.sum()),
        "buildings_pct": round(100.0 * float(flagged.sum()) / len(frame), 2),
        "residential_area_pct": (round(100.0 * float(area[flagged].sum()) / total_area, 2)
                                 if total_area else 0.0),
        "total_site_pct": (round(100.0 * float(energy[flagged].sum()) / total_energy, 2)
                           if total_energy else 0.0),
        "note": ("buildings above the threshold have no core/perimeter split; "
                 "they are included in every total above and this is the share "
                 "they account for"),
    })
    return block


def provenance_block(rows: list[dict], ledger_paths: list[Path]) -> dict:
    """What produced these numbers, carried INSIDE the published result.

    The per-row identity was always in the ledger, but the aggregate JSON -
    the one file an outside reader actually opens - carried none of it, and a
    result assembled from more than one ledger (Benicalap v3 plus its warmup
    re-run) had no record of its parents or of the merge rule (review finding,
    2026-08-03).
    """
    identity: dict = {}
    for key in IDENTITY_FIELDS:
        values = sorted({str(r[key]) for r in rows if r.get(key) is not None})
        identity[key] = values[0] if len(values) == 1 else values
    return {
        "identity": identity,
        "source_ledgers": [{"path": str(p), "sha256": file_sha256(Path(p)),
                            "rows": sum(1 for _ in open(p, encoding="utf-8"))}
                           for p in ledger_paths if Path(p).exists()],
        "merge_rule": ("latest_per_reference: when a refparcela appears more "
                       "than once, the last row in ledger order wins"),
    }


def aggregate(rows: list[dict], stock: gpd.GeoDataFrame | None = None) -> dict:
    """Roll the ledger up to cluster / district / city totals.

    Areas and energies come straight from the per-building records; nothing is
    re-derived, so a total can always be traced back to the buildings behind it.
    """
    # Kept before the dedupe: `energy_period` must see the ledger as written.
    # It reads the run mode, and a superseded row can only ever raise the mixed
    # alarm, never suppress it - whereas answering from deduped rows here while
    # `write_results_layer` answered from raw ones let one file carry two
    # different periods (review finding, 2026-08-12).
    raw_rows = list(rows)
    rows = latest_per_reference(rows)
    # Only `ok` feeds the totals: `failed_qa` rows carry numbers, but numbers
    # that failed their own cross-check against EnergyPlus.
    ok = [r for r in rows if r.get("status") == "ok"]
    if not ok:
        # The empty report carries the FULL schema at zero.  It used to omit
        # qa_failed/unexplained_severes, so a run in which every building
        # failed died in _print_report with KeyError instead of reporting the
        # failure it was built to report (review finding, 2026-08-03).
        return {"buildings_ok": 0,
                "buildings_failed": sum(1 for r in rows
                                        if r.get("status") == "failed"),
                "buildings_failed_qa": sum(1 for r in rows
                                           if r.get("status") == "failed_qa"),
                "buildings_excluded": sum(1 for r in rows
                                          if r.get("status") == "excluded"),
                "coverage": coverage_block(rows, ok, stock),
                "energy_period": rl.energy_period(raw_rows),
                "zoning": zoning_block(pd.DataFrame(ok)),
                "fragmentation": euf.fragmentation_block(pd.DataFrame(ok)),
                "floor_area_allocation": faa.allocation_block(ok, stock),
                "qa_failed": 0,
                "unexplained_severes": 0,
                "implausible_occupancy": 0,
                "totals": {},
                "by_cluster": [],
                "by_district": [],
                "seconds_per_building": {}}

    frame = pd.DataFrame(ok)
    if stock is not None:
        # A cadastral reference can contain more than one footprint row.  The
        # cluster/district attributes are parcel-level metadata, so collapse
        # that lookup before mapping it onto the one-row-per-reference ledger.
        # Joining the raw, non-unique index would duplicate energy totals.
        # `nombre` is Valencia's administrative district.  A stock from a city
        # that names no districts simply has no such column, and demanding one
        # would refuse the whole aggregate over a field nothing needs.
        attributes = [name for name in ("cluster", "nombre") if name in stock.columns]
        lookup = (
            stock[["refparcela", *attributes]]
            .drop_duplicates(subset=["refparcela"], keep="first")
            .set_index("refparcela")
        )
        # Schema-2 ledgers did not carry cluster, while current rows do.  Fill
        # either shape from the exact prepared stock without creating pandas'
        # overlapping-column error or replacing a value already frozen on the
        # ledger row.
        for column in attributes:
            fallback = frame["refparcela"].map(lookup[column])
            if column in frame.columns:
                frame[column] = frame[column].where(frame[column].notna(), fallback)
            else:
                frame[column] = fallback

    area = frame["res_area_m2"]
    energy_cols = {"heating": "space_heating_kwh_m2", "cooling": "cooling_kwh_m2",
                   "dhw": "dhw_kwh_m2", "total_site": "total_site_kwh_m2"}
    totals = {f"{name}_gwh": round(float((frame[col] * area).sum()) / 1e6, 5)
              for name, col in energy_cols.items()}
    totals["residential_area_m2"] = round(float(area.sum()), 1)
    totals["area_weighted_total_site_kwh_m2"] = round(
        float((frame["total_site_kwh_m2"] * area).sum() / area.sum()), 3)
    conditioned = (pd.to_numeric(frame["total_conditioned_area_m2"], errors="coerce")
                   if "total_conditioned_area_m2" in frame else area)
    totals["conditioned_area_m2"] = round(float(conditioned.fillna(area).sum()), 1)
    # The dwellings against their own floor area.  The figure above divides ALL
    # the energy - commercial storeys included - by residential area only; that
    # is Rai's convention and it is kept for the comparison against him, but on
    # a block with more shop floor than housing it is an accounting ratio rather
    # than an intensity.  This one is the physical number.
    if "residential_total_site_kwh_m2" in frame:
        residential_eui = pd.to_numeric(
            frame["residential_total_site_kwh_m2"], errors="coerce")
        if residential_eui.notna().any():
            covered = residential_eui.notna()
            totals["residential_total_site_kwh_m2"] = round(
                float((residential_eui[covered] * area[covered]).sum()
                      / area[covered].sum()), 3)
            totals["residential_site_gwh"] = round(
                float((residential_eui[covered] * area[covered]).sum()) / 1e6, 5)
            if "terciario_site_kwh" in frame:
                totals["terciario_site_gwh"] = round(
                    float(pd.to_numeric(frame["terciario_site_kwh"], errors="coerce")
                          .fillna(0.0).sum()) / 1e6, 5)
    # Absent from every ledger written before 2026-08-08; those runs are still
    # readable, they simply do not report a cap that did not exist yet.
    if "storey_cap_applied" in frame:
        capped = frame["storey_cap_applied"].fillna(False).astype(bool)
        totals["storey_capped_buildings"] = int(capped.sum())
        totals["storey_capped_energy_pct"] = round(
            100.0 * float((frame["total_site_kwh_m2"] * area)[capped].sum())
            / float((frame["total_site_kwh_m2"] * area).sum()), 2)
    # Which denominator every kWh/m2 above is on, stated rather than assumed.
    totals["area_basis"] = "geometric_residential_storeys"
    totals["area_basis_note"] = (
        "area_weighted_total_site_kwh_m2 and cadastral_total_site_kwh_m2 divide "
        "ALL the site energy, commercial storeys included, by residential area "
        "only.  That is Rai's own convention - his ground Terciario is "
        "conditioned but out of the floor-area basis - so the vs_rai_* figures "
        "are computed that way and stay comparable.  It is NOT the floor the "
        "model conditions: conditioned_area_m2 is the area actually simulated, "
        "and residential_total_site_kwh_m2 is the dwellings' own energy over "
        "their own area, which is the physical intensity.  An earlier version of "
        "this note called the residential basis 'the floor the model actually "
        "conditions', which was false (corrected 2026-08-08).  Rai's ConsumE "
        "constants are per CADASTRAL dwelling area (thesis annex: "
        "groupby('31_pc')['442_sfc'].sum()), so vs_rai_* uses the cadastral "
        "denominator; the earlier claim that the geometric basis matched 'Rai's "
        "own 954.80 m2' came from his trial box, not his city method "
        "(corrected 2026-08-04).")
    if "tipo15_res_area_m2" in frame:
        tipo15 = pd.to_numeric(frame["tipo15_res_area_m2"], errors="coerce")
        if tipo15.notna().any():
            totals["tipo15_residential_area_m2"] = round(float(tipo15.sum()), 1)
            totals["tipo15_vs_geometric_pct"] = round(
                float(area.sum() / tipo15.sum() - 1.0) * 100, 2)
            # The same energy on the basis Rai's constants are defined on, so a
            # reader never has to guess which denominator a kWh/m2 is on.
            totals["cadastral_total_site_kwh_m2"] = round(
                float((frame["total_site_kwh_m2"] * area).sum() / tipo15.sum()), 3)
    totals["carbon_total_site_t_yr"] = round(
        float((frame["total_site_co2_kg_m2"] * area).sum()) / 1000.0, 1)

    def group_block(key: str) -> list[dict]:
        if key not in frame.columns:
            return []
        out = []
        for name, group in frame.groupby(key):
            group_area = group["res_area_m2"]
            intensity = float((group["total_site_kwh_m2"] * group_area).sum()
                              / group_area.sum())
            energy_kwh = float((group["total_site_kwh_m2"] * group_area).sum())
            block = {key: name, "buildings": int(len(group)),
                     "residential_area_m2": round(float(group_area.sum()), 1),
                     "total_site_gwh": round(energy_kwh / 1e6, 5),
                     "area_weighted_kwh_m2": round(intensity, 3)}
            # Rai's constants are per m2 of CADASTRAL dwelling area (thesis
            # annex: `area = groupby('31_pc')['442_sfc'].sum()`), so comparing
            # our geometric intensity against them compares two different
            # quantities.  Measured 2026-08-04: on the geometric basis the
            # district looked +1 % against the reference while its total energy
            # was +41 % - an intensity coincidence produced by a larger
            # denominator.  The comparison therefore runs on the cadastral
            # basis, and the energy ratio is reported next to it because that
            # one is basis-free.
            if key == "cluster" and name in RAI_CLUSTER_CONSUME:
                rai = RAI_CLUSTER_CONSUME[name]
                cadastral = pd.to_numeric(group.get("tipo15_res_area_m2"),
                                          errors="coerce")
                cad_sum = float(cadastral.sum()) if cadastral is not None else 0.0
                block["rai_consume_kwh_m2"] = rai
                block["cadastral_area_m2"] = round(cad_sum, 1)
                if cad_sum > 0:
                    cad_intensity = energy_kwh / cad_sum
                    block["cadastral_kwh_m2"] = round(cad_intensity, 3)
                    block["vs_rai_pct"] = (round((cad_intensity - rai) / rai * 100, 2)
                                           if rai else None)
                    # basis-free: our kWh against the kWh Rai's own city method
                    # would assign to exactly these buildings
                    block["vs_rai_energy_ratio"] = (
                        round(energy_kwh / (rai * cad_sum), 4) if rai else None)
                else:
                    block["cadastral_kwh_m2"] = None
                    block["vs_rai_pct"] = None
                    block["vs_rai_energy_ratio"] = None
            out.append(block)
        return sorted(out, key=lambda b: -b["total_site_gwh"])

    return {
        "buildings_ok": len(ok),
        "buildings_failed": sum(1 for r in rows if r.get("status") == "failed"),
        "buildings_failed_qa": sum(1 for r in rows
                                   if r.get("status") == "failed_qa"),
        "buildings_excluded": sum(1 for r in rows if r.get("status") == "excluded"),
        "coverage": coverage_block(rows, ok, stock),
        # Beside the totals, not only inside the map block: a reader looking at
        # `total_site_gwh` has to be able to see what period it covers.
        "energy_period": rl.energy_period(raw_rows),
        "zoning": zoning_block(frame),
        "fragmentation": euf.fragmentation_block(frame),
        "floor_area_allocation": faa.allocation_block(ok, stock),
        "qa_failed": int((~frame["qa_all_passed"].astype(bool)).sum())
        if "qa_all_passed" in frame else 0,
        "unexplained_severes": int(frame.get("severes_unexplained",
                                             pd.Series(dtype=float)).fillna(0).gt(0).sum()),
        "implausible_occupancy": int(
            frame.get("occupancy_plausibility", pd.Series(dtype=str))
            .astype(str).str.startswith("implausible").sum()),
        "totals": totals,
        "by_cluster": group_block("cluster"),
        "by_district": group_block("nombre"),
        "seconds_per_building": {
            "median": round(float(frame["seconds"].median()), 1),
            "mean": round(float(frame["seconds"].mean()), 1),
            "max": round(float(frame["seconds"].max()), 1),
        } if "seconds" in frame else {},
    }


# ---------------------------------------------------------------------------
# Swappable inputs
# ---------------------------------------------------------------------------
def load_policy(policy_path: Path | None) -> sip.StockInputPolicy:
    """Read a data policy from JSON, or use the built-in default."""
    if policy_path is None:
        return sip.StockInputPolicy()
    spec = json.loads(Path(policy_path).read_text(encoding="utf-8"))
    spec.pop("_notes", None)          # the file is meant to be read by people too
    return sip.StockInputPolicy.from_dict(spec)


def build_config_for(climate, template):
    """A BuildConfig pointing at the chosen template and weather file.

    Returns None when neither is swapped, so the default path stays exactly
    what it has always been rather than a rebuilt copy of itself.
    """
    if climate is None and template is None:
        return None
    config = mb.DEFAULT_BUILD_CONFIG.model_copy(deep=True)
    if template is not None:
        config.data.template_path = template.resolved_path
    if climate is not None:
        config.data.epw_path = climate.epw_path
    return config


def input_fingerprints(climate, template, policy_fingerprint: str, *,
                       profile_fingerprint: str, zero_policy: str,
                       stock_source_fingerprint: str) -> dict:
    """Everything that decides what a row in the ledger means.

    The default path used to stamp the literal strings `valencia_iwec:builtin`
    and `plantilla_v2:builtin` instead of real hashes, so a change to the EPW or
    the template on disk would not have been noticed by anything.  Both are now
    always loaded through their own validators, so the default run is hashed
    exactly like an overridden one.
    """
    stamps = {
        "climate_fingerprint": climate.fingerprint,
        "climate_name": climate.name,
        "template_fingerprint": template.fingerprint,
        "policy_fingerprint": policy_fingerprint,
        "profile_fingerprint": profile_fingerprint,
        "zero_policy": zero_policy,
        "stock_source_fingerprint": stock_source_fingerprint,
        "runner_schema": RUNNER_SCHEMA,
    }
    stamps["run_identity"] = hashlib.sha256(
        json.dumps(stamps, sort_keys=True).encode("utf-8")).hexdigest()
    return stamps


# Every field the resume guard compares.  A ledger written with any of these
# different describes a different physical or data situation, and continuing
# into it would average two of them into one total.
IDENTITY_FIELDS = ("run_identity", "profile_fingerprint", "climate_fingerprint",
                   "template_fingerprint", "policy_fingerprint", "zero_policy",
                   "stock_source_fingerprint", "runner_schema")


class InputMismatch(ValueError):
    """A ledger was written with different inputs than this run is using."""


def assert_ledger_matches_inputs(previous: list[dict], fingerprints: dict) -> None:
    """Refuse to resume a ledger that was written with different inputs.

    Without this, `--resume` after changing the climate would append the new
    climate's buildings to the old climate's rows and aggregate the two into a
    single total - two different physical situations averaged into one number,
    with nothing in the output saying so.
    """
    mismatches: list[str] = []
    unstamped = [row for row in previous if row.get("run_identity") is None]
    if unstamped:
        # This used to be tolerated so older ledgers could be continued. It
        # cannot be: an unstamped row carries no evidence of which profile,
        # climate or data policy produced it, and the Benicalap ledger really
        # was written under a different profile than the one running today.
        mismatches.append(
            f"{len(unstamped)} of {len(previous)} rows predate run identity "
            f"stamping, so what produced them cannot be established")
    for key in IDENTITY_FIELDS:
        earlier = {row.get(key) for row in previous if row.get(key) is not None}
        if not earlier:
            continue
        if earlier != {fingerprints[key]}:
            mismatches.append(
                f"{key}: ledger has {sorted(str(v) for v in earlier)}, this run "
                f"has {fingerprints[key]}")
    if mismatches:
        raise InputMismatch(
            "REFUSED to resume: this ledger was written with different inputs, so "
            "continuing would mix two different situations into one total.\n  "
            + "\n  ".join(mismatches)
            + "\nStart a new --out-dir instead.")


class _StallWatchdog:
    """Report buildings that are still running well past the normal time.

    Deliberately a reporter, not a killer. EnergyPlus runs inside the worker as
    a single blocking C call (`api.runtime.run_energyplus`), so a Python-level
    timeout cannot interrupt it - a `signal.alarm` would only fire once the call
    already returned, i.e. never for the case it is meant to catch. Killing the
    worker instead would put `ProcessPoolExecutor` into a broken state and take
    the whole run down with it.

    What it can do is make a stall visible. Measured on Benicalap: median 62 s,
    P95 223 s, worst 3 768 s, 19 of 957 over 600 s - slow buildings are real and
    finite, and the danger in an unattended multi-day run is not that one is
    slow but that nobody can tell whether it is slow or stuck.
    """

    def __init__(self, futures: dict, slow_seconds: float, interval: float = 60.0):
        self._started = {ref: time.time() for ref in futures.values()}
        self._slow_seconds = slow_seconds
        self._interval = interval
        self._reported: set[str] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="stall-watchdog")

    def start(self) -> None:
        if self._slow_seconds > 0:
            self._thread.start()

    def finished(self, ref: str) -> None:
        self._started.pop(ref, None)

    def stop(self) -> None:
        self._stop.set()

    def slow_now(self) -> list[tuple[str, float]]:
        now = time.time()
        return [(ref, now - since) for ref, since in list(self._started.items())
                if now - since > self._slow_seconds]

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            for ref, seconds in self.slow_now():
                if ref in self._reported:
                    continue
                self._reported.add(ref)
                log.warning("[slow] %s has been running %.0f min (over the %.0f min "
                            "mark) - still alive, not stuck",
                            ref, seconds / 60, self._slow_seconds / 60)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _window_days(window, frame) -> int:
    """How many calendar days the event run period spans in this weather file."""
    month = pd.to_numeric(frame[1], errors="coerce").astype(int)
    day = pd.to_numeric(frame[2], errors="coerce").astype(int)
    key = month * 100 + day
    begin, end = window[0] * 100 + window[1], window[2] * 100 + window[3]
    return int(key[(key >= begin) & (key <= end)].nunique())


PINNED_ENVELOPE_COLUMNS = ("wall_u", "roof_u", "window_u")


def envelopes_by_reference(stock: gpd.GeoDataFrame) -> dict[str, dict]:
    """Per-building envelopes, for a stock that states its own.

    Empty for a stock that does not, which is what keeps the Valencia path on
    the cluster/TABULA_ES route it was verified on.  `floor_u` travels with the
    envelope because it is the same kind of statement about the same building,
    but it is optional: a stock may price its walls and not its slab.
    """
    if not set(PINNED_ENVELOPE_COLUMNS) <= set(stock.columns):
        return {}
    columns = list(PINNED_ENVELOPE_COLUMNS)
    if "floor_u" in stock.columns:
        columns.append("floor_u")
    envelopes: dict[str, dict] = {}
    for row in stock[["refparcela", *columns]].itertuples(index=False):
        values = {name: getattr(row, name) for name in columns}
        if any(pd.isna(values[name]) for name in PINNED_ENVELOPE_COLUMNS):
            continue                     # unusable: let the engine refuse it loudly
        if "floor_u" in values and pd.isna(values["floor_u"]):
            values.pop("floor_u")
        envelopes[str(row.refparcela)] = values
    return envelopes


def load_prepared_stock(stock_path: Path) -> tuple[gpd.GeoDataFrame, dict]:
    """A stock file that already carries what the engine reads.

    The cadastre policy is deliberately not run over it: the policy exists to
    derive these fields from a raw cadastre plus a dwelling ledger, and a file
    that states them has nothing to derive.
    """
    stock = gpd.read_file(stock_path)
    counters = {
        "policy_fingerprint": "prepared_stock",
        "stock_source_fingerprint": file_sha256(Path(stock_path)),
        "gis_path": str(stock_path),
        "tipo15_path": None,
        "imputed_floor_buildings": 0,
        "residential_area_proxy_buildings": 0,
        "duplicate_parcel_rows": int(
            len(stock) - stock["refparcela"].astype(str).nunique()),
    }
    return stock, counters


def run_stock(*, scope: str, out_dir: Path, workers: int,
              gis_path: Path | None = None, tipo15_path: Path | None = None,
              stock_path: Path | None = None, var_dir: Path,
              zero_policy: str = "literal_zero", keep: str = "full",
              district: str | None = None, references: list[str] | None = None,
              resume: bool = False, retry_failed: bool = False,
              limit: int | None = None,
              climate_path: Path | None = None,
              policy_path: Path | None = None,
              template_path: Path | None = None,
              eu_path: Path | None = None,
              microclimate_path: Path | None = None,
              height_token: str | None = None,
              spinup_days: int | None = None,
              slow_seconds: float = 900.0) -> dict:
    # a drifted profile must stop the run before a single building is written
    vm.assert_profile_intact()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = out_dir / "ledger.jsonl"

    # Every swappable input is validated up front, so a bad climate or a
    # template missing an object fails in the first second rather than
    # somewhere inside building 4 000.
    # Both are ALWAYS loaded through their validators, override or not, so the
    # default run is hashed and gated exactly like an overridden one.
    climate = cl.load_climate(climate_path or cl.VALENCIA_BUNDLE)
    template = tpl.load_template_set(template_path or tpl.DEFAULT_TEMPLATE,
                                     Path(var_dir) / "templates")
    policy = load_policy(policy_path)
    config = build_config_for(climate, template)

    if climate is not None:
        log.info("[climate] %s | %s | %s", climate.name, climate.site["city"],
                 climate.cross_check)
    if template is not None:
        log.info("[template] %s | %s aliases | normalised=%s", template.name,
                 len(template.aliases), template.normalised)

    if stock_path is not None:
        prepared_gis = Path(stock_path)
        stock, counters = load_prepared_stock(prepared_gis)
        # The stock is its own shading context: there is no separate raw
        # cadastre behind it to read neighbours from.
        context_gis = prepared_gis
    else:
        if gis_path is None or tipo15_path is None:
            raise InputMismatch(
                "REFUSED: a run needs either --stock (a prepared file) or both "
                "--gis and --tipo15 (a cadastre and its dwelling ledger).")
        prepared_gis, stock, counters = prepare_stock_file(
            gis_path, tipo15_path, policy, var_dir)
        context_gis = Path(gis_path)
    # key names come from stock_input_policy.prepare_stock; getting them wrong
    # printed "None" for both counts instead of failing, so the banner quietly
    # stopped reporting the data-quality numbers it claims to report
    for key in ("imputed_floor_buildings", "residential_area_proxy_buildings",
                "duplicate_parcel_rows"):
        assert key in counters, f"stock counter '{key}' is gone - fix the banner"

    # A stock that prices its own construction is telling the engine not to
    # consult the Spanish table.  Empty for the cadastre path.
    envelopes = envelopes_by_reference(stock)
    if envelopes:
        log.info("[envelope] %s buildings carry their own U-values | the Spanish "
                 "TABULA table is not consulted for them", len(envelopes))
    log.info("[stock] %s buildings after policy | floors imputed %s | area proxy %s "
             "| duplicate rows %s",
             len(stock), counters["imputed_floor_buildings"],
             counters["residential_area_proxy_buildings"],
             counters["duplicate_parcel_rows"])

    # Geometric annotation only.  Computed once here, handed to the workers as
    # data, and merged into the ledger row after the simulation has already
    # returned - it cannot reach the model.  Absent unless asked for, so the
    # distributed product (which ships no EU file) runs exactly as before.
    eu_flags: dict[str, dict] = {}
    if eu_path is not None:
        eu_flags = euf.flags_by_reference(stock, Path(eu_path))
        multi = sum(1 for f in eu_flags.values() if f.get("eu_multi_footprint"))
        log.info("[eu] %s | %s parcels carry >=2 EU footprints (%.2f %%) | "
                 "flag only, no physics changes",
                 Path(eu_path).name, multi, 100.0 * multi / max(len(eu_flags), 1))

    # A microclimate slice covers a piece of the city, not the city.  It is
    # therefore a second, narrower gate applied before the scope is fixed, so
    # the reduced coverage is visible in the plan rather than discovered as a
    # wall of failures once the run is under way.
    event_plan = None
    if microclimate_path is not None:
        import microclimate as mcl

        slice_ = mcl.load_slice(Path(microclimate_path),
                                height_token=height_token or mcl.DEFAULT_HEIGHT_TOKEN)
        deltas = mcl.sample_stock(slice_, stock)
        covered = deltas[deltas["sample_status"] == "ok"]
        _, frame = mcl.read_epw(climate.epw_path)
        window = mcl.hottest_window(
            frame, spinup_days=(spinup_days if spinup_days is not None
                                else mcl.DEFAULT_SPINUP_DAYS))
        days = _window_days(window, frame)
        inside = set(covered["refparcela"].astype(str))
        stock = stock[stock["refparcela"].astype(str).isin(inside)]
        log.info("[microclimate] %s: %s of %s buildings inside the slice; "
                 "event window %02d-%02d to %02d-%02d (%s days)",
                 slice_.name, len(covered), len(deltas),
                 window[0], window[1], window[2], window[3], days)
        event_plan = {
            "slice_record": slice_.record(),
            "base_epw": str(climate.epw_path),
            "epw_dir": str(out_dir / "event_epw"),
            "window": list(window), "days": days,
            "deltas": {str(r.refparcela): {
                "delta_peak_k": round(float(r.delta_peak_k), 4),
                "delta_base_k": round(float(r.delta_base_k), 4),
                "sample_radius_m": float(r.sample_radius_m),
                "sample_cells": int(r.sample_cells)}
                for r in covered.itertuples()},
        }

    scoped = select_scope(stock, scope, district=district, references=references)
    runnable, excluded = screen_geometry(scoped)
    if limit:
        # Deterministically shuffled before truncating.  A stock arrives in the
        # order its source was assembled, which tracks location and therefore
        # building type; taking a prefix of that makes a limited run a biased
        # sample of the city - and a long run WILL be read before it finishes.
        # With a fixed seed every prefix is an unbiased random sample and the
        # selection is still reproducible.
        import random

        shuffled = list(runnable)
        random.Random(42).shuffle(shuffled)
        runnable = shuffled[:limit]

    fingerprints = input_fingerprints(
        climate, template, counters["policy_fingerprint"],
        profile_fingerprint=vm.profile_fingerprint(),
        zero_policy=zero_policy,
        stock_source_fingerprint=counters["stock_source_fingerprint"])

    # The ledger is read whether or not this is a resume: without it, a second
    # run into the same directory appended to the first one's rows and the two
    # were aggregated together, with nothing recording that they were different
    # runs.
    previous = read_ledger(ledger_path)
    if previous and not (resume or retry_failed):
        raise InputMismatch(
            f"REFUSED: {ledger_path} already holds {len(previous)} rows.\n  "
            f"Pass --resume to continue that run, or choose a new --out-dir. "
            f"Appending silently would mix two runs into one total.")
    if previous:
        assert_ledger_matches_inputs(previous, fingerprints)
    done = completed_references(previous)
    # Captured before the resume filter runs, because that filter is about to
    # remove everything already finished.  `runnable`/`excluded` below then
    # describe the REMAINING work, which is the right thing to log and the
    # wrong thing to use as a progress denominator: the numerator is the
    # ledger's cumulative row count, so a resumed run reported 268 % (measured,
    # ALL-VALENC-A) and was only saved from showing it by a clamp.
    scope_total = len(runnable) + len(excluded)
    if retry_failed:
        failed_before = {r["refparcela"] for r in previous if r.get("status") == "failed"}
        runnable = [r for r in runnable if r in failed_before or r not in done]
    elif resume:
        runnable = [r for r in runnable if r not in done]
    pending_excluded = [e for e in excluded if e["refparcela"] not in done]

    log.info("[scope] %s -> %s runnable, %s excluded, %s already done",
             scope, len(runnable), len(excluded), len(done))

    worker_config = {
        "out_dir": str(out_dir / "runs"),
        "models_root": str(out_dir / "models"),
        "prepared_gis": str(prepared_gis),
        # For the cadastre path the raw shapefile stays the shading context on
        # purpose: the prepared file's imputed floor counts would otherwise give
        # 1 641 buildings a shadow they do not cast.  A prepared stock has no
        # such second file and is its own context.
        "context_gis": str(context_gis),
        "zero_policy": zero_policy,
        "keep": keep,
        "tmp_root": str(Path(var_dir) / "stock_tmp"),
        "profile_fingerprint": vm.profile_fingerprint(),
        # a ClimateSet and a BuildConfig both pickle, so each worker gets the
        # already-validated objects rather than re-reading and re-checking the
        # files 26 452 times
        "climate": climate,
        "config": config,
        "eu_flags": eu_flags,
        "envelopes": envelopes,
        "event_plan": event_plan,
        **fingerprints,
    }
    # the worker config carries live objects; the record on disk carries their
    # descriptions, so a finished run can always be read back without them
    # the worker config carries 26 452 flag dicts and two live objects; the
    # record on disk carries their descriptions, so a finished run can be read
    # back without them and run_config.json stays a page long
    recorded_config = {key: value for key, value in worker_config.items()
                       if key not in ("climate", "config", "eu_flags", "envelopes",
                                      "event_plan")}
    if event_plan is not None:
        # The per-building offsets are bulky; the slice's identity is what a
        # reader needs to know which field produced these numbers.
        recorded_config["microclimate"] = {
            "slice": event_plan["slice_record"],
            "window": event_plan["window"], "days": event_plan["days"],
            "buildings_covered": len(event_plan["deltas"]),
        }
    recorded_config["eu_source"] = (
        {"path": str(eu_path), "fingerprint": euf.source_fingerprint(Path(eu_path)),
         "rule_version": euf.RULE_VERSION, "annotated_references": len(eu_flags)}
        if eu_path is not None else None)
    (out_dir / "run_config.json").write_text(
        json.dumps({"scope": scope, "district": district, "workers": workers,
                    "zero_policy": zero_policy, "keep": keep,
                    "worker_config": recorded_config, "stock_counters": counters,
                    "climate": climate.record() if climate else None,
                    "template": template.record() if template else None,
                    "policy": dataclasses.asdict(policy),
                    "runnable": len(runnable), "excluded": len(excluded),
                    # The whole scope, not this invocation's share of it.
                    "scope_total": scope_total},
                   indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    models_root = out_dir / "models"
    models_root.mkdir(parents=True, exist_ok=True)
    (models_root / "OKU.md").write_text(MODEL_INDEX_README, encoding="utf-8")

    started = time.time()
    with Ledger(ledger_path) as ledger:
        for item in pending_excluded:
            # an exclusion is a ledger row like any other and carries the same
            # identity, or resume could not tell which run excluded it
            ledger.append({**item, "status": "excluded",
                           **_event_stamp(event_plan),
                           **{key: fingerprints[key] for key in IDENTITY_FIELDS}})

        if runnable:
            done_count = 0
            clusters = (stock.set_index("refparcela")["cluster"].astype(str)
                        .to_dict() if "cluster" in stock.columns else {})
            with ProcessPoolExecutor(max_workers=workers,
                                     initializer=_init_worker,
                                     initargs=(worker_config,)) as pool:
                futures = {pool.submit(run_one, (ref, clusters.get(ref))): ref
                           for ref in runnable}
                watchdog = _StallWatchdog(futures, slow_seconds)
                watchdog.start()
                try:
                    for future in as_completed(futures):
                        ref = futures[future]
                        try:
                            row = future.result()
                        except Exception as exc:   # noqa: BLE001 - worker died
                            # Stamped like every other row: an unstamped crash
                            # row made the whole ledger unresumable, because
                            # assert_ledger_matches_inputs rightly refuses rows
                            # whose producing run cannot be established - one
                            # dead worker cost the remaining days of a stock run
                            # (review finding, 2026-08-03).
                            # The event stamp belongs here too, and for the
                            # same reason as the identity: without it one dead
                            # worker leaves a single unstamped row, the ledger
                            # reads as two run modes, and the whole event run
                            # is reported as `mixed` - reintroducing exactly
                            # the annual mislabel the stamp exists to prevent
                            # (review finding, 2026-08-12).
                            row = {"refparcela": ref, "status": "failed",
                                   "reason": f"worker_{type(exc).__name__}",
                                   "message": str(exc)[:400],
                                   **_event_stamp(event_plan),
                                   **{key: fingerprints[key] for key in IDENTITY_FIELDS}}
                        watchdog.finished(ref)
                        ledger.append(row)
                        done_count += 1
                        elapsed = time.time() - started
                        rate = elapsed / done_count
                        log.info("[%4d/%4d] %s %-8s %5.1fs | eta %.1f min",
                                 done_count, len(runnable), ref, row["status"],
                                 row.get("seconds", 0),
                                 (len(runnable) - done_count) * rate / 60)
                finally:
                    watchdog.stop()

    rows = read_ledger(ledger_path)
    report = aggregate(rows, stock)
    report["elapsed_minutes"] = round((time.time() - started) / 60, 2)
    report["ledger"] = str(ledger_path)
    report["provenance"] = provenance_block(rows, [ledger_path])
    # The map of the result, written next to the numbers rather than left for
    # the reader to reconstruct with a spreadsheet join.  The full export packs
    # the run directory, so it travels with the signed package automatically.
    report["results_layer"] = rl.write_results_layer(rows, stock, out_dir)
    (out_dir / "aggregate.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def _print_report(report: dict) -> None:
    print()
    print(f"  ok {report['buildings_ok']} | failed {report['buildings_failed']} "
          f"| excluded {report['buildings_excluded']} "
          f"| QA fail {report['qa_failed']} "
          f"| unexplained severe {report['unexplained_severes']}")
    period = report.get("energy_period") or {}
    if period.get("period") not in (None, "annual", "no_rows"):
        # Printed above the totals, not below: the qualifier has to be read
        # before the numbers it qualifies, and an event window is not a year.
        print(f"  ENERGY PERIOD: {period.get('unit') or period['period']}"
              + (f" ({period['event_window']})" if period.get("event_window") else ""))
    totals = report.get("totals") or {}
    if totals:
        print(f"  heating {totals['heating_gwh']} GWh | cooling {totals['cooling_gwh']} "
              f"| DHW {totals['dhw_gwh']} | total site {totals['total_site_gwh']} GWh")
        print(f"  residential area {totals['residential_area_m2']:,.0f} m² | "
              f"area-weighted {totals['area_weighted_total_site_kwh_m2']} kWh/m² "
              f"(geometric basis)")
        if totals.get("cadastral_total_site_kwh_m2") is not None:
            print(f"  cadastral area  {totals.get('tipo15_residential_area_m2', 0):,.0f} m² | "
                  f"area-weighted {totals['cadastral_total_site_kwh_m2']} kWh/m² "
                  f"(Rai's basis - what vs-Rai uses)")
    # Printed immediately under the cadastral basis because it qualifies that
    # exact number: the denominator is the recorded dwelling area, the numerator
    # carries energy delivered to modelled floor no record asks for.
    alloc = report.get("floor_area_allocation") or {}
    if alloc.get("measured"):
        rounding = alloc["integer_storey_rounding"]
        print(f"  modelled floor {alloc['gap_pct_of_cadastral']:+.2f}% against the "
              f"cadastral record ({alloc['gap_m2']:,.0f} m² on "
              f"{alloc['buildings_measured']} buildings); "
              f"{rounding['share_of_gap_pct']:.1f}% of it is the storey rule "
              f"rounding up on {rounding['buildings']}")
        profile = alloc.get("loads_on_rounding_excess") or {}
        if profile.get("dwelling_loads_on_excess") is False:
            print(f"    excess carries no dwelling loads: the top storey was "
                  f"scaled to its recorded fraction on "
                  f"{profile.get('scaled_buildings')} buildings")
        band = (alloc.get("energy_on_rounding_excess") or {}).get("band_pct")
        if band:
            print(f"    energy standing on that excess: {band[0]:.2f}-{band[1]:.2f}% "
                  f"of the total above")
    frag = report.get("fragmentation") or {}
    if frag.get("buildings_measured"):
        print(f"  multi-mass parcels {frag['buildings']} "
              f"({frag['buildings_pct']}% of buildings, "
              f"{frag['residential_area_pct']}% of area, "
              f"{frag['total_site_pct']}% of site energy) "
              f"- flag only, measured on {frag['buildings_measured']}")
    if report.get("seconds_per_building"):
        spb = report["seconds_per_building"]
        print(f"  per building: median {spb['median']}s | mean {spb['mean']}s | max {spb['max']}s")
    if report.get("by_cluster"):
        # Two bases in one table would mislead, so both are named: `geo` is the
        # floor the model conditions, `kadastro` is the basis Rai's constants
        # are defined on and the only one `fark` may be read against.
        print(f"  {'cluster':16s} {'n':>6s} {'geo':>8s} {'kadastro':>9s} "
              f"{'Rai':>7s} {'fark':>8s} {'enerji':>7s}")
        for block in report["by_cluster"]:
            rai = block.get("rai_consume_kwh_m2")
            delta = block.get("vs_rai_pct")
            cad = block.get("cadastral_kwh_m2")
            ratio = block.get("vs_rai_energy_ratio")
            print(f"  {block['cluster']:16s} {block['buildings']:6d} "
                  f"{block['area_weighted_kwh_m2']:8.2f} "
                  f"{'' if cad is None else f'{cad:.2f}':>9} "
                  f"{'' if rai is None else f'{rai:.2f}':>7} "
                  f"{'' if delta is None else f'{delta:+.1f}%':>8} "
                  f"{'' if ratio is None else f'{ratio:.2f}x':>7}")
    print(f"  elapsed {report.get('elapsed_minutes')} min")



def _carry_forward_layer(block: dict, out_dir: Path) -> dict:
    """Keep the record of a layer this pass could not write but did not remove.

    `aggregate.json` is rewritten whole, so re-aggregating a finished run
    without `--stock` would otherwise stamp `written: false` onto a directory
    that still holds the GeoPackage and the heat map - and the Outputs screen
    gates both download buttons on that flag, so the run would appear to have
    lost artefacts that are sitting right there (review finding, 2026-08-12).
    A file's record has to describe the directory it is in.

    The carried block says it was carried, because it was written from an
    earlier state of the ledger and this pass cannot vouch for it: silently
    presenting a stale record as current would trade one wrong statement for
    another.
    """
    if block.get("written") or block.get("reason") != "no_stock_geometry":
        return block
    previous = Path(out_dir) / "aggregate.json"
    if not previous.exists():
        return block
    try:
        old = json.loads(previous.read_text(encoding="utf-8")).get("results_layer")
    except Exception:                               # noqa: BLE001 - unreadable
        return block
    if not isinstance(old, dict) or not old.get("written"):
        return block
    if not (Path(out_dir) / str(old.get("layer") or rl.LAYER_FILENAME)).exists():
        return block
    return {**old, "carried_forward":
            "written by an earlier pass and left untouched: this re-aggregation "
            "ran without `--stock`, so no layer could be rebuilt.  It reflects "
            "the ledger as it stood then, which may not be the ledger beside it "
            "now; re-run `--aggregate` with `--stock` to rewrite it."}

def _carry_forward_elapsed(out_dir: Path) -> dict:
    """Keep how long the simulation pass took, which no later pass can know.

    `elapsed_minutes` is timed by `run_stock`, so re-aggregating a finished run
    would drop it - and it is the one field in the file that records what the
    directory cost to produce.  Same principle as `_carry_forward_layer`: a
    re-aggregation corrects what it can recompute and must not quietly delete
    what it cannot.
    """
    previous = Path(out_dir) / "aggregate.json"
    if not previous.exists():
        return {}
    try:
        elapsed = json.loads(previous.read_text(encoding="utf-8")).get("elapsed_minutes")
    except Exception:                               # noqa: BLE001 - unreadable
        return {}
    return {"elapsed_minutes": elapsed} if elapsed is not None else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the verified model over a stock")
    parser.add_argument("--scope", default="clusters",
                        choices=["all", "clusters", "district", "references"])
    parser.add_argument("--district")
    parser.add_argument("--references", nargs="*")
    parser.add_argument("--references-file", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("out/stock/run"))
    parser.add_argument("--workers", type=int, default=6)
    # No defaults on the building inputs.  They used to point at the Valencia
    # cadastre and its dwelling ledger, which meant that forgetting to pass a
    # city's own files did not fail - it ran Valencia under that city's name.
    parser.add_argument("--gis", type=Path,
                        help="raw cadastre; requires --tipo15 alongside it")
    parser.add_argument("--tipo15", type=Path,
                        help="dwelling ledger that goes with --gis")
    parser.add_argument("--stock", type=Path,
                        help="a prepared stock file that already carries the "
                             "fields the engine reads; replaces --gis/--tipo15")
    parser.add_argument("--microclimate", type=Path,
                        help="PALM slice directory; switches the run from an "
                             "annual simulation to an event simulation over the "
                             "weather file's hottest week, offset per building")
    parser.add_argument("--height", help="which microclimate height slice to read")
    parser.add_argument("--spinup-days", type=int,
                        help="days of unmodified weather before a microclimate event")
    parser.add_argument("--var-dir", type=Path,
                        default=Path(mb._project_root()) / "var")
    parser.add_argument("--zero-policy", default="literal_zero",
                        choices=["literal_zero", "cluster_median_impute"])
    parser.add_argument("--climate", type=Path,
                        help="climate bundle JSON (EPW + .ddy + site temperatures); "
                             "omit for the verified Valencia set")
    parser.add_argument("--policy", type=Path,
                        help="stock input policy JSON (column names and rules)")
    parser.add_argument("--template", type=Path,
                        help="template .osm, or a JSON spec with an alias map")
    parser.add_argument("--eu", type=Path,
                        help="EU building footprints (category-1 GeoPackage); "
                             "adds the sub-footprint fragmentation FLAG to every "
                             "ledger row.  Changes no physics - omit it and the "
                             "run is byte-identical to one without this option")
    parser.add_argument("--slow-seconds", type=float, default=900.0,
                        help="warn when a building has been running this long "
                             "(0 disables); reporting only, nothing is killed")
    # `full` is the default: the EnergyPlus intermediates are what you need to
    # answer a question about a finished run without re-running it, and at
    # ~7.5 MB a building the whole stock is well inside the external disk.
    parser.add_argument("--keep", default="full", choices=["summary", "full"])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--aggregate", type=Path,
                        help="re-aggregate an existing ledger and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")

    if args.aggregate:
        rows = read_ledger(args.aggregate)
        source = args.stock or args.gis
        stock = gpd.read_file(source) if source and source.exists() else None
        if stock is not None:
            stock["refparcela"] = stock["refparcela"].astype(str).str.strip()
        report = aggregate(rows, stock)
        # Rewrite the summary next to the ledger.  A run that was interrupted
        # and resumed leaves an aggregate written by whichever version of this
        # module the long-lived process had loaded; re-aggregating has to make
        # the file on disk agree with the ledger, not just print to the screen.
        # Re-aggregating is also how a run that predates the layer gets one:
        # give it `--stock` and the map is written from the ledger it already
        # has, without simulating anything again.
        layer = _carry_forward_layer(
            rl.write_results_layer(rows, stock, args.aggregate.parent),
            args.aggregate.parent)
        # Carried into the report itself, not only into the copy being written:
        # the screen printed "elapsed None min" while the file on disk held the
        # real 182.46, because the two were built from different dicts.  The
        # same shape of split - write one thing, show another - is what hid the
        # cadastral-area rename above.
        report.update({"ledger": str(args.aggregate),
                       **_carry_forward_elapsed(args.aggregate.parent),
                       "provenance": provenance_block(rows, [args.aggregate]),
                       "results_layer": layer})
        (args.aggregate.parent / "aggregate.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        _print_report(report)
        return 0

    references = list(args.references or [])
    if args.references_file:
        references += [line.strip() for line in
                       args.references_file.read_text(encoding="utf-8").splitlines()
                       if line.strip()]

    try:
        report = run_stock(
            scope=args.scope, out_dir=args.out_dir, workers=args.workers,
            gis_path=args.gis, tipo15_path=args.tipo15, stock_path=args.stock,
            var_dir=args.var_dir,
            zero_policy=args.zero_policy, keep=args.keep, district=args.district,
            references=references or None, resume=args.resume,
            retry_failed=args.retry_failed, limit=args.limit,
            climate_path=args.climate, policy_path=args.policy,
            template_path=args.template, eu_path=args.eu,
            microclimate_path=args.microclimate, height_token=args.height,
            spinup_days=args.spinup_days,
            slow_seconds=args.slow_seconds)
    except vm.ProfileDrift as drift:
        print(f"REFUSED: {drift}", file=sys.stderr)
        return 2
    except (cl.ClimateError, tpl.TemplateError, sip.StockPolicyError,
            euf.EuSourceError) as bad_input:
        print(f"REFUSED: {bad_input}", file=sys.stderr)
        return 2
    except InputMismatch as mismatch:
        print(f"{mismatch}", file=sys.stderr)
        return 2

    _print_report(report)
    return 0 if report["buildings_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
