"""Neighbourhood-scale energy pipeline for Benicalap (Part C).

Representative-model method — this project does NOT simulate building by
building (AGENTS.md method; Sustainability 2021, doi:10.3390/su13094654
reports ~±9 % total error for the same typology/period approach):

    1. Load and clean the Benicalap building stock (959 buildings, 18 clusters).
    2. Select ONE representative building per cluster (closest to the cluster's
       area/floor medians, simple geometry preferred, pre-flight checked).
    3. Simulate each representative at its REAL location (its own neighbours ->
       party walls + 50 m context shading) with period/typology envelope
       parameters -> cluster kWh/m2*yr  (chain reused from run_simulation.py).
    4. Join back to GIS: building energy = cluster kWh/m2 x residential area
       (Tipo15 dwelling ledger) -> building carbon -> neighbourhood totals.
    5. Validation: per-building `demanda_ca` certificate column (definition
       pending Javier's answer) + neighbourhood totals + typology shares.

Period/typology U-values: TABULA Espana (IVE) typology brochure, "ESTADO
ORIGINAL" sheets — https://episcope.eu (ES_TABULA_TypologyBrochure_IVE.pdf).
If Javier's full database (`Valencia_2051.db`) arrives, ONLY the TABULA_ES
table below is updated (the EU DB export we have contains no U-values —
Pablo's 2025-11-06 e-mail: U-values live in the full DB with CS/UR/AR keys).

Usage:
    cd ~/valencia-energy-sim
    .venv/bin/python src/neighborhood_pipeline.py [--out-dir PATH]
        [--only CLUSTER [--only CLUSTER ...]] [-v | --quiet]

Exit codes:
    0  all representatives simulated and every QA check passed
    1  finished but at least one QA check failed (results still written)
    2  hard failure (missing input, EnergyPlus Severe/Fatal, ...)

Duration: ~10-15 min (18 sequential EnergyPlus runs).
"""

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd

# The two pilot modules are REUSED as-is (no rewriting):
#   mb  = 3D model builder (geometry, party walls, shading, windows, params)
#   sim = E+ run + result reading + QA + carbon (simulate_building chain)
import model_builder as mb
import run_simulation as sim
from stock_input_policy import (
    StockInputPolicy, ground_rule_for_representative, policy_fingerprint, prepare_stock,
)

logger = logging.getLogger("neighborhood_pipeline")

# ============================================================================
# CONFIG
# ============================================================================
BOUNDARY_GPKG = mb.PROJECT / "data/gis/benicalap_boundary.gpkg"
TIPO15_CSV = mb.PROJECT / "data/reference/Tipo15_soloV(in).csv"
DEFAULT_OUT_DIR = mb.OUT_DIR / "neighborhood"

# --- TABULA Espana (IVE) — ESTADO ORIGINAL U-values [W/m2K] ------------------
# Source: ES_TABULA_TypologyBrochure_IVE.pdf (episcope.eu), one sheet per
# typology/period: Fachada / Cubierta / Huecos. Roof: the 'Cubierta plana'
# value where the sheet offers one (flat roofs dominate Benicalap blocks and
# the model's roof is flat), otherwise the sheet's single roof value.
# Typology mapping (verified against the `tipologia_` column):
#   BlocPluri = 'Plurifamiliar >= 3 plantas' -> TABULA 'Bloque en altura'
#   EdiPluri  = 'Plurifamiliar < 3 plantas'  -> TABULA 'Edificio plurifamiliar'
#   VivUni    = 'Unifamiliar' -> TABULA 'Vivienda unifamiliar ADOSADA'
#               (Benicalap fabric is row housing, not detached — assumption,
#               justified in the experiment note).
# Period mapping (city `ano_cons_1` vs TABULA sheets; boundaries shifted by
# 1-4 years, accepted approximation): P01<1900 · P02 1901-40<->1901-36 ·
# P03 1941-60<->1937-59 · P04 1961-80<->1960-79 · P05 1981-2007<->1980-2006 ·
# P06 2008-20 and P07 2021+ <-> 'Posterior a 2006' (closest sheet).
TABULA_ES = {
    # (family,     period): wall_u  roof_u  window_u
    ("VivUni",    "P01"): dict(wall_u=2.56, roof_u=1.60, window_u=5.00),
    ("VivUni",    "P02"): dict(wall_u=2.56, roof_u=4.17, window_u=4.59),
    ("VivUni",    "P03"): dict(wall_u=2.56, roof_u=4.17, window_u=4.59),
    ("VivUni",    "P04"): dict(wall_u=1.33, roof_u=1.67, window_u=5.70),
    ("VivUni",    "P05"): dict(wall_u=0.72, roof_u=1.92, window_u=3.04),
    ("VivUni",    "P06"): dict(wall_u=0.47, roof_u=0.48, window_u=2.92),
    ("EdiPluri",  "P01"): dict(wall_u=2.56, roof_u=1.60, window_u=5.35),
    ("EdiPluri",  "P02"): dict(wall_u=2.56, roof_u=3.08, window_u=5.35),
    ("EdiPluri",  "P03"): dict(wall_u=2.94, roof_u=1.67, window_u=5.70),
    ("EdiPluri",  "P04"): dict(wall_u=1.64, roof_u=1.61, window_u=5.70),
    ("EdiPluri",  "P05"): dict(wall_u=0.62, roof_u=0.56, window_u=3.37),
    ("EdiPluri",  "P06"): dict(wall_u=0.52, roof_u=0.45, window_u=3.54),
    ("BlocPluri", "P01"): dict(wall_u=2.56, roof_u=4.17, window_u=5.35),
    ("BlocPluri", "P02"): dict(wall_u=2.56, roof_u=3.08, window_u=5.35),
    ("BlocPluri", "P03"): dict(wall_u=2.27, roof_u=1.37, window_u=4.72),
    ("BlocPluri", "P04"): dict(wall_u=1.33, roof_u=1.92, window_u=5.70),
    ("BlocPluri", "P05"): dict(wall_u=0.58, roof_u=0.60, window_u=3.37),
    ("BlocPluri", "P06"): dict(wall_u=0.48, roof_u=0.47, window_u=3.29),
}
PERIOD_FALLBACK = {"P07": "P06"}   # no sheet for P07 (2021+) -> closest period

