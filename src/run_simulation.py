"""EnergyPlus simulation runner for the Valencia building-energy pipeline (Part B).

Chain (3D model construction is NOT here - it lives in model_builder.py):
    model_builder.py (mb) OpenStudio model"
    -> EnergyPlus annual simulation (pyenergy API; E+ bundled with OpenStudio)
    -> Annual heating/cooling demand read from eplusout.sql
    (kWh/m2*yr)
    ->QA layer: error-file scan (Severe = 0 required), model <-> E+ cross-checks,
        plausibility band, qa_report.txt + run_metadata.json
    (reproducibility)
    -> operational carbon scenarios (demand -> consumption -> kgB02/m2*yr)
    -> result.csv
Usage:
    cd ~/valencia-energy-sim
    .venv/bin/python src/run_simulation.py [--gpkg PATH] [--out-dir PATH] [-v | --quiet]

Exit codes:
    0  all buildings simulated and every QA check passed
    1  simulation finished but at least one QA check failed (results still written)
    2  hard failure (missing input, EnergyPlus Severe/Fatal error, ...)"""


import argparse
import hashlib
import json
import logging
import platform
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import openstudio
import pandas as pd

import model_builder as mb      #geometry + shared CONFIG (paths, params, output vars)

logger = logging.getLogger("run_simulation")

# =======================================================================
# CONFIG -simulation / QA / carbon constants only. Geometry constants and 
# input paths live in the builder (mb.BUILDINGS_GPKG, mb.EPW_FILE, ...).
# =======================================================================

EPLUS_DIR = "/Applications/OpenStudio-3.11.0/EnergyPlus"    # E+ 25.2.0 bundled with OpenStudio

#QA thresholds (model <-> EnergyPlus cross-check tolerances):
QA_AREA_TOL = 0.005    #conditioned floor area: model vs E+ <= 0.5 %
QA_GLAZING_TOL = 0.02   # glazing area: model vs E+ <= 2 % expected; 50 h suspicious
QA_UNMET_HOURS_MAX = 50    # unmet hours ~0 expected with ideal loads; above 50 h suspicious
QA_RESULT_BAND = (1.0, 150.0)    # plausibility band, kWh/m2*yr (Mediterranean housing)
QA_FACADE_WWR_WARN_PCT = 15.0    # facade WWR deviation above this is flagged in the report

# Real-HVAC consumption run thresholds (Part F2):
QA_CONS_BAND = (1.0, 200.0)      # total site energy plausibility band, kWh/m2*yr
QA_UNMET_HOURS_MAX_HVAC = 500    # real systems cycle: some unmet hours are normal
GJ_TO_KWH = 1000.0 / 3.6         # E+ End Uses table reports GJ


# Cadastre reference columns. Rai confirmed demanda_ca as external-source
# heating demand. demanda__1 remains unconfirmed; internal evidence only
# suggests post-intervention heating, so it must never be labelled as cooling.
COL_HEAT_DEMAND = "demanda_ca"
COL_UNCONFIRMED_DEMAND = "demanda__1"

#--- CARBON LAYER -----------------------------------------------------------
# Chain: DEMAND (simulation, kWh/m2) -> CONSUMPTİON (demand / COP or SEER) -> 
#        CO2 (consumption x emission factor, kgCO2/kWh).
# SCOPE: space heating + cooling only (the simulated). DHW, lighting and 
# appliances are EXCLUDED - stated as such in every report. Embodied carbon 
# (LCA) is outside this layer. 

# Emission factors: Official Spanish building-certification values
# (IDAE/MITECO 2016, the CTE/RITE recognised document; CERMA uses the same) - 
# concistent basis with the cadastre 'calificaci' letters. The real 2024 grid
# is cleaner (~0.15-0.20); that gap is an LHS uncertainty-variable candidate.
EMISSION_FACTORS = {"electricity": 0.331, #electricity, peninsular Spain
                    "natural_gas": 0.252, # natural gas (unused in the scenarios; kept for variants)
                     }
# System scenarios — 1974 building, no measured system data; two bounding cases:
SYSTEM_SCENARIOS = {"S1 electric resistance + old split ": {"heating_cop": 1.0, #portable electric radiator (common in Valencia)
                                                            "cooling_seer": 2.0, # old split air conditioner
                                                            "fuel":"electricity",
                                                            },
                                                            "S2 reversible split (heat pump)": {"heating_cop": 2.5, # modern split, seasonal COP
                                                                                                "cooling_seer": 2.5,
                                                                                                "fuel": "electricity",
                                                                                                },
                                                                                                }
