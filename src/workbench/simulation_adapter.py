"""Thin immutable-model adapter around the user-owned Part B runner."""

from __future__ import annotations

import csv
import importlib
import json
import platform
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import openstudio


REQUIRED_ENERGY_KEYS = ("heating", "cooling")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _progress(settings: dict[str, Any], stage: str, progress: float) -> None:
    callback: Callable[[str, float], None] | None = settings.get("_progress")
    if callback:
        callback(stage, progress)


def _load_parent_model(osm_path: Path) -> openstudio.model.Model:
    translator = openstudio.osversion.VersionTranslator()
    loaded = translator.loadModel(openstudio.toPath(str(osm_path)))
    if loaded.isNull():
        raise ValueError(f"Parent OSM cannot be loaded: {osm_path}")
    return loaded.get()


def _raw_energy(sql_path: Path, output_variables: dict[str, str], area_m2: float) -> dict[str, Any]:
    names = [output_variables[key] for key in REQUIRED_ENERGY_KEYS]
    placeholders = ",".join("?" for _ in names)
    with sqlite3.connect(sql_path) as connection:
        rows = connection.execute(
            f"""SELECT d.Name,COALESCE(SUM(r.Value),0.0)
                FROM ReportData r
                JOIN ReportDataDictionary d
                  ON r.ReportDataDictionaryIndex=d.ReportDataDictionaryIndex
                WHERE d.ReportingFrequency='Run Period'
                  AND d.Name IN ({placeholders})
                GROUP BY d.Name""",
            names,
        ).fetchall()
    totals = {str(name): float(value) for name, value in rows}
    missing = [name for name in names if name not in totals]
    if missing:
        raise RuntimeError(f"Annual SQL output is missing exact variables: {', '.join(missing)}")
    result: dict[str, Any] = {"area_basis": "conditioned_residential_area", "area_m2": area_m2}
    for key in REQUIRED_ENERGY_KEYS:
        joule = totals[output_variables[key]]
        kwh = joule / 3_600_000.0
        result[key] = {
            "variable": output_variables[key],
            "joule": joule,
            "kwh": kwh,
            "kwh_m2": kwh / area_m2,
        }
    return result


def _detailed_hvac_energy(consumption: dict[str, Any], area_m2: float) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized = {
        "heating_kwh": consumption["cons_heating_kwh"],
        "cooling_kwh": consumption["cons_cooling_kwh"],
        "heating_kwh_m2": consumption["cons_heating_kwh_m2"],
        "cooling_kwh_m2": consumption["cons_cooling_kwh_m2"],
        "hvac_consumption_kwh_m2": consumption["cons_hc_kwh_m2"],
        "total_site_kwh_m2": consumption["total_site_kwh_m2"],
        "energy_basis": "detailed_hvac_consumption",
    }
    raw: dict[str, Any] = {"area_basis": "conditioned_residential_area", "area_m2": area_m2}
    for key, label in (("heating", "End Uses · Heating"), ("cooling", "End Uses · Cooling")):
        kwh = float(normalized[f"{key}_kwh"])
        raw[key] = {
            "variable": label,
            "joule": kwh * 3_600_000.0,
            "kwh": kwh,
            "kwh_m2": float(normalized[f"{key}_kwh_m2"]),
        }
    return normalized, raw


def _err_evidence(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "eplusout.err"
    if not path.exists():
        return {"warnings": 0, "severes": 0, "fatals": 0, "categories": [], "messages": []}
    text = path.read_text(encoding="utf-8", errors="replace")
    summary = re.findall(r"(\d+)\s+Warning;\s+(\d+)\s+Severe", text)
    warnings = int(summary[-1][0]) if summary else text.count("** Warning **")
    messages = [match.strip() for match in re.findall(r"\*\* Warning \*\*\s*(.+)", text)]
    counts: dict[str, int] = {}
    for message in messages:
        category = re.split(r"[:=]", message, maxsplit=1)[0].strip()[:120] or "EnergyPlus Warning"
        counts[category] = counts.get(category, 0) + 1
    return {
        "warnings": warnings,
        "severes": text.count("**  Severe  **"),
        "fatals": text.count("**  Fatal  **"),
        "categories": [{"category": key, "count": value} for key, value in sorted(counts.items())],
        "messages": messages,
    }


def _invalid_result(settings: dict[str, Any], evidence: dict[str, Any], error: Exception) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "settings": _public_settings(settings),
        "raw_energy": None,
        "normalized_energy": None,
        "qa": {
            "scientific_status": "INVALID",
            "all_pass": False,
            "checks": [],
            "error_summary": str(error),
            "warning_summary": evidence,
        },
        "warnings": evidence,
        "carbon": None,
        "cadastre_heating": settings.get("cadastre_heating", {}),
        "provenance": settings["provenance"],
    }


def _public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in settings.items() if not key.startswith("_")}


