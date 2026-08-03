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
import deep_building as db
import model_builder as mb
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
    "res_area_m2", "tipo15_res_area_m2", "res_area_source",
    "total_conditioned_area_m2", "footprint_m2", "large_footprint_single_zone",
    "n_floors_total", "n_floors_residential", "n_party_surfaces",
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
        # the written file, and pinned by a test on the FILE, not the frame.
        columns = [c for c in ("refparcela", "altura_max", "cluster", "family",
                               "period", "nombre", "pob_total", "num_vivend",
                               "footprint_area_m2", "imputed_floors",
                               "res_area_proxy", "dup_refparcela",
                               "ground_use", "ground_use_source",
                               "res_area_m2", "geometry")
                   if c in stock.columns]
        prepared = stock[columns].copy()
        # The policy's Tipo15 area travels under its own name.  The engine
        # derives its own `res_area_m2` from the geometry (footprint x
        # residential storeys) and that is the basis every energy figure and the
        # Rai comparison use - his own 954.80 m2 is 4 x 238.70, a geometric
        # storey area, not a net cadastral one.  Two different quantities that
        # differ by ~32 % city-wide must not share a column name.
        if "res_area_m2" in prepared.columns:
            prepared = prepared.rename(columns={"res_area_m2": "tipo15_res_area_m2"})
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
        if geom.geom_type != "Polygon":
            excluded.append({"refparcela": ref,
                             "reason": f"unsupported_geometry_{geom.geom_type}"})
            continue
        if len(geom.interiors) > 0:
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


