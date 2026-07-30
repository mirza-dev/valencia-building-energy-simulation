from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pandas as pd
import pytest
import mapbox_vector_tile
from shapely.geometry import box

from workbench import capabilities, db, service
from workbench import city_adapter as adapter
from workbench import city_service


PROJECT = Path(__file__).resolve().parents[1]


def _contract_manifest(tmp_path: Path, monkeypatch, runner_source: str) -> dict:
    runner = tmp_path / "city_pipeline.py"
    runner.write_text(runner_source, encoding="utf-8")
    fixture = json.loads((PROJECT / "tests/fixtures/city_smoke.json").read_text())
    fixture["runner"] = str(runner)
    fixture["runner_sha256"] = hashlib.sha256(runner.read_bytes()).hexdigest()
    fixture["stock_policy_sha256"] = hashlib.sha256(
        (PROJECT / fixture["stock_policy"]).read_bytes()
    ).hexdigest()
    fixture["adapter_sha256"] = hashlib.sha256(
        (PROJECT / "src/workbench/city_adapter.py").read_bytes()
    ).hexdigest()
    fixture_path = tmp_path / "city.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    manifest = json.loads((PROJECT / "workbench-capabilities.json").read_text())
    manifest["capabilities"]["city"]["fixture"] = str(fixture_path)
    manifest_path = tmp_path / "capabilities.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(capabilities, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        capabilities,
        "_neighborhood_contract",
        lambda *_args, **_kwargs: {"ok": True, "checks": {}},
    )
    return capabilities.capability_status()["capabilities"]["city"]


def test_city_capability_requires_part_d_hash_and_callable_contract(tmp_path, monkeypatch):
    source = "\n".join(f"def {name}():\n    pass\n" for name in adapter.REQUIRED_ENTRYPOINTS)
    status = _contract_manifest(tmp_path, monkeypatch, source)
    assert status["runtime_ready"] is True
    assert all(status["contract"]["checks"].values())

    status = _contract_manifest(tmp_path, monkeypatch, "def load_stock_city(:\n    pass")
    assert status["runtime_ready"] is False
    assert status["contract"]["checks"]["runner_functions"] is False

    runner = Path(status["contract"]["runner"])
    runner.write_text("def load_stock_city():\n    pass\n", encoding="utf-8")
    changed = capabilities.capability_status()["capabilities"]["city"]
    assert changed["runtime_ready"] is False
    assert changed["contract"]["checks"]["runner_hash"] is False


def _stock() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({
        "refparcela": ["A", "B"], "cluster": ["BlocPluriP04", "VivUniP03"],
        "family": ["BlocPluri", "VivUni"], "period": ["P04", "P03"],
        "altura_max": [5, 2], "res_area_m2": [1000.0, 200.0],
        "imputed_floors": [False, False], "res_area_proxy": [False, False],
        "dup_refparcela": [False, False], "nombre": ["DISTRICT A", "DISTRICT B"],
        "coddistrit": [1, 2], "demanda_ca": [20.0, 25.0],
    }, geometry=[box(0, 0, 10, 10), box(12, 0, 18, 8)], crs="EPSG:25830")


def _tile_coordinate(longitude: float, latitude: float, zoom: int) -> tuple[int, int]:
    scale = 2 ** zoom
    x = int((longitude + 180.0) / 360.0 * scale)
    y = int(
        (1.0 - math.asinh(math.tan(math.radians(latitude))) / math.pi)
        / 2.0
        * scale
    )
    return x, y


