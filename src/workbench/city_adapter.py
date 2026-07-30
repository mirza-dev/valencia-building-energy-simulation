"""Thin orchestration adapter for the user-owned Part D city pipeline.

The Workbench owns progress, failure isolation and serialization only. Stock
cleaning, representative selection, simulation, scaling, district aggregation
and scientific validation remain in the user's Part C/D modules.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any, Callable

import geopandas as gpd
import pandas as pd


REQUIRED_ENTRYPOINTS = (
    "load_stock_city",
    "district_breakdown",
    "validate_city",
)
REQUIRED_PART_C_ENTRYPOINTS = (
    "select_representatives",
    "run_representative",
    "scale_to_stock",
)


def _part_d():
    module = importlib.import_module("city_pipeline")
    missing = [name for name in REQUIRED_ENTRYPOINTS if not callable(getattr(module, name, None))]
    missing += [
        f"neighborhood_pipeline.{name}"
        for name in REQUIRED_PART_C_ENTRYPOINTS
        if not callable(getattr(module.nbp, name, None))
    ]
    if missing:
        raise RuntimeError(f"Part D callable contract is incomplete: {', '.join(missing)}")
    return module


def source_paths(policy_context: dict[str, Any] | None = None) -> dict[str, tuple[Path, str]]:
    """Return every real source/input used by the Part D baseline."""
    part_d = _part_d()
    policy_context = policy_context or {}
    dataset = policy_context.get("dataset") or {}
    tipo15_dataset = policy_context.get("tipo15_dataset") or {}
    paths = {
        "city_gis": (Path(dataset.get("path") or part_d.mb.NEIGHBORS_SHP), "gis"),
        "tipo15": (Path(tipo15_dataset.get("path") or part_d.nbp.TIPO15_CSV), "source"),
        "template": (Path(part_d.mb.TEMPLATE_OSM), "template"),
        "weather": (Path(part_d.mb.EPW_FILE), "weather"),
        "city_pipeline": (Path(part_d.__file__).resolve(), "source"),
        "neighborhood_pipeline": (Path(part_d.nbp.__file__).resolve(), "source"),
        "model_builder": (Path(part_d.mb.__file__).resolve(), "source"),
        "run_simulation": (Path(part_d.nbp.sim.__file__).resolve(), "source"),
        "city_adapter": (Path(__file__).resolve(), "source"),
    }
    heatmaps = importlib.util.find_spec("make_heatmaps")
    if heatmaps and heatmaps.origin:
        paths["make_heatmaps"] = (Path(heatmaps.origin).resolve(), "source")
    return paths


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if pd.notna(value) else None
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        return _json_value(value.item())
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [_json_value(item) for item in frame.to_dict(orient="records")]


def _district_inventory(stock: gpd.GeoDataFrame, part_d) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    points: list[Any] = []
    for name, group in stock.groupby(part_d.COL_DISTRICT_NAME, dropna=False):
        code = group[part_d.COL_DISTRICT_CODE].mode()
        records.append({
            "nombre": _json_value(name),
            "coddistrit": _json_value(code.iat[0] if not code.empty else None),
            "n_buildings": int(len(group)),
            "res_area_m2": round(float(group["res_area_m2"].sum()), 1),
        })
        points.append(group.geometry.union_all().representative_point())
    labels = gpd.GeoDataFrame(records, geometry=points, crs=stock.crs).to_crs(4326)
    for record, point in zip(records, labels.geometry, strict=True):
        record["longitude"] = float(point.x)
        record["latitude"] = float(point.y)
    return sorted(records, key=lambda item: str(item["nombre"]))


def _bounds_4326(stock: gpd.GeoDataFrame) -> list[float]:
    return [float(value) for value in stock.to_crs(4326).total_bounds]


def inspect_city(policy_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only city stock and representative-selection preflight."""
    part_d = _part_d()
    context = policy_context or {}
    dataset = context.get("dataset") or {}
    stock = part_d.load_stock_city(
        gis_path=dataset.get("path"), input_policy=context.get("resolved_policy"),
    )
    representatives = part_d.nbp.select_representatives(stock)
    districts = _district_inventory(stock, part_d)
    return {
        "schema_version": 1,
        "scope": "Valencia",
        "method": "representative_typology_period",
        "locked": True,
        "summary": {
            "buildings": int(len(stock)),
            "clusters": int(stock["cluster"].nunique()),
            "representatives": int(len(representatives)),
            "districts": int(len(districts)),
            "residential_area_m2": round(float(stock["res_area_m2"].sum()), 3),
            "imputed_floor_buildings": int(stock["imputed_floors"].sum()),
            "proxy_area_buildings": int(stock["res_area_proxy"].sum()),
            "duplicate_parcel_rows": int(stock.get("dup_refparcela", pd.Series(dtype=bool)).sum()),
            "stock_policy": stock.attrs.get("stock_policy_report", {}),
        },
        "representatives": _records(representatives),
        "districts": districts,
        "bounds": _bounds_4326(stock),
    }


