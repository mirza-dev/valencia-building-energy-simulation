"""Project defaults, preflight and immutable resolution for stock policies."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

from stock_input_policy import (
    POLICY_REGISTRY_VERSION, POLICY_SCHEMA_VERSION, TABULA_CLUSTER_TARGETS, StockInputPolicy,
    StockPolicyError, policy_fingerprint, prepare_stock,
    resolve_policy,
)
from workbench import db


SUPPORTED_WORKFLOWS = {"neighborhood", "city"}
TIPO15_DATASET_ID = "tipo15-ledger"


def _require_workflow(workflow: str) -> None:
    if workflow not in SUPPORTED_WORKFLOWS:
        raise ValueError(f"Stock input policy is not configurable for workflow: {workflow}")


def _default_requested_policy() -> StockInputPolicy:
    active = db.project_settings().get("building_dataset_id") or "valencia-city"
    return StockInputPolicy(gis_dataset_id=str(active))


def _project_record(workflow: str) -> dict[str, Any] | None:
    _require_workflow(workflow)
    return db.get_workflow_input_policy(workflow)


def _requested_policy(workflow: str) -> tuple[StockInputPolicy, str, int | None]:
    record = _project_record(workflow)
    if record:
        return StockInputPolicy.from_dict(record["policy"]), "project_default", int(record["revision"])
    return _default_requested_policy(), "automatic_default", None


def _dataset(policy: StockInputPolicy) -> dict[str, Any]:
    dataset = db.get_dataset(policy.gis_dataset_id)
    if dataset is None:
        raise StockPolicyError("dataset_not_found", f"GIS dataset not found: {policy.gis_dataset_id}")
    if dataset["kind"] != "gis":
        raise StockPolicyError("dataset_not_gis", f"Dataset {policy.gis_dataset_id} is not GIS")
    return dataset


def _tipo15_dataset() -> dict[str, Any]:
    dataset = db.get_dataset(TIPO15_DATASET_ID)
    if dataset is None:
        raise StockPolicyError("tipo15_not_found", "Tipo15 companion dataset is not registered")
    return dataset


def _tipo15_path() -> Path:
    return Path(_tipo15_dataset()["path"])


def _field_options(frame: gpd.GeoDataFrame, cluster_field: str | None = None) -> dict[str, Any]:
    numeric: list[str] = []
    text: list[str] = []
    for column in frame.columns:
        if column == frame.geometry.name:
            continue
        if pd.api.types.is_numeric_dtype(frame[column]):
            numeric.append(str(column))
        else:
            text.append(str(column))
    cluster_values = (
        sorted({str(value).strip() for value in frame[cluster_field].dropna() if str(value).strip()})
        if cluster_field and cluster_field in frame.columns else []
    )
    return {
        "all_fields": sorted(str(column) for column in frame.columns if column != frame.geometry.name),
        "numeric_fields": sorted(numeric),
        "text_fields": sorted(text),
        "geometry_area_available": frame.crs is not None,
        "cluster_values": cluster_values,
    }


def _merge_policy(base: StockInputPolicy, override: dict[str, Any] | None) -> StockInputPolicy:
    if not override:
        return base
    return StockInputPolicy.from_dict(base.to_dict() | override)


def _issue(code: str, message: str, *, count: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if count is not None:
        result["count"] = int(count)
    return result


def _preflight_policy_uncached(
    workflow: str, override: dict[str, Any] | None = None, *,
    district: str | None = None, building_ref: str | None = None,
) -> dict[str, Any]:
    _require_workflow(workflow)
    base, base_source, revision = _requested_policy(workflow)
    requested = _merge_policy(base, override)
    warnings: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    dataset: dict[str, Any] | None = None
    tipo15_dataset: dict[str, Any] | None = None
    fields: dict[str, Any] = {
        "all_fields": [], "numeric_fields": [], "text_fields": [],
        "geometry_area_available": False, "cluster_values": [],
    }
    resolved: StockInputPolicy | None = None
    resolved_base = base
    report: dict[str, Any] = {}
    representatives = 0
    try:
        dataset = _dataset(requested)
        tipo15_dataset = _tipo15_dataset()
        frame = gpd.read_file(Path(dataset["path"]))
        fields = _field_options(frame, requested.cluster_field)
        if base.gis_dataset_id == requested.gis_dataset_id:
            resolved_base, _ = resolve_policy(base, frame)
        else:
            base_dataset = _dataset(base)
            base_frame = gpd.read_file(Path(base_dataset["path"]))
            resolved_base, _ = resolve_policy(base, base_frame)
        import neighborhood_pipeline as part_c
        stock, resolved, report = prepare_stock(
            Path(dataset["path"]), Path(tipo15_dataset["path"]), requested,
            boundary_path=(
                part_c.BOUNDARY_GPKG
                if workflow == "neighborhood" and not district and not building_ref else None
            ),
            district=district if workflow == "neighborhood" else None,
            reference=building_ref if workflow == "neighborhood" else None,
            duplicate_parcel_apportioning=workflow == "city",
        )
        representatives = int(len(part_c.select_representatives(stock)))
        if report.get("excluded_cluster_buildings"):
            warnings.append(_issue(
                "cluster_exclusions",
                "Cluster mapping explicitly excludes buildings; retained stock remains runnable.",
                count=report["excluded_cluster_buildings"],
            ))
        if report.get("invalid_floor_buildings"):
            mode = resolved.floor_invalid_policy
            warnings.append(_issue(
                f"floor_{mode}", f"Invalid floor rows will use policy: {mode}.",
                count=report["invalid_floor_buildings"],
            ))
        if report.get("footprint_geometry_fallback_buildings"):
            warnings.append(_issue(
                "footprint_geometry_fallback",
                "Invalid selected footprint values fall back to EPSG:25830 geometry area.",
                count=report["footprint_geometry_fallback_buildings"],
            ))
        if report.get("residential_area_proxy_buildings"):
            warnings.append(_issue(
                "residential_area_proxy",
                "Residential area uses the visible cluster-ratio proxy for these buildings.",
                count=report["residential_area_proxy_buildings"],
            ))
        if resolved.ground_floor_mode in {"force_unconditioned", "force_conditioned"}:
            warnings.append(_issue(
                "forced_ground_mode",
                "Advanced ground-floor override applies to every representative in this run.",
                count=representatives,
            ))
        elif resolved.ground_floor_mode == "family_default":
            warnings.append(_issue(
                "family_ground_assumption",
                "Ground-floor rules ignore available Tipo15 records and use family assumptions.",
                count=representatives,
            ))
    except StockPolicyError as exc:
        blockers.append(_issue(exc.code, str(exc)))
    except (OSError, ValueError, KeyError) as exc:
        blockers.append(_issue("preflight_failed", str(exc)))

    requested_dict = requested.to_dict()
    resolved_dict = resolved.to_dict() if resolved else None
    source = "run_override" if override else base_source
    base_dict = resolved_base.to_dict()
    diff = {
        key: {"project_default": base_dict.get(key), "requested": value}
        for key, value in requested_dict.items()
        if (resolved_dict or requested_dict).get(key) != base_dict.get(key)
    }
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "registry_version": POLICY_REGISTRY_VERSION,
        "workflow": workflow,
        "source": source,
        "project_revision": revision,
        "requested_policy": requested_dict,
        "resolved_policy": resolved_dict,
        "policy_fingerprint": policy_fingerprint(resolved) if resolved else None,
        "dataset": None if dataset is None else {
            "id": dataset["id"], "name": dataset["name"], "path": dataset["path"],
            "snapshot_hash": dataset.get("snapshot_hash"),
        },
        "tipo15_dataset_id": TIPO15_DATASET_ID,
        "tipo15_dataset": None if tipo15_dataset is None else {
            "id": tipo15_dataset["id"], "name": tipo15_dataset["name"],
            "path": tipo15_dataset["path"], "snapshot_hash": tipo15_dataset.get("snapshot_hash"),
        },
        "field_options": fields,
        "cluster_targets": list(TABULA_CLUSTER_TARGETS),
        "coverage": report | {"representatives": representatives},
        "warnings": warnings,
        "blockers": blockers,
        "ready": not blockers,
        "override_diff": diff,
    }


def _path_stamp(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
        return str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns)
    except OSError:
        return str(path), -1, -1


def _preflight_cache_context(
    workflow: str, override: dict[str, Any] | None, district: str | None,
    building_ref: str | None,
) -> str:
    base, source, revision = _requested_policy(workflow)
    requested = _merge_policy(base, override)
    dataset = db.get_dataset(requested.gis_dataset_id)
    tipo15 = db.get_dataset(TIPO15_DATASET_ID)
    boundary_stamp: tuple[str, int, int] | None = None
    if workflow == "neighborhood" and not district and not building_ref:
        import neighborhood_pipeline as part_c
        boundary_stamp = _path_stamp(Path(part_c.BOUNDARY_GPKG))
    return json.dumps({
        "db": str(db.DB_PATH.resolve()),
        "registry": POLICY_REGISTRY_VERSION,
        "workflow": workflow,
        "source": source,
        "revision": revision,
        "requested": requested.to_dict(),
        "dataset": None if dataset is None else [
            dataset.get("id"), dataset.get("snapshot_hash"), _path_stamp(Path(dataset["path"])),
        ],
        "tipo15": None if tipo15 is None else [
            tipo15.get("id"), tipo15.get("snapshot_hash"), _path_stamp(Path(tipo15["path"])),
        ],
        "boundary": boundary_stamp,
        "district": district,
        "building_ref": building_ref,
    }, sort_keys=True, separators=(",", ":"))


@lru_cache(maxsize=64)
def _cached_preflight(
    workflow: str, override_json: str, district: str | None,
    building_ref: str | None, _context: str,
) -> dict[str, Any]:
    override = json.loads(override_json) if override_json else None
    return _preflight_policy_uncached(
        workflow, override, district=district, building_ref=building_ref,
    )


def preflight_policy(
    workflow: str, override: dict[str, Any] | None = None, *,
    district: str | None = None, building_ref: str | None = None,
) -> dict[str, Any]:
    """Return a defensive copy of an immutable-source keyed preflight result."""
    _require_workflow(workflow)
    override_json = json.dumps(override, sort_keys=True, separators=(",", ":")) if override else ""
    context = _preflight_cache_context(workflow, override, district, building_ref)
    return deepcopy(_cached_preflight(workflow, override_json, district, building_ref, context))


def get_policy(workflow: str) -> dict[str, Any]:
    return preflight_policy(workflow)


def save_project_policy(workflow: str, value: dict[str, Any]) -> dict[str, Any]:
    check = preflight_policy(workflow, value)
    if check["blockers"] or not check["resolved_policy"]:
        raise StockPolicyError("policy_blocked", "; ".join(item["message"] for item in check["blockers"]))
    resolved = StockInputPolicy.from_dict(check["resolved_policy"])
    db.upsert_workflow_input_policy(
        workflow, POLICY_SCHEMA_VERSION, resolved.to_dict(), policy_fingerprint(resolved),
    )
    return get_policy(workflow)


def resolve_job_policy(
    workflow: str, override: dict[str, Any] | None = None, *, district: str | None = None,
    building_ref: str | None = None,
) -> dict[str, Any]:
    check = preflight_policy(
        workflow, override, district=district, building_ref=building_ref,
    )
    if check["blockers"] or not check["resolved_policy"]:
        raise StockPolicyError("policy_blocked", "; ".join(item["message"] for item in check["blockers"]))
    # This entire object is copied into the queued job payload.  Later project
    # edits cannot alter the job's resolved policy or counters.
    return check