# Ground-floor rule (family-level default; overridden per representative when
# Tipo15 has a ground-floor dwelling record — data beats assumption):
GROUND_UNCONDITIONED_BY_FAMILY = {"VivUni": False, "EdiPluri": True, "BlocPluri": True}

CLUSTER_RE = re.compile(r"^(VivUni|EdiPluri|BlocPluri)(P\d\d)$")

COL_CERT = "demanda_ca"   # certificate heating-demand column (definition pending — CONDITIONAL)

KEEP_CADASTRE_COLS = [
    "nombre", "coddistrit",                                  # district
    "pob_total", "pob_0_14", "pob_15_65", "pob_66_mas",      # population (census)
    "num_vivend", "numero_viv",                              # dwellings (count / class)
    "tipologia_", "ano_constr", "uso_princi",               # typology / year / use
    "Shape_Area",                                            # raw footprint area (m²) — distinct from res_area_m2 (residential)
    "calificaci", "califica_1",                             # certificate letters (pre/post)
    "coste_inte", "coste_in_1",                             # intervention cost (€/m² / total)
    "demanda__1", "ConsumE", "ConsumETot",                  # consumption
]

# ============================================================================
# 1) STOCK PREPARATION — load, clean, attach residential area
# ============================================================================

def load_stock(
    district: str | None = None, *, gis_path: Path | None = None,
    tipo15_path: Path | None = None, input_policy: StockInputPolicy | dict | None = None,
) -> gpd.GeoDataFrame:
    """Benicalap building stock: boundary filter + floor imputation + Tipo15 join.

    Cleaning rules (every correction is FLAGGED — visible in the result files):
      - malformed cluster label -> building EXCLUDED (reported).
      - altura_max < 1 (51 buildings): the cluster's non-zero Benicalap median
        is written, `imputed_floors=True` (0 floors is a cadastre gap; a
        building cannot have 0 floors).
      - Residential area: Tipo15 dwelling ledger, `442_sup_Residencial` summed
        per parcel (~99 % coverage). Uncovered buildings get their cluster's
        median (residential area / footprint) ratio x own footprint,
        `res_area_proxy=True`.
    """
    policy = input_policy if isinstance(input_policy, StockInputPolicy) else StockInputPolicy.from_dict(input_policy)
    stock, resolved, report = prepare_stock(
        Path(gis_path or mb.NEIGHBORS_SHP), Path(tipo15_path or TIPO15_CSV), policy,
        boundary_path=None if district else BOUNDARY_GPKG, district=district,
        duplicate_parcel_apportioning=False,
    )
    logger.info("[stock] %s: %s buildings, %s clusters.", report["scope"], len(stock), report["clusters"])
    logger.info("[stock] floor policy %s: %s invalid rows.",
                resolved.floor_invalid_policy, report["invalid_floor_buildings"])
    logger.info("[stock] residential area: %s proxy rows | total %s m².",
                report["residential_area_proxy_buildings"], f"{stock['res_area_m2'].sum():,.0f}")
    return stock

