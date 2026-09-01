"""Uncertainty study for a microclimate event run.

The annual study in ``lhs_study.py`` samples the Valencia pilot on the retired
representative-cluster chain: ideal loads, no domestic hot water, no heat pump,
a massless wall.  An event run is none of those things.  Carrying that study's
band over to a Lecco result would print one engine's uncertainty next to another
engine's numbers, so this module runs a second study **on the event run's own
engine**, over the event's own eight days, and keeps the two apart everywhere:
separate module, separate adapter, separate ``run_type``.

Nothing here writes physics.  Every sample is the production call the stock
runner itself makes -- ``verified_model.simulate_verified_building`` with the
run's climate, template, slice-derived event and the row's pinned envelope --
perturbed only from the outside through ``BuildConfig.with_legacy_params``.
``assert_profile_intact()`` therefore still fires on every sample, against the
same four locked source hashes.

Three inputs are recorded in ``run_config.json`` by **fingerprint and not by
path**, and in all three cases a file of the same name that is *not* the one the
run used exists on disk: the climate bundle (a second
``managed_milano-bergamo.intl.ap`` with mains 10.0 instead of 13.7), the
template (``data/templates/PlantillaOS_v2.osm`` differs from the managed import
the run consumed) and the slice.  All three are therefore resolved by scanning
for a fingerprint match and the study refuses to start without one.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.stats import qmc, spearmanr  # noqa: E402

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import climate as cl  # noqa: E402
import microclimate as mcl  # noqa: E402
import stock_runner as sr  # noqa: E402
import template_contract as tpl  # noqa: E402
import verified_model as vm  # noqa: E402

PROJECT = _SRC.parent
logger = logging.getLogger("lhs_event")

DEFAULT_N = 50
DEFAULT_SEED = 42
DEFAULT_WORKERS = 6

#: The envelope U-values are sampled as a fraction of *this building's own*
#: pinned values rather than over the annual study's absolute ranges, which were
#: drawn from the Spanish IVE 1960-80 stock and do not transfer to an Italian
#: archetype.  The width itself is an assumption and is labelled as one.
ENVELOPE_SPREAD = 0.25

#: Electricity is the sampled carbon term; the gas factor is the run's own CTE
#: constant and is not a knob in the annual register either.
GAS_EMISSION_FACTOR = 0.252

FIXED_SIM_VARS: dict[str, tuple[float, float]] = {
    "window_g": (0.70, 0.85),
    "infiltration_ach": (0.1, 0.5),
    "shade_setpoint": (150.0, 400.0),
    "thermal_bridge_du": (0.0, 0.2),
    "microclimate_delta_scale": (0.5, 1.5),
}

POST_VARS: dict[str, tuple[float, float]] = {
    "emission_factor": (0.15, 0.331),
}

ENVELOPE_VARS = ("wall_u", "roof_u", "window_u")

OUTPUT_COLUMNS = ("total_site_kwh_m2", "cooling_kwh_m2", "co2_kg_m2")

#: Every variable declares whether its range came from a source or was assumed.
#: The distinction is printed in the CSV, the register and the product surface;
#: an assumed range that is presented as a sourced one is the failure this
#: project has had to retract before.
VARIABLE_SOURCE: dict[str, str] = {
    "wall_u": "assumed",
    "roof_u": "assumed",
    "window_u": "assumed",
    "window_g": "sourced",
    "infiltration_ach": "sourced",
    "shade_setpoint": "sourced",
    "thermal_bridge_du": "sourced",
    "microclimate_delta_scale": "assumed",
    "emission_factor": "sourced",
}

VARIABLE_META: dict[str, dict[str, str]] = {
    "wall_u": {"group": "simulation", "unit": "W/m2K", "domain": "envelope"},
    "roof_u": {"group": "simulation", "unit": "W/m2K", "domain": "envelope"},
    "window_u": {"group": "simulation", "unit": "W/m2K", "domain": "openings"},
    "window_g": {"group": "simulation", "unit": "-", "domain": "openings"},
    "infiltration_ach": {"group": "simulation", "unit": "1/h", "domain": "operation"},
    "shade_setpoint": {"group": "simulation", "unit": "W/m2", "domain": "operation"},
    "thermal_bridge_du": {"group": "simulation", "unit": "W/m2K", "domain": "envelope"},
    "microclimate_delta_scale": {"group": "simulation", "unit": "-", "domain": "microclimate"},
    "emission_factor": {"group": "post", "unit": "kgCO2/kWh", "domain": "carbon"},
}

#: ``cop`` and ``seer`` are deliberately absent.  The annual register carries
#: them because that chain reports ideal-loads *demand*, which has to be turned
#: into consumption before it can be turned into carbon.  An event run already
#: reports metered PTHP *consumption* under a locked COP (4.07 heating / 5.0
#: cooling); dividing the result by a second COP would count the system twice.
EXCLUDED_VARIABLES: dict[str, str] = {
    "cop": "event runs report metered PTHP consumption; the COP is already applied and locked",
    "seer": "event runs report metered PTHP consumption; the SEER is already applied and locked",
}


class EventStudyError(RuntimeError):
    """The study cannot be reconstructed from the run it was asked about."""


# ---------------------------------------------------------------------------
# 1. Reconstructing the run's own inputs
# ---------------------------------------------------------------------------

@dataclass
class EventContext:
    """Everything the study needs, all of it taken from the run itself."""

    run_dir: Path
    refparcela: str
    gis_path: Path
    climate_path: Path
    template_path: Path
    slice_dir: Path
    climate_fingerprint: str
    template_fingerprint: str
    slice_record: dict[str, Any]
    window: tuple[int, int, int, int, int]
    days: int
    zero_policy: str
    delta_peak_k: float
    delta_base_k: float
    sample_radius_m: float
    sample_cells: int
    envelope: dict[str, float]
    floor_u: float | None
    cluster: str
    ledger_row: dict[str, Any] = field(default_factory=dict)

    def plan(self) -> dict[str, Any]:
        """The serialisable slice of the context a worker process needs."""
        return {
            "refparcela": self.refparcela,
            "gis_path": str(self.gis_path),
            "climate_path": str(self.climate_path),
            "template_path": str(self.template_path),
            "window": list(self.window),
            "days": self.days,
            "zero_policy": self.zero_policy,
            "delta_peak_k": self.delta_peak_k,
            "delta_base_k": self.delta_base_k,
            "sample_radius_m": self.sample_radius_m,
            "sample_cells": self.sample_cells,
            "slice_record": self.slice_record,
            "envelope": dict(self.envelope),
            "floor_u": self.floor_u,
        }


def _resolve_by_fingerprint(candidates, loader, fingerprint: str, what: str):
    for candidate in candidates:
        try:
            loaded = loader(candidate)
        except Exception:
            continue
        if getattr(loaded, "fingerprint", None) == fingerprint:
            return candidate, loaded
    raise EventStudyError(
        f"no {what} on disk carries the fingerprint {fingerprint[:16]} recorded by the run; "
        f"refusing to substitute a same-named file"
    )


def resolve_climate(fingerprint: str, *, project: Path = PROJECT):
    return _resolve_by_fingerprint(
        sorted((project / "var/climates").glob("*.json")),
        cl.load_climate, fingerprint, "climate bundle",
    )


def resolve_template(fingerprint: str, *, project: Path = PROJECT):
    candidates = sorted((project / "var/imports").glob("*/*.osm"))
    default = Path(tpl.DEFAULT_TEMPLATE)
    if default.is_file():
        candidates.append(default)
    return _resolve_by_fingerprint(
        candidates,
        lambda path: tpl.load_template_set(path, project / "var/templates"),
        fingerprint, "template",
    )


def resolve_slice(fingerprint: str, *, project: Path = PROJECT):
    candidates = sorted(path.parent for path in (project / "var/imports").glob("*/slice/meta.json"))
    candidates += sorted((project / "var/imports").glob("*/slice"))
    seen: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    return _resolve_by_fingerprint(seen, mcl.load_slice, fingerprint, "microclimate slice")


def load_context(run_dir: str | Path, refparcela: str, *, project: Path = PROJECT) -> EventContext:
    """Rebuild the event run's inputs, refusing every same-named substitute."""
    import geopandas as gpd

    run_dir = Path(run_dir)
    config_path = run_dir / "run_config.json"
    if not config_path.is_file():
        raise EventStudyError(f"{config_path} is missing; this is not a stock run directory")
    config = json.loads(config_path.read_text())
    worker = config.get("worker_config", {})
    micro = worker.get("microclimate")
    if not micro:
        raise EventStudyError(
            f"{run_dir.name} is not a microclimate event run; the annual study applies there"
        )

    gis_path = Path(worker["prepared_gis"])
    if not gis_path.is_file():
        raise EventStudyError(f"the run's prepared stock is gone: {gis_path}")

    climate_fp = config["climate"]["fingerprint"]
    climate_path, climate = resolve_climate(climate_fp, project=project)
    template_fp = config["template"]["fingerprint"]
    template_path, template = resolve_template(template_fp, project=project)
    slice_fp = micro["slice"]["fingerprint"]
    slice_dir, slice_ = resolve_slice(slice_fp, project=project)

    stock = gpd.read_file(gis_path, where=f"refparcela = '{refparcela}'")
    if len(stock) != 1:
        raise EventStudyError(
            f"expected exactly one row for {refparcela} in the run's stock, found {len(stock)}"
        )
    row = stock.iloc[0]

    deltas = mcl.sample_stock(slice_, stock)
    entry = deltas.iloc[0]
    if str(entry["sample_status"]) != "ok":
        raise EventStudyError(
            f"{refparcela} is not covered by the slice ({entry['sample_status']}); "
            f"a study on it would sample nothing"
        )

    _, frame = mcl.read_epw(Path(climate.epw_path))
    window = mcl.hottest_window(frame, spinup_days=mcl.DEFAULT_SPINUP_DAYS)
    days = sr._window_days(window, frame)
    recorded = tuple(micro["window"])
    if tuple(window) != recorded or days != int(micro["days"]):
        raise EventStudyError(
            f"the event window rebuilt here {tuple(window)}/{days}d does not match the one the "
            f"run recorded {recorded}/{micro['days']}d"
        )

    ledger_row: dict[str, Any] = {}
    ledger = run_dir / "ledger.jsonl"
    if ledger.is_file():
        with ledger.open() as handle:
            for line in handle:
                candidate = json.loads(line)
                if candidate.get("refparcela") == refparcela:
                    ledger_row = candidate

    envelope = {name: float(row[name]) for name in ENVELOPE_VARS if row.get(name) is not None}
    missing = [name for name in ENVELOPE_VARS if name not in envelope]
    if missing:
        raise EventStudyError(
            f"{refparcela} carries no pinned {', '.join(missing)}; the envelope ranges are "
            f"defined relative to the row's own values and cannot be built"
        )
    floor_u = row.get("floor_u")

    return EventContext(
        run_dir=run_dir, refparcela=refparcela, gis_path=gis_path,
        climate_path=climate_path, template_path=template_path, slice_dir=slice_dir,
        climate_fingerprint=climate_fp, template_fingerprint=template_fp,
        slice_record=slice_.record(), window=tuple(window), days=days,
        zero_policy=str(worker.get("zero_policy", "literal_zero")),
        delta_peak_k=float(entry["delta_peak_k"]), delta_base_k=float(entry["delta_base_k"]),
        sample_radius_m=float(entry["sample_radius_m"]), sample_cells=int(entry["sample_cells"]),
        envelope=envelope, floor_u=None if floor_u is None else float(floor_u),
        cluster=str(row.get("cluster", "")), ledger_row=ledger_row,
    )


