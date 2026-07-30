"""Read-only readiness gate for user-owned scientific modules."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

import openstudio

from workbench import integrity
from workbench.capability_sync import diagnostics


PROJECT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT / "workbench-capabilities.json"
FORBIDDEN_PATH_PARTS = {"reference"}
FORBIDDEN_FILENAMES = {"model_pipeline.py"}


def _inspect_callable(path: Path, entrypoint: str | None) -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "reason": "module_missing"}
    if path.name in FORBIDDEN_FILENAMES or FORBIDDEN_PATH_PARTS.intersection(path.parts):
        return {"ok": False, "reason": "non_production_module"}
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return {"ok": False, "reason": "syntax_error", "detail": str(exc)}
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if entrypoint and entrypoint not in functions:
        return {"ok": False, "reason": "entrypoint_missing"}
    signature = None
    if entrypoint:
        node = functions[entrypoint]
        signature = [argument.arg for argument in node.args.args]
    return {
        "ok": True, "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "signature_parameters": signature,
    }


def _simulation_contract(declaration: dict[str, Any], inspection: dict[str, Any]) -> dict[str, Any]:
    fixture_path = PROJECT / declaration.get("fixture", "")
    checks: dict[str, Any] = {
        "fixture_schema": False,
        "adapter_signature": inspection.get("signature_parameters") == ["osm_path", "run_dir", "settings"],
        "adapter_hash": False,
        "runner_hash": False,
        "runner_functions": False,
        "energyplus_runtime": False,
        "weather_snapshot": False,
        "golden_smoke": False,
    }
    detail: dict[str, Any] = {"checks": checks}
    if not fixture_path.exists():
        return detail | {"ok": False, "reason": "fixture_missing"}
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return detail | {"ok": False, "reason": "fixture_invalid", "detail": str(exc)}
    required = {
        "schema_version", "adapter_sha256", "runner", "runner_sha256",
        "weather_path", "weather_snapshot_hash", "energyplus", "expected", "smoke_status",
    }
    checks["fixture_schema"] = fixture.get("schema_version") == 1 and required <= set(fixture)
    checks["adapter_hash"] = inspection.get("sha256") == fixture.get("adapter_sha256")

    runner_path = (PROJECT / str(fixture.get("runner", ""))).resolve()
    runner_inspections = {
        name: _inspect_callable(runner_path, name)
        for name in (
            "run_energyplus", "read_results", "scan_err_file", "crosscheck_energyplus",
            "check_plausibility", "write_qa_report", "carbon_footprint",
        )
    }
    runner_hash = hashlib.sha256(runner_path.read_bytes()).hexdigest() if runner_path.exists() else None
    checks["runner_hash"] = runner_hash == fixture.get("runner_sha256")
    checks["runner_functions"] = all(item.get("ok") for item in runner_inspections.values())

    energyplus = fixture.get("energyplus") or {}
    executable = Path(str(energyplus.get("executable", ""))).expanduser()
    checks["energyplus_runtime"] = (
        executable.is_file()
        and hashlib.sha256(executable.read_bytes()).hexdigest() == energyplus.get("sha256")
        and openstudio.openStudioVersion() == str(fixture.get("runtime", {}).get("openstudio"))
    )
    weather_path = (PROJECT / str(fixture.get("weather_path", ""))).resolve()
    try:
        weather_hash = integrity.snapshot_descriptor(weather_path, kind="weather")["snapshot_hash"]
    except (OSError, ValueError):
        weather_hash = None
    checks["weather_snapshot"] = weather_hash == fixture.get("weather_snapshot_hash")
    expected = fixture.get("expected") or {}
    checks["golden_smoke"] = (
        fixture.get("smoke_status") == "passed"
        and all(key in expected for key in ("heating_kwh_m2", "cooling_kwh_m2", "qa_all_pass"))
    )
    detail.update({
        "ok": all(checks.values()),
        "fixture": str(fixture_path),
        "runner": str(runner_path),
        "runner_sha256": runner_hash,
        "expected_runner_sha256": fixture.get("runner_sha256"),
        "weather_snapshot_hash": weather_hash,
        "runtime": {
            "openstudio": openstudio.openStudioVersion(),
            "energyplus_executable": str(executable),
        },
    })
    return detail


def _scenario_contract(declaration: dict[str, Any], inspection: dict[str, Any],
                       *, simulation_ready: bool) -> dict[str, Any]:
    fixture_path = PROJECT / declaration.get("fixture", "")
    checks: dict[str, Any] = {
        "fixture_schema": False,
        "adapter_signature": inspection.get("signature_parameters") == [
            "osm_path", "output_path", "settings",
        ],
        "adapter_hash": False,
        "builder_production": False,
        "builder_hash": False,
        "builder_functions": False,
        "simulation_dependency": simulation_ready,
        "golden_smoke": False,
    }
    detail: dict[str, Any] = {"checks": checks}
    if not fixture_path.exists():
        return detail | {"ok": False, "reason": "fixture_missing"}
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return detail | {"ok": False, "reason": "fixture_invalid", "detail": str(exc)}
    required = {
        "schema_version", "adapter_sha256", "builder", "builder_sha256",
        "required_functions", "expected", "smoke_status",
    }
    checks["fixture_schema"] = fixture.get("schema_version") == 1 and required <= set(fixture)
    checks["adapter_hash"] = inspection.get("sha256") == fixture.get("adapter_sha256")
    builder_path = (PROJECT / str(fixture.get("builder", ""))).resolve()
    checks["builder_production"] = (
        builder_path.exists()
        and builder_path.name not in FORBIDDEN_FILENAMES
        and not FORBIDDEN_PATH_PARTS.intersection(builder_path.parts)
    )
    builder_inspections = {
        name: _inspect_callable(builder_path, name)
        for name in fixture.get("required_functions", [])
    }
    checks["builder_functions"] = bool(builder_inspections) and all(
        item.get("ok") for item in builder_inspections.values()
    )
    builder_hash = hashlib.sha256(builder_path.read_bytes()).hexdigest() if builder_path.exists() else None
    checks["builder_hash"] = builder_hash == fixture.get("builder_sha256")
    expected = fixture.get("expected") or {}
    checks["golden_smoke"] = (
        fixture.get("smoke_status") == "passed"
        and expected.get("heat_delta_c") == 1.0
        and expected.get("cool_delta_c") == -1.0
        and abs(float(expected.get("heating_kwh_m2", 0.0)) - 12.38) <= 0.02
        and abs(float(expected.get("cooling_kwh_m2", 0.0)) - 17.22) <= 0.02
        and expected.get("qa_all_pass") is True
        and expected.get("default_day_unchanged") is True
        and expected.get("sentinel_values_unchanged") is True
    )
    detail.update({
        "ok": all(checks.values()),
        "fixture": str(fixture_path),
        "builder": str(builder_path),
        "builder_sha256": builder_hash,
        "expected_builder_sha256": fixture.get("builder_sha256"),
        "builder_inspections": builder_inspections,
        "expected": expected,
    })
    if not checks["builder_functions"]:
        detail["reason"] = "builder_contract_failed"
    elif not checks["builder_hash"]:
        detail["reason"] = "builder_hash_changed"
    return detail


def _model_editor_contract(declaration: dict[str, Any], inspection: dict[str, Any],
                           *, simulation_ready: bool) -> dict[str, Any]:
    fixture_path = PROJECT / declaration.get("fixture", "")
    checks: dict[str, Any] = {
        "fixture_schema": False,
        "adapter_signature": inspection.get("signature_parameters") == ["osm_path", "output_path", "patches"],
        "adapter_hash": False,
        "graph_adapter_hash": False,
        "measure_runner_hash": False,
        "measure_worker_hash": False,
        "artifact_hash": False,
        "openstudio_runtime": False,
        "simulation_dependency": simulation_ready,
        "golden_smoke": False,
    }
    detail: dict[str, Any] = {"checks": checks}
    if not fixture_path.exists():
        return detail | {"ok": False, "reason": "fixture_missing"}
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return detail | {"ok": False, "reason": "fixture_invalid", "detail": str(exc)}
    required = {
        "schema_version", "adapter_sha256", "graph_adapter", "graph_adapter_sha256",
        "measure_runner", "measure_runner_sha256", "measure_worker", "measure_worker_sha256",
        "artifact", "artifact_sha256", "before_graph_sha256", "after_graph_changed",
        "runtime", "expected_counts", "smoke", "phase3", "smoke_status",
    }
    checks["fixture_schema"] = fixture.get("schema_version") == 4 and required <= set(fixture)
    checks["adapter_hash"] = inspection.get("sha256") == fixture.get("adapter_sha256")
    graph_adapter_path = (PROJECT / str(fixture.get("graph_adapter", ""))).resolve()
    graph_adapter_hash = integrity.sha256_file(graph_adapter_path) if graph_adapter_path.is_file() else None
    checks["graph_adapter_hash"] = graph_adapter_hash == fixture.get("graph_adapter_sha256")
    measure_runner_path = (PROJECT / str(fixture.get("measure_runner", ""))).resolve()
    measure_runner_hash = integrity.sha256_file(measure_runner_path) if measure_runner_path.is_file() else None
    checks["measure_runner_hash"] = measure_runner_hash == fixture.get("measure_runner_sha256")
    measure_worker_path = (PROJECT / str(fixture.get("measure_worker", ""))).resolve()
    measure_worker_hash = integrity.sha256_file(measure_worker_path) if measure_worker_path.is_file() else None
    checks["measure_worker_hash"] = measure_worker_hash == fixture.get("measure_worker_sha256")
    artifact_path = (PROJECT / str(fixture.get("artifact", ""))).resolve()
    artifact_hash = integrity.sha256_file(artifact_path) if artifact_path.is_file() else None
    checks["artifact_hash"] = artifact_hash == fixture.get("artifact_sha256")
    checks["openstudio_runtime"] = openstudio.openStudioVersion() == str(
        (fixture.get("runtime") or {}).get("openstudio")
    )
    expected_counts = fixture.get("expected_counts") or {}
    smoke = fixture.get("smoke") or {}
    phase3 = fixture.get("phase3") or {}
    checks["golden_smoke"] = (
        fixture.get("smoke_status") == "passed"
        and len(str(fixture.get("before_graph_sha256", ""))) == 64
        and fixture.get("after_graph_changed") is True
        and smoke.get("preflight_ready") is True
        and smoke.get("applied") == 3
        and smoke.get("rejected") == 0
        and smoke.get("opening_count_delta") == 1
        and smoke.get("geometry_preflight_ready") is True
        and smoke.get("after_wall_u") == 1.5
        and smoke.get("sentinel_values_unchanged") is True
        and smoke.get("default_day_unchanged") is True
        and phase3.get("air_loops") == 1
        and phase3.get("plant_loops") == 1
        and phase3.get("connected_conditioned_zones") == 5
        and phase3.get("design_days") == 2
        and phase3.get("preflight_ready") is True
        and phase3.get("energy_basis") == "detailed_hvac_consumption"
        and phase3.get("measure_step_result") == "Success"
        and phase3.get("annual_energyplus", {}).get("status") == "passed"
        and phase3.get("annual_energyplus", {}).get("severe_count") == 0
        and all(int(expected_counts.get(key, 0)) > 0 for key in (
            "constructions", "materials", "schedules", "space_types", "spaces", "zones",
        ))
    )
    detail.update({
        "ok": all(checks.values()),
        "fixture": str(fixture_path),
        "artifact": str(artifact_path),
        "artifact_sha256": artifact_hash,
        "adapter_sha256": inspection.get("sha256"),
        "expected_adapter_sha256": fixture.get("adapter_sha256"),
        "graph_adapter": str(graph_adapter_path),
        "graph_adapter_sha256": graph_adapter_hash,
        "expected_graph_adapter_sha256": fixture.get("graph_adapter_sha256"),
        "measure_runner": str(measure_runner_path),
        "measure_runner_sha256": measure_runner_hash,
        "expected_measure_runner_sha256": fixture.get("measure_runner_sha256"),
        "measure_worker": str(measure_worker_path),
        "measure_worker_sha256": measure_worker_hash,
        "expected_measure_worker_sha256": fixture.get("measure_worker_sha256"),
        "expected": {
            "before_graph_sha256": fixture.get("before_graph_sha256"),
            "after_graph_changed": fixture.get("after_graph_changed"), **expected_counts,
        },
    })
    if not checks["adapter_hash"]:
        detail["reason"] = "adapter_hash_changed"
    elif not checks["graph_adapter_hash"]:
        detail["reason"] = "graph_adapter_hash_changed"
    elif not checks["measure_runner_hash"]:
        detail["reason"] = "measure_runner_hash_changed"
    elif not checks["measure_worker_hash"]:
        detail["reason"] = "measure_worker_hash_changed"
    elif not checks["artifact_hash"]:
        detail["reason"] = "golden_artifact_changed"
    elif not checks["golden_smoke"]:
        detail["reason"] = "golden_contract_failed"
    return detail


def _neighborhood_contract(declaration: dict[str, Any], inspection: dict[str, Any],
                           *, simulation_ready: bool) -> dict[str, Any]:
    fixture_path = PROJECT / declaration.get("fixture", "")
    checks: dict[str, Any] = {
        "fixture_schema": False,
        "adapter_signature": inspection.get("signature_parameters") == ["run_dir", "settings"],
        "adapter_hash": False,
        "runner_production": False,
        "runner_hash": False,
        "stock_policy_hash": False,
        "runner_functions": False,
        "simulation_dependency": simulation_ready,
        "golden_smoke": False,
    }
    detail: dict[str, Any] = {"checks": checks}
    if not fixture_path.exists():
        return detail | {"ok": False, "reason": "fixture_missing"}
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return detail | {"ok": False, "reason": "fixture_invalid", "detail": str(exc)}
    required = {
        "schema_version", "adapter_sha256", "runner", "runner_sha256",
        "stock_policy", "stock_policy_sha256",
        "required_functions", "expected", "smoke_status",
    }
    checks["fixture_schema"] = fixture.get("schema_version") == 1 and required <= set(fixture)
    checks["adapter_hash"] = inspection.get("sha256") == fixture.get("adapter_sha256")
    runner_path = (PROJECT / str(fixture.get("runner", ""))).resolve()
    checks["runner_production"] = (
        runner_path.exists()
        and runner_path.name not in FORBIDDEN_FILENAMES
        and not FORBIDDEN_PATH_PARTS.intersection(runner_path.parts)
    )
    runner_inspections = {
        name: _inspect_callable(runner_path, name)
        for name in fixture.get("required_functions", [])
    }
    checks["runner_functions"] = bool(runner_inspections) and all(
        item.get("ok") for item in runner_inspections.values()
    )
    runner_hash = hashlib.sha256(runner_path.read_bytes()).hexdigest() if runner_path.exists() else None
    checks["runner_hash"] = runner_hash == fixture.get("runner_sha256")
    stock_policy_path = (PROJECT / str(fixture.get("stock_policy", ""))).resolve()
    stock_policy_hash = integrity.sha256_file(stock_policy_path) if stock_policy_path.is_file() else None
    checks["stock_policy_hash"] = stock_policy_hash == fixture.get("stock_policy_sha256")
    expected = fixture.get("expected") or {}
    checks["golden_smoke"] = (
        fixture.get("smoke_status") == "passed"
        and expected.get("buildings") == 959
        and expected.get("clusters") == 18
        and expected.get("qa_passed_clusters") == 18
        and all(key in expected for key in ("heating_gwh_yr", "cooling_gwh_yr"))
    )
    detail.update({
        "ok": all(checks.values()),
        "fixture": str(fixture_path),
        "runner": str(runner_path),
        "runner_sha256": runner_hash,
        "expected_runner_sha256": fixture.get("runner_sha256"),
        "stock_policy": str(stock_policy_path),
        "stock_policy_sha256": stock_policy_hash,
        "expected_stock_policy_sha256": fixture.get("stock_policy_sha256"),
        "runner_inspections": runner_inspections,
        "expected": expected,
    })
    if not checks["runner_functions"]:
        detail["reason"] = "runner_contract_failed"
    elif not checks["runner_hash"]:
        detail["reason"] = "runner_hash_changed"
    elif not checks["stock_policy_hash"]:
        detail["reason"] = "stock_policy_hash_changed"
    return detail


def _city_contract(declaration: dict[str, Any], inspection: dict[str, Any],
                   *, neighborhood_ready: bool) -> dict[str, Any]:
    fixture_path = PROJECT / declaration.get("fixture", "")
    checks: dict[str, Any] = {
        "fixture_schema": False,
        "adapter_signature": inspection.get("signature_parameters") == ["run_dir", "settings"],
        "adapter_hash": False,
        "runner_production": False,
        "runner_hash": False,
        "stock_policy_hash": False,
        "runner_functions": False,
        "neighborhood_dependency": neighborhood_ready,
        "golden_smoke": False,
    }
    detail: dict[str, Any] = {"checks": checks}
    if not fixture_path.exists():
        return detail | {"ok": False, "reason": "fixture_missing"}
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return detail | {"ok": False, "reason": "fixture_invalid", "detail": str(exc)}
    required = {
        "schema_version", "adapter_sha256", "runner", "runner_sha256",
        "stock_policy", "stock_policy_sha256",
        "required_functions", "expected", "smoke_status",
    }
    checks["fixture_schema"] = fixture.get("schema_version") == 1 and required <= set(fixture)
    checks["adapter_hash"] = inspection.get("sha256") == fixture.get("adapter_sha256")
    runner_path = (PROJECT / str(fixture.get("runner", ""))).resolve()
    checks["runner_production"] = (
        runner_path.exists()
        and runner_path.name not in FORBIDDEN_FILENAMES
        and not FORBIDDEN_PATH_PARTS.intersection(runner_path.parts)
    )
    runner_inspections = {
        name: _inspect_callable(runner_path, name)
        for name in fixture.get("required_functions", [])
    }
    checks["runner_functions"] = bool(runner_inspections) and all(
        item.get("ok") for item in runner_inspections.values()
    )
    runner_hash = hashlib.sha256(runner_path.read_bytes()).hexdigest() if runner_path.exists() else None
    checks["runner_hash"] = runner_hash == fixture.get("runner_sha256")
    stock_policy_path = (PROJECT / str(fixture.get("stock_policy", ""))).resolve()
    stock_policy_hash = integrity.sha256_file(stock_policy_path) if stock_policy_path.is_file() else None
    checks["stock_policy_hash"] = stock_policy_hash == fixture.get("stock_policy_sha256")
    expected = fixture.get("expected") or {}
    checks["golden_smoke"] = (
        fixture.get("smoke_status") == "passed"
        and expected.get("buildings") == 26452
        and expected.get("clusters") == 21
        and expected.get("districts") == 19
        and expected.get("qa_passed_clusters") == 21
        and all(key in expected for key in (
            "heating_gwh_yr", "cooling_gwh_yr", "hvac_consumption_gwh_yr",
            "total_site_gwh_yr", "hvac_co2_t_yr", "total_site_co2_t_yr",
        ))
    )
    detail.update({
        "ok": all(checks.values()),
        "fixture": str(fixture_path),
        "runner": str(runner_path),
        "runner_sha256": runner_hash,
        "expected_runner_sha256": fixture.get("runner_sha256"),
        "stock_policy": str(stock_policy_path),
        "stock_policy_sha256": stock_policy_hash,
        "expected_stock_policy_sha256": fixture.get("stock_policy_sha256"),
        "runner_inspections": runner_inspections,
        "expected": expected,
    })
    if not checks["runner_functions"]:
        detail["reason"] = "runner_contract_failed"
    elif not checks["runner_hash"]:
        detail["reason"] = "runner_hash_changed"
    elif not checks["stock_policy_hash"]:
        detail["reason"] = "stock_policy_hash_changed"
    return detail


def _lhs_contract(declaration: dict[str, Any], inspection: dict[str, Any],
                  *, simulation_ready: bool) -> dict[str, Any]:
    fixture_path = PROJECT / declaration.get("fixture", "")
    checks: dict[str, Any] = {
        "fixture_schema": False,
        "adapter_signature": inspection.get("signature_parameters") == ["run_dir", "settings"],
        "adapter_hash": False,
        "runner_production": False,
        "runner_hash": False,
        "builder_hash": False,
        "simulation_runner_hash": False,
        "runner_functions": False,
        "simulation_dependency": simulation_ready,
        "golden_smoke": False,
    }
    detail: dict[str, Any] = {"checks": checks}
    if not fixture_path.exists():
        return detail | {"ok": False, "reason": "fixture_missing"}
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return detail | {"ok": False, "reason": "fixture_invalid", "detail": str(exc)}
    required = {
        "schema_version", "adapter_sha256", "runner", "runner_sha256",
        "builder", "builder_sha256", "simulation_runner", "simulation_runner_sha256",
        "required_functions", "expected", "smoke_status",
    }
    checks["fixture_schema"] = fixture.get("schema_version") == 1 and required <= set(fixture)
    checks["adapter_hash"] = inspection.get("sha256") == fixture.get("adapter_sha256")
    runner_path = (PROJECT / str(fixture.get("runner", ""))).resolve()
    checks["runner_production"] = (
        runner_path.exists()
        and runner_path.name not in FORBIDDEN_FILENAMES
        and not FORBIDDEN_PATH_PARTS.intersection(runner_path.parts)
    )
    runner_inspections = {
        name: _inspect_callable(runner_path, name)
        for name in fixture.get("required_functions", [])
    }
    checks["runner_functions"] = bool(runner_inspections) and all(
        item.get("ok") for item in runner_inspections.values()
    )
    runner_hash = hashlib.sha256(runner_path.read_bytes()).hexdigest() if runner_path.exists() else None
    checks["runner_hash"] = runner_hash == fixture.get("runner_sha256")
    builder_path = (PROJECT / str(fixture.get("builder", ""))).resolve()
    builder_hash = hashlib.sha256(builder_path.read_bytes()).hexdigest() if builder_path.exists() else None
    checks["builder_hash"] = builder_hash == fixture.get("builder_sha256")
    simulation_runner_path = (PROJECT / str(fixture.get("simulation_runner", ""))).resolve()
    simulation_runner_hash = (
        hashlib.sha256(simulation_runner_path.read_bytes()).hexdigest()
        if simulation_runner_path.exists() else None
    )
    checks["simulation_runner_hash"] = (
        simulation_runner_hash == fixture.get("simulation_runner_sha256")
    )
    expected = fixture.get("expected") or {}
    required_outputs = {"heating_kwh_m2", "cooling_kwh_m2", "co2_kg_m2"}
    checks["golden_smoke"] = (
        fixture.get("smoke_status") == "passed"
        and expected.get("n") == 50
        and expected.get("seed") == 42
        and required_outputs <= set(expected.get("statistics", {}))
        and expected.get("qa_all_pass") is True
    )
    detail.update({
        "ok": all(checks.values()),
        "fixture": str(fixture_path),
        "runner": str(runner_path),
        "runner_sha256": runner_hash,
        "expected_runner_sha256": fixture.get("runner_sha256"),
        "builder": str(builder_path),
        "builder_sha256": builder_hash,
        "expected_builder_sha256": fixture.get("builder_sha256"),
        "simulation_runner": str(simulation_runner_path),
        "simulation_runner_sha256": simulation_runner_hash,
        "expected_simulation_runner_sha256": fixture.get("simulation_runner_sha256"),
        "runner_inspections": runner_inspections,
        "expected": expected,
    })
    if not checks["runner_functions"]:
        detail["reason"] = "runner_contract_failed"
    elif not checks["runner_hash"]:
        detail["reason"] = "runner_hash_changed"
    elif not checks["builder_hash"]:
        detail["reason"] = "builder_hash_changed"
    elif not checks["simulation_runner_hash"]:
        detail["reason"] = "simulation_runner_hash_changed"
    return detail


def capability_status() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    output: dict[str, Any] = {"schema_version": manifest["schema_version"], "capabilities": {}}
    for name, declaration in manifest["capabilities"].items():
        module = (PROJECT / declaration["module"]).resolve()
        inspection = _inspect_callable(module, declaration.get("entrypoint"))
        fixture_ok = True
        if declaration.get("fixture"):
            fixture_ok = (PROJECT / declaration["fixture"]).exists()
        declared = bool(declaration.get("ready"))
        contract = None
        runtime_ready = declared and inspection["ok"] and fixture_ok
        if name == "simulation":
            contract = _simulation_contract(declaration, inspection)
            runtime_ready = runtime_ready and contract["ok"]
        elif name == "scenario":
            contract = _scenario_contract(
                declaration,
                inspection,
                simulation_ready=bool(
                    output["capabilities"].get("simulation", {}).get("runtime_ready")
                ),
            )
            runtime_ready = runtime_ready and contract["ok"]
        elif name == "neighborhood":
            contract = _neighborhood_contract(
                declaration,
                inspection,
                simulation_ready=bool(
                    output["capabilities"].get("simulation", {}).get("runtime_ready")
                ),
            )
            runtime_ready = runtime_ready and contract["ok"]
        elif name == "city":
            contract = _city_contract(
                declaration,
                inspection,
                neighborhood_ready=bool(
                    output["capabilities"].get("neighborhood", {}).get("runtime_ready")
                ),
            )
            runtime_ready = runtime_ready and contract["ok"]
        elif name == "lhs":
            contract = _lhs_contract(
                declaration,
                inspection,
                simulation_ready=bool(
                    output["capabilities"].get("simulation", {}).get("runtime_ready")
                ),
            )
            runtime_ready = runtime_ready and contract["ok"]
        elif name == "model_editor":
            contract = _model_editor_contract(
                declaration,
                inspection,
                simulation_ready=bool(
                    output["capabilities"].get("simulation", {}).get("runtime_ready")
                ),
            )
            runtime_ready = runtime_ready and contract["ok"]
        item = declaration | {
            "declared_ready": declared,
            "runtime_ready": runtime_ready,
            "inspection": inspection,
            "fixture_ok": fixture_ok,
            "contract": contract,
        }
        item["diagnostic"] = diagnostics(name, declaration, inspection, contract, runtime_ready)
        output["capabilities"][name] = item
    return output
