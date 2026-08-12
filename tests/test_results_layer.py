"""The results GIS layer: one feature per building, gaps visible, never fatal.

The property that matters most is the first one under test.  A cadastral
reference can hold several footprint polygons, and a layer that repeats the
building's energy once per polygon looks correct on a map while making every
total taken off it wrong by the multiplicity.  That is the failure this layer
exists to avoid, so it is the one pinned hardest here.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

import results_layer as rl


def _stock() -> gpd.GeoDataFrame:
    """SPLIT is one reference drawn as two touching footprints."""
    return gpd.GeoDataFrame(
        {"refparcela": ["ONE", "SPLIT", "SPLIT", "GONE"]},
        geometry=[box(0, 0, 10, 10), box(100, 0, 110, 10),
                  box(110, 0, 120, 10), box(300, 0, 310, 10)],
        crs="EPSG:25830",
    )


def _rows() -> list[dict]:
    return [
        {"refparcela": "ONE", "status": "ok", "cluster": "BlocPluriP04",
         "total_site_kwh_m2": 50.0, "res_area_m2": 100.0,
         "total_site_kwh": 5000.0, "qa_all_passed": True},
        {"refparcela": "SPLIT", "status": "ok", "cluster": "BlocPluriP04",
         "total_site_kwh_m2": 40.0, "res_area_m2": 200.0,
         "total_site_kwh": 8000.0, "qa_all_passed": True},
        {"refparcela": "GONE", "status": "excluded",
         "reason": "footprint outside range"},
        {"refparcela": "NOWHERE", "status": "failed", "reason": "ValueError",
         "traceback": "x" * 900},
    ]


def _read(path: Path) -> gpd.GeoDataFrame:
    return gpd.read_file(path, layer=rl.LAYER_NAME)


def test_a_multi_footprint_reference_becomes_one_feature(tmp_path):
    """The energy of a split parcel must not be counted twice."""
    block = rl.write_results_layer(_rows(), _stock(), tmp_path)
    assert block["written"] is True
    layer = _read(tmp_path / rl.LAYER_FILENAME)

    assert list(layer["refparcela"]).count("SPLIT") == 1
    assert block["footprints_dissolved"] == 1
    # the parcel keeps both footprints' area, it just carries one row
    split = layer.loc[layer["refparcela"] == "SPLIT"].iloc[0]
    assert split.geometry.area == pytest.approx(200.0)
    # and the total a reader computes off the layer is the true one
    assert float(layer["total_site_kwh"].sum()) == pytest.approx(13000.0)


def test_failed_and_excluded_buildings_are_drawn_with_null_energy(tmp_path):
    """A coverage gap has to be visible on the map, not absent from it."""
    rl.write_results_layer(_rows(), _stock(), tmp_path)
    layer = _read(tmp_path / rl.LAYER_FILENAME)

    gone = layer.loc[layer["refparcela"] == "GONE"].iloc[0]
    assert gone["status"] == "excluded"
    assert gone["reason"] == "footprint outside range"
    # NULL, not zero: zero would render as a real, very efficient building
    assert gone["total_site_kwh_m2"] is None or gone["total_site_kwh_m2"] != gone["total_site_kwh_m2"]


def test_a_row_without_geometry_is_counted_not_silently_dropped(tmp_path):
    block = rl.write_results_layer(_rows(), _stock(), tmp_path)
    assert block["rows_without_geometry"] == 1          # NOWHERE
    assert "no footprint" in block["note"]
    assert block["features"] == 3
    assert block["ledger_rows"] == 4


def test_bulk_and_path_fields_stay_out_of_the_layer(tmp_path):
    rl.write_results_layer(_rows(), _stock(), tmp_path)
    layer = _read(tmp_path / rl.LAYER_FILENAME)
    for field in rl.EXCLUDED_FIELDS:
        assert field not in layer.columns


def test_without_a_prepared_stock_it_skips_instead_of_raising(tmp_path):
    """`--aggregate` on a bare ledger is supported; it just cannot draw a map."""
    block = rl.write_results_layer(_rows(), None, tmp_path)
    assert block["written"] is False
    assert block["reason"] == "no_stock_geometry"
    assert not (tmp_path / rl.LAYER_FILENAME).exists()


def test_an_empty_ledger_says_so_rather_than_writing_an_empty_map(tmp_path):
    block = rl.write_results_layer([], _stock(), tmp_path)
    assert block == {"written": False, "layer": rl.LAYER_FILENAME,
                     "reason": "no_rows"}


def test_a_retried_building_keeps_its_last_outcome(tmp_path):
    """The ledger is append-only; the layer shows the row that stands."""
    rows = [
        {"refparcela": "ONE", "status": "failed", "reason": "ValueError"},
        {"refparcela": "ONE", "status": "ok", "total_site_kwh_m2": 50.0},
    ]
    rl.write_results_layer(rows, _stock(), tmp_path)
    layer = _read(tmp_path / rl.LAYER_FILENAME)
    assert len(layer) == 1
    assert layer.iloc[0]["status"] == "ok"


def test_the_projection_survives_the_join(tmp_path):
    """Javier's own layer is in the stock's CRS; a reprojection here would
    silently misalign the two."""
    block = rl.write_results_layer(_rows(), _stock(), tmp_path)
    assert block["crs"] == "EPSG:25830"
    assert _read(tmp_path / rl.LAYER_FILENAME).crs.to_string() == "EPSG:25830"


def test_a_partial_write_never_keeps_the_expected_name(tmp_path, monkeypatch):
    """The export packs whatever is in the run directory, so a truncated file
    under the real name would ship as if it were the result."""
    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(gpd.GeoDataFrame, "to_file", explode)
    block = rl.write_results_layer(_rows(), _stock(), tmp_path)
    assert block["written"] is False
    assert block["reason"].startswith("write_failed")
    assert not (tmp_path / rl.LAYER_FILENAME).exists()


def test_the_runner_records_the_layer_in_its_aggregate(tmp_path):
    """`aggregate.json` must say whether a map exists, so a reader who never
    opens the directory still knows what the package contains."""
    import stock_runner as sr

    # `aggregate()` reads a wider set of columns than the layer does, so the
    # two `ok` rows carry them here.  The layer itself needs none of them:
    # whatever a row happens to hold is what gets mapped.
    complete = {"space_heating_kwh_m2": 20.0, "cooling_kwh_m2": 10.0,
                "dhw_kwh_m2": 5.0, "total_site_co2_kg_m2": 12.0,
                "residential_total_site_kwh_m2": 45.0, "terciario_site_kwh": 0.0,
                "tipo15_res_area_m2": 95.0, "total_conditioned_area_m2": 110.0,
                "storey_cap_applied": False, "seconds": 4.0}
    rows = [{**row, **complete} if row["status"] == "ok" else row
            for row in _rows()]
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows),
                      encoding="utf-8")
    stock_path = tmp_path / "prepared.gpkg"
    _stock().to_file(stock_path, driver="GPKG")

    assert sr.main(["--aggregate", str(ledger), "--stock", str(stock_path)]) == 0
    report = json.loads((tmp_path / "aggregate.json").read_text(encoding="utf-8"))
    assert report["results_layer"]["written"] is True
    assert report["results_layer"]["features"] == 3
    assert (tmp_path / rl.LAYER_FILENAME).exists()


def _stock_with_context() -> gpd.GeoDataFrame:
    """SPLIT's two rows carry the population split between them, as the real
    prepared stock does: one row holds it and the other holds zero, while the
    dwelling count is repeated on both."""
    return gpd.GeoDataFrame(
        {"refparcela": ["ONE", "SPLIT", "SPLIT", "GONE"],
         "cluster": ["BlocPluriP04", "BlocPluriP06", "BlocPluriP06", "VivUniP02"],
         "nombre": ["BENICALAP"] * 4,
         "pob_total": [0, 0, 15, 4],
         "num_vivend": [3, 9, 9, 1],
         "altura_max": [5, 6, 6, 2]},
        geometry=[box(0, 0, 10, 10), box(100, 0, 110, 10),
                  box(110, 0, 120, 10), box(300, 0, 310, 10)],
        crs="EPSG:25830",
    )


def test_typology_and_district_come_from_the_stock_when_the_ledger_lacks_them(tmp_path):
    """Benicalap v8 predates the runner writing `cluster` into its rows, so
    without this the layer cannot answer "which typology" although the answer
    is in the file it was joined against."""
    rows = [dict(r) for r in _rows()]
    for row in rows:
        row.pop("cluster", None)

    block = rl.write_results_layer(rows, _stock_with_context(), tmp_path)
    layer = _read(tmp_path / rl.LAYER_FILENAME)

    assert "cluster" in block["context_fields"]
    assert "nombre" in block["context_fields"]
    one = layer.loc[layer["refparcela"] == "ONE"].iloc[0]
    assert one["cluster"] == "BlocPluriP04"
    assert one["nombre"] == "BENICALAP"


def test_a_split_parcels_population_is_summed_and_its_dwellings_are_not(tmp_path):
    """Measured on the real stock, not assumed: a multi-footprint reference has
    its population divided across its rows (0 + 15) while its dwelling count is
    repeated (9 + 9).  Taking the first row would report a building with
    residents as empty; summing the dwellings would double them."""
    rl.write_results_layer(_rows(), _stock_with_context(), tmp_path)
    layer = _read(tmp_path / rl.LAYER_FILENAME)

    split = layer.loc[layer["refparcela"] == "SPLIT"].iloc[0]
    assert split["pob_total"] == pytest.approx(15.0)
    assert split["num_vivend"] == pytest.approx(9.0)


def test_the_ledgers_own_value_is_never_overwritten_by_the_stock(tmp_path):
    """The ledger records what the run used; the stock is only context."""
    rows = [dict(r) for r in _rows()]
    rows[0]["cluster"] = "RECORDED_BY_THE_RUN"

    rl.write_results_layer(rows, _stock_with_context(), tmp_path)
    layer = _read(tmp_path / rl.LAYER_FILENAME)

    one = layer.loc[layer["refparcela"] == "ONE"].iloc[0]
    assert one["cluster"] == "RECORDED_BY_THE_RUN"


def test_per_person_energy_is_null_not_zero_where_nobody_lives(tmp_path):
    """A zero would read as "uses no energy per person"; NULL says there is
    nobody to divide by.  ONE has no registered residents, SPLIT has fifteen."""
    rl.write_results_layer(_rows(), _stock_with_context(), tmp_path)
    layer = _read(tmp_path / rl.LAYER_FILENAME)
    indexed = layer.set_index("refparcela")

    assert indexed.loc["ONE", "total_site_kwh_per_person"] is None or \
        pd_isna(indexed.loc["ONE", "total_site_kwh_per_person"])
    assert indexed.loc["SPLIT", "total_site_kwh_per_person"] == pytest.approx(8000.0 / 15, rel=1e-3)
    # dwellings exist for both, so both are computed
    assert indexed.loc["ONE", "total_site_kwh_per_dwelling"] == pytest.approx(5000.0 / 3, rel=1e-3)


def pd_isna(value) -> bool:
    import pandas as pd
    return bool(pd.isna(value))


def _many(n: int = 12):
    """A stock big enough to classify: the style needs one value per class."""
    stock = gpd.GeoDataFrame(
        {"refparcela": [f"B{i}" for i in range(n)],
         "cluster": ["A" if i % 2 else "B" for i in range(n)],
         "nombre": ["NORTH" if i < n // 2 else "SOUTH" for i in range(n)],
         "pob_total": [i for i in range(n)],
         "num_vivend": [1 + i for i in range(n)]},
        geometry=[box(20 * i, 0, 20 * i + 10, 10) for i in range(n)],
        crs="EPSG:25830")
    rows = [{"refparcela": f"B{i}", "status": "ok",
             "total_site_kwh_m2": 30.0 + 4 * i, "res_area_m2": 100.0,
             "space_heating_kwh_m2": 2.0 + i, "cooling_kwh_m2": 1.0 + i / 4,
             "total_site_kwh": (30.0 + 4 * i) * 100.0}
            for i in range(n)]
    # one building that never produced a result
    rows.append({"refparcela": "B0X", "status": "failed", "reason": "RuntimeError"})
    stock = gpd.GeoDataFrame(
        list(stock.drop(columns="geometry").to_dict("records")) +
        [{"refparcela": "B0X", "cluster": "A", "nombre": "NORTH",
          "pob_total": 5, "num_vivend": 2}],
        geometry=list(stock.geometry) + [box(20 * n, 0, 20 * n + 10, 10)],
        crs="EPSG:25830")
    return rows, stock


def test_zone_layers_roll_the_buildings_up_by_cluster_and_district(tmp_path):
    rows, stock = _many()
    block = rl.write_results_layer(rows, stock, tmp_path)

    assert block["zone_layers"] == {"by_cluster": 2, "by_district": 2}
    zones = gpd.read_file(tmp_path / rl.LAYER_FILENAME, layer="by_cluster")
    assert set(zones["cluster"]) == {"A", "B"}


def test_a_zone_total_follows_the_published_convention(tmp_path):
    """`aggregate()` rebuilds energy as intensity x area, and the row's own
    `total_site_kwh` parts company with it in the fifth decimal.  One package
    must not print the same quantity two ways."""
    rows, stock = _many()
    rl.write_results_layer(rows, stock, tmp_path)
    zones = gpd.read_file(tmp_path / rl.LAYER_FILENAME, layer="by_district")

    measured = [r for r in rows if r["status"] == "ok"]
    expected = sum(r["total_site_kwh_m2"] * r["res_area_m2"] for r in measured)
    assert float(zones["total_site_kwh"].sum()) == pytest.approx(expected, rel=1e-9)


def test_a_zone_counts_the_buildings_it_could_not_measure(tmp_path):
    """A zone coloured on half its stock must not read as if it covered all."""
    rows, stock = _many()
    rl.write_results_layer(rows, stock, tmp_path)
    zones = gpd.read_file(tmp_path / rl.LAYER_FILENAME, layer="by_district")
    north = zones.loc[zones["nombre"] == "NORTH"].iloc[0]

    assert north["buildings"] > north["buildings_measured"]


def test_the_style_is_well_formed_and_names_the_class_with_no_result(tmp_path):
    """Rule-based on purpose: a graduated renderer draws nothing where the
    attribute is NULL, which would hide the buildings the layer keeps."""
    import xml.etree.ElementTree as ET

    rows, stock = _many()
    block = rl.write_results_layer(rows, stock, tmp_path)

    assert block["style"]["written"] is True
    document = (tmp_path / rl.STYLE_FILENAME).read_text(encoding="utf-8")
    root = ET.fromstring(document)                        # raises if malformed
    filters = [r.get("filter") for r in root.iter("rule")]
    assert any("IS NULL" in (f or "") for f in filters), filters
    assert len(filters) == len(rl._STYLE_RAMP) + 1

    import sqlite3
    with sqlite3.connect(tmp_path / rl.LAYER_FILENAME) as db:
        stored = db.execute(
            "SELECT f_table_name, useAsDefault FROM layer_styles").fetchall()
    assert stored == [(rl.LAYER_NAME, 1)]


def test_the_heatmap_draws_the_buildings_without_a_result(tmp_path):
    import results_maps as rm

    rows, stock = _many()
    frame, _ = rl.build_frame(rows, stock)
    block = rm.write_heatmap(frame, tmp_path, title="unit")

    assert block["written"] is True
    assert block["buildings_without_result"] == 1
    assert (tmp_path / rm.MAP_FILENAME).is_file()
    assert not list(tmp_path.glob("*.partial.png"))


def test_the_heatmap_says_so_rather_than_raising_without_energy(tmp_path):
    import results_maps as rm

    frame, _ = rl.build_frame(
        [{"refparcela": "ONE", "status": "excluded", "reason": "x"}], _stock())
    block = rm.write_heatmap(frame, tmp_path)

    assert block["written"] is False
    assert block["reason"] == "no_energy_columns"


# ---------------------------------------------------------------------------
# What period the numbers cover
#
# An annual run and a microclimate event run write the same column names.
# Measured 2026-08-12 on a real 8-day Lecco event: `total_site_kwh_m2` held
# 1.02-1.26 instead of ~52, every panel was labelled `kWh/m2·yr`, and nothing
# on the row said otherwise.
# ---------------------------------------------------------------------------
def _event_rows() -> list[dict]:
    rows = _rows()
    for row in rows:
        row.update({"run_mode": rl.EVENT_MODE, "event_days": 8,
                    "event_window": "08-16..08-23"})
    return rows


def test_a_ledger_without_a_run_mode_is_annual_not_unknown():
    """Those runs were annual; a missing field is not missing information."""
    basis = rl.energy_period(_rows())

    assert basis["period"] == "annual"
    assert basis["unit"] == rl.ANNUAL_UNIT


def test_an_event_ledger_reports_its_window_instead_of_a_year():
    basis = rl.energy_period(_event_rows())

    assert basis["period"] == rl.EVENT_MODE
    assert "8-day event" in basis["unit"]
    assert "yr" not in basis["unit"]
    assert basis["event_window"] == "08-16..08-23"
    # The per-year field names do not become correct just because the run was
    # short, so the block warns about them by name.
    assert "total_site_co2_t_yr" in basis["note"]


def test_two_run_modes_in_one_ledger_are_refused_not_averaged():
    mixed = _rows() + _event_rows()

    basis = rl.energy_period(mixed)

    assert basis["period"] == "mixed"
    assert basis["unit"] is None


def test_the_layer_block_carries_the_period(tmp_path):
    block = rl.write_results_layer(_event_rows(), _stock(), tmp_path)

    assert block["energy_period"]["period"] == rl.EVENT_MODE
    assert block["heatmap"]["unit"] == block["energy_period"]["unit"]


def test_a_constant_panel_is_dropped_rather_than_given_an_invented_range(tmp_path):
    """August has no heating: every building is 0.0.

    Left in, matplotlib colours the constant by inventing a range around it -
    the panel looks like a measurement and is not one.
    """
    import results_maps as rm

    rows = _event_rows()
    for row in rows:
        if row["status"] == "ok":
            row["space_heating_kwh_m2"] = 0.0
            row["cooling_kwh_m2"] = 0.3 if row["refparcela"] == "ONE" else 0.5
    frame, _ = rl.build_frame(rows, _stock())

    block = rm.write_heatmap(frame, tmp_path)

    assert block["written"] is True
    assert "space_heating_kwh_m2" not in block["panels"]
    assert block["panels_omitted_constant"]["space_heating_kwh_m2"] == 0.0
    assert "cooling_kwh_m2" in block["panels"]


def test_an_event_run_stamps_every_row_including_the_failed_ones():
    """A building the slice refused is evidence about the slice's reach."""
    import stock_runner as sr

    sr._WORKER.clear()
    sr._WORKER["event_plan"] = {"days": 8, "window": [8, 16, 8, 23, 822]}
    try:
        stamp = sr._event_stamp()
    finally:
        sr._WORKER.clear()

    assert stamp == {"run_mode": "microclimate_event", "event_days": 8,
                     "event_window": "08-16..08-23"}
    assert sr._event_stamp() == {}          # annual run stamps nothing