# ---------------------------------------------------------------------------
# 2. The register and the sampling matrix
# ---------------------------------------------------------------------------

def simulation_variables(ctx: EventContext) -> dict[str, tuple[float, float]]:
    """The eight sampled simulation variables for this building."""
    variables: dict[str, tuple[float, float]] = {}
    for name in ENVELOPE_VARS:
        pinned = ctx.envelope[name]
        variables[name] = (
            round(pinned * (1.0 - ENVELOPE_SPREAD), 4),
            round(pinned * (1.0 + ENVELOPE_SPREAD), 4),
        )
    variables.update(FIXED_SIM_VARS)
    return variables


def all_variables(ctx: EventContext) -> dict[str, tuple[float, float]]:
    return {**simulation_variables(ctx), **POST_VARS}


def variable_catalog(ctx: EventContext) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for name, bounds in all_variables(ctx).items():
        meta = VARIABLE_META[name]
        entry = {
            "name": name,
            "minimum": float(bounds[0]),
            "maximum": float(bounds[1]),
            "distribution": "uniform",
            "group": meta["group"],
            "unit": meta["unit"],
            "domain": meta["domain"],
            "source": VARIABLE_SOURCE[name],
        }
        if name in ENVELOPE_VARS:
            entry["pinned_value"] = float(ctx.envelope[name])
            entry["spread_fraction"] = ENVELOPE_SPREAD
        catalog.append(entry)
    return catalog


