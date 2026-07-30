"""LHS (Latin Hypercube Sampling) uncertainty study — pilot building.

Ashby Layer 4 in code:
  1. Samples the 10-variable uncertainty register N times with LHS
     (stratified sampling: each variable's range is cut into N slices and
     exactly one sample is drawn per slice -> uniform coverage with far
     fewer runs than Monte Carlo; sources: Procedure for LHS + LECCO BIP).
  2. For the 7 SIMULATION variables the pilot chain runs N times (model
     from model_builder, E+ run / result reading from run_simulation — no
     code duplication). Neighbour shading stays ON in every run: the urban
     fabric is not uncertain, it comes from GIS.
  3. The 3 POST variables (COP / SEER / emission factor) need no simulation —
     carbon is computed analytically from the same LHS matrix.
  4. Outputs: runs.csv + histograms.png + tornado.png + console summary.

VARIABLE REGISTER (Ashby Layer 2; full table in the Obsidian experiment note):
  Energy   : wall_u, roof_u, window_u, window_g, infiltration_ach,
             thermal_bridge_du (TABULA blanket increment — detail unknown)
  Society  : shade_setpoint (persiana usage habit)
  Energy/Economics (post): cop, seer (1974 building: system stock unknown)
  Legislation/Environment (post): emission_factor (official CTE 0.331 vs
             ~0.15 for the 2024 grid)

NOTE ON THE WALL MODEL: the LHS deliberately uses the MASSLESS wall/roof
path (params["massless"] = True) so a single sampled U-value maps directly
onto the envelope; deterministic runs (pilot / neighbourhood / city) use the
real layered constructions instead. This is why the LHS distribution sits
above the layered IVE baseline (11.21 kWh/m2) — both baselines are drawn on
the plots for an honest comparison.

USAGE:
  cd ~/valencia-energy-sim
  .venv/bin/python src/lhs_study.py            # N=50, seed 42 (~10 min)
  .venv/bin/python src/lhs_study.py --n 2      # quick smoke test
"""

import argparse
import logging
import shutil
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")                  # headless rendering (no display needed)
import matplotlib.pyplot as plt
import pandas as pd
from scipy.stats import qmc, spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_builder as mb             # 3D model construction + shared CONFIG
import run_simulation as sim           # E+ run + result reading

logger = logging.getLogger("lhs_study")


# ============================================================================
# CONFIG — variable register: (min, max); all Uniform (v1 simplification;
# sources and meanings live in the Obsidian register table).
# ============================================================================

SIM_VARS = {                     # 7 variables passed to build_model params
    "wall_u": (1.2, 2.0),        # W/m2K — IVE 1960-80 period uncertainty (nominal 1.43 eff.)
    "roof_u": (1.4, 2.3),        # W/m2K — flat roof, uninsulated range (nominal ~1.9)
    "window_u": (4.5, 5.7),      # W/m2K — single glazing + frame condition
    "window_g": (0.70, 0.85),    # SHGC — glass soiling / type uncertainty
    "infiltration_ach": (0.1, 0.5),   # 1/h — 1974 joinery airtightness (nominal 0.2)
    "shade_setpoint": (150.0, 400.0), # W/m2 — persiana closing habit
    "thermal_bridge_du": (0.0, 0.2),  # W/m2K — TABULA blanket increment (nominal 0.10;
                                      # 1974 detailing unknown: 0=neglect, 0.2=poor detail)
}
POST_VARS = {                    # 3 carbon variables, no simulation needed
    "cop": (1.0, 3.0),           # heating system efficiency (resistance <-> heat pump)
    "seer": (1.8, 3.5),          # cooling efficiency (old <-> new split)
    "emission_factor": (0.15, 0.331),  # kgCO2/kWh — 2024 grid <-> official CTE
}
ALL_VARS = {**SIM_VARS, **POST_VARS}

DEFAULT_N = 50
DEFAULT_SEED = 42

# Comparison lines drawn on the plots:
BASELINE_MASSLESS = {"heating": 16.67, "cooling": 18.67,   # deterministic massless pilot
                     "co2_s1": 8.61, "co2_s2": 4.68}       # (same physics as the LHS path)
BASELINE_LAYERED = {"heating": 11.21, "cooling": 16.6}     # layered IVE pilot baseline
# Confirmed external-source heating validation reference. demanda__1 is
# deliberately absent: it is unconfirmed and cannot be used as cooling truth.
CADASTRE = {"heating": 27.97}

