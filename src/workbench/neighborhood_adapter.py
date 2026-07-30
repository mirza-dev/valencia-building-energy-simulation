"""Thin orchestration adapter for the user-owned Part C pipeline.

This module deliberately contains no stock, energy, carbon, or validation
formula. It invokes the public Part C functions and serializes their outputs
for the Workbench.
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
    "load_stock",
    "select_representatives",
    "run_representative",
    "scale_to_stock",
    "validate",
)


def _part_c():
    module = importlib.import_module("neighborhood_pipeline")
    missing = [name for name in REQUIRED_ENTRYPOINTS if not callable(getattr(module, name, None))]
    if missing:
        raise RuntimeError(f"Part C callable contract is incomplete: {', '.join(missing)}")
    return module


def source_paths(policy_context: dict[str, Any] | None = None) -> dict[str, tuple[Path, str]]:
    """Return the real inputs used by Part C for provenance snapshots."""
    part_c = _part_c()
    policy_context = policy_context or {}
    dataset = policy_context.get("dataset") or {}
    tipo15_dataset = policy_context.get("tipo15_dataset") or {}
    return {
        "city_gis": (Path(dataset.get("path") or part_c.mb.NEIGHBORS_SHP), "gis"),
        "boundary_gis": (Path(part_c.BOUNDARY_GPKG), "gis"),
        "tipo15": (Path(tipo15_dataset.get("path") or part_c.TIPO15_CSV), "source"),
        "template": (Path(part_c.mb.TEMPLATE_OSM), "template"),
        "weather": (Path(part_c.mb.EPW_FILE), "weather"),
        "neighborhood_pipeline": (Path(part_c.__file__).resolve(), "source"),
        "model_builder": (Path(part_c.mb.__file__).resolve(), "source"),
        "run_simulation": (Path(part_c.sim.__file__).resolve(), "source"),
        "neighborhood_adapter": (Path(__file__).resolve(), "source"),
    }


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


def _stock_geojson(stock: gpd.GeoDataFrame, representatives: pd.DataFrame,
                   *, include_results: bool) -> dict[str, Any]:
    representative_refs = set(representatives["refparcela"].astype(str))
    columns = [
        "refparcela", "cluster", "family", "period", "altura_max",
        "res_area_m2", "imputed_floors", "res_area_proxy",
    ]
    if include_results:
        columns += [
            "heating_kwh_m2", "cooling_kwh_m2", "heating_kwh", "cooling_kwh",
            "s1_co2_t", "s2_co2_t",
            "cons_hc_kwh_m2", "total_site_kwh_m2", "cons_hc_kwh", "total_site_kwh",
            "hvac_co2_kg_m2", "total_site_co2_kg_m2", "hvac_co2_t", "total_site_co2_t",
        ]
    view = stock[[column for column in columns if column in stock.columns] + ["geometry"]].copy()
    view["is_representative"] = view["refparcela"].astype(str).isin(representative_refs)
    view = view.to_crs(4326)
    references = view["refparcela"].astype(str)
    occurrence = references.groupby(references, sort=False).cumcount() + 1
    totals = references.map(references.value_counts())
    view.index = [
        reference if total == 1 else f"{reference}::part-{part}"
        for reference, total, part in zip(references, totals, occurrence, strict=True)
    ]
    return json.loads(view.to_json(drop_id=False, na="null"))


def inspect_neighborhood(
    district: str | None = None, policy_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call Part C stock and representative selection for a read-only preflight."""
    part_c = _part_c()
    context = policy_context or {}
    dataset = context.get("dataset") or {}
    policy = context.get("resolved_policy")
    stock = part_c.load_stock(
        district=district, gis_path=dataset.get("path"), input_policy=policy,
    )
    representatives = part_c.select_representatives(stock)
    scope = district.strip().upper() if district else "Benicalap"
    return {
        "schema_version": 1,
        "scope": scope,
        "method": "representative_typology_period",
        "locked": True,
        "summary": {
            "buildings": int(len(stock)),
            "clusters": int(stock["cluster"].nunique()),
            "representatives": int(len(representatives)),
            "residential_area_m2": round(float(stock["res_area_m2"].sum()), 3),
            "imputed_floor_buildings": int(stock["imputed_floors"].sum()),
            "proxy_area_buildings": int(stock["res_area_proxy"].sum()),
            "stock_policy": stock.attrs.get("stock_policy_report", {}),
        },
        "representatives": _records(representatives),
        "map": _stock_geojson(stock, representatives, include_results=False),
    }


