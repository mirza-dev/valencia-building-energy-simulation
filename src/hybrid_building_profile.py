"""Build a provenance-rich cadastre + EU profile for one Valencia building.

The cadastre remains the authoritative stock spine.  EU Building Database
attributes are supporting evidence and may only fill a missing field when a
single, high-confidence geometry match is available.  This module deliberately
does not mutate an OpenStudio model; it establishes the input contract that the
next deep-building pipeline stage will consume.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = 1
CADASTRE_CRS = "EPSG:25830"
DEFAULT_CADASTRE = Path("data/gis/DatosRai_ciudadValencia.shp")
DEFAULT_EU_BUILDINGS = Path("data/gis/all_bldg_category.gpkg")
DEFAULT_TIPO15 = Path("data/reference/Tipo15_soloV(in).csv")


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeometryMatch(ProfileModel):
    status: Literal["accepted", "review", "unmatched"]
    confidence: Literal["high", "medium", "low", "none"]
    reason: str
    candidate_count: int = 0
    eu_feature_id: int | str | None = None
    eu_building_id: int | str | None = None
    intersection_m2: float | None = None
    cadastre_coverage: float | None = None
    eu_coverage: float | None = None
    iou: float | None = None
    representative_point_inside: bool | None = None
    second_best_iou: float | None = None
    iou_gap: float | None = None
    ambiguous: bool = False


class CadastreEvidence(ProfileModel):
    refparcela: str
    district: str | None = None
    climate_zone: str | None = None
    principal_use: str | None = None
    cluster: str | None = None
    construction_year: int | None = None
    residential_floors: int | None = None
    footprint_attribute_m2: float | None = None
    footprint_geometry_m2: float
    dwellings: int | None = None
    population_total: int | None = None
    population_0_14: int | None = None
    population_15_65: int | None = None
    population_66_plus: int | None = None
    geometry_sha256: str


class Tipo15Evidence(ProfileModel):
    available: bool
    row_count: int = 0
    residential_unit_rows: int = 0
    residential_area_m2: float | None = None
    construction_years: list[int] = Field(default_factory=list)
    source_field_reference: str = "31_pc"
    source_field_use: str = "428_uso"
    source_field_residential_area: str = "442_sup_Residencial"


class EuEvidence(ProfileModel):
    available: bool
    feature_id: int | str | None = None
    building_id: int | str | None = None
    construction_year: int | None = None
    floors: int | None = None
    height_m: float | None = None
    occupancy_code: str | None = None
    dwelling_code: str | None = None
    net_floor_area_m2: float | None = None
    gross_floor_area_m2: float | None = None
    geometry_area_m2: float | None = None
    geometry_sha256: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    taxonomy: dict[str, Any] = Field(default_factory=dict)


class ResolvedBuildingInputs(ProfileModel):
    geometry_source: Literal["cadastre"] = "cadastre"
    construction_year: int | None = None
    construction_year_source: str
    residential_floors: int | None = None
    residential_floors_source: Literal["cadastre.altura_max"] = "cadastre.altura_max"
    footprint_m2: float
    footprint_source: Literal["cadastre.geometry"] = "cadastre.geometry"
    residential_area_m2: float | None = None
    residential_area_source: str
    dwellings: int | None = None
    dwellings_source: Literal["cadastre.num_vivend"] = "cadastre.num_vivend"
    occupants: int | None = None
    occupants_source: Literal["cadastre.pob_total"] = "cadastre.pob_total"
    cluster: str | None = None
    cluster_source: Literal["cadastre.cluster"] = "cadastre.cluster"
    occupancy_schedule: str = "Ocupacion Vivienda CTE"
    occupancy_distribution_status: Literal["pending"] = "pending"
    uniform_people_per_floor_candidate: float | None = None


class HybridBuildingProfile(ProfileModel):
    schema_version: int = SCHEMA_VERSION
    refparcela: str
    match: GeometryMatch
    cadastre: CadastreEvidence
    tipo15: Tipo15Evidence
    eu: EuEvidence
    resolved: ResolvedBuildingInputs
    warnings: list[str] = Field(default_factory=list)
    source_paths: dict[str, str]
    fingerprint: str


class HybridCoverageAudit(ProfileModel):
    schema_version: int = SCHEMA_VERSION
    cadastre_building_count: int
    eu_category1_building_count: int
    accepted_count: int
    accepted_percent: float
    review_count: int
    review_percent: float
    unmatched_count: int
    unmatched_percent: float
    ambiguous_candidate_count: int
    status_confidence_counts: dict[str, int]
    matching_policy: str


def _project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "data" / "gis").exists():
            return parent
    raise FileNotFoundError("Could not find project root containing data/gis")


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any, *, minimum: int | None = None) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    result = int(number)
    if minimum is not None and result < minimum:
        return None
    return result


def _text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    result = str(value).strip()
    return result or None


def _taxonomy_number(taxonomy: dict[str, Any], key: str, prefix: str) -> float | None:
    raw = _text(taxonomy.get(key))
    if raw is None or not raw.startswith(prefix):
        return None
    return _number(raw[len(prefix):])


def _geometry_sha256(geometry) -> str:
    return hashlib.sha256(geometry.wkb).hexdigest()


def load_cadastre_record(path: Path, refparcela: str) -> gpd.GeoDataFrame:
    escaped = str(refparcela).replace("'", "''")
    frame = gpd.read_file(path, where=f"refparcela = '{escaped}'")
    if frame.empty:
        raise ValueError(f"Cadastre refparcela {refparcela!r} was not found in {path}")
    if frame.crs is None or frame.crs.to_epsg() != 25830:
        raise ValueError(f"Cadastre must use EPSG:25830, got {frame.crs}")
    if len(frame) != 1:
        raise ValueError(
            f"Cadastre refparcela {refparcela!r} has {len(frame)} rows; "
            "the deep-building contract requires an explicit multipart decision"
        )
    geometry = frame.iloc[0].geometry
    if geometry is None or geometry.is_empty or not geometry.is_valid:
        raise ValueError(f"Cadastre refparcela {refparcela!r} has invalid geometry")
    return frame


def load_cadastre_buildings(path: Path) -> gpd.GeoDataFrame:
    frame = gpd.read_file(path)
    if frame.crs is None or frame.crs.to_epsg() != 25830:
        raise ValueError(f"Cadastre must use EPSG:25830, got {frame.crs}")
    if frame.empty:
        raise ValueError(f"Cadastre dataset is empty: {path}")
    invalid = frame.geometry.isna() | frame.geometry.is_empty | ~frame.geometry.is_valid
    if invalid.any():
        raise ValueError(
            f"Cadastre contains {int(invalid.sum())} null, empty, or invalid geometries"
        )
    return frame


def load_eu_buildings(path: Path, target_crs: Any = CADASTRE_CRS) -> gpd.GeoDataFrame:
    frame = gpd.read_file(path)
    if frame.crs is None:
        raise ValueError(f"EU dataset has no CRS: {path}")
    if "category" in frame.columns:
        frame = frame[pd.to_numeric(frame["category"], errors="coerce").eq(1)].copy()
    if frame.empty:
        raise ValueError(f"EU category-1 building dataset is empty: {path}")
    return frame.to_crs(target_crs).reset_index(drop=True)


def load_tipo15(path: Path) -> pd.DataFrame:
    return pd.read_csv(
        path,
        sep=";",
        encoding="latin-1",
        usecols=["31_pc", "372_ant", "428_uso", "442_sup_Residencial"],
        dtype={"31_pc": "string", "428_uso": "string"},
        low_memory=False,
    )


def summarize_tipo15(frame: pd.DataFrame | None, refparcela: str) -> Tipo15Evidence:
    if frame is None or frame.empty or "31_pc" not in frame.columns:
        return Tipo15Evidence(available=False)
    rows = frame[frame["31_pc"].astype("string").str.strip().eq(str(refparcela))]
    if rows.empty:
        return Tipo15Evidence(available=False)
    use = rows["428_uso"].astype("string").str.strip().str.upper()
    residential = rows[use.eq("V")]
    area = pd.to_numeric(residential["442_sup_Residencial"], errors="coerce")
    years = sorted({
        year for year in (_integer(value, minimum=1000) for value in rows["372_ant"])
        if year is not None
    })
    area_sum = float(area.dropna().sum()) if area.notna().any() else None
    return Tipo15Evidence(
        available=True,
        row_count=len(rows),
        residential_unit_rows=len(residential),
        residential_area_m2=round(area_sum, 3) if area_sum is not None else None,
        construction_years=years,
    )


def _eu_identity(row: pd.Series) -> tuple[int | str | None, int | str | None]:
    attributes = _json_object(row.get("attributes"))
    feature_id = row.get("id")
    if feature_id is not None and pd.isna(feature_id):
        feature_id = None
    building_id = attributes.get("building_id", row.get("building_id"))
    if building_id is not None and pd.isna(building_id):
        building_id = None
    for name, value in (("feature", feature_id), ("building", building_id)):
        number = _integer(value)
        if number is not None:
            if name == "feature":
                feature_id = number
            else:
                building_id = number
    return feature_id, building_id


def match_eu_building(cadastre_geometry, eu_buildings: gpd.GeoDataFrame) -> tuple[GeometryMatch, pd.Series | None]:
    if eu_buildings.empty:
        return GeometryMatch(
            status="unmatched", confidence="none", reason="EU dataset is empty"
        ), None
    candidate_indexes = list(eu_buildings.sindex.query(cadastre_geometry, predicate="intersects"))
    if not candidate_indexes:
        return GeometryMatch(
            status="unmatched",
            confidence="none",
            reason="No intersecting EU category-1 geometry",
        ), None

    cad_area = float(cadastre_geometry.area)
    point = cadastre_geometry.representative_point()
    scored: list[dict[str, Any]] = []
    for index in candidate_indexes:
        row = eu_buildings.iloc[int(index)]
        geometry = row.geometry
        intersection = float(cadastre_geometry.intersection(geometry).area)
        eu_area = float(geometry.area)
        union = cad_area + eu_area - intersection
        feature_id, building_id = _eu_identity(row)
        scored.append({
            "index": int(index),
            "feature_id": feature_id,
            "building_id": building_id,
            "intersection": intersection,
            "cadastre_coverage": intersection / cad_area if cad_area else 0.0,
            "eu_coverage": intersection / eu_area if eu_area else 0.0,
            "iou": intersection / union if union else 0.0,
            "point_inside": bool(geometry.covers(point)),
        })
    scored.sort(
        key=lambda item: (
            -item["iou"],
            -item["cadastre_coverage"],
            -item["eu_coverage"],
            str(item["building_id"] or ""),
            str(item["feature_id"] or ""),
        )
    )
    best = scored[0]
    second_iou = scored[1]["iou"] if len(scored) > 1 else None
    gap = best["iou"] - second_iou if second_iou is not None else None
    high = (
        best["iou"] >= 0.50
        or (
            best["point_inside"]
            and best["cadastre_coverage"] >= 0.80
            and best["eu_coverage"] >= 0.50
        )
    )
    medium = (
        not high
        and best["point_inside"]
        and best["cadastre_coverage"] >= 0.50
    )
    ambiguous = second_iou is not None and gap is not None and gap < 0.10
    if high and not ambiguous:
        status: Literal["accepted", "review", "unmatched"] = "accepted"
        confidence: Literal["high", "medium", "low", "none"] = "high"
        reason = "Unique high-overlap EU geometry"
    elif high:
        status, confidence = "review", "high"
        reason = "High overlap but competing EU candidates are too similar"
    elif medium:
        status, confidence = "review", "medium"
        reason = "Representative point and majority overlap agree, but IoU is below 0.50"
    else:
        status, confidence = "unmatched", "low"
        reason = "Intersecting EU evidence does not meet the guarded match threshold"

    match = GeometryMatch(
        status=status,
        confidence=confidence,
        reason=reason,
        candidate_count=len(scored),
        eu_feature_id=best["feature_id"],
        eu_building_id=best["building_id"],
        intersection_m2=round(best["intersection"], 3),
        cadastre_coverage=round(best["cadastre_coverage"], 6),
        eu_coverage=round(best["eu_coverage"], 6),
        iou=round(best["iou"], 6),
        representative_point_inside=best["point_inside"],
        second_best_iou=round(second_iou, 6) if second_iou is not None else None,
        iou_gap=round(gap, 6) if gap is not None else None,
        ambiguous=ambiguous,
    )
    return match, eu_buildings.iloc[best["index"]]


def extract_eu_evidence(row: pd.Series | None) -> EuEvidence:
    if row is None:
        return EuEvidence(available=False)
    attributes = _json_object(row.get("attributes"))
    taxonomy = _json_object(row.get("taxonomy"))
    feature_id, building_id = _eu_identity(row)
    year = _taxonomy_number(taxonomy, "DAT", "Y:")
    floors = _taxonomy_number(taxonomy, "HEI", "H:")
    height = _taxonomy_number(taxonomy, "HIM", "HHT:")
    geometry = row.geometry
    return EuEvidence(
        available=True,
        feature_id=feature_id,
        building_id=building_id,
        construction_year=_integer(year, minimum=1000),
        floors=_integer(floors, minimum=1),
        height_m=round(height, 3) if height is not None else None,
        occupancy_code=_text(taxonomy.get("OCC")),
        dwelling_code=_text(taxonomy.get("OCD")),
        net_floor_area_m2=_number(attributes.get("net_floor_area")),
        gross_floor_area_m2=_number(attributes.get("gross_floor_area")),
        geometry_area_m2=round(float(geometry.area), 3),
        geometry_sha256=_geometry_sha256(geometry),
        attributes=attributes,
        taxonomy=taxonomy,
    )


def _cadastre_evidence(row: pd.Series) -> CadastreEvidence:
    geometry = row.geometry
    return CadastreEvidence(
        refparcela=str(row["refparcela"]),
        district=_text(row.get("nombre")),
        climate_zone=_text(row.get("zona_clima")),
        principal_use=_text(row.get("uso_princi")),
        cluster=_text(row.get("cluster")),
        construction_year=_integer(row.get("ano_constr"), minimum=1000),
        residential_floors=_integer(row.get("altura_max"), minimum=1),
        footprint_attribute_m2=_number(row.get("Shape_Area")),
        footprint_geometry_m2=round(float(geometry.area), 3),
        dwellings=_integer(row.get("num_vivend"), minimum=0),
        population_total=_integer(row.get("pob_total"), minimum=0),
        population_0_14=_integer(row.get("pob_0_14"), minimum=0),
        population_15_65=_integer(row.get("pob_15_65"), minimum=0),
        population_66_plus=_integer(row.get("pob_66_mas"), minimum=0),
        geometry_sha256=_geometry_sha256(geometry),
    )


def _resolve_inputs(
    cadastre: CadastreEvidence,
    tipo15: Tipo15Evidence,
    eu: EuEvidence,
    match: GeometryMatch,
) -> ResolvedBuildingInputs:
    accepted_eu = eu.available and match.status == "accepted"
    construction_year = cadastre.construction_year
    construction_year_source = "cadastre.ano_constr"
    if construction_year is None and accepted_eu:
        construction_year = eu.construction_year
        construction_year_source = "eu.taxonomy.DAT"

    residential_area = tipo15.residential_area_m2
    residential_area_source = "tipo15.442_sup_Residencial"
    if residential_area is None and accepted_eu and eu.net_floor_area_m2 is not None:
        residential_area = eu.net_floor_area_m2
        residential_area_source = "eu.attributes.net_floor_area"
    if residential_area is None and cadastre.residential_floors is not None:
        residential_area = (
            cadastre.footprint_geometry_m2 * cadastre.residential_floors
        )
        residential_area_source = "cadastre.geometry_x_altura_max_proxy"

    per_floor = None
    if (
        cadastre.population_total is not None
        and cadastre.residential_floors is not None
        and cadastre.residential_floors > 0
    ):
        per_floor = round(
            cadastre.population_total / cadastre.residential_floors, 3
        )
    return ResolvedBuildingInputs(
        construction_year=construction_year,
        construction_year_source=construction_year_source,
        residential_floors=cadastre.residential_floors,
        footprint_m2=cadastre.footprint_geometry_m2,
        residential_area_m2=round(residential_area, 3) if residential_area is not None else None,
        residential_area_source=residential_area_source,
        dwellings=cadastre.dwellings,
        occupants=cadastre.population_total,
        cluster=cadastre.cluster,
        uniform_people_per_floor_candidate=per_floor,
    )


def _warnings(
    cadastre: CadastreEvidence,
    tipo15: Tipo15Evidence,
    eu: EuEvidence,
    match: GeometryMatch,
) -> list[str]:
    warnings: list[str] = []
    if match.status != "accepted":
        warnings.append(
            "EU evidence is not accepted for fallback resolution: " + match.reason
        )
    if (
        match.status == "accepted"
        and cadastre.construction_year is not None
        and eu.construction_year is not None
        and cadastre.construction_year != eu.construction_year
    ):
        warnings.append(
            "Construction-year disagreement: "
            f"cadastre {cadastre.construction_year}, EU {eu.construction_year}"
        )
    if eu.floors is not None and cadastre.residential_floors is not None:
        warnings.append(
            "EU HEI floors and cadastre altura_max have different scope and are "
            f"not substituted (EU {eu.floors}, cadastre {cadastre.residential_floors})"
        )
    if (
        tipo15.available
        and cadastre.dwellings is not None
        and tipo15.residential_unit_rows != cadastre.dwellings
    ):
        warnings.append(
            "Dwelling-count disagreement: "
            f"cadastre {cadastre.dwellings}, Tipo15 residential rows "
            f"{tipo15.residential_unit_rows}"
        )
    if (
        match.status == "accepted"
        and tipo15.residential_area_m2 is not None
        and eu.net_floor_area_m2 is not None
        and tipo15.residential_area_m2 > 0
    ):
        delta = abs(eu.net_floor_area_m2 - tipo15.residential_area_m2) / tipo15.residential_area_m2
        if delta > 0.10:
            warnings.append(
                "Residential-area disagreement exceeds 10%: "
                f"Tipo15 {tipo15.residential_area_m2:.1f} m², "
                f"EU net {eu.net_floor_area_m2:.1f} m²"
            )
    warnings.append(
        "Real pob_total is resolved at building level; equal distribution by "
        "residential floor is only a candidate and is not yet applied to OpenStudio"
    )
    return warnings


def build_hybrid_profile_from_frames(
    cadastre_frame: gpd.GeoDataFrame,
    eu_buildings: gpd.GeoDataFrame,
    tipo15_frame: pd.DataFrame | None = None,
    *,
    source_paths: dict[str, str] | None = None,
) -> HybridBuildingProfile:
    if len(cadastre_frame) != 1:
        raise ValueError("Exactly one cadastre row is required")
    if cadastre_frame.crs is None:
        raise ValueError("Cadastre frame has no CRS")
    if eu_buildings.crs is None:
        raise ValueError("EU frame has no CRS")
    if cadastre_frame.crs != eu_buildings.crs:
        eu_buildings = eu_buildings.to_crs(cadastre_frame.crs)
    row = cadastre_frame.iloc[0]
    cadastre = _cadastre_evidence(row)
    tipo15 = summarize_tipo15(tipo15_frame, cadastre.refparcela)
    match, eu_row = match_eu_building(row.geometry, eu_buildings)
    eu = extract_eu_evidence(eu_row)
    resolved = _resolve_inputs(cadastre, tipo15, eu, match)
    paths = source_paths or {}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "refparcela": cadastre.refparcela,
        "match": match.model_dump(mode="json"),
        "cadastre": cadastre.model_dump(mode="json"),
        "tipo15": tipo15.model_dump(mode="json"),
        "eu": eu.model_dump(mode="json"),
        "resolved": resolved.model_dump(mode="json"),
        "warnings": _warnings(cadastre, tipo15, eu, match),
        "source_paths": paths,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return HybridBuildingProfile(
        **payload,
        fingerprint=hashlib.sha256(canonical).hexdigest(),
    )


def build_hybrid_profile(
    refparcela: str,
    *,
    cadastre_path: Path,
    eu_path: Path,
    tipo15_path: Path | None = None,
) -> HybridBuildingProfile:
    cadastre_frame = load_cadastre_record(cadastre_path, refparcela)
    eu_buildings = load_eu_buildings(eu_path, cadastre_frame.crs)
    tipo15_frame = load_tipo15(tipo15_path) if tipo15_path is not None else None
    return build_hybrid_profile_from_frames(
        cadastre_frame,
        eu_buildings,
        tipo15_frame,
        source_paths={
            "cadastre": str(cadastre_path.resolve()),
            "eu_buildings": str(eu_path.resolve()),
            "tipo15": str(tipo15_path.resolve()) if tipo15_path is not None else "",
        },
    )


def audit_hybrid_coverage(
    cadastre_frame: gpd.GeoDataFrame,
    eu_buildings: gpd.GeoDataFrame,
) -> HybridCoverageAudit:
    if cadastre_frame.crs is None:
        raise ValueError("Cadastre frame has no CRS")
    if eu_buildings.crs is None:
        raise ValueError("EU frame has no CRS")
    if cadastre_frame.crs != eu_buildings.crs:
        eu_buildings = eu_buildings.to_crs(cadastre_frame.crs)

    counts: Counter[tuple[str, str]] = Counter()
    ambiguous = 0
    for geometry in cadastre_frame.geometry:
        match, _ = match_eu_building(geometry, eu_buildings)
        counts[(match.status, match.confidence)] += 1
        ambiguous += int(match.ambiguous)

    total = len(cadastre_frame)
    by_status = {
        status: sum(
            count for (candidate_status, _), count in counts.items()
            if candidate_status == status
        )
        for status in ("accepted", "review", "unmatched")
    }
    keyed_counts = {
        f"{status}_{confidence}": count
        for (status, confidence), count in sorted(counts.items())
    }
    return HybridCoverageAudit(
        cadastre_building_count=total,
        eu_category1_building_count=len(eu_buildings),
        accepted_count=by_status["accepted"],
        accepted_percent=round(100 * by_status["accepted"] / total, 4),
        review_count=by_status["review"],
        review_percent=round(100 * by_status["review"] / total, 4),
        unmatched_count=by_status["unmatched"],
        unmatched_percent=round(100 * by_status["unmatched"] / total, 4),
        ambiguous_candidate_count=ambiguous,
        status_confidence_counts=keyed_counts,
        matching_policy=(
            "Accept a unique match when IoU >= 0.50, or when the cadastre "
            "representative point is inside the EU footprint with cadastre "
            "coverage >= 0.80 and EU coverage >= 0.50; review a high match "
            "when the second-best IoU gap is < 0.10."
        ),
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    project = _project_root()
    parser = argparse.ArgumentParser(
        description="Create one guarded cadastre + EU hybrid building profile"
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--refparcela")
    action.add_argument(
        "--audit-coverage",
        action="store_true",
        help="Audit the guarded geometry match over the complete cadastre",
    )
    parser.add_argument("--cadastre", type=Path, default=project / DEFAULT_CADASTRE)
    parser.add_argument("--eu", type=Path, default=project / DEFAULT_EU_BUILDINGS)
    parser.add_argument("--tipo15", type=Path, default=project / DEFAULT_TIPO15)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.audit_coverage:
        cadastre_frame = load_cadastre_buildings(args.cadastre)
        eu_buildings = load_eu_buildings(args.eu, cadastre_frame.crs)
        result: ProfileModel = audit_hybrid_coverage(
            cadastre_frame,
            eu_buildings,
        )
    else:
        result = build_hybrid_profile(
            args.refparcela,
            cadastre_path=args.cadastre,
            eu_path=args.eu,
            tipo15_path=args.tipo15,
        )
    rendered = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, indent=2
    ) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
