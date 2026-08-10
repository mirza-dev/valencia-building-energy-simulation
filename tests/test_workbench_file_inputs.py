"""The Files page contracts: uploaded files are verified and reach stock_runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench import db, file_inputs, stock_adapter
from workbench.api import app


PROJECT = Path(__file__).resolve().parents[1]
EPW = PROJECT / "data/weather/ESP_Valencia.082840_IWEC.epw"
DDY = PROJECT / "data/weather/ESP_Valencia.082840_IWEC.ddy"


def _tipo15_bytes() -> bytes:
    return (
        "31_pc;252_planta;428_uso;442_sup_Residencial\n"
        "TEST0001;B0;V;82.5\n"
        "TEST0001;01;V;76.0\n"
    ).encode("latin-1")


def test_tipo15_contract_reports_join_and_area_evidence(tmp_path: Path):
    path = tmp_path / "tipo15.csv"
    path.write_bytes(_tipo15_bytes())
    report = file_inputs.inspect_tipo15(path)
    assert report["rows"] == 2
    assert report["unique_parcels"] == 1
    assert report["residential_area_m2"] == 158.5


def test_climate_pair_chooses_the_verified_annual_design_days(tmp_path: Path):
    bundle, record = file_inputs.build_climate_bundle(EPW, DDY, tmp_path)
    assert bundle.exists()
    assert record["heating_design_day"] == "VALENCIA Ann Htg 99.6% Condns DB"
    assert record["cooling_design_day"] == "VALENCIA Ann Clg .4% Condns DB=>MWB"
    assert record["cross_check"] == "epw_design_conditions"


def test_uploading_the_reference_pair_keeps_one_resume_identity(tmp_path: Path):
    """The same two files must not become two climates.

    `climate_fingerprint` is one of the runner's resume identity fields and it
    hashes the bundle name alongside the two file digests.  If an upload of the
    pair this project ships were named differently, a run started from the
    Files page could not resume one produced from the project's own bundle -
    including the finished Benicalap run shipped with the product.
    """
    import climate

    reference = climate.valencia_iwec()
    bundle, _ = file_inputs.build_climate_bundle(EPW, DDY, tmp_path)
    managed = climate.load_climate(bundle)

    assert managed.fingerprint == reference.fingerprint
    # Provenance is still the activated copy, not the project's own file.
    assert json.loads(bundle.read_text(encoding="utf-8"))["ddy"] == str(DDY.resolve())


def test_a_climate_we_do_not_ship_is_named_after_its_own_site(tmp_path: Path):
    import climate

    spec = file_inputs.climate_bundle_spec(EPW, DDY)
    assert spec["name"] == climate.valencia_iwec().name

    other_ddy = tmp_path / "other.ddy"
    other_ddy.write_text(DDY.read_text(encoding="latin-1") + "\n! edited\n", encoding="latin-1")
    # No longer the pair this project was verified on, so the site temperatures
    # have to be stated rather than inherited - see the declaration tests below.
    other = file_inputs.climate_bundle_spec(
        EPW, other_ddy, ground_temperature_c=18.0)
    assert other["name"].startswith("managed_")


LECCO_STOCK = PROJECT / "data/gis/lecco/lecco_stock_v2.gpkg"
LECCO_EPW = PROJECT / "data/weather/lecco/ITA_LM_Milano-Bergamo.Intl.AP.160760_TMYx.2009-2023.epw"
LECCO_DDY = PROJECT / "data/weather/lecco/ITA_LM_Milano-Bergamo.Intl.AP.160760_TMYx.2009-2023.ddy"


def _stock_frame(**overrides):
    """A minimal contract-satisfying stock, in metres, belonging to no country."""
    import geopandas as gpd
    from shapely.geometry import Polygon

    columns = {
        "refparcela": ["A1", "A2"],
        "altura_max": [3, 4],
        "pob_total": [8, 11],
        "num_vivend": [4, 5],
        "wall_u": [1.1, 1.2],
        "roof_u": [1.8, 1.9],
        "window_u": [3.2, 3.2],
    }
    columns.update(overrides)
    geometry = [
        Polygon([(0, 0), (14, 0), (14, 14), (0, 14)]),
        Polygon([(30, 0), (44, 0), (44, 14), (30, 14)]),
    ]
    return gpd.GeoDataFrame(columns, geometry=geometry, crs="EPSG:32633")


def _write_stock(tmp_path: Path, name: str = "stock.gpkg", **overrides) -> Path:
    path = tmp_path / name
    _stock_frame(**overrides).to_file(path, driver="GPKG")
    return path


@pytest.mark.skipif(not LECCO_STOCK.exists(), reason="Lecco stock not present")
def test_a_foreign_stock_is_accepted_on_its_own_terms():
    """Lecco carries a pinned envelope and no Spanish district column."""
    report = file_inputs.inspect_stock(LECCO_STOCK)
    assert report["contract"] == "stock-v1"
    assert report["envelope_source"] == "pinned"
    assert report["has_district_column"] is False
    assert report["crs"] == "EPSG:32632"


def test_the_valencia_cadastre_still_satisfies_the_same_contract():
    report = file_inputs.inspect_stock(PROJECT / "data/gis/DatosRai_ciudadValencia.shp")
    assert report["envelope_source"] == "tabula_es"
    assert report["has_district_column"] is True


def test_a_stock_in_degrees_is_refused_rather_than_silently_empty(tmp_path: Path):
    """Areas in square degrees fail the footprint gate for every building.

    Without this check the run reports "no usable buildings in this city",
    which reads as a data problem rather than as a projection mistake.
    """
    path = tmp_path / "degrees.gpkg"
    _stock_frame().to_crs("EPSG:4326").to_file(path, driver="GPKG")
    with pytest.raises(ValueError, match="planar measurements in metres"):
        file_inputs.inspect_stock(path)


def test_a_half_pinned_envelope_is_refused(tmp_path: Path):
    """This is the exact path to Spanish walls on a building outside Spain."""
    frame = _stock_frame().drop(columns=["window_u"])
    path = tmp_path / "half.gpkg"
    frame.to_file(path, driver="GPKG")
    with pytest.raises(ValueError, match="Spanish TABULA table"):
        file_inputs.inspect_stock(path)


def test_a_stock_stating_no_envelope_source_at_all_is_refused(tmp_path: Path):
    frame = _stock_frame().drop(columns=["wall_u", "roof_u", "window_u"])
    path = tmp_path / "bare.gpkg"
    frame.to_file(path, driver="GPKG")
    with pytest.raises(ValueError, match="neither a usable 'cluster'"):
        file_inputs.inspect_stock(path)


def test_a_stock_missing_an_engine_field_names_it(tmp_path: Path):
    frame = _stock_frame().drop(columns=["num_vivend"])
    path = tmp_path / "short.gpkg"
    frame.to_file(path, driver="GPKG")
    with pytest.raises(ValueError, match="num_vivend"):
        file_inputs.inspect_stock(path)


@pytest.mark.skipif(not LECCO_EPW.exists(), reason="Lecco climate not present")
def test_mains_temperature_is_derived_from_the_uploaded_weather_file():
    """The number must come out of this city's own EPW, not out of Valencia.

    13.7 C is the value the hand-written Lecco bundle states, derived by this
    method; reproducing it proves the derivation rather than asserting it.
    """
    proposal = file_inputs.propose_site_temperatures(LECCO_EPW)
    assert proposal["water_mains_temperature_c"] == 13.7
    assert proposal["undisturbed_ground_c"]["2.0"] == 13.66
    # Not derivable from weather at all: it follows the indoor regime.
    assert proposal["ground_temperature_c"] is None


@pytest.mark.skipif(not LECCO_EPW.exists(), reason="Lecco climate not present")
def test_a_new_city_cannot_inherit_the_verified_ground_temperature_silently():
    with pytest.raises(ValueError, match="ground_temperature_c must be declared"):
        file_inputs.climate_bundle_spec(LECCO_EPW, LECCO_DDY)

    declared = file_inputs.climate_bundle_spec(
        LECCO_EPW, LECCO_DDY, ground_temperature_c=18.0)
    assert declared["ground_temperature_c"] == 18.0
    assert declared["water_mains_temperature_c"] == 13.7
    assert declared["name"].startswith("managed_")


def test_the_reference_pair_keeps_its_verified_site_temperatures():
    """Deriving these for Valencia would break every published run.

    This EPW's own 2 m ground temperature averages 17.2 C.  Writing that into a
    re-upload of the pair the project was verified on would change
    `climate_fingerprint`, and the shipped Benicalap run could then neither
    resume nor compare against an uploaded copy of its own climate.
    """
    assert file_inputs.propose_site_temperatures(EPW)["water_mains_temperature_c"] == 17.2

    spec = file_inputs.climate_bundle_spec(EPW, DDY)
    assert spec["water_mains_temperature_c"] == 10.0
    assert spec["ground_temperature_c"] == 18.0


def test_tipo15_and_ddy_uploads_are_verified_and_activatable():
    with TestClient(app) as client:
        original = client.get("/api/project/settings").json()
        tipo15 = client.post(
            "/api/datasets", data={"kind": "tipo15", "name": "Test Tipo15"},
            files={"file": ("tipo15.csv", _tipo15_bytes(), "text/csv")},
        )
        assert tipo15.status_code == 200, tipo15.text
        tipo15_item = tipo15.json()
        assert tipo15_item["verification_status"] == "VERIFIED"
        assert tipo15_item["metadata"]["contract"] == "tipo15-v1"

        ddy = client.post(
            "/api/datasets", data={"kind": "ddy", "name": "Valencia DDY"},
            files={"file": (DDY.name, DDY.read_bytes(), "text/plain")},
        )
        assert ddy.status_code == 200, ddy.text
        ddy_item = ddy.json()
        assert ddy_item["metadata"]["design_day_count"] >= 2

        activated = client.patch("/api/project/settings", json={
            "tipo15_dataset_id": tipo15_item["id"],
            "ddy_dataset_id": ddy_item["id"],
        })
        assert activated.status_code == 200, activated.text
        settings = activated.json()
        assert settings["tipo15_dataset_id"] == tipo15_item["id"]
        assert settings["ddy_dataset_id"] == ddy_item["id"]

        inputs = stock_adapter.default_inputs()
        assert inputs.tipo15 == Path(tipo15_item["path"])
        assert inputs.climate is not None and inputs.climate.exists()
        spec = json.loads(inputs.climate.read_text(encoding="utf-8"))
        assert Path(spec["ddy"]) == Path(ddy_item["path"])
        restored = client.patch("/api/project/settings", json={
            "tipo15_dataset_id": original["tipo15_dataset_id"],
            "ddy_dataset_id": original["ddy_dataset_id"],
        })
        assert restored.status_code == 200, restored.text


def test_bad_tipo15_is_rejected_before_registration():
    with TestClient(app) as client:
        response = client.post(
            "/api/datasets", data={"kind": "tipo15", "name": "Broken"},
            files={"file": ("broken.csv", b"wrong;columns\n1;2\n", "text/csv")},
        )
    assert response.status_code == 422
    assert "Tipo15" in response.json()["detail"]


def test_stock_subprocess_receives_every_active_input(monkeypatch, tmp_path: Path):
    climate_path, _ = file_inputs.build_climate_bundle(EPW, DDY, tmp_path / "climates")
    tipo15 = tmp_path / "tipo15.csv"
    tipo15.write_bytes(_tipo15_bytes())
    inputs = stock_adapter.InputSet(
        gis=PROJECT / "data/gis/DatosRai_ciudadValencia.shp",
        tipo15=tipo15,
        climate=climate_path,
        template=PROJECT / "data/templates/PlantillaOS_v2.osm",
    )
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path / "stock")

    observed: dict[str, object] = {}

    class Process:
        pid = 12345

    def fake_popen(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(stock_adapter.subprocess, "Popen", fake_popen)
    stock_adapter.start_run(
        "input-proof", "references", references=["TEST0001"], inputs=inputs,
        log_dir=tmp_path / "logs",
    )
    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--climate") + 1] == str(climate_path)
    assert argv[argv.index("--template") + 1].endswith("PlantillaOS_v2.osm")