def test_city_tiles_aggregate_overview_and_keep_exact_footprints(tmp_path):
    longitude = -0.376
    latitude = 39.47
    rows = 12
    source = gpd.GeoDataFrame(
        {
            "refparcela": [f"P{index:02d}" for index in range(rows)],
            "cluster": ["BlocPluriP04"] * rows,
            "altura_max": [5] * rows,
            "nombre": ["L'EIXAMPLE"] * rows,
            "coddistrit": [2] * rows,
        },
        geometry=[
            box(
                longitude + index * 0.0002,
                latitude,
                longitude + index * 0.0002 + 0.0001,
                latitude + 0.0001,
            )
            for index in range(rows)
        ],
        crs="EPSG:4326",
    )
    path = tmp_path / "city.gpkg"
    source.to_file(path, driver="GPKG")
    mtime = path.stat().st_mtime_ns

    overview_x, overview_y = _tile_coordinate(longitude, latitude, 9)
    overview = mapbox_vector_tile.decode(
        service.city_vector_tile(9, overview_x, overview_y, str(path), mtime)
    )["overview"]["features"]
    assert len(overview) == 1
    assert overview[0]["properties"]["count"] == rows

    generalized_x, generalized_y = _tile_coordinate(longitude, latitude, 11)
    generalized = mapbox_vector_tile.decode(
        service.city_vector_tile(11, generalized_x, generalized_y, str(path), mtime)
    )["buildings"]["features"]
    assert len(generalized) == 1
    assert generalized[0]["geometry"]["type"] == "MultiPolygon"
    assert len(generalized[0]["geometry"]["coordinates"]) == rows
    assert generalized[0]["properties"]["cluster"] == "BlocPluriP04"

    exact_x, exact_y = _tile_coordinate(longitude, latitude, 14)
    exact = mapbox_vector_tile.decode(
        service.city_vector_tile(14, exact_x, exact_y, str(path), mtime)
    )["buildings"]["features"]
    assert len(exact) == rows
    assert {item["properties"]["refparcela"] for item in exact} == {
        f"P{index:02d}" for index in range(rows)
    }


def test_city_focus_view_excludes_sparse_outliers_without_hiding_stock(tmp_path):
    core = [
        box(-0.39 + index * 0.0001, 39.46, -0.38995 + index * 0.0001, 39.46005)
        for index in range(100)
    ]
    source = gpd.GeoDataFrame(
        {
            "refparcela": [f"C{index:03d}" for index in range(100)] + ["SOUTH"],
            "cluster": ["BlocPluriP04"] * 101,
            "altura_max": [5] * 101,
            "nombre": ["CORE"] * 100 + ["OUTLIER"],
            "coddistrit": [1] * 100 + [19],
        },
        geometry=core + [box(-0.28, 39.28, -0.2799, 39.2801)],
        crs="EPSG:4326",
    )
    path = tmp_path / "focus.gpkg"
    source.to_file(path, driver="GPKG")
    focus = service.city_focus_view(str(path), path.stat().st_mtime_ns)
    assert focus["coverage"] > 0.9
    assert focus["buildings"] < len(source)
    assert focus["bounds"][1] > 39.4


def _reps() -> pd.DataFrame:
    return pd.DataFrame([
        {"cluster": "BlocPluriP04", "family": "BlocPluri", "period": "P04", "refparcela": "A", "n_buildings": 1, "rep_area_m2": 100.0, "cluster_med_area_m2": 100.0, "rep_floors": 5, "cluster_med_floors": 5.0, "rep_vertices": 4},
        {"cluster": "VivUniP03", "family": "VivUni", "period": "P03", "refparcela": "B", "n_buildings": 1, "rep_area_m2": 48.0, "cluster_med_area_m2": 48.0, "rep_floors": 2, "cluster_med_floors": 2.0, "rep_vertices": 4},
    ])


def _record(rep: pd.Series) -> dict:
    return rep.to_dict() | {
        "qa_all_pass": True, "heating_kwh_m2": 10.0, "cooling_kwh_m2": 20.0,
        "s1_co2_kg_m2": 5.0, "s2_co2_kg_m2": 3.0, "eplus_warnings": 1,
        "cons_hc_kwh_m2": 12.0, "total_site_kwh_m2": 40.0,
        "s1_consumption_kwh_m2": 9.0, "s2_consumption_kwh_m2": 6.0,
        "hvac_co2_kg_m2": 4.0, "total_site_co2_kg_m2": 13.0,
        "qa_all_pass_hvac": True,
    }


