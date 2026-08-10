"""EU sub-footprint fragmentation flag: assignment rule, fail-closed source, no physics.

The single most important property under test is the last one: the flag must be
an annotation.  A building simulated with `--eu` and without it has to produce
the same energy, or the flag has stopped being a measurement of the model and
started being part of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

import eu_footprint_flags as euf
import stock_runner as sr


def _taxonomy(height: float | None = None, use: str | None = None) -> str:
    payload: dict[str, str] = {}
    if height is not None:
        payload["HIM"] = f"HHT:{height:.2f}"
    if use is not None:
        payload["OCC"] = use
    return json.dumps(payload)


def _cadastre() -> gpd.GeoDataFrame:
    """Three parcels: SINGLE holds one mass, SPLIT three, EMPTY none."""
    return gpd.GeoDataFrame(
        {"refparcela": ["SINGLE", "SPLIT", "EMPTY"]},
        geometry=[box(0, 0, 20, 20), box(100, 0, 140, 40), box(300, 0, 320, 20)],
        crs="EPSG:25830",
    )


def _eu_source(tmp_path: Path) -> Path:
    frame = gpd.GeoDataFrame(
        {
            "category": [1, 1, 1, 1, 2],
            "taxonomy": [
                _taxonomy(10.0, "RES"),          # inside SINGLE
                _taxonomy(42.5, "RES1"),         # inside SPLIT - tower
                _taxonomy(17.5, "RES1"),         # inside SPLIT - block
                _taxonomy(None, "MIX(COM1-RES)"),  # inside SPLIT - no height
                _taxonomy(5.0, "RES"),           # inside SINGLE but category 2
            ],
        },
        geometry=[
            box(2, 2, 8, 8),
            box(102, 2, 112, 12),
            box(115, 2, 125, 12),
            box(102, 20, 112, 30),
            box(12, 12, 18, 18),
        ],
        crs="EPSG:25830",
    )
    path = tmp_path / "eu.gpkg"
    frame.to_file(path, driver="GPKG")
    return path


# ---------------------------------------------------------------------------
# Assignment rule
# ---------------------------------------------------------------------------
def test_counts_masses_per_parcel_and_ignores_auxiliary_category(tmp_path):
    flags = euf.flags_by_reference(_cadastre(), _eu_source(tmp_path))

    # The category-2 shape falls inside SINGLE and must not be counted: it is
    # auxiliary geometry, and counting sheds as masses would inflate the flag.
    assert flags["SINGLE"]["eu_subfootprint_count"] == 1
    assert flags["SINGLE"]["eu_multi_footprint"] is False
    assert flags["SPLIT"]["eu_subfootprint_count"] == 3
    assert flags["SPLIT"]["eu_multi_footprint"] is True
    assert flags["EMPTY"]["eu_subfootprint_count"] == 0


def test_uncovered_parcel_is_not_reported_as_single_mass(tmp_path):
    """Count 0 means "not measured", not "one mass" - the two must stay apart."""
    flags = euf.flags_by_reference(_cadastre(), _eu_source(tmp_path))
    assert flags["EMPTY"]["eu_multi_footprint"] is False
    assert flags["EMPTY"]["eu_height_spread_m"] is None

    frame = pd.DataFrame([
        {"refparcela": "EMPTY", "eu_subfootprint_count": 0,
         "res_area_m2": 100.0, "total_site_kwh_m2": 50.0},
        {"refparcela": "SPLIT", "eu_subfootprint_count": 3,
         "res_area_m2": 100.0, "total_site_kwh_m2": 50.0},
    ])
    block = euf.fragmentation_block(frame)
    assert block["buildings_measured"] == 1          # EMPTY is not measured
    assert block["buildings"] == 1                   # only SPLIT is flagged


def test_height_spread_needs_two_measured_heights(tmp_path):
    flags = euf.flags_by_reference(_cadastre(), _eu_source(tmp_path))
    # SPLIT has 42.5 and 17.5 plus one footprint with no height at all.
    assert flags["SPLIT"]["eu_height_spread_m"] == pytest.approx(25.0)
    # SINGLE has exactly one height, so a spread cannot be stated.
    assert flags["SINGLE"]["eu_height_spread_m"] is None


def test_distinct_use_classes_are_counted(tmp_path):
    flags = euf.flags_by_reference(_cadastre(), _eu_source(tmp_path))
    assert flags["SPLIT"]["eu_distinct_use_classes"] == 2   # RES1, MIX(...)


# ---------------------------------------------------------------------------
# Fail-closed source
# ---------------------------------------------------------------------------
def test_missing_source_is_refused(tmp_path):
    with pytest.raises(euf.EuSourceError, match="not found"):
        euf.load_eu_footprints(tmp_path / "absent.gpkg")


def test_source_without_category_column_is_refused(tmp_path):
    frame = gpd.GeoDataFrame({"other": [1]}, geometry=[box(0, 0, 1, 1)],
                             crs="EPSG:25830")
    path = tmp_path / "no_category.gpkg"
    frame.to_file(path, driver="GPKG")
    with pytest.raises(euf.EuSourceError, match="category"):
        euf.load_eu_footprints(path)


def test_source_without_building_features_is_refused(tmp_path):
    """An all-auxiliary file would silently report "no fragmentation anywhere"."""
    frame = gpd.GeoDataFrame({"category": [2, 2]},
                             geometry=[box(0, 0, 1, 1), box(2, 2, 3, 3)],
                             crs="EPSG:25830")
    path = tmp_path / "aux_only.gpkg"
    frame.to_file(path, driver="GPKG")
    with pytest.raises(euf.EuSourceError, match="category-1"):
        euf.load_eu_footprints(path)


def test_fingerprint_covers_file_and_rule(tmp_path):
    path = _eu_source(tmp_path)
    first = euf.source_fingerprint(path)
    assert first == euf.source_fingerprint(path)      # deterministic

    original = euf.RULE_VERSION
    try:
        euf.RULE_VERSION = original + 1
        assert euf.source_fingerprint(path) != first  # rule change is visible
    finally:
        euf.RULE_VERSION = original


# ---------------------------------------------------------------------------
# The flag is an annotation, not an input
# ---------------------------------------------------------------------------
def test_flag_is_not_a_simulation_input():
    """`run_one` must merge the flag AFTER the model has already returned.

    If this ever reverses, the flag has become a physics input and the "energy
    is unchanged" claim in the module docstring stops being true.
    """
    source = Path(sr.__file__).read_text(encoding="utf-8")
    body = source.split("def run_one(")[1].split("\ndef ")[0]
    simulate_at = body.index("simulate_verified_building")
    merge_at = body.index('_WORKER.get("eu_flags"')
    assert simulate_at < merge_at

    # And it must never reach the builder call itself.
    call = body[simulate_at:body.index("except Exception")]
    assert "eu_" not in call


def test_worker_config_flags_default_to_absent():
    """Without --eu the row gains nothing, so old ledgers stay comparable."""
    assert {}.get("eu_flags", {}).get("ANY", {}) == {}
    source = Path(sr.__file__).read_text(encoding="utf-8")
    assert "eu_path: Path | None = None" in source
    assert 'parser.add_argument("--eu"' in source


def test_run_config_records_source_but_not_the_flag_table():
    """26 452 flag dicts in run_config.json would bury the config it exists for."""
    source = Path(sr.__file__).read_text(encoding="utf-8")
    recorded = source.split("recorded_config = {")[1].split("(out_dir /")[0]
    assert '"eu_flags"' in recorded          # excluded from the record
    assert "eu_source" in recorded           # its identity is kept


# ---------------------------------------------------------------------------
# Aggregate block
# ---------------------------------------------------------------------------
def test_block_reports_area_and_energy_share_not_just_count():
    """A count alone understates the flag: the flagged buildings are the big ones."""
    frame = pd.DataFrame([
        {"eu_subfootprint_count": 4, "eu_height_spread_m": 25.0,
         "res_area_m2": 8000.0, "total_site_kwh_m2": 50.0,
         "storey_cap_applied": True},
        {"eu_subfootprint_count": 1, "eu_height_spread_m": None,
         "res_area_m2": 1000.0, "total_site_kwh_m2": 50.0,
         "storey_cap_applied": False},
        {"eu_subfootprint_count": 1, "eu_height_spread_m": None,
         "res_area_m2": 1000.0, "total_site_kwh_m2": 50.0,
         "storey_cap_applied": False},
    ])
    block = euf.fragmentation_block(frame)
    assert block["buildings"] == 1
    assert block["buildings_pct"] == pytest.approx(33.33, abs=0.01)
    # one building in three, but 80 % of the area behind the totals
    assert block["residential_area_pct"] == pytest.approx(80.0)
    assert block["total_site_pct"] == pytest.approx(80.0)
    assert block["height_spread_median_m"] == pytest.approx(25.0)
    assert block["multi_footprint_pct_of_capped"] == pytest.approx(100.0)
    assert block["multi_footprint_pct_of_uncapped"] == pytest.approx(0.0)


def test_block_is_present_and_inert_on_a_ledger_without_the_flag():
    """Every run written before today must still aggregate."""
    rows = [{"refparcela": "A", "status": "ok", "res_area_m2": 100.0,
             "total_site_kwh_m2": 50.0, "qa_all_passed": True,
             "space_heating_kwh_m2": 10.0, "cooling_kwh_m2": 5.0,
             "dhw_kwh_m2": 2.0, "total_site_co2_kg_m2": 12.0,
             "total_conditioned_area_m2": 100.0}]
    report = sr.aggregate(rows)
    assert "fragmentation" in report
    assert "buildings" not in report["fragmentation"]     # nothing measured
    assert report["fragmentation"]["rule_version"] == euf.RULE_VERSION


def test_empty_report_carries_the_block_too():
    report = sr.aggregate([{"refparcela": "A", "status": "failed"}])
    assert report["buildings_ok"] == 0
    assert "fragmentation" in report