# ============================================================================
# 1) SAMPLING
# ============================================================================

def sample_matrix(n: int, seed: int) -> pd.DataFrame:
    """Build the 10-dimensional LHS sample and scale it to the real ranges."""
    names = list(ALL_VARS)
    sampler = qmc.LatinHypercube(d=len(names), seed=seed)
    unit = sampler.random(n=n)                    # [0,1) hypercube
    lows = [ALL_VARS[k][0] for k in names]
    highs = [ALL_VARS[k][1] for k in names]
    scaled = qmc.scale(unit, lows, highs)         # to the real ranges
    return pd.DataFrame(scaled, columns=names) 


# ============================================================================
# 2) STUDY LOOP — N x (build model -> E+ -> results -> analytic carbon)
# ============================================================================

def run_study(samples: pd.DataFrame, out_dir: Path) -> pd.DataFrame:
    """Run the pilot chain once per LHS row and collect the results."""
    n = len(samples)

    # Building + neighbours + party wall are prepared ONCE (constant in the
    # loop) — reading the shapefile 50 times would be pure waste.
    buildings = mb.load_buildings(mb.BUILDINGS_GPKG)
    row = buildings.iloc[0]
    geom = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(geom, row["refparcela"], mb.NEIGHBORS_SHP)
    party = mb.find_party_walls(geom, row["refparcela"], mb.NEIGHBORS_SHP,
                                neighbors=neighbors)

    records = []
    for i, s in samples.iterrows():
        params = {k: float(s[k]) for k in SIM_VARS}
        # The LHS deliberately uses the MASSLESS path (see module docstring):
        # a sampled U maps 1:1 onto the envelope, and results stay consistent
        # with the frozen N=50 study.
        params["massless"] = True
        osm, stats = mb.build_model(row, party, params, neighbors=neighbors)
        run_dir = out_dir / f"run_{i:02d}"
        sql = sim.run_energyplus(osm, run_dir)
        res = sim.read_results(sql, stats["res_area_m2"])

        # Carbon: post variables come from the same LHS row (continuous
        # ranges instead of the two fixed S1/S2 scenarios).
        cons = res["heating_kwh_m2"] / s["cop"] + res["cooling_kwh_m2"] / s["seer"]
        co2_kg_m2 = cons * s["emission_factor"]
        rec = {**{k: round(float(s[k]), 4) for k in ALL_VARS},
               "heating_kwh_m2": res["heating_kwh_m2"],
               "cooling_kwh_m2": res["cooling_kwh_m2"],
               "consumption_kwh_m2": round(cons, 2),
               "co2_kg_m2": round(co2_kg_m2, 2),
               "co2_t_building": round(co2_kg_m2 * stats["res_area_m2"] / 1000.0, 1)}
        records.append(rec)
        logger.info("[lhs] run %d/%d: heating %.1f | cooling %.1f | CO2 %.1f kg/m2",
                    i + 1, n, res["heating_kwh_m2"], res["cooling_kwh_m2"], co2_kg_m2)
        shutil.rmtree(run_dir, ignore_errors=True)   # disk hygiene (sql files are big)
    return pd.DataFrame(records)

# ============================================================================
# 3) PLOTS + SUMMARY
# ============================================================================

def make_histograms(df: pd.DataFrame, out_dir: Path, n: int) -> None:
    """Distribution histograms with baseline / cadastre comparison lines."""
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
    panels = [
        ("heating_kwh_m2", "Heating demand (kWh/m2*yr)",
         [("massless baseline", BASELINE_MASSLESS["heating"], "tab:blue"),
          ("layered IVE baseline", BASELINE_LAYERED["heating"], "tab:green"),
          ("cadastre*", CADASTRE["heating"], "tab:red")]),
        ("cooling_kwh_m2", "Cooling demand (kWh/m2*yr)",
         [("massless baseline", BASELINE_MASSLESS["cooling"], "tab:blue"),
          ("layered IVE baseline", BASELINE_LAYERED["cooling"], "tab:green")]),
        ("co2_kg_m2", "Carbon (kgCO2/m2*yr)",
         [("S1 resistance", BASELINE_MASSLESS["co2_s1"], "tab:orange"),
          ("S2 heat pump", BASELINE_MASSLESS["co2_s2"], "tab:green")]),
    ]
    for ax, (col, title, lines) in zip(axes, panels):
        ax.hist(df[col], bins=12, color="#8ab4d8", edgecolor="#345")
        for label, x, c in lines:
            ax.axvline(x, color=c, linestyle="--", linewidth=1.6, label=label)
        ax.set_title(title, fontsize=11)
        ax.legend(fontsize=8)
    fig.suptitle(f"LHS uncertainty distributions — pilot building, N={n} "
                 "(cadastre reference: external-source heating only)")
    fig.tight_layout()
    fig.savefig(out_dir / "histograms.png", dpi=110)