# ============================================================================
# 2) PERIOD PARAMETERS — cluster -> build_model params dict
# ============================================================================

def period_params(family: str, period: str) -> dict:
    """Produce the build_model params dict from the TABULA_ES table.

    - wall_u/roof_u: the builder turns these into period-aware LAYERED
      constructions (real template materials + a calibration layer sized to
      hit the target U exactly; `massless` stays False — the single-layer
      massless path is kept only for the frozen LHS study).
    - Thermal-bridge dU=0.10 is added to wall_u AUTOMATICALLY inside the
      builder (default param) — do NOT add it to the table here (double count).
    - window_g: single glazing (U>=4) -> 0.82 (datasheet §7.0.1); double
      glazing -> 0.75 (typical clear double; TABULA sheets give no g-value).
    """
    key = (family, PERIOD_FALLBACK.get(period, period))
    if key not in TABULA_ES:
        raise KeyError(f"Not in TABULA_ES: {key} — is the table incomplete?")
    u = TABULA_ES[key]
    return {
        "wall_u": u["wall_u"],
        "roof_u": u["roof_u"],
        "window_u": u["window_u"],
        "window_g": 0.82 if u["window_u"] >= 4.0 else 0.75,
        "ground_unconditioned": GROUND_UNCONDITIONED_BY_FAMILY[family],
    }


def ground_rule_from_tipo15(refparcela: str) -> bool | None:
    """Derive the ground-floor rule from DATA for one representative: if Tipo15
    has a planta-0 (ground) dwelling record on this parcel, the ground floor is
    residential (ground_unconditioned=False). Returns None when the parcel has
    no records at all (the family default is used then)."""
    t15 = pd.read_csv(TIPO15_CSV, sep=";", encoding="latin-1",
                      usecols=["31_pc", "252_planta"], dtype=str)
    rows = t15[t15["31_pc"] == refparcela]
    if rows.empty:
        return None
    plantas = rows["252_planta"].fillna("").str.strip().str.upper()
    has_ground_dwelling = plantas.isin({"0", "00", "BJ", "B", "BX"}).any()
    return not has_ground_dwelling


def resolve_ground_rule(
    refparcela: str, family: str, *, tipo15_path: Path | None = None,
    input_policy: StockInputPolicy | dict | None = None,
) -> tuple[bool, str]:
    """Resolve the representative ground rule under the typed stock policy."""
    policy = input_policy if isinstance(input_policy, StockInputPolicy) else StockInputPolicy.from_dict(input_policy)
    return ground_rule_for_representative(
        refparcela, family, Path(tipo15_path or TIPO15_CSV), policy.ground_floor_mode,
        GROUND_UNCONDITIONED_BY_FAMILY,
    )

# ============================================================================
# 3) REPRESENTATIVE SELECTION — one building per cluster
# ============================================================================

def _vertex_count(geom) -> int:
    """Exterior vertex count; exception-safe (city-scale lesson #17: courtyard/
    broken geometries must not crash the selection — a 999 score effectively
    disqualifies them while the pre-flight loop still guards the final pick)."""
    try:
        return len(mb.clean_polygon(geom).exterior.coords) - 1
    except Exception:
        return 999