def run_one(task: tuple[str, str | None] | str) -> dict:
    """Simulate one building. Never raises: a failure is a ledger row too."""
    refparcela, cluster = task if isinstance(task, tuple) else (task, None)
    started = time.time()
    out_dir = Path(_WORKER["out_dir"])
    # the full identity travels on every row, so what produced it can always be
    # established from the ledger alone
    base = {"refparcela": refparcela,
            **{key: _WORKER[key] for key in IDENTITY_FIELDS}}
    try:
        summary, qa_passed = vm.simulate_verified_building(
            refparcela, out_dir,
            zero_policy=_WORKER["zero_policy"],
            gis_path=Path(_WORKER["prepared_gis"]),
            neighbors_path=Path(_WORKER["context_gis"]),
            climate=_WORKER.get("climate"),
            config=_WORKER.get("config"))
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
# Rai's own per-cluster consumption intensities, proven on 2026-07-28 to be a
# single constant broadcast to every building of the cluster (13 distinct values
# across the city).  They are the closest thing we have to a per-cluster anchor.
RAI_CLUSTER_CONSUME = {
    "BlocPluriP01": 48, "BlocPluriP02": 52, "BlocPluriP03": 52, "BlocPluriP04": 47,
    "BlocPluriP05": 47, "BlocPluriP06": 45, "BlocPluriP07": 45,
    "EdiPluriP01": 54, "EdiPluriP02": 0, "EdiPluriP03": 52, "EdiPluriP04": 52,
    "EdiPluriP05": 46, "EdiPluriP06": 46, "EdiPluriP07": 46,
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
    claim it was.  The excluded buildings are not a random sample: the geometry
    gates fall hardest on large, complex buildings, and footprint correlates
    with EUI among the buildings that DID run (Spearman -0.552 on the Benicalap
    v3 ledger; large quartile 42.6 kWh/m2 against small quartile 57.4).
    Dropping large low-EUI buildings therefore biases the modellable subset's
    intensity UPWARD relative to the full stock.  So the intensity is the EUI
    of the modellable subset, stated as such - not an unbiased estimate of the
    whole district's.  With the 20 000 m2 ceiling the missing share is small
    (~1.6 % of footprint city-wide) but the direction is known and recorded.
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
        "note": ("totals are summed over buildings_with_result only, so an "
                 "absolute GWh figure under-reports by the missing share. The "
                 "area-weighted intensity is the EUI of the modellable subset, "
                 "not an unbiased estimate of the whole scope: exclusions "
                 "concentrate in large buildings and footprint anti-correlates "
                 "with EUI (Spearman -0.552, Benicalap v3), so the subset's "
                 "intensity is biased upward relative to the full stock."),
    }

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
                "zoning": zoning_block(pd.DataFrame(ok)),
                "qa_failed": 0,
                "unexplained_severes": 0,
                "implausible_occupancy": 0,
                "totals": {},
                "by_cluster": [],
                "by_district": [],
                "seconds_per_building": {}}

    frame = pd.DataFrame(ok)
    if stock is not None:
        lookup = stock.set_index("refparcela")[["cluster", "nombre"]]
        frame = frame.join(lookup, on="refparcela")

    area = frame["res_area_m2"]
    energy_cols = {"heating": "space_heating_kwh_m2", "cooling": "cooling_kwh_m2",
                   "dhw": "dhw_kwh_m2", "total_site": "total_site_kwh_m2"}
    totals = {f"{name}_gwh": round(float((frame[col] * area).sum()) / 1e6, 5)
              for name, col in energy_cols.items()}
    totals["residential_area_m2"] = round(float(area.sum()), 1)
    totals["area_weighted_total_site_kwh_m2"] = round(
        float((frame["total_site_kwh_m2"] * area).sum() / area.sum()), 3)
    # Which denominator every kWh/m2 above is on, stated rather than assumed.
    # Rai's own EUI is 52 008 kWh / 954.80 m2, and his 954.80 is 4 x 238.70 -
    # geometric residential storeys. So the comparison against his ConsumE is on
    # a matching basis. Tipo15 is the cadastral net area and is ~32 % smaller
    # city-wide; it is reported, never divided by.
    totals["area_basis"] = "geometric_residential_storeys"
    totals["area_basis_note"] = (
        "every kWh/m2 is per geometric residential storey area, matching Rai's "
        "own 954.80 m2 basis; tipo15_residential_area_m2 is the cadastral net "
        "area, reported for reference only")
    if "tipo15_res_area_m2" in frame:
        tipo15 = pd.to_numeric(frame["tipo15_res_area_m2"], errors="coerce")
        if tipo15.notna().any():
            totals["tipo15_residential_area_m2"] = round(float(tipo15.sum()), 1)
            totals["tipo15_vs_geometric_pct"] = round(
                float(area.sum() / tipo15.sum() - 1.0) * 100, 2)
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
            block = {key: name, "buildings": int(len(group)),
                     "residential_area_m2": round(float(group_area.sum()), 1),
                     "total_site_gwh": round(
                         float((group["total_site_kwh_m2"] * group_area).sum()) / 1e6, 5),
                     "area_weighted_kwh_m2": round(intensity, 3)}
            if key == "cluster" and name in RAI_CLUSTER_CONSUME:
                rai = RAI_CLUSTER_CONSUME[name]
                block["rai_consume_kwh_m2"] = rai
                block["vs_rai_pct"] = (round((intensity - rai) / rai * 100, 2)
                                       if rai else None)
            out.append(block)
        return sorted(out, key=lambda b: -b["total_site_gwh"])

    return {
        "buildings_ok": len(ok),
        "buildings_failed": sum(1 for r in rows if r.get("status") == "failed"),
        "buildings_failed_qa": sum(1 for r in rows
                                   if r.get("status") == "failed_qa"),
        "buildings_excluded": sum(1 for r in rows if r.get("status") == "excluded"),
        "coverage": coverage_block(rows, ok, stock),
        "zoning": zoning_block(frame),
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
def run_stock(*, scope: str, out_dir: Path, workers: int,
              gis_path: Path, tipo15_path: Path, var_dir: Path,
              zero_policy: str = "literal_zero", keep: str = "full",
              district: str | None = None, references: list[str] | None = None,
              resume: bool = False, retry_failed: bool = False,
              limit: int | None = None,
              climate_path: Path | None = None,
              policy_path: Path | None = None,
              template_path: Path | None = None,
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

    prepared_gis, stock, counters = prepare_stock_file(
        gis_path, tipo15_path, policy, var_dir)
    # key names come from stock_input_policy.prepare_stock; getting them wrong
    # printed "None" for both counts instead of failing, so the banner quietly
    # stopped reporting the data-quality numbers it claims to report
    for key in ("imputed_floor_buildings", "residential_area_proxy_buildings",
                "duplicate_parcel_rows"):
        assert key in counters, f"stock counter '{key}' is gone - fix the banner"
    log.info("[stock] %s buildings after policy | floors imputed %s | area proxy %s "
             "| duplicate rows %s",
             len(stock), counters["imputed_floor_buildings"],
             counters["residential_area_proxy_buildings"],
             counters["duplicate_parcel_rows"])

    scoped = select_scope(stock, scope, district=district, references=references)
    runnable, excluded = screen_geometry(scoped)
    if limit:
        runnable = runnable[:limit]

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
        # the raw cadastre stays the shading context on purpose
        "context_gis": str(gis_path),
        "zero_policy": zero_policy,
        "keep": keep,
        "tmp_root": str(Path(var_dir) / "stock_tmp"),
        "profile_fingerprint": vm.profile_fingerprint(),
        # a ClimateSet and a BuildConfig both pickle, so each worker gets the
        # already-validated objects rather than re-reading and re-checking the
        # files 26 452 times
        "climate": climate,
        "config": config,
        **fingerprints,
    }
    # the worker config carries live objects; the record on disk carries their
    # descriptions, so a finished run can always be read back without them
    recorded_config = {key: value for key, value in worker_config.items()
                       if key not in ("climate", "config")}
    (out_dir / "run_config.json").write_text(
        json.dumps({"scope": scope, "district": district, "workers": workers,
                    "zero_policy": zero_policy, "keep": keep,
                    "worker_config": recorded_config, "stock_counters": counters,
                    "climate": climate.record() if climate else None,
                    "template": template.record() if template else None,
                    "policy": dataclasses.asdict(policy),
                    "runnable": len(runnable), "excluded": len(excluded)},
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
                            row = {"refparcela": ref, "status": "failed",
                                   "reason": f"worker_{type(exc).__name__}",
                                   "message": str(exc)[:400],
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
    (out_dir / "aggregate.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def _print_report(report: dict) -> None:
    print()
    print(f"  ok {report['buildings_ok']} | failed {report['buildings_failed']} "
          f"| excluded {report['buildings_excluded']} "
          f"| QA fail {report['qa_failed']} "
          f"| unexplained severe {report['unexplained_severes']}")
    totals = report.get("totals") or {}
    if totals:
        print(f"  heating {totals['heating_gwh']} GWh | cooling {totals['cooling_gwh']} "
              f"| DHW {totals['dhw_gwh']} | total site {totals['total_site_gwh']} GWh")
        print(f"  residential area {totals['residential_area_m2']:,.0f} m² | "
              f"area-weighted {totals['area_weighted_total_site_kwh_m2']} kWh/m²")
    if report.get("seconds_per_building"):
        spb = report["seconds_per_building"]
        print(f"  per building: median {spb['median']}s | mean {spb['mean']}s | max {spb['max']}s")
    if report.get("by_cluster"):
        print(f"  {'cluster':16s} {'n':>6s} {'kWh/m²':>9s} {'Rai':>6s} {'fark':>8s}")
        for block in report["by_cluster"]:
            rai = block.get("rai_consume_kwh_m2")
            delta = block.get("vs_rai_pct")
            print(f"  {block['cluster']:16s} {block['buildings']:6d} "
                  f"{block['area_weighted_kwh_m2']:9.2f} "
                  f"{'' if rai is None else rai:>6} "
                  f"{'' if delta is None else f'{delta:+.1f}%':>8}")
    print(f"  elapsed {report.get('elapsed_minutes')} min")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the verified model over a stock")
    parser.add_argument("--scope", default="clusters",
                        choices=["all", "clusters", "district", "references"])
    parser.add_argument("--district")
    parser.add_argument("--references", nargs="*")
    parser.add_argument("--references-file", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("out/stock/run"))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--gis", type=Path, default=Path(mb.NEIGHBORS_SHP))
    parser.add_argument("--tipo15", type=Path,
                        default=Path(mb._project_root()) / "data/reference/Tipo15_soloV(in).csv")
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
        stock = gpd.read_file(args.gis) if args.gis.exists() else None
        if stock is not None:
            stock["refparcela"] = stock["refparcela"].astype(str).str.strip()
        report = aggregate(rows, stock)
        # Rewrite the summary next to the ledger.  A run that was interrupted
        # and resumed leaves an aggregate written by whichever version of this
        # module the long-lived process had loaded; re-aggregating has to make
        # the file on disk agree with the ledger, not just print to the screen.
        (args.aggregate.parent / "aggregate.json").write_text(
            json.dumps({**report, "ledger": str(args.aggregate),
                        "provenance": provenance_block(rows, [args.aggregate])},
                       indent=2, ensure_ascii=False), encoding="utf-8")
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
            gis_path=args.gis, tipo15_path=args.tipo15, var_dir=args.var_dir,
            zero_policy=args.zero_policy, keep=args.keep, district=args.district,
            references=references or None, resume=args.resume,
            retry_failed=args.retry_failed, limit=args.limit,
            climate_path=args.climate, policy_path=args.policy,
            template_path=args.template, slow_seconds=args.slow_seconds)
    except vm.ProfileDrift as drift:
        print(f"REFUSED: {drift}", file=sys.stderr)
        return 2
    except (cl.ClimateError, tpl.TemplateError, sip.StockPolicyError) as bad_input:
        print(f"REFUSED: {bad_input}", file=sys.stderr)
        return 2
    except InputMismatch as mismatch:
        print(f"{mismatch}", file=sys.stderr)
        return 2

    _print_report(report)
    return 0 if report["buildings_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
