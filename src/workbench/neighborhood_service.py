"""Immutable job and API service for the user-owned Part C pipeline."""

from __future__ import annotations

import json
import hashlib
import shutil
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from workbench import db, integrity, storage
from workbench import service
from workbench.capabilities import capability_status
from workbench.capability_sync import compare_golden, promote_fixture, revalidation_plan
from workbench.neighborhood_map_cache import cache_feature_collection, descriptor_for_file
from workbench.neighborhood_adapter import (
    district_options, inspect_neighborhood, run_neighborhood, run_single_building, source_paths,
)
from workbench.stock_scenarios import domain_kwargs, validate_request, weather_datasets
from workbench.stock_input_policy_service import resolve_job_policy


def _require_ready(*, allow_source_drift: bool = False) -> dict[str, Any]:
    capability = capability_status()["capabilities"]["neighborhood"]
    if not capability["runtime_ready"]:
        if allow_source_drift and revalidation_plan("neighborhood", capability)["eligible"]:
            return capability
        reason = capability.get("contract", {}).get("reason") or capability["inspection"].get("reason")
        raise RuntimeError(f"Part C capability is not ready: {reason or 'contract_failed'}")
    return capability


@lru_cache(maxsize=256)
def _component_sha256(path: str, size_bytes: int, mtime_ns: int, ctime_ns: int) -> str:
    del size_bytes, mtime_ns, ctime_ns
    return integrity.sha256_file(Path(path))