def sample_matrix(n: int, seed: int, variables: dict[str, tuple[float, float]]) -> pd.DataFrame:
    names = list(variables)
    sampler = qmc.LatinHypercube(d=len(names), seed=seed)
    unit = sampler.random(n)
    lower = [variables[name][0] for name in names]
    upper = [variables[name][1] for name in names]
    scaled = qmc.scale(unit, lower, upper)
    frame = pd.DataFrame(scaled, columns=names)
    frame.insert(0, "sample", range(1, n + 1))
    return frame


# ---------------------------------------------------------------------------
# 3. One sample -- the production call, perturbed from outside
# ---------------------------------------------------------------------------

_WORKER: dict[str, Any] = {}


def _worker_init(plan: dict[str, Any], epw_dir: str) -> None:
    _WORKER.clear()
    _WORKER["plan"] = plan
    _WORKER["epw_dir"] = Path(epw_dir)
    _WORKER["climate"] = cl.load_climate(Path(plan["climate_path"]))
    _WORKER["template"] = tpl.load_template_set(
        Path(plan["template_path"]), PROJECT / "var/templates",
    )


def simulate_sample(sample: dict[str, Any], *, plan: dict[str, Any] | None = None,
                    climate=None, template=None, epw_dir: Path | None = None,
                    out_dir: Path) -> dict[str, Any]:
    """Run one perturbed copy of the run's own building."""
    plan = plan if plan is not None else _WORKER["plan"]
    climate = climate if climate is not None else _WORKER["climate"]
    template = template if template is not None else _WORKER["template"]
    epw_dir = epw_dir if epw_dir is not None else _WORKER["epw_dir"]

    scale = float(sample["microclimate_delta_scale"])
    delta_peak = plan["delta_peak_k"] * scale
    delta_base = plan["delta_base_k"] * scale
    window = tuple(plan["window"])
    epw_dir.mkdir(parents=True, exist_ok=True)
    epw = mcl.write_event_epw(
        Path(climate.epw_path), epw_dir,
        delta_peak_k=delta_peak, delta_base_k=delta_base, window=window,
    )
    event = {
        "epw_path": epw, "window": window, "days": int(plan["days"]),
        "slice_record": plan["slice_record"],
        "delta_peak_k": delta_peak, "delta_base_k": delta_base,
        "sample_radius_m": float(plan["sample_radius_m"]),
        "sample_cells": int(plan["sample_cells"]),
    }

    base = sr.build_config_for(climate, template)
    config = base.model_copy(deep=True).with_legacy_params({
        "wall_u": float(sample["wall_u"]),
        "roof_u": float(sample["roof_u"]),
        "window_u": float(sample["window_u"]),
        "window_g": float(sample["window_g"]),
        "infiltration_ach": float(sample["infiltration_ach"]),
        "shade_setpoint": float(sample["shade_setpoint"]),
        "thermal_bridge_du": float(sample["thermal_bridge_du"]),
        "massless": False,
    })

    out_dir.mkdir(parents=True, exist_ok=True)
    summary, qa_passed = vm.simulate_verified_building(
        plan["refparcela"], out_dir,
        zero_policy=plan["zero_policy"],
        gis_path=Path(plan["gis_path"]),
        neighbors_path=Path(plan["gis_path"]),
        floor_u=plan["floor_u"], event=event, climate=climate, config=config,
    )

    results = summary.get("results", summary)
    # An eight-day result is around 1 kWh/m2, so the annual reader's two decimals
    # would quantise the whole study into a handful of steps.  Event runs already
    # carry `read_site_energy_precise` for exactly this reason; use it, and fail
    # loudly rather than silently falling back to the coarse field.
    if "total_site_kwh_m2_precise" not in results:
        raise EventStudyError(
            "the run returned no precise site energy; this is not an event-mode result"
        )
    total = float(results["total_site_kwh_m2_precise"])
    cooling = float(results["cooling_kwh_m2_precise"])
    # Gas is domestic hot water only: per-person demand under a locked boiler, so
    # it is invariant under every sampled knob.  It is read at the annual
    # reader's precision, subtracted to isolate electricity, and its constancy is
    # asserted by the study's own QA rather than assumed here.
    gas = float(results.get("site_gas_kwh_m2", 0.0) or 0.0)
    elec = total - gas
    record: dict[str, Any] = {key: float(value) for key, value in sample.items() if key != "sample"}
    record["sample"] = int(sample["sample"])
    record["total_site_kwh_m2"] = total
    record["cooling_kwh_m2"] = cooling
    record["space_heating_kwh_m2"] = float(results.get("space_heating_kwh_m2", 0.0) or 0.0)
    record["dhw_kwh_m2"] = float(results.get("dhw_kwh_m2", 0.0) or 0.0)
    record["site_elec_kwh_m2"] = elec
    record["site_gas_kwh_m2"] = gas
    record["co2_kg_m2"] = elec * float(sample["emission_factor"]) + gas * GAS_EMISSION_FACTOR
    record["delta_peak_k"] = delta_peak
    record["delta_base_k"] = delta_base
    record["event_epw"] = epw.name
    record["qa_passed"] = bool(qa_passed)
    return record


