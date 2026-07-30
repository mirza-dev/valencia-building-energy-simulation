from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest

import model_builder as mb
from workbench import capabilities, db, integrity, service, storage
from workbench import scenario_adapter, scenario_service, simulation_adapter
from workbench.api import app
from workbench.schemas import ScenarioRequest


PROJECT = Path(__file__).resolve().parents[1]
PILOT_OSM = PROJECT / "out/4252702YJ2745A/model_python.osm"
PILOT_META = PROJECT / "out/4252702YJ2745A/run_metadata.json"
EPW = PROJECT / "data/weather/ESP_Valencia.082840_IWEC.epw"


def test_adapter_shifts_only_real_setpoints_and_preserves_sentinels(tmp_path):
    result = scenario_adapter.create_model_variant(
        PILOT_OSM,
        tmp_path / "model_python.osm",
        {"heat_delta_c": 1.0, "cool_delta_c": -1.0},
    )
    assert result["mutation"]["heat_values_shifted"] == 6
    assert result["mutation"]["cool_values_shifted"] == 6
    assert result["audit"]["all_pass"] is True
    for mode in ("heating", "cooling"):
        audit = result["audit"][mode]
        assert audit["default_day_unchanged"] is True
        assert audit["sentinel_values_unchanged"] is True
        assert audit["all_values_expected"] is True
        assert audit["shifted_value_count"] == 6


def test_adapter_supports_weather_only_variant_and_zero_offsets(tmp_path):
    result = scenario_adapter.create_model_variant(
        PILOT_OSM,
        tmp_path / "model_python.osm",
        {"heat_delta_c": 0.0, "cool_delta_c": 0.0, "_weather_path": str(EPW)},
    )
    assert result["weather"] == {"epw_file": EPW.name}
    assert result["audit"]["all_pass"] is True
    assert result["audit"]["heating"]["shifted_value_count"] == 0
    assert result["audit"]["cooling"]["shifted_value_count"] == 0


def test_adapter_rejects_model_without_pipeline_thermostat(tmp_path):
    model = __import__("openstudio").model.Model()
    path = tmp_path / "empty.osm"
    assert model.save(__import__("openstudio").toPath(str(path)), True)
    with pytest.raises(RuntimeError, match="thermostat"):
        scenario_adapter.create_model_variant(
            path, tmp_path / "variant.osm", {"heat_delta_c": 1.0, "cool_delta_c": 0.0},
        )


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, -3.1, 3.1, "invalid"])
def test_delta_contract_rejects_nonfinite_and_out_of_range_values(value):
    with pytest.raises(ValueError):
        scenario_service._validate_delta(value, "Offset")


def test_capability_closes_on_adapter_builder_or_golden_drift(tmp_path, monkeypatch):
    original = json.loads((PROJECT / "tests/fixtures/scenario_smoke.json").read_text())
    for key, value, failed_check in (
        ("adapter_sha256", "wrong", "adapter_hash"),
        ("builder_sha256", "wrong", "builder_hash"),
        ("smoke_status", "failed", "golden_smoke"),
    ):
        fixture = dict(original)
        fixture[key] = value
        fixture_path = tmp_path / f"scenario-{key}.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        manifest = json.loads((PROJECT / "workbench-capabilities.json").read_text())
        manifest["capabilities"]["scenario"]["fixture"] = str(fixture_path)
        manifest_path = tmp_path / f"capabilities-{key}.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setattr(capabilities, "MANIFEST_PATH", manifest_path)
        status = capabilities.capability_status()["capabilities"]["scenario"]
        assert status["runtime_ready"] is False
        assert status["contract"]["checks"][failed_check] is False


def test_public_scenario_api_and_request_contract_are_registered():
    paths = app.openapi()["paths"]
    assert {"/api/scenarios/options", "/api/scenarios", "/api/scenarios/jobs/active", "/api/scenarios/jobs/{job_id}", "/api/scenarios/{scenario_id}"} <= set(paths)
    request = ScenarioRequest(
        parent_run_id="verified-parent", name="Comfort +1 K", heat_delta_c=1,
        cool_delta_c=0, reason="Sensitivity", source_type="human_judgement",
    )
    assert request.heat_delta_c == 1
    with pytest.raises(ValueError):
        ScenarioRequest(
            parent_run_id="verified-parent", name="Invalid", heat_delta_c=3.1,
            cool_delta_c=0, reason="Sensitivity", source_type="human_judgement",
        )


