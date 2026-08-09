"""Run the verified deep chain over the Lecco stock.

Thin orchestration only.  Every number comes out of `deep_building`, which is
the same frozen chain the Valencia work was verified with; this module decides
which buildings to run, hands each one its own Italian envelope, and collects
the results.  It writes no physics.

The one thing it does that `stock_runner` cannot
------------------------------------------------
`stock_runner` gives every building one shared `BuildConfig` and lets
`deep_building.config_for_building` resolve the envelope from the cluster - via
`model_config.TABULA_ES`, the Spanish IVE table.  Lecco's envelope comes from
the Italian TABULA table shipped inside the EU database, so each building needs
its own U-values.  `config_for_building` supports exactly this:

    a caller that has already pinned the envelope means it

so this runner pins `wall_u`, `roof_u` and `window_u` per building from the
columns `lecco_stock.py` wrote, and the Spanish table is never consulted.
Nothing in the hash-locked set is touched.

Staging
-------
Following the same discipline the Valencia stock run used on 2026-07-28: start
with one building per cluster, look at the numbers, and only then open up.
`--per-cluster N` runs a sample, `--all` runs everything.

Assumptions this run inherits, all stated rather than buried
------------------------------------------------------------
* Envelope layers are the Spanish template's masonry materials recalibrated to
  hit each Italian U-value (`model_builder._build_layered_wall` picks its regime
  from the target U, not from a country).  The U is Italian and measured; the
  layer composition, and therefore the thermal mass, is an approximation.
* `thermal_bridge_du` stays at the project default of 0.10 W/m2K on top of the
  TABULA element U, which is the same correction the Valencia runs used.
* The template, its schedules, its thermostat setpoints and the PTHP/DHW plant
  are Rai's Spanish CTE ones.  This run therefore answers "what does the Lecco
  stock do under the Valencia model's operation", which is the right question
  for a method comparison and the wrong one for an Italian code compliance
  figure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openstudio                                                 # noqa: E402

import climate as cl                                              # noqa: E402
import deep_building as db                                        # noqa: E402
import microclimate as mcl                                        # noqa: E402
import model_builder as mb                                        # noqa: E402
import run_simulation as sim                                      # noqa: E402
import verified_model as vm                                       # noqa: E402

log = logging.getLogger("lecco_run")

# Downward heat flow through a ground-contact slab: inside surface film only,
# since the ground side has no air film.  Matches the convention the frozen
# builder uses for its own calibrated assemblies.
GROUND_FILM_R = 0.17

DEFAULT_STOCK = Path("data/gis/lecco/lecco_stock.gpkg")
DEFAULT_CLIMATE = Path("climates/lecco_bergamo_tmyx.json")
DEFAULT_OUT = Path("out/lecco/run")

_WORKER: dict = {}


def config_for(stock_path: Path, climate, row) -> object:
    """A BuildConfig for one Lecco building, with its Italian envelope pinned."""
    config = mb.DEFAULT_BUILD_CONFIG.model_copy(deep=True)
    config.data.building_path = stock_path
    config.data.neighbor_path = stock_path
    config.data.epw_path = climate.epw_path
    return config.with_legacy_params({
        "wall_u": float(row["wall_u"]),
        "roof_u": float(row["roof_u"]),
        "window_u": float(row["window_u"]),
    })


def apply_italian_ground_slab(osm, target_u: float) -> dict:
    """Give the ground-contact floors the U-value the Italian database states.

    Without this the slab stays whatever the Spanish template's default
    construction set provides - `Solera sin aislante`, which EnergyPlus reports
    at U 4.35 W/m2K with film.  TABULA_IT prices this element for every class it
    covers (1.02-1.15 W/m2K for the residential ones), so leaving it at the
    template default discards real Lecco data in favour of a foreign default and
    inflates heating through a four-fold overstatement of floor loss.

    Built the same way the frozen builder builds its walls and roofs: real
    template materials with one calibration layer whose thickness is solved to
    hit the target U.  Applied post-build, which is the pattern
    `deep_building.apply_rai_ground_regime` already uses to swap this very
    surface.
    """
    # Concrete slab + screed as the fixed layers, the template's own ground
    # insulation as the calibration layer: at k = 0.035 the solved thickness
    # lands around 2-3 cm for the Italian residential floor U-values, which is a
    # physically sensible assembly.  A dense layer as the calibrator would need
    # over a metre of it to reach the same resistance.
    slab = mb._assemble_calibrated(
        osm, f"Solera IT (U={target_u:.3f})", target_u, GROUND_FILM_R,
        [mb._clone_template_material(osm, "Solera Hormigon Referencia"),
         mb._clone_template_material(osm, "Mortero de cemento referencia")],
        mb._clone_template_material(osm, "Aislante Solera Referencia B"),
        insert_at=1)

    applied = 0
    for surface in osm.getSurfaces():
        if (surface.surfaceType() == "Floor"
                and surface.outsideBoundaryCondition() == "Ground"):
            surface.setConstruction(slab)
            applied += 1
    if not applied:
        raise RuntimeError("no ground-contact floor surface found to apply the slab to")
    return {"ground_slab_construction": slab.nameString(),
            "ground_slab_target_u": round(target_u, 4),
            "ground_slab_surfaces": applied,
            "source": "TABULA_IT U_FLOOR from Lecco.db"}


def simulate_lecco_building(reference: str, out_dir: Path, row, *,
                            stock_path: Path, climate, config,
                            provenance: dict | None = None,
                            event: dict | None = None) -> tuple[dict, bool]:
    """One building, mirroring `deep_building.simulate_deep_building` exactly.

    Every physics call below is the frozen one.  The single difference from the
    function this mirrors is `apply_italian_ground_slab`, inserted between build
    and run - the one place the Italian data has something to say that the
    frozen chain has no parameter for.  It is written out here rather than added
    to `deep_building` because that module is hash-locked and changing it would
    drift the verified Valencia profile.
    """
    import shutil

    occupants, occupants_source = db.resolve_occupants(
        row.get("pob_total"), row.get("num_vivend"), None, "literal_zero")

    geom = mb.clean_polygon(row.geometry)
    neighbours = mb.load_neighbors(geom, reference, stock_path)
    party = mb.find_party_walls(geom, reference, stock_path, neighbors=neighbours)

    osm, stats = db.build_deep_model(row, party, occupants, neighbors=neighbours,
                                     climate=climate, config=config)

    stats["deep_layers"]["ground_slab"] = apply_italian_ground_slab(
        osm, float(row["floor_u"]))

    stats["occupants_source"] = occupants_source
    stats["climate"] = climate.name
    stats["climate_fingerprint"] = climate.fingerprint
    # Carried for reporting only.  The stock deliberately writes no
    # `tipo15_res_area_m2`, so the engine stamps `unchecked_no_tipo15` and
    # leaves every storey of a purely residential building as housing.
    stats["eu_gross_floor_area_m2"] = row.get("eu_gross_floor_area_m2")
    stats["eu_net_floor_area_m2"] = row.get("eu_net_floor_area_m2")
    stats["res_area_source"] = "geometry: footprint x residential storeys"

    # In an event run the weather file and the run period both come from the
    # microclimate slice, and the annual QA thresholds no longer describe what
    # is being simulated.  Everything else - geometry, envelope, occupancy,
    # systems - is identical to the annual path.
    epw_path = climate.epw_path
    if event is not None:
        epw_path = event["epw_path"]
        stats["deep_layers"]["microclimate"] = {
            "slice": event["slice_record"],
            "delta_peak_k": event["delta_peak_k"],
            "delta_base_k": event["delta_base_k"],
            "sample_radius_m": event["sample_radius_m"],
            "sample_cells": event["sample_cells"],
            "run_period": mcl.apply_event_run_period(osm, event["window"]),
            "event_epw": Path(epw_path).name,
        }
        mcl.request_outdoor_air_output(osm)

    run_dir = out_dir / f"{reference}_deep"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    sql_path = sim.run_energyplus(osm, run_dir, epw_path=epw_path)

    err_stats = db.scan_err_deep(run_dir)
    results = db.read_end_uses_split(sql_path, stats["res_area_m2"],
                                     stats["total_conditioned_area_m2"])
    if event is not None:
        results.update(mcl.read_site_energy_precise(sql_path, stats["res_area_m2"]))
        checks = mcl.event_qa(
            sim.crosscheck_energyplus(
                sql_path, stats,
                unmet_max=mcl.event_unmet_allowance(event["days"])),
            results, days=event["days"], baseline=event.get("baseline"))
        outdoor = mcl.observed_outdoor_air(sql_path)
        if outdoor:
            results.update(outdoor)
    else:
        checks = (sim.crosscheck_energyplus(sql_path, stats,
                                            unmet_max=sim.QA_UNMET_HOURS_MAX_HVAC)
                  + sim.check_plausibility_cons(
                      {"total_site_kwh_m2": results["total_site_kwh_m2"]}))
    carbon = sim.carbon_from_enduses(
        {"cons_heating_gas_kwh_m2": results["space_heating_gas_kwh_m2"],
         "cons_heating_elec_kwh_m2": results["space_heating_elec_kwh_m2"],
         "cons_cooling_kwh_m2": results["cooling_kwh_m2"],
         "cons_fans_kwh_m2": results["fans_kwh_m2"],
         "site_gas_kwh_m2": results["site_gas_kwh_m2"],
         "site_elec_kwh_m2": results["site_elec_kwh_m2"]},
        stats["res_area_m2"])

    qa_passed = all(check["passed"] for check in checks)
    summary = {"refparcela": reference,
               **{k: v for k, v in stats.items()
                  if k not in ("facade_qa", "deep_layers")},
               **results, **carbon, **err_stats, "qa_all_passed": qa_passed}
    if provenance:
        summary["verified_profile"] = provenance
    (run_dir / "deep_layers.json").write_text(
        json.dumps({"layers": stats["deep_layers"], "results": results,
                    "carbon": carbon, "qa": checks, "summary": summary},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary, qa_passed


def _init_worker(stock_path: str, climate_path: str, out_dir: str,
                 event_plan: str | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    scratch = Path(out_dir) / "_scratch" / str(os.getpid())
    scratch.mkdir(parents=True, exist_ok=True)
    # `run_energyplus` drives the in-process pyenergyplus API, which is not
    # thread-safe and writes into the working directory: separate processes with
    # separate scratch roots, exactly as the Valencia stock runner does.
    os.environ["TMPDIR"] = str(scratch)
    _WORKER["stock_path"] = Path(stock_path)
    _WORKER["climate"] = cl.load_climate(climate_path)
    _WORKER["out_dir"] = Path(out_dir)
    # The per-building offsets are computed once in the parent and handed to the
    # workers as data.  Re-reading the rasters in every worker would open the
    # same files six times and, worse, allow six subtly different samplings of
    # one field to end up in one ledger.
    _WORKER["event_plan"] = json.loads(event_plan) if event_plan else None


def _window_days(window, frame) -> int:
    """How many calendar days the run period actually spans in this file."""
    import pandas as pd

    month = pd.to_numeric(frame[1], errors="coerce").astype(int)
    day = pd.to_numeric(frame[2], errors="coerce").astype(int)
    key = month * 100 + day
    begin, end = window[0] * 100 + window[1], window[2] * 100 + window[3]
    return int(key[(key >= begin) & (key <= end)].nunique())


def _event_for(reference: str) -> dict | None:
    """This building's slot in the event plan, or `None` for an annual run.

    A building the slice does not reach is refused rather than run at a zero
    offset: zero is a measurement claiming the building sits exactly at the
    domain average, and "no data" is not that claim.
    """
    plan = _WORKER.get("event_plan")
    if plan is None:
        return None
    entry = plan["deltas"].get(str(reference))
    if entry is None:
        raise ValueError(
            f"{reference} has no microclimate offset: the slice does not cover it")
    # Coerced at the deserialisation boundary: the plan crosses a JSON round
    # trip to reach this process, and a date that arrives as text compares
    # unequal against every row in the weather file instead of failing loudly.
    window = tuple(int(v) for v in plan["window"])
    epw = mcl.write_event_epw(
        Path(plan["base_epw"]), Path(plan["epw_dir"]),
        delta_peak_k=entry["delta_peak_k"], delta_base_k=entry["delta_base_k"],
        window=window)
    return {"epw_path": epw, "window": window,
            "days": plan["days"], "slice_record": plan["slice_record"],
            **entry}


def _run_one(reference: str) -> dict:
    """Simulate one building.  Failures are returned, never raised: one bad
    building must not take the run down with it."""
    import geopandas as gpd

    started = time.time()
    stock_path = _WORKER["stock_path"]
    climate = _WORKER["climate"]
    try:
        escaped = str(reference).replace("'", "''")
        frame = gpd.read_file(stock_path, where=f"refparcela = '{escaped}'")
        if frame.empty:
            raise ValueError(f"{reference} not in {stock_path.name}")
        row = frame.iloc[0]

        event = _event_for(reference)
        summary, qa_passed = simulate_lecco_building(
            reference, _WORKER["out_dir"] / "buildings", row,
            stock_path=stock_path, climate=climate,
            config=config_for(stock_path, climate, row),
            provenance=vm.profile_record(), event=event)
        if event is not None:
            summary.update({
                "run_mode": "microclimate_event",
                "microclimate_slice": event["slice_record"]["name"],
                "microclimate_fingerprint": event["slice_record"]["fingerprint"],
                "delta_peak_k": event["delta_peak_k"],
                "delta_base_k": event["delta_base_k"],
                "sample_radius_m": event["sample_radius_m"],
                "event_days": event["days"],
            })
        summary.update({
            "status": "ok", "qa_all_passed": bool(qa_passed),
            "cluster": row["cluster"], "period": row["period"],
            "family": row["family"], "tabula_string": row["tabula_string"],
            "wall_u": float(row["wall_u"]), "roof_u": float(row["roof_u"]),
            "window_u": float(row["window_u"]),
            "period_assumed": bool(row["period_assumed"]),
            "seconds": round(time.time() - started, 1),
        })
        return summary
    except Exception as exc:                       # noqa: BLE001 - isolation boundary
        return {"refparcela": reference, "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - started, 1)}


def screen(stock) -> tuple[list[str], list[dict]]:
    """Split the stock into runnable and excluded, using the ENGINE's own gate.

    `model_builder.prepare_footprint` is the thing that will actually refuse the
    building, so it is what decides here too - the 2026-07-28 lesson from the
    Valencia run was that re-implementing a threshold produces a different
    answer from the one the engine enforces.  Calling it up front turns a
    mid-run crash into a recorded exclusion with a reason.

    Report exclusions by FLOOR AREA, not by building count: Lecco's 969
    sub-50 m2 buildings are 18.8 % of the count but 1.8 % of the floor area, and
    the count alone would badly misstate what is being left out.
    """
    runnable: list[str] = []
    excluded: list[dict] = []
    for row in stock.itertuples():
        try:
            mb.prepare_footprint(row.geometry, mb.DEFAULT_BUILD_CONFIG)
        except Exception as exc:                   # noqa: BLE001 - the gate itself
            excluded.append({"refparcela": str(row.refparcela), "status": "excluded",
                             "reason": str(exc),
                             "floor_area_m2": float(row.eu_gross_floor_area_m2 or 0.0)})
            continue
        runnable.append(str(row.refparcela))
    return runnable, excluded


def select(stock, *, per_cluster: int | None, limit: int | None,
           references: list[str] | None) -> list[str]:
    """Which buildings to run.

    A per-cluster sample takes the building closest to its cluster's median
    floor area, so the sample is representative rather than incidental.
    """
    if references:
        return list(references)
    frame = stock
    if per_cluster:
        picked: list[str] = []
        for _, group in frame.groupby("cluster"):
            median = group["eu_gross_floor_area_m2"].median()
            order = (group["eu_gross_floor_area_m2"] - median).abs().sort_values()
            picked += list(group.loc[order.index[:per_cluster], "refparcela"])
        return picked
    # Deterministically shuffled.  The stock arrives in database order, which
    # tracks how the source was assembled and therefore correlates with location
    # and with building type; running it in that order makes any partial ledger
    # a biased sample, and a long run WILL be read before it finishes.  Shuffled
    # with a fixed seed, every prefix of the run is an unbiased random sample of
    # the stock and the order is still reproducible.
    import random

    references_all = list(frame["refparcela"])
    random.Random(42).shuffle(references_all)
    return references_all[:limit] if limit else references_all


def source_identity() -> dict:
    """What produced a ledger row: the runner's own source hash and the inputs.

    Added after two runners were caught writing to one output directory with
    different versions of this file (2026-08-06).  The rows carried
    `verified_profile` and `climate_fingerprint` but nothing identifying the
    orchestrator, so which code wrote which row could not be recovered
    afterwards.  Cheap to stamp, impossible to reconstruct later.
    """
    source = Path(__file__).resolve()
    return {
        "runner_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "runner_pid": os.getpid(),
        "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def _process_alive(pid: int) -> bool:
    """Signal 0 probes for existence without touching the process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False                               # stale marker from a dead run
    except PermissionError:
        return True                                # alive, owned by someone else
    return True


