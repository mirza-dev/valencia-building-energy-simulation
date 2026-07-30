"""Immutable job, artifact and map service for the user-owned Part D pipeline."""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from workbench import db, integrity, service, storage
from workbench.capabilities import capability_status
from workbench.capability_sync import compare_golden, promote_fixture, revalidation_plan
from workbench.city_adapter import inspect_city, run_city, source_paths
from workbench.stock_scenarios import domain_kwargs, validate_request, weather_datasets
from workbench.stock_input_policy_service import resolve_job_policy


_MATERIALIZE_LOCK = threading.Lock()


def _require_ready(*, allow_source_drift: bool = False) -> dict[str, Any]:
    capability = capability_status()["capabilities"]["city"]
    if not capability["runtime_ready"]:
        if allow_source_drift and revalidation_plan("city", capability)["eligible"]:
            return capability
        reason = capability.get("contract", {}).get("reason") or capability["inspection"].get("reason")
        raise RuntimeError(f"Part D capability is not ready: {reason or 'contract_failed'}")
    return capability


def city_preflight() -> dict[str, Any]:
    capability = _require_ready()
    policy_context = resolve_job_policy("city")
    result = inspect_city(policy_context=policy_context)
    result["capability"] = {
        "version": capability.get("version"),
        "runner_sha256": capability.get("contract", {}).get("runner_sha256"),
        "adapter_sha256": capability["inspection"].get("sha256"),
    }
    result["input_policy"] = policy_context
    return result


def city_options() -> dict[str, Any]:
    capability = _require_ready()
    features = {
        item["key"]: item["available"]
        for item in capability.get("diagnostic", {}).get("features", [])
    }
    return {
        "features": features,
        "weather_datasets": weather_datasets() if features.get("weather_scenario") else [],
        "delta_bounds_c": {"min": -3.0, "max": 3.0, "step": 0.1},
        "source_types": ["human_judgement", "dataset", "publication", "supervisor", "other"],
    }


def create_city_job(request: dict[str, Any] | None = None,
                    *, revalidation: bool = False) -> dict[str, Any]:
    capability = (
        _require_ready(allow_source_drift=True)
        if revalidation
        else _require_ready()
    )
    active = db.find_current_job("city")
    if active:
        raise ValueError(f"City job already active: {active['id']}")
    request = request or {}
    scenario_settings = (
        {"run_mode": "full_baseline", "scenario": None, "selected_weather": None}
        if revalidation else validate_request(request, scope="Valencia")
    )
    policy_context = resolve_job_policy("city", request.get("input_policy_override"))
    payload = {
        "scope": "Valencia",
        "method": "representative_typology_period",
        "capability_version": capability.get("version"),
        "revalidation_candidate": revalidation,
        "stock_input_policy": policy_context,
        **scenario_settings,
    }
    payload = storage.reserve_payload("city", payload)
    job_id = db.create_job("city", "VALENCIA", payload, timeout_seconds=2400)
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
        if current != input_manifest[role]["snapshot_hash"]:
            raise IOError(f"Part D input changed during the run: {role}")


def _restore_interrupted_commit(job_id: str) -> bool:
    run = next((item for item in db.list_runs_by_type("city") if item["job_id"] == job_id), None)
    if run is None:
        return False
    service.recover_committing_runs()
    recovered = db.get_run(run["id"])
    if recovered and recovered["verification_status"] == "VERIFIED":
        db.update_job(job_id, "completed", result_path=recovered["artifact_dir"])
        db.add_event(job_id, "Recovered immutable city commit", 1.0)
        return True
    raise RuntimeError("Interrupted city commit could not be recovered")


def run_city_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["kind"] != "city":
        raise ValueError(f"Unexpected job kind: {job['kind']}")
    if _restore_interrupted_commit(job_id):
        return
    revalidation = bool(job["payload"].get("revalidation_candidate"))
    capability = (
        _require_ready(allow_source_drift=True)
        if revalidation
        else _require_ready()
    )
    scratch = service.RUN_ROOT / f".city-{job_id}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    try:
        db.add_event(job_id, "Input snapshots", 0.02)
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
        result = run_city(scratch, settings)
        if policy_context:
            _verify_inputs_unchanged(input_manifest, policy_context)
        else:
            _verify_inputs_unchanged(input_manifest)
        revalidation_report = None
        if revalidation:
            revalidation_report = compare_golden(
                "city", result, capability.get("contract", {}).get("expected", {}),
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
            "run_type": "city",
            "scope": "Valencia",
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
        run_record = {
            "id": run_id,
            "job_id": job_id,
            "refparcela": "VALENCIA",
            "scenario_name": (
                "Valencia · capability revalidation"
                if revalidation else
                (job["payload"].get("scenario") or {}).get("name")
                or "Valencia · full Part D baseline"
            ),
            "config": result["settings"],
            "stats": result["summary"],
            "qa": result["qa"],
            "artifact_dir": str(staging),
            "run_type": "city",
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
            promote_fixture("city", capability, revalidation_report, run_id=run_id)
        db.update_job(job_id, "completed", result_path=str(destination))
        db.add_event(job_id, "Finalize", 1.0)
    except Exception:
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
        raise


def city_detail(run_id: str) -> dict[str, Any]:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["run_type"] != "city":
        raise ValueError("Requested run is not a city run")
    result_path = Path(run["artifact_dir"]) / "results.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
    return run | {"result": result, "verification": service.verify_run_artifacts(run_id)}


def list_city_runs() -> list[dict[str, Any]]:
    return [city_detail(run["id"]) for run in db.list_runs_by_type("city")]


def list_city_run_summaries() -> list[dict[str, Any]]:
    return db.list_run_summaries_by_type("city")


def city_map_metrics(run_id: str) -> Path:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["run_type"] != "city":
        raise ValueError("Requested run is not a city run")
    path = Path(run["artifact_dir"]) / "map_metrics.json"
    if not path.exists():
        raise FileNotFoundError("map_metrics.json")
    return path


def _materialize_gis_snapshot(snapshot_hash: str) -> Path:
    root = db.VAR_DIR / "city_map_sources" / snapshot_hash
    source_name = integrity.load_snapshot(snapshot_hash)["source_name"]
    preferred = root / source_name
    if preferred.exists():
        return preferred
    with _MATERIALIZE_LOCK:
        if preferred.exists():
            return preferred
        temporary = root.parent / f".{snapshot_hash}-{uuid.uuid4().hex}"
        temporary.mkdir(parents=True)
        try:
            for name, blob in integrity.snapshot_files(snapshot_hash):
                shutil.copy2(blob, temporary / name)
            temporary.replace(root)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    if not preferred.exists():
        raise FileNotFoundError(f"GIS snapshot source missing: {source_name}")
    return preferred


def city_tile_source(run_id: str | None = None) -> Path:
    if run_id is None:
        return source_paths()["city_gis"][0]
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["run_type"] != "city":
        raise ValueError("Requested run is not a city run")
    manifest_path = Path(run["artifact_dir"]) / "input_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return _materialize_gis_snapshot(manifest["city_gis"]["snapshot_hash"])
