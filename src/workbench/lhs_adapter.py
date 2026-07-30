"""Thin serialization adapter for the user-owned LHS uncertainty study.

Sampling, model construction, EnergyPlus execution, carbon arithmetic and
Spearman analysis remain in ``src/lhs_study.py``. The Workbench adds only a
locked invocation contract, progress forwarding, structured QA and artifacts.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Callable

import pandas as pd


REQUIRED_ENTRYPOINTS = (
    "sample_matrix",
    "run_study",
    "make_histograms",
    "make_tornado",
    "summarize",
)
OUTPUT_COLUMNS = (
    "heating_kwh_m2",
    "cooling_kwh_m2",
    "consumption_kwh_m2",
    "co2_kg_m2",
    "co2_t_building",
)
VARIABLE_METADATA = {
    "wall_u": {"group": "simulation", "unit": "W/m2K", "domain": "envelope"},
    "roof_u": {"group": "simulation", "unit": "W/m2K", "domain": "envelope"},
    "window_u": {"group": "simulation", "unit": "W/m2K", "domain": "openings"},
    "window_g": {"group": "simulation", "unit": "-", "domain": "openings"},
    "infiltration_ach": {"group": "simulation", "unit": "1/h", "domain": "operation"},
    "shade_setpoint": {"group": "simulation", "unit": "W/m2", "domain": "operation"},
    "thermal_bridge_du": {"group": "simulation", "unit": "W/m2K", "domain": "envelope"},
    "cop": {"group": "post", "unit": "-", "domain": "system"},
    "seer": {"group": "post", "unit": "-", "domain": "system"},
    "emission_factor": {"group": "post", "unit": "kgCO2/kWh", "domain": "carbon"},
}


def _lhs():
    module = importlib.import_module("lhs_study")
    missing = [name for name in REQUIRED_ENTRYPOINTS if not callable(getattr(module, name, None))]
    if missing:
        raise RuntimeError(f"LHS callable contract is incomplete: {', '.join(missing)}")
    return module


def source_paths() -> dict[str, tuple[Path, str]]:
    """Return every real source/input used by the locked LHS baseline."""
    lhs = _lhs()
    return {
        "pilot_gis": (Path(lhs.mb.BUILDINGS_GPKG), "gis"),
        "context_gis": (Path(lhs.mb.NEIGHBORS_SHP), "gis"),
        "template": (Path(lhs.mb.TEMPLATE_OSM), "template"),
        "weather": (Path(lhs.mb.EPW_FILE), "weather"),
        "lhs_study": (Path(lhs.__file__).resolve(), "source"),
        "model_builder": (Path(lhs.mb.__file__).resolve(), "source"),
        "run_simulation": (Path(lhs.sim.__file__).resolve(), "source"),
        "lhs_adapter": (Path(__file__).resolve(), "source"),
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        return _json_value(value.item())
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [_json_value(item) for item in frame.to_dict(orient="records")]


def _variable_catalog(lhs) -> list[dict[str, Any]]:
    records = []
    for name, bounds in lhs.ALL_VARS.items():
        metadata = VARIABLE_METADATA.get(name, {})
        records.append({
            "name": name,
            "minimum": float(bounds[0]),
            "maximum": float(bounds[1]),
            "distribution": "uniform",
            "group": metadata.get("group", "unknown"),
            "unit": metadata.get("unit", "-"),
            "domain": metadata.get("domain", "unknown"),
        })
    return records


def _variable_fingerprint(catalog: list[dict[str, Any]]) -> str:
    payload = json.dumps(catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def inspect_lhs() -> dict[str, Any]:
    """Return the locked study protocol without running EnergyPlus."""
    lhs = _lhs()
    catalog = _variable_catalog(lhs)
    return {
        "schema_version": 1,
        "scope": "4252702YJ2745A",
        "method": "latin_hypercube_uniform_spearman",
        "locked": True,
        "settings": {
            "n": int(lhs.DEFAULT_N),
            "seed": int(lhs.DEFAULT_SEED),
            "simulation_variables": len(lhs.SIM_VARS),
            "post_variables": len(lhs.POST_VARS),
            "model_path": "massless",
            "context_shading": True,
            "estimated_minutes": 20,
        },
        "variables": catalog,
        "variable_fingerprint": _variable_fingerprint(catalog),
        "baselines": {
            "massless": _json_value(lhs.BASELINE_MASSLESS),
            "layered": _json_value(lhs.BASELINE_LAYERED),
            "cadastre": _json_value(lhs.CADASTRE),
        },
        "outputs": list(OUTPUT_COLUMNS),
    }


class _ProgressHandler(logging.Handler):
    def __init__(self, progress: Callable[[str, float], None], n: int):
        super().__init__(level=logging.INFO)
        self.progress = progress
        self.n = n

    def emit(self, record: logging.LogRecord) -> None:
        match = re.search(r"\[lhs\]\s+run\s+(\d+)/(\d+)", record.getMessage())
        if not match:
            return
        index = int(match.group(1))
        total = max(int(match.group(2)), self.n, 1)
        self.progress(f"EnergyPlus sample {index}/{total}", 0.10 + 0.72 * index / total)


def _statistics(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for column in OUTPUT_COLUMNS:
        series = frame[column].astype(float)
        result[column] = {
            "mean": float(series.mean()),
            "median": float(series.median()),
            "p5": float(series.quantile(0.05)),
            "p95": float(series.quantile(0.95)),
            "minimum": float(series.min()),
            "maximum": float(series.max()),
            "stddev": float(series.std(ddof=1)),
        }
    return result


def _sensitivity_records(summary: dict[str, list[tuple[str, float]]]) -> dict[str, list[dict[str, Any]]]:
    return {
        output: [
            {"variable": variable, "rho": float(rho), "rank": index}
            for index, (variable, rho) in enumerate(drivers, start=1)
        ]
        for output, drivers in summary.items()
    }


def _lhs_strata_pass(samples: pd.DataFrame, variables: dict[str, tuple[float, float]]) -> bool:
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


def _qa(lhs, samples: pd.DataFrame, results: pd.DataFrame,
        sensitivity: dict[str, list[dict[str, Any]]], out_dir: Path, n: int) -> dict[str, Any]:
    expected_columns = set(lhs.ALL_VARS) | set(OUTPUT_COLUMNS)
    finite_columns = [column for column in expected_columns if column in results.columns]
    finite_pass = bool(finite_columns) and all(
        math.isfinite(float(value))
        for column in finite_columns
        for value in results[column]
    )
    range_pass = all(
        samples[name].between(float(bounds[0]), float(bounds[1]), inclusive="both").all()
        for name, bounds in lhs.ALL_VARS.items()
    )
    sensitivity_pass = set(("heating_kwh_m2", "cooling_kwh_m2", "co2_kg_m2")) <= set(sensitivity) and all(
        len(drivers) == 3 and all(math.isfinite(float(item["rho"])) for item in drivers)
        for drivers in sensitivity.values()
    )
    artifact_pass = all((out_dir / name).is_file() and (out_dir / name).stat().st_size > 0 for name in (
        "samples.csv", "runs.csv", "histograms.png", "tornado.png", "summary.txt",
    ))
    checks = [
        {"name": "sample_count", "passed": len(results) == n, "expected": n, "actual": len(results)},
        {"name": "result_schema", "passed": expected_columns <= set(results.columns),
         "expected": len(expected_columns), "actual": len(expected_columns & set(results.columns))},
        {"name": "finite_values", "passed": finite_pass, "expected": True, "actual": finite_pass},
        {"name": "variable_ranges", "passed": bool(range_pass), "expected": True, "actual": bool(range_pass)},
        {"name": "lhs_stratification", "passed": _lhs_strata_pass(samples, lhs.ALL_VARS),
         "expected": True, "actual": _lhs_strata_pass(samples, lhs.ALL_VARS)},
        {"name": "nonnegative_outputs", "passed": bool((results[list(OUTPUT_COLUMNS)] >= 0).all().all()),
         "expected": True, "actual": bool((results[list(OUTPUT_COLUMNS)] >= 0).all().all())},
        {"name": "sensitivity_top3", "passed": sensitivity_pass, "expected": True, "actual": sensitivity_pass},
        {"name": "core_artifacts", "passed": artifact_pass, "expected": True, "actual": artifact_pass},
    ]
    all_pass = all(bool(item["passed"]) for item in checks)
    return {"all_pass": all_pass, "scientific_status": "VALIDATED" if all_pass else "UNVERIFIED", "checks": checks}


def run_lhs(run_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Execute the real locked LHS chain and serialize its scientific evidence."""
    lhs = _lhs()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    progress: Callable[[str, float], None] = settings.get("_progress", lambda *_: None)
    n = int(settings.get("n", lhs.DEFAULT_N))
    seed = int(settings.get("seed", lhs.DEFAULT_SEED))
    if n != int(lhs.DEFAULT_N) or seed != int(lhs.DEFAULT_SEED):
        raise ValueError("Workbench LHS v1 accepts only the frozen N=50, seed=42 baseline")

    progress("Sampling", 0.04)
    samples = lhs.sample_matrix(n, seed)
    samples.to_csv(run_dir / "samples.csv", index=False)

    handler = _ProgressHandler(progress, n)
    previous_level = lhs.logger.level
    lhs.logger.addHandler(handler)
    lhs.logger.setLevel(logging.INFO)
    try:
        progress("EnergyPlus sample 0/50", 0.10)
        results = lhs.run_study(samples, run_dir)
    finally:
        lhs.logger.removeHandler(handler)
        lhs.logger.setLevel(previous_level)
    results.to_csv(run_dir / "runs.csv", index=False)

    progress("Distributions", 0.85)
    lhs.make_histograms(results, run_dir, n)
    progress("Sensitivity", 0.90)
    sensitivity_raw = lhs.make_tornado(results, run_dir)
    sensitivity = _sensitivity_records(sensitivity_raw)
    summary_text = lhs.summarize(results, sensitivity_raw, run_dir)
    (run_dir / "summary.txt").write_text(summary_text, encoding="utf-8")

    statistics = _statistics(results)
    catalog = _variable_catalog(lhs)
    qa = _qa(lhs, samples, results, sensitivity, run_dir, n)
    clean_settings = {key: _json_value(value) for key, value in settings.items() if not key.startswith("_")}
    result = {
        "schema_version": 1,
        "settings": clean_settings,
        "summary": {
            "scope": "4252702YJ2745A",
            "samples_expected": n,
            "samples_completed": len(results),
            "simulation_variables": len(lhs.SIM_VARS),
            "post_variables": len(lhs.POST_VARS),
            "statistics": statistics,
        },
        "qa": qa,
        "variables": catalog,
        "variable_fingerprint": _variable_fingerprint(catalog),
        "baselines": {
            "massless": _json_value(lhs.BASELINE_MASSLESS),
            "layered": _json_value(lhs.BASELINE_LAYERED),
            "cadastre": _json_value(lhs.CADASTRE),
        },
        "sensitivity": sensitivity,
        "samples": _records(results),
        "figures": {"distributions": "histograms.png", "sensitivity": "tornado.png"},
        "summary_text": summary_text,
    }
    (run_dir / "statistics.json").write_text(
        json.dumps(statistics, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (run_dir / "sensitivity.json").write_text(
        json.dumps(sensitivity, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (run_dir / "qa.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    progress("Finalize", 0.96)
    return result
