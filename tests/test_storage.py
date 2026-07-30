from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench import db, integrity, service, storage


def usage(total_gib: int, free_gib: int) -> SimpleNamespace:
    total = total_gib * storage.GIB
    free = free_gib * storage.GIB
    return SimpleNamespace(total=total, used=total - free, free=free)


def test_capacity_thresholds_distinguish_ready_warning_and_blocked():
    assert storage.capacity_status(usage(100, 30))["status"] == "READY"
    assert storage.capacity_status(usage(100, 10))["status"] == "WARNING"
    assert storage.capacity_status(usage(100, 2))["status"] == "BLOCKED"


def test_job_admission_preserves_floor_and_records_reservation(monkeypatch):
    monkeypatch.setattr(storage, "active_reservations", lambda **_kwargs: {
        "total_bytes": 0, "items": [],
    })
    allowed = storage.admission("city", usage=usage(100, 8))
    blocked = storage.admission("city", usage=usage(100, 5))
    assert allowed["allowed"] is True
    assert allowed["available_after_bytes"] == 5 * storage.GIB
    assert blocked["allowed"] is False
    assert blocked["reason"] == "protected_reserve_would_be_crossed"

    monkeypatch.setattr(storage, "admission", lambda *_args, **_kwargs: allowed)
    payload = storage.reserve_payload("city", {"scope": "Valencia"})
    assert payload["storage_reservation"]["bytes"] == 3 * storage.GIB
    assert payload["storage_reservation"]["policy_version"] == storage.POLICY_VERSION


def _set_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    roots = {
        "runs": tmp_path / "runs",
        "previews": tmp_path / "previews",
        "exports": tmp_path / "exports",
        "imports": tmp_path / "imports",
        "var": tmp_path / "var",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(service, "RUN_ROOT", roots["runs"])
    monkeypatch.setattr(service, "PREVIEW_ROOT", roots["previews"])
    monkeypatch.setattr(service, "EXPORT_ROOT", roots["exports"])
    monkeypatch.setattr(service, "IMPORT_ROOT", roots["imports"])
    monkeypatch.setattr(db, "VAR_DIR", roots["var"])
    monkeypatch.setattr(db, "list_runs", lambda: [])
    monkeypatch.setattr(db, "list_jobs_with_statuses", lambda _statuses: [])
    monkeypatch.setattr(db, "referenced_snapshot_hashes", lambda: set())
    return roots


def test_cleanup_plan_never_offers_immutable_or_fresh_work(tmp_path, monkeypatch):
    roots = _set_roots(tmp_path, monkeypatch)
    immutable = roots["runs"] / "verified-run"
    immutable.mkdir()
    (immutable / "manifest.json").write_text("immutable", encoding="utf-8")
    old_scratch = roots["runs"] / ".staging-orphan"
    old_scratch.mkdir()
    (old_scratch / "partial.bin").write_bytes(b"x" * 50)
    fresh_scratch = roots["runs"] / ".city-active-looking"
    fresh_scratch.mkdir()
    (fresh_scratch / "partial.bin").write_bytes(b"x" * 25)
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
    os.utime(old_scratch, (old, old))
    export = roots["exports"] / "verified.zip"
    export.write_bytes(b"zip-cache")
    cache = roots["var"] / "city_map_sources" / "fingerprint"
    cache.mkdir(parents=True)
    (cache / "tile.bin").write_bytes(b"tile")
    monkeypatch.setattr(db, "list_runs", lambda: [{"artifact_dir": str(immutable)}])

    plan = storage.build_cleanup_plan()
    paths = {item["path"] for item in plan["items"]}
    assert str(immutable.resolve()) not in paths
    assert str(fresh_scratch.resolve()) not in paths
    assert str(old_scratch.resolve()) in paths
    assert str(export.resolve()) in paths
    assert str(cache.resolve()) in paths

    result = storage.execute_cleanup(plan["plan_token"], ["export_cache", "map_cache"])
    assert result["deleted_count"] == 2
    assert immutable.exists()
    assert old_scratch.exists()
    assert not export.exists()
    assert not cache.exists()


def test_cleanup_rejects_stale_plan(tmp_path, monkeypatch):
    roots = _set_roots(tmp_path, monkeypatch)
    export = roots["exports"] / "one.zip"
    export.write_bytes(b"one")
    plan = storage.build_cleanup_plan()
    export.write_bytes(b"changed")
    with pytest.raises(ValueError, match="stale"):
        storage.execute_cleanup(plan["plan_token"], ["export_cache"])
    assert export.exists()


def test_ready_job_snapshot_is_reference_protected(monkeypatch):
    monkeypatch.setattr(db, "referenced_snapshot_hashes", lambda: {"dataset-hash"})
    monkeypatch.setattr(db, "list_jobs_with_statuses", lambda _statuses: [{
        "id": "ready-preview", "kind": "preview", "status": "ready",
        "payload": {"inputs": {"weather": {"snapshot_hash": "preview-hash"}}},
    }])
    assert storage._referenced_snapshot_hashes() == {"dataset-hash", "preview-hash"}


def test_cleanup_confirmation_starts_object_store_grace_without_deleting(tmp_path, monkeypatch):
    roots = _set_roots(tmp_path, monkeypatch)
    source = tmp_path / "unreferenced.txt"
    source.write_text("research input", encoding="utf-8")
    snapshot = integrity.ensure_snapshot(source, kind="source")
    plan = storage.build_cleanup_plan()
    assert plan["categories"]["object_store_gc"]["count"] == 1
    assert plan["pending_object_snapshots"] == 1

    result = storage.execute_cleanup(plan["plan_token"], ["object_store_gc"])
    assert result["object_store"]["deleted_snapshots"] == []
    assert snapshot["snapshot_hash"] in result["object_store"]["pending"]
    assert integrity.load_snapshot(snapshot["snapshot_hash"])["snapshot_hash"] == snapshot["snapshot_hash"]
    assert (roots["var"] / "objects/gc-candidates.json").exists()


def test_external_archive_copy_is_atomic_and_hash_verified(tmp_path, monkeypatch):
    _set_roots(tmp_path, monkeypatch)
    archive_root = tmp_path / "external" / "valencia-archive"
    monkeypatch.setenv("WORKBENCH_ARCHIVE_ROOT", str(archive_root))
    export = tmp_path / "verified-run.zip"
    export.write_bytes(b"signed immutable package")

    evidence = storage.archive_export("run-12345678", export)
    archived = Path(evidence["archive_path"])
    assert archived.exists()
    assert archived.read_bytes() == export.read_bytes()
    assert evidence["sha256"] == integrity.sha256_file(export)
    assert archived.with_suffix(".archive.json").exists()
    assert not list(archived.parent.glob(".run-12345678-*"))
