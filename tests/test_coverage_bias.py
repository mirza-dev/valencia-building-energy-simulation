"""What the missing buildings do to the total - measured, not remembered.

The block exists because the sentence it replaces was a constant.  So the
properties worth locking are the ones a constant cannot have: the direction has
to follow the run's own exclusions and flip when they flip, an absent basis has
to read as absent rather than as zero, and the energy has to be reconstructed
the way `aggregate()` reconstructs it so one package never writes the same
total two ways.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

import coverage_bias as cbias

RUN = Path(__file__).resolve().parents[1] / "out" / "stock" / "benicalap_v8"
PREPARED = (Path(__file__).resolve().parents[1] / "var"
            / "stock_prepared_be32b6492a5a205d.gpkg")


def _row(ref, intensity=50.0, area=100.0, status="ok"):
    return {"refparcela": ref, "status": status,
            "total_site_kwh_m2": intensity, "res_area_m2": area,
            # deliberately inconsistent with intensity x area: the block must
            # ignore this field, exactly as `aggregate()` does
            "total_site_kwh": 999_999.0}


def _stock(entries):
    """entries: {ref: (footprint_m2, cadastral_m2)} - geometry is never read."""
    box = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    frame = gpd.GeoDataFrame(
        {"refparcela": list(entries),
         "footprint_area_m2": [v[0] for v in entries.values()],
         "tipo15_res_area_m2": [v[1] for v in entries.values()],
         "geometry": [box] * len(entries)},
        crs="EPSG:25830")
    return frame


def _call(rows, stock=None):
    ok = [r for r in rows if r.get("status") == "ok"]
    return cbias.bias_block(rows, ok, stock)


# ---------------------------------------------------------------------------
# The direction is measured, so it must flip when the exclusions flip
# ---------------------------------------------------------------------------
def _skewed(missing_footprint):
    """60 measured buildings where small runs hot and large runs cool."""
    rows, entries = [], {}
    for index in range(60):
        footprint = 100.0 + index * 40.0            # 100 .. 2 460 m2
        intensity = 70.0 - index * 0.5              # 70 .. 40.5 kWh/m2
        ref = f"M{index:03d}"
        rows.append(_row(ref, intensity=intensity, area=1000.0))
        entries[ref] = (footprint, 1000.0)
    rows.append({"refparcela": "GONE", "status": "excluded", "reason": "x"})
    entries["GONE"] = (missing_footprint, 1000.0)
    return rows, _stock(entries)


def test_missing_small_buildings_bias_the_published_intensity_downward():
    # small buildings run hot here, so leaving one out drags the measured
    # intensity BELOW an unbiased estimate
    block = _call(*_skewed(missing_footprint=100.0))
    assert block["measured"] is True
    assert block["footprint_skew"]["missing_are"] == "smaller"
    assert block["intensity_bias_pct"] < 0
    assert block["intensity_full_scope_kwh_m2"] > block["intensity_subset_kwh_m2"]


def test_missing_large_buildings_bias_the_published_intensity_upward():
    # the same block, the same physics, the opposite exclusion: this is the
    # v3 situation, and the direction must come back on its own
    block = _call(*_skewed(missing_footprint=5000.0))
    assert block["footprint_skew"]["missing_are"] == "larger"
    assert block["intensity_bias_pct"] > 0
    assert block["intensity_full_scope_kwh_m2"] < block["intensity_subset_kwh_m2"]


def test_a_direction_is_not_claimed_when_the_skew_is_too_small_to_matter():
    rows, stock = _skewed(missing_footprint=1300.0)   # near the median
    block = _call(rows, stock)
    assert abs(block["intensity_bias_pct"]) < cbias.NEGLIGIBLE_INTENSITY_PCT
    assert block["intensity_bias_direction"] == "negligible"
    assert "too small to correct for" in block["note"]


# ---------------------------------------------------------------------------
# Absence of a basis is not a measurement of no bias
# ---------------------------------------------------------------------------
def test_a_stock_without_cadastral_area_says_so_instead_of_printing_zero():
    rows = [_row("A"), _row("B"),
            {"refparcela": "C", "status": "excluded", "reason": "x"}]
    frame = _stock({"A": (100.0, 1.0), "B": (200.0, 1.0), "C": (150.0, 1.0)})
    block = _call(rows, frame.drop(columns=["tipo15_res_area_m2"]))
    assert block["measured"] is False
    assert block["reason"] == "stock_has_no_cadastral_area_column"
    assert "not a measurement of no bias" in block["note"]
    for absent in ("total_truncation_pct", "intensity_bias_pct"):
        assert absent not in block


def test_no_stock_at_all_is_reported_not_guessed():
    rows = [_row("A"), {"refparcela": "B", "status": "excluded"}]
    block = _call(rows, None)
    assert block["measured"] is False
    assert block["reason"] == "stock_has_no_cadastral_area_column"


def test_complete_coverage_is_a_measurement_of_zero_truncation():
    block = _call([_row("A"), _row("B")], _stock({"A": (100.0, 500.0),
                                                  "B": (200.0, 500.0)}))
    assert block["measured"] is True
    assert block["complete_coverage"] is True
    assert block["total_truncation_pct"] == {"low": 0.0, "high": 0.0}


def test_an_empty_ledger_reports_no_rows():
    block = _call([])
    assert block["measured"] is False
    assert block["reason"] == "no_rows"


# ---------------------------------------------------------------------------
# Conventions the rest of the package already fixed
# ---------------------------------------------------------------------------
def test_energy_uses_the_aggregate_convention_not_the_ledger_total():
    # `total_site_kwh` in the fixture is nonsense on purpose; the block must
    # rebuild energy from intensity x residential area like `aggregate()` does
    rows = [_row("A", intensity=50.0, area=1000.0),
            {"refparcela": "B", "status": "excluded"}]
    block = _call(rows, _stock({"A": (100.0, 1000.0), "B": (100.0, 1000.0)}))
    assert block["total_measured_gwh"] == pytest.approx(50_000 / 1e6)
    assert block["intensity_subset_kwh_m2"] == pytest.approx(50.0)


def test_duplicate_stock_rows_for_one_reference_are_summed_not_taken_first():
    # the stock apportions a shared parcel across rows; first() would drop
    # half a building's area and shrink the gap it is supposed to size
    rows = [_row("A", intensity=50.0, area=1000.0),
            {"refparcela": "B", "status": "excluded"}]
    entries = _stock({"A": (100.0, 600.0), "B": (100.0, 400.0)})
    doubled = pd.concat([entries, entries.iloc[[1]].assign(
        tipo15_res_area_m2=400.0)], ignore_index=True)
    block = _call(rows, gpd.GeoDataFrame(doubled, crs=entries.crs))
    assert block["cadastral_area_missing_m2"] == pytest.approx(800.0)


def test_a_missing_building_outside_the_measured_size_range_still_lands_in_a_band():
    rows, stock = _skewed(missing_footprint=999_999.0)
    block = _call(rows, stock)
    placed = sum(band.get("missing_buildings", 0)
                 for band in block.get("footprint_bands", []))
    assert placed == 1, "an outsized exclusion must not fall out of the imputation"


def test_the_block_never_raises_on_a_ledger_that_predates_the_fields():
    rows = [{"refparcela": "A", "status": "ok"},
            {"refparcela": "B", "status": "excluded"}]
    block = _call(rows, _stock({"A": (100.0, 500.0), "B": (100.0, 500.0)}))
    assert block["measured"] is False
    assert block["reason"] == "no_measured_building_carries_energy"


# ---------------------------------------------------------------------------
# Against the published run
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not (RUN / "ledger.jsonl").exists() or not PREPARED.exists(),
                    reason="Benicalap v8 evidence is not present")
def test_benicalap_v8_reproduces_the_published_intensity_and_bounds_the_rest():
    rows = [json.loads(line) for line in
            (RUN / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if line]
    stock = gpd.read_file(PREPARED)
    block = cbias.bias_block(rows, [r for r in rows if r.get("status") == "ok"], stock)

    # the subset intensity has to be the published one, or the bound is being
    # computed about some other run
    assert block["intensity_subset_kwh_m2"] == pytest.approx(50.867, abs=0.001)
    assert block["total_measured_gwh"] == pytest.approx(109.76248, abs=1e-5)

    assert block["buildings_without_result"] == 45
    assert block["cadastral_area_coverage_pct"] == pytest.approx(98.519, abs=0.01)
    assert block["total_truncation_pct"]["low"] == pytest.approx(1.503, abs=0.01)
    assert block["total_truncation_pct"]["high"] == pytest.approx(1.545, abs=0.01)

    # and the correction this module exists for: on v8 the exclusions are
    # SMALL and the residual intensity bias is negligible, not the upward
    # -0.552 story carried over from v3
    assert block["footprint_skew"]["missing_are"] == "smaller"
    assert block["intensity_bias_direction"] == "negligible"
    assert abs(block["intensity_bias_pct"]) < 0.1

    placed = sum(band["missing_buildings"] for band in block["footprint_bands"])
    assert placed == 45, "every excluded building must be accounted for"
