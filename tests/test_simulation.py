from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import geopandas as gpd
from shapely.geometry import Point

import model_builder as mb
import run_simulation as domain_simulation
from workbench import capabilities, db, integrity, service
from workbench import simulation_adapter as adapter
from workbench import simulation_service


PROJECT = Path(__file__).resolve().parents[1]
PILOT_OSM = PROJECT / "out/4252702YJ2745A/model_python.osm"
PILOT_META = PROJECT / "out/4252702YJ2745A/run_metadata.json"
EPW = PROJECT / "data/weather/ESP_Valencia.082840_IWEC.epw"


def test_demanda_companion_is_not_typed_as_cooling() -> None:
    assert domain_simulation.COL_HEAT_DEMAND == "demanda_ca"
    assert domain_simulation.COL_UNCONFIRMED_DEMAND == "demanda__1"
    assert not hasattr(domain_simulation, "COL_COOL_DEMAND")


def _fixture_manifest(tmp_path: Path, monkeypatch, mutate) -> dict:
    fixture = json.loads((PROJECT / "tests/fixtures/simulation_smoke.json").read_text())
    mutate(fixture)
    fixture_path = tmp_path / "simulation.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    manifest = json.loads((PROJECT / "workbench-capabilities.json").read_text())
    manifest["capabilities"]["simulation"]["fixture"] = str(fixture_path)
    manifest_path = tmp_path / "capabilities.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(capabilities, "MANIFEST_PATH", manifest_path)
    return capabilities.capability_status()["capabilities"]["simulation"]


@pytest.mark.parametrize("mutation,failed_check", [
    (lambda item: item.update(adapter_sha256="wrong"), "adapter_hash"),
    (lambda item: item.update(runner_sha256="wrong"), "runner_hash"),
    (lambda item: item["energyplus"].update(executable="/missing/energyplus"), "energyplus_runtime"),
    (lambda item: item.update(weather_snapshot_hash="wrong"), "weather_snapshot"),
    (lambda item: item.update(schema_version=99), "fixture_schema"),
    (lambda item: item.update(smoke_status="missing"), "golden_smoke"),
])
def test_capability_closes_when_contract_evidence_changes(tmp_path, monkeypatch, mutation, failed_check):
    status = _fixture_manifest(tmp_path, monkeypatch, mutation)
    assert status["runtime_ready"] is False
    assert status["contract"]["checks"][failed_check] is False


def test_capability_closes_when_fixture_is_missing(tmp_path, monkeypatch):
    manifest = json.loads((PROJECT / "workbench-capabilities.json").read_text())
    manifest["capabilities"]["simulation"]["fixture"] = str(tmp_path / "missing.json")
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(capabilities, "MANIFEST_PATH", path)
    status = capabilities.capability_status()["capabilities"]["simulation"]
    assert status["runtime_ready"] is False
    assert status["contract"]["reason"] == "fixture_missing"


