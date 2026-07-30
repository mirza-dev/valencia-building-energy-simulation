"""Recoverable edit sessions and immutable authored-model commits."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from model_config import BuildConfig
from workbench import db, integrity, renderer_provenance, service, storage
from workbench.measure_runner import (
    MeasureRejected, apply_model_measure, import_measure_archive, measure_catalog,
)
from workbench.model_editor import (
    POST_PROCESSING_PARAMETERS, PROJECT_PARAMETER_BOUNDS, apply_typed_patches,
    preflight_model,
)
from workbench.model_graph import (
    enrich_scene_construction_ids, extract_model_graph, public_artifact_metadata,
    resolve_model_artifact,
)
from workbench.scene import extract_scene_from_path


PROJECT = Path(__file__).resolve().parents[2]
SESSION_ROOT = Path(os.environ.get("WORKBENCH_MODEL_EDIT_ROOT", db.VAR_DIR / "model_edits"))
SESSION_TTL_HOURS = int(os.environ.get("WORKBENCH_MODEL_EDIT_TTL_HOURS", "24"))
ADAPTER_PATH = Path(__file__).with_name("model_editor.py")
GRAPH_PATH = Path(__file__).with_name("model_graph.py")
MEASURE_RUNNER_PATH = Path(__file__).with_name("measure_runner.py")
MEASURE_WORKER_PATH = Path(__file__).with_name("measure_worker.py")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat()


def _read(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_dir(session_id: str) -> Path:
    if not session_id or any(character not in "0123456789abcdef" for character in session_id):
        raise KeyError(session_id)
    return SESSION_ROOT / session_id


def _metadata(session_id: str, token: str, *, touch: bool = True) -> tuple[Path, dict[str, Any]]:
    root = _session_dir(session_id)
    metadata = _read(root / "session.json")
    if not isinstance(metadata, dict):
        raise KeyError(session_id)
    if not secrets.compare_digest(str(metadata.get("token_hash", "")), _token_hash(token)):
        raise PermissionError("Invalid model edit session token")
    expires_at = datetime.fromisoformat(str(metadata["expires_at"]))
    if expires_at <= _utcnow():
        shutil.rmtree(root, ignore_errors=True)
        raise KeyError("Model edit session expired")
    if touch:
        metadata["updated_at"] = _iso()
        metadata["expires_at"] = _iso(_utcnow() + timedelta(hours=SESSION_TTL_HOURS))
        _atomic_json(root / "session.json", metadata)
    return root, metadata


def cleanup_expired_sessions() -> int:
    SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    removed = 0
    now = _utcnow()
    for root in SESSION_ROOT.iterdir():
        if not root.is_dir():
            continue
        metadata = _read(root / "session.json", {})
        try:
            expired = datetime.fromisoformat(str(metadata.get("expires_at"))) <= now
        except (TypeError, ValueError):
            expired = True
        if expired:
            shutil.rmtree(root, ignore_errors=True)
            removed += 1
    return removed


def _source_documents(artifact: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = Path(artifact["osm_path"]).parent
    run = db.get_run(str(artifact["model_id"])) if artifact.get("source_kind") != "preview" else None
    if run is not None:
        return run["config"], run["stats"], run["qa"]
    config = _read(root / "config.json")
    stats = _read(root / "stats.json")
    qa = _read(root / "qa.json")
    if not all(isinstance(item, dict) for item in (config, stats, qa)):
        raise ValueError("The source model does not carry config/stats/QA documents")
    return config, stats, qa


def _public_session(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key != "token_hash"}


def _session_response(root: Path, metadata: dict[str, Any], *, token: str | None = None,
                      reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    model_path = root / "model_python.osm"
    graph = extract_model_graph(model_path)
    scene = extract_scene_from_path(model_path, _read(root / "base_stats.json", {}))
    response = {
        "session": _public_session(metadata),
        "model": metadata["source"],
        "graph": graph,
        "scene": scene,
        "journal": _read(root / "patch_journal.json", []),
        "run_settings": metadata.get("run_settings", {}),
        "preflight": preflight_model(model_path),
        "reports": reports or [],
        "measures": measure_catalog(root),
    }
    if token is not None:
        response["token"] = token
    return response


def create_session(model_id: str) -> dict[str, Any]:
    cleanup_expired_sessions()
    admission = storage.admission("model_edit")
    if not admission["allowed"]:
        raise storage.StorageAdmissionError(
            f"Insufficient protected disk capacity for model_edit: {admission['reason']}",
            admission,
        )
    artifact = resolve_model_artifact(model_id)
    config, stats, qa = _source_documents(artifact)
    source_root = Path(artifact["osm_path"]).parent
    input_manifest = source_root / "input_manifest.json"
    if not input_manifest.is_file():
        raise ValueError("The source model has no immutable input manifest")
    session_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    root = SESSION_ROOT / session_id
    root.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(artifact["osm_path"], root / "model_python.osm")
        shutil.copy2(artifact["scene_path"], root / "scene.json")
        shutil.copy2(input_manifest, root / "input_manifest.json")
        _atomic_json(root / "base_config.json", config)
        _atomic_json(root / "base_stats.json", stats)
        _atomic_json(root / "base_qa.json", qa)
        _atomic_json(root / "patch_journal.json", [])
        _atomic_json(root / "measure_provenance.json", [])
        now = _utcnow()
        metadata = {
            "schema_version": 1,
            "id": session_id,
            "token_hash": _token_hash(token),
            "status": "open",
            "source_model_id": model_id,
            "resolved_model_id": artifact["model_id"],
            "authored_from": artifact["model_id"],
            "source": public_artifact_metadata(artifact),
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "expires_at": _iso(now + timedelta(hours=SESSION_TTL_HOURS)),
            "patch_count": 0,
            "storage_reservation": admission,
            "run_settings": {"cop": None, "seer": None, "emission_factor": None},
        }
        _atomic_json(root / "session.json", metadata)
        return _session_response(root, metadata, token=token)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def session_detail(session_id: str, token: str) -> dict[str, Any]:
    root, metadata = _metadata(session_id, token)
    return _session_response(root, metadata)


def import_session_measure(session_id: str, token: str, archive_path: Path) -> dict[str, Any]:
    root, metadata = _metadata(session_id, token)
    uploaded = import_measure_archive(root, archive_path)
    response = _session_response(root, metadata)
    response["uploaded_measure"] = uploaded
    return response


def _post_setting_report(index: int, patch: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    payload = patch.get("payload") or {}
    key = str(payload.get("key") or "")
    if key not in POST_PROCESSING_PARAMETERS:
        raise ValueError(key)
    try:
        value = float(payload.get("value"))
    except (TypeError, ValueError):
        return {"index": index, "op": patch.get("op"), "status": "rejected", "severity": "error", "code": "number_required", "message": f"{key} must be a finite number", "warnings": []}
    if not value == value or value <= 0:
        return {"index": index, "op": patch.get("op"), "status": "rejected", "severity": "error", "code": "physical_limit", "message": f"{key} must be greater than zero", "warnings": []}
    bounds = {"cop": (1.0, 3.0), "seer": (1.8, 3.5), "emission_factor": (0.15, 0.331)}[key]
    warnings = [] if bounds[0] <= value <= bounds[1] else [{
        "severity": "warning", "code": "outside_lhs_band",
        "message": f"{key}={value:g} is outside the project reference band {bounds[0]:g}–{bounds[1]:g}; the run setting was stored",
    }]
    before = (metadata.get("run_settings") or {}).get(key)
    metadata.setdefault("run_settings", {})[key] = value
    return {
        "index": index, "op": patch.get("op"), "status": "applied",
        "severity": "warning" if warnings else "success", "code": "applied",
        "message": f"{key} run setting stored", "warnings": warnings,
        "result": {"key": key, "before": before, "value": value, "target": "run.carbon_settings"},
    }


def apply_session_edits(session_id: str, token: str,
                        patches: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(patches, list) or not patches:
        raise ValueError("At least one typed patch is required")
    root, metadata = _metadata(session_id, token)
    current = root / "model_python.osm"
    journal = _read(root / "patch_journal.json", [])
    reports: list[dict[str, Any]] = []
    measure_provenance = _read(root / "measure_provenance.json", [])
    for index, patch in enumerate(patches):
        if not isinstance(patch, dict):
            reports.append({"index": index, "op": None, "status": "rejected", "severity": "error", "code": "patch_object", "message": "Patch must be an object", "warnings": []})
            continue
        payload = patch.get("payload") or {}
        is_post = patch.get("op") == "project_parameter.update" and payload.get("key") in POST_PROCESSING_PARAMETERS
        before_hash = extract_model_graph(current)["graph_sha256"]
        is_measure = patch.get("op") == "measure.apply"
        if is_post:
            report = _post_setting_report(index, patch, metadata)
            after_hash = before_hash
        elif is_measure:
            measure_id = str(payload.get("measure_id") or "")
            arguments = payload.get("arguments") or {}
            if not isinstance(arguments, dict):
                report = {"index": index, "op": patch.get("op"), "status": "rejected", "severity": "error", "code": "measure_arguments", "message": "Measure arguments must be an object", "warnings": []}
                after_hash = before_hash
            else:
                candidate = root / f"candidate-{uuid.uuid4().hex}.osm"
                try:
                    outcome = apply_model_measure(current, candidate, root, measure_id, arguments)
                    candidate.replace(current)
                    source_archive = Path(outcome.pop("source_archive"))
                    report = {
                        "index": index, "op": patch.get("op"), "status": "applied", "severity": "success",
                        "code": "applied", "message": f"Measure {outcome['measure']['display_name']} applied",
                        "result": outcome, "warnings": [],
                    }
                    measure_provenance.append({
                        "sequence": len(journal) + 1,
                        "measure": outcome["measure"],
                        "arguments": arguments,
                        "openstudio_cli": outcome["openstudio_cli"],
                        "openstudio_version": outcome["openstudio_version"],
                        "step_result": outcome["step_result"],
                        "source_archive_name": source_archive.name,
                        "source_archive_path": str(source_archive),
                        "source_archive_sha256": outcome["source_archive_sha256"],
                        "output_osm_sha256": outcome["output_osm_sha256"],
                        "applied_at": _iso(),
                    })
                except MeasureRejected as exc:
                    report = {"index": index, "op": patch.get("op"), "status": "rejected", "severity": "error", "code": exc.code, "message": str(exc), "warnings": []}
                finally:
                    candidate.unlink(missing_ok=True)
                after_hash = extract_model_graph(current)["graph_sha256"]
        else:
            candidate = root / f"candidate-{uuid.uuid4().hex}.osm"
            try:
                result = apply_typed_patches(current, candidate, [patch])
                report = dict(result["reports"][0]) | {"index": index}
                if report["status"] == "applied":
                    candidate.replace(current)
                else:
                    candidate.unlink(missing_ok=True)
            finally:
                candidate.unlink(missing_ok=True)
            after_hash = extract_model_graph(current)["graph_sha256"]
        reports.append(report)
        if report["status"] == "applied":
            journal.append({
                "sequence": len(journal) + 1,
                "patch": patch,
                "report": report,
                "before_graph_sha256": before_hash,
                "after_graph_sha256": after_hash,
                "applied_at": _iso(),
            })
    metadata["patch_count"] = len(journal)
    metadata["updated_at"] = _iso()
    metadata["expires_at"] = _iso(_utcnow() + timedelta(hours=SESSION_TTL_HOURS))
    _atomic_json(root / "patch_journal.json", journal)
    _atomic_json(root / "measure_provenance.json", measure_provenance)
    _atomic_json(root / "session.json", metadata)
    return _session_response(root, metadata, reports=reports)


def preflight_session(session_id: str, token: str) -> dict[str, Any]:
    root, metadata = _metadata(session_id, token)
    result = preflight_model(root / "model_python.osm")
    graph = extract_model_graph(root / "model_python.osm")
    plausible = []
    for item in graph["project_parameters"]:
        bounds = item.get("warn_bounds")
        value = item.get("current_value")
        if bounds and isinstance(value, (int, float)) and not bounds["minimum"] <= value <= bounds["maximum"]:
            plausible.append({"code": "outside_lhs_band", "message": f"{item['key']}={value:g} is outside {bounds['minimum']:g}–{bounds['maximum']:g}; commit remains allowed"})
    result["warnings"].extend(plausible)
    result["graph_sha256"] = graph["graph_sha256"]
    result["patch_count"] = metadata["patch_count"]
    result["run_settings"] = metadata.get("run_settings", {})
    return result


def _updated_build_config(base: dict[str, Any], graph: dict[str, Any],
                          scenario_name: str) -> dict[str, Any]:
    config = BuildConfig.model_validate(base).model_copy(deep=True)
    parameters = {item["key"]: item.get("current_value") for item in graph["project_parameters"]}
    for key, target, attribute, converter in (
        ("wall_u", config.envelope, "wall_u", float),
        ("roof_u", config.envelope, "roof_u", float),
        ("window_u", config.envelope, "window_u", float),
        ("window_g", config.envelope, "window_g", float),
        ("thermal_bridge_du", config.envelope, "thermal_bridge_du", float),
        ("massless", config.envelope, "massless", bool),
        ("infiltration_ach", config.operation, "infiltration_ach", float),
        ("shade_setpoint", config.shading, "setpoint_w_m2", float),
        ("context_shading", config.shading, "context_enabled", bool),
        ("ground_unconditioned", config.geometry, "ground_unconditioned", bool),
    ):
        value = parameters.get(key)
        if value is not None:
            setattr(target, attribute, converter(value))
    config.provenance.scenario_name = scenario_name
    return config.model_dump(mode="json")


def _authored_stats(base: dict[str, Any], graph: dict[str, Any], scene: dict[str, Any], patch_count: int) -> dict[str, Any]:
    stats = dict(base)
    active_wall = next((item for item in graph["constructions"] if item["surface_usage"]["surface_types"].get("Wall")), None)
    active_roof = next((item for item in graph["constructions"] if item["surface_usage"]["surface_types"].get("RoofCeiling")), None)
    context_parameter = next(
        (item for item in graph["project_parameters"] if item.get("key") == "context_shading"),
        {"evidence": {}},
    )
    stats.update({
        "wall_construction": active_wall["name"] if active_wall else stats.get("wall_construction"),
        "roof_construction": active_roof["name"] if active_roof else stats.get("roof_construction"),
        "n_shading_surfaces": int(context_parameter["evidence"].get("site_shading_surfaces", 0)),
        "editor_graph_sha256": graph["graph_sha256"],
        "authored_patch_count": patch_count,
        "project_parameters": {item["key"]: item.get("current_value") for item in graph["project_parameters"]},
        "n_windows": sum(item.get("subsurface_type") in {"FixedWindow", "OperableWindow"} for item in scene["subsurfaces"]),
        "n_balcony_doors": sum(item.get("subsurface_type") == "GlassDoor" for item in scene["subsurfaces"]),
        "window_area_m2": round(sum(float(item.get("area_m2") or 0.0) for item in scene["subsurfaces"]
                                    if item.get("subsurface_type") in {"FixedWindow", "OperableWindow", "GlassDoor"}), 3),
        "n_party_surfaces": sum(item.get("boundary_condition") == "Adiabatic" for item in scene["surfaces"]),
        "n_floors_total": len({item.get("story", {}).get("id") for item in graph["spaces"] if item.get("story")}),
        "n_floors_residential": len({item.get("story", {}).get("id") for item in graph["spaces"]
                                     if "Vivienda" in str((item.get("space_type") or {}).get("name"))}),
        "res_area_m2": round(sum(float(item.get("floor_area_m2") or 0.0) for item in graph["spaces"]
                                 if "Vivienda" in str((item.get("space_type") or {}).get("name"))), 3),
        "n_overhangs": sum(item.get("category") == "overhang" for item in scene["shading"]),
        "facade_qa": scene["facade_qa"],
    })
    return stats


def commit_session(session_id: str, token: str, scenario_name: str | None = None) -> dict[str, Any]:
    root, metadata = _metadata(session_id, token, touch=False)
    journal = _read(root / "patch_journal.json", [])
    if not journal:
        raise ValueError("Commit requires at least one applied edit")
    preflight = preflight_session(session_id, token)
    if not preflight["ready"]:
        raise ValueError("Model preflight failed: " + "; ".join(item["message"] for item in preflight["errors"]))
    source_name = str(metadata["source"].get("scenario_name") or "PlantillaOS model")
    name = str(scenario_name or f"{source_name} · authored edit").strip()
    if len(name) < 2 or len(name) > 100:
        raise ValueError("Scenario name must contain 2–100 characters")
    payload = storage.reserve_payload("model_edit", {
        "session_id": session_id,
        "authored_from": metadata["authored_from"],
        "patch_count": len(journal),
    })
    job_id = db.create_immediate_job("model_edit", str(metadata["source"].get("refparcela") or "authored"), payload)
    run_id = uuid.uuid4().hex
    staging = service.RUN_ROOT / f".staging-{run_id}"
    destination = service.RUN_ROOT / run_id
    staging.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(root / "model_python.osm", staging / "model_python.osm")
        scene = extract_scene_from_path(staging / "model_python.osm", _read(root / "base_stats.json", {}))
        service.write_json(staging / "scene.json", scene)
        graph = extract_model_graph(staging / "model_python.osm")
        config = _updated_build_config(_read(root / "base_config.json", {}), graph, name)
        stats = _authored_stats(_read(root / "base_stats.json", {}), graph, scene, len(journal))
        qa = {
            "all_pass": preflight["ready"],
            "warning_count": len(preflight["warnings"]),
            "warnings": [item["message"] for item in preflight["warnings"]],
            "checks": [{"id": key, "status": "pass" if passed else "fail", "message": key.replace("_", " ")} for key, passed in preflight["checks"].items()],
        }
        inputs = _read(root / "input_manifest.json", {})
        for role, path in (
            ("model_editor_adapter", ADAPTER_PATH), ("model_graph_adapter", GRAPH_PATH),
            ("measure_runner_adapter", MEASURE_RUNNER_PATH), ("measure_worker_adapter", MEASURE_WORKER_PATH),
        ):
            inputs[role] = integrity.ensure_snapshot(path, kind="source")
        measure_provenance = _read(root / "measure_provenance.json", [])
        committed_measure_provenance = []
        for index, item in enumerate(measure_provenance, 1):
            source = Path(str(item.get("source_archive_path") or ""))
            if not source.is_file() or integrity.sha256_file(source) != item.get("source_archive_sha256"):
                raise ValueError("Measure source archive failed provenance verification")
            destination_name = f"measure-{index:03d}-{source.name}"
            shutil.copy2(source, staging / destination_name)
            inputs[f"measure:{index:03d}"] = integrity.ensure_snapshot(staging / destination_name, kind="measure")
            committed_measure_provenance.append({
                key: value for key, value in item.items()
                if key not in {"source_archive_path", "source_archive_name"}
            } | {"source_archive": destination_name})
        service.write_json(staging / "input_manifest.json", inputs)
        service.write_json(staging / "config.json", config)
        service.write_json(staging / "stats.json", stats)
        service.write_json(staging / "qa.json", qa)
        service.write_json(staging / "patch_journal.json", journal)
        service.write_json(staging / "measure_provenance.json", committed_measure_provenance)
        service.write_json(staging / "model_graph.json", graph)
        service.write_json(staging / "preflight.json", preflight)
        editor_provenance = {
            "schema_version": 1,
            "provenance": "authored",
            "authored_from": metadata["authored_from"],
            "source_model_id": metadata["source_model_id"],
            "session_id": session_id,
            "patch_count": len(journal),
            "run_settings": metadata.get("run_settings", {}),
            "measure_steps": len(committed_measure_provenance),
            "adapter_sha256": integrity.sha256_file(ADAPTER_PATH),
            "graph_adapter_sha256": integrity.sha256_file(GRAPH_PATH),
            "measure_runner_sha256": integrity.sha256_file(MEASURE_RUNNER_PATH),
            "measure_worker_sha256": integrity.sha256_file(MEASURE_WORKER_PATH),
            "committed_at": _iso(),
        }
        service.write_json(staging / "editor_provenance.json", editor_provenance)
        service.write_json(staging / "environment.json", integrity.environment_manifest())
        renderer_manifest = renderer_provenance.write_renderer_manifest(
            staging / "scene.json", staging / "renderer_manifest.json",
        )
        raw_model_sha256 = integrity.sha256_file(staging / "model_python.osm")
        canonical_fingerprint = integrity.canonical_model_fingerprint(staging / "model_python.osm")
        manifest_items = [{
            "name": item["name"], "sha256": item["sha256"], "size_bytes": item["size_bytes"],
        } for item in service.artifact_manifest(staging) if item["name"] != "manifest.json"]
        manifest = {
            "schema_version": 3,
            "run_id": run_id,
            "job_id": job_id,
            "run_type": "model",
            "provenance": "authored",
            "authored_from": metadata["authored_from"],
            "refparcela": metadata["source"].get("refparcela"),
            "raw_model_sha256": raw_model_sha256,
            "canonical_model_fingerprint": canonical_fingerprint,
            "graph_sha256": graph["graph_sha256"],
            "patch_journal_sha256": integrity.sha256_file(staging / "patch_journal.json"),
            "input_snapshots": inputs,
            "artifacts": manifest_items,
        }
        service.write_json(staging / "manifest.json", manifest)
        manifest_sha256 = integrity.sha256_file(staging / "manifest.json")
        artifacts = service.artifact_manifest(staging)
        for item in artifacts:
            item["path"] = str(destination / item["name"])
        run = {
            "id": run_id,
            "job_id": job_id,
            "refparcela": str(metadata["source"].get("refparcela") or "authored"),
            "scenario_name": name,
            "config": config,
            "stats": stats,
            "qa": qa,
            "artifact_dir": str(staging),
            "run_type": "model",
            "verification_status": "COMMITTING",
            "raw_model_sha256": raw_model_sha256,
            "canonical_fingerprint": canonical_fingerprint,
            "manifest_sha256": manifest_sha256,
            "provenance": "authored",
            "authored_from": metadata["authored_from"],
            "patch_journal": journal,
        }
        snapshot_refs = {role: item["snapshot_hash"] for role, item in inputs.items() if isinstance(item, dict) and item.get("snapshot_hash")}
        snapshot_refs.update({f"renderer:{index:03d}": item["snapshot_hash"] for index, item in enumerate(renderer_manifest.get("sources", []), 1)})
        db.insert_run(run, artifacts, snapshot_refs)
        staging.replace(destination)
        for path in destination.iterdir():
            if path.is_file():
                path.chmod(0o444)
        destination.chmod(0o555)
        db.finalize_run(run_id, str(destination), manifest_sha256)
        db.update_job(job_id, "completed", result_path=str(destination))
        db.add_event(job_id, "Immutable authored model committed", 1.0)
        shutil.rmtree(root, ignore_errors=True)
        return db.get_run(run_id) or run
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        db.update_job(job_id, "failed", error=str(exc))
        if db.get_run(run_id):
            db.update_run_verification(run_id, "TAMPERED")
        raise


def discard_session(session_id: str, token: str) -> dict[str, Any]:
    root, metadata = _metadata(session_id, token, touch=False)
    patch_count = int(metadata.get("patch_count", 0))
    shutil.rmtree(root, ignore_errors=True)
    return {"session_id": session_id, "discarded": True, "patch_count": patch_count}


def editor_options() -> dict[str, Any]:
    from workbench.model_editor import (
        HVAC_AIR_LOOP_TEMPLATES, HVAC_EQUIPMENT_LIBRARY, HVAC_SYSTEMS, SUPPORTED_PATCHES,
    )

    return {
        "schema_version": 3,
        "mode_default": "advanced",
        "guardrail": "warn_not_block",
        "base_template": "data/templates/PlantillaOS_v2.osm",
        "supported_patches": sorted(SUPPORTED_PATCHES),
        "project_parameter_bounds": {key: {"minimum": value[0], "maximum": value[1]} for key, value in PROJECT_PARAMETER_BOUNDS.items()},
        "hvac_systems": [{"id": key, **value} for key, value in HVAC_SYSTEMS.items()],
        "hvac_air_loop_templates": [{"id": key, **value} for key, value in HVAC_AIR_LOOP_TEMPLATES.items()],
        "hvac_equipment_library": [{"id": key, **value} for key, value in HVAC_EQUIPMENT_LIBRARY.items()],
        "measures": measure_catalog(Path("/nonexistent-workbench-session")),
        "guided_presets": [
            {
                "id": "comfort_plus_one_minus_one", "name": "Comfort +1 / −1 K",
                "patches": [{"op": "thermostat.set_setpoints", "target_id": "first_conditioned_zone", "payload": {"heating_delta_c": 1.0, "cooling_delta_c": -1.0}}],
            },
            {
                "id": "cte_b3_envelope", "name": "CTE B3 envelope study",
                "patches": [
                    {"op": "project_parameter.update", "payload": {"key": "wall_u", "value": 0.56}},
                    {"op": "project_parameter.update", "payload": {"key": "window_u", "value": 2.3}},
                ],
            },
            {
                "id": "ideal_loads", "name": "Ideal Loads",
                "patches": [{"op": "hvac.set_system", "payload": {"system": "ideal_loads"}}],
            },
            {
                "id": "vav_hot_water_dx", "name": "Detailed VAV + hot-water reheat",
                "patches": [{"op": "hvac.air_loop.create", "payload": {"template": "vav_reheat_dx"}}],
            },
        ],
    }