def _run_sample_task(args: tuple[dict[str, Any], str]) -> dict[str, Any]:
    sample, out_dir = args
    try:
        record = simulate_sample(sample, out_dir=Path(out_dir))
    except Exception as error:  # a failed sample is reported, never silently dropped
        return {"sample": int(sample["sample"]), "error": f"{type(error).__name__}: {error}"}
    return record


def run_study(ctx: EventContext, samples: pd.DataFrame, out_dir: Path, *,
              workers: int = DEFAULT_WORKERS, keep_models: bool = False) -> pd.DataFrame:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    epw_dir = out_dir / "event_epw"
    plan = ctx.plan()
    tasks = [
        ({key: sample[key] for key in samples.columns}, str(out_dir / f"sample_{int(sample['sample']):03d}"))
        for _, sample in samples.iterrows()
    ]

    records: list[dict[str, Any]] = []
    total = len(tasks)
    workers = max(1, min(int(workers), total))
    if workers == 1:
        _worker_init(plan, str(epw_dir))
        for index, task in enumerate(tasks, start=1):
            records.append(_run_sample_task(task))
            logger.info("sample %d/%d", index, total)
    else:
        # Explicit spawn: `run_energyplus` drives the in-process pyenergyplus API,
        # which is not fork-safe.  Spawn re-imports the caller's `__main__`, so any
        # caller must keep its own entry point behind an `if __name__` guard --
        # the CLI below and `workbench.worker` both do.
        import multiprocessing

        with futures.ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
            initializer=_worker_init, initargs=(plan, str(epw_dir)),
        ) as pool:
            for index, record in enumerate(pool.map(_run_sample_task, tasks), start=1):
                records.append(record)
                logger.info("sample %d/%d", index, total)

    failures = [record for record in records if "error" in record]
    if failures:
        raise EventStudyError(
            f"{len(failures)}/{total} samples failed; first: {failures[0]['error']}"
        )

    frame = pd.DataFrame(records).sort_values("sample").reset_index(drop=True)
    if not keep_models:
        import shutil
        for task in tasks:
            shutil.rmtree(task[1], ignore_errors=True)
    return frame


