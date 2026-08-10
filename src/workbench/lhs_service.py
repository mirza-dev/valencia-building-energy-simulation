"""Immutable job, artifact and comparison service for the LHS study."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from workbench import db, integrity, service, storage
from workbench.capabilities import capability_status
from workbench.lhs_adapter import OUTPUT_COLUMNS, inspect_lhs, run_lhs, source_paths


FIGURE_NAMES = {"histograms.png", "tornado.png"}


def _require_ready() -> dict[str, Any]:
    capability = capability_status()["capabilities"]["lhs"]
    if not capability["runtime_ready"]:
        reason = capability.get("contract", {}).get("reason") or capability["inspection"].get("reason")
        raise RuntimeError(f"LHS capability is not ready: {reason or 'contract_failed'}")
    return capability


def lhs_preflight() -> dict[str, Any]:
    capability = _require_ready()
    result = inspect_lhs()
    expected = (capability.get("contract") or {}).get("expected") or {}
    result["accepted_reference"] = {
        "n": expected.get("n"),
        "seed": expected.get("seed"),
        "statistics": expected.get("statistics") or {},
    }
    result["capability"] = {
        "version": capability.get("version"),
        "runner_sha256": capability.get("contract", {}).get("runner_sha256"),
        "adapter_sha256": capability["inspection"].get("sha256"),
    }
    return result


def create_lhs_job() -> dict[str, Any]:
    capability = _require_ready()
    active = db.find_current_job("lhs")
    if active:
        raise ValueError(f"LHS job already active: {active['id']}")
    contract = capability.get("contract") or {}
    expected = contract.get("expected") or {}
    payload = {
        "scope": "4252702YJ2745A",
        "run_mode": "frozen_baseline",
        "method": "latin_hypercube_uniform_spearman",
        "n": int(expected.get("n", 50)),
        "seed": int(expected.get("seed", 42)),
        "capability_version": capability.get("version"),
    }
    payload = storage.reserve_payload("lhs", payload)
    job_id = db.create_job("lhs", "4252702YJ2745A", payload, timeout_seconds=2400)
    return db.get_job(job_id) or {"id": job_id, "status": "queued"}


def _snapshot_inputs() -> tuple[dict[str, Any], dict[str, str]]:
    manifest: dict[str, Any] = {}
    refs: dict[str, str] = {}
    for role, (path, kind) in source_paths().items():
        snapshot = integrity.ensure_snapshot(path, kind=kind)
        manifest[role] = {
            "snapshot_hash": snapshot["snapshot_hash"],
            "source_name": snapshot["source_name"],
            "kind": kind,
            "components": snapshot["components"],
        }
        refs[role] = snapshot["snapshot_hash"]
    return manifest, refs


def _verify_inputs_unchanged(input_manifest: dict[str, Any]) -> None:
    for role, (path, kind) in source_paths().items():
        current = integrity.snapshot_descriptor(path, kind=kind)["snapshot_hash"]
        if current != input_manifest[role]["snapshot_hash"]:
            raise IOError(f"LHS input changed during the run: {role}")


def _restore_interrupted_commit(job_id: str) -> bool:
    run = next((item for item in db.list_runs_by_type("lhs") if item["job_id"] == job_id), None)
    if run is None:
        return False
    service.recover_committing_runs()
    recovered = db.get_run(run["id"])
    if recovered and recovered["verification_status"] == "VERIFIED":
        db.update_job(job_id, "completed", result_path=recovered["artifact_dir"])
        db.add_event(job_id, "Recovered immutable LHS commit", 1.0)
        return True
    raise RuntimeError("Interrupted LHS commit could not be recovered")


def run_lhs_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["kind"] != "lhs":
        raise ValueError(f"Unexpected job kind: {job['kind']}")
    if _restore_interrupted_commit(job_id):
        return
    _require_ready()
    scratch = service.RUN_ROOT / f".lhs-{job_id}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    staging: Path | None = None
    run_inserted = False

    try:
        db.add_event(job_id, "Input snapshots", 0.02)
        input_manifest, snapshot_refs = _snapshot_inputs()
        settings = job["payload"] | {
            "input_snapshot_hashes": {role: item["snapshot_hash"] for role, item in input_manifest.items()},
            "_progress": lambda stage, value: db.add_event(job_id, stage, value),
        }
        result = run_lhs(scratch, settings)
        _verify_inputs_unchanged(input_manifest)
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
            "schema_version": 2,
            "run_id": run_id,
            "job_id": job_id,
            "run_type": "lhs",
            "scope": "4252702YJ2745A",
            "scientific_status": result["qa"]["scientific_status"],
            "variable_fingerprint": result["variable_fingerprint"],
            "input_snapshots": input_manifest,
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
            "refparcela": "4252702YJ2745A",
            "scenario_name": "Pilot · frozen LHS N=50 seed=42",
            "config": result["settings"],
            "stats": result["summary"],
            "qa": result["qa"],
            "artifact_dir": str(staging),
            "run_type": "lhs",
            "verification_status": "COMMITTING",
            "manifest_sha256": manifest_sha256,
        }
        db.insert_run(run_record, artifacts, snapshot_refs)
        run_inserted = True
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
        if staging is not None and staging.exists() and not run_inserted:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def lhs_detail(run_id: str) -> dict[str, Any]:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["run_type"] != "lhs":
        raise ValueError("Requested run is not an LHS run")
    verification = service.verify_run_artifacts(run_id)
    run = db.get_run(run_id) or run
    result_path = Path(run["artifact_dir"]) / "results.json"
    result = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if verification["ok"] and result_path.exists() else None
    )
    compatibility = _current_input_compatibility(result)
    return run | {
        "result": result,
        "verification": verification,
        "current_compatibility": compatibility,
    }


def _current_input_compatibility(result: dict[str, Any] | None) -> dict[str, Any]:
    """Keep artifact integrity separate from whether a run is today's baseline.

    Historical LHS artifacts remain immutable and VERIFIED.  They must not,
    however, be presented as the current accepted baseline after any snapshotted
    runner, model, weather or input has changed.
    """
    stored = ((result or {}).get("settings") or {}).get("input_snapshot_hashes") or {}
    if not stored:
        return {"current": False, "changed_roles": ["input_snapshot_hashes"]}
    changed: list[str] = []
    for role, (path, kind) in source_paths().items():
        try:
            current = integrity.snapshot_descriptor(path, kind=kind)["snapshot_hash"]
        except (FileNotFoundError, OSError, ValueError):
            changed.append(role)
            continue
        if stored.get(role) != current:
            changed.append(role)
    return {"current": not changed, "changed_roles": changed}


def list_lhs_runs() -> list[dict[str, Any]]:
    return [lhs_detail(run["id"]) for run in db.list_runs_by_type("lhs")]


def lhs_figure(run_id: str, name: str) -> Path:
    if name not in FIGURE_NAMES:
        raise ValueError("Unsupported LHS figure")
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["run_type"] != "lhs":
        raise ValueError("Requested run is not an LHS run")
    verification = service.verify_run_artifacts(run_id)
    if not verification["ok"]:
        raise PermissionError(f"LHS figure is not verified: {verification['status']}")
    path = Path(run["artifact_dir"]) / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def compare_lhs(left_id: str, right_id: str) -> dict[str, Any]:
    if left_id == right_id:
        raise ValueError("Select two different LHS runs")
    left = lhs_detail(left_id)
    right = lhs_detail(right_id)
    left_result = left.get("result") or {}
    right_result = right.get("result") or {}
    left_settings = left_result.get("settings", {})
    right_settings = right_result.get("settings", {})
    protocol_keys = ("scope", "run_mode", "method", "n", "seed")
    comparable = (
        left["verification_status"] == "VERIFIED"
        and right["verification_status"] == "VERIFIED"
        and left_result.get("qa", {}).get("scientific_status") == "VALIDATED"
        and right_result.get("qa", {}).get("scientific_status") == "VALIDATED"
        and left_result.get("variable_fingerprint") == right_result.get("variable_fingerprint")
        and all(left_settings.get(key) == right_settings.get(key) for key in protocol_keys)
        and left_settings.get("input_snapshot_hashes") == right_settings.get("input_snapshot_hashes")
    )
    rows = []
    left_stats = left_result.get("summary", {}).get("statistics", {})
    right_stats = right_result.get("summary", {}).get("statistics", {})
    for output in OUTPUT_COLUMNS:
        for statistic in ("mean", "median", "p5", "p95"):
            left_value = left_stats.get(output, {}).get(statistic)
            right_value = right_stats.get(output, {}).get(statistic)
            if left_value is None or right_value is None:
                continue
            delta = float(right_value) - float(left_value)
            rows.append({
                "output": output,
                "statistic": statistic,
                "left": float(left_value),
                "right": float(right_value),
                "delta": delta,
                "percent": delta / float(left_value) * 100 if comparable and float(left_value) else None,
            })
    return {
        "left_run_id": left_id,
        "right_run_id": right_id,
        "comparable": comparable,
        "reason": None if comparable else "variable_register_or_settings_differ",
        "rows": rows,
    }
