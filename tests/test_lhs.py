from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import lhs_study as domain_lhs
from workbench import api as api_module
from workbench import capabilities, db, service
from workbench import lhs_adapter as adapter
from workbench import lhs_service


PROJECT = Path(__file__).resolve().parents[1]


def test_lhs_has_no_unconfirmed_cooling_reference() -> None:
    assert domain_lhs.CADASTRE == {"heating": 27.97}


def _contract_manifest(tmp_path: Path, monkeypatch, runner_source: str) -> dict:
    runner = tmp_path / "lhs_study.py"
    runner.write_text(runner_source, encoding="utf-8")
    fixture = json.loads((PROJECT / "tests/fixtures/lhs_smoke.json").read_text())
    fixture["runner"] = str(runner)
    fixture["runner_sha256"] = hashlib.sha256(runner.read_bytes()).hexdigest()
    builder = tmp_path / "model_builder.py"
    builder.write_text("# builder dependency fixture\n", encoding="utf-8")
    fixture["builder"] = str(builder)
    fixture["builder_sha256"] = hashlib.sha256(builder.read_bytes()).hexdigest()
    simulation_runner = tmp_path / "run_simulation.py"
    simulation_runner.write_text("# simulation dependency fixture\n", encoding="utf-8")
    fixture["simulation_runner"] = str(simulation_runner)
    fixture["simulation_runner_sha256"] = hashlib.sha256(simulation_runner.read_bytes()).hexdigest()
    fixture_path = tmp_path / "lhs.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    manifest = json.loads((PROJECT / "workbench-capabilities.json").read_text())
    manifest["capabilities"]["lhs"]["fixture"] = str(fixture_path)
    manifest_path = tmp_path / "capabilities.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(capabilities, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(capabilities, "_simulation_contract", lambda *_args, **_kwargs: {"ok": True})
    return capabilities.capability_status()["capabilities"]["lhs"]


def test_lhs_capability_requires_runner_hash_and_callable_contract(tmp_path, monkeypatch):
    source = "\n".join(f"def {name}():\n    pass\n" for name in adapter.REQUIRED_ENTRYPOINTS)
    status = _contract_manifest(tmp_path, monkeypatch, source)
    assert status["runtime_ready"] is True
    assert all(status["contract"]["checks"].values())

    Path(status["contract"]["builder"]).write_text("# changed builder\n", encoding="utf-8")
    changed_dependency = capabilities.capability_status()["capabilities"]["lhs"]
    assert changed_dependency["runtime_ready"] is False
    assert changed_dependency["contract"]["checks"]["builder_hash"] is False

    status = _contract_manifest(tmp_path, monkeypatch, "def sample_matrix(:\n    pass")
    assert status["runtime_ready"] is False
    assert status["contract"]["checks"]["runner_functions"] is False

    runner = Path(status["contract"]["runner"])
    runner.write_text("def sample_matrix():\n    pass\n", encoding="utf-8")
    changed = capabilities.capability_status()["capabilities"]["lhs"]
    assert changed["runtime_ready"] is False
    assert changed["contract"]["checks"]["runner_hash"] is False


