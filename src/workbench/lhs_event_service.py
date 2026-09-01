"""Immutable job, artifact and comparison service for the event uncertainty study.

Mirrors `lhs_service.py` line for line in shape, but commits under
`run_type='lhs_event'`.  That separation is the whole safety argument: the
annual list, its `[0]` selection on the product surface and its
`current_compatibility` chain never see an event study, so publishing one here
cannot regress the Valencia band.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from workbench import db, integrity, service, storage, stock_adapter
from workbench.capabilities import capability_status
from workbench.lhs_event_adapter import (
    OUTPUT_COLUMNS, event_runs, inspect, run_event_lhs, source_paths,
)

RUN_TYPE = "lhs_event"
JOB_KIND = "lhs_event"

FIGURE_NAMES = {"histograms.png", "tornado.png"}
DOWNLOADABLE = {
    "runs.csv": "text/csv",
    "samples.csv": "text/csv",
    "histograms.png": "image/png",
    "tornado.png": "image/png",
    "summary.txt": "text/plain; charset=utf-8",
}


def _require_ready() -> dict[str, Any]:
    """The event study runs the deep chain, so it needs the simulation capability.

    No eighth capability is registered: the study's scientific integrity is
    already carried by `assert_profile_intact()` on every sample -- against the
    same four locked source hashes -- plus its own input snapshots and
    `current_compatibility`.
    """
    capability = capability_status()["capabilities"].get("simulation")
    if capability is None or not capability["runtime_ready"]:
        reason = "simulation capability is not registered"
        if capability is not None:
            reason = (capability.get("contract", {}) or {}).get("reason") \
                or capability["inspection"].get("reason") or "contract_failed"
        raise RuntimeError(f"Event LHS cannot run: {reason}")
    return capability


def event_lhs_preflight(stock_run: str, refparcela: str) -> dict[str, Any]:
    _require_ready()
    result = inspect(stock_run, refparcela)
    result["available_runs"] = event_runs()
    return result


def create_event_lhs_job(stock_run: str, refparcela: str,
                         n: int | None = None, seed: int | None = None) -> dict[str, Any]:
    _require_ready()
    active = db.find_current_job(JOB_KIND)
    if active:
        raise ValueError(f"Event LHS job already active: {active['id']}")
    plan = inspect(stock_run, refparcela)
    if stock_adapter.active_process(stock_run) is not None:
        raise ValueError(f"{stock_run} is still running; its inputs are not final yet")
    payload = {
        "stock_run": stock_run,
        "refparcela": refparcela,
        "run_mode": "microclimate_event",
        "method": "latin_hypercube_uniform_spearman",
        "n": int(n if n is not None else plan["settings"]["n"]),
        "seed": int(seed if seed is not None else plan["settings"]["seed"]),
        "workers": int(plan["settings"]["workers"]),
        "event_days": plan["event"]["days"],
        "event_window": plan["event"]["window_label"],
        "slice_fingerprint": plan["event"]["slice_fingerprint"],
        "climate_fingerprint": plan["event"]["climate_fingerprint"],
        "template_fingerprint": plan["event"]["template_fingerprint"],
    }
    payload = storage.reserve_payload(JOB_KIND, payload)
    job_id = db.create_job(JOB_KIND, refparcela, payload, timeout_seconds=3600)
    return db.get_job(job_id) or {"id": job_id, "status": "queued"}


def _run_source_paths(stock_run: str) -> dict[str, tuple[Path, str]]:
    """Code, plus the four inputs this particular stock run was built from.

    The run's own inputs are pinned per run rather than globally because they
    differ between runs; pinning them is what makes a stale study say so instead
    of quietly presenting a band built on a replaced climate bundle.
    """
    paths = dict(source_paths())
    import lhs_event_study as study

    config = json.loads((stock_adapter.run_directory(stock_run) / "run_config.json").read_text())
    worker = config.get("worker_config", {})
    micro = worker.get("microclimate") or {}
    climate_path, _ = study.resolve_climate(config["climate"]["fingerprint"])
    template_path, _ = study.resolve_template(config["template"]["fingerprint"])
    slice_dir, _ = study.resolve_slice(micro["slice"]["fingerprint"])
    paths["prepared_stock"] = (Path(worker["prepared_gis"]), "gis")
    paths["climate_bundle"] = (climate_path, "climate")
    paths["template"] = (template_path, "template")
    # The slice root holds no file of its own; `load_slice` finds `meta.json`
    # one level down.  Pin the file that actually exists rather than a path that
    # merely looks canonical.
    meta = next(iter(sorted(Path(slice_dir).rglob("meta.json"))), None)
    if meta is None:
        raise FileNotFoundError(f"the slice at {slice_dir} carries no meta.json")
    paths["microclimate_slice"] = (meta, "microclimate")
    return paths


def _snapshot_inputs(stock_run: str) -> tuple[dict[str, Any], dict[str, str]]:
    manifest: dict[str, Any] = {}
    refs: dict[str, str] = {}
    for role, (path, kind) in _run_source_paths(stock_run).items():
        snapshot = integrity.ensure_snapshot(path, kind=kind)
        manifest[role] = {
            "snapshot_hash": snapshot["snapshot_hash"],
            "source_name": snapshot["source_name"],
            "kind": kind,
            "components": snapshot["components"],
        }
        refs[role] = snapshot["snapshot_hash"]
    return manifest, refs


def _verify_inputs_unchanged(stock_run: str, input_manifest: dict[str, Any]) -> None:
    for role, (path, kind) in _run_source_paths(stock_run).items():
        current = integrity.snapshot_descriptor(path, kind=kind)["snapshot_hash"]
        if current != input_manifest[role]["snapshot_hash"]:
            raise IOError(f"Event LHS input changed during the run: {role}")


def _restore_interrupted_commit(job_id: str) -> bool:
    run = next((item for item in db.list_runs_by_type(RUN_TYPE) if item["job_id"] == job_id), None)
    if run is None:
        return False
    service.recover_committing_runs()
    recovered = db.get_run(run["id"])
    if recovered and recovered["verification_status"] == "VERIFIED":
        db.update_job(job_id, "completed", result_path=recovered["artifact_dir"])
        db.add_event(job_id, "Recovered immutable event LHS commit", 1.0)
        return True
    raise RuntimeError("Interrupted event LHS commit could not be recovered")


def run_event_lhs_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["kind"] != JOB_KIND:
        raise ValueError(f"Unexpected job kind: {job['kind']}")
    if _restore_interrupted_commit(job_id):
        return
    _require_ready()
    stock_run = job["payload"]["stock_run"]
    refparcela = job["payload"]["refparcela"]
    scratch = service.RUN_ROOT / f".lhs-event-{job_id}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    staging: Path | None = None
    run_inserted = False

    try:
        db.add_event(job_id, "Input snapshots", 0.02)
        input_manifest, snapshot_refs = _snapshot_inputs(stock_run)
        settings = job["payload"] | {
            "input_snapshot_hashes": {role: item["snapshot_hash"] for role, item in input_manifest.items()},
            "_progress": lambda stage, value: db.add_event(job_id, stage, value),
        }
        result = run_event_lhs(scratch, settings)
        _verify_inputs_unchanged(stock_run, input_manifest)
        service.write_json(scratch / "input_manifest.json", input_manifest)
        service.write_json(scratch / "environment.json", integrity.environment_manifest())
        # Per-sample scratch is already removed by the study; the shared weather
        # cache is not evidence and must not enter the signed manifest.
        shutil.rmtree(scratch / "event_epw", ignore_errors=True)

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
            "run_type": RUN_TYPE,
            "scope": refparcela,
            "stock_run": stock_run,
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
        summary = result["summary"]
        run_record = {
            "id": run_id,
            "job_id": job_id,
            "refparcela": refparcela,
            "scenario_name": (f"{stock_run} · {refparcela} · event LHS "
                              f"N={result['settings']['n']} seed={result['settings']['seed']}"),
            "config": result["settings"],
            "stats": summary,
            "qa": result["qa"],
            "artifact_dir": str(staging),
            "run_type": RUN_TYPE,
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


def _current_input_compatibility(result: dict[str, Any] | None) -> dict[str, Any]:
    """Artifact integrity and "is this today's baseline" stay separate.

    A committed study stays VERIFIED forever.  It must not, however, be shown as
    current after the code or any of the run's four inputs has moved.
    """
    settings = (result or {}).get("settings") or {}
    stored = settings.get("input_snapshot_hashes") or {}
    if not stored:
        return {"current": False, "changed_roles": ["input_snapshot_hashes"]}
    stock_run = settings.get("stock_run")
    try:
        roles = _run_source_paths(str(stock_run))
    except Exception:
        return {"current": False, "changed_roles": ["stock_run"]}
    changed: list[str] = []
    for role, (path, kind) in roles.items():
        try:
            current = integrity.snapshot_descriptor(path, kind=kind)["snapshot_hash"]
        except (FileNotFoundError, OSError, ValueError):
            changed.append(role)
            continue
        if stored.get(role) != current:
            changed.append(role)
    return {"current": not changed, "changed_roles": changed}


def event_lhs_detail(run_id: str) -> dict[str, Any]:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["run_type"] != RUN_TYPE:
        raise ValueError("Requested run is not an event LHS run")
    verification = service.verify_run_artifacts(run_id)
    run = db.get_run(run_id) or run
    result_path = Path(run["artifact_dir"]) / "results.json"
    result = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if verification["ok"] and result_path.exists() else None
    )
    return run | {
        "result": result,
        "verification": verification,
        "current_compatibility": _current_input_compatibility(result),
    }


def list_event_lhs_runs(stock_run: str | None = None) -> list[dict[str, Any]]:
    runs = [event_lhs_detail(run["id"]) for run in db.list_runs_by_type(RUN_TYPE)]
    if stock_run is None:
        return runs
    return [
        run for run in runs
        if ((run.get("config") or {}).get("stock_run") == stock_run)
    ]


def _verified_artifact(run_id: str, name: str, kind: str) -> Path:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["run_type"] != RUN_TYPE:
        raise ValueError("Requested run is not an event LHS run")
    verification = service.verify_run_artifacts(run_id)
    if not verification["ok"]:
        raise PermissionError(f"Event LHS {kind} is not verified: {verification['status']}")
    path = Path(run["artifact_dir"]) / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def event_lhs_figure(run_id: str, name: str) -> Path:
    if name not in FIGURE_NAMES:
        raise ValueError("Unsupported event LHS figure")
    return _verified_artifact(run_id, name, "figure")


def event_lhs_artifact(run_id: str, name: str) -> tuple[Path, str]:
    if name not in DOWNLOADABLE:
        raise ValueError("Unsupported event LHS artifact")
    return _verified_artifact(run_id, name, "artifact"), DOWNLOADABLE[name]