def _preflight_source_key() -> str:
    roles = {"city_gis", "boundary_gis", "tipo15", "neighborhood_pipeline", "neighborhood_adapter"}
    components = []
    for role, (path, kind) in source_paths().items():
        if role not in roles:
            continue
        for component in sorted(integrity.dataset_components(path), key=lambda item: str(item)):
            stat = component.stat()
            components.append({
                "role": role,
                "kind": kind,
                "name": component.name,
                "size_bytes": stat.st_size,
                "sha256": _component_sha256(
                    str(component.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                ),
            })
    return hashlib.sha256(integrity.canonical_json_bytes(components)).hexdigest()


@lru_cache(maxsize=8)
def _cached_preflight(source_key: str, district: str | None, policy_json: str) -> dict[str, Any]:
    del source_key
    policy_context = json.loads(policy_json)
    return inspect_neighborhood(district=district, policy_context=policy_context)


def clear_preflight_cache() -> None:
    _component_sha256.cache_clear()
    _cached_preflight.cache_clear()


def neighborhood_preflight(*, include_map: bool = True, district: str | None = None) -> dict[str, Any]:
    capability = _require_ready()
    policy_context = resolve_job_policy("neighborhood", district=district)
    inspection = _cached_preflight(
        _preflight_source_key(), district,
        json.dumps(policy_context, ensure_ascii=False, sort_keys=True),
    )
    map_data = inspection["map"]
    descriptor = cache_feature_collection(map_data)
    result = {key: value for key, value in inspection.items() if key != "map"}
    result["map"] = map_data if include_map else None
    result["map_descriptor"] = descriptor
    result["capability"] = {
        "version": capability.get("version"),
        "runner_sha256": capability.get("contract", {}).get("runner_sha256"),
        "adapter_sha256": capability["inspection"].get("sha256"),
    }
    result["input_policy"] = policy_context
    return result


def neighborhood_options() -> dict[str, Any]:
    capability = _require_ready()
    features = {
        item["key"]: item["available"]
        for item in capability.get("diagnostic", {}).get("features", [])
    }
    return {
        "features": features,
        "districts": district_options() if features.get("district_scope") else [],
        "weather_datasets": weather_datasets() if features.get("weather_scenario") else [],
        "delta_bounds_c": {"min": -3.0, "max": 3.0, "step": 0.1},
        "source_types": ["human_judgement", "dataset", "publication", "supervisor", "other"],
    }


def create_neighborhood_job(request: dict[str, Any] | None = None,
                            *, revalidation: bool = False) -> dict[str, Any]:
    capability = (
        _require_ready(allow_source_drift=True)
        if revalidation
        else _require_ready()
    )
    active = db.find_current_job("neighborhood")
    if active:
        raise ValueError(f"Neighborhood job already active: {active['id']}")
    request = request or {}
    scope_mode = str(request.get("scope_mode", "boundary"))
    district = str(request.get("district") or "").strip() or None
    building_ref = str(request.get("building_ref") or "").strip() or None
    if scope_mode not in {"boundary", "district", "building"}:
        raise ValueError("Neighborhood scope must be boundary, district, or building")
    if scope_mode == "district" and not district:
        raise ValueError("A municipal district is required")
    if scope_mode == "building" and not building_ref:
        raise ValueError("A refparcela is required for a single-building run")
    if scope_mode != "district":
        district = None
    if scope_mode != "building":
        building_ref = None
    if revalidation:
        scope_mode = "boundary"
        district = None
        building_ref = None
        scenario_settings = {"run_mode": "full_baseline", "scenario": None, "selected_weather": None}
    else:
        scenario_settings = validate_request(request, scope=building_ref or district or "Benicalap")
    policy_context = resolve_job_policy(
        "neighborhood", request.get("input_policy_override"),
        district=district, building_ref=building_ref,
    )
    scope = building_ref.upper() if building_ref else district.upper() if district else "Benicalap"
    payload = {
        "scope": scope,
        "scope_mode": scope_mode,
        "district": district,
        "building_ref": building_ref,
        "method": "representative_typology_period",
        "capability_version": capability.get("version"),
        "revalidation_candidate": revalidation,
        "stock_input_policy": policy_context,
        **scenario_settings,
    }
    payload = storage.reserve_payload("neighborhood", payload)
    job_id = db.create_job("neighborhood", scope.upper(), payload, timeout_seconds=1800)
    return db.get_job(job_id) or {"id": job_id, "status": "queued"}


def _snapshot_inputs(
    selected_weather: dict[str, Any] | None = None,
    policy_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    manifest: dict[str, Any] = {}
    refs: dict[str, str] = {}
    for role, (path, kind) in source_paths(policy_context).items():
        snapshot = integrity.ensure_snapshot(path, kind=kind)
        manifest[role] = {
            "snapshot_hash": snapshot["snapshot_hash"],
            "source_name": snapshot["source_name"],
            "kind": kind,
            "components": snapshot["components"],
        }
        refs[role] = snapshot["snapshot_hash"]
    if selected_weather:
        manifest["selected_weather"] = {
            "snapshot_hash": selected_weather["snapshot_hash"],
            "source_name": selected_weather["source_name"],
            "kind": "weather",
            "components": selected_weather["components"],
        }
        refs["selected_weather"] = selected_weather["snapshot_hash"]
    return manifest, refs


def _verify_inputs_unchanged(
    input_manifest: dict[str, Any], policy_context: dict[str, Any] | None = None,
) -> None:
    for role, (path, kind) in source_paths(policy_context).items():
        current = integrity.snapshot_descriptor(path, kind=kind)["snapshot_hash"]
        expected = input_manifest[role]["snapshot_hash"]
        if current != expected:
            raise IOError(f"Part C input changed during the run: {role}")


def _restore_interrupted_commit(job_id: str) -> bool:
    run = next((item for item in db.list_runs_by_type("neighborhood") if item["job_id"] == job_id), None)
    if run is None:
        return False
    service.recover_committing_runs()
    recovered = db.get_run(run["id"])
    if recovered and recovered["verification_status"] == "VERIFIED":
        db.update_job(job_id, "completed", result_path=recovered["artifact_dir"])
        db.add_event(job_id, "Recovered immutable neighborhood commit", 1.0)
        return True
    raise RuntimeError("Interrupted neighborhood commit could not be recovered")


def run_neighborhood_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["kind"] != "neighborhood":
        raise ValueError(f"Unexpected job kind: {job['kind']}")
    if _restore_interrupted_commit(job_id):
        return
    revalidation = bool(job["payload"].get("revalidation_candidate"))
    capability = (
        _require_ready(allow_source_drift=True)
        if revalidation
        else _require_ready()
    )
    scratch = service.RUN_ROOT / f".neighborhood-{job_id}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    try:
        db.add_event(job_id, "Input snapshots", 0.03)
        selected_weather = job["payload"].get("selected_weather")
        policy_context = job["payload"].get("stock_input_policy")
        if policy_context:
            input_manifest, snapshot_refs = (
                _snapshot_inputs(selected_weather, policy_context)
                if selected_weather
                else _snapshot_inputs(policy_context=policy_context)
            )
        else:  # Jobs queued before schema v6 retain their original automatic contract.
            input_manifest, snapshot_refs = (
                _snapshot_inputs(selected_weather) if selected_weather else _snapshot_inputs()
            )
        scientific_kwargs = domain_kwargs(job["payload"], scratch)
        settings = job["payload"] | {
            "input_snapshot_hashes": {role: item["snapshot_hash"] for role, item in input_manifest.items()},
            "_domain_scenario": scientific_kwargs,
            "_progress": lambda stage, value: db.add_event(job_id, stage, value),
        }
        result = (
            run_single_building(scratch, settings)
            if job["payload"].get("scope_mode") == "building"
            else run_neighborhood(scratch, settings)
        )
        if policy_context:
            _verify_inputs_unchanged(input_manifest, policy_context)
        else:
            _verify_inputs_unchanged(input_manifest)
        revalidation_report = None
        if revalidation:
            revalidation_report = compare_golden(
                "neighborhood", result, capability.get("contract", {}).get("expected", {}),
            )
            service.write_json(scratch / "capability_revalidation.json", revalidation_report)
        service.write_json(scratch / "input_manifest.json", input_manifest)
        service.write_json(scratch / "environment.json", integrity.environment_manifest())

        run_id = uuid.uuid4().hex
        staging = service.RUN_ROOT / f".staging-{run_id}"
        destination = service.RUN_ROOT / run_id
        scratch.replace(staging)
        manifest_items = [
            {"name": item["name"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
            for item in service.artifact_manifest(staging) if item["name"] != "manifest.json"
        ]
        manifest = {
            "schema_version": 3,
            "run_id": run_id,
            "job_id": job_id,
            "run_type": "neighborhood",
            "scope": result["summary"]["scope"],
            "scientific_status": result["qa"]["scientific_status"],
            "input_snapshots": input_manifest,
            "stock_input_policy": policy_context or {"schema_version": 0, "source": "legacy_pre_phase2"},
            "artifacts": manifest_items,
        }
        service.write_json(staging / "manifest.json", manifest)
        manifest_sha256 = integrity.sha256_file(staging / "manifest.json")
        artifacts = service.artifact_manifest(staging)
        for item in artifacts:
            item["path"] = str(destination / item["name"])
        summary = result["summary"]
        run_record = {
            "id": run_id,
            "job_id": job_id,
            "refparcela": str(result["summary"]["scope"]).upper(),
            "scenario_name": (
                f"{result['summary']['scope']} · capability revalidation"
                if revalidation else
                (job["payload"].get("scenario") or {}).get("name")
                or f"{result['summary']['scope']} · full Part C baseline"
            ),
            "config": result["settings"],
            "stats": summary,
            "qa": result["qa"],
            "artifact_dir": str(staging),
            "run_type": "neighborhood",
            "verification_status": "COMMITTING",
            "manifest_sha256": manifest_sha256,
        }
        db.insert_run(run_record, artifacts, snapshot_refs)
        staging.replace(destination)
        for path in destination.iterdir():
            if path.is_file():
                path.chmod(0o444)
        destination.chmod(0o555)
        db.finalize_run(run_id, str(destination), manifest_sha256)
        if revalidation_report and revalidation_report["passed"]:
            promote_fixture("neighborhood", capability, revalidation_report, run_id=run_id)
        db.update_job(job_id, "completed", result_path=str(destination))
        db.add_event(job_id, "Finalize", 1.0)
    except Exception:
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
        raise


def neighborhood_detail(run_id: str) -> dict[str, Any]:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["run_type"] != "neighborhood":
        raise ValueError("Requested run is not a neighborhood run")
    verification = service.verify_run_artifacts(run_id)
    result_path = Path(run["artifact_dir"]) / "results.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
    map_path = Path(run["artifact_dir"]) / "map.geojson"
    if result is not None and map_path.exists() and verification["ok"]:
        result = result | {
            "map_descriptor": descriptor_for_file(
                map_path, url=f"/api/neighborhood/runs/{run_id}/map",
            ),
        }
    return (db.get_run(run_id) or run) | {"result": result, "verification": verification}


def list_neighborhood_runs() -> list[dict[str, Any]]:
    return [neighborhood_detail(run["id"]) for run in db.list_runs_by_type("neighborhood")]


def list_neighborhood_run_summaries() -> list[dict[str, Any]]:
    return db.list_run_summaries_by_type("neighborhood")


def neighborhood_map(run_id: str) -> Path:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["run_type"] != "neighborhood":
        raise ValueError("Requested run is not a neighborhood run")
    verification = service.verify_run_artifacts(run_id)
    if not verification["ok"]:
        raise PermissionError(f"Neighborhood map is not verified: {verification['status']}")
    path = Path(run["artifact_dir"]) / "map.geojson"
    if not path.exists():
        raise FileNotFoundError("map.geojson")
    return path