def _fake_lhs() -> SimpleNamespace:
    variables = {
        "wall_u": (1.0, 2.0), "roof_u": (1.0, 2.0), "window_u": (4.0, 6.0),
        "window_g": (0.6, 0.9), "infiltration_ach": (0.1, 0.5),
        "shade_setpoint": (100.0, 400.0), "thermal_bridge_du": (0.0, 0.2),
        "cop": (1.0, 3.0), "seer": (1.8, 3.5), "emission_factor": (0.15, 0.33),
    }
    simulation = dict(list(variables.items())[:7])
    post = dict(list(variables.items())[7:])

    def sample_matrix(n, _seed):
        return pd.DataFrame({
            name: [low + (high - low) * 0.25, low + (high - low) * 0.75]
            for name, (low, high) in variables.items()
        }).iloc[:n]

    def run_study(samples, _out_dir):
        rows = samples.copy()
        rows["heating_kwh_m2"] = [12.0, 20.0]
        rows["cooling_kwh_m2"] = [14.0, 16.0]
        rows["consumption_kwh_m2"] = [10.0, 18.0]
        rows["co2_kg_m2"] = [2.0, 5.0]
        rows["co2_t_building"] = [6.0, 15.0]
        fake.logger.info("[lhs] run 1/2: heating 12")
        fake.logger.info("[lhs] run 2/2: heating 20")
        return rows

    def make_tornado(_df, out):
        (out / "tornado.png").write_bytes(b"png")
        return {
            "heating_kwh_m2": [("infiltration_ach", 0.8), ("wall_u", 0.4), ("thermal_bridge_du", 0.3)],
            "cooling_kwh_m2": [("shade_setpoint", 0.7), ("wall_u", 0.5), ("infiltration_ach", 0.2)],
            "co2_kg_m2": [("emission_factor", 0.6), ("cop", -0.5), ("infiltration_ach", 0.4)],
        }

    fake = SimpleNamespace(
        ALL_VARS=variables, SIM_VARS=simulation, POST_VARS=post,
        DEFAULT_N=2, DEFAULT_SEED=42,
        BASELINE_MASSLESS={"heating": 16.67, "cooling": 18.67, "co2_s1": 8.61, "co2_s2": 4.68},
        BASELINE_LAYERED={"heating": 11.21, "cooling": 16.6},
        CADASTRE={"heating": 27.97},
        logger=logging.getLogger("fake_lhs"), sample_matrix=sample_matrix, run_study=run_study,
        make_histograms=lambda _df, out, _n: (out / "histograms.png").write_bytes(b"png"),
        make_tornado=make_tornado,
        summarize=lambda *_: "LHS fixture summary",
    )
    return fake


def test_lhs_adapter_serializes_samples_sensitivity_and_qa(tmp_path, monkeypatch):
    fake = _fake_lhs()
    monkeypatch.setattr(adapter, "_lhs", lambda: fake)
    events = []
    result = adapter.run_lhs(tmp_path, {"n": 2, "seed": 42, "_progress": lambda *args: events.append(args)})
    assert result["qa"]["scientific_status"] == "VALIDATED"
    assert result["summary"]["samples_completed"] == 2
    assert result["summary"]["statistics"]["heating_kwh_m2"]["mean"] == 16.0
    assert result["sensitivity"]["heating_kwh_m2"][0]["variable"] == "infiltration_ach"
    assert len(result["samples"]) == 2
    assert any("2/2" in stage for stage, _progress in events)
    for name in ("samples.csv", "runs.csv", "histograms.png", "tornado.png", "summary.txt", "results.json", "qa.json"):
        assert (tmp_path / name).exists()


