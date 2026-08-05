"""Immutable EnergyPlus child-run lifecycle for the local workbench."""

from __future__ import annotations

import json
import platform
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

import geopandas as gpd
import openstudio

import run_simulation as part_b
from model_config import OUTPUT_VARIABLES
from workbench import db, integrity, storage
from workbench.capabilities import capability_status
from workbench.simulation_adapter import run_simulation
from workbench import service


PROJECT = Path(__file__).resolve().parents[2]
ADAPTER_PATH = Path(__file__).with_name("simulation_adapter.py")
RUNNER_PATH = PROJECT / "src/run_simulation.py"
# Windows names the binary energyplus.exe; everywhere else it has no suffix.
ENERGYPLUS_EXECUTABLE = (
    Path(part_b.EPLUS_DIR)
    / ("energyplus.exe" if platform.system() == "Windows" else "energyplus")
).resolve()
TERMINAL_STATUSES = {"completed", "failed", "canceled"}


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


def _parent_model_path(parent: dict[str, Any]) -> Path:
    path = Path(parent["artifact_dir"]) / "model_python.osm"
    if not path.exists():
        raise ValueError("Parent model artifact is missing")
    if not parent.get("raw_model_sha256"):
        raise ValueError("Parent model has no raw OSM fingerprint")
    if integrity.sha256_file(path) != parent["raw_model_sha256"]:
        db.update_run_verification(parent["id"], "TAMPERED")
        raise ValueError("Parent OSM no longer matches its immutable run hash")
    return path


def _verified_parent(parent_run_id: str) -> dict[str, Any]:
    parent = db.get_run(parent_run_id)
    if parent is None:
        raise KeyError(parent_run_id)
    if parent["run_type"] != "model":
        raise ValueError("Simulation parent must be a model run")
    if parent["verification_status"] in {"LEGACY", "TAMPERED"}:
        raise ValueError(f"{parent['verification_status']} model runs cannot be simulated")
    verification = service.verify_run_artifacts(parent_run_id)
    if verification["status"] != "VERIFIED":
        raise ValueError("Parent model artifacts are not verified")
    _parent_model_path(parent)
    return db.get_run(parent_run_id) or parent


def _parent_inputs(parent: dict[str, Any]) -> dict[str, Any]:
    path = Path(parent["artifact_dir"]) / "input_manifest.json"
    if not path.exists():
        raise ValueError("Parent run has no immutable input snapshot manifest")
    manifest = _read_json(path)
    if "weather" not in manifest or "buildings" not in manifest:
        raise ValueError("Parent run is missing weather or GIS snapshot provenance")
    for role in ("weather", "buildings"):
        integrity.snapshot_files(manifest[role]["snapshot_hash"])
    return manifest


def _model_settings(osm_path: Path, parent: dict[str, Any]) -> dict[str, Any]:
    translator = openstudio.osversion.VersionTranslator()
    loaded = translator.loadModel(openstudio.toPath(str(osm_path)))
    if loaded.isNull():
        raise ValueError("Verified parent OSM cannot be loaded by OpenStudio")
    model = loaded.get()
    variables = []
    for variable in model.getOutputVariables():
        variables.append({
            "name": variable.variableName(),
            "key": variable.keyValue(),
            "frequency": variable.reportingFrequency(),
        })
    exact = parent.get("config", {}).get("operation", {}).get("output_variables", OUTPUT_VARIABLES)
    declared_names = {item["name"] for item in variables}
    missing = [name for name in exact.values() if name not in declared_names]
    detailed_hvac = bool(model.getAirLoopHVACs() or model.getPlantLoops() or any(
        item.iddObjectType().valueName() != "OS_ZoneHVAC_IdealLoadsAirSystem"
        for zone in model.getThermalZones() for item in zone.equipment()
    ))
    if missing and not detailed_hvac:
        raise ValueError(f"Parent OSM lacks required annual output variables: {', '.join(missing)}")
    area = float(parent.get("stats", {}).get("res_area_m2") or 0.0)
    if area <= 0:
        raise ValueError("Parent model has no positive conditioned residential area")
    editor_path = Path(parent["artifact_dir"]) / "editor_provenance.json"
    editor = _read_json(editor_path) if editor_path.exists() else {}
    run_settings = editor.get("run_settings") if isinstance(editor, dict) else None
    return {
        "run_period": "annual",
        "timestep_per_hour": int(model.getTimestep().numberOfTimestepsPerHour()),
        "output_variables": variables,
        "energy_output_variables": exact,
        "energy_basis": "detailed_hvac_consumption" if detailed_hvac else "ideal_loads_demand",
        "area_basis": "conditioned_residential_area",
        "conditioned_residential_area_m2": area,
        "carbon_settings": {
            key: value for key, value in (run_settings or {}).items() if value is not None
        },
    }