# ============================================================================
# 5) ENERGYPLUS RUN
# ============================================================================

def run_energyplus(osm: openstudio.model.Model, run_dir: Path,
                   epw_path: Path | None = None) -> Path:
    """Translate the OpenStudio model to IDF and run EnergyPlus.

Return the path to eplusout.sql. Notes versus a classic E+ workflow:
1. No ExpandObjects step: OpenStudio writes
    ZoneHVAC:Ideal Loads Air System directly (not HVAC Template).
2. No seperate E+ install: the copy bundled with OpenStudio 3.11.0
is used.
3. No'---readvars' (CSV writer): the bundled distribution has no ReadVarsESO tool; results are read from eplusout.sql instead (more robust anyway).

`epw_path` selects the weather file for this run; unset means the project
default, which is what every result before 2026-07-30 was produced with.

It has to be an argument. EnergyPlus's `--weather` overrides whatever weather
file the model carries, so until now `mb.set_weather_file()` could rewrite the
model while the simulation still ran the default 8 760 hours - a climate
scenario that changed nothing and said nothing. Measured: an EPW with every
hour +5 K and the city renamed gave a byte-identical result, with eplusout.eio
naming VALENCIA and the OSM naming TESTCITY."""

    run_dir.mkdir(parents=True, exist_ok=True)

    # Keep the OSM next to the run outputs (openable in OpenStudio Application).
    osm.save(openstudio.toPath(str(run_dir / "model_python.osm")), True)

    ft = openstudio.energyplus.ForwardTranslator()
    workspace = ft.translateModel(osm)
    idf_path = run_dir / "model.idf"
    workspace.save(openstudio.toPath(str(idf_path)), True)

    if not Path(EPLUS_DIR).exists():
        raise FileNotFoundError(f"EnergyPlus directory not found: {EPLUS_DIR}")
    #ENERGYPLUS is imported from the E+ install directory (NOT a pip package).
    if EPLUS_DIR not in sys.path:
        sys.path.insert(0,EPLUS_DIR)
    from pyenergyplus.api import EnergyPlusAPI
    weather = Path(epw_path) if epw_path is not None else Path(mb.EPW_FILE)
    if not weather.exists():
        raise FileNotFoundError(f"Weather file not found: {weather}")
    api = EnergyPlusAPI()
    state = api.state_manager.new_state()    # fresh state per run (required for N buildings)
    exit_code = api.runtime.run_energyplus(state, ["--weather", str(weather),
                                                   "--output-directory", str(run_dir),
                                                   str(idf_path),
                                                   ])
    api.state_manager.delete_state(state)
    if exit_code != 0:
        raise RuntimeError(
            f"EnergyPlus failed (exit={exit_code}) - see {run_dir}/eplusout.err")
    return run_dir / "eplusout.sql"


# ============================================================================
# 6) RESULT READING — E+ output -> pandas -> kWh/m2
# ============================================================================

def read_results(sql_path: Path, res_area_m2: float) -> dict:
    """Read the annual heating/cooling totals from eplusout.sql, in kWh/m2.

    E+ SQL schema: ReportDataDictionary is the catalogue (which variable, which
    zone, which frequency); ReportData holds the values. They are joined and
    filtered to 'Run Period' (annual totals) rows of the two OutputVariables
    defined by mb.build_model().

    The filter is an EXACT-name IN match (mb.OUTPUT_VARIABLES): a loose filter
    like contains('Heating') could silently sum a future extra variable too
    (double-counting protection).

    Values are Joules; kWh = J / 3.6e6. The m2 basis is the RESIDENTIAL area
    (unconditioned commercial ground floor excluded — the cadastre certificate
    values use the same basis).
    """
    con = sqlite3.connect(sql_path)
    df = pd.read_sql("""
                    SELECT d.Name AS variable, d.KeyValue AS zone, r.Value AS joule 
                    FROM ReportData r
                    JOIN ReportDataDictionary d
                    ON r.ReportDataDictionaryIndex = d.ReportDataDictionaryIndex
                    WHERE d.ReportingFrequency = 'Run Period'
                    AND d.Name IN (?, ?)
                    """, con, params=(mb.OUTPUT_VARIABLES["heating"], mb.OUTPUT_VARIABLES["cooling"]))
    con.close()
    if df.empty:
        raise RuntimeError(f"No annual (Run Period) data in SQL output: {sql_path}")
    heat_j = df.loc[df["variable"] == mb.OUTPUT_VARIABLES["heating"], "joule"].sum()
    cool_j = df.loc[df["variable"] == mb.OUTPUT_VARIABLES["cooling"], "joule"].sum()
    heat_kwh = float(heat_j) / 3.6e6
    cool_kwh = float(cool_j) / 3.6e6
    return {
        "heating_kwh": round(heat_kwh, 1),
        "cooling_kwh": round(cool_kwh, 1),
        "heating_kwh_m2": round(heat_kwh / res_area_m2, 2),
        "cooling_kwh_m2": round(cool_kwh / res_area_m2, 2),
    }