def test_an_unstamped_exclusion_would_make_one_run_look_like_two():
    """Regression: the excluded rows are written before the worker pool starts.

    They did not pass through `run_one`, so they carried no run mode, and the
    mixed-mode guard then refused the basis of a perfectly ordinary event run
    (caught by that guard on the first real event run, 2026-08-12).
    """
    rows = _event_rows()
    unstamped = {"refparcela": "GONE2", "status": "excluded", "reason": "x"}

    assert rl.energy_period(rows + [unstamped])["period"] == "mixed"
    # ...which is why the runner stamps it from the plan, in the parent process
    # where `_WORKER` is empty.
    import stock_runner as sr

    sr._WORKER.clear()
    stamp = sr._event_stamp({"days": 8, "window": [8, 16, 8, 23, 822]})
    assert rl.energy_period(rows + [{**unstamped, **stamp}])["period"] == rl.EVENT_MODE


# --- review findings, 2026-08-12 -------------------------------------------
# Five defects found by a review of the changeset above.  Each test below
# names the wrong number the defect produced, not just the code path, because
# the point of every one of them is that a file said something untrue.


def test_a_qa_rejected_building_stays_out_of_the_zone_totals(tmp_path):
    """The layer must total what `aggregate()` totals, or the package lies.

    `run_one` copies the full metric set onto a row whatever its status, so a
    `failed_qa` row carries energy - numbers that failed their own cross-check
    against EnergyPlus.  `aggregate()` sums `ok` only.  Admitting the rejected
    row here made the same signed package's .gpkg report a higher district
    total than its own aggregate.json.
    """
    rows, stock = _many(6)
    rows.append({"refparcela": "B0X", "status": "failed_qa",
                 "reason": "unmet_hours", "res_area_m2": 100.0,
                 "total_site_kwh_m2": 999.0, "total_site_kwh": 99900.0})
    rl.write_results_layer(rows, stock, tmp_path)
    zones = gpd.read_file(tmp_path / rl.LAYER_FILENAME, layer="by_cluster")
    expected = sum(r["total_site_kwh_m2"] * r["res_area_m2"]
                   for r in rows if r["status"] == "ok")
    assert round(float(zones["total_site_kwh"].sum()), 1) == round(expected, 1)
    # and it is still counted as a building, so the gap stays visible
    assert int(zones["buildings"].sum()) > int(zones["buildings_measured"].sum())


