"""City-scale energy pipeline for the whole of Valencia (Part D).

Generalises the Benicalap neighbourhood pipeline (Part C) to the full city:
26,452 buildings / 21 clusters. The physics chain is NOT rewritten — Part C's
stock-agnostic functions are reused on the city stock (representative-model
method, AGENTS.md; Sustainability 2021, doi:10.3390/su13094654):

    1. Load and clean the FULL city stock (NO boundary filter): cluster parse
       + floor imputation + Tipo15 residential-area join. City-only rule:
       duplicated parcel references share the parcel's residential area in
       proportion to their footprints (prevents double counting).
    2. nbp.select_representatives -> ONE building per cluster (21 clusters).
    3. Default mode — FULL simulation: nbp.run_representative for each
       representative (build -> EnergyPlus -> QA -> carbon), then
       nbp.scale_to_stock over all 26,452 buildings, a district breakdown
       (19 districts, NEW at city scale) and validation.
       --models-only: build + render the 21 representative 3D models instead
       (no EnergyPlus) — the visual-QA mode of the original city builder.
       --building REF: build ONE model anywhere in Valencia (no EnergyPlus) —
       the "generalised Part A" proof: any Valencian building, one command.

Usage:
    cd ~/valencia-energy-sim
    .venv/bin/python src/city_pipeline.py [--models-only] [--building REF]
        [--only CLUSTER [--only CLUSTER ...]] [--out-dir PATH] [-v | --quiet]

Exit codes:
    0  all representatives simulated/built and every QA check passed
    1  finished but at least one QA check (or model build) failed
    2  hard failure (missing input, EnergyPlus Severe/Fatal, ...)

Duration: full ~6 min (21 sequential E+ runs); --models-only ~4 min (renders).
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd

import matplotlib
matplotlib.use("Agg")                      # headless rendering (no display)
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np

# The pilot + neighbourhood modules are REUSED as-is (no rewriting):
#   mb  = 3D model builder (geometry, party walls, shading, windows, params)
#   nbp = TABULA_ES table, period params, Tipo15 ground rule, representative
#         selection, run_representative (-> sim.simulate_building), scaling
import model_builder as mb
import neighborhood_pipeline as nbp
from stock_input_policy import StockInputPolicy, prepare_stock

logger = logging.getLogger("city_pipeline")

# ============================================================================
# CONFIG
# ============================================================================
DEFAULT_OUT_DIR = mb.OUT_DIR / "city"

COL_DISTRICT_CODE = "coddistrit"   # 19 municipal districts
COL_DISTRICT_NAME = "nombre"

# City-scale consumption references (Part F3):
# NOTE (2026-07-16): the reference chain's own E+ output (EdiPluriP04) shows
# its total INCLUDES DHW (~19%, year-round-flat gas) and assumes heat-pump
# systems (COP 4-5); our total site EXCLUDES DHW -> the nominal ratio is a
# scope-mismatched order-of-magnitude check, not a 1% validation.
RAI_CITY_TOTAL_SITE_GWH = 2015.0   # Rai, SimulaciónEnergéticaValencia.xlsx (ConsumETot total, DHW included)
RAI_DHW_SHARE = 0.19               # DHW share of the reference total (EdiPluriP04 E+ End Uses)
MUNICIPAL_RESID_2019_GWH = 1743.0  # Valencia municipal residential consumption, 2019 (bills: DHW included)

# ============================================================================
# 1) CITY STOCK — nbp.load_stock WITHOUT the Benicalap boundary filter
# ============================================================================

def load_stock_city(
    *, gis_path: Path | None = None, tipo15_path: Path | None = None,
    input_policy: StockInputPolicy | dict | None = None,
) -> gpd.GeoDataFrame:
    """Full Valencia building stock. Differences from nbp.load_stock: no
    boundary filter (all 26,452 buildings) + duplicated parcel references
    are handled (a city-only data problem).

    Cleaning rules (same as Benicalap, every correction FLAGGED):
      - malformed cluster label -> building EXCLUDED (reported).
      - altura_max < 1 (~1,648 buildings city-wide): cluster median written,
        `imputed_floors=True`.
      - Residential area: Tipo15 parcel totals (~99.3 % coverage); uncovered
        buildings get the cluster median ratio x own footprint,
        `res_area_proxy=True`.
      - Duplicated refparcela (several rows share one parcel reference): the
        Tipo15 join would give the SAME parcel area to every row -> the parcel
        area is apportioned by footprint share (`dup_refparcela=True`).
    """
    policy = input_policy if isinstance(input_policy, StockInputPolicy) else StockInputPolicy.from_dict(input_policy)
    stock, resolved, report = prepare_stock(
        Path(gis_path or mb.NEIGHBORS_SHP), Path(tipo15_path or nbp.TIPO15_CSV), policy,
        duplicate_parcel_apportioning=True,
    )
    logger.info("[stock] full Valencia: %s buildings, %s clusters.", len(stock), report["clusters"])
    logger.info("[stock] floor policy %s: %s invalid rows.",
                resolved.floor_invalid_policy, report["invalid_floor_buildings"])
    logger.info("[stock] residential area: %s proxy rows | total %s m².",
                report["residential_area_proxy_buildings"], f"{stock['res_area_m2'].sum():,.0f}")
    return stock

# ============================================================================
# 2) MODELS-ONLY PATH — build + render one representative (NO EnergyPlus)
# ============================================================================
# Colour code = OpenStudio Application's 'Render by Boundary Condition':
#   blue = exterior (Outdoors)   red = adiabatic party wall   green = interior
#   brown = ground   cyan = window   dark blue = balcony door   grey = shading

OBC_COLOR = {
    "Outdoors": ("#4a90d9", 0.45),
    "Adiabatic": ("#d64545", 0.85),
    "Surface": ("#5cb85c", 0.15),
    "Ground": ("#8b6f47", 0.6),
}


def _surface_polys(model) -> list:
    """Collect model surfaces as (vertex list, colour, alpha) triples."""
    polys = []
    for srf in model.getSurfaces():
        pts = [(v.x(), v.y(), v.z()) for v in srf.vertices()]
        color, alpha = OBC_COLOR.get(srf.outsideBoundaryCondition(), ("#999999", 0.3))
        polys.append((pts, color, alpha))
        for ss in srf.subSurfaces():
            wpts = [(v.x(), v.y(), v.z()) for v in ss.vertices()]
            color = "#0077cc" if ss.subSurfaceType() == "GlassDoor" else "#00d0d0"
            polys.append((wpts, color, 0.95))
    # Two kinds of shading surfaces (told apart by their group type):
    #   Space group = balcony overhangs -> dark grey plates
    #   Site group  = neighbour shading volumes -> very transparent light grey
    for sh in model.getShadingSurfaces():
        spts = [(v.x(), v.y(), v.z()) for v in sh.vertices()]
        grp = sh.shadingSurfaceGroup()
        is_context = (not grp.isNull()) and grp.get().shadingSurfaceType() == "Site"
        polys.append((spts, "#aaaaaa", 0.10) if is_context else (spts, "#666666", 0.9))
    return polys


def render_model(model, png_path: Path, title: str) -> None:
    """Four-view PNG render for visual QA (generalised plot_model: draws the
    given model object instead of a fixed pilot file)."""
    polys = _surface_polys(model)
    # Axis limits from BUILDING surfaces only — the 50 m shading context would
    # otherwise shrink the building to a speck (neighbours get cropped: wanted).
    bld_pts = np.array([(v.x(), v.y(), v.z())
                        for srf in model.getSurfaces() for v in srf.vertices()])
    mins, maxs = bld_pts.min(axis=0), bld_pts.max(axis=0)
    center, span = (mins + maxs) / 2, (maxs - mins).max() / 2
    span *= 1.35

    views = [("From SW", 25, -120), ("From E", 25, -30),
             ("From N", 25, 60), ("Top (footprint)", 88, -90)]
    fig = plt.figure(figsize=(16, 12))
    for i, (vtitle, elev, azim) in enumerate(views, 1):
        ax = fig.add_subplot(2, 2, i, projection="3d")
        for pts, color, alpha in polys:
            ax.add_collection3d(Poly3DCollection([pts], facecolor=color, alpha=alpha,
                                                 edgecolor="#333333", linewidth=0.4))
        ax.set_xlim(center[0] - span, center[0] + span)
        ax.set_ylim(center[1] - span, center[1] + span)
        ax.set_zlim(0, 2 * span)
        ax.view_init(elev=elev, azim=azim)
        ax.set_title(vtitle, fontsize=11)
        ax.set_xlabel("x (m)"); ax.set_ylabel("y (m -> N)"); ax.set_zlabel("z (m)")
    fig.suptitle(f"{title}\n(blue=exterior, red=party wall, cyan=window, "
                 f"dark blue=balcony door, green=interior, brown=ground, "
                 f"grey=neighbour shading)", fontsize=12)
    fig.tight_layout()
    fig.savefig(png_path, dpi=100)
    plt.close(fig)


def build_representative_model(stock: gpd.GeoDataFrame, rep: pd.Series,
                               out_dir: Path) -> dict:
    """Build + save + render ONE representative's 3D model (NO simulation).
    Same params/geometry chain as nbp.run_representative; E+ is replaced by
    save_model + a four-view render."""
    row = stock[stock["refparcela"] == rep["refparcela"]].iloc[0]
    cluster = rep["cluster"]
    logger.info("===== %s -> representative %s (%s buildings) =====",
                cluster, rep["refparcela"], rep["n_buildings"])

    params = nbp.period_params(rep["family"], rep["period"])
    g_data = nbp.ground_rule_from_tipo15(rep["refparcela"])   # data beats family default
    if g_data is not None and g_data != params["ground_unconditioned"]:
        logger.info("[ground] corrected from the Tipo15 record: ground_unconditioned=%s", g_data)
        params["ground_unconditioned"] = g_data
    logger.info("[params] wall U=%s roof U=%s window U=%s/g=%s ground-buffer=%s",
                params["wall_u"], params["roof_u"], params["window_u"],
                params["window_g"], params["ground_unconditioned"])

    geom = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(geom, row["refparcela"], mb.NEIGHBORS_SHP)
    party = mb.find_party_walls(geom, row["refparcela"], mb.NEIGHBORS_SHP, neighbors=neighbors)

    osm, stats = mb.build_model(row, party, params=params, neighbors=neighbors)
    logger.info("[model] footprint %s m² × %s storeys | party surfaces: %s | "
                "%s m² glass | %s shading surfaces",
                stats["footprint_m2"], stats["n_floors_total"],
                stats["n_party_surfaces"], stats["window_area_m2"],
                stats["n_shading_surfaces"])

    run_dir = out_dir / cluster
    run_dir.mkdir(parents=True, exist_ok=True)
    osm_path = mb.save_model(osm, run_dir)
    png_path = run_dir / "model_3d.png"
    render_model(osm, png_path, f"{cluster} — {rep['refparcela']} "
                                f"({rep['n_buildings']} buildings)")
    logger.info("[saved] %s + %s -> %s", osm_path.name, png_path.name, run_dir)

    rec = {k: v for k, v in stats.items() if k != "facade_qa"}
    return {**rep.to_dict(),
            **{f"stat_{k}": v for k, v in rec.items()},
            **{f"param_{k}": v for k, v in params.items()},
            "osm_path": str(osm_path)}

# ============================================================================
# 3) DISTRICT BREAKDOWN — NEW at city scale (19 municipal districts)
# ============================================================================

def district_breakdown(buildings: gpd.GeoDataFrame) -> pd.DataFrame:
    """Aggregate the scaled per-building results to the 19 municipal districts
    — the city-scale result map Javier can act on.

    Grouping key: the district NAME. One cadastre row carries a wrong district
    code (2795702YJ2729F: code 1 = Ciutat Vella, but the building sits 34 m
    inside Poblats del Nord and 6.5 km from Ciutat Vella), so the name is the
    reliable key; the modal code per name is reported alongside."""
    g = (buildings.groupby(COL_DISTRICT_NAME, dropna=False)
         .agg(coddistrit=(COL_DISTRICT_CODE, lambda s: s.mode().iat[0]),
              n_buildings=("refparcela", "size"),
              res_area_m2=("res_area_m2", "sum"),
              heating_kwh=("heating_kwh", "sum"),
              cooling_kwh=("cooling_kwh", "sum"),
              cons_hc_kwh=("cons_hc_kwh", "sum"),
              total_site_kwh=("total_site_kwh", "sum"),
              hvac_co2_t=("hvac_co2_t", "sum"),
              total_site_co2_t=("total_site_co2_t", "sum"),
              s1_co2_t=("s1_co2_t", "sum"),
              s2_co2_t=("s2_co2_t", "sum"))
         .reset_index())
    g["heating_gwh"] = (g["heating_kwh"] / 1e6).round(3)
    g["cooling_gwh"] = (g["cooling_kwh"] / 1e6).round(3)
    g["cons_hc_gwh"] = (g["cons_hc_kwh"] / 1e6).round(3)
    g["total_site_gwh"] = (g["total_site_kwh"] / 1e6).round(3)
    g["res_area_m2"] = g["res_area_m2"].round(0)
    for col in ("hvac_co2_t", "total_site_co2_t", "s1_co2_t", "s2_co2_t"):
        g[col] = g[col].round(1)
    g = g.sort_values("cons_hc_gwh", ascending=False)   # ranked by HVAC consumption
    return g.drop(columns=["heating_kwh", "cooling_kwh",
                           "cons_hc_kwh", "total_site_kwh"])

# ============================================================================
# 4) VALIDATION — certificate column + city totals + district top 5
# ============================================================================

def validate_city(buildings: gpd.GeoDataFrame, districts: pd.DataFrame) -> str:
    """Three levels:
    (a) per building: model heating kWh/m2 vs the `demanda_ca` certificate
        column (CONDITIONAL — definition still with Javier, Q3; 0 = missing
        certificate, excluded like NaN).
    (b) city totals + typology breakdown.
    (c) district ranking (top 5 by heating demand).
    """
    L = ["", "=" * 70, "VALIDATION — CITY OF VALENCIA", "=" * 70]

    ok = buildings[nbp.COL_CERT].notna() & (buildings[nbp.COL_CERT] > 0)
    L.append(f"\n(a) Per building — model vs certificate `demanda_ca` "
             f"({int(ok.sum())} of {len(buildings)} buildings):")
    w_model = (buildings.loc[ok, "heating_kwh_m2"] * buildings.loc[ok, "res_area_m2"]).sum() \
        / buildings.loc[ok, "res_area_m2"].sum()
    w_cert = (buildings.loc[ok, nbp.COL_CERT] * buildings.loc[ok, "res_area_m2"]).sum() \
        / buildings.loc[ok, "res_area_m2"].sum()
    L.append(f"Area-weighted heating: model {w_model:.2f} vs certificate {w_cert:.2f} kWh/m² "
             f"(ratio {w_model / w_cert:.2f}) — read with the definition caveat (Javier Q3).")

    if "cons_hc_kwh" in buildings.columns:
        hc_gwh = buildings["cons_hc_kwh"].sum() / 1e6
        ts_gwh = buildings["total_site_kwh"].sum() / 1e6
        L.append(f"\n(b) CONSUMPTION — primary metric (real HVAC: gas burner "
                 f"η={mb.HVAC_HEATING_EFFICIENCY} + split COP {mb.HVAC_COOLING_COP}; "
                 f"no mechanical outdoor air):")
        L.append(f"  HVAC consumption (heat+cool+fans): {hc_gwh:8.2f} GWh/yr")
        L.append(f"  Total site energy (+light+equip) : {ts_gwh:8.2f} GWh/yr")
        L.append(f"  Carbon, real system (HVAC only)  : {buildings['hvac_co2_t'].sum():8.0f} tCO₂/yr")
        L.append(f"  Carbon, total site               : {buildings['total_site_co2_t'].sum():8.0f} tCO₂/yr")
        L.append(f"  S1–S2 demand-conversion bracket  : "
                 f"{buildings['s2_consumption_kwh'].sum() / 1e6:.2f}–"
                 f"{buildings['s1_consumption_kwh'].sum() / 1e6:.2f} GWh/yr (reference)")
        L.append("  External references (total site basis; both references INCLUDE "
                 "DHW, our total does NOT — order-of-magnitude checks):")
        L.append(f"    vs reference city calculation ({RAI_CITY_TOTAL_SITE_GWH:.0f} GWh, DHW incl.): "
                 f"nominal ratio {ts_gwh / RAI_CITY_TOTAL_SITE_GWH:.2f}, "
                 f"scope-matched {ts_gwh / (RAI_CITY_TOTAL_SITE_GWH * (1 - RAI_DHW_SHARE)):.2f}")
        L.append(f"    vs municipal residential 2019 ({MUNICIPAL_RESID_2019_GWH:.0f} GWh, DHW incl.): "
                 f"ratio {ts_gwh / MUNICIPAL_RESID_2019_GWH:.2f}")

    L.append(f"\n(b2) City totals — DEMAND, reference ({len(buildings)} buildings, "
             f"{buildings['res_area_m2'].sum():,.0f} m² residential area):")
    tot_h = buildings["heating_kwh"].sum() / 1e6
    tot_c = buildings["cooling_kwh"].sum() / 1e6
    L.append(f"  Heating demand : {tot_h:8.2f} GWh/yr")
    L.append(f"  Cooling demand : {tot_c:8.2f} GWh/yr")
    L.append(f"  Carbon S1 (resistance+split): {buildings['s1_co2_t'].sum():8.0f} tCO₂/yr")
    L.append(f"  Carbon S2 (heat pump)       : {buildings['s2_co2_t'].sum():8.0f} tCO₂/yr")

    L.append("\n  Typology family breakdown (share of heating+cooling demand):")
    tot = (buildings["heating_kwh"] + buildings["cooling_kwh"]).sum()
    fam = (buildings.groupby("family").apply(lambda d: (d["heating_kwh"] + d["cooling_kwh"]).sum() / tot * 100,
                  include_groups=False).round(1))
    for f_, v in fam.sort_values(ascending=False).items():
        L.append(f"    {f_:10s}: {v} %")
    top = (buildings.groupby("cluster").apply(lambda d: (d["heating_kwh"] + d["cooling_kwh"]).sum() / tot * 100,
                  include_groups=False).round(1).sort_values(ascending=False))
    L.append(f"Largest cluster share: {top.index[0]} {top.iloc[0]} %")

    L.append(f"\n(c) Districts — top 5 of {len(districts)} by HVAC consumption "
             f"(full table: districts.csv):")
    cols = [COL_DISTRICT_CODE, COL_DISTRICT_NAME, "n_buildings",
            "cons_hc_gwh", "total_site_gwh", "heating_gwh", "cooling_gwh"]
    cols = [c for c in cols if c in districts.columns]
    L.append(districts[cols].head(5).to_string(index=False))
    return "\n".join(L)

# ============================================================================
# 5) MAIN — CLI + orchestration
# ============================================================================

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Valencia city pipeline: 21 representative simulations "
                    "scaled to 26,452 buildings (default), or 3D-model-only modes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--out-dir", type=Path, default=Path(DEFAULT_OUT_DIR),
                   help="output directory (one sub-folder per cluster + CSV/GPKG/summary)")
    p.add_argument("--models-only", action="store_true",
                   help="build + render the representative 3D models, NO EnergyPlus")
    p.add_argument("--building", metavar="REFPARCELA",
                   help="build ONE building's 3D model (any Valencian parcel "
                        "reference), NO EnergyPlus; ignores --only/--models-only")
    p.add_argument("--only", action="append", metavar="CLUSTER",
                   help="run only this cluster (repeatable); scaling/validation "
                        "are SKIPPED on partial simulation runs")
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
        if not nbp.TIPO15_CSV.exists():
            raise FileNotFoundError(f"Required input not found: {nbp.TIPO15_CSV}")
        out_dir = args.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        stock = load_stock_city()

        # --- Single-building mode: model ANY Valencian building -------------
        if args.building:
            ref = args.building.strip()
            hit = stock[stock["refparcela"] == ref]
            if hit.empty:
                raise ValueError(f"refparcela not found in the city stock: {ref}")
            row = hit.iloc[0]
            rep = pd.Series({"cluster": row["cluster"], "family": row["family"],
                             "period": row["period"], "refparcela": ref,
                             "n_buildings": 1,
                             "rep_area_m2": round(float(row["Shape_Area"]), 1),
                             "cluster_med_area_m2": float("nan"),
                             "rep_floors": int(row["altura_max"]),
                             "cluster_med_floors": float("nan"), "rep_vertices": -1})
            build_representative_model(stock, rep, out_dir)
            logger.info("Duration: %.1f min", (datetime.now() - t0).total_seconds() / 60)
            return 0

        reps = nbp.select_representatives(stock)
        if args.only:
            unknown = sorted(set(args.only) - set(reps["cluster"]))
            if unknown:
                raise ValueError(f"Unknown cluster(s) in --only: {unknown}; "
                                 f"valid: {sorted(reps['cluster'])}")
            reps = reps[reps["cluster"].isin(args.only)]
            logger.warning("[partial] --only: %s of %s clusters — scaling and "
                           "validation will be SKIPPED.", len(reps), stock["cluster"].nunique())
        reps.to_csv(out_dir / "representatives.csv", index=False)

        # --- Models-only mode: 21 x (build + render), NO EnergyPlus ---------
        if args.models_only:
            results, failures = [], []
            for _, rep in reps.iterrows():
                try:
                    results.append(build_representative_model(stock, rep, out_dir))
                except Exception as e:   # one broken cluster must not stop the rest
                    logger.error("[FAILED] %s: %s", rep["cluster"], e)
                    failures.append({"cluster": rep["cluster"],
                                     "refparcela": rep["refparcela"], "error": str(e)})
            pd.DataFrame(results).to_csv(out_dir / "models_stats.csv", index=False)
            if failures:
                pd.DataFrame(failures).to_csv(out_dir / "failures.csv", index=False)
            logger.info("CITY MODEL BUILD DONE: %s/%s models -> %s "
                        "(visual QA: <cluster>/model_3d.png)",
                        len(results), len(reps), out_dir)
            logger.info("Duration: %.1f min", (datetime.now() - t0).total_seconds() / 60)
            return 0 if not failures else 1

        # --- Full simulation mode (default) ----------------------------------
        scenario = {}
        if args.heat_delta:
            scenario["heat_delta"] = args.heat_delta
        if args.cool_delta:
            scenario["cool_delta"] = args.cool_delta
        if args.epw is not None:
            scenario["epw"] = args.epw
        if scenario and args.out_dir == Path(DEFAULT_OUT_DIR):
            logger.warning("[scenario] scenario knobs are set but --out-dir is the "
                           "default: the official baseline will be OVERWRITTEN. "
                           "Consider --out-dir tmp/scenario_x.")

        results, all_qa_ok = [], True
        for _, rep in reps.iterrows():
            rec, qa_ok = nbp.run_representative(stock, rep, out_dir, scenario=scenario)
            results.append(rec)
            all_qa_ok &= qa_ok
        cluster_results = pd.DataFrame(results)
        cluster_results.to_csv(out_dir / "clusters_results.csv", index=False)

        if args.only:
            logger.warning("Partial run — results_buildings.gpkg / districts.csv / "
                           "summary.txt NOT written.")
        else:
            buildings = nbp.scale_to_stock(stock, cluster_results)
            base_keep = ["refparcela", "cluster", "family", "period", "altura_max",
                         "imputed_floors", "res_area_m2", "res_area_proxy", "dup_refparcela",
                         COL_DISTRICT_CODE, COL_DISTRICT_NAME, "pob_total",
                         "heating_kwh_m2", "cooling_kwh_m2", "heating_kwh", "cooling_kwh",
                         "s1_co2_kg_m2", "s2_co2_kg_m2", "s1_co2_t", "s2_co2_t",
                         "cons_hc_kwh_m2", "total_site_kwh_m2", "cons_hc_kwh",
                         "total_site_kwh", "hvac_co2_t", "total_site_co2_t", nbp.COL_CERT]
            extra = [c for c in nbp.KEEP_CADASTRE_COLS
                     if c in buildings.columns and c not in base_keep]
            missing = [c for c in nbp.KEEP_CADASTRE_COLS if c not in buildings.columns]
            if missing:
                logger.warning("[output] cadastre columns not found -> skipped: %s", missing)
            keep = base_keep + extra + ["geometry"]
            buildings[keep].to_file(out_dir / "results_buildings.gpkg", driver="GPKG")
            logger.info("[output] results_buildings.gpkg: %s energy + %s cadastre columns.",
                        len(base_keep), len(extra))

            districts = district_breakdown(buildings)
            districts.to_csv(out_dir / "districts.csv", index=False)

            report = validate_city(buildings, districts)
            logger.info("%s", report)
            summary = (f"VALENCIA CITY RESULT ({datetime.now():%Y-%m-%d %H:%M})\n"
                       f"{len(stock)} buildings, {len(reps)} clusters / representative simulations\n"
                       + cluster_results[["cluster", "n_buildings", "cons_hc_kwh_m2",
                                          "total_site_kwh_m2", "heating_kwh_m2",
                                          "cooling_kwh_m2", "qa_all_pass"]].to_string(index=False)
                       + "\n" + report + "\n")
            (out_dir / "summary.txt").write_text(summary)
            logger.info("Outputs: %s/(representatives|clusters_results|districts).csv, "
                        "results_buildings.gpkg (open in QGIS), summary.txt", out_dir)

        logger.info("Duration: %.1f min", (datetime.now() - t0).total_seconds() / 60)
        return 0 if all_qa_ok else 1
    except Exception as exc:
        logger.error("Run failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