def read_end_uses(sql_path: Path, res_area_m2: float) -> dict:
    """Read the annual End Uses table (real-HVAC consumption run), in kWh/m2.

    Source: TabularDataWithStrings, AnnualBuildingUtilityPerformanceSummary ->
    'End Uses' (values in GJ). Same residential-area basis as read_results().
    Fuel split is kept because the carbon factors differ (gas 0.252 vs
    electricity 0.331 kgCO2/kWh). cons_hc = heating + cooling + fans (the HVAC
    consumption block, space conditioning only; our models have no DHW plant,
    so the Heating row is pure space heating)."""
    con = sqlite3.connect(sql_path)

    def gj(row_name: str, col_name: str) -> float:
        v = _tabular_value(con, "AnnualBuildingUtilityPerformanceSummary",
                           "End Uses", row_name, col_name)
        return (v or 0.0) * GJ_TO_KWH

    heat_gas = gj("Heating", "Natural Gas")
    heat_elec = gj("Heating", "Electricity")
    cool_elec = gj("Cooling", "Electricity")
    fans_elec = gj("Fans", "Electricity")
    lighting = gj("Interior Lighting", "Electricity")
    equipment = gj("Interior Equipment", "Electricity")
    site_elec = gj("Total End Uses", "Electricity")
    site_gas = gj("Total End Uses", "Natural Gas")
    con.close()

    total_site = site_elec + site_gas
    if total_site <= 0:
        raise RuntimeError(f"End Uses table empty or zero in SQL output: {sql_path}")
    hc = heat_gas + heat_elec + cool_elec + fans_elec
    a = res_area_m2
    return {
        "cons_heating_kwh": round(heat_gas + heat_elec, 1),
        "cons_cooling_kwh": round(cool_elec, 1),
        "total_site_kwh": round(total_site, 1),
        "cons_heating_kwh_m2": round((heat_gas + heat_elec) / a, 2),
        "cons_heating_gas_kwh_m2": round(heat_gas / a, 2),
        "cons_heating_elec_kwh_m2": round(heat_elec / a, 2),
        "cons_cooling_kwh_m2": round(cool_elec / a, 2),
        "cons_fans_kwh_m2": round(fans_elec / a, 2),
        "cons_hc_kwh_m2": round(hc / a, 2),
        "lighting_kwh_m2": round(lighting / a, 2),
        "equipment_kwh_m2": round(equipment / a, 2),
        "site_elec_kwh_m2": round(site_elec / a, 2),
        "site_gas_kwh_m2": round(site_gas / a, 2),
        "total_site_kwh_m2": round(total_site / a, 2),
    }


def carbon_from_enduses(cons: dict, res_area_m2: float) -> dict:
    """Fuel-split operational carbon for the real-HVAC consumption run.

    No scenario bracket here: the system (gas burner + split DX) is IN the
    model, so there is exactly one consumption -> one CO2 line. The S1/S2
    bracket keeps living on the demand run (carbon_footprint)."""
    ef_e = EMISSION_FACTORS["electricity"]
    ef_g = EMISSION_FACTORS["natural_gas"]
    hvac_co2 = (cons["cons_heating_gas_kwh_m2"] * ef_g
                + (cons["cons_heating_elec_kwh_m2"] + cons["cons_cooling_kwh_m2"]
                   + cons["cons_fans_kwh_m2"]) * ef_e)
    total_co2 = cons["site_gas_kwh_m2"] * ef_g + cons["site_elec_kwh_m2"] * ef_e
    return {
        "hvac_co2_kg_m2": round(hvac_co2, 2),
        "hvac_co2_t_yr": round(hvac_co2 * res_area_m2 / 1000.0, 1),
        "total_site_co2_kg_m2": round(total_co2, 2),
        "total_site_co2_t_yr": round(total_co2 * res_area_m2 / 1000.0, 1),
    }