def test_one_ledger_never_carries_two_different_periods(tmp_path):
    """`aggregate()` and the layer block sit in one file and must agree.

    `aggregate()` dedupes by reference before totalling; the layer block did
    not, so a ledger with a superseded row could be described two ways inside
    one `aggregate.json`.  Closed by giving both the ledger as written - the
    conservative reading, since a stale row can only raise the mixed alarm,
    never suppress it.  Deduping instead was tried and reverted: two ledgers
    of the same city share their references, so it made a hand-concatenated
    pair collapse into whichever run happened to be appended last.
    """
    import inspect

    import stock_runner as sr

    ledger = _rows() + _event_rows()          # two runs, overlapping references
    assert len({row["refparcela"] for row in ledger}) < len(ledger), \
        "the fixture must overlap, or it cannot show what deduping would cost"

    block = rl.write_results_layer(ledger, None, tmp_path)
    assert rl.energy_period(ledger)["period"] == "mixed"
    assert block["energy_period"]["period"] == "mixed"

    # `aggregate()` answers from the same untouched list, so the two blocks in
    # one `aggregate.json` cannot part company.
    body = inspect.getsource(sr.aggregate)
    assert "rl.energy_period(raw_rows)" in body
    assert "rl.energy_period(rows)" not in body


def test_an_unknown_period_is_not_drawn_as_a_year(tmp_path):
    """`None` means "could not be established", which is not "annual".

    Defaulting it on the picture asserted precisely what `energy_period`
    refused to guess for a mixed ledger.
    """
    import results_maps

    rows, stock = _many(6)
    frame, _ = rl.build_frame(rows, stock)
    block = results_maps.write_heatmap(frame, tmp_path, unit=None)
    assert block["written"] is True
    assert block["unit"] == results_maps.UNKNOWN_UNIT
    assert block["unit"] != results_maps.DEFAULT_UNIT