def select_representatives(stock: gpd.GeoDataFrame) -> pd.DataFrame:
    """Select the median-typical building of each cluster.

    Score (lower = better): |area-median|/median + |floors-median|/median
                            + 0.01 x vertex count (simple geometry preferred).
    Conditions: floors not imputed (real data) + passes the geometry pre-flight
    (clean_polygon -> prepare_footprint -> area band). Candidates are tried in
    score order; the first one that passes becomes the representative
    (generalisation of the select_building.py logic).
    """
    reps = []
    for cluster, sub in stock.groupby("cluster"):
        cand = sub[~sub["imputed_floors"]].copy()
        if cand.empty:                      # whole cluster imputed: use all of it
            cand = sub.copy()
        area_field = "footprint_area_m2" if "footprint_area_m2" in cand.columns else "Shape_Area"
        med_area = cand[area_field].median()
        med_fl = cand["altura_max"].median()
        cand["n_vertices"] = cand.geometry.apply(_vertex_count)
        cand["score"] = (abs(cand[area_field] - med_area) / max(med_area, 1)
                         + abs(cand["altura_max"] - med_fl) / max(med_fl, 1)
                         + 0.01 * cand["n_vertices"])
        chosen, reason = None, None
        for _, row in cand.sort_values("score").iterrows():
            try:
                mb.validate_building_row(row)
                mb.prepare_footprint(mb.clean_polygon(row.geometry))
                chosen, reason = row, "pre-flight OK"
                break
            except Exception as e:          # failing candidate skipped, next one tried
                reason = f"candidate skipped: {e}"
        if chosen is None:
            raise RuntimeError(f"{cluster}: no candidate passed the pre-flight ({reason})")
        reps.append({
            "cluster": cluster, "family": chosen["family"], "period": chosen["period"],
            "refparcela": chosen["refparcela"], "n_buildings": len(sub),
            "rep_area_m2": round(float(chosen[area_field]), 1),
            "cluster_med_area_m2": round(float(med_area), 1),
            "rep_floors": int(chosen["altura_max"]), "cluster_med_floors": float(med_fl),
            "rep_vertices": int(chosen["n_vertices"]),
        })
    df = pd.DataFrame(reps).sort_values("n_buildings", ascending=False)
    logger.info("[selection] representative chosen for %s clusters.", len(df))
    return df

# ============================================================================
# 4) BATCH RUN — each representative through the SAME chain as the pilot
# ============================================================================

def run_representative(stock: gpd.GeoDataFrame, rep: pd.Series,
                       out_dir: Path,
                       scenario: dict | None = None, *,
                       tipo15_path: Path | None = None,
                       input_policy: StockInputPolicy | dict | None = None,
                       gis_path: Path | None = None) -> tuple[dict, bool]:
    """Run one representative: period params + data-driven ground rule, then
    the full pilot chain via sim.simulate_building (build -> E+ -> read -> QA
    -> carbon). Output folder: <out_dir>/<cluster>/.

    scenario (Part G, optional): extra simulate_building kwargs applied to
    BOTH runs — heat_delta / cool_delta (K) and/or epw (Path)."""
    scenario = scenario or {}
    row = stock[stock["refparcela"] == rep["refparcela"]].iloc[0]
    cluster = rep["cluster"]
    logger.info("===== %s -> representative %s (%s buildings) =====",
                cluster, rep["refparcela"], rep["n_buildings"])

    params = period_params(rep["family"], rep["period"])
    g_data, ground_source = resolve_ground_rule(
        rep["refparcela"], rep["family"], tipo15_path=tipo15_path,
        input_policy=input_policy,
    )
    if g_data != params["ground_unconditioned"]:
        logger.info("[ground] policy %s: ground_unconditioned=%s", ground_source, g_data)
    params["ground_unconditioned"] = g_data
    logger.info("[params] wall U=%s roof U=%s window U=%s/g=%s ground-buffer=%s",
                params["wall_u"], params["roof_u"], params["window_u"],
                params["window_g"], params["ground_unconditioned"])

    resolved_gis_path = Path(gis_path or mb.NEIGHBORS_SHP)
    rec, qa_ok = sim.simulate_building(
        row, out_dir, params=params, label=cluster,
        neighbors_path=resolved_gis_path, source_gis_path=resolved_gis_path, **scenario,
    )

    # Part F3: second run of the SAME representative with the real HVAC system
    # (consumption). Only the consumption/carbon keys are merged; geometry
    # stats come from the demand run (identical model).
    rec_h, qa_h = sim.simulate_building(
        row, out_dir, params=params, label=cluster, hvac=True,
        neighbors_path=resolved_gis_path, source_gis_path=resolved_gis_path, **scenario,
    )
    cons_keys = [k for k in rec_h if k.startswith(
        ("cons_", "lighting_", "equipment_", "site_", "total_site_",
         "hvac_", "heating_efficiency", "cooling_cop"))]
    rec.update({k: rec_h[k] for k in cons_keys})
    rec["qa_all_pass_hvac"] = qa_h
    qa_ok = qa_ok and qa_h

    return ({**rep.to_dict(), **rec, "ground_rule_source": ground_source,
             **{f"param_{k}": v for k, v in params.items()}}, qa_ok)
 