def _isolate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "var/workbench.sqlite3")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(scenario_service.service, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(storage, "WARNING_FREE_BYTES", 0)
    monkeypatch.setattr(storage, "BLOCKED_FREE_BYTES", 0)
    monkeypatch.setattr(storage, "WARNING_FREE_RATIO", 0.0)
    monkeypatch.setattr(storage, "BLOCKED_FREE_RATIO", 0.0)
    monkeypatch.setattr(storage, "RESERVE_FLOOR_BYTES", 0)
    service.RUN_ROOT.mkdir(parents=True)
    db.init_db()


def _parent_fixture(tmp_path: Path) -> tuple[dict, dict]:
    root = tmp_path / "parent"
    root.mkdir()
    shutil.copy2(PILOT_OSM, root / "model_python.osm")
    (root / "scene.json").write_text('{"schema_version":1,"surfaces":[]}', encoding="utf-8")
    (root / "model_3d.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    (root / "geometry_actions.json").write_text("[]", encoding="utf-8")
    metadata = json.loads(PILOT_META.read_text(encoding="utf-8"))
    weather = integrity.ensure_snapshot(EPW, kind="weather")
    config = mb.DEFAULT_BUILD_CONFIG.model_dump(mode="json")
    parent = {
        "id": "root-parent", "job_id": "parent-job", "refparcela": "4252702YJ2745A",
        "scenario_name": "Pilot baseline", "config": config, "stats": metadata["stats"],
        "qa": {"all_pass": True, "warning_count": 0, "checks": []},
        "artifact_dir": str(root), "run_type": "model", "parent_run_id": None,
        "verification_status": "VERIFIED",
        "raw_model_sha256": integrity.sha256_file(root / "model_python.osm"),
        "canonical_fingerprint": integrity.canonical_model_fingerprint(root / "model_python.osm"),
    }
    inputs = {
        "weather": {
            "snapshot_hash": weather["snapshot_hash"], "source_name": weather["source_name"],
            "kind": "weather", "components": weather["components"],
        }
    }
    return parent, inputs


def test_create_scenario_job_requires_a_real_change_and_records_provenance(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    parent, inputs = _parent_fixture(tmp_path)
    db.replace_profiles([{
        "id": parent["config"]["provenance"]["baseline_profile"],
        "label": "Pilot", "source": "Fixture", "config": parent["config"],
    }])
    monkeypatch.setattr(
        scenario_service, "capability_status",
        lambda: {"capabilities": {"scenario": {"runtime_ready": True}}},
    )
    monkeypatch.setattr(scenario_service, "_root_parent", lambda _run_id: (parent, inputs))
    base_request = {
        "parent_run_id": parent["id"], "name": "Comfort sensitivity",
        "heat_delta_c": 0.0, "cool_delta_c": 0.0, "weather_dataset_id": None,
        "reason": "Scientific sensitivity", "source_type": "human_judgement",
        "source_ref": None,
    }
    with pytest.raises(ValueError, match="must change"):
        scenario_service.create_scenario_job(base_request)
    job = scenario_service.create_scenario_job(base_request | {"heat_delta_c": 1.0})
    assert job["kind"] == "scenario"
    scenario = db.get_scenario(job["payload"]["scenario_id"])
    assert scenario["status"] == "QUEUED"
    assert scenario["overrides"] == [{
        "field": "part_g.heat_delta_c", "reason": "Scientific sensitivity",
        "source_type": "human_judgement", "source_ref": None,
        "revision": 1, "created_at": scenario["overrides"][0]["created_at"],
    }]
    future_epw = tmp_path / "Valencia_2050.epw"
    future_epw.write_text(
        EPW.read_text(encoding="utf-8").replace("LOCATION,Valencia", "LOCATION,Valencia Future", 1),
        encoding="utf-8",
    )
    snapshot = integrity.ensure_snapshot(future_epw, kind="weather")
    db.upsert_dataset({
        "id": "future-weather", "kind": "weather", "name": "Valencia 2050",
        "path": str(future_epw), "sha256": snapshot["snapshot_hash"],
        "snapshot_hash": snapshot["snapshot_hash"], "verification_status": "VERIFIED",
        "read_only": True, "metadata": {"managed": True},
    })
    future_job = scenario_service.create_scenario_job(base_request | {
        "name": "Future climate", "weather_dataset_id": "future-weather",
    })
    assert future_job["payload"]["scenario_settings"]["weather_changed"] is True
    assert future_job["payload"]["weather"]["snapshot_hash"] == snapshot["snapshot_hash"]


def test_scenario_job_commits_verified_variant_without_changing_parent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    parent, inputs = _parent_fixture(tmp_path)
    parent_hash_before = integrity.sha256_file(Path(parent["artifact_dir"]) / "model_python.osm")
    monkeypatch.setattr(
        scenario_service, "capability_status",
        lambda: {"capabilities": {"scenario": {"runtime_ready": True}}},
    )
    monkeypatch.setattr(scenario_service, "_root_parent", lambda _run_id: (parent, inputs))
    monkeypatch.setattr(
        scenario_service.simulation_service, "create_simulation_job",
        lambda run_id: {"id": f"simulation-{run_id}", "status": "queued"},
    )
    scenario_settings = {
        "schema_version": 1, "name": "Comfort +1 / -1 K", "parent_run_id": parent["id"],
        "heat_delta_c": 1.0, "cool_delta_c": -1.0, "weather_dataset_id": None,
        "weather_dataset_name": None, "weather_snapshot_hash": inputs["weather"]["snapshot_hash"],
        "weather_source_name": inputs["weather"]["source_name"], "weather_changed": False,
        "reason": "Golden comfort sensitivity", "source_type": "human_judgement",
        "source_ref": None,
    }
    fingerprint = scenario_service._scenario_fingerprint(
        parent, 1.0, -1.0, inputs["weather"]["snapshot_hash"],
    )
    scenario_settings["scenario_fingerprint"] = fingerprint
    job_id = db.create_job("scenario", parent["refparcela"], {
        "parent_run_id": parent["id"], "scenario_id": "scenario-1",
        "config": parent["config"], "scenario_settings": scenario_settings,
        "weather": inputs["weather"], "scenario_fingerprint": fingerprint,
    })
    with db.connect() as con:
        con.execute("UPDATE jobs SET status='running',attempt_count=1 WHERE id=?", (job_id,))

    scenario_service.run_scenario_job(job_id)
    job = db.get_job(job_id)
    run = db.get_run(job["run_id"])
    assert job["status"] == "completed"
    assert job["payload"]["simulation_job_id"] == f"simulation-{run['id']}"
    assert run["run_type"] == "model"
    assert run["parent_run_id"] == parent["id"]
    assert run["verification_status"] == "VERIFIED"
    assert run["stats"]["part_g"]["heat_delta_c"] == 1.0
    assert run["stats"]["part_g"]["cool_delta_c"] == -1.0
    root = Path(run["artifact_dir"])
    assert {item["name"] for item in run["artifacts"]} >= {
        "parent_model.osm", "model_python.osm", "scene.json", "model_3d.png",
        "renderer_manifest.json", "scenario_settings.json", "scenario_qa.json",
        "config.json", "stats.json", "qa.json", "qa.csv", "input_manifest.json", "manifest.json",
    }
    assert _read(root / "scenario_qa.json")["all_pass"] is True
    assert integrity.sha256_file(root / "scene.json") == integrity.sha256_file(Path(parent["artifact_dir"]) / "scene.json")
    assert integrity.sha256_file(Path(parent["artifact_dir"]) / "model_python.osm") == parent_hash_before
    assert integrity.sha256_file(root / "parent_model.osm") == parent_hash_before
    assert integrity.sha256_file(root / "model_python.osm") != parent_hash_before

    model_count = len(db.list_runs_by_type("model"))
    with db.connect() as con:
        con.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
    scenario_service.run_scenario_job(job_id)
    assert len(db.list_runs_by_type("model")) == model_count
    assert db.get_job(job_id)["status"] == "completed"


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_real_part_g_energyplus_golden(tmp_path):
    variant = tmp_path / "variant.osm"
    scenario_adapter.create_model_variant(
        PILOT_OSM, variant, {"heat_delta_c": 1.0, "cool_delta_c": -1.0},
    )
    metadata = json.loads(PILOT_META.read_text(encoding="utf-8"))
    stats = metadata["stats"]
    settings = {
        "run_period": "annual", "timestep_per_hour": 6,
        "output_variables": [], "energy_output_variables": mb.OUTPUT_VARIABLES,
        "area_basis": "conditioned_residential_area",
        "conditioned_residential_area_m2": float(stats["res_area_m2"]),
        "parent_run_id": "part-g-golden", "parent_model_sha256": integrity.sha256_file(variant),
        "parent_canonical_fingerprint": integrity.canonical_model_fingerprint(variant),
        "weather_snapshot_hash": integrity.ensure_snapshot(EPW, kind="weather")["snapshot_hash"],
        "cadastre_heating": {"baseline_heating_kwh_m2": 27.97, "post_intervention_heating_kwh_m2": 6.63, "cooling_reference": None},
        "scenario": {"name": "Golden +1/-1 K", "heat_delta_c": 1.0, "cool_delta_c": -1.0, "weather_snapshot_hash": "golden", "weather_source_name": EPW.name, "weather_changed": False},
        "provenance": {"scenario": "golden"}, "_weather_path": str(EPW), "_parent_stats": stats,
    }
    result = simulation_adapter.run_simulation(variant, tmp_path / "simulation", settings)
    assert result["qa"]["scientific_status"] == "VALIDATED"
    assert result["qa"]["all_pass"] is True
    assert result["warnings"]["severes"] == 0
    assert result["warnings"]["fatals"] == 0
    assert result["normalized_energy"]["heating_kwh_m2"] == pytest.approx(12.38, abs=0.02)
    assert result["normalized_energy"]["cooling_kwh_m2"] == pytest.approx(17.22, abs=0.02)