def _write_representative_archive(run_dir: Path, cluster_names: list[str]) -> None:
    manifest: list[dict[str, Any]] = []
    archive_path = run_dir / "representative_artifacts.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for cluster in sorted(cluster_names):
            root = run_dir / cluster
            if not root.exists():
                continue
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                relative = path.relative_to(run_dir).as_posix()
                manifest.append({
                    "path": relative,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                })
                archive.write(path, arcname=relative)
    (run_dir / "representative_artifacts_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    for cluster in cluster_names:
        shutil.rmtree(run_dir / cluster, ignore_errors=True)


def _write_buildings(buildings: gpd.GeoDataFrame, part_d, target: Path) -> None:
    columns = [
        "refparcela", "cluster", "family", "period", "altura_max",
        "imputed_floors", "res_area_m2", "res_area_proxy", "dup_refparcela",
        part_d.COL_DISTRICT_CODE, part_d.COL_DISTRICT_NAME, "pob_total",
        "heating_kwh_m2", "cooling_kwh_m2", "heating_kwh", "cooling_kwh",
        "s1_co2_kg_m2", "s2_co2_kg_m2", "s1_co2_t", "s2_co2_t",
        "cons_hc_kwh_m2", "total_site_kwh_m2",
        "s1_consumption_kwh_m2", "s2_consumption_kwh_m2",
        "hvac_co2_kg_m2", "total_site_co2_kg_m2",
        "cons_hc_kwh", "total_site_kwh",
        "s1_consumption_kwh", "s2_consumption_kwh",
        "hvac_co2_t", "total_site_co2_t",
        part_d.nbp.COL_CERT, "geometry",
    ]
    buildings[[column for column in columns if column in buildings.columns]].to_file(
        target, driver="GPKG",
    )


def _certificate_summary(buildings: gpd.GeoDataFrame, cert_column: str) -> dict[str, Any]:
    valid = buildings[cert_column].notna() & (buildings[cert_column] > 0)
    if not valid.any():
        return {"buildings": 0, "model_kwh_m2": None, "certificate_kwh_m2": None, "ratio": None}
    area = buildings.loc[valid, "res_area_m2"]
    model = float((buildings.loc[valid, "heating_kwh_m2"] * area).sum() / area.sum())
    certificate = float((buildings.loc[valid, cert_column] * area).sum() / area.sum())
    return {
        "buildings": int(valid.sum()),
        "model_kwh_m2": model,
        "certificate_kwh_m2": certificate,
        "ratio": model / certificate if certificate else None,
        "definition_status": "CONDITIONAL_JAVIER_Q3",
    }


def run_city(run_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Execute the real Part D chain and preserve independent cluster evidence."""
    part_d = _part_d()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    progress: Callable[[str, float], None] = settings.get("_progress", lambda *_: None)

    progress("City stock", 0.05)
    policy_context = settings.get("stock_input_policy") or {}
    dataset = policy_context.get("dataset") or {}
    policy = policy_context.get("resolved_policy")
    tipo15_dataset = policy_context.get("tipo15_dataset") or {}
    tipo15_path = Path(tipo15_dataset.get("path") or part_d.nbp.TIPO15_CSV) if policy_context else None
    if policy_context:
        stock = part_d.load_stock_city(
            gis_path=dataset.get("path"), tipo15_path=tipo15_path, input_policy=policy,
        )
    else:
        stock = part_d.load_stock_city()
    domain_scenario = settings.get("_domain_scenario") or {}
    progress("Representatives", 0.10)
    representatives = part_d.nbp.select_representatives(stock)
    representatives.to_csv(run_dir / "representatives.csv", index=False)

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    total = len(representatives)
    for index, (_, representative) in enumerate(representatives.iterrows(), start=1):
        cluster = str(representative["cluster"])
        progress(f"Simulation {index}/{total} · {cluster}", 0.12 + 0.62 * (index - 1) / total)
        try:
            if domain_scenario and policy_context:
                record, qa_ok = part_d.nbp.run_representative(
                    stock, representative, run_dir, scenario=domain_scenario,
                    tipo15_path=tipo15_path, input_policy=policy, gis_path=dataset.get("path"),
                )
            elif domain_scenario:
                record, qa_ok = part_d.nbp.run_representative(
                    stock, representative, run_dir, scenario=domain_scenario,
                )
            elif policy_context:
                record, qa_ok = part_d.nbp.run_representative(
                    stock, representative, run_dir,
                    tipo15_path=tipo15_path, input_policy=policy, gis_path=dataset.get("path"),
                )
            else:
                record, qa_ok = part_d.nbp.run_representative(stock, representative, run_dir)
            results.append(_json_value(record) | {"qa_all_pass": bool(qa_ok)})
        except Exception as exc:
            failures.append({
                "cluster": cluster,
                "refparcela": str(representative["refparcela"]),
                "stage": "representative_simulation",
                "error": str(exc),
            })
    cluster_results = pd.DataFrame(results)
    cluster_results.to_csv(run_dir / "clusters_results.csv", index=False)
    (run_dir / "failed_clusters.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    buildings = None
    districts = pd.DataFrame()
    validation = ""
    visualization_warning = None
    if not failures and len(cluster_results) == len(representatives):
        progress("City scaling", 0.78)
        buildings = part_d.nbp.scale_to_stock(stock, cluster_results)
        _write_buildings(buildings, part_d, run_dir / "results_buildings.gpkg")
        progress("District aggregation", 0.84)
        districts = part_d.district_breakdown(buildings)
        districts.to_csv(run_dir / "districts.csv", index=False)
        progress("Validation", 0.89)
        validation = part_d.validate_city(buildings, districts)
        (run_dir / "validation.txt").write_text(validation, encoding="utf-8")
        try:
            heatmaps = importlib.import_module("make_heatmaps")
            heatmaps.render_heatmap(run_dir / "results_buildings.gpkg", run_dir / "heatmap.png")
        except Exception as exc:
            visualization_warning = str(exc)
            (run_dir / "visualization_warning.txt").write_text(visualization_warning, encoding="utf-8")

    district_inventory = _district_inventory(stock, part_d)
    district_positions = {str(item["nombre"]): item for item in district_inventory}
    district_records = _records(districts)
    for record in district_records:
        position = district_positions.get(str(record.get("nombre")), {})
        record["longitude"] = position.get("longitude")
        record["latitude"] = position.get("latitude")
    map_metrics = {
        "schema_version": 1,
        "bounds": _bounds_4326(stock),
        "clusters": _records(cluster_results) if buildings is not None else [],
        "districts": district_records if buildings is not None else district_inventory,
        "energy_available": buildings is not None,
    }
    (run_dir / "map_metrics.json").write_text(
        json.dumps(map_metrics, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    _write_representative_archive(run_dir, list(representatives["cluster"].astype(str)))
    qa_passed_count = sum(bool(item.get("qa_all_pass")) for item in results)
    clusters_complete = len(results) == len(representatives) and not failures
    cluster_qa_pass = clusters_complete and qa_passed_count == len(representatives)
    district_count_pass = buildings is not None and len(districts) == 19
    demand_reconciliation_pass = False
    consumption_reconciliation_pass = False
    consumption_columns = {
        "cons_hc_kwh", "total_site_kwh", "hvac_co2_t", "total_site_co2_t",
        "s1_consumption_kwh", "s2_consumption_kwh",
    }
    consumption_available = buildings is not None and consumption_columns.issubset(buildings.columns)
    if buildings is not None and not districts.empty:
        district_heating = float(districts["heating_gwh"].sum())
        district_cooling = float(districts["cooling_gwh"].sum())
        city_heating = float(buildings["heating_kwh"].sum() / 1e6)
        city_cooling = float(buildings["cooling_kwh"].sum() / 1e6)
        demand_reconciliation_pass = (
            abs(district_heating - city_heating) <= 0.05
            and abs(district_cooling - city_cooling) <= 0.05
        )
        if consumption_available and {"cons_hc_gwh", "total_site_gwh"}.issubset(districts.columns):
            district_hvac = float(districts["cons_hc_gwh"].sum())
            district_site = float(districts["total_site_gwh"].sum())
            city_hvac = float(buildings["cons_hc_kwh"].sum() / 1e6)
            city_site = float(buildings["total_site_kwh"].sum() / 1e6)
            consumption_reconciliation_pass = (
                abs(district_hvac - city_hvac) <= 0.05
                and abs(district_site - city_site) <= 0.05
            )
    all_pass = (
        clusters_complete and cluster_qa_pass and district_count_pass
        and demand_reconciliation_pass and consumption_reconciliation_pass
    )
    scientific_status = "INVALID" if failures else "VALIDATED" if all_pass else "UNVERIFIED"
    totals = None if buildings is None else {
        "heating_gwh_yr": float(buildings["heating_kwh"].sum() / 1e6),
        "cooling_gwh_yr": float(buildings["cooling_kwh"].sum() / 1e6),
        "s1_co2_t_yr": float(buildings["s1_co2_t"].sum()),
        "s2_co2_t_yr": float(buildings["s2_co2_t"].sum()),
        "hvac_consumption_gwh_yr": float(buildings["cons_hc_kwh"].sum() / 1e6),
        "total_site_gwh_yr": float(buildings["total_site_kwh"].sum() / 1e6),
        "s1_consumption_gwh_yr": float(buildings["s1_consumption_kwh"].sum() / 1e6),
        "s2_consumption_gwh_yr": float(buildings["s2_consumption_kwh"].sum() / 1e6),
        "hvac_co2_t_yr": float(buildings["hvac_co2_t"].sum()),
        "total_site_co2_t_yr": float(buildings["total_site_co2_t"].sum()),
        "residential_area_m2": float(buildings["res_area_m2"].sum()),
    } if consumption_available else None
    result = {
        "schema_version": 1,
        "settings": {key: _json_value(value) for key, value in settings.items() if not key.startswith("_")},
        "summary": {
            "scope": "Valencia",
            "buildings": int(len(stock)),
            "districts": int(len(districts)) if buildings is not None else 0,
            "clusters_expected": int(len(representatives)),
            "clusters_completed": int(len(results)),
            "clusters_failed": int(len(failures)),
            "qa_passed_clusters": int(qa_passed_count),
            "totals": totals,
            "certificate_heating": _certificate_summary(buildings, part_d.nbp.COL_CERT) if buildings is not None else None,
            "stock_policy": stock.attrs.get("stock_policy_report", {}),
        },
        "qa": {
            "all_pass": all_pass,
            "scientific_status": scientific_status,
            "checks": [
                {"id": "cluster_completion", "status": "pass" if clusters_complete else "fail", "message": f"{len(results)}/{len(representatives)} representative simulations completed"},
                {"id": "cluster_qa", "status": "pass" if cluster_qa_pass else "warn" if clusters_complete else "fail", "message": f"{qa_passed_count}/{len(representatives)} cluster QA passed"},
                {"id": "district_count", "status": "pass" if district_count_pass else "fail", "message": f"{len(districts) if buildings is not None else 0}/19 districts aggregated"},
                {"id": "demand_reconciliation", "status": "pass" if demand_reconciliation_pass else "fail", "message": "District demand totals reconcile with the city stock"},
                {"id": "consumption_reconciliation", "status": "pass" if consumption_reconciliation_pass else "fail", "message": "District HVAC and total-site consumption reconcile with the city stock"},
            ],
            "failures": failures,
            "visualization_warning": visualization_warning,
        },
        "representatives": _records(representatives),
        "clusters": _records(cluster_results),
        "districts": district_records,
        "validation": validation,
        "map_available": True,
        "energy_map_available": buildings is not None,
    }
    (run_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    progress("Finalize", 0.96)
    return result
