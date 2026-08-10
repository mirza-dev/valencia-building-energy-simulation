"""Splitting a parcel into masses: conservation, the engine's own gate, and the seam.

Two properties carry the whole measurement.  The split must conserve the
parcel - the same dwelling area and the same people, redistributed - or the
comparison against the single prism is measuring a different building.  And a
mass must be refused by the *engine's* footprint gate rather than by a threshold
rewritten here, or the split would quietly build things the stock run would not.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

import eu_mass_split as ems
import model_builder as mb
import verified_model as vm


def _taxonomy(hei=None, him=None, use="RES1") -> str:
    payload = {"OCC": use}
    if hei is not None:
        payload["HEI"] = f"H:{hei}"
    if him is not None:
        payload["HIM"] = f"HHT:{him:.2f}"
    return json.dumps(payload)


def _parent(**overrides) -> pd.Series:
    row = {"refparcela": "PARENT", "altura_max": 6, "cluster": "BlocPluriP04",
           "ground_use": "terciario", "tipo15_res_area_m2": 6000.0,
           "pob_total": 120.0, "num_vivend": 60.0, "footprint_area_m2": 1000.0,
           "geometry": box(0, 0, 100, 100)}
    row.update(overrides)
    return pd.Series(row)


def _masses(n: int = 2) -> list[dict]:
    out = []
    for i in range(n):
        poly = box(i * 40, 0, i * 40 + 30, 30)          # 900 m2 each
        out.append({"eu_area_m2": 900.0, "altura_max": 4 + i,
                    "height_source": "eu_hei", "polygon": poly,
                    "geometry": poly, "footprint_m2": 900.0})
    return out


# ---------------------------------------------------------------------------
# Conservation
# ---------------------------------------------------------------------------
def test_allocation_conserves_area_people_and_dwellings():
    parent = _parent()
    allocated = ems.allocate(parent, _masses(3))
    assert sum(m["volume_share"] for m in allocated) == pytest.approx(1.0)
    assert sum(m["tipo15_res_area_m2"] for m in allocated) == pytest.approx(6000.0)
    assert sum(m["pob_total"] for m in allocated) == pytest.approx(120.0)
    assert sum(m["num_vivend"] for m in allocated) == pytest.approx(60.0)


def test_allocation_is_weighted_by_built_volume_not_by_footprint():
    """Equal footprints at unequal heights must not receive equal area."""
    parent = _parent()
    masses = _masses(2)                                  # altura 4 and 5
    allocated = ems.allocate(parent, masses)
    assert allocated[0]["volume_share"] == pytest.approx(4 / 9)
    assert allocated[1]["volume_share"] == pytest.approx(5 / 9)


def test_allocation_refuses_a_single_mass():
    with pytest.raises(ems.SplitError, match="at least two"):
        ems.allocate(_parent(), _masses(1))


def test_missing_cadastral_area_is_carried_through_not_invented():
    """No Tipo15 area means no cap for this parcel, exactly as in the stock run."""
    allocated = ems.allocate(_parent(tipo15_res_area_m2=None), _masses(2))
    assert all(m["tipo15_res_area_m2"] is None for m in allocated)
    # with nothing to cap against, the EU storey count survives untouched
    assert [m["expected_storeys_effective"] for m in allocated] == [4, 5]


# ---------------------------------------------------------------------------
# Storey source
# ---------------------------------------------------------------------------
def test_hei_is_preferred_and_offset_to_the_cadastral_convention():
    """`altura_max` excludes the bajo; `HEI` counts it.  Measured offset: 1."""
    storeys, source = ems.mass_storeys(_taxonomy(hei=8, him=20.0), parent_altura_max=3)
    assert (storeys, source) == (7, "eu_hei")


def test_him_is_converted_with_the_eu_own_storey_height():
    """2.5 m, not our 3.0 m - HIM/HEI has a median of exactly 2.50 in that file."""
    storeys, source = ems.mass_storeys(_taxonomy(him=42.5), parent_altura_max=3)
    assert (storeys, source) == (16, "eu_him")           # 42.5 / 2.5 - 1


def test_a_mass_without_height_inherits_the_parcel_and_says_so():
    storeys, source = ems.mass_storeys(_taxonomy(), parent_altura_max=9)
    assert (storeys, source) == (9, "parent_cadastre")


# ---------------------------------------------------------------------------
# The engine's own gate decides what is buildable
# ---------------------------------------------------------------------------
def test_masses_below_the_engine_floor_are_refused_with_a_reason():
    eu = gpd.GeoDataFrame(
        {"taxonomy": [_taxonomy(hei=6), _taxonomy(hei=2)]},
        geometry=[box(0, 0, 30, 30), box(50, 0, 55, 5)],   # 900 m2 and 25 m2
        crs="EPSG:25830")
    kept, refused = ems.buildable_masses(box(0, 0, 100, 100), eu, 5)
    assert len(kept) == 1 and len(refused) == 1
    assert "outside the expected range" in refused[0]["reason"]
    # the floor is the builder's, not a copy of it
    assert mb.DEFAULT_BUILD_CONFIG.geometry.footprint_min_m2 > 25


def test_a_parcel_left_with_one_buildable_mass_is_not_split(tmp_path):
    """One real block plus a shed is the case the single prism already describes."""
    prepared = gpd.GeoDataFrame(
        [{"refparcela": "PARENT", "altura_max": 5, "tipo15_res_area_m2": 3000.0,
          "pob_total": 40.0, "num_vivend": 20.0, "ground_use": "terciario"}],
        geometry=[box(0, 0, 100, 100)], crs="EPSG:25830")
    eu = gpd.GeoDataFrame(
        {"category": [1, 1], "taxonomy": [_taxonomy(hei=6), _taxonomy(hei=1)]},
        geometry=[box(10, 10, 40, 40), box(60, 60, 64, 64)], crs="EPSG:25830")
    path = tmp_path / "eu.gpkg"
    eu.to_file(path, driver="GPKG")

    plans = ems.plan_parcels(prepared, path)
    assert len(plans) == 1
    assert "masses" not in plans[0]
    assert plans[0]["skipped"].startswith("fewer than two")


# ---------------------------------------------------------------------------
# The context a mass stands in
# ---------------------------------------------------------------------------
def test_context_drops_the_parent_prism_and_adds_the_siblings(tmp_path):
    """Leaving the parent in would shade every mass with the object it replaces."""
    cadastre = gpd.GeoDataFrame(
        {"refparcela": ["PARENT", "OTHER"], "altura_max": [15, 4]},
        geometry=[box(0, 0, 100, 100), box(110, 0, 140, 30)], crs="EPSG:25830")
    parent = _parent()
    plan = {"refparcela": "PARENT", "masses": ems.allocate(parent, _masses(2))}
    targets = gpd.GeoDataFrame(
        {"refparcela": ["PARENT_M1", "PARENT_M2"]},
        geometry=[m["polygon"] for m in plan["masses"]], crs="EPSG:25830")

    out = tmp_path / "ctx.gpkg"
    ems.write_context(cadastre, plan, targets, out)
    written = gpd.read_file(out)

    assert "PARENT" not in set(written["refparcela"])
    assert {"PARENT_M1", "PARENT_M2", "OTHER"} == set(written["refparcela"])
    heights = written.set_index("refparcela")["altura_max"].to_dict()
    assert heights["PARENT_M1"] == plan["masses"][0]["modelled_altura_max"]
    assert heights["PARENT_M2"] == plan["masses"][1]["modelled_altura_max"]
    assert heights["OTHER"] == 4                      # untouched neighbour


def test_mass_reference_is_usable_as_a_directory_name():
    """The run directory is `<refparcela>_deep`; a separator would escape it."""
    reference = ems._mass_reference("4252702YJ2745A", 3)
    assert reference == "4252702YJ2745A_M3"
    assert not set(reference) & set("/\\:#*? ")


def test_sibling_geometry_separates_touching_from_gapped():
    touching = [{"polygon": box(0, 0, 10, 10)}, {"polygon": box(10, 0, 20, 10)}]
    apart = [{"polygon": box(0, 0, 10, 10)}, {"polygon": box(18, 0, 28, 10)}]
    assert ems.sibling_geometry(touching)["sibling_contact_m"] == pytest.approx(10.0)
    assert ems.sibling_geometry(touching)["sibling_gap_min_m"] == 0.0
    assert ems.sibling_geometry(apart)["sibling_contact_m"] == 0.0
    assert ems.sibling_geometry(apart)["sibling_gap_min_m"] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# The seam: the frozen engine runs the mass, and the report keeps size and
# shape apart
# ---------------------------------------------------------------------------
def test_a_mass_is_run_through_the_verified_entry_point():
    """Not through `deep_building` directly: the profile guard must fire per mass."""
    source = Path(ems.__file__).read_text(encoding="utf-8")
    body = source.split("def run_mass(")[1].split("\ndef ")[0]
    assert "vm.simulate_verified_building" in body
    assert "simulate_deep_building" not in body
    assert "build_model" not in body


def test_the_frozen_modules_are_still_the_verified_ones():
    vm.assert_profile_intact()


def test_comparison_reports_size_and_shape_separately():
    """A split that builds 20 % more floor area at the same intensity is not a
    20 % massing effect, and the report must not let it read as one."""
    baseline = {"P": {"refparcela": "P", "status": "ok", "total_site_kwh": 100000.0,
                      "res_area_m2": 2000.0, "footprint_m2": 400.0,
                      "tipo15_res_area_m2": 2000.0, "space_heating_kwh_m2": 5.0,
                      "cooling_kwh_m2": 4.0, "n_party_surfaces": 6}}
    rows = [{"refparcela": "P_M1", "parent_refparcela": "P", "status": "ok",
             "total_site_kwh": 60000.0, "res_area_m2": 1200.0, "footprint_m2": 300.0,
             "space_heating_kwh_m2": 5.0, "cooling_kwh_m2": 4.0,
             "n_party_surfaces": 0, "qa_all_passed": True},
            {"refparcela": "P_M2", "parent_refparcela": "P", "status": "ok",
             "total_site_kwh": 60000.0, "res_area_m2": 1200.0, "footprint_m2": 300.0,
             "space_heating_kwh_m2": 5.0, "cooling_kwh_m2": 4.0,
             "n_party_surfaces": 0, "qa_all_passed": True}]
    report = ems.compare(rows, baseline, [{"refparcela": "P", "masses": [],
                                           "eu_masses": 2}])
    parcel = report["parcels"][0]
    assert parcel["delta_pct"] == pytest.approx(20.0)          # absolute energy
    assert parcel["modelled_area_ratio"] == pytest.approx(1.2)  # all of it is size
    assert parcel["intensity_delta_pct"] == pytest.approx(0.0)  # none of it is shape
    assert report["intensity_delta_pct"] == pytest.approx(0.0)


def test_a_failed_mass_invalidates_its_parcel_rather_than_shrinking_it():
    """Summing the masses that happened to run would understate the parcel."""
    baseline = {"P": {"refparcela": "P", "status": "ok", "total_site_kwh": 100.0,
                      "res_area_m2": 10.0, "footprint_m2": 5.0,
                      "tipo15_res_area_m2": 10.0, "space_heating_kwh_m2": 1.0,
                      "cooling_kwh_m2": 1.0}}
    rows = [{"refparcela": "P_M1", "parent_refparcela": "P", "status": "ok",
             "total_site_kwh": 40.0, "res_area_m2": 5.0, "footprint_m2": 2.5,
             "space_heating_kwh_m2": 1.0, "cooling_kwh_m2": 1.0, "qa_all_passed": True},
            {"refparcela": "P_M2", "parent_refparcela": "P", "status": "failed",
             "error": "boom"}]
    report = ems.compare(rows, baseline, [{"refparcela": "P"}])
    assert report["parcels"] == []
    assert report["parcels_incomplete"][0]["failed"] == ["P_M2"]
