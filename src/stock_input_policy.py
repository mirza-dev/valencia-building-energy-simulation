"""Typed, auditable stock-input policy shared by Parts C and D.

This module owns data preparation only.  It does not contain EnergyPlus,
envelope, representative-energy, carbon, or validation formulae.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import pandas as pd


POLICY_SCHEMA_VERSION = 1
POLICY_REGISTRY_VERSION = "2026-07-22.2"
EXCLUDE_CLUSTER = "__exclude__"
CLUSTER_PATTERN = re.compile(r"^(VivUni|EdiPluri|BlocPluri)(P\d\d)$")
TABULA_CLUSTER_TARGETS = tuple(
    f"{family}P{period:02d}"
    for family in ("VivUni", "EdiPluri", "BlocPluri")
    for period in range(1, 8)
)

FloorInvalidPolicy = Literal[
    "cluster_family_one", "exclude_invalid", "block_run", "fixed_fallback",
]
GroundFloorMode = Literal[
    "tipo15_family_fallback", "family_default", "force_unconditioned", "force_conditioned",
]
ResidentialAreaMode = Literal["tipo15_proxy", "field_proxy", "proxy_only"]
FootprintAreaMode = Literal["field", "geometry_epsg25830"]


class StockPolicyError(ValueError):
    """A structurally un-runnable stock policy."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StockInputPolicy:
    schema_version: int = POLICY_SCHEMA_VERSION
    gis_dataset_id: str = "valencia-city"
    reference_field: str = "refparcela"
    district_field: str = "nombre"
    footprint_area_mode: FootprintAreaMode = "field"
    footprint_area_field: str | None = "Shape_Area"
    floors_field: str = "altura_max"
    cluster_field: str = "cluster"
    cluster_mapping: dict[str, str] = field(default_factory=dict)
    floor_invalid_policy: FloorInvalidPolicy = "cluster_family_one"
    floor_fixed_fallback: int | None = None
    ground_floor_mode: GroundFloorMode = "tipo15_family_fallback"
    residential_area_mode: ResidentialAreaMode = "tipo15_proxy"
    residential_area_field: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "StockInputPolicy":
        raw = dict(value or {})
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise StockPolicyError("unknown_policy_field", f"Unknown stock policy fields: {sorted(unknown)}")
        policy = cls(**raw)
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.schema_version != POLICY_SCHEMA_VERSION:
            raise StockPolicyError("unsupported_schema", f"Stock policy schema must be {POLICY_SCHEMA_VERSION}")
        for name in ("gis_dataset_id", "reference_field", "district_field", "floors_field", "cluster_field"):
            if not str(getattr(self, name)).strip():
                raise StockPolicyError("empty_field", f"{name} cannot be empty")
        if self.footprint_area_mode not in {"field", "geometry_epsg25830"}:
            raise StockPolicyError("invalid_footprint_mode", "Unsupported footprint-area mode")
        if self.footprint_area_mode == "field" and not self.footprint_area_field:
            raise StockPolicyError("missing_footprint_field", "A numeric footprint field is required")
        if self.floor_invalid_policy not in {
            "cluster_family_one", "exclude_invalid", "block_run", "fixed_fallback",
        }:
            raise StockPolicyError("invalid_floor_policy", "Unsupported floor invalid-value policy")
        if self.floor_invalid_policy == "fixed_fallback":
            fallback = self.floor_fixed_fallback
            if (
                fallback is None or isinstance(fallback, bool)
                or not float(fallback).is_integer() or not 1 <= int(fallback) <= 100
            ):
                raise StockPolicyError("invalid_floor_fallback", "Expert floor fallback must be an integer from 1 to 100")
        if self.ground_floor_mode not in {
            "tipo15_family_fallback", "family_default", "force_unconditioned", "force_conditioned",
        }:
            raise StockPolicyError("invalid_ground_mode", "Unsupported ground-floor mode")
        if self.residential_area_mode not in {"tipo15_proxy", "field_proxy", "proxy_only"}:
            raise StockPolicyError("invalid_residential_area_mode", "Unsupported residential-area mode")
        if self.residential_area_mode == "field_proxy" and not self.residential_area_field:
            raise StockPolicyError("missing_residential_area_field", "A numeric residential-area field is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_policy_bytes(policy: StockInputPolicy | dict[str, Any]) -> bytes:
    value = policy.to_dict() if isinstance(policy, StockInputPolicy) else policy
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def policy_fingerprint(policy: StockInputPolicy | dict[str, Any]) -> str:
    return hashlib.sha256(canonical_policy_bytes(policy)).hexdigest()


def geometry_area_25830(frame: gpd.GeoDataFrame) -> pd.Series:
    if frame.crs is None:
        raise StockPolicyError("missing_crs", "GIS geometry has no CRS; EPSG:25830 area cannot be resolved")
    projected = frame if frame.crs.to_epsg() == 25830 else frame.to_crs(25830)
    return projected.geometry.area.astype(float)


def resolve_policy(policy: StockInputPolicy, frame: gpd.GeoDataFrame) -> tuple[StockInputPolicy, dict[str, Any]]:
    """Resolve automatic recognised cluster codes to an explicit immutable map."""
    policy.validate()
    required = {policy.reference_field, policy.floors_field, policy.cluster_field, policy.district_field}
    if policy.footprint_area_mode == "field" and policy.footprint_area_field:
        required.add(policy.footprint_area_field)
    if policy.residential_area_mode == "field_proxy" and policy.residential_area_field:
        required.add(policy.residential_area_field)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise StockPolicyError("missing_fields", f"Selected GIS fields are missing: {missing}")
    numeric_fields = {policy.floors_field}
    if policy.footprint_area_mode == "field" and policy.footprint_area_field:
        numeric_fields.add(policy.footprint_area_field)
    if policy.residential_area_mode == "field_proxy" and policy.residential_area_field:
        numeric_fields.add(policy.residential_area_field)
    incompatible = sorted(
        field_name for field_name in numeric_fields
        if pd.to_numeric(frame[field_name], errors="coerce").notna().sum() == 0
    )
    if incompatible:
        raise StockPolicyError(
            "invalid_numeric_field",
            f"Selected numeric fields contain no usable numeric values: {incompatible}",
        )
    refs = frame[policy.reference_field].astype("string").str.strip()
    if refs.isna().any() or (refs == "").any():
        raise StockPolicyError("missing_reference", "The selected reference field contains empty values")
    source_values = sorted({str(value).strip() for value in frame[policy.cluster_field].dropna() if str(value).strip()})
    mapping = {str(key).strip(): str(value).strip() for key, value in policy.cluster_mapping.items()}
    automatic: list[str] = []
    for value in source_values:
        if value not in mapping and CLUSTER_PATTERN.fullmatch(value):
            mapping[value] = value
            automatic.append(value)
    unmapped = sorted(set(source_values) - set(mapping))
    if unmapped:
        raise StockPolicyError("unmapped_clusters", f"Every source cluster value needs a TABULA mapping or Exclude: {unmapped}")
    invalid_targets = sorted({target for target in mapping.values() if target != EXCLUDE_CLUSTER and not CLUSTER_PATTERN.fullmatch(target)})
    if invalid_targets:
        raise StockPolicyError("invalid_cluster_targets", f"Unknown TABULA target classes: {invalid_targets}")
    resolved = StockInputPolicy.from_dict(policy.to_dict() | {"cluster_mapping": mapping})
    return resolved, {
        "cluster_source_values": source_values,
        "auto_mapped_cluster_values": automatic,
        "excluded_cluster_values": sorted(key for key, target in mapping.items() if target == EXCLUDE_CLUSTER),
    }


def _tipo15_area_map(tipo15_path: Path) -> pd.Series:
    ledger = pd.read_csv(
        tipo15_path, sep=";", encoding="latin-1",
        usecols=["31_pc", "442_sup_Residencial"], dtype={"31_pc": str},
    )
    values = pd.to_numeric(ledger["442_sup_Residencial"], errors="coerce")
    return values.groupby(ledger["31_pc"]).sum(min_count=1)


def _apply_scope(
    frame: gpd.GeoDataFrame, policy: StockInputPolicy, *, boundary_path: Path | None,
    district: str | None, reference: str | None,
) -> tuple[gpd.GeoDataFrame, str]:
    if reference:
        key = reference.strip().upper()
        references = frame[policy.reference_field].astype(str).str.upper().str.strip()
        scoped = frame[references == key].copy()
        if scoped.empty:
            raise StockPolicyError(
                "empty_scope", f"Reference {reference!r} has no rows in the selected GIS dataset",
            )
        return scoped, f"{key} (building)"
    if district:
        key = district.strip().upper()
        names = frame[policy.district_field].astype(str).str.upper().str.strip()
        if key not in set(names):
            raise StockPolicyError("empty_scope", f"District {district!r} has no rows in the selected GIS dataset")
        return frame[names == key].copy(), f"{key} (district)"
    if boundary_path is not None:
        boundary = gpd.read_file(boundary_path).to_crs(frame.crs)
        scoped = frame[frame.geometry.centroid.within(boundary.union_all())].copy()
        if scoped.empty:
            raise StockPolicyError("empty_scope", "The boundary contains no buildings in the selected GIS dataset")
        return scoped, "Benicalap (boundary)"
    return frame.copy(), "Valencia"


def prepare_stock(
    gis_path: Path, tipo15_path: Path, policy: StockInputPolicy, *,
    boundary_path: Path | None = None, district: str | None = None,
    reference: str | None = None,
    duplicate_parcel_apportioning: bool = False,
) -> tuple[gpd.GeoDataFrame, StockInputPolicy, dict[str, Any]]:
    """Prepare canonical Part C/D stock and return auditable counters."""
    raw = gpd.read_file(gis_path)
    resolved, resolution = resolve_policy(policy, raw)
    stock, scope_label = _apply_scope(
        raw, resolved, boundary_path=boundary_path, district=district, reference=reference,
    )
    scoped_buildings = int(len(stock))
    geometry_area = geometry_area_25830(stock)

    stock["refparcela"] = stock[resolved.reference_field].astype(str).str.strip()
    # Downstream Part D aggregation keeps its proven formula and reads the
    # canonical column; the policy only controls which raw source feeds it.
    stock["nombre"] = stock[resolved.district_field]
    stock["source_cluster_value"] = stock[resolved.cluster_field].astype("string").fillna("").str.strip()
    stock["cluster"] = stock["source_cluster_value"].map(resolved.cluster_mapping)
    excluded_cluster = stock["cluster"].eq(EXCLUDE_CLUSTER)
    missing_cluster = stock["cluster"].isna()
    if missing_cluster.any():
        raise StockPolicyError("unmapped_clusters", "Scoped stock contains an unmapped cluster value")
    stock = stock[~excluded_cluster].copy()
    geometry_area = geometry_area.loc[stock.index]
    if stock.empty:
        raise StockPolicyError("zero_valid_clusters", "Cluster mapping excludes every building in scope")

    parsed = stock["cluster"].str.extract(CLUSTER_PATTERN)
    stock["family"], stock["period"] = parsed[0], parsed[1]
    if stock["family"].isna().any():
        raise StockPolicyError("invalid_cluster_targets", "Resolved stock contains an invalid TABULA class")

    if resolved.footprint_area_mode == "geometry_epsg25830":
        footprint = geometry_area.copy()
        footprint_fallback = pd.Series(False, index=stock.index)
    else:
        footprint = pd.to_numeric(stock[resolved.footprint_area_field], errors="coerce")
        footprint_fallback = footprint.isna() | (footprint <= 0)
        footprint = footprint.where(~footprint_fallback, geometry_area)
    if footprint.isna().any() or (footprint <= 0).any():
        raise StockPolicyError("invalid_footprint", "No positive footprint area can be resolved for every retained building")
    stock["footprint_area_m2"] = footprint.astype(float)
    stock["footprint_area_fallback"] = footprint_fallback.astype(bool)

    floors_numeric = pd.to_numeric(stock[resolved.floors_field], errors="coerce")
    valid_floor = floors_numeric.notna() & (floors_numeric >= 1) & (floors_numeric <= 100) & ((floors_numeric % 1).abs() < 1e-9)
    stock["imputed_floors"] = ~valid_floor
    invalid_floor_count = int((~valid_floor).sum())
    if invalid_floor_count and resolved.floor_invalid_policy == "block_run":
        raise StockPolicyError("invalid_floors", f"{invalid_floor_count} buildings have invalid floor values")
    if resolved.floor_invalid_policy == "exclude_invalid":
        stock = stock[valid_floor].copy()
        floors_numeric = floors_numeric.loc[stock.index]
    elif resolved.floor_invalid_policy == "fixed_fallback":
        floors_numeric = floors_numeric.where(valid_floor, int(resolved.floor_fixed_fallback))
    else:
        floors_numeric = floors_numeric.where(valid_floor)
        cluster_median = floors_numeric.groupby(stock["cluster"]).transform("median")
        family_median = floors_numeric.groupby(stock["family"]).transform("median")
        floors_numeric = floors_numeric.fillna(cluster_median).fillna(family_median).fillna(1)
    if stock.empty:
        raise StockPolicyError("zero_valid_buildings", "Floor policy excludes every building in scope")
    stock["altura_max"] = floors_numeric.loc[stock.index].round().astype(int)

    area_map = _tipo15_area_map(tipo15_path)
    tipo15_candidate = stock["refparcela"].map(area_map)
    if resolved.residential_area_mode == "tipo15_proxy":
        residential = tipo15_candidate.copy()
    elif resolved.residential_area_mode == "field_proxy":
        residential = pd.to_numeric(stock[resolved.residential_area_field], errors="coerce")
    else:
        residential = pd.Series(float("nan"), index=stock.index)
    residential = residential.where(residential > 0)
    stock["res_area_proxy"] = residential.isna()
    stock["dup_refparcela"] = stock["refparcela"].duplicated(keep=False)
    apportioned_rows = 0
    if duplicate_parcel_apportioning and resolved.residential_area_mode == "tipo15_proxy":
        for reference, group in stock[stock["dup_refparcela"]].groupby("refparcela"):
            total = area_map.get(reference)
            denominator = group["footprint_area_m2"].sum()
            if total is None or pd.isna(total) or denominator <= 0:
                continue
            residential.loc[group.index] = float(total) * group["footprint_area_m2"] / denominator
            apportioned_rows += len(group)

    # Proxy ratios use the selected area's own valid observations.  Proxy-only
    # deliberately uses Tipo15 as a calibration ledger without assigning its
    # parcel totals directly.
    ratio_source = residential.copy()
    if resolved.residential_area_mode == "proxy_only":
        ratio_source = tipo15_candidate.where(tipo15_candidate > 0)
    valid_ratio = ratio_source.notna() & (stock["footprint_area_m2"] > 0)
    ratios = ratio_source[valid_ratio] / stock.loc[valid_ratio, "footprint_area_m2"]
    ratio_clusters = stock.loc[valid_ratio, "cluster"]
    # A one-building scope may itself be one of the Tipo15 gaps.  In that
    # case, retain the declared cluster-ratio fallback by calibrating it from
    # the selected GIS dataset, without expanding the run scope.
    if ratios.empty:
        calibration_refs = raw[resolved.reference_field].astype(str).str.strip()
        calibration_clusters = raw[resolved.cluster_field].astype("string").fillna("").str.strip().map(
            resolved.cluster_mapping
        )
        calibration_geometry = geometry_area_25830(raw)
        if resolved.footprint_area_mode == "geometry_epsg25830":
            calibration_footprint = calibration_geometry
        else:
            calibration_footprint = pd.to_numeric(
                raw[resolved.footprint_area_field], errors="coerce",
            )
            calibration_footprint = calibration_footprint.where(
                calibration_footprint > 0, calibration_geometry,
            )
        if resolved.residential_area_mode == "field_proxy":
            calibration_area = pd.to_numeric(
                raw[resolved.residential_area_field], errors="coerce",
            )
        else:
            calibration_area = calibration_refs.map(area_map)
        calibration_valid = (
            calibration_area.notna() & (calibration_area > 0)
            & calibration_footprint.notna() & (calibration_footprint > 0)
            & calibration_clusters.notna() & calibration_clusters.ne(EXCLUDE_CLUSTER)
        )
        ratios = calibration_area[calibration_valid] / calibration_footprint[calibration_valid]
        ratio_clusters = calibration_clusters[calibration_valid]
        if ratios.empty:
            raise StockPolicyError(
                "no_area_proxy_basis", "Residential-area proxy has no valid calibration rows",
            )
    ratio_by_cluster = ratios.groupby(ratio_clusters).median()
    ratio_global = float(ratios.median())
    proxy_mask = residential.isna()
    for index in residential.index[proxy_mask]:
        ratio = ratio_by_cluster.get(stock.at[index, "cluster"], ratio_global)
        residential.at[index] = float(stock.at[index, "footprint_area_m2"]) * float(ratio)
    if residential.isna().any() or (residential <= 0).any():
        raise StockPolicyError("invalid_residential_area", "No positive residential area can be resolved for every retained building")
    stock["res_area_m2"] = residential.astype(float)

    report = {
        "scope": scope_label,
        "source_buildings": int(len(raw)),
        "scoped_buildings": scoped_buildings,
        "retained_buildings": int(len(stock)),
        "clusters": int(stock["cluster"].nunique()),
        "invalid_floor_buildings": invalid_floor_count,
        "imputed_floor_buildings": int(stock["imputed_floors"].sum()),
        "excluded_floor_buildings": invalid_floor_count if resolved.floor_invalid_policy == "exclude_invalid" else 0,
        "fixed_floor_fallback_buildings": invalid_floor_count if resolved.floor_invalid_policy == "fixed_fallback" else 0,
        "excluded_cluster_buildings": int(excluded_cluster.sum()),
        "footprint_geometry_fallback_buildings": int(stock["footprint_area_fallback"].sum()),
        "residential_area_proxy_buildings": int(stock["res_area_proxy"].sum()),
        "duplicate_parcel_rows": int(stock["dup_refparcela"].sum()),
        "duplicate_parcel_apportioned_rows": int(apportioned_rows),
        "residential_area_m2": float(stock["res_area_m2"].sum()),
        **resolution,
    }
    stock.attrs["stock_policy_report"] = report
    stock.attrs["stock_input_policy"] = resolved.to_dict()
    stock.attrs["stock_input_policy_fingerprint"] = policy_fingerprint(resolved)
    return stock, resolved, report


def ground_rule_for_representative(
    reference: str, family: str, tipo15_path: Path, mode: GroundFloorMode,
    family_defaults: dict[str, bool],
) -> tuple[bool, str]:
    if mode == "force_unconditioned":
        return True, "forced_unconditioned"
    if mode == "force_conditioned":
        return False, "forced_conditioned"
    default = bool(family_defaults[family])
    if mode == "family_default":
        return default, "family_default"
    ledger = pd.read_csv(tipo15_path, sep=";", encoding="latin-1", usecols=["31_pc", "252_planta"], dtype=str)
    rows = ledger[ledger["31_pc"] == reference]
    if rows.empty:
        return default, "family_fallback"
    plantas = rows["252_planta"].fillna("").str.strip().str.upper()
    has_ground_dwelling = plantas.isin({"0", "00", "BJ", "B", "BX"}).any()
    return not bool(has_ground_dwelling), "tipo15"
