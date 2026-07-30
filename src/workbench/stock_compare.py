"""Scientific comparison contracts for immutable Part C and Part D runs."""

from __future__ import annotations

import math
from typing import Any, Callable, Literal

from workbench.city_service import city_detail
from workbench.neighborhood_service import neighborhood_detail


StockKind = Literal["neighborhood", "city"]

TOTAL_METRICS = (
    ("heating_gwh_yr", "demand", "GWh/yr"),
    ("cooling_gwh_yr", "demand", "GWh/yr"),
    ("hvac_consumption_gwh_yr", "consumption", "GWh/yr"),
    ("total_site_gwh_yr", "consumption", "GWh/yr"),
    ("s1_co2_t_yr", "carbon", "tCO2/yr"),
    ("s2_co2_t_yr", "carbon", "tCO2/yr"),
    ("hvac_co2_t_yr", "carbon", "tCO2/yr"),
    ("total_site_co2_t_yr", "carbon", "tCO2/yr"),
)

CLUSTER_METRICS = (
    ("heating_kwh_m2", "demand", "kWh/m2/yr"),
    ("cooling_kwh_m2", "demand", "kWh/m2/yr"),
    ("cons_hc_kwh_m2", "consumption", "kWh/m2/yr"),
    ("total_site_kwh_m2", "consumption", "kWh/m2/yr"),
    ("s1_co2_kg_m2", "carbon", "kgCO2/m2/yr"),
    ("s2_co2_kg_m2", "carbon", "kgCO2/m2/yr"),
    ("hvac_co2_kg_m2", "carbon", "kgCO2/m2/yr"),
    ("total_site_co2_kg_m2", "carbon", "kgCO2/m2/yr"),
)

DISTRICT_METRICS = (
    ("heating_gwh", "demand", "GWh/yr"),
    ("cooling_gwh", "demand", "GWh/yr"),
    ("cons_hc_gwh", "consumption", "GWh/yr"),
    ("total_site_gwh", "consumption", "GWh/yr"),
    ("s1_co2_t", "carbon", "tCO2/yr"),
    ("s2_co2_t", "carbon", "tCO2/yr"),
    ("hvac_co2_t", "carbon", "tCO2/yr"),
    ("total_site_co2_t", "carbon", "tCO2/yr"),
)

STOCK_INPUT_ROLES = {
    "neighborhood": (
        "city_gis", "boundary_gis", "tipo15", "template",
        "neighborhood_pipeline", "model_builder", "run_simulation",
        "neighborhood_adapter",
    ),
    "city": (
        "city_gis", "tipo15", "template", "city_pipeline",
        "neighborhood_pipeline", "model_builder", "run_simulation", "city_adapter",
    ),
}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _metric(left: Any, right: Any, *, percent_enabled: bool) -> dict[str, float | None]:
    left_number = _finite_number(left)
    right_number = _finite_number(right)
    delta = right_number - left_number if left_number is not None and right_number is not None else None
    percent = (
        delta / left_number * 100.0
        if percent_enabled and delta is not None and left_number not in {None, 0.0}
        else None
    )
    return {"left": left_number, "right": right_number, "delta": delta, "percent": percent}


def _settings(detail: dict[str, Any]) -> dict[str, Any]:
    return (detail.get("result") or {}).get("settings") or detail.get("config") or {}


def _summary(detail: dict[str, Any]) -> dict[str, Any]:
    return (detail.get("result") or {}).get("summary") or detail.get("stats") or {}


def _effective_weather(settings: dict[str, Any]) -> str | None:
    scenario = settings.get("scenario") or {}
    if scenario.get("weather_snapshot_hash"):
        return str(scenario["weather_snapshot_hash"])
    selected = settings.get("selected_weather") or {}
    if selected.get("snapshot_hash"):
        return str(selected["snapshot_hash"])
    snapshots = settings.get("input_snapshot_hashes") or {}
    return snapshots.get("selected_weather") or snapshots.get("weather")


def _scope_key(kind: StockKind, settings: dict[str, Any]) -> tuple[Any, ...]:
    if kind == "city":
        return (settings.get("scope", "Valencia"),)
    return (
        settings.get("scope"), settings.get("scope_mode", "boundary"),
        settings.get("district"), settings.get("building_ref"),
    )


