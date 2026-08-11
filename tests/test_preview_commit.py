from pathlib import Path
import time
import zipfile

import pytest

from workbench import db
from workbench import service
from workbench import renderer_provenance
from workbench import integrity
from workbench.jobs import JobManager

from conftest import provision_reference_city


@pytest.mark.integration
def test_preview_commit_keeps_exact_osm_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "workbench.sqlite3")
    monkeypatch.setattr(db, "VAR_DIR", tmp_path)
    monkeypatch.setattr(service, "PREVIEW_ROOT", tmp_path / "previews")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(service, "EXPORT_ROOT", tmp_path / "exports")
    # No city ships with the product, so this run provides its own.
    provision_reference_city()

    config = service.workbench_base_config()
    config.data.output_root = service.RUN_ROOT
    geometry = service.validate_geometry("4252702YJ2745A", config)
    actions = [{**item, "approved": True} for item in geometry["actions"]]
    job_id = db.create_job("preview", "4252702YJ2745A", {
        "building_ref": "4252702YJ2745A",
        "config": config.model_dump(mode="json"),
        "geometry_actions": actions,
    })
    db.update_job(job_id, "running")
    service.run_build_job(job_id)
    preview = Path(db.get_job(job_id)["result_path"])
    preview_hash = service.sha256_file(preview / "model_python.osm")
    renderer_manifest = renderer_provenance.load_renderer_manifest(
        preview / "renderer_manifest.json",
    )
    assert renderer_manifest["renderer_version"] == "site-viewer/1.0.0"
    assert renderer_manifest["visual_geometry"]["context_roof_count"] == 9
    assert renderer_manifest["visual_geometry"]["fingerprint"]
    assert renderer_manifest["sources"]

    changed_renderer = tmp_path / "changed-renderer.ts"
    changed_renderer.write_text("export const changed = true\n", encoding="utf-8")
    with monkeypatch.context() as stale:
        stale.setattr(renderer_provenance, "renderer_source_paths", lambda: [changed_renderer])
        with pytest.raises(ValueError, match="Renderer changed after preview"):
            service.commit_preview(job_id)
    assert Path(db.get_job(job_id)["result_path"]) == preview

    run = service.commit_preview(job_id)
    final_model = Path(run["artifact_dir"]) / "model_python.osm"
    assert service.sha256_file(final_model) == preview_hash
    assert run["qa"]["all_pass"] is True
    assert (Path(run["artifact_dir"]) / "input_manifest.json").exists()
    assert (Path(run["artifact_dir"]) / "geometry_actions.json").exists()
    assert (Path(run["artifact_dir"]) / "renderer_manifest.json").exists()
    assert db.get_job(job_id)["status"] == "completed"
    assert run["verification_status"] == "VERIFIED"
    assert run["raw_model_sha256"] == preview_hash
    assert run["canonical_fingerprint"]
    renderer_summary = service.run_with_renderer_summary(run)["renderer"]
    assert renderer_summary["status"] == "VERSIONED"
    assert renderer_summary["current_match"] is True

    exported = service.export_run(run["id"])
    with zipfile.ZipFile(exported) as archive:
        names = set(archive.namelist())
        assert "renderer_manifest.json" in names
        assert "renderer/source/frontend/src/components/ModelViewer.tsx" in names
        assert any(name.startswith("renderer/source/frontend/dist/assets/") for name in names)

    first_renderer_source = renderer_manifest["sources"][0]
    renderer_blob = integrity.snapshot_files(first_renderer_source["snapshot_hash"])[0][1]
    renderer_blob.chmod(0o644)
    renderer_blob.write_text("tampered renderer source", encoding="utf-8")
    verification = service.verify_run_artifacts(run["id"])
    assert verification["status"] == "TAMPERED"
    with pytest.raises(PermissionError, match="TAMPERED"):
        service.export_run(run["id"])


@pytest.mark.integration
def test_isolated_worker_auto_commit_reaches_verified_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "workbench.sqlite3")
    monkeypatch.setattr(db, "VAR_DIR", tmp_path)
    monkeypatch.setattr(service, "PREVIEW_ROOT", tmp_path / "previews")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(service, "EXPORT_ROOT", tmp_path / "exports")
    monkeypatch.setenv("WORKBENCH_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("WORKBENCH_DB_PATH", str(tmp_path / "workbench.sqlite3"))
    monkeypatch.setenv("WORKBENCH_PREVIEW_ROOT", str(tmp_path / "previews"))
    monkeypatch.setenv("WORKBENCH_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("WORKBENCH_EXPORT_ROOT", str(tmp_path / "exports"))
    # No city ships with the product, so this run provides its own.
    provision_reference_city()
    config = service.workbench_base_config()
    config.data.output_root = service.RUN_ROOT
    geometry = service.validate_geometry("4252702YJ2745A", config)
    actions = [{**item, "approved": True} for item in geometry["actions"]]
    job_id = db.create_job("batch", "4252702YJ2745A", {
        "building_ref": "4252702YJ2745A", "config": config.model_dump(mode="json"),
        "geometry_actions": actions,
    }, auto_commit=True, timeout_seconds=90)
    manager = JobManager()
    manager.start()
    manager.notify()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and db.get_job(job_id)["status"] not in {"completed", "failed"}:
        time.sleep(0.1)
    manager.stop()
    job = db.get_job(job_id)
    assert job["status"] == "completed", job["error"]
    runs = [item for item in db.list_runs() if item["job_id"] == job_id]
    assert len(runs) == 1
    assert runs[0]["verification_status"] == "VERIFIED"
