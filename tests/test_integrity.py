from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import zipfile

from workbench import db, integrity, service


def _isolate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "VAR_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "workbench.sqlite3")
    db.init_db()


def test_shapefile_sidecar_participates_in_snapshot(tmp_path):
    shp = tmp_path / "stock.shp"
    for suffix, content in {
        ".shp": b"geometry", ".dbf": b"attributes", ".shx": b"index",
        ".prj": b"projection", ".cpg": b"UTF-8",
    }.items():
        shp.with_suffix(suffix).write_bytes(content)
    first = integrity.snapshot_descriptor(shp, kind="gis")
    shp.with_suffix(".dbf").write_bytes(b"changed attributes")
    second = integrity.snapshot_descriptor(shp, kind="gis")
    assert first["snapshot_hash"] != second["snapshot_hash"]
    assert {item["name"] for item in second["components"]} >= {
        "stock.shp", "stock.dbf", "stock.shx", "stock.prj", "stock.cpg",
    }


def test_signed_manifest_and_private_key_permissions(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    manifest = {"run_id": "run-1", "files": [{"name": "model.osm", "sha256": "abc"}]}
    signature = integrity.sign_manifest(manifest)
    assert integrity.verify_signed_manifest(manifest, signature) is True
    assert integrity.verify_signed_manifest(manifest | {"run_id": "changed"}, signature) is False
    key = tmp_path / "keys/signing.key"
    assert key.exists()
    assert key.stat().st_mode & 0o777 == 0o600


def test_gc_waits_for_grace_and_preserves_shared_blob(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    one = tmp_path / "one.dat"
    two = tmp_path / "two.dat"
    one.write_bytes(b"shared content")
    two.write_bytes(b"shared content")
    retained = integrity.ensure_snapshot(one, kind="source")
    candidate = integrity.ensure_snapshot(two, kind="source")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = integrity.garbage_collect({retained["snapshot_hash"]}, now=start)
    assert candidate["snapshot_hash"] in first["pending"]
    second = integrity.garbage_collect(
        {retained["snapshot_hash"]}, now=start + timedelta(days=31),
    )
    assert candidate["snapshot_hash"] in second["deleted_snapshots"]
    assert integrity.snapshot_files(retained["snapshot_hash"])[0][1].exists()


def test_export_is_signed_and_never_contains_private_key(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "EXPORT_ROOT", tmp_path / "exports")
    service.EXPORT_ROOT.mkdir()
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    snapshot = integrity.ensure_snapshot(source, kind="source")
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "model_python.osm").write_text("model", encoding="utf-8")
    (run_root / "input_manifest.json").write_text(json.dumps({
        "source": {"snapshot_hash": snapshot["snapshot_hash"]},
    }), encoding="utf-8")
    (run_root / "manifest.json").write_text("{}", encoding="utf-8")
    artifacts = service.artifact_manifest(run_root)
    job_id = db.create_job("preview", "A", {"config": {}})
    db.insert_run({
        "id": "signed-run", "job_id": job_id, "refparcela": "A", "scenario_name": "Signed",
        "config": {}, "stats": {}, "qa": {}, "artifact_dir": str(run_root),
        "verification_status": "VERIFIED",
        "manifest_sha256": service.sha256_file(run_root / "manifest.json"),
    }, artifacts, {"source": snapshot["snapshot_hash"]})
    package = service.export_run("signed-run")
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert "export_manifest.json" in names
        assert "export_manifest.sig.json" in names
        assert "inputs/source/source.txt" in names
        assert not any(name.endswith("signing.key") for name in names)
        manifest = json.loads(archive.read("export_manifest.json"))
        signature = json.loads(archive.read("export_manifest.sig.json"))
    assert integrity.verify_signed_manifest(manifest, signature) is True
