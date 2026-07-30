"""Live capability diagnostics and guarded golden-evidence promotion.

The user-owned domain modules are inspected read-only. Workbench adapters and
fixtures may evolve, but a changed scientific source is never trusted merely
because it parses: a complete immutable baseline run must reproduce the golden
evidence before the new hashes are promoted.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workbench import db


PROJECT = Path(__file__).resolve().parents[2]
SUPPORTED_REVALIDATION = {"neighborhood", "city"}
RUN_ROLE = {
    "model_builder": "model_builder",
    "neighborhood": "neighborhood_pipeline",
    "city": "city_pipeline",
}
RUN_TYPE = {"model_builder": "model", "neighborhood": "neighborhood", "city": "city"}
ESTIMATES = {
    "neighborhood": {"duration_seconds": 360, "disk_bytes": 2 * 1024 ** 3},
    "city": {"duration_seconds": 600, "disk_bytes": 3 * 1024 ** 3},
}


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ast_inventory(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"ok": False, "functions": {}, "cli_options": [], "constants": []}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return {
            "ok": False, "functions": {}, "cli_options": [], "constants": [],
            "error": str(exc),
        }
    functions: dict[str, list[str]] = {}
    cli_options: set[str] = set()
    constants: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = [argument.arg for argument in node.args.args]
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    constants.add(target.id)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument":
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if argument.value.startswith("-"):
                    cli_options.add(argument.value)
    return {
        "ok": True,
        "functions": functions,
        "cli_options": sorted(cli_options),
        "constants": sorted(constants),
    }


def _domain_path(name: str, declaration: dict[str, Any], contract: dict[str, Any] | None) -> Path:
    contract = contract or {}
    candidate = contract.get("runner") or contract.get("builder") or declaration.get("module")
    return (PROJECT / str(candidate or "")).resolve()


def discover_features(name: str, declaration: dict[str, Any],
                      contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return conservative UI feature flags derived from the public source API."""
    path = _domain_path(name, declaration, contract)
    inventory = _ast_inventory(path)
    functions = inventory.get("functions", {})
    options = set(inventory.get("cli_options", []))
    source = path.read_text(encoding="utf-8") if path.is_file() else ""

    def feature(key: str, available: bool, evidence: str) -> dict[str, Any]:
        return {"key": key, "available": bool(available), "evidence": evidence}

    if name == "simulation":
        return [
            feature("annual_baseline", "run_energyplus" in functions, "run_energyplus"),
            feature(
                "real_hvac_consumption",
                {"read_end_uses", "carbon_from_enduses", "check_plausibility_cons"} <= set(functions),
                "read_end_uses + carbon_from_enduses",
            ),
        ]
    if name == "scenario":
        return [
            feature("comfort_offsets", "apply_comfort_offsets" in source, "builder.apply_comfort_offsets"),
            feature("weather_scenario", "set_weather_file" in source, "builder.set_weather_file"),
        ]
    if name == "neighborhood":
        load_stock_args = set(functions.get("load_stock", []))
        representative_args = set(functions.get("run_representative", []))
        return [
            feature("district_scope", "district" in load_stock_args and "--district" in options, "load_stock(district)"),
            feature("single_building", "run_single_building" in functions and "--building" in options, "run_single_building"),
            feature("comfort_scenario", "scenario" in representative_args and {"--heat-delta", "--cool-delta"} <= options, "run_representative(..., scenario)"),
            feature("weather_scenario", "scenario" in representative_args and "--epw" in options, "--epw"),
            feature("real_hvac_consumption", "cons_hc_kwh_m2" in source and "total_site_kwh_m2" in source, "Part C result fields"),
        ]
    if name == "city":
        return [
            feature("district_ledger", "district_breakdown" in functions, "district_breakdown"),
            feature("single_building", "--building" in options, "--building"),
            feature("comfort_scenario", {"--heat-delta", "--cool-delta"} <= options, "--heat-delta/--cool-delta"),
            feature("weather_scenario", "--epw" in options, "--epw"),
            feature("real_hvac_consumption", "cons_hc_gwh" in source and "total_site_gwh" in source, "Part D result fields"),
        ]
    if name == "lhs":
        return [
            feature("configurable_sample_size", "--n" in options, "--n"),
            feature("reproducible_seed", "--seed" in options, "--seed"),
            feature("sensitivity_outputs", "make_tornado" in functions, "make_tornado"),
        ]
    if name == "model_editor":
        service_path = PROJECT / "src/workbench/model_editor_service.py"
        service_source = service_path.read_text(encoding="utf-8") if service_path.is_file() else ""
        return [
            feature("typed_patch_editing", "apply_typed_patches" in functions, "apply_typed_patches"),
            feature("warn_not_block", "_warning" in functions and "outside_lhs_band" in source, "plausibility warnings"),
            feature("energyplus_preflight", "preflight_model" in functions, "preflight_model"),
            feature("recoverable_sessions", "create_session" in service_source and "SESSION_TTL_HOURS" in service_source, "model_editor_service.create_session"),
            feature("immutable_authored_commit", "commit_session" in service_source and "authored_from" in service_source, "model_editor_service.commit_session"),
            feature("simulation_handoff", "run_settings" in service_source and "run_type\": \"model" in service_source, "Part B parent contract"),
            feature("hvac_loop_authoring", "_air_loop_create" in functions and "_plant_loop_create" in functions, "typed AirLoopHVAC/PlantLoop patches"),
            feature("measure_runner", "apply_model_measure" in service_source and "measure_provenance" in service_source, "isolated OpenStudio Measure runner"),
        ]
    return [feature("model_build", declaration.get("entrypoint") in functions, str(declaration.get("entrypoint")))]