def _scale(stock: gpd.GeoDataFrame, results: pd.DataFrame) -> gpd.GeoDataFrame:
    output = stock.copy()
    metrics = results.set_index("cluster")
    for column in (
        "heating_kwh_m2", "cooling_kwh_m2", "s1_co2_kg_m2", "s2_co2_kg_m2",
        "cons_hc_kwh_m2", "total_site_kwh_m2", "s1_consumption_kwh_m2",
        "s2_consumption_kwh_m2", "hvac_co2_kg_m2", "total_site_co2_kg_m2",
    ):
        output[column] = output["cluster"].map(metrics[column])
    output["heating_kwh"] = output.heating_kwh_m2 * output.res_area_m2
    output["cooling_kwh"] = output.cooling_kwh_m2 * output.res_area_m2
    output["s1_co2_t"] = output.s1_co2_kg_m2 * output.res_area_m2 / 1000
    output["s2_co2_t"] = output.s2_co2_kg_m2 * output.res_area_m2 / 1000
    output["cons_hc_kwh"] = output.cons_hc_kwh_m2 * output.res_area_m2
    output["total_site_kwh"] = output.total_site_kwh_m2 * output.res_area_m2
    output["s1_consumption_kwh"] = output.s1_consumption_kwh_m2 * output.res_area_m2
    output["s2_consumption_kwh"] = output.s2_consumption_kwh_m2 * output.res_area_m2
    output["hvac_co2_t"] = output.hvac_co2_kg_m2 * output.res_area_m2 / 1000
    output["total_site_co2_t"] = output.total_site_co2_kg_m2 * output.res_area_m2 / 1000
    return output


def _districts(buildings: gpd.GeoDataFrame) -> pd.DataFrame:
    heating = buildings.heating_kwh.sum() / 1e6
    cooling = buildings.cooling_kwh.sum() / 1e6
    hvac = buildings.cons_hc_kwh.sum() / 1e6
    total_site = buildings.total_site_kwh.sum() / 1e6
    return pd.DataFrame([
        {"nombre": f"DISTRICT {index + 1}", "coddistrit": index + 1,
         "n_buildings": len(buildings) if index == 0 else 0,
         "res_area_m2": buildings.res_area_m2.sum() if index == 0 else 0,
         "heating_gwh": heating if index == 0 else 0,
         "cooling_gwh": cooling if index == 0 else 0,
         "cons_hc_gwh": hvac if index == 0 else 0,
         "total_site_gwh": total_site if index == 0 else 0,
         "hvac_co2_t": buildings.hvac_co2_t.sum() if index == 0 else 0,
         "total_site_co2_t": buildings.total_site_co2_t.sum() if index == 0 else 0,
         "s1_co2_t": buildings.s1_co2_t.sum() if index == 0 else 0,
         "s2_co2_t": buildings.s2_co2_t.sum() if index == 0 else 0}
        for index in range(19)
    ])


def _fake_part_d(run):
    return SimpleNamespace(
        COL_DISTRICT_CODE="coddistrit", COL_DISTRICT_NAME="nombre",
        load_stock_city=_stock, district_breakdown=_districts,
        validate_city=lambda *_: "VALIDATION FROM PART D",
        nbp=SimpleNamespace(
            COL_CERT="demanda_ca", select_representatives=lambda _value: _reps(),
            run_representative=run, scale_to_stock=_scale,
        ),
    )