def test_re_aggregating_without_the_stock_keeps_the_layer_on_disk(tmp_path):
    """`aggregate.json` is rewritten whole; it must not disown its own files.

    The Outputs screen gates both download buttons on `written`, so a
    re-aggregation that forgot `--stock` made a run appear to have lost the
    GeoPackage and heat map that were sitting in its directory.
    """
    import stock_runner as sr

    (tmp_path / rl.LAYER_FILENAME).write_bytes(b"not really a gpkg")
    (tmp_path / "aggregate.json").write_text(json.dumps(
        {"results_layer": {"written": True, "layer": rl.LAYER_FILENAME,
                           "features": 12}}), encoding="utf-8")
    carried = sr._carry_forward_layer(
        {"written": False, "reason": "no_stock_geometry"}, tmp_path)
    assert carried["written"] is True and carried["features"] == 12
    # and it admits it was carried rather than posing as this pass's work
    assert "carried_forward" in carried


def test_re_aggregating_keeps_how_long_the_run_took(tmp_path):
    """A re-aggregation corrects what it can recompute, not what it cannot.

    `elapsed_minutes` is timed by the simulation pass, so a later `--aggregate`
    has no way to derive it.  Dropping it would quietly delete the one field
    that records what the directory cost to produce - the same failure the
    layer carry-forward exists to prevent.
    """
    import stock_runner as sr

    (tmp_path / "aggregate.json").write_text(
        json.dumps({"elapsed_minutes": 323.47}), encoding="utf-8")
    assert sr._carry_forward_elapsed(tmp_path) == {"elapsed_minutes": 323.47}