def _latest_verified_run(name: str) -> dict[str, Any] | None:
    run_type = RUN_TYPE.get(name)
    if run_type is None:
        return None
    try:
        runs = db.list_runs_by_type(run_type)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None
    for run in runs:
        if run.get("verification_status") != "VERIFIED":
            continue
        if name == "model_builder" and run.get("parent_run_id"):
            continue
        manifest = Path(run["artifact_dir"]) / "input_manifest.json"
        if not manifest.is_file():
            continue
        try:
            inputs = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        role = RUN_ROLE[name]
        components = inputs.get(role, {}).get("components", [])
        source_hash = next((item.get("sha256") for item in components if item.get("name", "").endswith(".py")), None)
        if source_hash:
            return {
                "run_id": run["id"],
                "verified_at": run.get("committed_at") or run.get("created_at"),
                "source_sha256": source_hash,
                "input_snapshot_hash": inputs[role].get("snapshot_hash"),
            }
    return None


def _fixture_verified_evidence(contract: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the accepted golden promotion recorded by the capability fixture.

    Revalidation may run in an isolated Workbench database by design. In that
    case the production run ledger is older than the atomically promoted
    fixture, so the fixture is the authoritative evidence for the current
    source contract.
    """
    contract = contract or {}
    fixture_path = Path(str(contract.get("fixture", "")))
    if not fixture_path.is_file():
        return None
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    acceptance = fixture.get("acceptance") or {}
    verified_at = fixture.get("verified_at") or acceptance.get("validated_at")
    run_id = fixture.get("verification_run_id") or acceptance.get("run_id")
    source_hash = fixture.get("runner_sha256") or fixture.get("builder_sha256")
    if not verified_at or not source_hash:
        return None
    return {
        "run_id": run_id,
        "verified_at": verified_at,
        "source_sha256": source_hash,
        "input_snapshot_hash": None,
        "evidence_source": "accepted_fixture",
    }


def _expected_hash(name: str, declaration: dict[str, Any], contract: dict[str, Any] | None) -> str | None:
    contract = contract or {}
    if name in {"neighborhood", "city", "lhs", "simulation"}:
        return contract.get("expected_runner_sha256") or contract.get("runner_sha256")
    if name == "scenario":
        return contract.get("expected_builder_sha256") or contract.get("builder_sha256")
    if name == "model_editor":
        return contract.get("expected_adapter_sha256") or contract.get("adapter_sha256")
    return None


def _diagnostic_source(name: str, declaration: dict[str, Any],
                       contract: dict[str, Any] | None) -> tuple[Path, str | None]:
    """Point diagnostics at the dependency whose content check actually failed."""
    contract = contract or {}
    checks = contract.get("checks", {})
    candidates = (
        ("runner_hash", "runner", "expected_runner_sha256"),
        ("builder_hash", "builder", "expected_builder_sha256"),
        ("simulation_runner_hash", "simulation_runner", "expected_simulation_runner_sha256"),
        ("stock_policy_hash", "stock_policy", "expected_stock_policy_sha256"),
    )
    for check, path_key, expected_key in candidates:
        if checks.get(check) is False and contract.get(path_key):
            return Path(contract[path_key]).resolve(), contract.get(expected_key)
    return _domain_path(name, declaration, contract), _expected_hash(name, declaration, contract)


def diagnostics(name: str, declaration: dict[str, Any], inspection: dict[str, Any],
                contract: dict[str, Any] | None, runtime_ready: bool) -> dict[str, Any]:
    path, expected_hash = _diagnostic_source(name, declaration, contract)
    current_hash = _sha256(path)
    ledger_evidence = _latest_verified_run(name)
    fixture_evidence = _fixture_verified_evidence(contract)
    latest = (
        fixture_evidence
        if runtime_ready and fixture_evidence
        and fixture_evidence.get("source_sha256") == current_hash
        else ledger_evidence
    )
    checks = (contract or {}).get("checks", {})
    hash_checks = {key for key in checks if key.endswith("_hash")}
    non_hash_failures = [key for key, value in checks.items() if not value and key not in hash_checks]
    hash_failures = [key for key, value in checks.items() if not value and key in hash_checks]
    dependency_failures = [key for key in non_hash_failures if key.endswith("_dependency")]
    structural_failures = [key for key in non_hash_failures if key not in dependency_failures]
    evidence_changed = bool(
        ledger_evidence and ledger_evidence.get("source_sha256")
        and ledger_evidence["source_sha256"] != current_hash
    )
    if runtime_ready:
        state = "verified"
        reason = None
    elif evidence_changed:
        state = "source_changed"
        reason = "source_changed_since_latest_verified_run"
    elif not inspection.get("ok") or structural_failures:
        state = "contract_failed"
        reason = (contract or {}).get("reason") or inspection.get("reason") or "contract_failed"
    elif hash_failures or (expected_hash and current_hash != expected_hash):
        state = "source_changed"
        reason = "source_changed"
    elif dependency_failures:
        state = "dependency_blocked"
        reason = "dependency_blocked"
    elif not declaration.get("ready"):
        state = "disabled"
        reason = "declared_not_ready"
    else:
        state = "blocked"
        reason = (contract or {}).get("reason") or "dependency_blocked"
    modified_at = None
    if path.exists():
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return {
        "state": state,
        "reason": reason,
        "source": {
            "path": str(path),
            "current_sha256": current_hash,
            "expected_sha256": expected_hash or (latest or {}).get("source_sha256"),
            "modified_at": modified_at,
        },
        "latest_verified_evidence": latest,
        "failed_checks": non_hash_failures + hash_failures,
        "features": discover_features(name, declaration, contract),
        "revalidation_supported": name in SUPPORTED_REVALIDATION,
    }


def revalidation_plan(name: str, capability: dict[str, Any]) -> dict[str, Any]:
    if name not in SUPPORTED_REVALIDATION:
        return {
            "capability": name, "supported": False, "eligible": False,
            "reason": "full_revalidation_not_supported",
        }
    diagnostic = capability.get("diagnostic", {})
    contract = capability.get("contract") or {}
    checks = contract.get("checks", {})
    ignored = {"adapter_hash", "runner_hash", "stock_policy_hash"}
    blocking_checks = [key for key, passed in checks.items() if not passed and key not in ignored]
    eligible = (
        capability.get("declared_ready") is True
        and capability.get("inspection", {}).get("ok") is True
        and diagnostic.get("state") == "source_changed"
        and not blocking_checks
    )
    fixture = Path(str(contract.get("fixture", "")))
    expected = contract.get("expected") or {}
    estimate = ESTIMATES[name]
    plan_payload = {
        "capability": name,
        "current_sha256": diagnostic.get("source", {}).get("current_sha256"),
        "expected_sha256": diagnostic.get("source", {}).get("expected_sha256"),
        "fixture": str(fixture),
        "expected": expected,
    }
    return {
        "capability": name,
        "supported": True,
        "eligible": eligible,
        "reason": None if eligible else diagnostic.get("reason") or "contract_failed",
        "blocking_checks": blocking_checks,
        "current_sha256": diagnostic.get("source", {}).get("current_sha256"),
        "expected_sha256": diagnostic.get("source", {}).get("expected_sha256"),
        "latest_verified_evidence": diagnostic.get("latest_verified_evidence"),
        "expected": expected,
        "duration_seconds": estimate["duration_seconds"],
        "disk_bytes": estimate["disk_bytes"],
        "steps": ["source_snapshot", "full_baseline", "scientific_qa", "golden_comparison", "hash_promotion"],
        "plan_token": hashlib.sha256(json.dumps(plan_payload, sort_keys=True).encode()).hexdigest(),
    }


def compare_golden(name: str, result: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary", {})
    totals = summary.get("totals") or {}
    qa = result.get("qa", {})
    actual: dict[str, Any] = {
        "buildings": summary.get("buildings"),
        "clusters": summary.get("clusters_completed"),
        "qa_passed_clusters": summary.get("qa_passed_clusters"),
        "qa_all_pass": qa.get("all_pass"),
    }
    if name == "city":
        actual["districts"] = summary.get("districts")
    for key in (
        "heating_gwh_yr", "cooling_gwh_yr", "s1_co2_t_yr", "s2_co2_t_yr",
        "hvac_consumption_gwh_yr", "total_site_gwh_yr", "hvac_co2_t_yr", "total_site_co2_t_yr",
    ):
        if key in totals:
            actual[key] = totals[key]
    aliases = {
        "clusters": "clusters", "qa_passed_clusters": "qa_passed_clusters",
        "buildings": "buildings", "districts": "districts",
    }
    energy_tolerance = float(expected.get("absolute_tolerance_gwh", 0.05))
    carbon_tolerance = float(expected.get("absolute_tolerance_co2_t", 10.0))
    comparisons = []
    for expected_key, expected_value in expected.items():
        key = aliases.get(expected_key, expected_key)
        if key not in actual or expected_key.startswith("absolute_tolerance"):
            continue
        actual_value = actual[key]
        if isinstance(expected_value, (int, float)) and not isinstance(expected_value, bool):
            tolerance = carbon_tolerance if "co2_t" in key else energy_tolerance if "gwh" in key else 0.0
            passed = actual_value is not None and abs(float(actual_value) - float(expected_value)) <= tolerance
        else:
            tolerance = None
            passed = actual_value == expected_value
        comparisons.append({
            "metric": expected_key, "expected": expected_value, "actual": actual_value,
            "tolerance": tolerance, "passed": passed,
        })
    comparisons.append({
        "metric": "qa_all_pass", "expected": True, "actual": qa.get("all_pass"),
        "tolerance": None, "passed": qa.get("all_pass") is True,
    })
    passed = bool(comparisons) and all(item["passed"] for item in comparisons)
    return {
        "schema_version": 1,
        "capability": name,
        "status": "PASSED" if passed else "REVIEW_REQUIRED",
        "passed": passed,
        "comparisons": comparisons,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def promote_fixture(name: str, capability: dict[str, Any], report: dict[str, Any],
                    *, run_id: str) -> None:
    if not report.get("passed"):
        return
    contract = capability.get("contract") or {}
    fixture_path = Path(str(contract.get("fixture", "")))
    if not fixture_path.is_file():
        raise FileNotFoundError(fixture_path)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["runner_sha256"] = contract["runner_sha256"]
    if contract.get("stock_policy_sha256"):
        fixture["stock_policy_sha256"] = contract["stock_policy_sha256"]
    fixture["adapter_sha256"] = capability["inspection"]["sha256"]
    fixture["verified_at"] = datetime.now(timezone.utc).isoformat()
    fixture["verification_run_id"] = run_id
    temporary = fixture_path.with_name(f".{fixture_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(fixture_path)