# ---------------------------------------------------------------------------
# 4. Distributions, drivers, summary
# ---------------------------------------------------------------------------

#: Why an output can be measured and still not move.  Stated per column so the
#: summary never explains one constant with another one's reason.
CONSTANT_REASONS = {
    "space_heating_kwh_m2": "the event window falls in August, so no heating is called for",
    "site_gas_kwh_m2": "gas is domestic hot water only, driven by occupancy under a locked boiler",
}


def constant_outputs(frame: pd.DataFrame) -> dict[str, float]:
    """Outputs the study measured and found invariant -- reported, not dropped."""
    constant: dict[str, float] = {}
    for column in ("space_heating_kwh_m2", "site_gas_kwh_m2"):
        if column in frame.columns:
            values = frame[column].astype(float)
            if float(values.max() - values.min()) <= 1e-9:
                constant[column] = float(values.iloc[0])
    return constant


def make_histograms(frame: pd.DataFrame, out_dir: Path, days: int) -> Path:
    fig, axes = plt.subplots(1, len(OUTPUT_COLUMNS), figsize=(4.2 * len(OUTPUT_COLUMNS), 3.6))
    labels = {
        "total_site_kwh_m2": f"Total site energy [kWh/m² over the {days}-day event]",
        "cooling_kwh_m2": f"Space cooling [kWh/m² over the {days}-day event]",
        "co2_kg_m2": f"Operational carbon [kgCO₂/m² over the {days}-day event]",
    }
    for axis, column in zip(np.atleast_1d(axes), OUTPUT_COLUMNS):
        values = frame[column].astype(float)
        axis.hist(values, bins=12, color="#4f8f6d", edgecolor="white")
        axis.axvline(float(values.median()), color="#b4472f", linestyle="--", linewidth=1.4,
                     label=f"median {values.median():.3f}")
        axis.set_xlabel(labels[column], fontsize=8)
        axis.set_ylabel("samples", fontsize=8)
        axis.tick_params(labelsize=7)
        axis.legend(fontsize=7)
    fig.tight_layout()
    target = out_dir / "histograms.png"
    fig.savefig(target, dpi=110)
    plt.close(fig)
    return target


