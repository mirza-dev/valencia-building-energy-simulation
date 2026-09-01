"""Thin orchestration of the microclimate event uncertainty study.

This adapter calls `src/lhs_event_study.py` and serialises its evidence.  It
does not compute physics, does not re-derive a range and does not repair a
failing check; the study owns all of that.

It is deliberately a **separate** module from `lhs_adapter.py`.  That file lists
its own path in `source_paths()`, so a single added line there would change its
hash, flip every committed Valencia LHS run to `current_compatibility.current ==
false` and replace the published band with an OUTDATED banner.  The annual
study, its adapter, its service, its fixture and `/api/lhs/*` are untouched by
design, and `run_type='lhs_event'` keeps event studies out of the annual list
that the product surface indexes with `[0]`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from workbench import stock_adapter

OUTPUT_COLUMNS = ("total_site_kwh_m2", "cooling_kwh_m2", "co2_kg_m2")


def _study():
    import lhs_event_study  # noqa: PLC0415 - imported lazily so import errors surface as capability failures

    return lhs_event_study


def source_paths() -> dict[str, tuple[Path, str]]:
    """What a committed event study is pinned to.

    The stock run's own inputs (prepared stock, climate bundle, template, slice)
    are pinned per run inside `run_event_lhs`, because they differ between runs;
    only the code is global.
    """
    study = _study()
    import deep_building as db
    import verified_model as vm

    return {
        "lhs_event_study": (Path(study.__file__).resolve(), "source"),
        "verified_model": (Path(vm.__file__).resolve(), "source"),
        "deep_building": (Path(db.__file__).resolve(), "source"),
        "lhs_event_adapter": (Path(__file__).resolve(), "source"),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _variable_fingerprint(catalog: list[dict[str, Any]]) -> str:
    payload = json.dumps(catalog, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def event_runs() -> list[dict[str, Any]]:
    """Stock runs a study can be built on: microclimate event runs that finished."""
    candidates: list[dict[str, Any]] = []
    for run in stock_adapter.list_runs():
        name = run.get("run") or run.get("name")
        if not name:
            continue
        try:
            config = json.loads(
                (stock_adapter.run_directory(name) / "run_config.json").read_text())
        except (OSError, ValueError, KeyError):
            continue
        micro = (config.get("worker_config") or {}).get("microclimate")
        if not micro:
            continue
        candidates.append({
            "name": name,
            "running": stock_adapter.active_process(name) is not None,
            "window": micro.get("window"),
            "days": micro.get("days"),
            "slice_fingerprint": (micro.get("slice") or {}).get("fingerprint"),
        })
    return candidates


def inspect(run_name: str, refparcela: str) -> dict[str, Any]:
    """What the study would do, without doing it."""
    study = _study()
    run_dir = stock_adapter.run_directory(run_name)
    ctx = study.load_context(run_dir, refparcela)
    catalog = study.variable_catalog(ctx)
    return {
        "schema_version": 1,
        "stock_run": run_name,
        "scope": refparcela,
        "cluster": ctx.cluster,
        "method": "latin_hypercube_uniform_spearman",
        "run_mode": "microclimate_event",
        "settings": {
            "n": int(study.DEFAULT_N),
            "seed": int(study.DEFAULT_SEED),
            "simulation_variables": len(study.simulation_variables(ctx)),
            "post_variables": len(study.POST_VARS),
            "workers": int(study.DEFAULT_WORKERS),
        },
        "event": {
            "window": list(ctx.window),
            "days": ctx.days,
            "window_label": f"{ctx.window[0]:02d}-{ctx.window[1]:02d}"
                            f"..{ctx.window[2]:02d}-{ctx.window[3]:02d}",
            "delta_peak_k": ctx.delta_peak_k,
            "delta_base_k": ctx.delta_base_k,
            "slice_fingerprint": ctx.slice_record["fingerprint"],
            "climate_fingerprint": ctx.climate_fingerprint,
            "template_fingerprint": ctx.template_fingerprint,
        },
        "variables": catalog,
        "variable_fingerprint": _variable_fingerprint(catalog),
        "excluded_variables": dict(study.EXCLUDED_VARIABLES),
        "outputs": list(OUTPUT_COLUMNS),
        "energy_period": f"kWh/m² over the {ctx.days}-day event",
        "ledger_anchor": {
            key: ctx.ledger_row.get(key)
            for key in ("total_site_kwh_m2", "cooling_kwh_m2", "space_heating_kwh_m2",
                        "dhw_kwh_m2", "res_area_m2")
            if key in ctx.ledger_row
        },
    }


class _ProgressHandler(logging.Handler):
    def __init__(self, progress: Callable[[str, float], None], n: int) -> None:
        super().__init__(level=logging.INFO)
        self._progress = progress
        self._n = max(1, n)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if not message.startswith("sample "):
            return
        try:
            done = int(message.split()[1].split("/")[0])
        except (IndexError, ValueError):
            return
        self._progress(f"EnergyPlus sample {done}/{self._n}", 0.10 + 0.72 * done / self._n)


def _statistics(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for column in OUTPUT_COLUMNS:
        series = frame[column].astype(float)
        stats[column] = {
            "mean": float(series.mean()),
            "median": float(series.median()),
            "p5": float(series.quantile(0.05)),
            "p95": float(series.quantile(0.95)),
            "minimum": float(series.min()),
            "maximum": float(series.max()),
            "stddev": float(series.std(ddof=1)),
        }
    return stats


def _sensitivity_records(summary: dict[str, list[tuple[str, float]]]) -> dict[str, list[dict[str, Any]]]:
    return {
        output: [
            {"variable": variable, "rho": float(rho), "rank": index}
            for index, (variable, rho) in enumerate(drivers, start=1)
        ]
        for output, drivers in summary.items()
    }


def _strata_pass(samples: pd.DataFrame, variables: dict[str, tuple[float, float]]) -> bool:
    n = len(samples)
    if n <= 0:
        return False
    for name, (low, high) in variables.items():
        if high <= low:
            return False
        normalized = (samples[name].astype(float) - low) / (high - low)
        strata = (normalized * n).clip(upper=n - 1).astype(int)
        if set(strata.tolist()) != set(range(n)):
            return False
    return True


def _qa(study, ctx, samples: pd.DataFrame, results: pd.DataFrame,
        sensitivity: dict[str, list[dict[str, Any]]], out_dir: Path, n: int) -> dict[str, Any]:
    variables = study.all_variables(ctx)
    expected_columns = set(variables) | set(OUTPUT_COLUMNS)
    finite_pass = all(
        math.isfinite(float(value))
        for column in expected_columns if column in results.columns
        for value in results[column]
    )
    range_pass = all(
        samples[name].between(float(bounds[0]), float(bounds[1]), inclusive="both").all()
        for name, bounds in variables.items()
    )
    sensitivity_pass = set(OUTPUT_COLUMNS) <= set(sensitivity) and all(
        len(drivers) == 3 and all(math.isfinite(float(item["rho"])) for item in drivers)
        for drivers in sensitivity.values()
    )
    artifact_pass = all(
        (out_dir / name).is_file() and (out_dir / name).stat().st_size > 0
        for name in ("samples.csv", "runs.csv", "histograms.png", "tornado.png", "summary.txt")
    )
    # Every sampled output must actually vary; a constant column would make its
    # Spearman rank correlation meaningless rather than zero, and publishing a
    # band around it would claim a spread that was never measured.
    varying = {
        column: float(results[column].astype(float).max() - results[column].astype(float).min())
        for column in OUTPUT_COLUMNS
    }
    variation_pass = all(spread > 1e-9 for spread in varying.values())
    qa_all = bool(results["qa_passed"].all()) if "qa_passed" in results.columns else False
    # The published band belongs to a run whose inputs are exactly the stock
    # run's; a mismatch here means the study sampled a different building.
    epw_distinct = int(results["event_epw"].nunique()) if "event_epw" in results.columns else 0

    checks = [
        {"name": "sample_count", "passed": len(results) == n, "expected": n, "actual": len(results)},
        {"name": "result_schema", "passed": expected_columns <= set(results.columns),
         "expected": len(expected_columns), "actual": len(expected_columns & set(results.columns))},
        {"name": "finite_values", "passed": finite_pass, "expected": True, "actual": finite_pass},
        {"name": "variable_ranges", "passed": bool(range_pass), "expected": True, "actual": bool(range_pass)},
        {"name": "lhs_stratification", "passed": _strata_pass(samples, variables),
         "expected": True, "actual": _strata_pass(samples, variables)},
        {"name": "nonnegative_outputs",
         "passed": bool((results[list(OUTPUT_COLUMNS)] >= 0).all().all()),
         "expected": True, "actual": bool((results[list(OUTPUT_COLUMNS)] >= 0).all().all())},
        {"name": "outputs_vary", "passed": variation_pass, "expected": True, "actual": varying},
        {"name": "per_sample_qa", "passed": qa_all, "expected": True, "actual": qa_all},
        {"name": "distinct_event_weather", "passed": epw_distinct > 1,
         "expected": ">1", "actual": epw_distinct},
        {"name": "sensitivity_top3", "passed": sensitivity_pass, "expected": True, "actual": sensitivity_pass},
        {"name": "core_artifacts", "passed": artifact_pass, "expected": True, "actual": artifact_pass},
    ]
    all_pass = all(bool(item["passed"]) for item in checks)
    return {
        "all_pass": all_pass,
        "scientific_status": "VALIDATED" if all_pass else "UNVERIFIED",
        "checks": checks,
        "constant_outputs": study.constant_outputs(results),
    }


def run_event_lhs(out_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Execute the study and serialise its scientific evidence."""
    study = _study()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    progress: Callable[[str, float], None] = settings.get("_progress", lambda *_: None)

    run_name = str(settings["stock_run"])
    refparcela = str(settings["refparcela"])
    n = int(settings.get("n", study.DEFAULT_N))
    seed = int(settings.get("seed", study.DEFAULT_SEED))
    workers = int(settings.get("workers", study.DEFAULT_WORKERS))

    progress("Reconstructing the run's inputs", 0.03)
    ctx = study.load_context(stock_adapter.run_directory(run_name), refparcela)

    progress("Sampling", 0.06)
    variables = study.all_variables(ctx)
    samples = study.sample_matrix(n, seed, variables)
    samples.to_csv(out_dir / "samples.csv", index=False)

    handler = _ProgressHandler(progress, n)
    previous = study.logger.level
    study.logger.addHandler(handler)
    study.logger.setLevel(logging.INFO)
    try:
        progress(f"EnergyPlus sample 0/{n}", 0.10)
        results = study.run_study(ctx, samples, out_dir, workers=workers)
    finally:
        study.logger.removeHandler(handler)
        study.logger.setLevel(previous)
    results.to_csv(out_dir / "runs.csv", index=False)

    progress("Distributions", 0.86)
    study.make_histograms(results, out_dir, ctx.days)
    progress("Sensitivity", 0.90)
    sensitivity_raw = study.make_tornado(results, study.all_variables(ctx), out_dir)
    sensitivity = _sensitivity_records(sensitivity_raw)
    summary_text = study.summarize(ctx, results, sensitivity_raw, out_dir)

    statistics = _statistics(results)
    catalog = study.variable_catalog(ctx)
    qa = _qa(study, ctx, samples, results, sensitivity, out_dir, n)
    clean = {key: _json_value(value) for key, value in settings.items() if not key.startswith("_")}

    result = {
        "schema_version": 1,
        "settings": clean,
        "summary": {
            "stock_run": run_name,
            "scope": refparcela,
            "cluster": ctx.cluster,
            "run_mode": "microclimate_event",
            "event_days": ctx.days,
            "event_window": f"{ctx.window[0]:02d}-{ctx.window[1]:02d}"
                            f"..{ctx.window[2]:02d}-{ctx.window[3]:02d}",
            "energy_period": f"kWh/m² over the {ctx.days}-day event",
            "samples_expected": n,
            "samples_completed": len(results),
            "simulation_variables": len(study.simulation_variables(ctx)),
            "post_variables": len(study.POST_VARS),
            "statistics": statistics,
        },
        "provenance": {
            "prepared_stock": str(ctx.gis_path),
            "climate_fingerprint": ctx.climate_fingerprint,
            "template_fingerprint": ctx.template_fingerprint,
            "slice": ctx.slice_record,
            "delta_peak_k": ctx.delta_peak_k,
            "delta_base_k": ctx.delta_base_k,
            "zero_policy": ctx.zero_policy,
            "pinned_envelope": ctx.envelope,
            "floor_u": ctx.floor_u,
        },
        "qa": qa,
        "variables": catalog,
        "variable_fingerprint": _variable_fingerprint(catalog),
        "excluded_variables": dict(study.EXCLUDED_VARIABLES),
        "sensitivity": sensitivity,
        "samples": json.loads(results.to_json(orient="records")),
        "figures": {"distributions": "histograms.png", "sensitivity": "tornado.png"},
        "summary_text": summary_text,
        "ledger_anchor": {
            key: ctx.ledger_row.get(key)
            for key in ("total_site_kwh_m2", "cooling_kwh_m2", "space_heating_kwh_m2",
                        "dhw_kwh_m2", "res_area_m2")
            if key in ctx.ledger_row
        },
    }
    for name, payload in (("statistics.json", statistics), ("sensitivity.json", sensitivity),
                          ("qa.json", qa), ("results.json", result)):
        (out_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    progress("Finalize", 0.96)
    return result
