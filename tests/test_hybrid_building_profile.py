from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from hybrid_building_profile import (
    audit_hybrid_coverage,
    build_hybrid_profile,
    build_hybrid_profile_from_frames,
)


PROJECT = Path(__file__).resolve().parents[1]


def cadastre_frame(geometry=box(0, 0, 10, 10)) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame([{
        "refparcela": "TEST-1",
        "nombre": "TEST DISTRICT",
        "zona_clima": "B3",
        "uso_princi": "Residencial",
        "cluster": "BlocPluriP04",
        "ano_constr": 1974,
        "altura_max": 5,
        "Shape_Area": 100.0,
        "num_vivend": 10,
        "pob_total": 20,
        "pob_0_14": 4,
        "pob_15_65": 14,
        "pob_66_mas": 2,
        "geometry": geometry,
    }], crs="EPSG:25830")


def eu_frame(geometries: list, years: list[int] | None = None) -> gpd.GeoDataFrame:
    years = years or [1974] * len(geometries)
    rows = []
    for index, (geometry, year) in enumerate(zip(geometries, years), start=1):
        rows.append({
            "id": index,
            "category": 1,
            "attributes": json.dumps({
                "building_id": 1000 + index,
                "net_floor_area": 500.0,
                "gross_floor_area": 680.0,
            }),
            "taxonomy": json.dumps({
                "DAT": f"Y:{year}",
                "HEI": "H:7",
                "HIM": "HHT:17.50",
                "OCC": "RES1",
                "OCD": "DWEBET:2-",
            }),
            "geometry": geometry,
        })
    return gpd.GeoDataFrame(rows, crs="EPSG:25830")


def tipo15_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "31_pc": ["TEST-1", "TEST-1", "TEST-1"],
        "372_ant": [1974, 1974, 1974],
        "428_uso": ["V", "V", "C"],
        "442_sup_Residencial": [80, 90, 50],
    })


def test_high_confidence_match_keeps_cadastre_authority_and_tipo15_area():
    profile = build_hybrid_profile_from_frames(
        cadastre_frame(),
        eu_frame([box(0.2, 0.2, 10.2, 10.2)]),
        tipo15_frame(),
    )

    assert profile.match.status == "accepted"
    assert profile.match.confidence == "high"
    assert profile.match.eu_building_id == 1001
    assert profile.cadastre.population_total == 20
    assert profile.tipo15.residential_area_m2 == 170.0
    assert profile.resolved.construction_year == 1974
    assert profile.resolved.construction_year_source == "cadastre.ano_constr"
    assert profile.resolved.residential_area_m2 == 170.0
    assert profile.resolved.residential_area_source == "tipo15.442_sup_Residencial"
    assert profile.resolved.occupants == 20
    assert profile.resolved.uniform_people_per_floor_candidate == 4.0
    assert profile.resolved.occupancy_distribution_status == "pending"


def test_ambiguous_equal_geometry_is_review_not_silent_fallback():
    cadastre = cadastre_frame()
    cadastre.loc[0, "ano_constr"] = None
    profile = build_hybrid_profile_from_frames(
        cadastre,
        eu_frame([box(0, 0, 10, 10), box(0, 0, 10, 10)], [1960, 1974]),
    )

    assert profile.match.status == "review"
    assert profile.match.ambiguous is True
    assert profile.match.iou_gap == 0.0
    assert profile.resolved.construction_year is None
    assert profile.resolved.construction_year_source == "cadastre.ano_constr"
    assert any("not accepted for fallback" in warning for warning in profile.warnings)


def test_unmatched_profile_uses_guarded_cadastre_proxy():
    profile = build_hybrid_profile_from_frames(
        cadastre_frame(),
        eu_frame([box(100, 100, 110, 110)]),
    )

    assert profile.match.status == "unmatched"
    assert profile.eu.available is False
    assert profile.resolved.residential_area_m2 == 500.0
    assert (
        profile.resolved.residential_area_source
        == "cadastre.geometry_x_altura_max_proxy"
    )


def test_profile_fingerprint_is_deterministic():
    first = build_hybrid_profile_from_frames(
        cadastre_frame(),
        eu_frame([box(0, 0, 10, 10)]),
        tipo15_frame(),
        source_paths={"cadastre": "a", "eu_buildings": "b", "tipo15": "c"},
    )
    second = build_hybrid_profile_from_frames(
        cadastre_frame(),
        eu_frame([box(0, 0, 10, 10)]),
        tipo15_frame(),
        source_paths={"cadastre": "a", "eu_buildings": "b", "tipo15": "c"},
    )

    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


def test_coverage_audit_keeps_accept_review_and_unmatched_separate():
    cadastre = gpd.GeoDataFrame(
        [
            {"refparcela": "A", "geometry": box(0, 0, 10, 10)},
            {"refparcela": "B", "geometry": box(20, 0, 30, 10)},
            {"refparcela": "C", "geometry": box(40, 0, 50, 10)},
        ],
        crs="EPSG:25830",
    )
    eu = eu_frame([
        box(0, 0, 10, 10),
        box(0, 0, 10, 10),
        box(20, -10, 32, 20),
    ])

    audit = audit_hybrid_coverage(cadastre, eu)

    assert audit.cadastre_building_count == 3
    assert audit.eu_category1_building_count == 3
    assert audit.accepted_count == 0
    assert audit.review_count == 2
    assert audit.unmatched_count == 1
    assert audit.accepted_percent == 0.0
    assert audit.review_percent == 66.6667
    assert audit.unmatched_percent == 33.3333
    assert audit.status_confidence_counts == {
        "review_high": 1,
        "review_medium": 1,
        "unmatched_none": 1,
    }


def test_real_pilot_profile_matches_documented_eu_and_tipo15_evidence():
    profile = build_hybrid_profile(
        "4252702YJ2745A",
        cadastre_path=PROJECT / "data/gis/DatosRai_ciudadValencia.shp",
        eu_path=PROJECT / "data/gis/all_bldg_category.gpkg",
        tipo15_path=PROJECT / "data/reference/Tipo15_soloV(in).csv",
    )

    assert profile.match.status == "accepted"
    assert profile.match.eu_building_id == 688184613
    assert profile.match.cadastre_coverage > 0.96
    assert profile.match.iou > 0.85
    assert profile.cadastre.construction_year == 1974
    assert profile.cadastre.population_total == 62
    assert profile.cadastre.dwellings == 35
    assert profile.tipo15.residential_unit_rows == 35
    # The supplied Tipo15 CSV contains 10×89 + 20×83 + 5×72 m².
    assert profile.tipo15.residential_area_m2 == 2910.0
    assert profile.eu.construction_year == 1974
    assert profile.eu.floors == 7
    assert profile.eu.net_floor_area_m2 == 3011.919
    assert profile.resolved.residential_area_source == "tipo15.442_sup_Residencial"
    assert profile.resolved.occupancy_distribution_status == "pending"