def check_plausibility_cons(cons: dict) -> list[dict]:
    """Plausibility band for the consumption run (total site energy)."""
    lo, hi = QA_CONS_BAND
    v = cons["total_site_kwh_m2"]
    return [{"check": "plausible_band_total_site_kwh_m2", "model": v,
             "eplus": f"[{lo}-{hi}]", "tolerance": "-", "passed": lo <= v <= hi}]


# ============================================================================
# 6.5) QA / VERIFICATION LAYER — the evidence behind any "validated" claim
# ============================================================================
# Principle: before trusting a result, check (1) the E+ error file is clean,
# (2) the building E+ saw equals the building built from GIS (cross-check),
# (3) the numbers are physically plausible. All three are automated and
# archived per building (qa_report.txt + run_metadata.json).   

def scan_err_file(run_dir: Path) -> dict:
    """Scan eplusout.err; raise if any Severe/Fatal (run is INVALID then).

    E+ can return exit code 0 and still emit Severe errors — the exit code
    alone is not sufficient. Returns {'warnings': n, 'severes': n}.
    """
    err_path = run_dir / "eplusout.err"
    if not err_path.exists():
        raise RuntimeError(f"eplusout.err missing in {run_dir} - run may not have started.")
    text = err_path.read_text(errors="replace")
    # E+ 25.2 writes "** Severe  **" with ONE space after the asterisks; an
    # earlier literal with two spaces matched nothing, so this guard never
    # fired. Match the label whitespace-insensitively so a formatting change
    # in a future E+ release cannot silently disable it again.
    n_severe = len(re.findall(r"\*\*\s*Severe\s*\*\*", text))
    n_fatal = len(re.findall(r"\*\*\s*Fatal\s*\*\*", text))
    #CAREFUL = the err file has several summary lines (Warmup/Sizing/final);
    # the TRUE total is on the LAST one ('Completed Successfully-- N Warning;').
    #Taking the first match would read Warmup's zero (this happened once).
    matches = re.findall(r"(\d+)\s+Warning;\s+(\d+)\s+Severe", text)
    n_warning = int(matches[-1][0]) if matches else text.count("** Warning **")
    if n_severe or n_fatal:
        raise RuntimeError(f"EnergyPlus produced {n_severe} Severe / {n_fatal} Fatal"
                           f"errors - results invalid. See {err_path}")
    return {"warnings": n_warning, "severes": n_severe}

def _tabular_value(con: sqlite3.Connection, report: str, table: str, row: str, column: str) -> float | None:
    """Fetch one value from the E+ TabularDataWithStrings report (None if absent)."""
    cur = con.execute("""SELECT Value
                      FROM TabularDataWithStrings
                      WHERE ReportName=? AND TableName=? AND RowName=? AND ColumnName=?""", (report, table, row, column))
    r = cur.fetchone()
    try:
        return float(r[0]) if r else None
    except (TypeError, ValueError):
        return None