def make_tornado(df: pd.DataFrame, out_dir: Path) -> dict:
    """Spearman rank-correlation tornado plots; returns the top-3 summary.

    Rank correlation: high |rho| = that input strongly drives the output
    (red bars increase it, blue bars decrease it).
    """
    outputs = [("heating_kwh_m2", list(SIM_VARS)),
               ("cooling_kwh_m2", list(SIM_VARS)),
               ("co2_kg_m2", list(ALL_VARS))]
    fig, axes = plt.subplots(1, 3, figsize=(17, 5))
    sens_summary = {}
    for ax, (col, varlist) in zip(axes, outputs):
        rhos = {v: spearmanr(df[v], df[col]).statistic for v in varlist}
        ordered = sorted(rhos.items(), key=lambda kv: abs(kv[1]))
        names = [k for k, _ in ordered]
        vals = [v for _, v in ordered]
        ax.barh(names, vals, color=["#d64545" if v > 0 else "#4a90d9" for v in vals])
        ax.axvline(0, color="#333", linewidth=0.8)
        ax.set_title(f"{col} — Spearman rho", fontsize=11)
        ax.set_xlim(-1, 1)
        sens_summary[col] = sorted(rhos.items(), key=lambda kv: -abs(kv[1]))[:3]
    fig.suptitle("Sensitivity: which uncertain input drives which output? "
                 "(red = increases, blue = decreases)")
    fig.tight_layout()
    fig.savefig(out_dir / "tornado.png", dpi=110)
    return sens_summary


def summarize(df: pd.DataFrame, sens_summary: dict, out_dir: Path) -> str:
    """Console summary: quantiles per output + strongest drivers."""
    L = ["", "===== LHS SUMMARY ====="]
    for col in ("heating_kwh_m2", "cooling_kwh_m2", "co2_kg_m2", "co2_t_building"):
        q = df[col].quantile
        L.append(f"{col:20s} mean {df[col].mean():7.2f} | median {q(0.5):7.2f} | "
                 f"P5 {q(0.05):7.2f} | P95 {q(0.95):7.2f}")
    L.append("\nTop-3 drivers (|Spearman rho|):")
    for col, top in sens_summary.items():
        pretty = ", ".join(f"{k} ({v:+.2f})" for k, v in top)
        L.append(f"  {col}: {pretty}")
    L.append(f"\nOutputs: {out_dir}/runs.csv | histograms.png | tornado.png")
    return "\n".join(L)

# ============================================================================
# 4) MAIN — CLI + orchestration
# ============================================================================

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="LHS uncertainty study on the pilot building "
                    "(10 variables, Spearman sensitivity).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--n", type=int, default=DEFAULT_N,
                   help="number of LHS runs (use 2 for a smoke test)")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help="sampler seed (42 = the frozen reference study)")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="output directory (default: <project>/out/lhs)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-v", "--verbose", action="store_true", help="debug-level output")
    g.add_argument("--quiet", action="store_true", help="warnings and errors only")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    try:
        mb.validate_input_files()                # fail fast on missing inputs
        out_dir = args.out_dir if args.out_dir is not None else mb.OUT_DIR / "lhs"
        if out_dir.exists():
            shutil.rmtree(out_dir)               # each study starts clean
        out_dir.mkdir(parents=True)

        samples = sample_matrix(args.n, args.seed)
        logger.info("[lhs] %d runs x %d variables (seed=%d)",
                    args.n, len(ALL_VARS), args.seed)

        df = run_study(samples, out_dir)
        df.to_csv(out_dir / "runs.csv", index=False)

        make_histograms(df, out_dir, args.n)
        sens_summary = make_tornado(df, out_dir)
        print(summarize(df, sens_summary, out_dir))
        return 0
    except Exception as exc:
        logger.error("Run failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