def test_lhs_service_commits_an_immutable_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "var/workbench.sqlite3")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(lhs_service.service, "RUN_ROOT", tmp_path / "runs")
    service.RUN_ROOT.mkdir(parents=True)
    db.init_db()
    monkeypatch.setattr(lhs_service, "_require_ready", lambda: {"runtime_ready": True})
    monkeypatch.setattr(lhs_service, "_snapshot_inputs", lambda: ({}, {}))
    monkeypatch.setattr(lhs_service, "_verify_inputs_unchanged", lambda _manifest: None)
    result = {
        "schema_version": 1,
        "settings": {"n": 50, "seed": 42, "scope": "4252702YJ2745A"},
        "summary": {"samples_expected": 50, "samples_completed": 50, "statistics": {}},
        "qa": {"all_pass": True, "scientific_status": "VALIDATED", "checks": []},
        "variable_fingerprint": "a" * 64,
    }

    def fake_run(run_dir, _settings):
        service.write_json(run_dir / "results.json", result)
        (run_dir / "histograms.png").write_bytes(b"png")
        (run_dir / "tornado.png").write_bytes(b"png")
        return result

    monkeypatch.setattr(lhs_service, "run_lhs", fake_run)
    job_id = db.create_job("lhs", "4252702YJ2745A", result["settings"], timeout_seconds=2400)
    lhs_service.run_lhs_job(job_id)
    job = db.get_job(job_id)
    assert job["status"] == "completed"
    run = db.get_run(job["run_id"])
    assert run["run_type"] == "lhs"
    assert run["verification_status"] == "VERIFIED"
    assert Path(run["artifact_dir"], "manifest.json").exists()
    assert lhs_service.lhs_figure(run["id"], "histograms.png").is_file()

    results_path = Path(run["artifact_dir"], "results.json")
    results_path.chmod(0o644)
    results_path.write_text("{}", encoding="utf-8")
    tampered = lhs_service.lhs_detail(run["id"])
    assert tampered["verification"]["status"] == "TAMPERED"
    assert tampered["result"] is None
    with pytest.raises(PermissionError, match="not verified"):
        lhs_service.lhs_figure(run["id"], "histograms.png")

    failed_job_id = db.create_job("lhs", "4252702YJ2745A", result["settings"], timeout_seconds=2400)
    monkeypatch.setattr(db, "insert_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("commit interruption")))
    with pytest.raises(RuntimeError, match="commit interruption"):
        lhs_service.run_lhs_job(failed_job_id)
    assert not list(service.RUN_ROOT.glob(".lhs-*"))
    assert not list(service.RUN_ROOT.glob(".staging-*"))


def test_lhs_compare_rejects_same_run():
    with pytest.raises(ValueError, match="different"):
        lhs_service.compare_lhs("same", "same")


def test_lhs_compare_requires_identical_protocol_and_input_snapshots(monkeypatch):
    settings = {
        "scope": "4252702YJ2745A", "run_mode": "frozen_baseline",
        "method": "latin_hypercube_uniform_spearman", "n": 50, "seed": 42,
        "input_snapshot_hashes": {"model_builder": "a" * 64},
    }
    result = {
        "settings": settings,
        "qa": {"scientific_status": "VALIDATED"},
        "variable_fingerprint": "b" * 64,
        "summary": {"statistics": {"heating_kwh_m2": {"mean": 16.0}}},
    }
    runs = {
        "left": {"id": "left", "verification_status": "VERIFIED", "result": result},
        "same": {"id": "same", "verification_status": "VERIFIED", "result": result},
        "changed": {"id": "changed", "verification_status": "VERIFIED", "result": result | {"settings": settings | {"input_snapshot_hashes": {"model_builder": "c" * 64}}}},
    }
    monkeypatch.setattr(lhs_service, "lhs_detail", lambda run_id: runs[run_id])
    assert lhs_service.compare_lhs("left", "same")["comparable"] is True
    changed = lhs_service.compare_lhs("left", "changed")
    assert changed["comparable"] is False
    assert changed["rows"][0]["percent"] is None


def test_lhs_api_exposes_preflight_history_detail_compare_figure_and_job_creation(tmp_path, monkeypatch):
    figure = tmp_path / "histograms.png"
    figure.write_bytes(b"png")
    run = {"id": "lhs-run", "run_type": "lhs", "result": {"schema_version": 1}}
    job = {"id": "lhs-job", "kind": "lhs", "status": "queued"}
    monkeypatch.setattr(api_module, "lhs_preflight", lambda: {"scope": "4252702YJ2745A", "locked": True})
    monkeypatch.setattr(api_module, "list_lhs_runs", lambda: [run])
    monkeypatch.setattr(api_module, "lhs_detail", lambda run_id: run | {"id": run_id})
    monkeypatch.setattr(api_module, "compare_lhs", lambda left, right: {"left_run_id": left, "right_run_id": right, "comparable": True, "rows": []})
    monkeypatch.setattr(api_module, "lhs_figure", lambda _run_id, _name: figure)
    monkeypatch.setattr(api_module, "create_lhs_job", lambda: job)
    monkeypatch.setattr(api_module.manager, "notify", lambda: None)

    with TestClient(api_module.app) as client:
        assert client.get("/api/lhs/preflight").json()["locked"] is True
        assert client.get("/api/lhs/runs").json()[0]["id"] == "lhs-run"
        assert client.get("/api/lhs/runs/lhs-run").json()["result"]["schema_version"] == 1
        assert client.get("/api/lhs/compare", params={"left": "a", "right": "b"}).json()["comparable"] is True
        image = client.get("/api/lhs/runs/lhs-run/figures/histograms.png")
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/png")
        created = client.post("/api/lhs/runs")
        assert created.status_code == 202
        assert created.json()["kind"] == "lhs"