def run_single_building(
    refparcela: str, out_dir: Path, scenario: dict | None = None, *,
    gis_path: Path | None = None, tipo15_path: Path | None = None,
    input_policy: StockInputPolicy | dict | None = None,
) -> int:
    """Simulate ONE building anywhere in Valencia and write a readable summary
    (building_<ref>.txt). Uses the representative chain — the TABULA envelope of
    the building's OWN cluster + the Tipo15 ground rule — but SKIPS scaling and
    validation (a single building has no cohort to scale to)."""
    scenario = scenario or {}
    policy = input_policy if isinstance(input_policy, StockInputPolicy) else StockInputPolicy.from_dict(input_policy)
    city, resolved_policy, _policy_report = prepare_stock(
        Path(gis_path or mb.NEIGHBORS_SHP), Path(tipo15_path or TIPO15_CSV), policy,
        reference=refparcela,
        duplicate_parcel_apportioning=False,
    )
    match = city[city["refparcela"].astype(str) == str(refparcela)]
    if match.empty:
        raise ValueError(
            f"refparcela {refparcela!r} is not available after applying the selected stock input policy"
        )
    row = match.iloc[0].copy()

    m = CLUSTER_RE.match(str(row["cluster"]))
    if not m:
        raise ValueError(f"{refparcela}: unrecognised cluster label {row['cluster']!r}")
    family, period = m.group(1), m.group(2)

    params = period_params(family, period)
    g_data, ground_source = resolve_ground_rule(
        refparcela, family, tipo15_path=Path(tipo15_path or TIPO15_CSV),
        input_policy=resolved_policy,
    )
    if g_data != params["ground_unconditioned"]:
        logger.info("[ground] policy %s: ground_unconditioned=%s", ground_source, g_data)
    params["ground_unconditioned"] = g_data

    logger.info("===== SINGLE BUILDING %s (%s) =====", refparcela, row["cluster"])
    resolved_gis_path = Path(gis_path or mb.NEIGHBORS_SHP)
    try:
        rec, qa_ok = sim.simulate_building(
            row, out_dir, params=params, label=f"building_{refparcela}",
            neighbors_path=resolved_gis_path, source_gis_path=resolved_gis_path, **scenario,
        )
    except ValueError as exc:      # builder guardrail (e.g. footprint outside 50–5000 m²)
        logger.error("[single] cannot model %s: %s", refparcela, exc)
        logger.error("[single] the neighbourhood/city pipeline handles such buildings by "
                     "choosing a median in-range representative for the cluster instead.")
        return 2

    # Part F: second run of the SAME building with the real HVAC system.
    # CONSUMPTION is the primary metric; the demand run above stays as reference.
    try:
        rec_h, qa_h = sim.simulate_building(
            row, out_dir, params=params, label=f"building_{refparcela}", hvac=True,
            neighbors_path=resolved_gis_path, source_gis_path=resolved_gis_path, **scenario,
        )
        cons_keys = [k for k in rec_h if k.startswith(
            ("cons_", "lighting_", "equipment_", "site_", "total_site_",
             "hvac_", "heating_efficiency", "cooling_cop"))]
        rec.update({k: rec_h[k] for k in cons_keys})
        qa_ok = qa_ok and qa_h
        has_cons = True
    except ValueError as exc:
        logger.warning("[single] consumption (HVAC) run skipped: %s", exc)
        has_cons = False

    cert = rec.get("cadastre_heat_kwh_m2")
    lines = [
        f"SINGLE BUILDING RESULT ({datetime.now():%Y-%m-%d %H:%M})",
        f"refparcela         : {refparcela}",
        f"district (nombre)  : {row.get('nombre')}",
        f"cluster            : {row['cluster']}  (family {family}, period {period})",
        f"stock policy       : {policy_fingerprint(resolved_policy)[:16]}  (ground {ground_source})",
        f"floors (altura_max): {int(row['altura_max'])}",
        f"footprint          : {rec['footprint_m2']} m²",
        f"residential area   : {rec['res_area_m2']} m²",
        f"TABULA envelope    : wall U={params['wall_u']}  roof U={params['roof_u']}  "
        f"window U={params['window_u']} / g={params['window_g']}",
        "",
        "CONSUMPTION — real HVAC, PRIMARY (gas heating eta=0.85 + split COP 2.5):",
    ]
    if has_cons:
        lines += [
            f"  HVAC consumption (heat+cool+fans): {rec['cons_hc_kwh_m2']} kWh/m²/yr",
            f"  total site energy (+light+equip) : {rec['total_site_kwh_m2']} kWh/m²/yr",
            f"  carbon, real system (HVAC)       : {rec['hvac_co2_kg_m2']} kgCO₂/m² ({rec['hvac_co2_t_yr']} t/yr)",
            f"  carbon, total site               : {rec['total_site_co2_kg_m2']} kgCO₂/m² ({rec['total_site_co2_t_yr']} t/yr)",
        ]
    else:
        lines.append("  (skipped — building outside the HVAC-model range)")
    lines += [
        "",
        "DEMAND — ideal loads, reference:",
        f"  heating demand : {rec['heating_kwh_m2']} kWh/m²/yr",
        f"  cooling demand : {rec['cooling_kwh_m2']} kWh/m²/yr",
        f"  carbon S1 (resistance+split): {rec['s1_co2_kg_m2']} kgCO₂/m² ({rec['s1_co2_t_yr']} t/yr)",
        f"  carbon S2 (heat pump)       : {rec['s2_co2_kg_m2']} kgCO₂/m² ({rec['s2_co2_t_yr']} t/yr)",
        "",
        f"QA all passed      : {qa_ok}",
    ]
    if cert is not None and cert > 0:
        lines.append(f"cadastre demanda_ca (heating): {cert} kWh/m²  "
                     f"(model/cert ratio {rec.get('heat_model_over_cadastre')} — read with Q3 caveat)")
    text = "\n".join(lines) + "\n"
    (out_dir / f"building_{refparcela}.txt").write_text(text)
    logger.info("\n%s", text)
    logger.info("Readable summary written: %s", out_dir / f"building_{refparcela}.txt")
    return 0 if qa_ok else 1