def _materialize_snapshot(snapshot_hash: str, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    source_name = integrity.load_snapshot(snapshot_hash)["source_name"]
    for name, blob in integrity.snapshot_files(snapshot_hash):
        shutil.copy2(blob, destination / name)
    preferred = destination / source_name
    if preferred.exists():
        return preferred
    files = sorted(path for path in destination.iterdir() if path.is_file())
    if len(files) == 1:
        return files[0]
    shapefiles = [path for path in files if path.suffix.lower() == ".shp"]
    if len(shapefiles) == 1:
        return shapefiles[0]
    raise ValueError(f"Snapshot {snapshot_hash} has no unambiguous primary file")


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _cadastre_heating(parent: dict[str, Any], inputs: dict[str, Any], temp_root: Path) -> dict[str, Any]:
    path = _materialize_snapshot(inputs["buildings"]["snapshot_hash"], temp_root / "buildings")
    frame = gpd.read_file(path)
    if "refparcela" not in frame.columns:
        raise ValueError("Parent GIS snapshot has no refparcela field")
    matches = frame.loc[frame["refparcela"].astype(str) == str(parent["refparcela"])]
    if matches.empty:
        raise ValueError("Parent building is absent from its GIS snapshot")
    row = matches.iloc[0]
    baseline = _number(row.get("demanda_ca"))
    intervention = _number(row.get("demanda__1"))
    return {
        "baseline_heating_kwh_m2": baseline if baseline and baseline > 0 else None,
        "post_intervention_heating_kwh_m2": intervention if intervention and intervention > 0 else None,
        "cooling_reference": None,
        "source_snapshot_hash": inputs["buildings"]["snapshot_hash"],
    }


def _source_provenance(parent: dict[str, Any], inputs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    extra = {
        "parent_model": _snapshot_item(_parent_model_path(parent), "model"),
        "simulation_adapter": _snapshot_item(ADAPTER_PATH, "source"),
        "simulation_runner": _snapshot_item(RUNNER_PATH, "source"),
        "energyplus_executable": _snapshot_item(ENERGYPLUS_EXECUTABLE, "runtime"),
    }
    combined = dict(inputs) | extra
    provenance = {
        "parent_run_id": parent["id"],
        "parent_model_sha256": parent["raw_model_sha256"],
        "parent_canonical_fingerprint": parent["canonical_fingerprint"],
        "weather_snapshot_hash": inputs["weather"]["snapshot_hash"],
        "adapter_sha256": integrity.sha256_file(ADAPTER_PATH),
        "runner_sha256": integrity.sha256_file(RUNNER_PATH),
        "energyplus_executable_sha256": integrity.sha256_file(ENERGYPLUS_EXECUTABLE),
        "energyplus_executable": str(ENERGYPLUS_EXECUTABLE),
        "openstudio_version": openstudio.openStudioVersion(),
    }
    scenario_path = Path(parent["artifact_dir"]) / "scenario_settings.json"
    if scenario_path.exists():
        scenario = _read_json(scenario_path)
        provenance.update({
            "scenario_id": str(scenario.get("scenario_id", "")),
            "scenario_fingerprint": str(scenario.get("scenario_fingerprint", "")),
            "scenario_settings_sha256": integrity.sha256_file(scenario_path),
        })
    editor_path = Path(parent["artifact_dir"]) / "editor_provenance.json"
    if editor_path.exists():
        editor = _read_json(editor_path)
        provenance.update({
            "model_provenance": "authored",
            "authored_from": str(editor.get("authored_from", "")),
            "editor_adapter_sha256": str(editor.get("adapter_sha256", "")),
            "editor_patch_count": str(editor.get("patch_count", 0)),
        })
    return combined, provenance


def _part_g_settings(parent: dict[str, Any]) -> dict[str, Any] | None:
    path = Path(parent["artifact_dir"]) / "scenario_settings.json"
    if not path.exists():
        return None
    item = _read_json(path)
    return {
        "scenario_id": item.get("scenario_id"),
        "name": item.get("name"),
        "heat_delta_c": item.get("heat_delta_c", 0.0),
        "cool_delta_c": item.get("cool_delta_c", 0.0),
        "weather_snapshot_hash": item.get("weather_snapshot_hash"),
        "weather_source_name": item.get("weather_source_name"),
        "weather_changed": item.get("weather_changed", False),
        "reason": item.get("reason"),
        "source_type": item.get("source_type"),
        "source_ref": item.get("source_ref"),
    }


def eligible_models() -> list[dict[str, Any]]:
    output = []
    for run in db.list_runs_by_type("model"):
        if run["verification_status"] != "VERIFIED":
            continue
        try:
            parent = _verified_parent(run["id"])
            inputs = _parent_inputs(parent)
            model_settings = _model_settings(_parent_model_path(parent), parent)
        except (KeyError, OSError, ValueError):
            continue
        output.append({
            "id": parent["id"],
            "refparcela": parent["refparcela"],
            "scenario_name": parent["scenario_name"],
            "created_at": parent["created_at"],
            "verification_status": parent["verification_status"],
            "raw_model_sha256": parent["raw_model_sha256"],
            "canonical_fingerprint": parent["canonical_fingerprint"],
            "weather_snapshot_hash": inputs["weather"]["snapshot_hash"],
            "settings": model_settings,
            "provenance": parent.get("provenance", "pipeline"),
            "authored_from": parent.get("authored_from"),
            "patch_count": len(parent.get("patch_journal", [])),
        })
    return output


def create_simulation_job(parent_run_id: str) -> dict[str, Any]:
    simulation = capability_status()["capabilities"]["simulation"]
    if not simulation["runtime_ready"]:
        raise RuntimeError("Simulation capability is not ready")
    parent = _verified_parent(parent_run_id)
    _parent_inputs(parent)
    _model_settings(_parent_model_path(parent), parent)
    active = db.find_active_simulation_job(parent_run_id)
    if active:
        raise ValueError(f"Simulation job already active for parent: {active['id']}")
    payload = storage.reserve_payload("simulation", {"parent_run_id": parent_run_id})
    job_id = db.create_job(
        "simulation", parent["refparcela"], payload,
        timeout_seconds=600,
    )
    return db.get_job(job_id) or {"id": job_id, "status": "queued"}


def _latest_validated_simulation(parent_run_id: str) -> dict[str, Any] | None:
    """Return the newest reusable annual result for one exact model artifact."""
    for run in db.list_runs_by_type("simulation"):
        if run.get("parent_run_id") != parent_run_id:
            continue
        scientific = run.get("qa", {}).get("scientific_status") \
            or run.get("stats", {}).get("scientific_status")
        if run.get("verification_status") != "VERIFIED" or scientific != "VALIDATED":
            continue
        if service.verify_run_artifacts(run["id"])["status"] == "VERIFIED":
            return run
    return None


def _pair_leg(model_id: str) -> dict[str, Any]:
    completed = _latest_validated_simulation(model_id)
    if completed is not None:
        return {
            "model_id": model_id,
            "simulation_run_id": completed["id"],
            "job": None,
            "status": "completed",
        }
    active = db.find_active_simulation_job(model_id)
    job = active or create_simulation_job(model_id)
    return {
        "model_id": model_id,
        "simulation_run_id": None,
        "job": job,
        "status": "active" if active else "queued",
    }


def create_authored_simulation_pair(parent_run_id: str) -> dict[str, Any]:
    """Queue the auto-derived baseline before its authored override.

    Both legs use the unchanged Part B job path.  The orchestration only makes
    the editor's baseline relationship explicit and reproducible.
    """
    authored = _verified_parent(parent_run_id)
    baseline_model_id = str(authored.get("authored_from") or "")
    if authored.get("provenance") != "authored" or not baseline_model_id:
        raise ValueError("Paired simulation requires an authored model with an automatic baseline")
    baseline = _verified_parent(baseline_model_id)
    if str(baseline.get("refparcela")) != str(authored.get("refparcela")):
        raise ValueError("Authored model and automatic baseline must describe the same refparcela")
    # Creation order is deliberate: the single worker completes the untouched
    # auto-derived baseline before evaluating the authored override.
    baseline_leg = _pair_leg(baseline_model_id)
    authored_leg = _pair_leg(parent_run_id)
    return {
        "schema_version": 1,
        "refparcela": authored["refparcela"],
        "automatic_baseline_model_id": baseline_model_id,
        "authored_model_id": parent_run_id,
        "baseline": baseline_leg,
        "authored": authored_leg,
    }


def _restore_interrupted_commit(job_id: str) -> bool:
    run = next((item for item in db.list_runs_by_type("simulation") if item["job_id"] == job_id), None)
    if run is None:
        return False
    service.recover_committing_runs()
    recovered = db.get_run(run["id"])
    if recovered and recovered["verification_status"] == "VERIFIED":
        db.update_job(job_id, "completed", result_path=recovered["artifact_dir"])
        db.add_event(job_id, "Recovered immutable simulation commit", 1.0)
        return True
    raise RuntimeError("Interrupted simulation commit could not be recovered")


def run_simulation_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["kind"] != "simulation":
        raise ValueError(f"Unexpected job kind: {job['kind']}")
    if _restore_interrupted_commit(job_id):
        return
    parent_id = job["payload"]["parent_run_id"]
    scratch = service.RUN_ROOT / f".simulation-{job_id}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    db.add_event(job_id, "Parent Verification", 0.08)
    parent = _verified_parent(parent_id)
    parent_osm = _parent_model_path(parent)
    inputs = _parent_inputs(parent)
    model_settings = _model_settings(parent_osm, parent)
    combined_inputs, provenance = _source_provenance(parent, inputs)
    exact_parent = scratch / "parent_model.osm"
    shutil.copyfile(parent_osm, exact_parent)
    if integrity.sha256_file(exact_parent) != parent["raw_model_sha256"]:
        raise IOError("Byte-identical parent OSM copy verification failed")

    try:
        with tempfile.TemporaryDirectory(prefix="workbench-simulation-") as temp_name:
            temp_root = Path(temp_name)
            weather_path = _materialize_snapshot(
                inputs["weather"]["snapshot_hash"], temp_root / "weather",
            )
            settings = model_settings | {
                "parent_run_id": parent_id,
                "parent_model_sha256": parent["raw_model_sha256"],
                "parent_canonical_fingerprint": parent["canonical_fingerprint"],
                "weather_snapshot_hash": inputs["weather"]["snapshot_hash"],
                "cadastre_heating": _cadastre_heating(parent, inputs, temp_root),
                "provenance": provenance,
                "scenario": _part_g_settings(parent),
                "_weather_path": str(weather_path),
                "_parent_stats": parent["stats"],
                "_progress": lambda stage, value: db.add_event(job_id, stage, value),
            }
            result = run_simulation(exact_parent, scratch, settings)

        service.write_json(scratch / "input_manifest.json", combined_inputs)
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
            "schema_version": 2,
            "run_id": run_id,
            "job_id": job_id,
            "run_type": "simulation",
            "parent_run_id": parent_id,
            "refparcela": parent["refparcela"],
            "scientific_status": result["qa"]["scientific_status"],
            "raw_model_sha256": parent["raw_model_sha256"],
            "canonical_model_fingerprint": parent["canonical_fingerprint"],
            "input_snapshots": combined_inputs,
            "provenance": provenance,
            "artifacts": manifest_items,
        }
        service.write_json(staging / "manifest.json", manifest)
        manifest_sha256 = integrity.sha256_file(staging / "manifest.json")
        artifacts = service.artifact_manifest(staging)
        for item in artifacts:
            item["path"] = str(destination / item["name"])
        normalized = result.get("normalized_energy") or {}
        stats = {
            "scientific_status": result["qa"]["scientific_status"],
            "energy": normalized,
            "raw_energy": result.get("raw_energy"),
            "carbon": result.get("carbon"),
            "cadastre_heating": result.get("cadastre_heating"),
            "conditioned_residential_area_m2": model_settings["conditioned_residential_area_m2"],
            "warning_count": result["warnings"]["warnings"],
            "severe_count": result["warnings"]["severes"],
            "fatal_count": result["warnings"]["fatals"],
        }
        run_record = {
            "id": run_id,
            "job_id": job_id,
            "refparcela": parent["refparcela"],
            "scenario_name": f"{parent['scenario_name']} · annual EnergyPlus",
            "config": result["settings"],
            "stats": stats,
            "qa": result["qa"],
            "artifact_dir": str(staging),
            "run_type": "simulation",
            "parent_run_id": parent_id,
            "scenario_id": parent.get("scenario_id"),
            "verification_status": "COMMITTING",
            "raw_model_sha256": parent["raw_model_sha256"],
            "canonical_fingerprint": parent["canonical_fingerprint"],
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
        db.update_job(job_id, "completed", result_path=str(destination))
        db.add_event(job_id, "Finalize", 1.0)
    except Exception:
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
        raise


def list_simulations() -> list[dict[str, Any]]:
    return [simulation_detail(run["id"]) for run in db.list_runs_by_type("simulation")]


def simulation_detail(run_id: str) -> dict[str, Any]:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["run_type"] != "simulation":
        raise ValueError("Requested run is not a simulation")
    verification = service.verify_run_artifacts(run_id)
    root = Path(run["artifact_dir"])
    result_path = root / "results.json"
    result = _read_json(result_path) if result_path.exists() else None
    parent = db.get_run(run["parent_run_id"]) if run.get("parent_run_id") else None
    automatic_baseline = None
    if parent and parent.get("provenance") == "authored" and parent.get("authored_from"):
        baseline_model_id = str(parent["authored_from"])
        baseline_run = _latest_validated_simulation(baseline_model_id)
        baseline_job = db.find_active_simulation_job(baseline_model_id)
        automatic_baseline = {
            "model_id": baseline_model_id,
            "simulation_run_id": baseline_run["id"] if baseline_run else None,
            "job_id": baseline_job["id"] if baseline_job else None,
            "status": "completed" if baseline_run else baseline_job["status"] if baseline_job else "missing",
        }
    return run | {
        "verification": verification,
        "result": result,
        "parent": ({
            "id": parent["id"], "refparcela": parent["refparcela"],
            "scenario_name": parent["scenario_name"],
            "verification_status": parent["verification_status"],
            "provenance": parent.get("provenance", "pipeline"),
            "authored_from": parent.get("authored_from"),
        } if parent else None),
        "automatic_baseline": automatic_baseline,
    }


def compare_simulations(left_id: str, right_id: str) -> dict[str, Any]:
    if left_id == right_id:
        raise ValueError("A simulation cannot be compared with itself")
    left = simulation_detail(left_id)
    right = simulation_detail(right_id)
    left_result = left.get("result") or {}
    right_result = right.get("result") or {}
    keys = ("weather_snapshot_hash", "run_period", "area_basis", "energy_basis")
    same_basis = left["refparcela"] == right["refparcela"] and all(
        left_result.get("settings", {}).get(key) == right_result.get("settings", {}).get(key)
        for key in keys
    )
    metrics = {}
    for key in ("heating_kwh_m2", "cooling_kwh_m2", "heating_kwh", "cooling_kwh"):
        left_value = (left_result.get("normalized_energy") or {}).get(key)
        right_value = (right_result.get("normalized_energy") or {}).get(key)
        delta = right_value - left_value if left_value is not None and right_value is not None else None
        percent = delta / left_value * 100 if same_basis and delta is not None and left_value else None
        metrics[key] = {"left": left_value, "right": right_value, "delta": delta, "percent": percent}
    return {"left": left, "right": right, "same_basis": same_basis, "metrics": metrics}