def crosscheck_energyplus(sql_path: Path, stats: dict,
                          unmet_max: float | None = None) -> list[dict]:
    """Cross-check E+ tabular report against the GIS-derived model stats.

    Catches model-construction errors (wrong area, lost windows, missing
    zones) WITHOUT looking at the energy results. Returns a list of
    {check, model, eplus, tolerance, passed} dicts.
    """
    con = sqlite3.connect(sql_path)
    checks: list[dict] = []
    
    def add(name: str, model_val: float, eplus_val: float | None, tol_rel: float):
        if eplus_val is None:
            checks.append({"check": name, "model": model_val, "eplus": None, "tolerance": tol_rel, "passed": False })
            return
        ok = abs(eplus_val - model_val) <= tol_rel * max(abs(model_val), 1e-9)
        checks.append({"check": name, "model": round(model_val, 2), "eplus": round(eplus_val, 2), "tolerance": tol_rel, "passed": ok})
    
    # 1) Conditioned floor area: residential area (GIS footprint x floors) vs E+
    add("conditioned_area_m2", stats["res_area_m2"], _tabular_value(con, "AnnualBuildingUtilityPerformanceSummary", "Building Area", "Net Conditioned Building Area", "Area"), QA_AREA_TOL)

    # 2) Glazing area: placed openings vs E+ fenestration summary
    add("glazing_area_m2", stats["window_area_m2"], _tabular_value(con, "EnvelopeSummary", "Exterior Fenestration", "Total or Average", "Area of Multiplied Openings"), QA_GLAZING_TOL)
    
    # 3) Zone count must equal the number of storeys
    n_zones = con.execute("SELECT COUNT(*) FROM Zones").fetchone()[0]
    checks.append({"check": "zone_count", "model": stats["n_floors_total"],
                   "eplus": n_zones, "tolerance": 0,
                   "passed": n_zones == stats["n_floors_total"]})
    
    # 4) Unmet hours: ideal loads have unlimited capacity -> ~0 expected;
    #    otherwise the thermostat/schedule setup is broken. Real HVAC cycles,
    #    so the consumption run passes a looser limit via unmet_max.
    limit = QA_UNMET_HOURS_MAX if unmet_max is None else unmet_max
    unmet = 0.0
    for col in ("During Occupied Heating", "During Occupied Cooling"):
        v = _tabular_value(con, "SystemSummary", "Time Setpoint Not Met", "Facility", col)
        unmet += v or 0.0
    checks.append({"check": "unmet_hours", "model": 0.0, "eplus": round(unmet, 1),
                   "tolerance": limit, "passed": unmet <= limit})
    con.close()
    return checks

def check_plausibility(res: dict) -> list[dict]:
    """Plausibility band: outside 1-150 kWh/m2*yr for Mediterranean housing
    means a gross model error (unit mix-up, wrong area basis, ...)."""
    lo, hi = QA_RESULT_BAND
    out = []
    for k in ("heating_kwh_m2", "cooling_kwh_m2"):
        v = res[k]
        out.append({"check": f"plausible_band_{k}", "model": v,
                    "eplus": f"[{lo}-{hi}]", "tolerance": "-", "passed": lo <= v <= hi})
    return out


