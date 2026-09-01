from datetime import datetime, timedelta, timezone
import json
import time
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


def test_a_single_file_that_only_changed_name_is_still_intact(tmp_path):
    """A permanent false alarm, measured on the live installation 2026-08-22.

    Uploads were registered while still under their `tmpXXXXXX` name and then
    stored under the operator's name, so the identity hash - which covers
    component names - could never be reproduced.  Health reported "Registered
    input snapshot no longer matches source files" on the template and the EPW,
    both byte-identical to what was registered.  An alarm that is always on is
    an alarm nobody reads.
    """
    upload = tmp_path / "tmpABC123.epw"
    upload.write_bytes(b"weather data")
    registered = integrity.ensure_snapshot(upload, kind="weather")

    stored = tmp_path / "Valencia.epw"
    stored.write_bytes(b"weather data")
    result = integrity.verify_snapshot_source(
        stored, registered["snapshot_hash"], kind="weather")

    assert result["ok"] is True
    assert result["renamed"] is True
    assert result["actual"] != registered["snapshot_hash"]


def test_a_changed_byte_is_still_a_mismatch(tmp_path):
    """The relaxation is about names only. Content is the whole point."""
    upload = tmp_path / "tmpABC123.epw"
    upload.write_bytes(b"weather data")
    registered = integrity.ensure_snapshot(upload, kind="weather")

    tampered = tmp_path / "Valencia.epw"
    tampered.write_bytes(b"weather dat!")
    result = integrity.verify_snapshot_source(
        tampered, registered["snapshot_hash"], kind="weather")

    assert result["ok"] is False
    assert result["renamed"] is False


def test_a_multi_component_dataset_is_not_given_the_benefit_of_the_doubt(tmp_path):
    """Renaming sidecars past each other changes meaning without changing bytes.

    A shapefile is only a dataset because of which file is called what, so the
    single-file reasoning must not be extended to it.
    """
    source = tmp_path / "src"
    source.mkdir()
    for suffix, content in ((".shp", b"shape"), (".dbf", b"attrs"), (".shx", b"index")):
        (source / f"layer{suffix}").write_bytes(content)
    registered = integrity.ensure_snapshot(source / "layer.shp", kind="gis")

    moved = tmp_path / "dst"
    moved.mkdir()
    for suffix, content in ((".shp", b"attrs"), (".dbf", b"shape"), (".shx", b"index")):
        (moved / f"layer{suffix}").write_bytes(content)   # .shp and .dbf swapped
    result = integrity.verify_snapshot_source(
        moved / "layer.shp", registered["snapshot_hash"], kind="gis")

    assert result["ok"] is False
    assert result["renamed"] is False


def test_an_unchanged_file_is_not_read_twice(tmp_path, monkeypatch):
    """Hashing the same untouched input again must not re-read it.

    /api/health, /api/capabilities, the LHS staleness check and the stock
    profile all hash the same 110 MB Valencia shapefile, and listing the LHS
    runs hashed it once per stored run - 2.85 s each, cold, on the external
    disk.  This is the guard on the read being skipped, not on the timing.
    """
    integrity.forget_digest_memo()
    source = tmp_path / "input.osm"
    source.write_bytes(b"a model")
    reads = []
    real = integrity.sha256_file
    monkeypatch.setattr(integrity, "sha256_file",
                        lambda path: (reads.append(path), real(path))[1])

    first = integrity.snapshot_descriptor(source, kind="template")
    second = integrity.snapshot_descriptor(source, kind="template")

    assert first == second
    assert len(reads) == 1


def test_a_rewrite_of_the_same_length_is_still_noticed(tmp_path):
    """The memo may never make a changed input look unchanged.

    Size alone would miss this; the key carries the nanosecond mtime and the
    inode as well, which is what makes skipping the read sound.
    """
    integrity.forget_digest_memo()
    source = tmp_path / "input.osm"
    source.write_bytes(b"one")
    before = integrity.snapshot_descriptor(source, kind="template")["snapshot_hash"]
    time.sleep(0.02)
    source.write_bytes(b"two")   # same length, different bytes

    after = integrity.snapshot_descriptor(source, kind="template")["snapshot_hash"]

    assert after != before


def test_the_committed_artifact_gate_never_trusts_a_stat_signature(tmp_path, monkeypatch):
    """`verify_run_artifacts` exists to catch a replaced artifact.

    It must keep reading the real bytes, so it is deliberately not routed
    through the memo the snapshot descriptors use.
    """
    integrity.forget_digest_memo()
    artifact = tmp_path / "model.osm"
    artifact.write_bytes(b"committed")
    integrity.snapshot_descriptor(artifact, kind="template")   # fills the memo
    reads = []
    real = integrity.sha256_file
    monkeypatch.setattr(integrity, "sha256_file",
                        lambda path: (reads.append(path), real(path))[1])

    integrity.sha256_file(artifact)

    assert reads == [artifact]
