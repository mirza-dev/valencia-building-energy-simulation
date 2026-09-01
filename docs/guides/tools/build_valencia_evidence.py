#!/usr/bin/env python3
"""Build a path-scrubbed, read-only publication evidence manifest for one run.

The tool never recomputes physical results.  It reconciles the settled ledger,
aggregate and configuration, selects reproducible building examples, and writes
only the requested documentation-side JSON output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any


IDENTITY_FIELDS = (
    "run_identity", "profile_fingerprint", "climate_fingerprint",
    "template_fingerprint", "policy_fingerprint", "zero_policy",
    "stock_source_fingerprint", "runner_schema",
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def latest_rows(path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    latest: dict[str, dict[str, Any]] = {}
    attempts = 0
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            attempts += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"ledger line {line_number} is invalid JSON: {exc}") from exc
            reference = str(row.get("refparcela") or "").strip()
            if not reference:
                raise ValueError(f"ledger line {line_number} has no refparcela")
            latest[reference] = row
    return latest, attempts


def one_identity(rows: dict[str, dict[str, Any]], field: str) -> Any:
    values = {json.dumps(row.get(field), sort_keys=True) for row in rows.values()}
    if len(values) != 1:
        raise ValueError(f"latest ledger rows contain {len(values)} values for {field}")
    return json.loads(next(iter(values)))


def count_status(rows: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(row.get("status") or "missing") for row in rows.values())
    allowed = {"ok", "failed", "failed_qa", "excluded"}
    unknown = sorted(set(counts) - allowed)
    if unknown:
        raise ValueError(f"unknown terminal status values: {unknown}")
    return {key: counts.get(key, 0) for key in ("ok", "failed", "failed_qa", "excluded")}


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def percentile(values: list[float], quantile: float) -> float | None:
    """Return a deterministic nearest-rank-style percentile for presentation."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(math.floor((len(ordered) - 1) * quantile + 0.5))
    return ordered[index]


def distribution(rows: dict[str, dict[str, Any]], field: str) -> dict[str, Any]:
    values = [number for row in rows.values()
              if row.get("status") == "ok"
              if (number := finite_number(row.get(field))) is not None]
    return {
        "field": field,
        "buildings_measured": len(values),
        "median": median(values) if values else None,
        "p95": percentile(values, 0.95),
        "maximum": max(values) if values else None,
    }


def source_file(path_value: Any, *, collection: bool = False) -> dict[str, Any] | None:
    """Describe a preserved input without exposing its workstation path."""
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = Path(path_value)
    if not path.is_file():
        raise ValueError(f"preserved input is missing: {path.name}")
    parts = [path]
    if collection:
        allowed = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".xml"}
        parts = sorted(
            candidate for candidate in path.parent.glob(f"{path.stem}.*")
            if candidate.is_file() and candidate.suffix.lower() in allowed
        )
        required = {".shp", ".shx", ".dbf"}
        if not required.issubset({candidate.suffix.lower() for candidate in parts}):
            raise ValueError(f"Shapefile collection is incomplete: {path.name}")
    return {
        "name": path.name,
        "parts": [
            {"name": part.name, "sha256": sha256(part), "bytes": part.stat().st_size}
            for part in parts
        ],
    }