# ============================================================================
# 5) SCALING — cluster kWh/m2 -> GIS join over all 959 buildings
# ============================================================================

def scale_to_stock(stock: gpd.GeoDataFrame, cluster_results: pd.DataFrame) -> gpd.GeoDataFrame:
    """The heart of the method (a deliberate granularity mismatch): simulate at
    typology level, apply at building level. Every building multiplies its
    cluster's kWh/m2 by its OWN residential area; carbon scales the same way."""
    per_cluster = cluster_results.set_index("cluster")
    out = stock.copy()
    for col in ("heating_kwh_m2", "cooling_kwh_m2", "s1_co2_kg_m2", "s2_co2_kg_m2"):
        out[col] = out["cluster"].map(per_cluster[col])
    out["heating_kwh"] = out["heating_kwh_m2"] * out["res_area_m2"]
    out["cooling_kwh"] = out["cooling_kwh_m2"] * out["res_area_m2"]
    out["s1_co2_t"] = out["s1_co2_kg_m2"] * out["res_area_m2"] / 1000.0
    out["s2_co2_t"] = out["s2_co2_kg_m2"] * out["res_area_m2"] / 1000.0

    # Part F3: consumption columns (real-HVAC run). Conditional so that old
    # demand-only clusters_results.csv files keep working.
    cons_m2 = ("cons_hc_kwh_m2", "total_site_kwh_m2",
               "s1_consumption_kwh_m2", "s2_consumption_kwh_m2",
               "hvac_co2_kg_m2", "total_site_co2_kg_m2")
    for col in cons_m2:
        if col in per_cluster.columns:
            out[col] = out["cluster"].map(per_cluster[col])
    if "cons_hc_kwh_m2" in out.columns:
        out["cons_hc_kwh"] = out["cons_hc_kwh_m2"] * out["res_area_m2"]
        out["total_site_kwh"] = out["total_site_kwh_m2"] * out["res_area_m2"]
        out["s1_consumption_kwh"] = out["s1_consumption_kwh_m2"] * out["res_area_m2"]
        out["s2_consumption_kwh"] = out["s2_consumption_kwh_m2"] * out["res_area_m2"]
        out["hvac_co2_t"] = out["hvac_co2_kg_m2"] * out["res_area_m2"] / 1000.0
        out["total_site_co2_t"] = out["total_site_co2_kg_m2"] * out["res_area_m2"] / 1000.0
    return out

# ============================================================================
# 6) VALIDATION — certificate column + neighbourhood totals
# ============================================================================