def district_options() -> list[str]:
    """Read municipal district labels from the same Part C stock source."""
    part_c = _part_c()
    labels = gpd.read_file(part_c.mb.NEIGHBORS_SHP, columns=["nombre"], ignore_geometry=True)
    return sorted({str(value).strip() for value in labels["nombre"].dropna() if str(value).strip()})


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
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                manifest.append({"path": relative, "sha256": digest, "size_bytes": path.stat().st_size})
                archive.write(path, arcname=relative)
    (run_dir / "representative_artifacts_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    for cluster in cluster_names:
        shutil.rmtree(run_dir / cluster, ignore_errors=True)


def run_neighborhood(run_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Execute the real Part C callable chain and retain per-cluster failures."""
    part_c = _part_c()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    progress: Callable[[str, float], None] = settings.get("_progress", lambda *_: None)

    progress("Stock", 0.06)
    district = settings.get("district")
    policy_context = settings.get("stock_input_policy") or {}
    dataset = policy_context.get("dataset") or {}
    policy = policy_context.get("resolved_policy")
    tipo15_dataset = policy_context.get("tipo15_dataset") or {}
    tipo15_path = Path(tipo15_dataset.get("path") or part_c.TIPO15_CSV) if policy_context else None
    if policy_context:
        stock = part_c.load_stock(
            district=district, gis_path=dataset.get("path"), tipo15_path=tipo15_path,
            input_policy=policy,
        )
    elif district:
        stock = part_c.load_stock(district=district)
    else:
        stock = part_c.load_stock()
    scope = str(district).strip().upper() if district else "Benicalap"
    domain_scenario = settings.get("_domain_scenario") or {}
    progress("Representatives", 0.12)
    representatives = part_c.select_representatives(stock)
    representatives.to_csv(run_dir / "representatives.csv", index=False)

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    total = max(1, len(representatives))
    for index, (_, representative) in enumerate(representatives.iterrows(), start=1):
        cluster = str(representative["cluster"])
        progress(f"Simulation {index}/{total} · {cluster}", 0.14 + 0.66 * (index - 1) / total)
        try:
            if domain_scenario and policy_context:
                record, qa_ok = part_c.run_representative(
                    stock, representative, run_dir, scenario=domain_scenario,
                    tipo15_path=tipo15_path, input_policy=policy, gis_path=dataset.get("path"),
                )
            elif domain_scenario:
                record, qa_ok = part_c.run_representative(
                    stock, representative, run_dir, scenario=domain_scenario,
                )
            elif policy_context:
                record, qa_ok = part_c.run_representative(
                    stock, representative, run_dir,
                    tipo15_path=tipo15_path, input_policy=policy, gis_path=dataset.get("path"),
                )
            else:
                record, qa_ok = part_c.run_representative(stock, representative, run_dir)
            results.append(_json_value(record) | {"qa_all_pass": bool(qa_ok)})
        except Exception as exc:  # failure isolation is orchestration, not physics
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
    validation = ""
    if not failures and len(cluster_results) == len(representatives):
        progress("Scaling", 0.84)
        buildings = part_c.scale_to_stock(stock, cluster_results)
        buildings.to_file(run_dir / "results_buildings.gpkg", driver="GPKG")
        (run_dir / "map.geojson").write_text(
            json.dumps(_stock_geojson(buildings, representatives, include_results=True), ensure_ascii=False),
            encoding="utf-8",
        )
        progress("Validation", 0.91)
        validation = part_c.validate(buildings)
        (run_dir / "validation.txt").write_text(validation, encoding="utf-8")

    _write_representative_archive(run_dir, list(representatives["cluster"].astype(str)))
    qa_passed = len(results) == len(representatives) and all(bool(item.get("qa_all_pass")) for item in results)
    scientific_status = "INVALID" if failures else "VALIDATED" if qa_passed else "UNVERIFIED"
    totals = None if buildings is None else {
        "heating_gwh_yr": float(buildings["heating_kwh"].sum() / 1e6),
        "cooling_gwh_yr": float(buildings["cooling_kwh"].sum() / 1e6),
        "s1_co2_t_yr": float(buildings["s1_co2_t"].sum()),
        "s2_co2_t_yr": float(buildings["s2_co2_t"].sum()),
        "residential_area_m2": float(buildings["res_area_m2"].sum()),
    }
    consumption_columns = {"cons_hc_kwh", "total_site_kwh", "hvac_co2_t", "total_site_co2_t"}
    if totals is not None and consumption_columns.issubset(buildings.columns):
        totals.update({
            "hvac_consumption_gwh_yr": float(buildings["cons_hc_kwh"].sum() / 1e6),
            "total_site_gwh_yr": float(buildings["total_site_kwh"].sum() / 1e6),
            "hvac_co2_t_yr": float(buildings["hvac_co2_t"].sum()),
            "total_site_co2_t_yr": float(buildings["total_site_co2_t"].sum()),
        })
    result = {
        "schema_version": 1,
        "settings": {key: _json_value(value) for key, value in settings.items() if not key.startswith("_")},
        "summary": {
            "scope": scope,
            "buildings": int(len(stock)),
            "clusters_expected": int(len(representatives)),
            "clusters_completed": int(len(results)),
            "clusters_failed": int(len(failures)),
            "qa_passed_clusters": int(sum(bool(item.get("qa_all_pass")) for item in results)),
            "totals": totals,
            "stock_policy": stock.attrs.get("stock_policy_report", {}),
        },
        "qa": {
            "all_pass": qa_passed,
            "scientific_status": scientific_status,
            "checks": [
                {"id": "cluster_completion", "status": "pass" if not failures else "fail",
                 "message": f"{len(results)}/{len(representatives)} representative simulations completed"},
                {"id": "cluster_qa", "status": "pass" if qa_passed else "warn" if not failures else "fail",
                 "message": f"{sum(bool(item.get('qa_all_pass')) for item in results)}/{len(representatives)} cluster QA passed"},
            ],
            "failures": failures,
        },
        "representatives": _records(representatives),
        "clusters": _records(cluster_results),
        "validation": validation,
        "map_available": buildings is not None,
    }
    (run_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    progress("Finalize", 0.96)
    return result


def run_single_building(run_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Invoke Part C's public single-building chain without duplicating physics.

    Part C deliberately exposes a status code and a human-readable evidence file,
    rather than a structured result object. The Workbench preserves that contract
    verbatim and only maps the status code to its immutable scientific state.
    """
    part_c = _part_c()
    entrypoint = getattr(part_c, "run_single_building", None)
    if not callable(entrypoint):
        raise RuntimeError("Part C single-building callable is unavailable")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    progress: Callable[[str, float], None] = settings.get("_progress", lambda *_: None)
    reference = str(settings.get("building_ref") or "").strip()
    if not reference:
        raise ValueError("A refparcela is required for a single-building run")
    domain_scenario = settings.get("_domain_scenario") or {}
    policy_context = settings.get("stock_input_policy") or {}
    dataset = policy_context.get("dataset") or {}
    policy = policy_context.get("resolved_policy")
    tipo15_dataset = policy_context.get("tipo15_dataset") or {}
    tipo15_path = Path(tipo15_dataset.get("path") or part_c.TIPO15_CSV) if policy_context else None

    progress("Single building", 0.12)
    if domain_scenario and policy_context:
        exit_code = int(entrypoint(
            reference, run_dir, scenario=domain_scenario,
            gis_path=dataset.get("path"), tipo15_path=tipo15_path, input_policy=policy,
        ))
    elif domain_scenario:
        exit_code = int(entrypoint(reference, run_dir, scenario=domain_scenario))
    elif policy_context:
        exit_code = int(entrypoint(
            reference, run_dir, gis_path=dataset.get("path"),
            tipo15_path=tipo15_path, input_policy=policy,
        ))
    else:
        exit_code = int(entrypoint(reference, run_dir))
    if exit_code not in {0, 1, 2}:
        raise RuntimeError(f"Part C returned an unsupported status code: {exit_code}")
    report_path = run_dir / f"building_{reference}.txt"
    report_text = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    if exit_code in {0, 1} and not report_text.strip():
        raise RuntimeError("Part C completed without its single-building evidence report")

    scientific_status = {0: "VALIDATED", 1: "UNVERIFIED", 2: "INVALID"}[exit_code]
    qa_passed = exit_code == 0
    result = {
        "schema_version": 1,
        "settings": {key: _json_value(value) for key, value in settings.items() if not key.startswith("_")},
        "summary": {
            "scope": reference.upper(),
            "buildings": 1,
            "clusters_expected": 1,
            "clusters_completed": 0 if exit_code == 2 else 1,
            "clusters_failed": 1 if exit_code == 2 else 0,
            "qa_passed_clusters": 1 if qa_passed else 0,
            "totals": None,
        },
        "qa": {
            "all_pass": qa_passed,
            "scientific_status": scientific_status,
            "checks": [{
                "id": "single_building_domain_status",
                "status": "pass" if exit_code == 0 else "warn" if exit_code == 1 else "fail",
                "message": f"Part C run_single_building returned {exit_code}",
            }],
            "failures": [] if exit_code != 2 else [{
                "cluster": "single_building",
                "refparcela": reference,
                "stage": "single_building",
                "error": "Part C rejected the building; inspect the immutable job log.",
            }],
        },
        "representatives": [],
        "clusters": [],
        "validation": report_text,
        "map_available": False,
        "single_building": {
            "refparcela": reference,
            "exit_code": exit_code,
            "report_artifact": report_path.name if report_path.exists() else None,
            "report_text": report_text,
        },
    }
    (run_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    progress("Finalize", 0.96)
    return result