def _sha1(path: Path) -> str:
    """Input-file fingerprint (reproducibility: WHICH data produced the result)."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _eplus_version(run_dir: Path) -> str:
    """Read the E+ version from the first line of eplusout.err ('Program Version,...')."""
    try:
        first = (run_dir / "eplusout.err").read_text(errors="replace").splitlines()[0]
        m = re.search(r"Version ([\d.]+)", first)
        return m.group(1) if m else first.strip()
    except Exception:
        return "?"


def write_run_metadata(
    run_dir: Path, stats: dict, res: dict, params: dict | None, *,
    source_gis_path: Path | None = None, neighbors_path: Path | None = None,
) -> None:
    """run_metadata.json: which inputs + versions + parameters produced this
    run — the minimum record for academic reproducibility."""
    meta = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "command": sys.argv,
        "python": sys.version.split()[0],
        "openstudio": openstudio.openStudioVersion(),
        "energyplus": _eplus_version(run_dir),
        "packages": {"geopandas": gpd.__version__, "pandas": pd.__version__},
        "inputs_sha1": {
            "buildings_gpkg": _sha1(Path(source_gis_path or mb.BUILDINGS_GPKG)),
            "template_osm": _sha1(mb.TEMPLATE_OSM),
            "epw": _sha1(mb.EPW_FILE),
            "neighbors_shp": _sha1(Path(neighbors_path or mb.NEIGHBORS_SHP)),
        },
        "params": {k: v for k, v in (params or mb.DEFAULT_PARAMS).items()},
        "stats": {k: v for k, v in stats.items() if k != "facade_qa"},
        "results": res,
    }
    with open(run_dir / "run_metadata.json", "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


def write_qa_report(run_dir: Path, stats: dict, res: dict,
                    err_stats: dict, checks: list[dict]) -> bool:
    """Human-readable QA report (qa_report.txt). Returns True if all checks passed."""
    lines = [f"QA REPORT — {stats['refparcela']}",
             f"({datetime.now().isoformat(timespec='seconds')})", ""]
    lines.append(f"E+ error file: {err_stats['warnings']} Warning, "
                 f"{err_stats['severes']} Severe (Severe=0 required, satisfied)")
    lines.append("")
    lines.append("Cross-checks (model <-> EnergyPlus):")
    all_ok = True
    for c in checks:
        mark = "OK " if c["passed"] else "FAIL"
        all_ok &= c["passed"]
        lines.append(f"  [{mark}] {c['check']:28s} model={c['model']} "
                     f"eplus={c['eplus']} tol={c['tolerance']}")
    lines.append("")
    lines.append(f"Facade openings (target vs achieved WWR; deviation > "
                 f"{QA_FACADE_WWR_WARN_PCT:.0f}% flagged):")
    for f in stats.get("facade_qa", []):
        flag = "  <-- CHECK" if abs(f["lapse_pct"]) > QA_FACADE_WWR_WARN_PCT else ""
        lines.append(f"  azimuth {f['azimut']:6.1f}°: target {f['wwr_target']:.3f} -> "
                     f"achieved {f['wwr_real']:.3f} "
                     f"({f['glass_m2']} / {f['wall_m2']} m²; {f['window']} windows + "
                     f"{f['door']} balcony doors; deviation {f['lapse_pct']:+.1f}%){flag}")
    lines.append("")
    if "total_site_kwh_m2" in res:
        lines.append(f"Results (consumption): HVAC {res['cons_hc_kwh_m2']} / "
                     f"total site {res['total_site_kwh_m2']} kWh/m²·yr")
    else:
        lines.append(f"Results: heating {res['heating_kwh_m2']} / cooling "
                     f"{res['cooling_kwh_m2']} kWh/m²·yr")
    lines.append(f"OVERALL: {'ALL CHECKS PASSED' if all_ok else 'AT LEAST ONE CHECK FAILED'}")
    (run_dir / "qa_report.txt").write_text("\n".join(lines) + "\n")
    return all_ok

# ============================================================================
# 7) OPERATIONAL CARBON — demand -> consumption -> CO2
# ============================================================================

def carbon_footprint(res: dict, res_area_m2: float) -> dict:
    """Convert the simulated DEMAND to carbon per system scenario.

    Three arithmetic steps per scenario (no physics — the model is NOT re-run):
      1. consumption = heating demand / COP + cooling demand / SEER  [kWh/m2*yr]
         (COP/SEER: how many kWh of heat/cold one kWh of electricity moves;
          resistance heater COP=1, heat pump > 1 -> lower consumption)
      2. CO2 = consumption x emission factor                         [kgCO2/m2*yr]
      3. building total = CO2 x residential area / 1000              [tCO2/yr]

    N-building note: the function is pure (dict in, dict out) -> called per
    building in a loop; neighbourhood total = sum(kgCO2/m2 x area). LHS link:
    COP/SEER and the emission factor become uncertainty variables (Ashby L4).
    """
    out = {}
    for i, (name, sc) in enumerate(SYSTEM_SCENARIOS.items(), start=1):
        ef = EMISSION_FACTORS[sc["fuel"]]
        cons_kwh_m2 = (res["heating_kwh_m2"] / sc["heating_cop"]
                       + res["cooling_kwh_m2"] / sc["cooling_seer"])
        co2_kg_m2 = cons_kwh_m2 * ef
        co2_t_building = co2_kg_m2 * res_area_m2 / 1000.0
        prefix = f"s{i}"
        out[f"{prefix}_scenario"] = name
        out[f"{prefix}_consumption_kwh_m2"] = round(cons_kwh_m2, 2)
        out[f"{prefix}_co2_kg_m2"] = round(co2_kg_m2, 2)
        out[f"{prefix}_co2_t_yr"] = round(co2_t_building, 1)
    return out

# ============================================================================
# 8) MAIN — CLI + per-building chain (v1: pilot; the loop is N-ready)
# ============================================================================

def simulate_building(row, out_dir: Path, params: dict | None = None,
                      label: str | None = None, hvac: bool = False,
                      heat_delta: float = 0.0, cool_delta: float = 0.0,
                      epw: Path | None = None, neighbors_path: Path | None = None,
                      source_gis_path: Path | None = None) -> tuple[dict, bool]:
    """Full chain for ONE building row: model -> E+ -> results -> QA -> carbon.

    Returns (record for results.csv, qa_all_passed). Kept as a standalone
    function so the neighbourhood/city pipelines can reuse it directly:
    `params` overrides the builder defaults (period/typology envelope values —
    Part C) and `label` names the output sub-folder (default: refparcela).

    hvac=False (default): ideal-loads DEMAND run — behaviour unchanged.
    hvac=True (Part F2): the same model is converted with mb.add_real_hvac()
    and E+ reports CONSUMPTION (End Uses; gas heating + DX cooling + lighting
    + equipment). Output folder gets an '_hvac' suffix.

    Scenario knobs (Part G, all default = off): heat_delta / cool_delta shift
    the CTE thermostat setpoints (K); epw swaps the weather file (climate run).
    """
    ref = row["refparcela"]
    logger.info("===== BUILDING: %s%s =====", ref, " [HVAC]" if hvac else "")

    geom = mb.clean_polygon(row.geometry)
    resolved_neighbors_path = Path(neighbors_path or mb.NEIGHBORS_SHP)
    neighbors = mb.load_neighbors(geom, ref, resolved_neighbors_path)   # one read: party + shading
    party = mb.find_party_walls(geom, ref, resolved_neighbors_path, neighbors=neighbors)

    osm, stats = mb.build_model(row, party, params=params, neighbors=neighbors)
    logger.info("[model] footprint %s m² × %s storeys (%s residential) | party surfaces: %s "
                "| %s windows + %s balcony doors = %s m² glass | %s shading surfaces",
                stats["footprint_m2"], stats["n_floors_total"], stats["n_floors_residential"],
                stats["n_party_surfaces"], stats["n_windows"], stats["n_balcony_doors"],
                stats["window_area_m2"], stats["n_shading_surfaces"])

    scenario_info = {}
    if heat_delta or cool_delta:
        scenario_info.update(mb.apply_comfort_offsets(osm, heat_delta, cool_delta))
        logger.info("[scenario] comfort offsets applied: %s", scenario_info)
    if epw is not None:
        scenario_info.update(mb.set_weather_file(osm, epw))
        logger.info("[scenario] weather file replaced: %s", epw)

    hvac_info = None
    if hvac:
        hvac_info = mb.add_real_hvac(osm)
        logger.info("[hvac] real system installed: %s", hvac_info)

    run_dir = out_dir / ((label or str(ref)) + ("_hvac" if hvac else ""))
    if run_dir.exists():
        shutil.rmtree(run_dir)                    # clean any previous run
    # the scenario EPW has to reach EnergyPlus itself, not only the model
    sql_path = run_energyplus(osm, run_dir, epw_path=epw)

    err_stats = scan_err_file(run_dir)            # raises on Severe/Fatal

    if hvac:
        res = read_end_uses(sql_path, stats["res_area_m2"])
        logger.info("[result] consumption: HVAC %s kWh/m² | total site %s kWh/m²",
                    res["cons_hc_kwh_m2"], res["total_site_kwh_m2"])
        checks = (crosscheck_energyplus(sql_path, stats,
                                        unmet_max=QA_UNMET_HOURS_MAX_HVAC)
                  + check_plausibility_cons(res))
        carbon = carbon_from_enduses(res, stats["res_area_m2"])
        logger.info("[carbon] real system: HVAC %s kgCO₂/m² | total site %s kgCO₂/m²",
                    carbon["hvac_co2_kg_m2"], carbon["total_site_co2_kg_m2"])
    else:
        res = read_results(sql_path, stats["res_area_m2"])
        logger.info("[result] heating %s kWh/m² | cooling %s kWh/m²",
                    res["heating_kwh_m2"], res["cooling_kwh_m2"])
        checks = crosscheck_energyplus(sql_path, stats) + check_plausibility(res)
        carbon = carbon_footprint(res, stats["res_area_m2"])
        for i in range(1, len(SYSTEM_SCENARIOS) + 1):
            logger.info("[carbon] %s: consumption %s kWh/m² -> %s kgCO₂/m²·yr -> "
                        "building total %s tCO₂/yr",
                        carbon[f"s{i}_scenario"], carbon[f"s{i}_consumption_kwh_m2"],
                        carbon[f"s{i}_co2_kg_m2"], carbon[f"s{i}_co2_t_yr"])

    qa_ok = write_qa_report(run_dir, stats, res, err_stats, checks)
    write_run_metadata(
        run_dir, stats, res, params=params,
        source_gis_path=source_gis_path, neighbors_path=resolved_neighbors_path,
    )
    if qa_ok:
        logger.info("[QA] ALL CHECKS PASSED (%s E+ warnings) -> %s",
                    err_stats["warnings"], run_dir / "qa_report.txt")
    else:
        logger.warning("[QA] CHECK FAILED — see %s", run_dir / "qa_report.txt")

    # Record for results.csv (+ validation references where available). The
    # unconfirmed companion is retained as evidence, not interpreted as cooling.
    rec = {k: v for k, v in stats.items() if k != "facade_qa"}   # lists don't go to CSV
    rec.update({"eplus_warnings": err_stats["warnings"], "qa_all_pass": qa_ok})
    rec.update({**res, **carbon})
    if hvac_info is not None:
        rec.update(hvac_info)
    if scenario_info:
        rec.update(scenario_info)
    if not hvac:
        if COL_HEAT_DEMAND in row.index and pd.notna(row[COL_HEAT_DEMAND]):
            rec["cadastre_heat_kwh_m2"] = float(row[COL_HEAT_DEMAND])
            if rec["cadastre_heat_kwh_m2"] > 0:  # 0 = missing certificate, not a real value
                rec["heat_model_over_cadastre"] = round(
                    res["heating_kwh_m2"] / rec["cadastre_heat_kwh_m2"], 2)
        if (COL_UNCONFIRMED_DEMAND in row.index
                and pd.notna(row[COL_UNCONFIRMED_DEMAND])):
            rec["cadastre_unconfirmed_kwh_m2"] = float(
                row[COL_UNCONFIRMED_DEMAND])
    return rec, qa_ok


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run the EnergyPlus simulation chain (model -> E+ -> QA -> carbon).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--gpkg", type=Path, default=Path(mb.BUILDINGS_GPKG),
                   help="input GeoPackage with the building rows to simulate")
    p.add_argument("--out-dir", type=Path, default=Path(mb.OUT_DIR),
                   help="output directory (one sub-folder per building + results.csv)")
    p.add_argument("--heat-delta", type=float, default=0.0, metavar="K",
                   help="shift heating setpoints by this many K (scenario run)")
    p.add_argument("--cool-delta", type=float, default=0.0, metavar="K",
                   help="shift cooling setpoints by this many K (scenario run)")
    p.add_argument("--epw", type=Path, default=None, metavar="FILE",
                   help="alternative weather file (climate-scenario run)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-v", "--verbose", action="store_true", help="debug-level output")
    g.add_argument("--quiet", action="store_true", help="warnings and errors only")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    t0 = datetime.now()
    try:
        mb.validate_input_files()                 # fail fast on missing inputs
        if not args.gpkg.exists():
            raise FileNotFoundError(f"Input GeoPackage not found: {args.gpkg}")
        buildings = mb.load_buildings(args.gpkg)

        scenario_on = args.heat_delta or args.cool_delta or args.epw is not None
        if scenario_on and args.out_dir == Path(mb.OUT_DIR):
            logger.warning("[scenario] scenario knobs are set but --out-dir is the "
                           "default: the official baseline will be OVERWRITTEN. "
                           "Consider --out-dir tmp/scenario_x.")

        rows, all_qa_ok = [], True
        for _, row in buildings.iterrows():
            rec, qa_ok = simulate_building(row, args.out_dir,
                                           heat_delta=args.heat_delta,
                                           cool_delta=args.cool_delta,
                                           epw=args.epw)
            rows.append(rec)
            all_qa_ok &= qa_ok

        results = pd.DataFrame(rows)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        out_csv = args.out_dir / "results.csv"
        results.to_csv(out_csv, index=False)

        logger.info("===== SUMMARY =====\n%s", results.to_string(index=False))
        logger.info("Results file: %s", out_csv)
        logger.info("Duration: %.0f s", (datetime.now() - t0).total_seconds())
        return 0 if all_qa_ok else 1
    except Exception as exc:
        logger.error("Run failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
