"""Application services shared by FastAPI and the isolated OpenStudio worker."""

from __future__ import annotations

import csv
import base64
import hashlib
import inspect
import json
import math
import os
import shutil
import threading
import uuid
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import openstudio
import pandas as pd
import mapbox_vector_tile
from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon, box, mapping

import model_builder as mb
import climate as climate_domain
import template_contract
from model_config import (
    BuildConfig,
    config_for_profile,
    profile_catalog,
    validate_override_provenance,
)
from workbench import db, file_inputs
from workbench import integrity, renderer_provenance, storage
from workbench.data_dictionary import TIPO15_PATH, companion_bootstrap_metadata
from workbench.environment import runtime_environment
from workbench.scene import extract_scene, render_scene_png, write_scene


PROJECT = Path(__file__).resolve().parents[2]
PREVIEW_ROOT = Path(os.environ.get("WORKBENCH_PREVIEW_ROOT", PROJECT / "var/previews"))
IMPORT_ROOT = Path(os.environ.get("WORKBENCH_IMPORT_ROOT", PROJECT / "var/imports"))
RUN_ROOT = Path(os.environ.get("WORKBENCH_RUN_ROOT", PROJECT / "out/ui_runs"))
EXPORT_ROOT = Path(os.environ.get("WORKBENCH_EXPORT_ROOT", PROJECT / "var/exports"))
CITY_PATH = PROJECT / "data/gis/DatosRai_ciudadValencia.shp"


class GISValidationError(ValueError):
    """Dataset-contract failure carrying row-level evidence."""

    def __init__(self, message: str, errors: list[dict[str, Any]]):
        super().__init__(message)
        self.errors = errors


def sha256_file(path: Path) -> str:
    return integrity.sha256_file(path)


def safe_json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (float, np.floating)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def workbench_base_config() -> BuildConfig:
    config = mb.DEFAULT_BUILD_CONFIG.model_copy(deep=True)
    config.data.output_root = RUN_ROOT
    settings = db.project_settings()
    dataset_fields = {
        "building_dataset_id": ("gis", "building_path"),
        "neighbor_dataset_id": ("gis", "neighbor_path"),
        "template_dataset_id": ("template", "template_path"),
        "weather_dataset_id": ("weather", "epw_path"),
    }
    missing = []
    for setting, (kind, config_field) in dataset_fields.items():
        dataset_id = settings.get(setting)
        dataset = db.get_dataset(dataset_id) if dataset_id else None
        if dataset and dataset["kind"] == kind:
            setattr(config.data, config_field, Path(dataset["path"]))
        else:
            missing.append(setting)
    if missing:
        raise RuntimeError(f"Active project inputs are incomplete: {', '.join(missing)}")
    return config


def bootstrap() -> None:
    db.init_db()
    db.recover_orphan_jobs()
    db.recover_incomplete_auto_commits()
    db.reconcile_all_batches()
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    IMPORT_ROOT.mkdir(parents=True, exist_ok=True)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    for scratch in RUN_ROOT.glob(".simulation-*"):
        if scratch.is_dir():
            shutil.rmtree(scratch, ignore_errors=True)
    for scratch in RUN_ROOT.glob(".scenario-*"):
        if scratch.is_dir():
            shutil.rmtree(scratch, ignore_errors=True)
    config = mb.DEFAULT_BUILD_CONFIG.model_copy(deep=True)
    config.data.building_path = CITY_PATH
    defaults = [
        ("valencia-city", "gis", "Valencia city buildings", config.data.building_path),
        ("tipo15-ledger", "companion", "Tipo15 dwelling ledger", TIPO15_PATH),
        ("plantilla-v2", "template", "PlantillaOS_v2", config.data.template_path),
        ("valencia-iwec", "weather", "Valencia IWEC", config.data.epw_path),
        ("valencia-iwec-ddy", "ddy", "Valencia IWEC design days",
         PROJECT / "data/weather/ESP_Valencia.082840_IWEC.ddy"),
    ]
    for dataset_id, kind, name, path in defaults:
        existing = db.get_dataset(dataset_id)
        if path.exists() and (existing is None or not existing.get("snapshot_hash")):
            snapshot = integrity.ensure_snapshot(path, kind=kind)
            metadata: dict[str, Any] = {"managed": False, "suffix": path.suffix.lower()}
            if kind == "gis":
                try:
                    gdf = read_gdf(path)
                    metadata.update({
                        "rows": len(gdf), "crs": str(gdf.crs),
                        "columns": list(gdf.columns),
                        "geometry_types": sorted(gdf.geometry.geom_type.dropna().unique().tolist()),
                    })
                except Exception as exc:
                    metadata["inspection_error"] = str(exc)
            elif kind == "companion":
                metadata.update(companion_bootstrap_metadata())
            elif kind in {"template", "weather", "ddy"}:
                metadata.update(_inspect_uploaded_dataset(kind, path))
            db.upsert_dataset({
                "id": dataset_id,
                "kind": kind,
                "name": name,
                "path": str(path),
                "sha256": snapshot["snapshot_hash"],
                "snapshot_hash": snapshot["snapshot_hash"],
                "verification_status": "VERIFIED",
                "read_only": True,
                "metadata": metadata | {"snapshot_components": snapshot["components"]},
            })
    settings = db.project_settings()
    defaults_by_field = {
        "building_dataset_id": "valencia-city",
        "neighbor_dataset_id": "valencia-city",
        "tipo15_dataset_id": "tipo15-ledger",
        "template_dataset_id": "plantilla-v2",
        "weather_dataset_id": "valencia-iwec",
        "ddy_dataset_id": "valencia-iwec-ddy",
    }
    initial_settings = {
        field: dataset_id for field, dataset_id in defaults_by_field.items()
        if db.get_dataset(dataset_id)
    }
    if not settings.get("initialized"):
        db.update_project_settings(initial_settings)
    else:
        # Schema v8 introduced these two explicit inputs.  Existing installations
        # already used these exact files implicitly; record them without changing
        # any of the four older active selections.
        migrated_defaults = {
            field: defaults_by_field[field]
            for field in ("tipo15_dataset_id", "ddy_dataset_id")
            if not settings.get(field) and db.get_dataset(defaults_by_field[field])
        }
        if migrated_defaults:
            db.update_project_settings(migrated_defaults)
    try:
        db.replace_profiles(profile_catalog(workbench_base_config()))
    except RuntimeError:
        pass
    recover_committing_runs()
    integrity.garbage_collect(db.snapshot_hashes_in_use())


def recover_committing_runs() -> None:
    for run in db.committing_runs():
        run_id = run["id"]
        staging = Path(run["artifact_dir"])
        final = RUN_ROOT / run_id
        try:
            if final.exists():
                root = final
            elif staging.exists() and staging.name == f".staging-{run_id}":
                staging.replace(final)
                root = final
            else:
                db.update_run_verification(run_id, "TAMPERED")
                continue
            for path in root.iterdir():
                if path.is_file():
                    path.chmod(0o444)
            root.chmod(0o555)
            manifest = root / "manifest.json"
            if not manifest.exists() or sha256_file(manifest) != run["manifest_sha256"]:
                db.update_run_verification(run_id, "TAMPERED")
                continue
            db.finalize_run(run_id, str(root), run["manifest_sha256"])
            verify_run_artifacts(run_id)
        except Exception:
            db.update_run_verification(run_id, "TAMPERED")


SETTING_KINDS = {
    "building_dataset_id": "gis",
    "neighbor_dataset_id": "gis",
    "tipo15_dataset_id": ("tipo15", "companion"),
    "template_dataset_id": "template",
    "weather_dataset_id": "weather",
    "ddy_dataset_id": "ddy",
}


def get_project_settings() -> dict[str, Any]:
    settings = db.project_settings()
    settings["datasets"] = {
        field: db.get_dataset(settings[field]) if settings.get(field) else None
        for field in SETTING_KINDS
    }
    return settings


def set_project_settings(values: dict[str, str | None]) -> dict[str, Any]:
    for field, dataset_id in values.items():
        if field not in SETTING_KINDS:
            raise ValueError(f"Unknown project setting: {field}")
        if dataset_id is None:
            continue
        dataset = db.get_dataset(dataset_id)
        if dataset is None:
            raise KeyError(dataset_id)
        expected = SETTING_KINDS[field]
        expected_kinds = (expected,) if isinstance(expected, str) else expected
        if dataset["kind"] not in expected_kinds:
            raise ValueError(
                f"{field} requires one of {', '.join(expected_kinds)}; got {dataset['kind']}"
            )
        if "gis" in expected_kinds:
            metadata = dataset.get("metadata", {})
            columns = set(metadata.get("columns", []))
            missing = {"refparcela", "altura_max"} - columns
            if missing:
                raise ValueError(
                    f"GIS dataset must be normalized before activation; missing {', '.join(sorted(missing))}"
                )
            if "25830" not in str(metadata.get("crs", "")):
                raise ValueError("Active GIS datasets must use EPSG:25830; normalize the dataset first")
    resolved = db.project_settings() | values
    weather_id = resolved.get("weather_dataset_id")
    ddy_id = resolved.get("ddy_dataset_id")
    if weather_id and ddy_id:
        weather = db.get_dataset(str(weather_id))
        ddy = db.get_dataset(str(ddy_id))
        if weather is None or ddy is None:
            raise KeyError(weather_id if weather is None else ddy_id)
        _validate_climate_pair(Path(weather["path"]), Path(ddy["path"]))
    _read_gdf.cache_clear()
    if values:
        db.update_project_settings(values)
    return get_project_settings()