def make_tornado(frame: pd.DataFrame, variables: dict[str, tuple[float, float]],
                 out_dir: Path) -> dict[str, list[tuple[str, float]]]:
    names = list(variables)
    summary: dict[str, list[tuple[str, float]]] = {}
    fig, axes = plt.subplots(1, len(OUTPUT_COLUMNS), figsize=(4.6 * len(OUTPUT_COLUMNS), 4.0))
    for axis, column in zip(np.atleast_1d(axes), OUTPUT_COLUMNS):
        rhos: list[tuple[str, float]] = []
        for name in names:
            values = frame[name].astype(float)
            target = frame[column].astype(float)
            if float(target.max() - target.min()) <= 1e-12:
                rho = 0.0
            else:
                rho = float(spearmanr(values, target).statistic)
                if not math.isfinite(rho):
                    rho = 0.0
            rhos.append((name, rho))
        rhos.sort(key=lambda item: abs(item[1]), reverse=True)
        summary[column] = rhos[:3]
        top = rhos[:8][::-1]
        axis.barh([item[0] for item in top], [item[1] for item in top],
                  color=["#b4472f" if item[1] < 0 else "#4f8f6d" for item in top])
        axis.axvline(0.0, color="#333", linewidth=0.8)
        axis.set_xlim(-1.0, 1.0)
        axis.set_title(column, fontsize=9)
        axis.tick_params(labelsize=7)
    fig.tight_layout()
    fig.savefig(out_dir / "tornado.png", dpi=110)
    plt.close(fig)
    return summary


