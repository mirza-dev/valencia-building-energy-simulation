"""Disk admission, storage inventory, and reference-safe cleanup services."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from workbench import db, integrity


GIB = 1024 ** 3
POLICY_VERSION = "2026-07-15.1"
WARNING_FREE_BYTES = int(os.environ.get("WORKBENCH_STORAGE_WARNING_BYTES", 20 * GIB))
BLOCKED_FREE_BYTES = int(os.environ.get("WORKBENCH_STORAGE_BLOCKED_BYTES", 3 * GIB))
RESERVE_FLOOR_BYTES = int(os.environ.get("WORKBENCH_STORAGE_RESERVE_BYTES", 3 * GIB))
WARNING_FREE_RATIO = float(os.environ.get("WORKBENCH_STORAGE_WARNING_RATIO", "0.10"))
BLOCKED_FREE_RATIO = float(os.environ.get("WORKBENCH_STORAGE_BLOCKED_RATIO", "0.02"))
SCRATCH_GRACE_HOURS = int(os.environ.get("WORKBENCH_SCRATCH_GRACE_HOURS", "24"))
PREVIEW_GRACE_DAYS = int(os.environ.get("WORKBENCH_PREVIEW_GRACE_DAYS", "7"))
OBJECT_GRACE_DAYS = int(os.environ.get("WORKBENCH_OBJECT_GRACE_DAYS", "30"))

JOB_ESTIMATES = {
    "preview": 256 * 1024 ** 2,
    "model_edit": 256 * 1024 ** 2,
    "batch": 256 * 1024 ** 2,
    "simulation": 1 * GIB,
    "scenario": 1536 * 1024 ** 2,
    "neighborhood": 2 * GIB,
    "city": 3 * GIB,
    "lhs": 2 * GIB,
    "lhs_event": 2 * GIB,
}
HEAVY_JOB_KINDS = frozenset(JOB_ESTIMATES)
SAFE_CLEANUP_CATEGORIES = frozenset({
    "abandoned_scratch", "expired_previews", "export_cache", "map_cache",
    "object_store_gc",
})


class StorageAdmissionError(RuntimeError):
    """Raised before a job can create artifacts without the protected reserve."""

    def __init__(self, message: str, evidence: dict[str, Any]):
        super().__init__(message)
        self.evidence = evidence


def _roots() -> dict[str, Path]:
    # Read service roots lazily so pytest monkeypatches and local env overrides are honored.
    from workbench import service

    return {
        "runs": service.RUN_ROOT,
        "previews": service.PREVIEW_ROOT,
        "exports": service.EXPORT_ROOT,
        "imports": service.IMPORT_ROOT,
        "objects": db.VAR_DIR / "objects",
        "cache": db.VAR_DIR / "cache",
        "city_map_sources": db.VAR_DIR / "city_map_sources",
        "job_logs": db.VAR_DIR / "job_logs",
        "backups": db.VAR_DIR / "backups",
        "model_edits": db.VAR_DIR / "model_edits",
    }


def _disk_root() -> Path:
    roots = _roots()
    for key in ("runs", "objects", "previews"):
        path = roots[key]
        if path.exists():
            return path
    return db.VAR_DIR


def _usage_dict(usage: Any | None = None) -> dict[str, Any]:
    usage = usage or shutil.disk_usage(_disk_root())
    free_ratio = usage.free / usage.total if usage.total else 0.0
    if usage.free < BLOCKED_FREE_BYTES or free_ratio < BLOCKED_FREE_RATIO:
        status = "BLOCKED"
    elif usage.free < WARNING_FREE_BYTES or free_ratio < WARNING_FREE_RATIO:
        status = "WARNING"
    else:
        status = "READY"
    return {
        "status": status,
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
        "free_ratio": free_ratio,
        "used_ratio": 1.0 - free_ratio,
        "warning_free_bytes": WARNING_FREE_BYTES,
        "blocked_free_bytes": BLOCKED_FREE_BYTES,
        "warning_free_ratio": WARNING_FREE_RATIO,
        "blocked_free_ratio": BLOCKED_FREE_RATIO,
        "reserve_floor_bytes": RESERVE_FLOOR_BYTES,
    }


def capacity_status(usage: Any | None = None) -> dict[str, Any]:
    """Return the cheap filesystem threshold check used by frequent health polling."""
    return _usage_dict(usage)


def estimate_bytes(kind: str, *, units: int = 1) -> int:
    if kind not in JOB_ESTIMATES:
        raise ValueError(f"Unknown storage job kind: {kind}")
    if units < 1:
        raise ValueError("Storage reservation units must be positive")
    return JOB_ESTIMATES[kind] * units


def _reservation_bytes(job: dict[str, Any]) -> int:
    payload = job.get("payload") or {}
    reservation = payload.get("storage_reservation") or {}
    value = reservation.get("bytes")
    if isinstance(value, int) and value >= 0:
        return value
    return JOB_ESTIMATES.get(str(job.get("kind")), 0)


def active_reservations(*, exclude_job_id: str | None = None) -> dict[str, Any]:
    jobs = [job for job in db.list_active_jobs() if job["id"] != exclude_job_id]
    items = [{
        "job_id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "bytes": _reservation_bytes(job),
    } for job in jobs]
    return {"total_bytes": sum(item["bytes"] for item in items), "items": items}


def admission(kind: str, *, units: int = 1, requested_bytes: int | None = None,
              exclude_job_id: str | None = None, usage: Any | None = None) -> dict[str, Any]:
    capacity = _usage_dict(usage)
    reservations = active_reservations(exclude_job_id=exclude_job_id)
    required = estimate_bytes(kind, units=units) if requested_bytes is None else int(requested_bytes)
    available_before = max(0, capacity["free_bytes"] - reservations["total_bytes"])
    available_after = available_before - required
    allowed = capacity["status"] != "BLOCKED" and available_after >= RESERVE_FLOOR_BYTES
    reason = None
    if capacity["status"] == "BLOCKED":
        reason = "filesystem_blocked"
    elif available_after < RESERVE_FLOOR_BYTES:
        reason = "protected_reserve_would_be_crossed"
    return {
        "allowed": allowed,
        "reason": reason,
        "kind": kind,
        "units": units,
        "estimated_bytes": required,
        "active_reserved_bytes": reservations["total_bytes"],
        "available_before_bytes": available_before,
        "available_after_bytes": available_after,
        "reserve_floor_bytes": RESERVE_FLOOR_BYTES,
        "capacity_status": capacity["status"],
        "policy_version": POLICY_VERSION,
    }


def reserve_payload(kind: str, payload: dict[str, Any], *, units: int = 1) -> dict[str, Any]:
    evidence = admission(kind, units=units)
    if not evidence["allowed"]:
        raise StorageAdmissionError(
            f"Insufficient protected disk capacity for {kind}: {evidence['reason']}",
            evidence,
        )
    return dict(payload) | {"storage_reservation": {
        "policy_version": POLICY_VERSION,
        "kind": kind,
        "bytes": evidence["estimated_bytes"],
        "free_bytes_at_admission": evidence["available_before_bytes"],
        "reserve_floor_bytes": RESERVE_FLOOR_BYTES,
    }}


def attach_batch_reservations(items: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    evidence = admission("batch", units=len(items))
    if not evidence["allowed"]:
        raise StorageAdmissionError(
            f"Insufficient protected disk capacity for batch: {evidence['reason']}",
            evidence,
        )
    per_job = estimate_bytes("batch")
    return [(ref, dict(payload) | {"storage_reservation": {
        "policy_version": POLICY_VERSION,
        "kind": "batch",
        "bytes": per_job,
        "free_bytes_at_admission": evidence["available_before_bytes"],
        "reserve_floor_bytes": RESERVE_FLOOR_BYTES,
        "batch_units": len(items),
    }}) for ref, payload in items]


def verify_worker_capacity(job: dict[str, Any]) -> dict[str, Any]:
    required = _reservation_bytes(job)
    evidence = admission(
        str(job["kind"]), requested_bytes=required, exclude_job_id=str(job["id"]),
    )
    if not evidence["allowed"]:
        raise StorageAdmissionError(
            f"Disk capacity changed before {job['kind']} worker start: {evidence['reason']}",
            evidence,
        )
    return evidence


def _measure(paths: Iterable[Path]) -> dict[str, int]:
    total = 0
    files = 0
    seen: set[tuple[int, int]] = set()
    for root in paths:
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except FileNotFoundError:
                continue
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            total += stat.st_size
            files += 1
    return {"size_bytes": total, "file_count": files}


def _known_run_paths() -> set[Path]:
    return {Path(run["artifact_dir"]).resolve() for run in db.list_runs()}


def _category_inventory() -> dict[str, dict[str, Any]]:
    roots = _roots()
    known_runs = _known_run_paths()
    scratch_paths: list[Path] = []
    untracked_paths: list[Path] = []
    if roots["runs"].exists():
        for path in roots["runs"].iterdir():
            resolved = path.resolve()
            if path.name.startswith("."):
                scratch_paths.append(path)
            elif resolved not in known_runs:
                untracked_paths.append(path)
    categories = {
        "immutable_runs": _measure(sorted(known_runs)),
        "scratch": _measure(scratch_paths),
        "untracked_run_data": _measure(untracked_paths),
        "previews": _measure([roots["previews"]]),
        "model_edit_sessions": _measure([roots["model_edits"]]),
        "exports": _measure([roots["exports"]]),
        "object_store": _measure([roots["objects"]]),
        "imports": _measure([roots["imports"]]),
        "map_cache": _measure([roots["cache"], roots["city_map_sources"]]),
        "job_logs": _measure([roots["job_logs"]]),
        "database_backups": _measure([roots["backups"]]),
    }
    for key, item in categories.items():
        item.update(id=key)
    return categories


def _archive_status() -> dict[str, Any]:
    raw = os.environ.get("WORKBENCH_ARCHIVE_ROOT", "").strip()
    if not raw:
        return {"configured": False, "path": None, "writable": False,
                "same_device": None, "free_bytes": None}
    path = Path(raw).expanduser().resolve()
    try:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        writable = (path.is_dir() and os.access(path, os.W_OK)) or (
            not path.exists() and probe.is_dir() and os.access(probe, os.W_OK)
        )
        roots = _roots()
        same_device = probe.stat().st_dev == roots["runs"].parent.stat().st_dev
        usage = shutil.disk_usage(probe)
        return {
            "configured": True, "path": str(path), "writable": writable,
            "same_device": same_device, "free_bytes": usage.free,
            "error": None,
        }
    except OSError as exc:
        return {
            "configured": True, "path": str(path), "writable": False,
            "same_device": None, "free_bytes": None, "error": str(exc),
        }


def storage_overview() -> dict[str, Any]:
    capacity = _usage_dict()
    reservations = active_reservations()
    admissions = {
        kind: admission(kind) for kind in JOB_ESTIMATES
    }
    categories = _category_inventory()
    cleanup = build_cleanup_plan()
    return {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "status": capacity["status"],
        "capacity": capacity,
        "reservations": reservations,
        "job_admissions": admissions,
        "categories": categories,
        "tracked_bytes": sum(item["size_bytes"] for item in categories.values()),
        "cleanup": {
            "candidate_count": cleanup["candidate_count"],
            "reclaimable_bytes": cleanup["reclaimable_bytes"],
            "pending_object_snapshots": cleanup["pending_object_snapshots"],
        },
        "archive": _archive_status(),
    }


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _path_candidate(path: Path, category: str, reason: str) -> dict[str, Any]:
    measured = _measure([path])
    return {
        "id": f"{category}:{path.resolve()}",
        "category": category,
        "path": str(path.resolve()),
        "size_bytes": measured["size_bytes"],
        "file_count": measured["file_count"],
        "modified_at": _iso(path.stat().st_mtime),
        "reason": reason,
    }


def _referenced_snapshot_hashes() -> set[str]:
    references = db.referenced_snapshot_hashes()
    for job in db.list_jobs_with_statuses(("queued", "running", "ready")):
        stack: list[Any] = [job.get("payload")]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "snapshot_hash" and isinstance(child, str):
                        references.add(child)
                    else:
                        stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)
    return references


def _object_gc_candidates(now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    root = db.VAR_DIR / "objects"
    manifest_root = root / "manifests"
    state_path = root / "gc-candidates.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    referenced = _referenced_snapshot_hashes()
    candidates: list[dict[str, Any]] = []
    pending: list[str] = []
    for path in sorted(manifest_root.glob("*.json")):
        snapshot_hash = path.stem
        if snapshot_hash in referenced:
            continue
        first_seen = state.get(snapshot_hash)
        if not first_seen:
            pending.append(snapshot_hash)
            continue
        eligible_at = datetime.fromisoformat(first_seen) + timedelta(days=OBJECT_GRACE_DAYS)
        if now < eligible_at:
            pending.append(snapshot_hash)
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        size = sum(int(item.get("size_bytes", 0)) for item in manifest.get("components", []))
        candidates.append({
            "id": f"object_store_gc:{snapshot_hash}",
            "category": "object_store_gc",
            "path": None,
            "snapshot_hash": snapshot_hash,
            "size_bytes": size,
            "file_count": len(manifest.get("components", [])) + 1,
            "modified_at": first_seen,
            "reason": "Unreferenced snapshot exceeded the 30-day GC grace period",
        })
    return candidates, pending


def _cleanup_candidates(now: datetime | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    now = now or datetime.now(timezone.utc)
    roots = _roots()
    protected_paths = _known_run_paths()
    protected_job_ids = {
        job["id"] for job in db.list_jobs_with_statuses(("queued", "running", "ready"))
    }
    candidates: list[dict[str, Any]] = []

    if roots["runs"].exists():
        for path in roots["runs"].iterdir():
            if not path.name.startswith(".") or path.resolve() in protected_paths:
                continue
            if any(job_id in path.name for job_id in protected_job_ids):
                continue
            age = now - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if age >= timedelta(hours=SCRATCH_GRACE_HOURS):
                candidates.append(_path_candidate(
                    path, "abandoned_scratch", "Unreferenced worker staging exceeded the safety grace period",
                ))

    terminal_preview_ids = {
        job["id"] for job in db.list_jobs_with_statuses(("completed", "failed", "canceled"))
        if job["kind"] == "preview"
    }
    if roots["previews"].exists():
        for path in roots["previews"].iterdir():
            age = now - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if path.name in terminal_preview_ids and age >= timedelta(days=PREVIEW_GRACE_DAYS):
                candidates.append(_path_candidate(
                    path, "expired_previews", "Terminal preview exceeded the retention period",
                ))

    for path in roots["exports"].iterdir() if roots["exports"].exists() else []:
        candidates.append(_path_candidate(
            path, "export_cache", "Signed export can be reproduced from its verified immutable run",
        ))
    for cache_root in (roots["cache"], roots["city_map_sources"]):
        for path in cache_root.iterdir() if cache_root.exists() else []:
            candidates.append(_path_candidate(
                path, "map_cache", "Derived map cache can be rebuilt from verified source snapshots",
            ))

    object_candidates, pending = _object_gc_candidates(now)
    candidates.extend(object_candidates)
    return sorted(candidates, key=lambda item: (item["category"], item["id"])), pending


def _plan_token(items: list[dict[str, Any]], pending_snapshots: list[str]) -> str:
    identity: dict[str, Any] = {"items": [{
        key: item.get(key) for key in (
            "id", "category", "path", "snapshot_hash", "size_bytes", "modified_at",
        )
    } for item in items], "pending_snapshots": sorted(pending_snapshots)}
    return hashlib.sha256(integrity.canonical_json_bytes(identity)).hexdigest()


def build_cleanup_plan() -> dict[str, Any]:
    items, pending = _cleanup_candidates()
    categories: dict[str, dict[str, int]] = {}
    for item in items:
        bucket = categories.setdefault(item["category"], {"count": 0, "size_bytes": 0})
        bucket["count"] += 1
        bucket["size_bytes"] += item["size_bytes"]
    if pending:
        bucket = categories.setdefault("object_store_gc", {"count": 0, "size_bytes": 0})
        bucket["count"] += len(pending)
    return {
        "schema_version": 1,
        "plan_token": _plan_token(items, pending),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(items) + len(pending),
        "reclaimable_bytes": sum(item["size_bytes"] for item in items),
        "pending_object_snapshots": len(pending),
        "categories": categories,
        "items": items,
        "protected": {
            "immutable_runs": len(db.list_runs()),
            "active_or_ready_jobs": len(db.list_jobs_with_statuses(("queued", "running", "ready"))),
            "referenced_snapshots": len(_referenced_snapshot_hashes()),
        },
    }


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _delete_candidate(item: dict[str, Any]) -> None:
    path_value = item.get("path")
    if not path_value:
        return
    path = Path(path_value)
    roots = _roots()
    allowed_roots = {
        "abandoned_scratch": roots["runs"],
        "expired_previews": roots["previews"],
        "export_cache": roots["exports"],
        "map_cache": roots["cache"] if _within(path, roots["cache"]) else roots["city_map_sources"],
    }
    root = allowed_roots.get(item["category"])
    if root is None or not _within(path, root) or path.resolve() == root.resolve():
        raise PermissionError(f"Cleanup candidate escaped its managed root: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def execute_cleanup(plan_token: str, categories: list[str]) -> dict[str, Any]:
    requested = set(categories)
    if not requested or not requested <= SAFE_CLEANUP_CATEGORIES:
        raise ValueError("Cleanup categories are empty or unsupported")
    plan = build_cleanup_plan()
    if plan["plan_token"] != plan_token:
        raise ValueError("Cleanup plan is stale; review a fresh plan before confirming")
    selected = [item for item in plan["items"] if item["category"] in requested]
    estimated = sum(item["size_bytes"] for item in selected)
    for item in selected:
        if item["category"] != "object_store_gc":
            _delete_candidate(item)
    gc_result = {"deleted_snapshots": [], "deleted_blobs": [], "pending": []}
    if "object_store_gc" in requested:
        gc_result = integrity.garbage_collect(
            _referenced_snapshot_hashes(), grace_days=OBJECT_GRACE_DAYS,
        )
    return {
        "deleted_count": len([item for item in selected if item["category"] != "object_store_gc"])
        + len(gc_result["deleted_snapshots"]),
        "estimated_freed_bytes": estimated,
        "categories": sorted(requested),
        "object_store": gc_result,
        "storage": storage_overview(),
    }


def archive_export(run_id: str, export_path: Path) -> dict[str, Any]:
    raw = os.environ.get("WORKBENCH_ARCHIVE_ROOT", "").strip()
    if not raw:
        raise RuntimeError("WORKBENCH_ARCHIVE_ROOT is not configured as a writable archive")
    Path(raw).expanduser().mkdir(parents=True, exist_ok=True)
    status = _archive_status()
    if not status["configured"] or not status["writable"]:
        raise RuntimeError("WORKBENCH_ARCHIVE_ROOT is not configured as a writable archive")
    root = Path(str(status["path"])) / "signed-run-exports"
    root.mkdir(parents=True, exist_ok=True)
    digest = integrity.sha256_file(export_path)
    destination = root / f"{run_id}-{digest[:12]}.zip"
    if destination.exists():
        if integrity.sha256_file(destination) != digest:
            raise IOError("Existing archive copy has an unexpected hash")
    else:
        with tempfile.NamedTemporaryFile(dir=root, prefix=f".{run_id}-", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copy2(export_path, temporary)
            if integrity.sha256_file(temporary) != digest:
                raise IOError("Archive copy hash verification failed")
            temporary.replace(destination)
            destination.chmod(0o444)
        finally:
            temporary.unlink(missing_ok=True)
    evidence = {
        "schema_version": 1,
        "run_id": run_id,
        "source_export": export_path.name,
        "archive_path": str(destination),
        "sha256": digest,
        "size_bytes": destination.stat().st_size,
        "archived_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest = destination.with_suffix(".archive.json")
    temporary_manifest = manifest.with_suffix(".tmp")
    temporary_manifest.write_bytes(integrity.canonical_json_bytes(evidence))
    temporary_manifest.replace(manifest)
    return evidence