def _selected_inputs(kind: StockKind, settings: dict[str, Any]) -> dict[str, str | None]:
    snapshots = settings.get("input_snapshot_hashes") or {}
    return {role: snapshots.get(role) for role in STOCK_INPUT_ROLES[kind]}


def _coverage(kind: StockKind, summary: dict[str, Any]) -> tuple[Any, ...]:
    base = (
        summary.get("buildings"), summary.get("clusters_expected"),
        summary.get("clusters_completed"), summary.get("clusters_failed"),
    )
    return base + ((summary.get("districts"),) if kind == "city" else ())


def _result_schema_ok(kind: StockKind, detail: dict[str, Any]) -> bool:
    result = detail.get("result")
    if not isinstance(result, dict):
        return False
    summary = result.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("totals"), dict):
        return False
    if not isinstance(result.get("clusters"), list):
        return False
    return kind != "city" or isinstance(result.get("districts"), list)


def _area(summary: dict[str, Any]) -> float | None:
    return _finite_number((summary.get("totals") or {}).get("residential_area_m2"))


def _area_matches(left: float | None, right: float | None) -> bool:
    return left is not None and right is not None and math.isclose(left, right, rel_tol=1e-9, abs_tol=0.01)


def _check(identifier: str, left: Any, right: Any, passed: bool) -> dict[str, Any]:
    return {"id": identifier, "passed": passed, "left": left, "right": right}


def _evidence(kind: StockKind, detail: dict[str, Any]) -> dict[str, Any]:
    result = detail.get("result") or {}
    settings = _settings(detail)
    summary = _summary(detail)
    qa = result.get("qa") or detail.get("qa") or {}
    verification = detail.get("verification") or {}
    return {
        "id": detail["id"],
        "run_type": kind,
        "scenario_name": detail.get("scenario_name"),
        "created_at": detail.get("created_at"),
        "verification_status": detail.get("verification_status"),
        "verification_ok": bool(verification.get("ok")),
        "verification_issues": verification.get("issues", []),
        "scientific_status": qa.get("scientific_status"),
        "scope": settings.get("scope"),
        "scope_mode": settings.get("scope_mode", "city" if kind == "city" else "boundary"),
        "run_mode": settings.get("run_mode"),
        "scenario": settings.get("scenario"),
        "weather_snapshot_hash": _effective_weather(settings),
        "input_snapshot_hashes": settings.get("input_snapshot_hashes", {}),
        "summary": summary,
        "qa_checks": qa.get("checks", []),
        "map_descriptor": result.get("map_descriptor"),
    }


def _comparison_rows(
    left_rows: list[dict[str, Any]], right_rows: list[dict[str, Any]], *, key: str,
    metrics: tuple[tuple[str, str, str], ...], percent_enabled: bool,
) -> list[dict[str, Any]]:
    left_index = {str(item.get(key)): item for item in left_rows if item.get(key) is not None}
    right_index = {str(item.get(key)): item for item in right_rows if item.get(key) is not None}
    output = []
    for identifier in sorted(set(left_index) | set(right_index)):
        left = left_index.get(identifier, {})
        right = right_index.get(identifier, {})
        row_metrics = {
            name: _metric(left.get(name), right.get(name), percent_enabled=percent_enabled)
            for name, _category, _unit in metrics
            if _finite_number(left.get(name)) is not None or _finite_number(right.get(name)) is not None
        }
        output.append({
            "key": identifier,
            "label": identifier,
            "left_present": identifier in left_index,
            "right_present": identifier in right_index,
            "left_buildings": _finite_number(left.get("n_buildings")),
            "right_buildings": _finite_number(right.get("n_buildings")),
            "metrics": row_metrics,
        })
    return output