def test_city_adapter_isolates_cluster_failure_and_disables_city_scaling(tmp_path, monkeypatch):
    calls = []

    def run(_stock_value, rep, run_dir):
        calls.append(rep["cluster"])
        (run_dir / rep["cluster"]).mkdir()
        if rep["cluster"] == "BlocPluriP04":
            raise RuntimeError("EnergyPlus fixture failed")
        return _record(rep), True

    fake = _fake_part_d(run)
    fake.nbp.scale_to_stock = lambda *_: (_ for _ in ()).throw(AssertionError("must not scale partial city"))
    monkeypatch.setattr(adapter, "_part_d", lambda: fake)
    result = adapter.run_city(tmp_path, {"scope": "Valencia"})
    assert calls == ["BlocPluriP04", "VivUniP03"]
    assert result["qa"]["scientific_status"] == "INVALID"
    assert result["summary"]["clusters_completed"] == 1
    assert result["summary"]["totals"] is None
    assert not (tmp_path / "results_buildings.gpkg").exists()
    assert (tmp_path / "representative_artifacts.zip").exists()


def test_city_adapter_uses_real_part_d_district_and_validation_outputs(tmp_path, monkeypatch):
    def run(_stock_value, rep, run_dir):
        (run_dir / rep["cluster"]).mkdir()
        return _record(rep), True

    monkeypatch.setattr(adapter, "_part_d", lambda: _fake_part_d(run))
    monkeypatch.setattr(adapter.importlib, "import_module", lambda _name: SimpleNamespace(
        render_heatmap=lambda _source, target: target.write_bytes(b"png"),
    ))
    result = adapter.run_city(tmp_path, {"scope": "Valencia"})
    assert result["qa"]["scientific_status"] == "VALIDATED"
    assert result["summary"]["districts"] == 19
    assert result["summary"]["totals"]["heating_gwh_yr"] == 0.012
    assert result["summary"]["totals"]["hvac_consumption_gwh_yr"] == pytest.approx(0.0144)
    assert result["summary"]["totals"]["total_site_gwh_yr"] == pytest.approx(0.048)
    assert result["summary"]["certificate_heating"]["ratio"] == pytest.approx(0.48)
    assert result["validation"] == "VALIDATION FROM PART D"
    assert (tmp_path / "districts.csv").exists()
    assert (tmp_path / "map_metrics.json").exists()


def test_city_service_commits_an_immutable_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "var/workbench.sqlite3")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(city_service.service, "RUN_ROOT", tmp_path / "runs")
    service.RUN_ROOT.mkdir(parents=True)
    db.init_db()
    monkeypatch.setattr(city_service, "_require_ready", lambda: {"runtime_ready": True})
    monkeypatch.setattr(city_service, "_snapshot_inputs", lambda: ({}, {}))
    monkeypatch.setattr(city_service, "_verify_inputs_unchanged", lambda _manifest: None)
    result = {
        "schema_version": 1,
        "settings": {"scope": "Valencia", "run_mode": "full_baseline"},
        "summary": {"scope": "Valencia", "buildings": 26452, "districts": 19,
                    "clusters_expected": 21, "clusters_completed": 21,
                    "clusters_failed": 0, "qa_passed_clusters": 21,
                    "totals": {"heating_gwh_yr": 596.41}},
        "qa": {"all_pass": True, "scientific_status": "VALIDATED", "checks": [], "failures": []},
        "clusters": [], "representatives": [], "districts": [], "validation": "ok",
        "map_available": True, "energy_map_available": True,
    }

    def fake_run(run_dir, _settings):
        service.write_json(run_dir / "results.json", result)
        service.write_json(run_dir / "map_metrics.json", {"clusters": [], "districts": []})
        return result

    monkeypatch.setattr(city_service, "run_city", fake_run)
    job_id = db.create_job("city", "VALENCIA", result["settings"], timeout_seconds=2400)
    city_service.run_city_job(job_id)
    job = db.get_job(job_id)
    assert job["status"] == "completed"
    run = db.get_run(job["run_id"])
    assert run["run_type"] == "city"
    assert run["verification_status"] == "VERIFIED"
    assert Path(run["artifact_dir"], "manifest.json").exists()
