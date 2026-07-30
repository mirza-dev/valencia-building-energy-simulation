from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box, mapping

from workbench import capabilities, db, service
from workbench import neighborhood_adapter as adapter
from workbench import neighborhood_map_cache
from workbench import neighborhood_service


PROJECT = Path(__file__).resolve().parents[1]


def _contract_manifest(tmp_path: Path, monkeypatch, runner_source: str) -> dict:
    runner = tmp_path / "neighborhood_pipeline.py"
    runner.write_text(runner_source, encoding="utf-8")
    fixture = json.loads((PROJECT / "tests/fixtures/neighborhood_smoke.json").read_text())
    fixture["runner"] = str(runner)
    fixture["runner_sha256"] = hashlib.sha256(runner.read_bytes()).hexdigest()
    fixture["stock_policy_sha256"] = hashlib.sha256(
        (PROJECT / fixture["stock_policy"]).read_bytes()
    ).hexdigest()
    fixture["adapter_sha256"] = hashlib.sha256(
        (PROJECT / "src/workbench/neighborhood_adapter.py").read_bytes()
    ).hexdigest()
    fixture_path = tmp_path / "neighborhood.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    manifest = json.loads((PROJECT / "workbench-capabilities.json").read_text())
    manifest["capabilities"]["neighborhood"]["fixture"] = str(fixture_path)
    manifest_path = tmp_path / "capabilities.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(capabilities, "MANIFEST_PATH", manifest_path)
    return capabilities.capability_status()["capabilities"]["neighborhood"]


def test_neighborhood_capability_requires_real_part_c_callable_contract(tmp_path, monkeypatch):
    source = "\n".join(f"def {name}():\n    pass\n" for name in adapter.REQUIRED_ENTRYPOINTS)
    status = _contract_manifest(tmp_path, monkeypatch, source)
    assert status["runtime_ready"] is True
    assert all(status["contract"]["checks"].values())

    status = _contract_manifest(tmp_path, monkeypatch, "def load_stock(:\n    pass")
    assert status["runtime_ready"] is False
    assert status["contract"]["checks"]["runner_functions"] is False


def _stock() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({
        "refparcela": ["A", "B"], "cluster": ["BlocPluriP04", "VivUniP03"],
        "family": ["BlocPluri", "VivUni"], "period": ["P04", "P03"],
        "altura_max": [5, 2], "res_area_m2": [1000.0, 200.0],
        "imputed_floors": [False, False], "res_area_proxy": [False, False],
    }, geometry=[box(0, 0, 10, 10), box(12, 0, 18, 8)], crs="EPSG:25830")


def _map_data() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"refparcela": "A", "cluster": "BlocPluriP04"},
                "geometry": mapping(box(-0.4, 39.49, -0.399, 39.491)),
            },
            {
                "type": "Feature",
                "properties": {"refparcela": "B", "cluster": "VivUniP03"},
                "geometry": mapping(box(-0.398, 39.49, -0.397, 39.491)),
            },
        ],
    }


def _reps() -> pd.DataFrame:
    return pd.DataFrame([
        {"cluster": "BlocPluriP04", "family": "BlocPluri", "period": "P04", "refparcela": "A", "n_buildings": 1, "rep_area_m2": 100.0, "cluster_med_area_m2": 100.0, "rep_floors": 5, "cluster_med_floors": 5.0, "rep_vertices": 4},
        {"cluster": "VivUniP03", "family": "VivUni", "period": "P03", "refparcela": "B", "n_buildings": 1, "rep_area_m2": 48.0, "cluster_med_area_m2": 48.0, "rep_floors": 2, "cluster_med_floors": 2.0, "rep_vertices": 4},
    ])


def _record(rep: pd.Series) -> dict:
    return rep.to_dict() | {
        "qa_all_pass": True, "heating_kwh_m2": 10.0, "cooling_kwh_m2": 20.0,
        "s1_co2_kg_m2": 5.0, "s2_co2_kg_m2": 3.0, "eplus_warnings": 1,
        "param_wall_u": 1.0, "param_roof_u": 1.0, "param_window_u": 4.0,
        "param_window_g": 0.8, "param_ground_unconditioned": True,
    }


def test_neighborhood_adapter_isolates_cluster_failure_and_disables_scaling(tmp_path, monkeypatch):
    calls = []

    def run(_stock_value, rep, run_dir):
        calls.append(rep["cluster"])
        (run_dir / rep["cluster"]).mkdir()
        if rep["cluster"] == "BlocPluriP04":
            raise RuntimeError("EnergyPlus fixture failed")
        return _record(rep), True

    fake = SimpleNamespace(
        load_stock=_stock, select_representatives=lambda _value: _reps(),
        run_representative=run,
        scale_to_stock=lambda *_: (_ for _ in ()).throw(AssertionError("must not scale partial stock")),
        validate=lambda *_: "unused",
    )
    monkeypatch.setattr(adapter, "_part_c", lambda: fake)
    result = adapter.run_neighborhood(tmp_path, {"scope": "Benicalap"})
    assert calls == ["BlocPluriP04", "VivUniP03"]
    assert result["qa"]["scientific_status"] == "INVALID"
    assert result["summary"]["clusters_completed"] == 1
    assert result["summary"]["totals"] is None
    assert not (tmp_path / "results_buildings.gpkg").exists()
    assert (tmp_path / "representative_artifacts.zip").exists()
    assert not (tmp_path / "VivUniP03").exists()