def compare_stock_runs(kind: StockKind, left_id: str, right_id: str) -> dict[str, Any]:
    if left_id == right_id:
        raise ValueError("A stock run cannot be compared with itself")
    loader: Callable[[str], dict[str, Any]] = neighborhood_detail if kind == "neighborhood" else city_detail
    left = loader(left_id)
    right = loader(right_id)
    left_settings, right_settings = _settings(left), _settings(right)
    left_summary, right_summary = _summary(left), _summary(right)
    left_qa = (left.get("result") or {}).get("qa") or left.get("qa") or {}
    right_qa = (right.get("result") or {}).get("qa") or right.get("qa") or {}
    left_verification, right_verification = left.get("verification") or {}, right.get("verification") or {}
    left_area, right_area = _area(left_summary), _area(right_summary)
    left_inputs, right_inputs = _selected_inputs(kind, left_settings), _selected_inputs(kind, right_settings)
    left_coverage, right_coverage = _coverage(kind, left_summary), _coverage(kind, right_summary)

    checks = [
        _check(
            "artifact_integrity", left.get("verification_status"), right.get("verification_status"),
            left.get("verification_status") == right.get("verification_status") == "VERIFIED"
            and bool(left_verification.get("ok")) and bool(right_verification.get("ok")),
        ),
        _check(
            "result_schema", _result_schema_ok(kind, left), _result_schema_ok(kind, right),
            _result_schema_ok(kind, left) and _result_schema_ok(kind, right),
        ),
        _check(
            "scientific_status", left_qa.get("scientific_status"), right_qa.get("scientific_status"),
            left_qa.get("scientific_status") == right_qa.get("scientific_status") == "VALIDATED",
        ),
        _check(
            "scope", _scope_key(kind, left_settings), _scope_key(kind, right_settings),
            _scope_key(kind, left_settings) == _scope_key(kind, right_settings),
        ),
        _check(
            "weather", _effective_weather(left_settings), _effective_weather(right_settings),
            bool(_effective_weather(left_settings))
            and _effective_weather(left_settings) == _effective_weather(right_settings),
        ),
        _check(
            "method", left_settings.get("method"), right_settings.get("method"),
            bool(left_settings.get("method")) and left_settings.get("method") == right_settings.get("method"),
        ),
        _check(
            "stock_snapshots", left_inputs, right_inputs,
            left_inputs == right_inputs
            and all(bool(value) for value in left_inputs.values())
            and all(bool(value) for value in right_inputs.values()),
        ),
        _check(
            "coverage", left_coverage, right_coverage,
            left_coverage == right_coverage
            and all(value is not None for value in left_coverage)
            and all(value is not None for value in right_coverage),
        ),
        _check("area_basis", left_area, right_area, _area_matches(left_area, right_area)),
    ]
    interpretation_enabled = all(check["passed"] for check in checks[:3])
    percent_enabled = interpretation_enabled and all(check["passed"] for check in checks[3:])
    left_result = (left.get("result") or {}) if interpretation_enabled else {}
    right_result = (right.get("result") or {}) if interpretation_enabled else {}
    left_totals = (left_summary.get("totals") or {}) if interpretation_enabled else {}
    right_totals = (right_summary.get("totals") or {}) if interpretation_enabled else {}

    metrics = [
        {
            "key": name,
            "category": category,
            "unit": unit,
            **_metric(left_totals.get(name), right_totals.get(name), percent_enabled=percent_enabled),
        }
        for name, category, unit in TOTAL_METRICS
        if _finite_number(left_totals.get(name)) is not None or _finite_number(right_totals.get(name)) is not None
    ]
    clusters = _comparison_rows(
        left_result.get("clusters", []), right_result.get("clusters", []),
        key="cluster", metrics=CLUSTER_METRICS, percent_enabled=percent_enabled,
    ) if interpretation_enabled else []
    districts = _comparison_rows(
        left_result.get("districts", []), right_result.get("districts", []),
        key="nombre", metrics=DISTRICT_METRICS, percent_enabled=percent_enabled,
    ) if kind == "city" and interpretation_enabled else []
    return {
        "kind": kind,
        "left": _evidence(kind, left),
        "right": _evidence(kind, right),
        "compatibility": {
            "interpretation_enabled": interpretation_enabled,
            "percent_enabled": percent_enabled,
            "reasons": [check["id"] for check in checks if not check["passed"]],
            "checks": checks,
        },
        "metrics": metrics,
        "clusters": clusters,
        "districts": districts,
    }
