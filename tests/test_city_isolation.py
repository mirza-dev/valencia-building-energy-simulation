"""No city may inherit Valencia's data.

Valencia was the first city this engine was built and verified against, so its
files, its envelope table and its site constants sit at the bottom of several
defaults.  Every one of those is a path by which another city's results could
silently contain a Valencia number.  The point of this file is that "we removed
the leaks" is not a claim anyone has to take on trust: each test below fails if
a leak comes back.

The strongest test here is `test_a_foreign_city_runs_with_the_valencia_files_
hidden`.  Reading the code can miss a default; running with the files absent
cannot.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
LECCO_STOCK = PROJECT / "data/gis/lecco/lecco_stock_v2.gpkg"
LECCO_CLIMATE = PROJECT / "climates/lecco_bergamo_tmyx.json"

# What Valencia contributes that must never reach another city.
VALENCIA_GIS = PROJECT / "data/gis/DatosRai_ciudadValencia.shp"
VALENCIA_TIPO15 = PROJECT / "data/reference/Tipo15_soloV(in).csv"
VALENCIA_EPW = PROJECT / "data/weather/ESP_Valencia.082840_IWEC.epw"

requires_lecco = pytest.mark.skipif(
    not (LECCO_STOCK.exists() and LECCO_CLIMATE.exists()),
    reason="the second city's data is not present in this checkout",
)


# ---------------------------------------------------------------------------
# The site constants
# ---------------------------------------------------------------------------
@requires_lecco
def test_site_conditions_come_from_the_uploaded_climate_not_from_valencia():
    """`deep_building` falls back to Valencia for all of these when climate is None.

    Design days, barometric pressure, ground temperature and mains temperature
    are Valencia's verified values in that fallback.  A climate bundle replaces
    every one of them, which is why the Workbench path refuses to start a run
    without one.
    """
    import climate as cl
    import deep_building as db

    lecco = cl.load_climate(LECCO_CLIMATE)

    assert lecco.barometric_pressure_pa != db.SITE_BAROMETRIC_PRESSURE_PA
    assert lecco.water_mains_temperature_c != db.WATER_MAINS_TEMPERATURE_C
    assert set(lecco.design_days) == set(db.RAI_DESIGN_DAYS)
    for kind, day in lecco.design_days.items():
        assert "VALENCIA" not in str(day.get("name", "")).upper(), kind
        assert day != db.RAI_DESIGN_DAYS[kind]


@requires_lecco
def test_a_run_without_a_climate_is_refused_rather_than_defaulted():
    from workbench import stock_adapter

    inputs = stock_adapter.InputSet(
        stock=LECCO_STOCK, template=PROJECT / "data/templates/PlantillaOS_v2.osm",
        climate=None,
    )
    assert "climate" in inputs.missing()
    with pytest.raises(ValueError, match="belong.*to Valencia"):
        stock_adapter.start_run("isolation-probe", "references",
                                references=["1"], inputs=inputs)


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------
@requires_lecco
def test_a_pinned_envelope_keeps_the_spanish_table_out_of_it():
    """The Spanish IVE table must not price an Italian building.

    `config_for_building` consults `TABULA_ES` for any building whose envelope
    is not already pinned.  A stock that states its own U-values is pinning it,
    and this proves the pin survives all the way to the config the engine gets.
    """
    import geopandas as gpd
    import model_config as mc
    import stock_runner as sr

    stock = gpd.read_file(LECCO_STOCK)
    envelopes = sr.envelopes_by_reference(stock)
    assert envelopes, "the second city states its own envelope"

    reference = next(iter(envelopes))
    sr._WORKER.clear()
    sr._WORKER.update({"config": None, "envelopes": envelopes})
    try:
        config = sr._config_for(reference)
    finally:
        sr._WORKER.clear()

    assert config.envelope.wall_u == pytest.approx(envelopes[reference]["wall_u"])
    assert config.envelope.roof_u == pytest.approx(envelopes[reference]["roof_u"])

    spanish_walls = {values["wall_u"] for values in mc.TABULA_ES.values()}
    pinned_walls = {round(float(v["wall_u"]), 6) for v in envelopes.values()}
    assert not (pinned_walls & {round(float(u), 6) for u in spanish_walls}), (
        "a pinned envelope that coincides with the Spanish table would make this "
        "test unable to tell the two apart"
    )


def test_the_cadastre_path_still_uses_the_spanish_table():
    """The other half of the same guarantee: Valencia must not lose its table."""
    import geopandas as gpd
    import stock_runner as sr

    stock = gpd.read_file(VALENCIA_GIS, rows=50)
    assert sr.envelopes_by_reference(stock) == {}, (
        "the cadastre states no U-values, so the cluster/TABULA_ES route must "
        "stay the one Valencia was verified on"
    )


# ---------------------------------------------------------------------------
# The outputs
# ---------------------------------------------------------------------------
@requires_lecco
def test_the_reference_comparison_is_absent_for_another_city_not_zero():
    """A missing comparison must read as missing, not as a measured zero."""
    import stock_runner as sr

    rows = [{
        "refparcela": "L1", "status": "ok", "cluster": "AB_YBET:1946-1969",
        "res_area_m2": 1000.0, "total_site_kwh_m2": 42.24,
        "space_heating_kwh_m2": 7.73, "cooling_kwh_m2": 1.14, "dhw_kwh_m2": 3.66,
        "total_site_co2_kg_m2": 9.0, "seconds": 1.0,
    }]
    report = sr.aggregate(rows)
    block = report["by_cluster"][0]
    assert "rai_consume_kwh_m2" not in block
    assert block["cluster"] == "AB_YBET:1946-1969"


@requires_lecco
def test_an_aggregate_survives_a_stock_that_names_no_districts():
    """Lecco has no `nombre` column; demanding one refused the whole aggregate."""
    import geopandas as gpd
    import stock_runner as sr

    stock = gpd.read_file(LECCO_STOCK)
    assert "nombre" not in stock.columns
    rows = [{
        "refparcela": str(stock["refparcela"].iloc[0]), "status": "ok",
        "res_area_m2": 1000.0, "total_site_kwh_m2": 42.24,
        "space_heating_kwh_m2": 7.73, "cooling_kwh_m2": 1.14, "dhw_kwh_m2": 3.66,
        "total_site_co2_kg_m2": 9.0, "seconds": 1.0,
    }]
    report = sr.aggregate(rows, stock)
    assert report["by_district"] == []
    assert report["totals"]["area_weighted_total_site_kwh_m2"] == pytest.approx(42.24)


@requires_lecco
def test_a_stock_without_a_district_column_offers_no_district_scope():
    from workbench import stock_adapter

    options = stock_adapter.district_options(
        stock_adapter.InputSet(stock=LECCO_STOCK))
    assert options == []


# ---------------------------------------------------------------------------
# The blind test
# ---------------------------------------------------------------------------
@requires_lecco
@pytest.mark.slow
def test_a_foreign_city_runs_with_the_valencia_files_hidden(tmp_path: Path):
    """Run a second city in a tree where Valencia's data cannot be opened.

    Every default that would quietly reach for `DatosRai_ciudadValencia.shp`,
    the Tipo15 ledger or the Valencia EPW raises here instead of succeeding, so
    a leak becomes a failed run rather than a plausible-looking number.  Reading
    the code for defaults can miss one; this cannot.

    The template is deliberately still shared: running the Spanish CTE template
    is a stated modelling choice, not a data leak, and it is documented as such
    wherever these results are reported.
    """
    import builtins

    import stock_runner as sr

    forbidden = tuple(str(p.resolve()) for p in
                      (VALENCIA_GIS, VALENCIA_TIPO15, VALENCIA_EPW))
    opened: list[str] = []
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        text = str(file)
        for banned in forbidden:
            if text.startswith(banned) or text.startswith(banned.rsplit(".", 1)[0]):
                opened.append(text)
                raise AssertionError(
                    f"a run of another city opened a Valencia file: {text}")
        return real_open(file, *args, **kwargs)

    out_dir = tmp_path / "run"
    builtins.open = guarded_open
    try:
        report = sr.run_stock(
            scope="references",
            references=[_first_runnable_reference()],
            out_dir=out_dir, workers=1, keep="summary",
            stock_path=LECCO_STOCK, var_dir=tmp_path / "var",
            climate_path=LECCO_CLIMATE,
        )
    finally:
        builtins.open = real_open

    assert not opened
    assert report["buildings_ok"] == 1, report

    # And the numbers that came out are this city's, not the other one's.
    layers = json.loads(
        (out_dir / "runs" / f"{_first_runnable_reference()}_deep"
         / "deep_layers.json").read_text(encoding="utf-8"))
    weather = json.dumps(layers["layers"].get("weather", {}))
    assert "Valencia" not in weather and "VALENCIA" not in weather


# ---------------------------------------------------------------------------
# The third city
# ---------------------------------------------------------------------------
def _synthetic_city(tmp_path: Path) -> Path:
    """A stock belonging to no country this project has ever run.

    Lecco working only proves Lecco works.  This one is built to the contract
    and to nothing else: a projection neither city uses, no district column, no
    Spanish cluster names, an envelope stated outright.  Anything that quietly
    assumes "Valencia or Lecco" fails here rather than in front of a user.
    """
    import geopandas as gpd
    from shapely.geometry import Polygon

    frame = gpd.GeoDataFrame(
        {
            "refparcela": ["N-001", "N-002", "N-003"],
            "altura_max": [3, 2, 4],
            "pob_total": [14.0, 6.0, 22.0],
            "num_vivend": [6.0, 3.0, 9.0],
            "cluster": ["north_block_1960", "north_block_1960", "north_tower_1980"],
            "ground_use": ["terciario", "terciario", "terciario"],
            "wall_u": [0.92, 0.92, 0.61],
            "roof_u": [0.78, 0.78, 0.44],
            "window_u": [2.80, 2.80, 1.90],
            "floor_u": [0.85, 0.85, 0.52],
        },
        geometry=[
            Polygon([(0, 0), (18, 0), (18, 16), (0, 16)]),
            Polygon([(40, 0), (54, 0), (54, 14), (40, 14)]),
            Polygon([(0, 40), (22, 40), (22, 58), (0, 58)]),
        ],
        # UTM 33N: neither Valencia's 25830 nor Lecco's 32632.
        crs="EPSG:32633",
    )
    path = tmp_path / "northland_stock.gpkg"
    frame.to_file(path, driver="GPKG")
    return path


def test_a_third_city_satisfies_the_contract_with_no_special_casing(tmp_path: Path):
    from workbench import file_inputs

    report = file_inputs.inspect_stock(_synthetic_city(tmp_path))
    assert report["contract"] == "stock-v1"
    assert report["envelope_source"] == "pinned"
    assert report["has_district_column"] is False
    assert report["buildings"] == 3


def test_a_third_city_scopes_and_screens_without_a_district_column(tmp_path: Path):
    import geopandas as gpd
    import stock_runner as sr
    from workbench import stock_adapter

    path = _synthetic_city(tmp_path)
    stock = gpd.read_file(path)

    runnable, excluded = sr.screen_geometry(stock)
    assert len(runnable) == 3, excluded
    assert sr.envelopes_by_reference(stock).keys() == {"N-001", "N-002", "N-003"}
    assert stock_adapter.district_options(stock_adapter.InputSet(stock=path)) == []
    assert len(sr.select_scope(stock, "clusters")) == 2      # one per cluster


@pytest.mark.slow
def test_a_third_city_runs_end_to_end(tmp_path: Path):
    """The whole point: a city nobody wrote code for still simulates."""
    import stock_runner as sr

    report = sr.run_stock(
        scope="references", references=["N-002"],
        out_dir=tmp_path / "run", workers=1, keep="summary",
        stock_path=_synthetic_city(tmp_path), var_dir=tmp_path / "var",
        climate_path=LECCO_CLIMATE if LECCO_CLIMATE.exists() else None,
    )
    assert report["buildings_ok"] == 1, report
    assert report["by_district"] == []
    assert all("rai_consume_kwh_m2" not in block for block in report["by_cluster"])
    assert report["totals"]["area_weighted_total_site_kwh_m2"] > 0


def _first_runnable_reference() -> str:
    import geopandas as gpd
    import stock_runner as sr

    stock = gpd.read_file(LECCO_STOCK)
    runnable, _ = sr.screen_geometry(stock.head(30))
    assert runnable, "no runnable building in the sample"
    return runnable[0]