def normalize_gis_dataset(dataset_id: str, mapping_fields: dict[str, str | None]) -> dict[str, Any]:
    dataset = db.get_dataset(dataset_id)
    if dataset is None:
        raise KeyError(dataset_id)
    if dataset["kind"] != "gis":
        raise ValueError("Only GIS datasets can be field-mapped")
    source = Path(dataset["path"])
    source_snapshot = dataset.get("snapshot_hash") or integrity.ensure_snapshot(
        source, kind="gis"
    )["snapshot_hash"]
    gdf = gpd.read_file(source)
    reference_field = str(mapping_fields["reference_field"])
    floors_field = str(mapping_fields["floors_field"])
    cluster_field = mapping_fields.get("cluster_field") or None
    required = [reference_field, floors_field]
    if cluster_field:
        required.append(cluster_field)
    missing = [field for field in required if field not in gdf.columns]
    if missing:
        raise GISValidationError(
            "GIS field mapping is incomplete",
            [{"row": None, "field": field, "code": "FIELD_NOT_FOUND",
              "message": f"Field not found: {field}"} for field in missing],
        )

    errors: list[dict[str, Any]] = []
    if gdf.crs is None:
        errors.append({"row": None, "field": "geometry", "code": "CRS_MISSING",
                       "message": "Dataset must declare a CRS"})
    else:
        try:
            gdf.to_crs(25830)
        except Exception as exc:
            errors.append({"row": None, "field": "geometry", "code": "CRS_INVALID",
                           "message": f"CRS cannot be transformed to EPSG:25830: {exc}"})

    references = gdf[reference_field].astype("string").str.strip()
    duplicate_mask = references.notna() & references.duplicated(keep=False)
    floors = pd.to_numeric(gdf[floors_field], errors="coerce")
    for position, (index, row) in enumerate(gdf.iterrows()):
        geometry = row.geometry
        row_id = safe_json_value(index)
        reference = references.iloc[position]
        if geometry is None or geometry.is_empty:
            errors.append({"row": row_id, "field": "geometry", "code": "GEOMETRY_EMPTY",
                           "message": "Geometry is null or empty"})
        elif geometry.geom_type != "Polygon":
            errors.append({"row": row_id, "field": "geometry", "code": "GEOMETRY_TYPE",
                           "message": f"Expected Polygon, found {geometry.geom_type}"})
        else:
            if not geometry.is_valid:
                errors.append({"row": row_id, "field": "geometry", "code": "GEOMETRY_INVALID",
                               "message": "Polygon is invalid; correction requires explicit review"})
            if geometry.interiors:
                errors.append({"row": row_id, "field": "geometry", "code": "GEOMETRY_HOLES",
                               "message": f"Polygon has {len(geometry.interiors)} interior ring(s)"})
        if pd.isna(reference) or not str(reference):
            errors.append({"row": row_id, "field": reference_field, "code": "ID_EMPTY",
                           "message": "Building identifier is null or empty"})
        elif bool(duplicate_mask.iloc[position]):
            errors.append({"row": row_id, "field": reference_field, "code": "ID_DUPLICATE",
                           "message": f"Duplicate building identifier: {reference}"})
        floor_value = floors.iloc[position]
        if pd.isna(floor_value) or not np.isfinite(floor_value):
            errors.append({"row": row_id, "field": floors_field, "code": "FLOOR_NOT_FINITE",
                           "message": "Floor value must be a finite number"})
        elif floor_value < 1 or floor_value > 100:
            errors.append({"row": row_id, "field": floors_field, "code": "FLOOR_RANGE",
                           "message": f"Floor value must be between 1 and 100: {floor_value}"})
        elif not float(floor_value).is_integer():
            errors.append({"row": row_id, "field": floors_field, "code": "FLOOR_FRACTIONAL",
                           "message": f"Fractional floor value is not allowed: {floor_value}"})
        if cluster_field and (pd.isna(row[cluster_field]) or not str(row[cluster_field]).strip()):
            errors.append({"row": row_id, "field": cluster_field, "code": "CLUSTER_EMPTY",
                           "message": "Mapped cluster value is null or empty"})
    if errors:
        raise GISValidationError(
            f"GIS contract failed with {len(errors)} issue(s)", errors[:1000]
        )

    normalized = gdf.copy()
    normalized["refparcela"] = references.astype(str)
    normalized["altura_max"] = floors.astype(int)
    if cluster_field:
        normalized["cluster"] = normalized[cluster_field].astype(str)
    normalized = normalized.to_crs(25830)

    mapping_record = {
        "schema_version": 2,
        "source_snapshot": source_snapshot,
        "target_crs": "EPSG:25830",
        "mapping": {
            "geometry": "geometry", "refparcela": reference_field,
            "altura_max": floors_field, "cluster": cluster_field,
        },
    }
    mapping_fingerprint = hashlib.sha256(
        json.dumps(mapping_record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    normalized_id = f"normalized-{mapping_fingerprint[:20]}"
    target_dir = IMPORT_ROOT / normalized_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "normalized.gpkg"
    if not target.exists():
        normalized.to_file(target, driver="GPKG", layer="buildings")
        target.chmod(0o444)
    normalized_snapshot = integrity.ensure_snapshot(target, kind="gis")
    metadata = {
        "managed": True,
        "normalized": True,
        "source_dataset_id": dataset_id,
        "source_snapshot": source_snapshot,
        "mapping_fingerprint": mapping_fingerprint,
        "rows": len(normalized),
        "crs": str(normalized.crs),
        "columns": list(normalized.columns),
        "geometry_types": sorted(normalized.geometry.geom_type.dropna().unique().tolist()),
        "field_mapping": {
            "geometry": "geometry",
            "refparcela": reference_field,
            "altura_max": floors_field,
            "cluster": cluster_field,
        },
    }
    db.upsert_dataset({
        "id": normalized_id,
        "kind": "gis",
        "name": f"{dataset['name']} (normalized)",
        "path": str(target),
        "sha256": normalized_snapshot["snapshot_hash"],
        "snapshot_hash": normalized_snapshot["snapshot_hash"],
        "verification_status": "VERIFIED",
        "read_only": True,
        "metadata": metadata,
    })
    _read_gdf.cache_clear()
    return db.get_dataset(normalized_id)


@lru_cache(maxsize=4)
def _read_gdf(path_str: str, mtime_ns: int) -> gpd.GeoDataFrame:
    del mtime_ns
    return gpd.read_file(path_str)


_READ_GDF_LOCK = threading.RLock()


def read_gdf(path: Path) -> gpd.GeoDataFrame:
    # functools.lru_cache intentionally permits duplicate work on concurrent
    # misses. MapLibre requests several tiles at once, so serialize the first
    # shapefile load and let all following tile workers reuse the same frame.
    with _READ_GDF_LOCK:
        return _read_gdf(str(path), path.stat().st_mtime_ns)


def read_building(refparcela: str, path: Path | None = None):
    gdf = read_gdf(path or workbench_base_config().data.building_path)
    if "refparcela" not in gdf.columns:
        raise ValueError("Dataset has no refparcela column")
    rows = gdf[gdf["refparcela"].astype(str) == str(refparcela)]
    if rows.empty:
        raise KeyError(f"Building not found: {refparcela}")
    return rows.iloc[0]


def serialize_row(row, crs) -> dict[str, Any]:
    geo = gpd.GeoSeries([row.geometry], crs=crs).to_crs(4326).iloc[0]
    props = {
        key: safe_json_value(value)
        for key, value in row.items()
        if key != "geometry"
    }
    return {
        "type": "Feature",
        "id": str(props.get("refparcela", "")),
        "geometry": mapping(geo),
        "properties": props,
    }


def query_buildings(*, query: str | None = None, bbox_4326: tuple[float, float, float, float] | None = None,
                    limit: int = 1000, path: Path | None = None) -> dict[str, Any]:
    gdf = read_gdf(path or workbench_base_config().data.building_path)
    filtered = gdf
    if query:
        needle = query.strip().lower()
        mask = filtered["refparcela"].astype(str).str.lower().str.contains(needle, regex=False)
        filtered = filtered[mask]
    if bbox_4326 is not None:
        transformer = Transformer.from_crs(4326, gdf.crs, always_xy=True)
        minx, miny = transformer.transform(bbox_4326[0], bbox_4326[1])
        maxx, maxy = transformer.transform(bbox_4326[2], bbox_4326[3])
        filtered = filtered[filtered.geometry.intersects(box(minx, miny, maxx, maxy))]
    total = len(filtered)
    filtered = filtered.head(max(1, min(limit, 2500)))
    return {
        "type": "FeatureCollection",
        "features": [serialize_row(row, gdf.crs) for _, row in filtered.iterrows()],
        "total": total,
        "truncated": total > len(filtered),
    }


def search_buildings(query: str, limit: int = 20) -> dict[str, Any]:
    needle = query.strip().lower()
    if len(needle) < 2:
        return {"items": [], "total": 0}
    gdf = read_gdf(workbench_base_config().data.building_path)
    matches = gdf[gdf["refparcela"].astype(str).str.lower().str.contains(needle, regex=False)]
    total = len(matches)
    items = []
    for _, row in matches.head(max(1, min(limit, 50))).iterrows():
        centroid = gpd.GeoSeries([row.geometry.centroid], crs=gdf.crs).to_crs(4326).iloc[0]
        items.append({
            "refparcela": str(row["refparcela"]),
            "cluster": safe_json_value(row.get("cluster")),
            "floors": safe_json_value(row.get("altura_max")),
            "center": [centroid.x, centroid.y],
        })
    return {"items": items, "total": total}


def _tile_lon_lat_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


@lru_cache(maxsize=512)
def building_vector_tile(z: int, x: int, y: int, dataset_path: str, mtime_ns: int) -> bytes:
    del mtime_ns
    source = read_gdf(Path(dataset_path))
    west, south, east, north = _tile_lon_lat_bounds(z, x, y)
    to_source = Transformer.from_crs(4326, source.crs, always_xy=True)
    minx, miny = to_source.transform(west, south)
    maxx, maxy = to_source.transform(east, north)
    subset = source[source.geometry.intersects(box(minx, miny, maxx, maxy))]
    if subset.empty:
        return mapbox_vector_tile.encode({"name": "buildings", "features": []})
    mercator = subset.to_crs(3857)
    tile_bounds = gpd.GeoSeries([box(west, south, east, north)], crs=4326).to_crs(3857).total_bounds
    tile_box = box(*tile_bounds)
    features = []
    for _, row in mercator.iterrows():
        geometry = row.geometry.intersection(tile_box)
        if geometry.is_empty:
            continue
        features.append({
            "id": str(row.get("refparcela", "")),
            "geometry": mapping(geometry),
            "properties": {
                "refparcela": str(row.get("refparcela", "")),
                "cluster": safe_json_value(row.get("cluster")),
                "altura_max": safe_json_value(row.get("altura_max")),
                "nombre": safe_json_value(row.get("nombre")),
                "coddistrit": safe_json_value(row.get("coddistrit")),
            },
        })
    return mapbox_vector_tile.encode(
        {"name": "buildings", "features": features},
        default_options={"quantize_bounds": tuple(tile_bounds), "extents": 4096},
    )


_CITY_TILE_COLUMNS = ("refparcela", "cluster", "altura_max", "nombre", "coddistrit")
_CITY_OVERVIEW_CELL_METERS = {8: 1600.0, 9: 800.0, 10: 400.0, 11: 200.0}


@lru_cache(maxsize=4)
def _city_tile_frame(dataset_path: str, mtime_ns: int) -> gpd.GeoDataFrame:
    """Project only tile-relevant stock columns once per immutable snapshot."""
    del mtime_ns
    source = read_gdf(Path(dataset_path))
    columns = [column for column in _CITY_TILE_COLUMNS if column in source.columns]
    return source[columns + ["geometry"]].to_crs(3857)


@lru_cache(maxsize=4)
def _city_overview_points(dataset_path: str, mtime_ns: int) -> gpd.GeoDataFrame:
    """Cache one projected representative point per stock building."""
    source = _city_tile_frame(dataset_path, mtime_ns)
    columns = [column for column in ("cluster", "nombre") if column in source.columns]
    frame = source[columns].copy()
    frame["geometry"] = source.geometry.representative_point()
    points = gpd.GeoDataFrame(frame, geometry="geometry", crs=3857)
    points["_tile_x"] = points.geometry.x
    points["_tile_y"] = points.geometry.y
    return points


_CITY_OVERVIEW_LOCK = threading.RLock()


@lru_cache(maxsize=4)
def city_focus_view(dataset_path: str, mtime_ns: int) -> dict[str, Any]:
    """Return a robust urban focus while retaining the complete stock extent."""
    with _CITY_OVERVIEW_LOCK:
        points = _city_overview_points(dataset_path, mtime_ns)
    if points.empty:
        return {"bounds": None, "buildings": 0, "coverage": 0.0}
    minx = float(points["_tile_x"].quantile(0.01))
    miny = float(points["_tile_y"].quantile(0.05))
    maxx = float(points["_tile_x"].quantile(0.99))
    maxy = float(points["_tile_y"].quantile(0.99))
    to_lon_lat = Transformer.from_crs(3857, 4326, always_xy=True)
    west, south = to_lon_lat.transform(minx, miny)
    east, north = to_lon_lat.transform(maxx, maxy)
    included = points["_tile_x"].between(minx, maxx, inclusive="both") & points[
        "_tile_y"
    ].between(miny, maxy, inclusive="both")
    buildings = int(included.sum())
    return {
        "bounds": [west, south, east, north],
        "buildings": buildings,
        "coverage": buildings / len(points),
    }


def _dominant_value(values: pd.Series) -> Any:
    modes = values.dropna().mode()
    return modes.iat[0] if not modes.empty else None


@lru_cache(maxsize=20)
def _city_aggregated_overview(
    dataset_path: str, mtime_ns: int, z: int
) -> gpd.GeoDataFrame:
    """Reduce invisible building-level detail once per snapshot and zoom."""
    points = _city_overview_points(dataset_path, mtime_ns)
    cell_size = _CITY_OVERVIEW_CELL_METERS.get(z, 100.0)
    working = points.copy()
    working["_grid_x"] = np.floor(working["_tile_x"] / cell_size).astype(np.int64)
    working["_grid_y"] = np.floor(working["_tile_y"] / cell_size).astype(np.int64)
    aggregations: dict[str, Any] = {
        "_tile_x": "mean",
        "_tile_y": "mean",
    }
    if "cluster" in working.columns:
        aggregations["cluster"] = _dominant_value
    if "nombre" in working.columns:
        aggregations["nombre"] = _dominant_value
    grouped = (
        working.groupby(["_grid_x", "_grid_y"], sort=False)
        .agg(**{
            "tile_x": pd.NamedAgg(column="_tile_x", aggfunc="mean"),
            "tile_y": pd.NamedAgg(column="_tile_y", aggfunc="mean"),
            "count": pd.NamedAgg(column="_tile_x", aggfunc="size"),
            **({
                "cluster": pd.NamedAgg(column="cluster", aggfunc=_dominant_value),
            } if "cluster" in aggregations else {}),
            **({
                "nombre": pd.NamedAgg(column="nombre", aggfunc=_dominant_value),
            } if "nombre" in aggregations else {}),
        })
        .reset_index()
    )
    grouped["_feature_id"] = np.arange(1, len(grouped) + 1, dtype=np.int64)
    grouped["_tile_x"] = grouped.pop("tile_x")
    grouped["_tile_y"] = grouped.pop("tile_y")
    return gpd.GeoDataFrame(
        grouped,
        geometry=gpd.points_from_xy(grouped["_tile_x"], grouped["_tile_y"]),
        crs=3857,
    )


@lru_cache(maxsize=512)
def _city_exact_vector_tile(
    z: int, x: int, y: int, dataset_path: str, mtime_ns: int
) -> bytes:
    source = _city_tile_frame(dataset_path, mtime_ns)
    west, south, east, north = _tile_lon_lat_bounds(z, x, y)
    tile_bounds = gpd.GeoSeries([box(west, south, east, north)], crs=4326).to_crs(3857).total_bounds
    tile_box = box(*tile_bounds)
    candidate_indices = source.sindex.query(tile_box, predicate="intersects")
    subset = source.iloc[candidate_indices]
    if subset.empty:
        return mapbox_vector_tile.encode({"name": "buildings", "features": []})
    features = []
    for _, row in subset.iterrows():
        geometry = row.geometry.intersection(tile_box)
        if geometry.is_empty:
            continue
        features.append({
            "id": str(row.get("refparcela", "")),
            "geometry": mapping(geometry),
            "properties": {
                "refparcela": str(row.get("refparcela", "")),
                "cluster": safe_json_value(row.get("cluster")),
                "altura_max": safe_json_value(row.get("altura_max")),
                "nombre": safe_json_value(row.get("nombre")),
                "coddistrit": safe_json_value(row.get("coddistrit")),
            },
        })
    return mapbox_vector_tile.encode(
        {"name": "buildings", "features": features},
        default_options={"quantize_bounds": tuple(tile_bounds), "extents": 4096},
    )


@lru_cache(maxsize=12)
def _city_generalized_tile_frame(
    dataset_path: str, mtime_ns: int, z: int
) -> gpd.GeoDataFrame:
    """Keep individual footprints while removing sub-pixel vertex detail."""
    source = _city_tile_frame(dataset_path, mtime_ns)
    pixel_size_m = (2 * math.pi * 6378137.0) / (2 ** z * 4096)
    frame = source.copy()
    frame["geometry"] = source.geometry.simplify(pixel_size_m, preserve_topology=True)
    return frame


@lru_cache(maxsize=512)
def _city_generalized_vector_tile(
    z: int, x: int, y: int, dataset_path: str, mtime_ns: int
) -> bytes:
    """Encode real per-building footprints at screen-scale precision."""
    with _CITY_OVERVIEW_LOCK:
        source = _city_generalized_tile_frame(dataset_path, mtime_ns, z)
    west, south, east, north = _tile_lon_lat_bounds(z, x, y)
    tile_bounds = gpd.GeoSeries([box(west, south, east, north)], crs=4326).to_crs(3857).total_bounds
    tile_box = box(*tile_bounds)
    candidate_indices = source.sindex.query(tile_box, predicate="intersects")
    subset = source.iloc[candidate_indices]
    if subset.empty:
        return mapbox_vector_tile.encode({"name": "buildings", "features": []})
    grouped_polygons: dict[tuple[Any, Any], list[Polygon]] = {}
    for row in subset.itertuples(index=False):
        geometry = row.geometry
        if not tile_box.contains(geometry):
            geometry = geometry.intersection(tile_box)
        if geometry.is_empty:
            continue
        key = (
            safe_json_value(getattr(row, "cluster", None)),
            safe_json_value(getattr(row, "nombre", None)),
        )
        polygons = grouped_polygons.setdefault(key, [])
        if isinstance(geometry, Polygon):
            polygons.append(geometry)
        elif isinstance(geometry, MultiPolygon):
            polygons.extend(geometry.geoms)
    features = [{
            "id": index,
            "geometry": mapping(MultiPolygon(polygons)),
            "properties": {
                "cluster": cluster,
                "nombre": nombre,
            },
        }
        for index, ((cluster, nombre), polygons) in enumerate(grouped_polygons.items(), start=1)
        if polygons
    ]
    return mapbox_vector_tile.encode(
        {"name": "buildings", "features": features},
        default_options={"quantize_bounds": tuple(tile_bounds), "extents": 4096},
    )


@lru_cache(maxsize=512)
def city_vector_tile(z: int, x: int, y: int, dataset_path: str, mtime_ns: int) -> bytes:
    """Use screen-scale stock aggregation and exact footprints up close."""
    if z >= 14:
        return _city_exact_vector_tile(z, x, y, dataset_path, mtime_ns)
    if z >= 11:
        return _city_generalized_vector_tile(z, x, y, dataset_path, mtime_ns)
    with _CITY_OVERVIEW_LOCK:
        points = _city_aggregated_overview(dataset_path, mtime_ns, z)
    west, south, east, north = _tile_lon_lat_bounds(z, x, y)
    tile_bounds = gpd.GeoSeries([box(west, south, east, north)], crs=4326).to_crs(3857).total_bounds
    minx, miny, maxx, maxy = tile_bounds
    subset = points[
        points["_tile_x"].between(minx, maxx, inclusive="both")
        & points["_tile_y"].between(miny, maxy, inclusive="both")
    ]
    features = [{
        "id": int(row["_feature_id"]),
        "geometry": mapping(row.geometry),
        "properties": {
            "cluster": safe_json_value(row.get("cluster")),
            "nombre": safe_json_value(row.get("nombre")),
            "count": int(row["count"]),
        },
    } for _, row in subset.iterrows()]
    return mapbox_vector_tile.encode(
        {"name": "overview", "features": features},
        default_options={"quantize_bounds": tuple(tile_bounds), "extents": 4096},
    )


def apply_geometry_actions(geometry, actions: list[dict[str, Any]]):
    result = geometry
    for action in actions:
        if not action.get("approved"):
            continue
        name = action.get("action")
        if name == "make_valid":
            result = make_valid(result)
        elif name in {"extract_single_part", "select_multipolygon_part"}:
            if not isinstance(result, MultiPolygon):
                raise ValueError(f"{name} requires a MultiPolygon")
            part_index = int(action.get("part_index", 0))
            if not 0 <= part_index < len(result.geoms):
                raise ValueError(f"Invalid MultiPolygon part index: {part_index}")
            result = result.geoms[part_index]
    return result


def _vertex_count(geometry) -> int:
    if isinstance(geometry, Polygon):
        return len(list(geometry.exterior.coords)) - 1
    if isinstance(geometry, MultiPolygon):
        return sum(len(list(part.exterior.coords)) - 1 for part in geometry.geoms)
    return 0


def validate_geometry(refparcela: str, config: BuildConfig) -> dict[str, Any]:
    row = read_building(refparcela, config.data.building_path)
    original = row.geometry
    candidate = original
    actions: list[dict[str, Any]] = []
    issues: list[str] = []
    if not candidate.is_valid:
        fixed = make_valid(candidate)
        actions.append({
            "action": "make_valid", "approved": False, "required": True,
            "before_type": candidate.geom_type, "after_type": fixed.geom_type,
            "area_delta_pct": round(abs(fixed.area - candidate.area) / max(candidate.area, 1e-9) * 100, 4),
        })
        issues.append("Geometry is invalid; make_valid requires explicit approval")
        candidate = fixed
    if isinstance(candidate, MultiPolygon):
        parts = [{"part_index": i, "area_m2": round(part.area, 3), "vertices": _vertex_count(part)}
                 for i, part in enumerate(candidate.geoms)]
        largest_index = max(range(len(parts)), key=lambda i: parts[i]["area_m2"])
        actions.append({
            "action": "extract_single_part" if len(parts) == 1 else "select_multipolygon_part",
            "approved": False, "required": True, "part_index": largest_index, "parts": parts,
        })
        issues.append(f"MultiPolygon has {len(parts)} part(s); part selection requires explicit approval")
        candidate = candidate.geoms[largest_index]
    supported = isinstance(candidate, Polygon) and candidate.is_valid and not candidate.is_empty
    if supported and candidate.interiors:
        supported = False
        issues.append(f"Polygon has {len(candidate.interiors)} interior ring(s), which are not supported")
    if not supported:
        issues.append(f"Resulting geometry type is not buildable: {candidate.geom_type}")

    original_wgs = gpd.GeoSeries([original], crs=25830).to_crs(4326).iloc[0]
    simplified = candidate
    simplified_area = candidate.area
    coords: list[tuple[float, float]] = []
    neighbors = gpd.GeoDataFrame(geometry=[], crs=25830)
    party = None
    if supported:
        cleaned = mb.clean_polygon(candidate)
        coords, simplified_area = mb.prepare_footprint(cleaned, config=config)
        simplified = Polygon(coords)
        neighbors = mb.load_neighbors(candidate, refparcela, config.data.neighbor_path, config=config)
        party = mb.find_party_walls(
            candidate, refparcela, config.data.neighbor_path, neighbors=neighbors, config=config)
    simplified_wgs = gpd.GeoSeries([simplified], crs=25830).to_crs(4326).iloc[0]
    area_delta = abs(simplified.area - original.area) / max(original.area, 1e-9)
    neighbor_features = [serialize_row(item, neighbors.crs) for _, item in neighbors.iterrows()]
    party_wgs = None
    if party is not None:
        party_wgs = mapping(gpd.GeoSeries([party], crs=25830).to_crs(4326).iloc[0])
    actions.append({
        "action": "simplify",
        "tolerance_m": config.geometry.simplify_tolerance_m,
        "approved": False,
        "required": config.geometry.simplify_tolerance_m > 0,
        "area_delta_pct": round(abs(simplified.area - candidate.area) / max(candidate.area, 1e-9) * 100, 4),
    })
    requires_approval = any(action.get("required") and not action.get("approved") for action in actions)
    return {
        "valid": supported,
        "ready": supported and not requires_approval,
        "requires_approval": requires_approval,
        "issues": issues,
        "geometry_type": original.geom_type,
        "original_area_m2": round(original.area, 3),
        "simplified_area_m2": round(simplified_area, 3),
        "area_delta_pct": round(area_delta * 100, 4),
        "original_vertices": _vertex_count(original),
        "simplified_vertices": len(coords) if coords else _vertex_count(simplified),
        "original": mapping(original_wgs),
        "simplified": mapping(simplified_wgs),
        "neighbors": {"type": "FeatureCollection", "features": neighbor_features},
        "party_geometry": party_wgs,
        "party_length_m": round(party.length, 3) if party is not None else 0.0,
        "actions": actions,
    }


def template_health(config: BuildConfig) -> dict[str, Any]:
    required = {
        "space_types": ["Espacio Tipo Vivienda CTE", "Espacio Tipo No habitable 1ACH"],
        "schedules": ["T Calefaccion vivienda CTE", "T refrigeracion vivienda CTE"],
        "blinds": [config.shading.blind_name],
        "materials": [
            "Mortero de cemento referencia", "Ladrillo Perforado Referencia",
            "Ladrillo Hueco Referencia", "Ladrillo Doble Hueco Referencia",
            "Aislante Medianera Referencia B", "FU Entrevigado de hormigon aligerado -Canto 300 mm",
        ],
        "constructions": ["Cubierta plana no aislada", "Medianera Referencia B"],
    }
    result = {"ok": True, "required": {}, "error": None}
    try:
        model = mb.load_template(config=config)
        available = {
            "space_types": {x.nameString() for x in model.getSpaceTypes()},
            "schedules": {x.nameString() for x in model.getScheduleRulesets()},
            "blinds": {x.nameString() for x in model.getBlinds()},
            "materials": {x.nameString() for x in model.getStandardOpaqueMaterials()},
            "constructions": {x.nameString() for x in model.getConstructions()},
        }
        for kind, names in required.items():
            missing = [name for name in names if name not in available[kind]]
            result["required"][kind] = {"ok": not missing, "missing": missing}
            result["ok"] = result["ok"] and not missing
    except Exception as exc:
        result.update(ok=False, error=str(exc))
    return result


def system_health() -> dict[str, Any]:
    from workbench.jobs import manager

    environment = runtime_environment()
    checks: dict[str, Any] = {}
    capacity = storage.capacity_status()
    try:
        config = workbench_base_config()
    except Exception as exc:
        return {
            "ok": False, "readiness": "BLOCKED", "error": str(exc),
            "openstudio_version": openstudio.openStudioVersion(),
            "files": {}, "checks": {"active_inputs": {"ok": False, "message": str(exc)}},
            "template": {"ok": False, "required": {}, "error": "Active inputs incomplete"},
            "python_builder": str(Path(mb.__file__).resolve()),
            "worker": manager.status(), "database": str(db.DB_PATH),
            "storage": capacity, "environment": environment,
        }
    files = {
        "buildings": config.data.building_path,
        "neighbors": config.data.neighbor_path,
        "template": config.data.template_path,
        "weather": config.data.epw_path,
    }
    file_checks: dict[str, Any] = {}
    kinds = {"buildings": "gis", "neighbors": "gis", "template": "template", "weather": "weather"}
    settings = db.project_settings()
    dataset_fields = {
        "buildings": "building_dataset_id", "neighbors": "neighbor_dataset_id",
        "template": "template_dataset_id", "weather": "weather_dataset_id",
    }
    descriptor_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for key, path in files.items():
        item: dict[str, Any] = {"ok": path.exists(), "path": str(path)}
        if path.exists():
            try:
                cache_key = (str(path.resolve()), kinds[key])
                actual = descriptor_cache.get(cache_key)
                if actual is None:
                    actual = integrity.snapshot_descriptor(path, kind=kinds[key])
                    descriptor_cache[cache_key] = actual
                dataset_id = settings.get(dataset_fields[key])
                dataset = db.get_dataset(dataset_id) if dataset_id else None
                expected = dataset.get("snapshot_hash") if dataset else None
                item.update(
                    ok=bool(expected) and actual["snapshot_hash"] == expected,
                    snapshot_hash=actual["snapshot_hash"], expected_snapshot_hash=expected,
                    components=len(actual["components"]),
                )
                if expected and actual["snapshot_hash"] != expected:
                    item["error"] = "Registered input snapshot no longer matches source files"
            except Exception as exc:
                item.update(ok=False, error=str(exc))
        file_checks[key] = item

    gdf_check: dict[str, Any] = {"ok": False}
    try:
        gdf = read_gdf(config.data.building_path)
        required = {"refparcela", "altura_max", "geometry"}
        missing = sorted(required - set(gdf.columns))
        gdf_check = {
            "ok": not missing and gdf.crs is not None and "25830" in str(gdf.crs),
            "rows": len(gdf), "crs": str(gdf.crs), "missing_columns": missing,
        }
    except Exception as exc:
        gdf_check = {"ok": False, "error": str(exc)}

    epw_check: dict[str, Any] = {"ok": False}
    try:
        header = config.data.epw_path.open(encoding="utf-8", errors="replace").readline().strip()
        fields = header.split(",")
        epw_check = {"ok": fields[0].upper() == "LOCATION" and len(fields) >= 10,
                     "location": fields[1] if len(fields) > 1 else None}
    except Exception as exc:
        epw_check = {"ok": False, "error": str(exc)}

    db_check: dict[str, Any] = {"ok": False}
    try:
        with db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("CREATE TEMP TABLE IF NOT EXISTS health_probe(value INTEGER)")
            con.rollback()
            integrity_result = con.execute("PRAGMA integrity_check").fetchone()[0]
        db_check = {"ok": integrity_result == "ok", "integrity": integrity_result,
                    "schema_version": db.LATEST_SCHEMA_VERSION}
    except Exception as exc:
        db_check = {"ok": False, "error": str(exc)}

    disk_check = {
        "ok": capacity["status"] != "BLOCKED",
        "status": capacity["status"],
        "free_bytes": capacity["free_bytes"],
        "total_bytes": capacity["total_bytes"],
        "free_ratio": capacity["free_ratio"],
        "reserve_floor_bytes": capacity["reserve_floor_bytes"],
        "message": "Protected disk reserve is available" if capacity["status"] == "READY"
        else "Disk capacity requires attention" if capacity["status"] == "WARNING"
        else "Disk capacity blocks new artifact-producing jobs",
    }
    template = template_health(config)
    worker = manager.status()
    checks = {
        "gis_contract": gdf_check, "template_contract": template,
        "epw_parse": epw_check, "database": db_check, "disk": disk_check,
        "worker": {"ok": bool(worker["running"]), **worker},
        "openstudio_runtime": {"ok": bool(openstudio.openStudioVersion()),
                               "version": openstudio.openStudioVersion()},
    }
    ready = all(item.get("ok", False) for item in file_checks.values()) and all(
        item.get("ok", False) for item in checks.values()
    )
    readiness = "BLOCKED" if not ready else capacity["status"]
    return {
        "ok": ready,
        "readiness": readiness,
        "openstudio_version": openstudio.openStudioVersion(),
        "python_builder": str(Path(mb.__file__).resolve()),
        "files": file_checks,
        "template": template,
        "checks": checks,
        "worker": worker,
        "database": str(db.DB_PATH),
        "storage": capacity,
        "environment": environment,
    }


def qa_from_result(result: mb.BuildResult, neighbor_count: int, osm_path: Path) -> dict[str, Any]:
    translator = openstudio.osversion.VersionTranslator()
    reloaded = translator.loadModel(openstudio.toPath(str(osm_path)))
    model_reload_ok = not reloaded.isNull()
    orphan_subsurfaces = sum(
        1 for item in result.osm.getSubSurfaces() if not item.surface().is_initialized()
    )
    missing_constructions = sum(
        1 for item in [*result.osm.getSurfaces(), *result.osm.getSubSurfaces()]
        if not item.construction().is_initialized()
    )
    context_ok = (
        not result.config.shading.context_enabled
        or neighbor_count == 0
        or result.stats["n_shading_surfaces"] > 0
    )
    checks = [
        {"id": "model_saved", "status": "pass" if model_reload_ok else "fail",
         "message": "Saved OpenStudio model reloads successfully" if model_reload_ok
         else "Saved OpenStudio model could not be reloaded"},
        {"id": "subsurface_ownership", "status": "pass" if orphan_subsurfaces == 0 else "fail",
         "message": f"{orphan_subsurfaces} orphan subsurface(s)"},
        {"id": "construction_assignment", "status": "pass" if missing_constructions == 0 else "fail",
         "message": f"{missing_constructions} surface/subsurface construction assignment(s) missing"},
        {"id": "residential_floors", "status": "pass" if result.stats["n_floors_residential"] >= 1 else "fail",
         "message": f"{result.stats['n_floors_residential']} residential floors"},
        {"id": "openings", "status": "pass" if result.stats["window_area_m2"] > 0 else "fail",
         "message": f"{result.stats['window_area_m2']} m² glazing"},
        {"id": "context", "status": "pass" if context_ok else "fail",
         "message": f"{neighbor_count} source neighbors / {result.stats['n_shading_surfaces']} shading surfaces"},
    ]
    for facade in result.stats["facade_qa"]:
        deviation = abs(facade["lapse_pct"])
        checks.append({
            "id": f"facade_{facade['azimut']}",
            "status": "warn" if deviation > result.config.qa.facade_wwr_warning_pct else "pass",
            "message": f"Facade {facade['azimut']}° WWR deviation {facade['lapse_pct']}%",
        })
    return {
        "all_pass": all(item["status"] != "fail" for item in checks),
        "warning_count": sum(item["status"] == "warn" for item in checks),
        "checks": checks,
        "warnings": result.warnings,
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def artifact_manifest(directory: Path) -> list[dict[str, Any]]:
    items = []
    for path in sorted(p for p in directory.iterdir() if p.is_file()):
        items.append({
            "name": path.name,
            "path": str(path),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        })
    return items


def input_snapshot_manifest(config: BuildConfig) -> dict[str, Any]:
    inputs = {
        "buildings": (config.data.building_path, "gis"),
        "neighbors": (config.data.neighbor_path, "gis"),
        "template": (config.data.template_path, "template"),
        "weather": (config.data.epw_path, "weather"),
        "model_builder": (Path(mb.__file__).resolve(), "source"),
        "model_config": (PROJECT / "src/model_config.py", "source"),
    }
    output: dict[str, Any] = {}
    for role, (path, kind) in inputs.items():
        snapshot = integrity.ensure_snapshot(Path(path), kind=kind)
        output[role] = {
            "snapshot_hash": snapshot["snapshot_hash"],
            "source_name": snapshot["source_name"],
            "kind": kind,
            "components": snapshot["components"],
        }
    return output


def assert_active_input_integrity(config: BuildConfig) -> None:
    settings = db.project_settings()
    roles = {
        "building_dataset_id": (config.data.building_path, "gis"),
        "neighbor_dataset_id": (config.data.neighbor_path, "gis"),
        "template_dataset_id": (config.data.template_path, "template"),
        "weather_dataset_id": (config.data.epw_path, "weather"),
    }
    failures = []
    for setting, (path, kind) in roles.items():
        dataset_id = settings.get(setting)
        dataset = db.get_dataset(dataset_id) if dataset_id else None
        if dataset is None or not dataset.get("snapshot_hash"):
            failures.append(f"{setting}: no verified snapshot")
            continue
        verification = integrity.verify_snapshot_source(
            Path(path), dataset["snapshot_hash"], kind=kind,
        )
        if not verification["ok"]:
            failures.append(f"{setting}: source hash changed")
    if failures:
        raise ValueError("Input integrity failed; " + "; ".join(failures))


def run_build_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    payload = job["payload"]
    config = BuildConfig.model_validate(payload["config"])
    preview_dir = PREVIEW_ROOT / job_id
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    preview_dir.mkdir(parents=True)

    try:
        db.add_event(job_id, "Validating provenance and input files", 0.08)
        base = config_for_profile(workbench_base_config(), config.provenance.baseline_profile)
        missing = validate_override_provenance(config, base)
        if missing:
            raise ValueError(f"Missing override provenance for: {', '.join(missing)}")
        assert_active_input_integrity(config)
        mb.validate_input_files(config=config)

        db.add_event(job_id, "Loading building and context", 0.20)
        row = read_building(job["refparcela"], config.data.building_path)
        geometry_actions = payload.get("geometry_actions", [])
        validation = validate_geometry(job["refparcela"], config)
        provided = {
            (action.get("action"), action.get("part_index")): action
            for action in geometry_actions
        }
        pending = []
        for required in validation["actions"]:
            if not required.get("required"):
                continue
            candidate = provided.get((required.get("action"), required.get("part_index")))
            if candidate is None:
                candidate = provided.get((required.get("action"), None))
            if candidate is None:
                candidate = next(
                    (item for item in geometry_actions if item.get("action") == required.get("action")),
                    None,
                )
            if candidate is None or not candidate.get("approved"):
                pending.append(str(required.get("action", "unknown")))
        if pending:
            raise ValueError(f"Geometry actions require approval: {', '.join(pending)}")
        row = row.copy()
        row["geometry"] = apply_geometry_actions(row.geometry, geometry_actions)
        mb.validate_building_row(row)
        geometry = mb.clean_polygon(row.geometry)
        neighbors = mb.load_neighbors(
            geometry, job["refparcela"], config.data.neighbor_path, config=config)
        party = mb.find_party_walls(
            geometry, job["refparcela"], config.data.neighbor_path,
            neighbors=neighbors, config=config)

        db.add_event(job_id, "Building exact OpenStudio geometry", 0.45)
        result = mb.build_model_with_config(row, party, config, neighbors=neighbors)
        osm_path = mb.save_model(result.osm, preview_dir)

        db.add_event(job_id, "Extracting inspectable 3D scene", 0.68)
        scene = extract_scene(result.osm, result.stats)
        scene_path = preview_dir / "scene.json"
        write_scene(scene, scene_path)
        render_scene_png(scene, preview_dir / "model_3d.png")

        db.add_event(job_id, "Pinning renderer and derived visual geometry", 0.73)
        renderer_provenance.write_renderer_manifest(
            scene_path, preview_dir / "renderer_manifest.json",
        )

        qa = qa_from_result(result, len(neighbors), osm_path)
        write_json(preview_dir / "config.json", config.model_dump(mode="json"))
        write_json(preview_dir / "geometry_actions.json", geometry_actions)
        write_json(preview_dir / "input_manifest.json", input_snapshot_manifest(config))
        write_json(preview_dir / "environment.json", integrity.environment_manifest())
        write_json(preview_dir / "stats.json", result.stats)
        write_json(preview_dir / "qa.json", qa)
        with (preview_dir / "qa.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["id", "status", "message"])
            writer.writeheader()
            writer.writerows(qa["checks"])

        manifest_items = [
            {"name": item["name"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
            for item in artifact_manifest(preview_dir)
        ]
        write_json(preview_dir / "manifest.json", {
            "schema_version": 1,
            "job_id": job_id,
            "refparcela": job["refparcela"],
            "artifacts": manifest_items,
        })
        db.update_job(job_id, "ready", result_path=str(preview_dir))
        db.add_event(job_id, "Preview ready for review", 1.0)
        if job["auto_commit"]:
            commit_preview(job_id)
            db.update_batch_for_job(job, True)
    except Exception as exc:
        db.update_job(job_id, "failed", error=str(exc))
        db.add_event(job_id, str(exc), 1.0, "error")
        db.update_batch_for_job(job, False)
        raise


def preview_detail(job_id: str) -> dict[str, Any]:
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["kind"] != "preview":
        raise ValueError("Job is not a model preview")
    artifact_state = preview_artifact_state(job)
    detail = {"job": job, "scene": None, "stats": None, "qa": None,
              "renderer": None, "config": job["payload"].get("config"),
              "geometry_actions": job["payload"].get("geometry_actions", []),
              "request_fingerprint": hashlib.sha256(
                  integrity.canonical_json_bytes(job["payload"])).hexdigest(),
              "artifact_state": artifact_state,
              "queue_context": db.worker_queue_context(job_id)}
    if job.get("result_path") and artifact_state["status"] in {"AVAILABLE", "COMMITTED"}:
        root = Path(job["result_path"])
        for key, filename in (("scene", "scene.json"), ("stats", "stats.json"), ("qa", "qa.json")):
            path = root / filename
            if path.exists():
                detail[key] = json.loads(path.read_text(encoding="utf-8"))
        detail["renderer"] = renderer_provenance.summary(root)
    return detail


def preview_artifact_state(job: dict[str, Any]) -> dict[str, Any]:
    """Verify a mutable preview before it can be restored or committed."""
    if job["status"] == "completed" and job.get("run_id"):
        return {"status": "COMMITTED", "recoverable": True, "issues": []}
    if job["status"] in {"queued", "running"}:
        return {"status": "PENDING", "recoverable": True, "issues": []}
    if not job.get("result_path"):
        return {"status": "MISSING", "recoverable": False,
                "issues": ["Preview result path is missing"]}
    root = Path(job["result_path"])
    manifest_path = root / "manifest.json"
    issues: list[str] = []
    if not root.is_dir():
        issues.append("Preview artifact directory is missing")
    elif not manifest_path.is_file():
        issues.append("Preview manifest is missing")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("job_id") != job["id"]:
                issues.append("Preview manifest job identity mismatch")
            if manifest.get("refparcela") != job["refparcela"]:
                issues.append("Preview manifest building identity mismatch")
            artifacts = manifest.get("artifacts")
            if not isinstance(artifacts, list) or not artifacts:
                issues.append("Preview manifest has no artifacts")
            else:
                for item in artifacts:
                    name = item.get("name") if isinstance(item, dict) else None
                    if not isinstance(name, str) or Path(name).name != name:
                        issues.append("Preview manifest contains an invalid artifact name")
                        continue
                    path = root / name
                    if not path.is_file():
                        issues.append(f"Missing preview artifact: {name}")
                    elif (path.stat().st_size != item.get("size_bytes")
                          or sha256_file(path) != item.get("sha256")):
                        issues.append(f"Preview artifact hash mismatch: {name}")
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            issues.append(f"Preview manifest is invalid: {exc}")
    return {
        "status": "AVAILABLE" if not issues else "TAMPERED",
        "recoverable": not issues,
        "issues": issues,
    }


def recoverable_previews(limit: int = 20) -> dict[str, Any]:
    """Return active and uncommitted previews as a compact recovery ledger."""
    jobs = db.list_current_jobs("preview", include_ready=True, limit=limit)
    items = []
    for job in jobs:
        state = preview_artifact_state(job)
        config = job["payload"].get("config") or {}
        provenance = config.get("provenance") or {}
        items.append({
            "id": job["id"],
            "status": job["status"],
            "stage": job["stage"],
            "refparcela": job["refparcela"],
            "scenario_name": provenance.get("scenario_name", "Model preview"),
            "baseline_profile": provenance.get("baseline_profile"),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "queue_position": job.get("queue_position"),
            "artifact_state": state,
        })
    active = next((item for item in items if item["status"] in {"running", "queued"}), None)
    return {
        "active": active,
        "items": items,
        "ready_count": sum(item["status"] == "ready" for item in items),
    }


def _write_user_view(directory: Path, view_state: dict[str, Any] | None,
                     screenshot_data_url: str | None) -> None:
    if view_state is not None:
        write_json(directory / "user_view_state.json", view_state)
    if screenshot_data_url:
        prefix = "data:image/png;base64,"
        if not screenshot_data_url.startswith(prefix):
            raise ValueError("User view must be a PNG data URL")
        content = base64.b64decode(screenshot_data_url[len(prefix):], validate=True)
        if len(content) > 20 * 1024 * 1024 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("User view PNG is invalid or exceeds 20 MB")
        (directory / "user_view.png").write_bytes(content)


def commit_preview(job_id: str, *, view_state: dict[str, Any] | None = None,
                   screenshot_data_url: str | None = None) -> dict[str, Any]:
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] == "completed":
        runs = [run for run in db.list_runs() if run["job_id"] == job_id]
        return runs[0]
    if job["status"] != "ready" or not job.get("result_path"):
        raise ValueError(f"Job is not ready: {job['status']}")
    artifact_state = preview_artifact_state(job)
    if not artifact_state["recoverable"]:
        raise ValueError("Preview artifact integrity failed; " + "; ".join(artifact_state["issues"]))
    source = Path(job["result_path"])
    run_id = uuid.uuid4().hex
    staging = RUN_ROOT / f".staging-{run_id}"
    destination = RUN_ROOT / run_id
    source.replace(staging)
    try:
        renderer_manifest = renderer_provenance.verify_preview_renderer(staging)
        _write_user_view(staging, view_state, screenshot_data_url)
    except Exception:
        staging.replace(source)
        raise
    config = json.loads((staging / "config.json").read_text(encoding="utf-8"))
    stats = json.loads((staging / "stats.json").read_text(encoding="utf-8"))
    qa = json.loads((staging / "qa.json").read_text(encoding="utf-8"))
    osm_path = staging / "model_python.osm"
    raw_model_sha256 = sha256_file(osm_path)
    canonical_fingerprint = integrity.canonical_model_fingerprint(osm_path)
    input_manifest = json.loads((staging / "input_manifest.json").read_text(encoding="utf-8"))
    manifest_items = [
        {"name": item["name"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
        for item in artifact_manifest(staging) if item["name"] != "manifest.json"
    ]
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "job_id": job_id,
        "run_type": "model",
        "refparcela": job["refparcela"],
        "raw_model_sha256": raw_model_sha256,
        "canonical_model_fingerprint": canonical_fingerprint,
        "input_snapshots": input_manifest,
        "artifacts": manifest_items,
    }
    write_json(staging / "manifest.json", manifest)
    manifest_sha256 = sha256_file(staging / "manifest.json")
    artifacts = artifact_manifest(staging)
    for item in artifacts:
        item["path"] = str(destination / item["name"])
    run = {
        "id": run_id,
        "job_id": job_id,
        "refparcela": job["refparcela"],
        "scenario_name": config["provenance"]["scenario_name"],
        "config": config,
        "stats": stats,
        "qa": qa,
        "artifact_dir": str(staging),
        "verification_status": "COMMITTING",
        "raw_model_sha256": raw_model_sha256,
        "canonical_fingerprint": canonical_fingerprint,
        "manifest_sha256": manifest_sha256,
        "scenario_id": job["payload"].get("scenario_id"),
    }
    try:
        snapshot_refs = {role: item["snapshot_hash"] for role, item in input_manifest.items()}
        snapshot_refs.update({
            f"renderer:{index:03d}": item["snapshot_hash"]
            for index, item in enumerate(renderer_manifest.get("sources", []), 1)
        })
        db.insert_run(run, artifacts, snapshot_refs)
        staging.replace(destination)
        for path in destination.iterdir():
            if path.is_file():
                path.chmod(0o444)
        destination.chmod(0o555)
        db.finalize_run(run_id, str(destination), manifest_sha256)
    except Exception:
        if db.get_run(run_id):
            db.update_run_verification(run_id, "TAMPERED")
        raise
    db.update_job(job_id, "completed", result_path=str(destination))
    db.add_event(job_id, "Immutable run committed", 1.0)
    return db.get_run(run_id)


def verify_run_artifacts(run_id: str) -> dict[str, Any]:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["verification_status"] == "LEGACY":
        return {"ok": False, "status": "LEGACY", "issues": ["Run predates integrity manifests"]}
    issues = []
    root = Path(run["artifact_dir"])
    for artifact in run["artifacts"]:
        path = root / artifact["name"]
        if not path.exists():
            issues.append(f"Missing artifact: {artifact['name']}")
        elif path.stat().st_size != artifact["size_bytes"] or sha256_file(path) != artifact["sha256"]:
            issues.append(f"Artifact hash mismatch: {artifact['name']}")
    manifest_path = root / "manifest.json"
    if manifest_path.exists() and run.get("manifest_sha256"):
        if sha256_file(manifest_path) != run["manifest_sha256"]:
            issues.append("Run manifest hash mismatch")
    renderer_path = root / "renderer_manifest.json"
    if renderer_path.exists():
        try:
            renderer_manifest = renderer_provenance.load_renderer_manifest(renderer_path)
            issues.extend(renderer_provenance.verify_snapshot_references(renderer_manifest))
        except (ValueError, json.JSONDecodeError) as exc:
            issues.append(f"Renderer manifest is invalid: {exc}")
    if issues:
        db.update_run_verification(run_id, "TAMPERED")
        return {"ok": False, "status": "TAMPERED", "issues": issues}
    db.update_run_verification(run_id, "VERIFIED")
    return {"ok": True, "status": "VERIFIED", "issues": []}


def export_run(run_id: str) -> Path:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    verification = verify_run_artifacts(run_id)
    if verification["status"] == "TAMPERED":
        raise PermissionError("TAMPERED runs cannot be exported")
    output = EXPORT_ROOT / f"{run['refparcela']}_{run_id}.zip"
    root = Path(run["artifact_dir"])
    package_files = [{"path": item["name"], "sha256": item["sha256"],
                      "size_bytes": item["size_bytes"]} for item in run["artifacts"]]
    input_manifest_path = root / "input_manifest.json"
    input_manifest = json.loads(input_manifest_path.read_text(encoding="utf-8")) \
        if input_manifest_path.exists() else {}
    for role, snapshot in input_manifest.items():
        for name, path in integrity.snapshot_files(snapshot["snapshot_hash"]):
            package_files.append({"path": f"inputs/{role}/{name}", "sha256": sha256_file(path),
                                  "size_bytes": path.stat().st_size})
    renderer_path = root / "renderer_manifest.json"
    renderer_manifest = renderer_provenance.load_renderer_manifest(renderer_path) \
        if renderer_path.exists() else None
    if renderer_manifest:
        for source in renderer_manifest.get("sources", []):
            files = integrity.snapshot_files(source["snapshot_hash"])
            if len(files) != 1:
                raise IOError(f"Renderer source snapshot is ambiguous: {source['path']}")
            path = files[0][1]
            package_files.append({
                "path": f"renderer/source/{source['path']}",
                "sha256": sha256_file(path), "size_bytes": path.stat().st_size,
            })
    export_manifest = {
        "schema_version": 1, "run_id": run_id,
        "run_verification": verification["status"], "files": package_files,
    }
    signature = integrity.sign_manifest(export_manifest)
    required_bytes = sum(int(item["size_bytes"]) for item in package_files)
    evidence = storage.admission("export", requested_bytes=required_bytes)
    if not evidence["allowed"]:
        raise storage.StorageAdmissionError(
            f"Insufficient protected disk capacity for export: {evidence['reason']}", evidence,
        )
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = EXPORT_ROOT / f".{output.name}.{uuid.uuid4().hex}.tmp"
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.iterdir()):
                if path.is_file():
                    archive.write(path, arcname=path.name)
            for role, snapshot in input_manifest.items():
                for name, path in integrity.snapshot_files(snapshot["snapshot_hash"]):
                    archive.write(path, arcname=f"inputs/{role}/{name}")
            if renderer_manifest:
                for source in renderer_manifest.get("sources", []):
                    path = integrity.snapshot_files(source["snapshot_hash"])[0][1]
                    archive.write(path, arcname=f"renderer/source/{source['path']}")
            archive.writestr("export_manifest.json", integrity.canonical_json_bytes(export_manifest))
            archive.writestr("export_manifest.sig.json", integrity.canonical_json_bytes(signature))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def run_with_renderer_summary(run: dict[str, Any], *, current_fingerprint: str | None = None) -> dict[str, Any]:
    return run | {
        "renderer": renderer_provenance.summary(
            Path(run["artifact_dir"]), current_fingerprint=current_fingerprint,
        ),
    }


def _extract_zipped_shapefile(source: Path, staging: Path) -> Path:
    staging.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(source) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            for item in members:
                member_path = Path(item.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"Unsafe ZIP member path: {item.filename}")
            shp_members = [item for item in members if item.filename.lower().endswith(".shp")]
            if len(shp_members) != 1:
                raise ValueError("A zipped Shapefile must contain exactly one .shp dataset")
            shp_name = Path(shp_members[0].filename).name
            stem = Path(shp_name).stem
            selected = []
            for item in members:
                basename = Path(item.filename).name
                tail = basename[len(stem):].lower() if basename.lower().startswith(stem.lower()) else ""
                if tail in integrity.SHAPEFILE_SUFFIXES:
                    selected.append((item, basename))
            if len({name.lower() for _, name in selected}) != len(selected):
                raise ValueError("ZIP contains duplicate Shapefile sidecar names")
            for item, basename in selected:
                (staging / basename).write_bytes(archive.read(item))
        shp_path = staging / shp_name
        integrity.dataset_components(shp_path)
        return shp_path
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_climate_pair(epw: Path, ddy: Path) -> dict[str, Any]:
    mutable = runtime_environment()["mutable_paths"]
    assert isinstance(mutable, dict)
    _, record = file_inputs.build_climate_bundle(
        epw, ddy, Path(str(mutable["var_dir"])) / "climates")
    return record


def _inspect_uploaded_dataset(kind: str, source: Path) -> dict[str, Any]:
    """Run the domain contract before an upload is registered as VERIFIED."""
    if kind == "tipo15":
        return file_inputs.inspect_tipo15(source)
    if kind == "weather":
        return file_inputs.inspect_weather(source)
    if kind == "ddy":
        return file_inputs.inspect_ddy(source)
    if kind == "template":
        report = template_contract.validate_template(source)
        return {
            "contract": "template-roles-v1",
            "bound_roles": len(report["found"]),
            "missing_optional_roles": [item["role"] for item in report["missing"]],
        }
    if kind == "stock":
        return file_inputs.inspect_stock(source)
    if kind == "microclimate":
        return _inspect_microclimate(source)
    return {}


def _inspect_microclimate(source: Path) -> dict[str, Any]:
    """Validate a PALM slice with the loader that will actually read it.

    `microclimate.load_slice` is already fail-closed - it demands the metadata,
    exactly one `ta_max`/`ta_min` raster, a declared CRS and a matching grid -
    so it is called here rather than restated.  A slice arrives as a directory
    or a zip of one; both are reduced to the directory the loader wants.
    """
    import microclimate as mcl

    directory = source
    if source.is_file() and source.suffix.lower() == ".zip":
        staged = IMPORT_ROOT / f".microclimate-{uuid.uuid4().hex}"
        try:
            with zipfile.ZipFile(source) as archive:
                for member in archive.namelist():
                    resolved = (staged / member).resolve()
                    if staged.resolve() not in resolved.parents and resolved != staged.resolve():
                        raise ValueError(f"microclimate archive escapes its directory: {member}")
                archive.extractall(staged)
            directory = _slice_root(staged)
            slice_ = mcl.load_slice(directory)
        finally:
            shutil.rmtree(staged, ignore_errors=True)
    else:
        slice_ = mcl.load_slice(directory)
    record = slice_.record()
    return {
        "contract": "palm-slice-v1",
        "slice_name": record.get("name"),
        "slice_fingerprint": record.get("fingerprint"),
        "crs": record.get("crs"),
        "coverage_note": record.get("coverage_note"),
    }


def ingest_eu_database(dataset_id: str, name: str, *, population: int,
                       crs: str, include_mixed: bool = False) -> dict[str, Any]:
    """Turn a registered EU building database into a registered stock file.

    The translation itself is `lecco_stock.extract`, which is a data adapter and
    not a second pipeline: it maps one source's shape onto the fields the engine
    reads.  Wiring it here is what lets a city be brought in from the interface
    instead of from a terminal, and the product of one step becomes the input of
    the next - the stock it writes is registered exactly like an uploaded one and
    goes through the same contract.
    """
    import lecco_stock

    source = db.get_dataset(dataset_id)
    if source is None or source.get("kind") != "eu_database":
        raise ValueError(f"{dataset_id} is not a registered EU building database")

    target_dir = IMPORT_ROOT / f"derived-{dataset_id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{Path(str(source['path'])).stem}_stock.gpkg"
    if out_path.exists():
        out_path.chmod(0o644)
        out_path.unlink()
    lecco_stock.extract(Path(str(source["path"])), out_path,
                        population=population, crs=crs,
                        include_mixed=include_mixed)
    out_path.chmod(0o444)

    # Registered through the same door as an upload, so the stock contract is
    # enforced on a derived file exactly as on one somebody hands us.
    return import_dataset("stock", name, out_path, original_name=out_path.name)


def _slice_root(staged: Path) -> Path:
    """A zip made from a folder nests everything one level down; accept both."""
    if (staged / "meta.json").is_file():
        return staged
    children = [child for child in staged.iterdir() if child.is_dir()]
    if len(children) == 1 and (children[0] / "meta.json").is_file():
        return children[0]
    return staged


def import_dataset(kind: str, name: str, source: Path, *, original_name: str | None = None) -> dict[str, Any]:
    preserved_name = Path(original_name).name if original_name else source.name
    staged_dir: Path | None = None
    snapshot_source = source
    if kind == "gis" and Path(preserved_name).suffix.lower() == ".zip":
        staged_dir = IMPORT_ROOT / f".staging-{uuid.uuid4().hex}"
        snapshot_source = _extract_zipped_shapefile(source, staged_dir)
        preserved_name = snapshot_source.name

    validation = _inspect_uploaded_dataset(kind, snapshot_source)
    snapshot = integrity.ensure_snapshot(snapshot_source, kind=kind)
    digest = snapshot["snapshot_hash"]
    dataset_id = f"managed-{digest[:20]}"
    target_dir = IMPORT_ROOT / dataset_id
    if staged_dir is not None and not target_dir.exists():
        staged_dir.replace(target_dir)
        staged_dir = None
        snapshot_source = target_dir / preserved_name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / preserved_name
    source_components = integrity.dataset_components(snapshot_source)
    for component in source_components:
        component_name = preserved_name if len(source_components) == 1 else component.name
        component_target = target_dir / component_name
        if not component_target.exists():
            shutil.copy2(component, component_target)
        component_target.chmod(0o444)
    if staged_dir is not None:
        shutil.rmtree(staged_dir)
    metadata: dict[str, Any] = {
        "managed": True, "original_name": preserved_name,
        "snapshot_components": snapshot["components"],
    } | validation
    if kind == "gis":
        try:
            gdf = gpd.read_file(target)
            metadata.update({
                "rows": len(gdf), "crs": str(gdf.crs), "columns": list(gdf.columns),
                "geometry_types": sorted(gdf.geometry.geom_type.dropna().unique().tolist()),
            })
        except Exception as exc:
            metadata["inspection_error"] = str(exc)
    item = {
        "id": dataset_id, "kind": kind, "name": name, "path": str(target),
        "sha256": digest, "snapshot_hash": digest, "verification_status": "VERIFIED",
        "read_only": True, "metadata": metadata,
    }
    db.upsert_dataset(item)
    return db.get_dataset(dataset_id)


ALLOWED_CODE_SYMBOLS = {
    "validate_input_files", "validate_building_row", "load_buildings", "clean_polygon",
    "prepare_footprint", "load_neighbors", "find_party_walls", "_wall_plan_segment",
    "_build_layered_wall", "_build_layered_roof", "_construction_with_delta_u",
    "wwr_for_azimuth", "_add_facade_openings", "_add_context_shading",
    "build_model", "build_model_with_config", "save_model",
}


def code_symbol(name: str) -> dict[str, Any]:
    if name not in ALLOWED_CODE_SYMBOLS or not hasattr(mb, name):
        raise KeyError(name)
    symbol = getattr(mb, name)
    source_lines, start = inspect.getsourcelines(symbol)
    source = "".join(source_lines)
    return {
        "name": name,
        "file": str(Path(inspect.getsourcefile(symbol) or "").resolve()),
        "start_line": start,
        "end_line": start + len(source_lines) - 1,
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
        "source": source,
    }