def validate(buildings: gpd.GeoDataFrame) -> str:
    """Two levels:
    (a) per building: model heating kWh/m2 vs the `demanda_ca` certificate
        column (CONDITIONAL — the column definition is still with Javier;
        pilot finding: 27.97 lay outside the whole model distribution).
    (b) neighbourhood: total demand + typology breakdown (expectation from the
        literature: multi-family dominant; Sustainability 2021 had the P04
        multi-family cluster at ~59 %).
    """
    L = ["", "=" * 70, "VALIDATION", "=" * 70]

    ok = buildings[COL_CERT].notna() & (buildings[COL_CERT] > 0)
    L.append(f"\n(a) Per building — model vs certificate `demanda_ca` ({int(ok.sum())} buildings):")
    grp = buildings[ok].groupby("cluster").agg(
        n=("cluster", "size"),
        model=("heating_kwh_m2", "first"),
        cert_med=(COL_CERT, "median"))
    grp["model_over_cert"] = (grp["model"] / grp["cert_med"]).round(2)
    L.append(grp.round(2).to_string())
    w_model = (buildings.loc[ok, "heating_kwh_m2"] * buildings.loc[ok, "res_area_m2"]).sum() \
        / buildings.loc[ok, "res_area_m2"].sum()
    w_cert = (buildings.loc[ok, COL_CERT] * buildings.loc[ok, "res_area_m2"]).sum() \
        / buildings.loc[ok, "res_area_m2"].sum()
    L.append(f"\nArea-weighted heating: model {w_model:.2f} vs certificate {w_cert:.2f} kWh/m² "
             f"(ratio {w_model / w_cert:.2f}) — read with the definition caveat (Javier Q3).")

    if "cons_hc_kwh" in buildings.columns:
        L.append(f"\n(b) CONSUMPTION — primary metric (real HVAC in the model: gas burner "
                 f"η={mb.HVAC_HEATING_EFFICIENCY} + split COP {mb.HVAC_COOLING_COP}; "
                 f"ventilation = infiltration + windows, no mechanical outdoor air):")
        L.append(f"  HVAC consumption (heat+cool+fans): {buildings['cons_hc_kwh'].sum() / 1e6:8.2f} GWh/yr")
        L.append(f"  Total site energy (+light+equip) : {buildings['total_site_kwh'].sum() / 1e6:8.2f} GWh/yr")
        L.append(f"  Carbon, real system (HVAC only)  : {buildings['hvac_co2_t'].sum():8.0f} tCO₂/yr")
        L.append(f"  Carbon, total site               : {buildings['total_site_co2_t'].sum():8.0f} tCO₂/yr")
        L.append(f"  S1–S2 demand-conversion bracket  : "
                 f"{buildings['s2_consumption_kwh'].sum() / 1e6:.2f}–"
                 f"{buildings['s1_consumption_kwh'].sum() / 1e6:.2f} GWh/yr (reference)")

    L.append(f"\n(c) Neighbourhood totals — DEMAND, reference ({len(buildings)} buildings, "
             f"{buildings['res_area_m2'].sum():,.0f} m² residential area):")
    tot_h = buildings["heating_kwh"].sum() / 1e6
    tot_c = buildings["cooling_kwh"].sum() / 1e6
    L.append(f"  Heating demand : {tot_h:8.2f} GWh/yr")
    L.append(f"  Cooling demand : {tot_c:8.2f} GWh/yr")
    L.append(f"  Carbon S1 (resistance+split): {buildings['s1_co2_t'].sum():8.0f} tCO₂/yr")
    L.append(f"  Carbon S2 (heat pump)       : {buildings['s2_co2_t'].sum():8.0f} tCO₂/yr")

    L.append("\n  Typology family breakdown (share of heating+cooling demand):")
    tot = (buildings["heating_kwh"] + buildings["cooling_kwh"]).sum()
    fam = (buildings.groupby("family").apply(lambda d: (d["heating_kwh"] + d["cooling_kwh"]).sum() / tot * 100, include_groups=False).round(1))
    for f_, v in fam.sort_values(ascending=False).items():
        L.append(f"{f_:10s}: {v} %")
    top = (buildings.groupby("cluster").apply(lambda d : (d["heating_kwh"] + d["cooling_kwh"]).sum() / tot * 100, include_groups=False).round(1).sort_values(ascending=False))
    L.append(f"Largest cluster share: {top.index[0]} {top.iloc[0]} % "
             f"(Sustainability 2021 benchmark: P04 multi-family ~59 %)")
    return "\n".join(L)