def claim_output_dir(out_dir: Path) -> Path:
    """Take ownership of an output directory, or refuse.

    A second runner appending to a live run's ledger doubles the work and
    double-counts every duplicated building in the totals, and `already_done`
    cannot prevent it: it reads the ledger once at start-up, so everything the
    first process is still working on looks un-run.  `stock_adapter` guards its
    runs with a PID file for the same reason; this is that pattern.
    """
    marker = out_dir / "run_process.json"
    if marker.exists():
        try:
            owner = json.loads(marker.read_text(encoding="utf-8"))
            pid = int(owner.get("pid", -1))
        except (json.JSONDecodeError, TypeError, ValueError):
            pid = -1
        if pid > 0 and pid != os.getpid() and _process_alive(pid):
            raise RunAlreadyActive(
                    f"{out_dir} is owned by live process {pid} "
                    f"(started {owner.get('started')}). Two runners sharing one "
                    f"ledger duplicate work and double-count the totals. Stop "
                    f"that process, or use a different --out-dir.")
    marker.write_text(json.dumps(
        {"pid": os.getpid(),
         "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         "runner_sha256": source_identity()["runner_sha256"]},
        indent=2), encoding="utf-8")
    return marker


class RunAlreadyActive(RuntimeError):
    """Another live process owns this output directory."""


class LedgerNotEmpty(RuntimeError):
    """The ledger already holds rows and this is not a resume."""


def already_done(ledger: Path) -> set[str]:
    if not ledger.exists():
        return set()
    done = set()
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") == "ok":
            done.add(str(record.get("refparcela")))
    return done


def aggregate(records: list[dict]) -> dict:
    """City and cluster totals from the per-building ledger."""
    ok = [r for r in records if r.get("status") == "ok"]
    if not ok:
        return {"buildings_ok": 0}

    def total(record: dict, key: str) -> float:
        return float(record.get(key) or 0.0) * float(record.get("res_area_m2") or 0.0)

    area = sum(float(r.get("res_area_m2") or 0.0) for r in ok)
    keys = ("space_heating_kwh_m2", "cooling_kwh_m2", "dhw_kwh_m2",
            "total_site_kwh_m2")
    city = {key: (sum(total(r, key) for r in ok) / area if area else 0.0)
            for key in keys}

    clusters: dict[str, dict] = {}
    for record in ok:
        entry = clusters.setdefault(str(record.get("cluster")), {
            "n": 0, "area_m2": 0.0, "wall_u": record.get("wall_u"),
            **{key: 0.0 for key in keys}})
        entry["n"] += 1
        entry["area_m2"] += float(record.get("res_area_m2") or 0.0)
        for key in keys:
            entry[key] += total(record, key)
    for entry in clusters.values():
        for key in keys:
            entry[key] = round(entry[key] / entry["area_m2"], 2) if entry["area_m2"] else 0.0
        entry["area_m2"] = round(entry["area_m2"], 1)

    return {
        "buildings_ok": len(ok),
        "buildings_failed": len(records) - len(ok),
        "qa_failed": sum(1 for r in ok if not r.get("qa_all_passed")),
        "residential_area_m2": round(area, 1),
        "city_kwh_m2": {key: round(value, 2) for key, value in city.items()},
        "city_gwh": {key: round(value * area / 1e6, 4) for key, value in city.items()},
        "clusters": dict(sorted(clusters.items(),
                                key=lambda item: item[1]["n"], reverse=True)),
    }


def run(stock_path: Path, climate_path: Path, out_dir: Path, *,
        per_cluster: int | None = 1, limit: int | None = None,
        references: list[str] | None = None, workers: int = 6,
        resume: bool = False, microclimate: Path | None = None,
        height_token: str = mcl.DEFAULT_HEIGHT_TOKEN,
        spinup_days: int = mcl.DEFAULT_SPINUP_DAYS) -> int:
    import geopandas as gpd

    # Refuse before spending hours if the frozen chain has drifted.
    vm.assert_profile_intact()
    climate = cl.load_climate(climate_path)
    log.info("[climate] %s | %s", climate.name, climate.fingerprint[:16])

    stock = gpd.read_file(stock_path)
    log.info("[stock] %d buildings from %s", len(stock), stock_path.name)

    runnable, excluded = screen(stock)
    total_area = float(stock["eu_gross_floor_area_m2"].sum())
    lost_area = sum(item["floor_area_m2"] for item in excluded)
    log.info("[screen] %d runnable, %d excluded by the engine's own footprint "
             "gate = %.2f %% of buildings but %.2f %% of floor area "
             "(coverage %.2f %%)", len(runnable), len(excluded),
             len(excluded) / len(stock) * 100 if len(stock) else 0.0,
             lost_area / total_area * 100 if total_area else 0.0,
             (total_area - lost_area) / total_area * 100 if total_area else 0.0)

    runnable_set = set(runnable)

    # A microclimate slice covers a piece of the city, not the city.  It is
    # therefore a second, narrower gate on top of the footprint gate, and it is
    # applied here so the reduced scope is visible in the plan rather than
    # discovered as a wall of failures once the run is under way.
    event_plan = None
    if microclimate is not None:
        slice_ = mcl.load_slice(microclimate, height_token=height_token)
        deltas = mcl.sample_stock(slice_, stock)
        covered = deltas[deltas["sample_status"] == "ok"]
        header, frame = mcl.read_epw(climate.epw_path)
        window = mcl.hottest_window(frame, spinup_days=spinup_days)
        days = _window_days(window, frame)
        runnable_set &= set(covered["refparcela"].astype(str))
        outside = len(deltas) - len(covered)
        log.info("[microclimate] %s: %d of %d buildings inside the slice, "
                 "%d outside; event window %02d-%02d to %02d-%02d (%d days)",
                 slice_.name, len(covered), len(deltas), outside,
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

    wanted = [reference for reference
              in select(stock[stock["refparcela"].isin(runnable_set)],
                        per_cluster=per_cluster, limit=limit,
                        references=references)
              if reference in runnable_set or references]
    out_dir.mkdir(parents=True, exist_ok=True)
    claim_output_dir(out_dir)
    (out_dir / "excluded.json").write_text(
        json.dumps({"excluded": excluded, "runnable": len(runnable),
                    "floor_area_total_m2": round(total_area, 1),
                    "floor_area_excluded_m2": round(lost_area, 1),
                    "coverage_pct": round((total_area - lost_area) / total_area * 100, 2)
                    if total_area else 0.0}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    ledger = out_dir / "ledger.jsonl"
    if ledger.exists() and ledger.stat().st_size > 0 and not resume:
        rows = sum(1 for line in ledger.read_text(encoding="utf-8").splitlines()
                   if line.strip())
        raise LedgerNotEmpty(
            f"{ledger} already holds {rows} rows. Appending to it without "
            f"--resume mixes two runs into one set of totals. Pass --resume to "
            f"continue that run, or use a different --out-dir.")
    done = already_done(ledger) if resume else set()
    todo = [reference for reference in wanted if reference not in done]
    log.info("[plan] %d selected, %d already done, %d to run, %d workers",
             len(wanted), len(wanted) - len(todo), len(todo), workers)
    if not todo:
        log.info("nothing to do")
        return 0

    started = time.time()
    completed = 0
    identity = source_identity() | {
        "climate_fingerprint": climate.fingerprint,
        "profile_fingerprint": vm.profile_record()["fingerprint"],
        "stock": stock_path.name,
    }
    if event_plan is not None:
        identity |= {
            "run_mode": "microclimate_event",
            "microclimate_fingerprint": event_plan["slice_record"]["fingerprint"],
            "microclimate_slice": event_plan["slice_record"]["name"],
            "event_window": event_plan["window"],
        }
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(str(stock_path), str(climate_path),
                                       str(out_dir),
                                       json.dumps(event_plan, default=str)
                                       if event_plan else None)) as pool:
        futures = {pool.submit(_run_one, reference): reference for reference in todo}
        with ledger.open("a", encoding="utf-8") as handle:
            for future in as_completed(futures):
                record = future.result() | identity
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())     # a killed run must not lose work
                completed += 1
                if record["status"] == "ok":
                    log.info("[%d/%d] %s  %s  heat %.2f  cool %.2f  DHW %.2f  "
                             "site %.2f kWh/m2  QA %s  %.0fs",
                             completed, len(todo), record["refparcela"],
                             record.get("cluster"), record["space_heating_kwh_m2"],
                             record["cooling_kwh_m2"], record["dhw_kwh_m2"],
                             record["total_site_kwh_m2"],
                             "ok" if record["qa_all_passed"] else "FAIL",
                             record["seconds"])
                else:
                    log.warning("[%d/%d] %s FAILED: %s", completed, len(todo),
                                record["refparcela"], record["error"])

    records = [json.loads(line) for line in
               ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = aggregate(records)
    summary["climate"] = {"name": climate.name, "fingerprint": climate.fingerprint}
    summary["profile_fingerprint"] = vm.profile_record()["fingerprint"]
    summary["stock"] = stock_path.name
    summary["elapsed_minutes"] = round((time.time() - started) / 60, 1)
    (out_dir / "aggregate.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("\n%s", report(summary))
    return 0 if summary.get("buildings_failed", 0) == 0 else 1


def report(summary: dict) -> str:
    if not summary.get("buildings_ok"):
        return "no successful buildings"
    city = summary["city_kwh_m2"]
    lines = [
        f"buildings ok         {summary['buildings_ok']}",
        f"buildings failed     {summary.get('buildings_failed', 0)}",
        f"QA failed            {summary.get('qa_failed', 0)}",
        f"residential area     {summary['residential_area_m2']:,.0f} m2",
        "",
        "area-weighted, kWh/m2/yr",
        f"  space heating      {city['space_heating_kwh_m2']:>8.2f}",
        f"  cooling            {city['cooling_kwh_m2']:>8.2f}",
        f"  DHW                {city['dhw_kwh_m2']:>8.2f}",
        f"  total site         {city['total_site_kwh_m2']:>8.2f}",
        "",
        f"{'cluster':<26}{'n':>4}{'wall_u':>8}{'heat':>8}{'cool':>7}{'DHW':>7}{'site':>8}",
    ]
    for name, entry in summary["clusters"].items():
        lines.append(
            f"{name:<26}{entry['n']:>4}{entry['wall_u']:>8.3f}"
            f"{entry['space_heating_kwh_m2']:>8.2f}{entry['cooling_kwh_m2']:>7.2f}"
            f"{entry['dhw_kwh_m2']:>7.2f}{entry['total_site_kwh_m2']:>8.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deep chain over the Lecco stock")
    parser.add_argument("--stock", type=Path, default=DEFAULT_STOCK)
    parser.add_argument("--climate", type=Path, default=DEFAULT_CLIMATE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--per-cluster", type=int, default=1,
                        help="run N representatives per cluster (default 1)")
    parser.add_argument("--all", action="store_true", help="run the whole stock")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--references", nargs="*")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--microclimate", type=Path,
                        help="PALM slice directory; switches the run from an "
                             "annual simulation to an event simulation over the "
                             "weather file's hottest week, offset per building")
    parser.add_argument("--height", default=mcl.DEFAULT_HEIGHT_TOKEN,
                        help="which height slice to read (default 2m)")
    parser.add_argument("--spinup-days", type=int, default=mcl.DEFAULT_SPINUP_DAYS,
                        help="days of unmodified weather before the event")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")
    try:
        return run(args.stock, args.climate, args.out_dir,
                   per_cluster=None if (args.all or args.references) else args.per_cluster,
                   limit=args.limit, references=args.references,
                   workers=args.workers, resume=args.resume,
                   microclimate=args.microclimate, height_token=args.height,
                   spinup_days=args.spinup_days)
    except (cl.ClimateError, vm.ProfileDrift, mcl.MicroclimateError,
            RunAlreadyActive, LedgerNotEmpty, OSError) as exc:
        print(f"REFUSED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