def test_neighborhood_adapter_uses_real_scaler_output_for_totals_and_map(tmp_path, monkeypatch):
    stock = _stock()

    def scale(stock_value, results):
        output = stock_value.copy()
        metrics = results.set_index("cluster")
        for column in ("heating_kwh_m2", "cooling_kwh_m2", "s1_co2_kg_m2", "s2_co2_kg_m2"):
            output[column] = output["cluster"].map(metrics[column])
        output["heating_kwh"] = output.heating_kwh_m2 * output.res_area_m2
        output["cooling_kwh"] = output.cooling_kwh_m2 * output.res_area_m2
        output["s1_co2_t"] = output.s1_co2_kg_m2 * output.res_area_m2 / 1000
        output["s2_co2_t"] = output.s2_co2_kg_m2 * output.res_area_m2 / 1000
        return output

    fake = SimpleNamespace(
        load_stock=lambda: stock, select_representatives=lambda _value: _reps(),
        run_representative=lambda _stock_value, rep, run_dir: (
            (run_dir / rep["cluster"]).mkdir() or _record(rep), True,
        ),
        scale_to_stock=scale, validate=lambda _value: "VALIDATION FROM PART C",
    )
    monkeypatch.setattr(adapter, "_part_c", lambda: fake)
    result = adapter.run_neighborhood(tmp_path, {"scope": "Benicalap"})
    assert result["qa"]["scientific_status"] == "VALIDATED"
    assert result["summary"]["totals"]["heating_gwh_yr"] == 0.012
    assert result["validation"] == "VALIDATION FROM PART C"
    assert (tmp_path / "map.geojson").exists()


def test_neighborhood_adapter_preserves_single_building_domain_report(tmp_path, monkeypatch):
    calls = []

    def run_single(reference, run_dir, scenario=None):
        calls.append((reference, scenario))
        (run_dir / f"building_{reference}.txt").write_text(
            "SINGLE BUILDING RESULT\nQA all passed      : True\n", encoding="utf-8",
        )
        return 0

    fake = SimpleNamespace(run_single_building=run_single)
    monkeypatch.setattr(adapter, "_part_c", lambda: fake)
    result = adapter.run_single_building(tmp_path, {
        "scope_mode": "building", "building_ref": "4252702YJ2745A",
        "_domain_scenario": {"heat_delta": 1.0},
    })
    assert calls == [("4252702YJ2745A", {"heat_delta": 1.0})]
    assert result["qa"]["scientific_status"] == "VALIDATED"
    assert result["summary"]["buildings"] == 1
    assert result["single_building"]["report_text"].startswith("SINGLE BUILDING RESULT")
    assert json.loads((tmp_path / "results.json").read_text())["single_building"]["exit_code"] == 0


def test_neighborhood_service_validates_single_building_scope(monkeypatch):
    monkeypatch.setattr(neighborhood_service, "_require_ready", lambda **_kwargs: {"version": "test"})
    monkeypatch.setattr(db, "find_current_job", lambda _kind: None)
    monkeypatch.setattr(db, "create_job", lambda kind, scope, payload, timeout_seconds: "job-1")
    monkeypatch.setattr(db, "get_job", lambda job_id: {"id": job_id, "status": "queued"})
    monkeypatch.setattr(neighborhood_service.storage, "reserve_payload", lambda _kind, payload: payload)
    monkeypatch.setattr(neighborhood_service, "resolve_job_policy", lambda *_args, **_kwargs: {
        "schema_version": 1, "resolved_policy": {}, "dataset": {}, "policy_fingerprint": "test",
    })
    monkeypatch.setattr(neighborhood_service, "validate_request", lambda _request, scope: {
        "run_mode": "full_baseline", "scenario": None, "selected_weather": None,
    })
    with pytest.raises(ValueError, match="refparcela"):
        neighborhood_service.create_neighborhood_job({"scope_mode": "building"})
    job = neighborhood_service.create_neighborhood_job({
        "scope_mode": "building", "building_ref": "4252702YJ2745A",
    })
    assert job["id"] == "job-1"


def test_neighborhood_map_uses_unique_feature_ids_without_rewriting_cadastre_ids():
    stock = _stock()
    stock.loc[1, "refparcela"] = "A"
    data = adapter._stock_geojson(stock, _reps(), include_results=False)
    assert [feature["properties"]["refparcela"] for feature in data["features"]] == ["A", "A"]
    assert [feature["id"] for feature in data["features"]] == ["A::part-1", "A::part-2"]
    summary = neighborhood_map_cache.validate_feature_collection(data)
    assert summary["feature_count"] == 2
    assert summary["unique_refparcela_count"] == 1
    assert summary["duplicate_refparcela_count"] == 1