# ============================================================================
# 7) MAIN — CLI + orchestration
# ============================================================================

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Benicalap neighbourhood pipeline: 18 representative "
                    "simulations scaled to 959 buildings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--out-dir", type=Path, default=Path(DEFAULT_OUT_DIR),
                   help="output directory (one sub-folder per cluster + CSV/GPKG/summary)")
    p.add_argument("--district", metavar="NAME",
                   help="run a whole municipal district by its `nombre` value "
                        "(e.g. --district CAMPANAR) instead of the Benicalap boundary. "
                        "NOTE: --district BENICALAP = 1013 buildings (admin border), "
                        "NOT the 959-building boundary baseline.")
    p.add_argument("--building", metavar="REFPARCELA",
                   help="simulate ONE building anywhere in Valencia and write a "
                        "readable building_<ref>.txt summary; skips scaling/validation")
    p.add_argument("--only", action="append", metavar="CLUSTER",
                   help="run only this cluster (repeatable, e.g. --only BlocPluriP04); "
                        "scaling/validation are SKIPPED on partial runs")
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
        for path in (BOUNDARY_GPKG, TIPO15_CSV):
            if not path.exists():
                raise FileNotFoundError(f"Required input not found: {path}")
        out_dir = args.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_dir = args.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        scenario = {}
        if args.heat_delta:
            scenario["heat_delta"] = args.heat_delta
        if args.cool_delta:
            scenario["cool_delta"] = args.cool_delta
        if args.epw is not None:
            scenario["epw"] = args.epw

        if args.building:                     # single-building readable output
            return run_single_building(args.building, out_dir, scenario=scenario)

        if args.district and args.out_dir == Path(DEFAULT_OUT_DIR):
            logger.warning("[out] --district with the default out-dir would overwrite the "
                           "Benicalap baseline in out/neighborhood; consider --out-dir tmp/<district>.")

        stock = load_stock(district=args.district)

        stock = load_stock()
        reps = select_representatives(stock)
        if args.only:
            unknown = sorted(set(args.only) - set(reps["cluster"]))
            if unknown:
                raise ValueError(f"Unknown cluster(s) in --only: {unknown}; "
                                 f"valid: {sorted(reps['cluster'])}")
            reps = reps[reps["cluster"].isin(args.only)]
            logger.warning("[partial] --only: %s of %s clusters — scaling and "
                           "validation will be SKIPPED.", len(reps), stock["cluster"].nunique())
        reps.to_csv(out_dir / "representatives.csv", index=False)

        results, all_qa_ok = [], True
        for _, rep in reps.iterrows():
            rec, qa_ok = run_representative(stock, rep, out_dir, scenario=scenario)
            results.append(rec)
            all_qa_ok &= qa_ok
        cluster_results = pd.DataFrame(results)
        cluster_results.to_csv(out_dir / "clusters_results.csv", index=False)

        if args.only:
            logger.warning("Partial run — results_buildings.gpkg / summary.txt NOT written.")
        else:
            buildings = scale_to_stock(stock, cluster_results)
            base_keep = ["refparcela", "cluster", "family", "period", "altura_max",
                         "imputed_floors", "res_area_m2", "res_area_proxy",
                         "heating_kwh_m2", "cooling_kwh_m2", "heating_kwh", "cooling_kwh",
                         "s1_co2_kg_m2", "s2_co2_kg_m2", "s1_co2_t", "s2_co2_t",
                         "cons_hc_kwh_m2", "total_site_kwh_m2", "cons_hc_kwh",
                         "total_site_kwh", "hvac_co2_t", "total_site_co2_t", COL_CERT]
            extra = [c for c in KEEP_CADASTRE_COLS
                     if c in buildings.columns and c not in base_keep]
            missing = [c for c in KEEP_CADASTRE_COLS if c not in buildings.columns]
            if missing:
                logger.warning("[output] cadastre columns not found -> skipped: %s", missing)
            keep = base_keep + extra + ["geometry"]
            buildings[keep].to_file(out_dir / "results_buildings.gpkg", driver="GPKG")
            logger.info("[output] results_buildings.gpkg: %s energy + %s cadastre columns.",
                        len(base_keep), len(extra))

            report = validate(buildings)
            logger.info("%s", report)
            area_label = args.district.upper() if args.district else "BENICALAP"
            summary = (f"{area_label} NEIGHBOURHOOD RESULT ({datetime.now():%Y-%m-%d %H:%M})\n"
                       f"{len(stock)} buildings, {len(reps)} clusters / representative simulations\n"
                       + cluster_results[["cluster", "n_buildings", "cons_hc_kwh_m2",
                                          "total_site_kwh_m2", "heating_kwh_m2",
                                          "cooling_kwh_m2", "qa_all_pass"]].to_string(index=False)
                       + "\n" + report + "\n")
            (out_dir / "summary.txt").write_text(summary)
            logger.info("Outputs: %s/(representatives|clusters_results).csv, "
                        "results_buildings.gpkg (open in QGIS), summary.txt", out_dir)

        logger.info("Duration: %.1f min", (datetime.now() - t0).total_seconds() / 60)
        return 0 if all_qa_ok else 1
    except Exception as exc:
        logger.error("Run failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