def test_elapsed_is_not_invented_when_there_is_nothing_to_carry(tmp_path):
    """No previous file, an unreadable one, or one without the field: say nothing."""
    import stock_runner as sr

    assert sr._carry_forward_elapsed(tmp_path) == {}
    (tmp_path / "aggregate.json").write_text("{ not json", encoding="utf-8")
    assert sr._carry_forward_elapsed(tmp_path) == {}
    (tmp_path / "aggregate.json").write_text(json.dumps({"totals": {}}), encoding="utf-8")
    assert sr._carry_forward_elapsed(tmp_path) == {}


def test_a_carried_layer_is_refused_when_the_file_is_gone(tmp_path):
    """Carrying the record forward is only honest while the file is there."""
    import stock_runner as sr

    (tmp_path / "aggregate.json").write_text(json.dumps(
        {"results_layer": {"written": True, "layer": rl.LAYER_FILENAME}}),
        encoding="utf-8")
    block = sr._carry_forward_layer(
        {"written": False, "reason": "no_stock_geometry"}, tmp_path)
    assert block["written"] is False


def test_a_dead_worker_does_not_unlabel_the_whole_event_run():
    """One crash row without the stamp made the ledger read as two run modes.

    The consequence is the one this changeset exists to prevent: `mixed`
    suppresses the unit, and the heat map reverts to the annual label.  Held
    at the source, because the row is built inside the pool's failure path.
    """
    import inspect

    import stock_runner as sr

    body = inspect.getsource(sr.run_stock)
    crash = body.index('"reason": f"worker_{type(exc).__name__}"')
    window = body[crash - 400:crash + 400]
    assert "_event_stamp(event_plan)" in window, \
        "the worker-death row must carry the run mode like every other row"