def test_neighborhood_map_cache_is_deterministic_validated_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    first = neighborhood_map_cache.cache_feature_collection(_map_data())
    second = neighborhood_map_cache.cache_feature_collection(_map_data())
    assert first == second
    assert first["feature_count"] == 2
    assert first["crs"] == "EPSG:4326"
    assert first["bounds"] == pytest.approx([-0.4, 39.49, -0.397, 39.491])
    path = neighborhood_map_cache.cached_map_path(first["fingerprint"])
    assert path.stat().st_size == first["size_bytes"]
    assert not list(path.parent.glob("*.tmp"))

    duplicate = _map_data()
    duplicate["features"][1]["properties"]["refparcela"] = "A"
    with pytest.raises(ValueError, match="duplicate feature id"):
        neighborhood_map_cache.cache_feature_collection(duplicate)
    with pytest.raises(ValueError, match="contains no buildings"):
        neighborhood_map_cache.cache_feature_collection({"type": "FeatureCollection", "features": []})
    point = _map_data()
    point["features"][0]["geometry"] = {"type": "Point", "coordinates": [-0.4, 39.49]}
    with pytest.raises(ValueError, match="Polygon or MultiPolygon"):
        neighborhood_map_cache.cache_feature_collection(point)


def test_neighborhood_preflight_cache_tracks_source_content(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    source = tmp_path / "city-source.json"
    source.write_text("first", encoding="utf-8")
    calls = []
    monkeypatch.setattr(neighborhood_service, "_require_ready", lambda: {
        "version": "1", "contract": {"runner_sha256": "r"}, "inspection": {"sha256": "a"},
    })
    monkeypatch.setattr(neighborhood_service, "resolve_job_policy", lambda *_args, **_kwargs: {
        "schema_version": 1, "resolved_policy": {}, "dataset": {}, "policy_fingerprint": "test",
    })
    monkeypatch.setattr(
        neighborhood_service, "source_paths",
        lambda: {"city_gis": (source, "gis")},
    )
    monkeypatch.setattr(
        neighborhood_service, "inspect_neighborhood",
        lambda **_kwargs: calls.append(source.read_text(encoding="utf-8")) or {
            "schema_version": 1, "summary": {"buildings": 2}, "representatives": [], "map": _map_data(),
        },
    )
    neighborhood_service.clear_preflight_cache()
    first = neighborhood_service.neighborhood_preflight(include_map=False)
    assert first["map"] is None
    assert first["map_descriptor"]["feature_count"] == 2
    assert calls == ["first"]
    neighborhood_service.neighborhood_preflight(include_map=False)
    assert calls == ["first"]

    source.write_text("other", encoding="utf-8")
    second = neighborhood_service.neighborhood_preflight(include_map=False)
    assert calls == ["first", "other"]
    assert second["map_descriptor"]["fingerprint"] == first["map_descriptor"]["fingerprint"]
    neighborhood_service.clear_preflight_cache()


def test_neighborhood_service_commits_an_immutable_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "var/workbench.sqlite3")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(neighborhood_service.service, "RUN_ROOT", tmp_path / "runs")
    service.RUN_ROOT.mkdir(parents=True)
    db.init_db()
    monkeypatch.setattr(neighborhood_service, "_require_ready", lambda: {"runtime_ready": True})
    monkeypatch.setattr(neighborhood_service, "_snapshot_inputs", lambda: ({}, {}))
    monkeypatch.setattr(neighborhood_service, "_verify_inputs_unchanged", lambda _manifest: None)

    result = {
        "schema_version": 1,
        "settings": {"scope": "Benicalap", "run_mode": "full_baseline", "method": "representative_typology_period", "capability_version": "1"},
        "summary": {"scope": "Benicalap", "buildings": 959, "clusters_expected": 18, "clusters_completed": 18, "clusters_failed": 0, "qa_passed_clusters": 18, "totals": {"heating_gwh_yr": 21.04}},
        "qa": {"all_pass": True, "scientific_status": "VALIDATED", "checks": [], "failures": []},
        "clusters": [], "representatives": [], "validation": "ok", "map_available": True,
    }

    def fake_run(run_dir, _settings):
        service.write_json(run_dir / "results.json", result)
        (run_dir / "map.geojson").write_text(json.dumps(_map_data()), encoding="utf-8")
        return result

    monkeypatch.setattr(neighborhood_service, "run_neighborhood", fake_run)
    job_id = db.create_job("neighborhood", "BENICALAP", result["settings"], timeout_seconds=1800)
    neighborhood_service.run_neighborhood_job(job_id)
    job = db.get_job(job_id)
    assert job["status"] == "completed"
    run = db.get_run(job["run_id"])
    assert run["run_type"] == "neighborhood"
    assert run["verification_status"] == "VERIFIED"
    assert Path(run["artifact_dir"], "manifest.json").exists()
    detail = neighborhood_service.neighborhood_detail(run["id"])
    assert detail["result"]["map_descriptor"]["feature_count"] == 2

    map_path = Path(run["artifact_dir"], "map.geojson")
    map_path.chmod(0o644)
    map_path.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError, match="TAMPERED"):
        neighborhood_service.neighborhood_map(run["id"])