def summarize(ctx: EventContext, frame: pd.DataFrame,
              drivers: dict[str, list[tuple[str, float]]], out_dir: Path) -> str:
    lines: list[str] = []
    lines.append("MICROCLIMATE EVENT UNCERTAINTY STUDY")
    lines.append(f"stock run       : {ctx.run_dir.name}")
    lines.append(f"building        : {ctx.refparcela}  ({ctx.cluster})")
    lines.append(f"event window    : {ctx.window[0]:02d}-{ctx.window[1]:02d}"
                 f"..{ctx.window[2]:02d}-{ctx.window[3]:02d}  ({ctx.days} days)")
    lines.append(f"slice           : {ctx.slice_record['fingerprint'][:16]}")
    lines.append(f"climate bundle  : {ctx.climate_fingerprint[:16]}")
    lines.append(f"template        : {ctx.template_fingerprint[:16]}")
    lines.append(f"samples         : {len(frame)}")
    lines.append("")
    lines.append(f"All energy below is over the {ctx.days}-day event, not a year.")
    lines.append("")
    for column in OUTPUT_COLUMNS:
        values = frame[column].astype(float)
        lines.append(
            f"{column:22s} mean {values.mean():8.4f}   P5 {values.quantile(0.05):8.4f}   "
            f"median {values.median():8.4f}   P95 {values.quantile(0.95):8.4f}"
        )
    constants = constant_outputs(frame)
    if constants:
        lines.append("")
        for column, value in constants.items():
            lines.append(
                f"{column}: measured constant at {value:.4f} across all {len(frame)} samples "
                f"-- {CONSTANT_REASONS.get(column, 'no sampled variable reaches it')}; "
                f"reported, not omitted."
            )
    lines.append("")
    lines.append("Rank drivers (Spearman rho, top 3):")
    for column, items in drivers.items():
        rendered = "  ".join(f"{name} {rho:+.3f}" for name, rho in items)
        lines.append(f"  {column:22s} {rendered}")
    lines.append("")
    lines.append("Excluded on purpose:")
    for name, reason in EXCLUDED_VARIABLES.items():
        lines.append(f"  {name}: {reason}")
    assumed = [name for name, source in VARIABLE_SOURCE.items() if source == "assumed"]
    lines.append("")
    lines.append(f"Assumed ranges (no external source): {', '.join(assumed)}")
    lines.append("")
    lines.append("The band belongs to this one building under this one event, not to the run's totals.")
    text = "\n".join(lines) + "\n"
    (out_dir / "summary.txt").write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="stock run directory (a microclimate event run)")
    parser.add_argument("--refparcela", required=True)
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--keep-models", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        ctx = load_context(args.run, args.refparcela)
    except EventStudyError as error:
        logger.error("%s", error)
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else (
        PROJECT / "out/lhs_event" / ctx.run_dir.name / ctx.refparcela
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    variables = all_variables(ctx)
    samples = sample_matrix(args.n, args.seed, variables)
    samples.to_csv(out_dir / "samples.csv", index=False)

    frame = run_study(ctx, samples, out_dir, workers=args.workers, keep_models=args.keep_models)
    frame.to_csv(out_dir / "runs.csv", index=False)

    make_histograms(frame, out_dir, ctx.days)
    drivers = make_tornado(frame, all_variables(ctx), out_dir)
    text = summarize(ctx, frame, drivers, out_dir)
    print(text)
    logger.info("outputs: %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