def publication_summaries(
    rows: dict[str, dict[str, Any]], aggregate: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    successful = [row for row in rows.values() if row.get("status") == "ok"]
    classified = [
        row for row in successful
        if (finite_number(row.get("severes_benign_shading_ems")) or 0) > 0
    ]
    diagnostics = {
        "classified_diagnostic_buildings": len(classified),
        "classified_benign_severe_messages": int(sum(
            finite_number(row.get("severes_benign_shading_ems")) or 0 for row in successful
        )),
        "unexplained_severe_messages": int(sum(
            finite_number(row.get("severes_unexplained")) or 0 for row in rows.values()
        )),
        "fatal_messages": int(sum(
            finite_number(row.get("fatals")) or 0 for row in rows.values()
        )),
        "occupancy_flagged_buildings": sum(
            row.get("occupancy_plausibility") not in (None, "plausible") for row in successful
        ),
    }
    zoning = aggregate.get("zoning") or {}
    geometry = {
        "party_surfaces": distribution(rows, "n_party_surfaces"),
        "shading_surfaces": distribution(rows, "n_shading_surfaces"),
        "windows": distribution(rows, "n_windows"),
        "large_single_zone_buildings": zoning.get("buildings"),
        "large_single_zone_energy_pct": zoning.get("total_site_pct"),
    }
    totals = aggregate.get("totals") or {}
    carbon_t = finite_number(totals.get("carbon_total_site_t_yr"))
    total_site_gwh = finite_number(totals.get("total_site_gwh"))
    geometric_area = finite_number(totals.get("residential_area_m2"))
    conditioned_area = finite_number(totals.get("conditioned_area_m2"))
    hvac_carbon_t = sum(
        finite_number(row.get("hvac_co2_t_yr")) or 0 for row in successful
    )
    presentation = {
        "whole_site_kwh_m2_conditioned": (
            total_site_gwh * 1_000_000 / conditioned_area
            if total_site_gwh is not None and conditioned_area else None
        ),
        "operational_carbon_kg_m2_geometric_residential": (
            carbon_t * 1000 / geometric_area if carbon_t is not None and geometric_area else None
        ),
        "operational_carbon_kg_m2_conditioned": (
            carbon_t * 1000 / conditioned_area if carbon_t is not None and conditioned_area else None
        ),
        "hvac_operational_carbon_t_yr": hvac_carbon_t,
        "derivation": "settled building fields were summed or aggregate units converted and divided by the named aggregate area; no building physics or simulation result was recomputed",
    }
    return diagnostics, geometry, presentation


def select_buildings(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    valid = [row for row in rows.values()
             if row.get("status") == "ok" and finite_number(row.get("total_site_kwh_m2")) is not None]
    valid.sort(key=lambda row: (float(row["total_site_kwh_m2"]), str(row["refparcela"])))
    if not valid:
        raise ValueError("settled ledger contains no successful finite intensity")

    def rank(role: str, q: float) -> tuple[str, dict[str, Any]]:
        index = int(math.floor((len(valid) - 1) * q + 0.5))
        return role, valid[index]

    candidates: list[tuple[str, dict[str, Any]]] = [
        rank("median-nearest", 0.50),
        rank("p5-nearest", 0.05),
        rank("p95-nearest", 0.95),
        ("highest-total-contribution", max(
            valid,
            key=lambda row: (finite_number(row.get("total_site_kwh")) or -1.0,
                             str(row["refparcela"])),
        )),
    ]
    flagged = [row for row in valid if row.get("occupancy_plausibility") not in (None, "plausible")]
    if not flagged:
        flagged = [row for row in valid if (finite_number(row.get("severes_benign_shading_ems")) or 0) > 0]
    if flagged:
        candidates.append(("flagged-accepted", sorted(flagged, key=lambda row: str(row["refparcela"]))[0]))

    non_success = [row for row in rows.values() if row.get("status") != "ok"]
    if non_success:
        reasons = Counter(str(row.get("reason") or row.get("status") or "unspecified") for row in non_success)
        reason = sorted(reasons, key=lambda item: (-reasons[item], item))[0]
        representative = sorted(
            (row for row in non_success
             if str(row.get("reason") or row.get("status") or "unspecified") == reason),
            key=lambda row: str(row["refparcela"]),
        )[0]
        candidates.append(("failed-or-excluded", representative))

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for role, row in candidates:
        reference = str(row["refparcela"])
        if reference in seen:
            continue
        seen.add(reference)
        selected.append({
            "role": role,
            "refparcela": reference,
            "status": row.get("status"),
            "cluster": row.get("cluster"),
            "total_site_kwh_m2": row.get("total_site_kwh_m2"),
            "total_site_kwh": row.get("total_site_kwh"),
            "total_site_co2_t_yr": row.get("total_site_co2_t_yr"),
            "residential_area_m2": row.get("res_area_m2"),
            "cadastral_residential_area_m2": row.get("tipo15_res_area_m2"),
            "conditioned_area_m2": row.get("total_conditioned_area_m2"),
            "occupancy_plausibility": row.get("occupancy_plausibility"),
            "occupants_applied": row.get("occupants_applied"),
            "footprint_fidelity": row.get("footprint_fidelity"),
            "storey_rule_snapped": row.get("storey_rule_snapped"),
            "qa_all_passed": row.get("qa_all_passed"),
            "severes_benign_shading_ems": row.get("severes_benign_shading_ems"),
            "severes_unexplained": row.get("severes_unexplained"),
            "fatals": row.get("fatals"),
            "reason": row.get("reason"),
        })
    return selected


def execution_segment(path: Path) -> dict[str, Any]:
    record = read_json(path)
    argv = [str(item) for item in record.get("argv") or []]

    def option(name: str) -> str | None:
        try:
            return argv[argv.index(name) + 1]
        except (ValueError, IndexError):
            return None

    return {
        "record": f"{path.parent.name}/{path.name}",
        "run": record.get("run"),
        "started_at_epoch": record.get("started_at"),
        "workers": int(option("--workers")) if option("--workers") else None,
        "retention": option("--keep"),
        "resume": "--resume" in argv,
        "retry_failed": "--retry-failed" in argv,
    }


def compare_aggregate(aggregate: dict[str, Any], counts: dict[str, int], scope: int) -> None:
    expected = {
        "buildings_ok": counts["ok"],
        "buildings_failed": counts["failed"],
        "buildings_failed_qa": counts["failed_qa"],
        "buildings_excluded": counts["excluded"],
    }
    for key, value in expected.items():
        if int(aggregate.get(key, -1)) != value:
            raise ValueError(f"aggregate {key}={aggregate.get(key)!r}, latest ledger={value}")
    coverage = aggregate.get("coverage") or {}
    if int(coverage.get("buildings_in_scope", -1)) != scope:
        raise ValueError("aggregate coverage scope does not match run_config scope_total")
    if int(coverage.get("buildings_with_result", -1)) != counts["ok"]:
        raise ValueError("aggregate result coverage does not match successful latest rows")
    if int(aggregate.get("qa_failed", -1)) != counts["failed_qa"]:
        raise ValueError("aggregate QA-failed count does not match latest ledger")


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--segment-record", type=Path, action="append", default=[])
    parser.add_argument("--settle-seconds", type=int, default=30)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    config_path = run_dir / "run_config.json"
    ledger_path = run_dir / "ledger.jsonl"
    aggregate_path = run_dir / "aggregate.json"
    for path in (config_path, ledger_path, aggregate_path):
        if not path.is_file():
            raise SystemExit(f"SETTLEMENT_REQUIRED: {path.name} is missing")
    process_path = run_dir / "run_process.json"
    if process_path.is_file():
        pid = int(read_json(process_path).get("pid") or 0)
        if pid > 0 and pid_exists(pid):
            raise SystemExit(f"SETTLEMENT_REQUIRED: recorded PID {pid} still exists")
    newest = max(path.stat().st_mtime for path in (ledger_path, aggregate_path))
    age = time.time() - newest
    if age < args.settle_seconds:
        raise SystemExit(
            f"SETTLEMENT_REQUIRED: evidence changed {age:.1f}s ago; require {args.settle_seconds}s")

    config = read_json(config_path)
    aggregate = read_json(aggregate_path)
    rows, attempts = latest_rows(ledger_path)
    scope = int(config.get("scope_total") or 0)
    if scope <= 0 or len(rows) != scope:
        raise SystemExit(f"EVIDENCE_CONFLICT: scope_total={scope}, latest references={len(rows)}")
    identities = {field: one_identity(rows, field) for field in IDENTITY_FIELDS}
    worker = config.get("worker_config") or {}
    for field in IDENTITY_FIELDS:
        expected = worker.get(field) if field != "zero_policy" else config.get("zero_policy")
        if expected is not None and identities[field] != expected:
            raise SystemExit(f"EVIDENCE_CONFLICT: run_config and ledger disagree on {field}")
    counts = count_status(rows)
    compare_aggregate(aggregate, counts, scope)

    segment_paths = list(args.segment_record)
    if process_path.is_file() and process_path not in segment_paths:
        segment_paths.append(process_path)
    segments = [execution_segment(path) for path in segment_paths]
    run_name = run_dir.name
    if any(segment.get("run") not in (None, run_name) for segment in segments):
        raise SystemExit("EVIDENCE_CONFLICT: an execution segment names a different run")

    climate = config.get("climate") or {}
    template = config.get("template") or {}
    stock_counters = config.get("stock_counters") or {}
    diagnostics, geometry_distributions, presentation = publication_summaries(rows, aggregate)
    source_inputs = {
        "building_gis": source_file(stock_counters.get("gis_path") or worker.get("context_gis"), collection=True),
        "tipo15": source_file(stock_counters.get("tipo15_path")),
        "prepared_stock": source_file(worker.get("prepared_gis")),
    }
    evidence = {
        "schema": "bsew-valencia-publication-evidence-v1",
        "run": run_name,
        "settled": True,
        "evidence_files": {
            "run_config.json": sha256(config_path),
            "ledger.jsonl": sha256(ledger_path),
            "aggregate.json": sha256(aggregate_path),
        },
        "scope": {
            "scope_total": scope,
            "runnable_after_preflight": counts["ok"] + counts["failed"] + counts["failed_qa"],
            "attempt_records": attempts,
            "latest_records": len(rows),
            **counts,
        },
        "identities": identities,
        "execution_segments": segments,
        "source_inputs": source_inputs,
        "stock_counters": {
            key: stock_counters.get(key) for key in (
                "source_buildings", "scoped_buildings", "clusters",
                "invalid_floor_buildings", "imputed_floor_buildings",
                "residential_area_proxy_buildings", "duplicate_parcel_rows",
                "ground_residential_buildings", "ground_terciario_buildings",
                "ground_use_from_tipo15", "residential_area_m2",
            )
        },
        "climate": {
            "name": climate.get("name"), "epw": climate.get("epw"), "epw_sha256": climate.get("epw_sha256"),
            "ddy": climate.get("ddy"), "ddy_sha256": climate.get("ddy_sha256"),
            "heating_design_day": climate.get("heating_design_day"),
            "cooling_design_day": climate.get("cooling_design_day"),
            "ground_temperature_c": climate.get("ground_temperature_c"),
            "water_mains_temperature_c": climate.get("water_mains_temperature_c"),
        },
        "template": {
            "name": template.get("name"), "source": template.get("source"),
            "sha256": template.get("sha256"), "fingerprint": template.get("fingerprint"),
        },
        "energy_period": aggregate.get("energy_period"),
        "coverage": aggregate.get("coverage"),
        "totals": aggregate.get("totals"),
        "qa": {
            "qa_failed": aggregate.get("qa_failed"),
            "unexplained_severes": aggregate.get("unexplained_severes"),
            "implausible_occupancy": aggregate.get("implausible_occupancy"),
        },
        "diagnostics": diagnostics,
        "geometry_quality": aggregate.get("geometry_quality"),
        "geometry_distributions": geometry_distributions,
        "floor_area_allocation": aggregate.get("floor_area_allocation"),
        "presentation_derivations": presentation,
        "zoning": aggregate.get("zoning"),
        "results_layer": aggregate.get("results_layer"),
        "by_cluster": aggregate.get("by_cluster"),
        "by_district": aggregate.get("by_district"),
        "selected_buildings": select_buildings(rows),
        "statement": "Physical results were copied from settled preserved evidence; none were recomputed.",
    }
    write_atomic(args.output, evidence)
    print(f"VALENCIA_EVIDENCE_OK {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
