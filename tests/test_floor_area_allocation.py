"""Splitting the modelled-versus-cadastral gap: what the report may and may not claim.

Two properties carry the measurement.  It must reproduce the published headline
exactly - a decomposition of a total it cannot reconstruct is describing a
different run - and the rounding term must stay what it claims to be: one-sided,
under one footprint, and charged only to buildings where the rule actually
decided the height.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import floor_area_allocation as faa

RUN = Path(__file__).resolve().parents[1] / "out" / "stock" / "benicalap_v8"


def _row(ref="A", footprint=100.0, cadastral=250.0, built=5, ground="terciario",
         storeys_effective=3, intensity=40.0, **over):
    row = {"refparcela": ref, "status": "ok", "footprint_m2": footprint,
           "tipo15_res_area_m2": cadastral, "res_area_m2": footprint * storeys_effective,
           "built_storeys": built, "residential_storeys_effective": storeys_effective,
           "ground_use": ground, "total_site_kwh_m2": intensity,
           "lighting_kwh_m2": 15.0, "equipment_kwh_m2": 15.0, "dhw_kwh_m2": 8.0}
    row.update(over)
    return row


def _ledger(tmp_path, rows) -> Path:
    path = tmp_path / "ledger.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The decomposition is exhaustive and each term means what it says
# ---------------------------------------------------------------------------
def test_the_three_terms_add_back_to_the_gap(tmp_path):
    rows = [_row("A", 100.0, 250.0, built=5, storeys_effective=3),   # rule bound
            _row("B", 100.0, 480.0, built=5, storeys_effective=5),   # geometry over
            _row("C", 100.0, 700.0, built=5, storeys_effective=5)]   # cadastre over
    report = faa.measure(_ledger(tmp_path, rows))
    d = report["decomposition"]
    total = (d["integer_storey_rounding"]["excess_m2"]
             + d["geometry_above_cadastre"]["excess_m2"]
             + d["cadastre_above_geometry"]["excess_m2"])
    assert total == pytest.approx(report["area"]["gap_m2"], abs=0.2)
    assert d["integer_storey_rounding"]["buildings"] == 1
    assert d["geometry_above_cadastre"]["buildings"] == 1
    assert d["cadastre_above_geometry"]["buildings"] == 1


def test_rounding_excess_is_one_sided_and_under_one_footprint(tmp_path):
    """It is `f * frac`, so it can never reach a whole storey or go negative."""
    rows = [_row(f"R{i}", 100.0, 200.0 + i * 7.0, built=9,
                 storeys_effective=math.ceil((200.0 + i * 7.0) / 100.0))
            for i in range(12)]
    annotated = [faa._annotate(r) for r in rows]
    assert all(r["_rule_bound"] for r in annotated)
    assert all(0 <= r["_excess"] < r["_f"] for r in annotated)


def test_a_building_the_rule_did_not_bind_is_not_charged_to_rounding(tmp_path):
    """Its gap is the sources disagreeing, and that term has no preferred sign."""
    rows = [_row("B", 100.0, 480.0, built=5, storeys_effective=5)]
    report = faa.measure(_ledger(tmp_path, rows))
    assert report["decomposition"]["integer_storey_rounding"]["excess_m2"] == 0.0
    assert report["decomposition"]["integer_storey_rounding"]["buildings"] == 0
    assert report["decomposition"]["geometry_above_cadastre"]["excess_m2"] == pytest.approx(20.0)


def test_the_one_storey_floor_keeps_a_building_out_of_the_rounding_term(tmp_path):
    """`ceil` reaching 1 on a one-storey building decided nothing to round."""
    row = faa._annotate(_row("F", 100.0, 40.0, built=1, storeys_effective=1))
    assert not row["_rule_bound"]


# ---------------------------------------------------------------------------
# The energy band
# ---------------------------------------------------------------------------
def test_dhw_is_excluded_from_the_band_because_it_does_not_scale_with_area(tmp_path):
    """DHW is per person; charging it to floor area would overstate the excess."""
    rows = [_row("A", 100.0, 250.0, built=5, storeys_effective=3)]
    report = faa.measure(_ledger(tmp_path, rows))
    lo, hi = report["energy"]["band_pct"]
    assert lo < hi                                   # proportional is the floor
    full = 100.0 * report["energy"]["on_excess_at_full_intensity_gwh"] * 1e6 / (
        report["energy"]["total_gwh"] * 1e6)
    assert hi == pytest.approx(full - report["energy"]["dhw_component_pct"], abs=0.01)


def test_lighting_and_equipment_scale_exactly_with_the_excess(tmp_path):
    rows = [_row("A", 100.0, 250.0, built=5, storeys_effective=3)]
    report = faa.measure(_ledger(tmp_path, rows))
    excess = report["decomposition"]["integer_storey_rounding"]["excess_m2"]
    assert excess == pytest.approx(50.0)             # 100 * (3 - 2.5)
    assert report["energy"]["on_excess_proportional_gwh"] * 1e6 == pytest.approx(30.0 * 50.0)


# ---------------------------------------------------------------------------
# Refusals: a missing term is an error, never a silent skip
# ---------------------------------------------------------------------------
def test_a_row_missing_a_needed_field_stops_the_measurement(tmp_path):
    """Dropping it would shrink the very gap this module exists to size."""
    bad = _row("A")
    del bad["tipo15_res_area_m2"]
    with pytest.raises(faa.AllocationError, match="tipo15_res_area_m2"):
        faa.measure(_ledger(tmp_path, [bad]))


def test_a_ledger_without_successful_rows_is_refused(tmp_path):
    path = _ledger(tmp_path, [{"refparcela": "A", "status": "excluded"}])
    with pytest.raises(faa.AllocationError, match="no rows"):
        faa.measure(path)


def test_alternative_rules_are_measured_on_the_same_buildings(tmp_path):
    rows = [_row("A", 100.0, 250.0, built=5, storeys_effective=3)]
    alt = faa.measure(_ledger(tmp_path, rows))["alternative_rules"]
    assert alt["ceil"]["modelled_m2"] == pytest.approx(300.0)
    assert alt["round"]["modelled_m2"] == pytest.approx(200.0)
    assert alt["floor"]["modelled_m2"] == pytest.approx(200.0)
    assert alt["exact_fractional"]["vs_cadastral_pct"] == 0.0


# ---------------------------------------------------------------------------
# Against the published run
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not (RUN / "aggregate.json").exists(),
                    reason="published Benicalap v8 run not on this machine")
def test_it_reproduces_the_published_headline_exactly():
    """A decomposition of a total it cannot rebuild is describing another run."""
    published = json.loads((RUN / "aggregate.json").read_text())["totals"]
    report = faa.measure(RUN / "ledger.jsonl")
    assert report["energy"]["total_gwh"] == pytest.approx(published["total_site_gwh"])
    assert report["area"]["modelled_m2"] == pytest.approx(published["residential_area_m2"])
    assert report["area"]["geometric_kwh_m2"] == pytest.approx(
        published["area_weighted_total_site_kwh_m2"])


@pytest.mark.skipif(not (RUN / "aggregate.json").exists(),
                    reason="published Benicalap v8 run not on this machine")
def test_removing_the_excess_lowers_the_reference_ratio():
    report = faa.measure(RUN / "ledger.jsonl")
    ratio = faa.reference_ratio(report, RUN / "aggregate.json")
    lo, hi = ratio["ratio_without_rounding_excess"]
    assert lo <= hi < ratio["published_ratio"]
    assert ratio["share_of_district_energy_pct"] == pytest.approx(100.0, abs=0.5)