def _write_result_artifacts(run_dir: Path, result: dict[str, Any]) -> None:
    _write_json(run_dir / "results.json", result)
    _write_json(run_dir / "raw_energy.json", result.get("raw_energy"))
    _write_json(run_dir / "qa.json", result["qa"])
    _write_json(run_dir / "warnings.json", result["warnings"])
    _write_json(run_dir / "simulation_settings.json", result["settings"])
    metadata = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "openstudio": openstudio.openStudioVersion(),
        "provenance": result["provenance"],
    }
    _write_json(run_dir / "simulation_metadata.json", metadata)
    normalized = result.get("normalized_energy") or {}
    carbon = result.get("carbon") or {}
    row = {
        "scientific_status": result["qa"]["scientific_status"],
        "heating_kwh": normalized.get("heating_kwh"),
        "cooling_kwh": normalized.get("cooling_kwh"),
        "heating_kwh_m2": normalized.get("heating_kwh_m2"),
        "cooling_kwh_m2": normalized.get("cooling_kwh_m2"),
        "area_m2": result["settings"].get("conditioned_residential_area_m2"),
        **carbon,
    }
    with (run_dir / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def run_simulation(osm_path: Path, run_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Simulate one exact parent OSM through the real Part B functions."""
    osm_path = Path(osm_path)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    runner = importlib.import_module("run_simulation")
    output_variables = settings["energy_output_variables"]
    if set(REQUIRED_ENERGY_KEYS) - set(output_variables):
        raise ValueError("Simulation settings do not identify exact heating/cooling variables")

    model = _load_parent_model(osm_path)
    original_epw = runner.mb.EPW_FILE
    original_outputs = dict(runner.mb.OUTPUT_VARIABLES)
    runner.mb.EPW_FILE = Path(settings["_weather_path"])
    runner.mb.OUTPUT_VARIABLES = dict(output_variables)
    try:
        _progress(settings, "EnergyPlus", 0.26)
        try:
            sql_path = runner.run_energyplus(model, run_dir)
            evidence = _err_evidence(run_dir)
            err_stats = runner.scan_err_file(run_dir)
        except Exception as exc:
            evidence = _err_evidence(run_dir)
            if (run_dir / "model.idf").exists() or (run_dir / "eplusout.err").exists():
                result = _invalid_result(settings, evidence, exc)
                _write_result_artifacts(run_dir, result)
                return result
            raise

        _progress(settings, "SQL Parse", 0.66)
        area_m2 = float(settings["conditioned_residential_area_m2"])
        if area_m2 <= 0:
            raise ValueError("Conditioned residential area must be positive")
        detailed_hvac = settings.get("energy_basis") == "detailed_hvac_consumption"
        consumption = runner.read_end_uses(sql_path, area_m2) if detailed_hvac else None
        if detailed_hvac:
            normalized, raw = _detailed_hvac_energy(consumption, area_m2)
        else:
            normalized = runner.read_results(sql_path, area_m2)
            raw = _raw_energy(sql_path, output_variables, area_m2)

        _progress(settings, "QA", 0.82)
        if detailed_hvac:
            checks = runner.crosscheck_energyplus(sql_path, settings["_parent_stats"], unmet_max=300.0)
            checks += runner.check_plausibility_cons(consumption)
        else:
            checks = runner.crosscheck_energyplus(sql_path, settings["_parent_stats"])
            checks += runner.check_plausibility(normalized)
        qa_all_pass = runner.write_qa_report(
            run_dir, settings["_parent_stats"], consumption if detailed_hvac else normalized, err_stats, checks,
        )
        unmet = next((float(item.get("eplus") or 0.0) for item in checks
                      if item.get("check") == "unmet_hours"), 0.0)
        plausibility_failed = any(
            not item.get("passed") and str(item.get("check", "")).startswith("plausible_band")
            for item in checks
        )
        if plausibility_failed or unmet > 300:
            scientific_status = "INVALID"
        elif not qa_all_pass:
            scientific_status = "UNVERIFIED"
        else:
            scientific_status = "VALIDATED"
        carbon = None if scientific_status == "INVALID" else (
            runner.carbon_from_enduses(consumption, area_m2) if detailed_hvac
            else runner.carbon_footprint(normalized, area_m2)
        )
        authored_carbon = settings.get("carbon_settings") or {}
        if carbon is not None and authored_carbon and not detailed_hvac:
            # Project Parameters expose an additional authored demand-to-carbon
            # lens without mutating Part B's frozen S1/S2 domain function.
            cop = float(authored_carbon.get("cop", 2.5))
            seer = float(authored_carbon.get("seer", 2.5))
            emission_factor = float(authored_carbon.get("emission_factor", 0.331))
            consumption = normalized["heating_kwh_m2"] / cop + normalized["cooling_kwh_m2"] / seer
            co2_kg_m2 = consumption * emission_factor
            carbon.update({
                "authored_scenario": "Project Parameters",
                "authored_cop": cop,
                "authored_seer": seer,
                "authored_emission_factor_kg_kwh": emission_factor,
                "authored_consumption_kwh_m2": round(consumption, 2),
                "authored_co2_kg_m2": round(co2_kg_m2, 2),
                "authored_co2_t_yr": round(co2_kg_m2 * area_m2 / 1000.0, 1),
            })
        result = {
            "schema_version": 1,
            "settings": _public_settings(settings),
            "raw_energy": raw,
            "normalized_energy": None if scientific_status == "INVALID" else normalized,
            "consumption": None if scientific_status == "INVALID" else consumption,
            "qa": {
                "scientific_status": scientific_status,
                "all_pass": qa_all_pass,
                "checks": checks,
                "unmet_hours": unmet,
                "error_summary": None,
                "warning_summary": evidence,
            },
            "warnings": evidence,
            "carbon": carbon,
            "cadastre_heating": settings.get("cadastre_heating", {}),
            "provenance": settings["provenance"],
        }
        _write_result_artifacts(run_dir, result)
        _progress(settings, "Finalize", 0.92)
        return result
    finally:
        runner.mb.EPW_FILE = original_epw
        runner.mb.OUTPUT_VARIABLES = original_outputs
