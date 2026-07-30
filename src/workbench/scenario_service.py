"""Immutable Part G model-variant lifecycle and Simulation orchestration."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from model_config import BuildConfig, OverrideRecord
from workbench import db, integrity, renderer_provenance, service, storage
from workbench.capabilities import capability_status
from workbench.scenario_adapter import create_model_variant
from workbench import simulation_service


PROJECT = Path(__file__).resolve().parents[2]
ADAPTER_PATH = Path(__file__).with_name("scenario_adapter.py")
BUILDER_PATH = PROJECT / "src/model_builder.py"
DELTA_MIN_C = -3.0
DELTA_MAX_C = 3.0
SOURCE_TYPES = {"human_judgement", "dataset", "publication", "supervisor", "other"}
REQUIRED_VISUAL_ARTIFACTS = {
    "scene.json", "model_3d.png", "geometry_actions.json",
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot_item(path: Path, kind: str) -> dict[str, Any]:
    snapshot = integrity.ensure_snapshot(path, kind=kind)
    return {
        "snapshot_hash": snapshot["snapshot_hash"],
        "source_name": snapshot["source_name"],
        "kind": kind,
        "components": snapshot["components"],
    }


def _root_parent(parent_run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    parent = simulation_service._verified_parent(parent_run_id)
    if parent.get("parent_run_id"):
        raise ValueError("Part G scenarios must start from a root model run; scenario stacking is disabled")
    root = Path(parent["artifact_dir"])
    if (root / "scenario_settings.json").exists():
        raise ValueError("Part G scenario variants cannot be used as another scenario parent")
    missing = sorted(name for name in REQUIRED_VISUAL_ARTIFACTS if not (root / name).exists())
    if missing:
        raise ValueError(f"Parent model lacks required visual evidence: {', '.join(missing)}")
    config = BuildConfig.model_validate(parent["config"])
    inputs = simulation_service._parent_inputs(parent)
    return parent | {"config": config.model_dump(mode="json")}, inputs


def _weather_datasets() -> list[dict[str, Any]]:
    output = []
    for dataset in db.list_datasets():
        if dataset["kind"] != "weather" or dataset.get("verification_status") != "VERIFIED":
            continue
        snapshot_hash = dataset.get("snapshot_hash")
        if not snapshot_hash:
            continue
        try:
            snapshot = integrity.load_snapshot(snapshot_hash)
            files = integrity.snapshot_files(snapshot_hash)
        except (OSError, ValueError):
            continue
        source_name = str(snapshot.get("source_name", ""))
        if len(files) != 1 or Path(source_name).suffix.lower() != ".epw":
            continue
        output.append({
            "id": dataset["id"],
            "name": dataset["name"],
            "snapshot_hash": snapshot_hash,
            "source_name": source_name,
            "managed": bool(dataset.get("metadata", {}).get("managed")),
        })
    return output


def scenario_options() -> dict[str, Any]:
    capability = capability_status()["capabilities"].get("scenario", {})
    parents = []
    for candidate in simulation_service.eligible_models():
        run = db.get_run(candidate["id"])
        if not run or run.get("parent_run_id"):
            continue
        root = Path(run["artifact_dir"])
        if (root / "scenario_settings.json").exists():
            continue
        if any(not (root / name).exists() for name in REQUIRED_VISUAL_ARTIFACTS):
            continue
        inputs = simulation_service._parent_inputs(run)
        parents.append(candidate | {
            "weather_source_name": inputs["weather"].get("source_name", "EPW"),
        })
    return {
        "ready": capability.get("runtime_ready") is True,
        "delta_bounds_c": {"min": DELTA_MIN_C, "max": DELTA_MAX_C, "step": 0.1},
        "parents": parents,
        "weather_datasets": _weather_datasets(),
        "source_types": sorted(SOURCE_TYPES),
    }


def _validate_delta(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    if result < DELTA_MIN_C or result > DELTA_MAX_C:
        raise ValueError(f"{label} must be between {DELTA_MIN_C:g} and {DELTA_MAX_C:g} K")
    return 0.0 if result == 0 else result


def _resolve_weather(dataset_id: str | None, parent_inputs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if dataset_id is None:
        return dict(parent_inputs["weather"]), None
    dataset = db.get_dataset(dataset_id)
    if dataset is None:
        raise KeyError(dataset_id)
    if dataset["kind"] != "weather" or dataset.get("verification_status") != "VERIFIED":
        raise ValueError("Scenario weather must be a VERIFIED EPW dataset")
    matches = [item for item in _weather_datasets() if item["id"] == dataset_id]
    if not matches:
        raise ValueError("Scenario weather snapshot is unavailable or is not a valid EPW")
    item = matches[0]
    descriptor = {
        "snapshot_hash": item["snapshot_hash"],
        "source_name": item["source_name"],
        "kind": "weather",
        "components": integrity.load_snapshot(item["snapshot_hash"])["components"],
    }
    return descriptor, item


def _scenario_fingerprint(parent: dict[str, Any], heat_delta: float, cool_delta: float,
                          weather_hash: str) -> str:
    payload = {
        "parent_model_sha256": parent["raw_model_sha256"],
        "heat_delta_c": heat_delta,
        "cool_delta_c": cool_delta,
        "weather_snapshot_hash": weather_hash,
    }
    return hashlib.sha256(integrity.canonical_json_bytes(payload)).hexdigest()


def create_scenario_job(request: dict[str, Any]) -> dict[str, Any]:
    capability = capability_status()["capabilities"].get("scenario", {})
    if capability.get("runtime_ready") is not True:
        raise RuntimeError("Part G scenario capability is not ready")
    reservation = storage.reserve_payload("scenario", {})["storage_reservation"]
    parent, inputs = _root_parent(str(request["parent_run_id"]))
    heat_delta = _validate_delta(request.get("heat_delta_c", 0.0), "Heating offset")
    cool_delta = _validate_delta(request.get("cool_delta_c", 0.0), "Cooling offset")
    name = str(request.get("name", "")).strip()
    reason = str(request.get("reason", "")).strip()
    source_type = str(request.get("source_type", ""))
    source_ref = str(request.get("source_ref") or "").strip() or None
    if len(name) < 2 or len(name) > 80:
        raise ValueError("Scenario name must contain 2 to 80 characters")
    if len(reason) < 3 or len(reason) > 500:
        raise ValueError("Scenario rationale must contain 3 to 500 characters")
    if source_type not in SOURCE_TYPES:
        raise ValueError("Scenario source type is invalid")
    if source_ref and len(source_ref) > 500:
        raise ValueError("Scenario source reference cannot exceed 500 characters")

    weather, weather_dataset = _resolve_weather(request.get("weather_dataset_id"), inputs)
    weather_changed = weather["snapshot_hash"] != inputs["weather"]["snapshot_hash"]
    if heat_delta == 0.0 and cool_delta == 0.0 and not weather_changed:
        raise ValueError("A Part G scenario must change an offset or the parent EPW snapshot")

    fingerprint = _scenario_fingerprint(
        parent, heat_delta, cool_delta, weather["snapshot_hash"],
    )
    active = db.find_current_scenario_job(fingerprint)
    if active:
        raise ValueError(f"An identical Part G scenario job is already active: {active['id']}")

    base_config = BuildConfig.model_validate(parent["config"]).model_copy(deep=True)
    base_config.provenance.scenario_name = name
    changed_fields = []
    if heat_delta != 0.0:
        changed_fields.append("part_g.heat_delta_c")
    if cool_delta != 0.0:
        changed_fields.append("part_g.cool_delta_c")
    if weather_changed:
        changed_fields.append("part_g.weather_snapshot_hash")
    overrides = [{
        "field": field,
        "reason": reason,
        "source_type": source_type,
        "source_ref": source_ref,
    } for field in changed_fields]
    base_config.provenance.overrides.extend(
        OverrideRecord.model_validate(item) for item in overrides
    )
    scenario_settings = {
        "schema_version": 1,
        "name": name,
        "parent_run_id": parent["id"],
        "heat_delta_c": heat_delta,
        "cool_delta_c": cool_delta,
        "weather_dataset_id": weather_dataset["id"] if weather_dataset else None,
        "weather_dataset_name": weather_dataset["name"] if weather_dataset else None,
        "weather_snapshot_hash": weather["snapshot_hash"],
        "weather_source_name": weather["source_name"],
        "weather_changed": weather_changed,
        "reason": reason,
        "source_type": source_type,
        "source_ref": source_ref,
        "scenario_fingerprint": fingerprint,
    }
    scenario_id = db.create_scenario(
        base_config.provenance.baseline_profile,
        name,
        {"base_config": base_config.model_dump(mode="json"), "part_g": scenario_settings},
        overrides,
        [],
    )
    payload = {
        "parent_run_id": parent["id"],
        "scenario_id": scenario_id,
        "config": base_config.model_dump(mode="json"),
        "scenario_settings": scenario_settings,
        "weather": weather,
        "scenario_fingerprint": fingerprint,
        "storage_reservation": reservation,
    }
    job_id = db.create_job("scenario", parent["refparcela"], payload, timeout_seconds=180)
    db.update_scenario_status(scenario_id, "QUEUED")
    return db.get_job(job_id) or {"id": job_id, "status": "queued", "payload": payload}


def _simulation_for_model(model_run_id: str) -> tuple[str | None, str | None]:
    active = db.find_current_simulation_job(model_run_id)
    if active:
        return active["id"], None
    for run in db.list_runs_by_type("simulation"):
        if run.get("parent_run_id") == model_run_id:
            return run["job_id"], run["id"]
    return None, None


def _queue_child_simulation(job_id: str, model_run: dict[str, Any]) -> None:
    simulation_job_id, simulation_run_id = _simulation_for_model(model_run["id"])
    simulation_error = None
    if simulation_job_id is None:
        try:
            child = simulation_service.create_simulation_job(model_run["id"])
            simulation_job_id = child["id"]
        except Exception as exc:  # the verified model remains a valid terminal artifact
            simulation_error = str(exc)
    db.update_job_payload(job_id, {
        "model_run_id": model_run["id"],
        "simulation_job_id": simulation_job_id,
        "simulation_run_id": simulation_run_id,
        "simulation_error": simulation_error,
    })
    if simulation_error:
        db.add_event(job_id, f"Simulation queue warning: {simulation_error}", 1.0, "warning")
    else:
        db.add_event(job_id, "Annual EnergyPlus child queued", 1.0)


def _restore_interrupted_job(job_id: str) -> bool:
    model_run = next(
        (item for item in db.list_runs_by_type("model") if item["job_id"] == job_id), None,
    )
    if model_run is None:
        return False
    service.recover_committing_runs()
    model_run = db.get_run(model_run["id"])
    if not model_run or model_run["verification_status"] != "VERIFIED":
        raise RuntimeError("Interrupted Part G model commit could not be recovered")
    _queue_child_simulation(job_id, model_run)
    if model_run.get("scenario_id"):
        db.update_scenario_status(model_run["scenario_id"], "COMMITTED")
    db.update_job(job_id, "completed", result_path=model_run["artifact_dir"])
    db.add_event(job_id, "Recovered immutable Part G model commit", 1.0)
    return True


def _copy_visual_evidence(parent_root: Path, scratch: Path) -> None:
    for name in REQUIRED_VISUAL_ARTIFACTS:
        shutil.copy2(parent_root / name, scratch / name)
    renderer = parent_root / "renderer_manifest.json"
    if renderer.exists():
        shutil.copy2(renderer, scratch / renderer.name)
    else:
        renderer_provenance.write_renderer_manifest(
            scratch / "scene.json", scratch / "renderer_manifest.json",
        )


def run_scenario_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["kind"] != "scenario":
        raise ValueError(f"Unexpected job kind: {job['kind']}")
    if _restore_interrupted_job(job_id):
        return
    capability = capability_status()["capabilities"].get("scenario", {})
    if capability.get("runtime_ready") is not True:
        raise RuntimeError("Part G source or runtime contract changed before worker execution")

    payload = job["payload"]
    scenario_settings = dict(payload["scenario_settings"])
    parent, parent_inputs = _root_parent(payload["parent_run_id"])
    if payload["scenario_fingerprint"] != _scenario_fingerprint(
        parent,
        float(scenario_settings["heat_delta_c"]),
        float(scenario_settings["cool_delta_c"]),
        payload["weather"]["snapshot_hash"],
    ):
        raise ValueError("Part G job payload fingerprint no longer matches its parent and inputs")

    scratch = service.RUN_ROOT / f".scenario-{job_id}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    parent_root = Path(parent["artifact_dir"])
    parent_osm = simulation_service._parent_model_path(parent)
    exact_parent = scratch / "parent_model.osm"

    try:
        db.update_scenario_status(payload["scenario_id"], "RUNNING")
        db.add_event(job_id, "Parent Verification", 0.08)
        shutil.copyfile(parent_osm, exact_parent)
        if integrity.sha256_file(exact_parent) != parent["raw_model_sha256"]:
            raise IOError("Byte-identical Part G parent copy verification failed")
        _copy_visual_evidence(parent_root, scratch)
        parent_scene_hash = integrity.sha256_file(parent_root / "scene.json")

        db.add_event(job_id, "Schedule and Weather Mutation", 0.38)
        with tempfile.TemporaryDirectory(prefix="workbench-scenario-") as temp_name:
            weather_path = simulation_service._materialize_snapshot(
                payload["weather"]["snapshot_hash"], Path(temp_name) / "weather",
            )
            adapter_settings = {
                "heat_delta_c": scenario_settings["heat_delta_c"],
                "cool_delta_c": scenario_settings["cool_delta_c"],
                "_weather_path": str(weather_path) if scenario_settings["weather_changed"] else None,
            }
            adapter_result = create_model_variant(
                exact_parent, scratch / "model_python.osm", adapter_settings,
            )

        if integrity.sha256_file(scratch / "model_python.osm") == parent["raw_model_sha256"]:
            raise ValueError("Part G mutation produced a byte-identical no-op model")
        geometry_unchanged = integrity.sha256_file(scratch / "scene.json") == parent_scene_hash
        weather_verified = bool(integrity.snapshot_files(payload["weather"]["snapshot_hash"]))
        scenario_checks = [
            {"id": "scenario_parent_verified", "status": "pass", "message": "Parent run and OSM hash verified"},
            {"id": "scenario_heating_schedule", "status": "pass" if adapter_result["audit"]["heating"]["passed"] else "fail", "message": "Heating schedule shift, default day, and sentinels audited"},
            {"id": "scenario_cooling_schedule", "status": "pass" if adapter_result["audit"]["cooling"]["passed"] else "fail", "message": "Cooling schedule shift, default day, and sentinels audited"},
            {"id": "scenario_geometry_unchanged", "status": "pass" if geometry_unchanged else "fail", "message": "Exact visual geometry fingerprint preserved"},
            {"id": "scenario_weather_snapshot", "status": "pass" if weather_verified else "fail", "message": "Selected EPW object-store snapshot verified"},
        ]
        if any(item["status"] == "fail" for item in scenario_checks):
            raise ValueError("Part G scientific mutation QA failed")

        db.add_event(job_id, "Scenario QA", 0.67)
        config = BuildConfig.model_validate(payload["config"])
        parent_qa = dict(parent["qa"])
        qa = parent_qa | {
            "all_pass": bool(parent_qa.get("all_pass", True)),
            "checks": list(parent_qa.get("checks", [])) + scenario_checks,
        }
        stats = dict(parent["stats"])
        stats["part_g"] = {
            "heat_delta_c": scenario_settings["heat_delta_c"],
            "cool_delta_c": scenario_settings["cool_delta_c"],
            "weather_snapshot_hash": scenario_settings["weather_snapshot_hash"],
        }
        combined_inputs = dict(parent_inputs)
        combined_inputs["weather"] = payload["weather"]
        combined_inputs.update({
            "parent_model": _snapshot_item(parent_osm, "model"),
            "scenario_adapter": _snapshot_item(ADAPTER_PATH, "source"),
            "scenario_model_builder": _snapshot_item(BUILDER_PATH, "source"),
        })
        provenance = {
            "parent_run_id": parent["id"],
            "parent_model_sha256": parent["raw_model_sha256"],
            "scenario_id": payload["scenario_id"],
            "scenario_fingerprint": payload["scenario_fingerprint"],
            "scenario_adapter_sha256": integrity.sha256_file(ADAPTER_PATH),
            "model_builder_sha256": integrity.sha256_file(BUILDER_PATH),
            "weather_snapshot_hash": payload["weather"]["snapshot_hash"],
        }
        scenario_settings.update({
            "scenario_id": payload["scenario_id"],
            "parent_model_sha256": parent["raw_model_sha256"],
            "adapter_result": adapter_result,
            "provenance": provenance,
        })
        scenario_qa = {
            "schema_version": 1,
            "all_pass": True,
            "checks": scenario_checks,
            "schedule_audit": adapter_result["audit"],
            "geometry_scene_sha256": parent_scene_hash,
        }
        service.write_json(scratch / "config.json", config.model_dump(mode="json"))
        service.write_json(scratch / "stats.json", stats)
        service.write_json(scratch / "qa.json", qa)
        service.write_json(scratch / "scenario_settings.json", scenario_settings)
        service.write_json(scratch / "scenario_qa.json", scenario_qa)
        service.write_json(scratch / "input_manifest.json", combined_inputs)
        service.write_json(scratch / "environment.json", integrity.environment_manifest())
        with (scratch / "qa.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["id", "status", "message"])
            writer.writeheader()
            writer.writerows(qa["checks"])

        db.add_event(job_id, "Model Commit", 0.82)
        run_id = uuid.uuid4().hex
        staging = service.RUN_ROOT / f".staging-{run_id}"
        destination = service.RUN_ROOT / run_id
        scratch.replace(staging)
        raw_model_sha256 = integrity.sha256_file(staging / "model_python.osm")
        canonical_fingerprint = integrity.canonical_model_fingerprint(staging / "model_python.osm")
        manifest_items = [
            {"name": item["name"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
            for item in service.artifact_manifest(staging) if item["name"] != "manifest.json"
        ]
        manifest = {
            "schema_version": 2,
            "run_id": run_id,
            "job_id": job_id,
            "run_type": "model",
            "parent_run_id": parent["id"],
            "scenario_id": payload["scenario_id"],
            "refparcela": parent["refparcela"],
            "raw_model_sha256": raw_model_sha256,
            "canonical_model_fingerprint": canonical_fingerprint,
            "input_snapshots": combined_inputs,
            "provenance": provenance,
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
            "refparcela": parent["refparcela"],
            "scenario_name": scenario_settings["name"],
            "config": config.model_dump(mode="json"),
            "stats": stats,
            "qa": qa,
            "artifact_dir": str(staging),
            "run_type": "model",
            "parent_run_id": parent["id"],
            "scenario_id": payload["scenario_id"],
            "verification_status": "COMMITTING",
            "raw_model_sha256": raw_model_sha256,
            "canonical_fingerprint": canonical_fingerprint,
            "manifest_sha256": manifest_sha256,
        }
        snapshot_refs = {role: item["snapshot_hash"] for role, item in combined_inputs.items()}
        db.insert_run(run_record, artifacts, snapshot_refs)
        staging.replace(destination)
        for path in destination.iterdir():
            if path.is_file():
                path.chmod(0o444)
        destination.chmod(0o555)
        db.finalize_run(run_id, str(destination), manifest_sha256)
        db.update_scenario_status(payload["scenario_id"], "COMMITTED")
        model_run = db.get_run(run_id)
        if model_run is None:
            raise RuntimeError("Committed Part G run could not be reloaded")
        _queue_child_simulation(job_id, model_run)
        db.update_job(job_id, "completed", result_path=str(destination))
    except Exception:
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
        raise


def scenario_detail(scenario_id: str) -> dict[str, Any]:
    scenario = db.get_scenario(scenario_id)
    if scenario is None:
        raise KeyError(scenario_id)
    model_run = next(
        (item for item in db.list_runs_by_type("model") if item.get("scenario_id") == scenario_id), None,
    )
    simulation_run = None
    if model_run:
        simulation_run = next(
            (item for item in db.list_runs_by_type("simulation") if item.get("parent_run_id") == model_run["id"]),
            None,
        )
    return scenario | {"model_run": model_run, "simulation_run": simulation_run}