def _fake_sql(path: Path, heating_j: float, cooling_j: float) -> None:
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE ReportDataDictionary(
              ReportDataDictionaryIndex INTEGER, Name TEXT, KeyValue TEXT, ReportingFrequency TEXT);
            CREATE TABLE ReportData(ReportDataDictionaryIndex INTEGER, Value REAL);
        """)
        con.executemany(
            "INSERT INTO ReportDataDictionary VALUES(?,?,?,?)",
            [
                (1, "Zone Ideal Loads Supply Air Total Heating Energy", "Zone", "Run Period"),
                (2, "Zone Ideal Loads Supply Air Total Cooling Energy", "Zone", "Run Period"),
            ],
        )
        con.executemany("INSERT INTO ReportData VALUES(?,?)", [(1, heating_j), (2, cooling_j)])


def _adapter_settings(tmp_path: Path) -> dict:
    return {
        "run_period": "annual",
        "timestep_per_hour": 6,
        "output_variables": [],
        "energy_output_variables": {
            "heating": "Zone Ideal Loads Supply Air Total Heating Energy",
            "cooling": "Zone Ideal Loads Supply Air Total Cooling Energy",
        },
        "area_basis": "conditioned_residential_area",
        "conditioned_residential_area_m2": 100.0,
        "parent_run_id": "parent-1",
        "parent_model_sha256": "model-hash",
        "parent_canonical_fingerprint": "canonical",
        "weather_snapshot_hash": "weather-hash",
        "cadastre_heating": {
            "baseline_heating_kwh_m2": 27.97,
            "post_intervention_heating_kwh_m2": 6.63,
            "cooling_reference": None,
        },
        "provenance": {"runner_sha256": "runner", "adapter_sha256": "adapter"},
        "_weather_path": str(tmp_path / "parent.epw"),
        "_parent_stats": {"refparcela": "A", "res_area_m2": 100.0, "facade_qa": []},
    }


def test_adapter_calls_part_b_chain_without_rebuilding_and_retains_raw_joules(tmp_path, monkeypatch):
    order = []
    mb = SimpleNamespace(EPW_FILE=Path("original.epw"), OUTPUT_VARIABLES={"old": "value"})

    def run_energyplus(_model, run_dir):
        order.append("run_energyplus")
        (run_dir / "model.idf").write_text("idf")
        (run_dir / "model_python.osm").write_text("osm")
        (run_dir / "eplusout.err").write_text("Completed Successfully-- 11 Warning; 0 Severe Errors;")
        _fake_sql(run_dir / "eplusout.sql", 36_000_000.0, 72_000_000.0)
        return run_dir / "eplusout.sql"

    fake = SimpleNamespace(
        mb=mb,
        run_energyplus=run_energyplus,
        read_results=lambda _path, _area: order.append("read_results") or {
            "heating_kwh": 10.0, "cooling_kwh": 20.0,
            "heating_kwh_m2": 0.1, "cooling_kwh_m2": 0.2,
        },
        scan_err_file=lambda _dir: order.append("scan_err_file") or {"warnings": 11, "severes": 0},
        crosscheck_energyplus=lambda _path, _stats: order.append("crosscheck_energyplus") or [],
        check_plausibility=lambda _res: order.append("check_plausibility") or [],
        write_qa_report=lambda *_args: order.append("write_qa_report") or True,
        carbon_footprint=lambda _res, _area: order.append("carbon_footprint") or {"s1_co2_kg_m2": 1.0},
        simulate_building=lambda *_args: pytest.fail("simulate_building must never be called"),
    )
    monkeypatch.setattr(adapter.importlib, "import_module", lambda _name: fake)
    monkeypatch.setattr(adapter, "_load_parent_model", lambda _path: object())
    settings = _adapter_settings(tmp_path)
    settings["carbon_settings"] = {"cop": 2.0, "seer": 4.0, "emission_factor": 0.3}
    result = adapter.run_simulation(tmp_path / "parent.osm", tmp_path / "run", settings)
    assert order == [
        "run_energyplus", "scan_err_file", "read_results", "crosscheck_energyplus",
        "check_plausibility", "write_qa_report", "carbon_footprint",
    ]
    assert result["raw_energy"]["heating"]["joule"] == 36_000_000.0
    assert result["raw_energy"]["heating"]["kwh"] == 10.0
    assert result["raw_energy"]["heating"]["kwh_m2"] == 0.1
    assert result["qa"]["scientific_status"] == "VALIDATED"
    assert result["carbon"]["authored_consumption_kwh_m2"] == pytest.approx(0.1)
    assert result["carbon"]["authored_co2_kg_m2"] == pytest.approx(0.03)
    assert result["carbon"]["authored_cop"] == 2.0
    assert mb.EPW_FILE == Path("original.epw")
    assert mb.OUTPUT_VARIABLES == {"old": "value"}


def test_adapter_preserves_severe_failure_as_invalid_diagnostic_run(tmp_path, monkeypatch):
    mb = SimpleNamespace(EPW_FILE=Path("original.epw"), OUTPUT_VARIABLES={})

    def fail(_model, run_dir):
        (run_dir / "model.idf").write_text("diagnostic idf")
        (run_dir / "eplusout.err").write_text(
            "**  Severe  ** Invalid object\n**  Fatal  ** Simulation terminated\n",
        )
        raise RuntimeError("EnergyPlus failed")

    fake = SimpleNamespace(mb=mb, run_energyplus=fail)
    monkeypatch.setattr(adapter.importlib, "import_module", lambda _name: fake)
    monkeypatch.setattr(adapter, "_load_parent_model", lambda _path: object())
    result = adapter.run_simulation(tmp_path / "parent.osm", tmp_path / "run", _adapter_settings(tmp_path))
    assert result["qa"]["scientific_status"] == "INVALID"
    assert result["normalized_energy"] is None
    assert result["carbon"] is None
    assert (tmp_path / "run/results.json").exists()
    assert (tmp_path / "run/eplusout.err").exists()


def _isolate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "var/workbench.sqlite3")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(simulation_service.service, "RUN_ROOT", tmp_path / "runs")
    service.RUN_ROOT.mkdir(parents=True)
    db.init_db()


def _insert_parent(tmp_path: Path, status: str = "VERIFIED", run_type: str = "model") -> tuple[str, Path]:
    root = tmp_path / f"parent-{status.lower()}-{run_type}"
    root.mkdir()
    osm = root / "model_python.osm"
    osm.write_text("immutable parent", encoding="utf-8")
    manifest = root / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    artifacts = service.artifact_manifest(root)
    job = db.create_job("preview", "A", {"config": {}})
    run_id = f"parent-{status.lower()}-{run_type}"
    db.insert_run({
        "id": run_id, "job_id": job, "refparcela": "A", "scenario_name": "Parent",
        "config": {}, "stats": {}, "qa": {}, "artifact_dir": str(root),
        "run_type": run_type, "verification_status": status,
        "raw_model_sha256": integrity.sha256_file(osm),
        "manifest_sha256": integrity.sha256_file(manifest),
    }, artifacts)
    return run_id, osm


@pytest.mark.parametrize("status,run_type,message", [
    ("LEGACY", "model", "LEGACY"),
    ("TAMPERED", "model", "TAMPERED"),
    ("VERIFIED", "simulation", "model run"),
])
def test_parent_security_gate_rejects_ineligible_runs(tmp_path, monkeypatch, status, run_type, message):
    _isolate(tmp_path, monkeypatch)
    run_id, _ = _insert_parent(tmp_path, status, run_type)
    with pytest.raises(ValueError, match=message):
        simulation_service._verified_parent(run_id)


def test_changed_parent_osm_is_marked_tampered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    run_id, osm = _insert_parent(tmp_path)
    osm.write_text("changed parent", encoding="utf-8")
    with pytest.raises(ValueError, match="not verified|hash|TAMPERED"):
        simulation_service._verified_parent(run_id)
    assert db.get_run(run_id)["verification_status"] == "TAMPERED"


def test_duplicate_active_simulation_job_is_detected(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    first = db.create_job("simulation", "A", {"parent_run_id": "parent"}, timeout_seconds=600)
    found = db.find_active_simulation_job("parent")
    assert found and found["id"] == first
    with db.connect() as con:
        con.execute("UPDATE jobs SET status='completed' WHERE id=?", (first,))
    assert db.find_active_simulation_job("parent") is None


def test_authored_pair_runs_automatic_baseline_first_through_same_part_b_path(monkeypatch):
    parents = {
        "automatic-model": {
            "id": "automatic-model", "refparcela": "9876501YJ2797F",
            "provenance": "pipeline", "authored_from": None,
        },
        "authored-model": {
            "id": "authored-model", "refparcela": "9876501YJ2797F",
            "provenance": "authored", "authored_from": "automatic-model",
        },
    }
    order = []
    monkeypatch.setattr(simulation_service, "_verified_parent", lambda run_id: parents[run_id])
    monkeypatch.setattr(simulation_service, "_pair_leg", lambda model_id: order.append(model_id) or {
        "model_id": model_id, "simulation_run_id": None,
        "job": {"id": f"job-{model_id}"}, "status": "queued",
    })
    paired = simulation_service.create_authored_simulation_pair("authored-model")
    assert order == ["automatic-model", "authored-model"]
    assert paired["refparcela"] == "9876501YJ2797F"
    assert paired["baseline"]["model_id"] == "automatic-model"
    assert paired["authored"]["model_id"] == "authored-model"


def test_authored_pair_rejects_a_baseline_from_another_refparcela(monkeypatch):
    parents = {
        "automatic-model": {"id": "automatic-model", "refparcela": "A", "provenance": "pipeline"},
        "authored-model": {
            "id": "authored-model", "refparcela": "B",
            "provenance": "authored", "authored_from": "automatic-model",
        },
    }
    monkeypatch.setattr(simulation_service, "_verified_parent", lambda run_id: parents[run_id])
    with pytest.raises(ValueError, match="same refparcela"):
        simulation_service.create_authored_simulation_pair("authored-model")


def _service_parent(tmp_path: Path) -> tuple[str, str]:
    parent_root = tmp_path / "parent-run"
    parent_root.mkdir()
    parent_osm = parent_root / "model_python.osm"
    shutil.copy2(PILOT_OSM, parent_osm)
    gis = tmp_path / "parent.gpkg"
    gpd.GeoDataFrame({
        "refparcela": ["4252702YJ2745A"], "demanda_ca": [27.97], "demanda__1": [6.63],
    }, geometry=[Point(0, 0)], crs="EPSG:25830").to_file(gis, driver="GPKG")
    weather = tmp_path / "parent.epw"
    weather.write_text("parent weather snapshot", encoding="utf-8")
    gis_snapshot = integrity.ensure_snapshot(gis, kind="gis")
    weather_snapshot = integrity.ensure_snapshot(weather, kind="weather")
    inputs = {
        "buildings": {"snapshot_hash": gis_snapshot["snapshot_hash"], "source_name": gis.name, "kind": "gis", "components": gis_snapshot["components"]},
        "weather": {"snapshot_hash": weather_snapshot["snapshot_hash"], "source_name": weather.name, "kind": "weather", "components": weather_snapshot["components"]},
    }
    service.write_json(parent_root / "input_manifest.json", inputs)
    service.write_json(parent_root / "manifest.json", {"schema_version": 2})
    artifacts = service.artifact_manifest(parent_root)
    job = db.create_job("preview", "4252702YJ2745A", {"config": {}})
    metadata = json.loads(PILOT_META.read_text(encoding="utf-8"))
    parent_id = "verified-parent"
    db.insert_run({
        "id": parent_id, "job_id": job, "refparcela": "4252702YJ2745A",
        "scenario_name": "Pilot parent", "config": {
            "operation": {"output_variables": {
                "heating": "Zone Ideal Loads Supply Air Total Heating Energy",
                "cooling": "Zone Ideal Loads Supply Air Total Cooling Energy",
            }},
        }, "stats": metadata["stats"], "qa": {"all_pass": True},
        "artifact_dir": str(parent_root), "run_type": "model",
        "verification_status": "VERIFIED", "raw_model_sha256": integrity.sha256_file(parent_osm),
        "canonical_fingerprint": "canonical-parent",
        "manifest_sha256": integrity.sha256_file(parent_root / "manifest.json"),
    }, artifacts, {"buildings": gis_snapshot["snapshot_hash"], "weather": weather_snapshot["snapshot_hash"]})
    return parent_id, weather_snapshot["snapshot_hash"]


def _fake_simulation_result(settings: dict, scientific_status: str = "VALIDATED") -> dict:
    invalid = scientific_status == "INVALID"
    normalized = None if invalid else {
        "heating_kwh": 31493.1, "cooling_kwh": 46635.8,
        "heating_kwh_m2": 11.21, "cooling_kwh_m2": 16.6,
    }
    return {
        "schema_version": 1,
        "settings": {key: value for key, value in settings.items() if not key.startswith("_")},
        "raw_energy": None if invalid else {
            "area_basis": "conditioned_residential_area", "area_m2": 2809.9,
            "heating": {"variable": "heat", "joule": 113375144288.0, "kwh": 31493.1, "kwh_m2": 11.21},
            "cooling": {"variable": "cool", "joule": 167888785496.0, "kwh": 46635.8, "kwh_m2": 16.6},
        },
        "normalized_energy": normalized,
        "qa": {"scientific_status": scientific_status, "all_pass": not invalid, "checks": [], "unmet_hours": 0.0, "error_summary": "fatal fixture" if invalid else None, "warning_summary": {"warnings": 0, "severes": int(invalid), "fatals": int(invalid), "categories": [], "messages": []}},
        "warnings": {"warnings": 0, "severes": int(invalid), "fatals": int(invalid), "categories": [], "messages": []},
        "carbon": None if invalid else {"s1_co2_kg_m2": 6.46, "s2_co2_kg_m2": 3.68},
        "cadastre_heating": settings["cadastre_heating"], "provenance": settings["provenance"],
    }


def test_simulation_child_commit_uses_parent_snapshots_and_exports_core_artifacts(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "EXPORT_ROOT", tmp_path / "exports")
    service.EXPORT_ROOT.mkdir()
    parent_id, parent_weather_hash = _service_parent(tmp_path)
    parent_before = db.get_run(parent_id)
    parent_artifact_hashes = {item["name"]: item["sha256"] for item in parent_before["artifacts"]}
    observed = {}

    def fake_adapter(osm_path, run_dir, settings):
        observed["osm_sha256"] = integrity.sha256_file(osm_path)
        observed["weather"] = Path(settings["_weather_path"]).read_text(encoding="utf-8")
        result = _fake_simulation_result(settings)
        for name, content in {
            "model_python.osm": Path(osm_path).read_bytes(), "model.idf": b"idf",
            "eplusout.sql": b"sql", "eplusout.err": b"0 Severe", "eplusout.eio": b"eio",
            "eplusout.eso": b"eso", "eplusout.end": b"end", "qa_report.txt": b"PASS",
        }.items():
            target = run_dir / name
            target.write_bytes(content if isinstance(content, bytes) else bytes(content))
        for name, value in {
            "results.json": result, "results.csv": {"status": "VALIDATED"},
            "raw_energy.json": result["raw_energy"], "qa.json": result["qa"],
            "warnings.json": result["warnings"], "simulation_settings.json": result["settings"],
            "simulation_metadata.json": result["provenance"],
        }.items():
            if name.endswith(".csv"):
                (run_dir / name).write_text("status\nVALIDATED\n", encoding="utf-8")
            else:
                service.write_json(run_dir / name, value)
        return result

    monkeypatch.setattr(simulation_service, "run_simulation", fake_adapter)
    job_id = db.create_job("simulation", "4252702YJ2745A", {"parent_run_id": parent_id}, timeout_seconds=600)
    with db.connect() as con:
        con.execute("UPDATE jobs SET status='running',attempt_count=1 WHERE id=?", (job_id,))
    simulation_service.run_simulation_job(job_id)
    job = db.get_job(job_id)
    child = db.get_run(job["run_id"])
    assert child["run_type"] == "simulation"
    assert child["parent_run_id"] == parent_id
    assert child["verification_status"] == "VERIFIED"
    assert child["qa"]["scientific_status"] == "VALIDATED"
    assert observed["weather"] == "parent weather snapshot"
    assert child["config"]["weather_snapshot_hash"] == parent_weather_hash
    assert observed["osm_sha256"] == parent_before["raw_model_sha256"]
    assert integrity.sha256_file(Path(child["artifact_dir"]) / "parent_model.osm") == parent_before["raw_model_sha256"]
    assert {item["name"] for item in child["artifacts"]} >= {
        "parent_model.osm", "model_python.osm", "model.idf", "eplusout.sql", "eplusout.err",
        "eplusout.eio", "eplusout.eso", "eplusout.end", "results.json", "results.csv",
        "raw_energy.json", "qa.json", "qa_report.txt", "warnings.json",
        "simulation_settings.json", "simulation_metadata.json", "input_manifest.json", "manifest.json",
    }
    parent_after = db.get_run(parent_id)
    assert {item["name"]: item["sha256"] for item in parent_after["artifacts"]} == parent_artifact_hashes
    package = service.export_run(child["id"])
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert {"parent_model.osm", "model.idf", "eplusout.sql", "eplusout.err", "export_manifest.sig.json"} <= names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["parent_run_id"] == parent_id
        assert manifest["provenance"]["weather_snapshot_hash"] == parent_weather_hash


def test_invalid_diagnostic_result_commits_without_energy_interpretation(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    parent_id, _ = _service_parent(tmp_path)

    def fake_invalid(osm_path, run_dir, settings):
        result = _fake_simulation_result(settings, "INVALID")
        shutil.copy2(osm_path, run_dir / "model_python.osm")
        (run_dir / "model.idf").write_text("invalid idf")
        (run_dir / "eplusout.err").write_text("**  Severe  ** fixture\n**  Fatal  ** fixture")
        for name, value in {"results.json": result, "raw_energy.json": None, "qa.json": result["qa"], "warnings.json": result["warnings"], "simulation_settings.json": result["settings"], "simulation_metadata.json": result["provenance"]}.items():
            service.write_json(run_dir / name, value)
        (run_dir / "results.csv").write_text("scientific_status\nINVALID\n")
        (run_dir / "qa_report.txt").write_text("INVALID")
        return result

    monkeypatch.setattr(simulation_service, "run_simulation", fake_invalid)
    job_id = db.create_job("simulation", "4252702YJ2745A", {"parent_run_id": parent_id}, timeout_seconds=600)
    with db.connect() as con:
        con.execute("UPDATE jobs SET status='running',attempt_count=1 WHERE id=?", (job_id,))
    simulation_service.run_simulation_job(job_id)
    detail = simulation_service.simulation_detail(db.get_job(job_id)["run_id"])
    assert detail["verification"]["status"] == "VERIFIED"
    assert detail["result"]["qa"]["scientific_status"] == "INVALID"
    assert detail["result"]["normalized_energy"] is None
    assert detail["result"]["carbon"] is None


def test_real_pilot_adapter_golden_values(tmp_path):
    config = mb.DEFAULT_BUILD_CONFIG.model_copy(deep=True)
    row = service.read_building("4252702YJ2745A", config.data.building_path)
    geometry = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(
        geometry, row["refparcela"], config.data.neighbor_path, config=config,
    )
    party = mb.find_party_walls(
        geometry,
        row["refparcela"],
        config.data.neighbor_path,
        neighbors=neighbors,
        config=config,
    )
    model = mb.build_model_with_config(row, party, config, neighbors=neighbors)
    pilot_osm = mb.save_model(model.osm, tmp_path / "model")
    stats = model.stats
    settings = {
        "run_period": "annual", "timestep_per_hour": 6, "output_variables": [],
        "energy_output_variables": {
            "heating": "Zone Ideal Loads Supply Air Total Heating Energy",
            "cooling": "Zone Ideal Loads Supply Air Total Cooling Energy",
        },
        "area_basis": "conditioned_residential_area",
        "conditioned_residential_area_m2": stats["res_area_m2"],
        "parent_run_id": "golden", "parent_model_sha256": integrity.sha256_file(pilot_osm),
        "parent_canonical_fingerprint": "golden", "weather_snapshot_hash": "golden-weather",
        "cadastre_heating": {"baseline_heating_kwh_m2": 27.97, "post_intervention_heating_kwh_m2": 6.63, "cooling_reference": None},
        "provenance": {"test": "golden"}, "_weather_path": str(EPW), "_parent_stats": stats,
    }
    result = adapter.run_simulation(pilot_osm, tmp_path / "energyplus", settings)
    assert result["normalized_energy"]["heating_kwh_m2"] == pytest.approx(11.21, abs=0.02)
    assert result["normalized_energy"]["cooling_kwh_m2"] == pytest.approx(16.60, abs=0.02)
    assert result["qa"]["all_pass"] is True
    assert result["warnings"]["warnings"] == 11
    assert result["warnings"]["severes"] == 0
    assert result["carbon"]["s1_co2_kg_m2"] == pytest.approx(6.46, abs=0.01)
    assert result["carbon"]["s2_co2_kg_m2"] == pytest.approx(3.68, abs=0.01)
    raw = result["raw_energy"]
    assert raw["heating"]["joule"] / 3_600_000 / raw["area_m2"] == pytest.approx(raw["heating"]["kwh_m2"])
    assert raw["cooling"]["joule"] / 3_600_000 / raw["area_m2"] == pytest.approx(raw["cooling"]["kwh_m2"])
