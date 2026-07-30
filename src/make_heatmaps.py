"""Heat maps from pipeline results — building-level heating/cooling choropleths.

Scans the standard output folders (out/neighborhood, out/city) for
`results_buildings.gpkg` (written by neighborhood_pipeline / city_pipeline)
and renders a 2-panel heat map (heating / cooling kWh/m²) as `heatmap.png`
INTO THE SAME folder. Re-run after any pipeline run to refresh the maps:

    python src/make_heatmaps.py             # all standard folders found
    python src/make_heatmaps.py --out-dir out/city   # a single folder
    python src/make_heatmaps.py -v          # verbose logging

No EnergyPlus involved — takes seconds. The colour scale is clipped to the
P2–P98 percentile band so a handful of extreme VivUni values do not crush
the scale (noted on the figure).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt

import model_builder as mb

logger = logging.getLogger("make_heatmaps")

# Standard pipeline output folders (relative to the project's out/ dir)
STANDARD_DIRS = ("neighborhood", "city")
RESULT_GPKG = "results_buildings.gpkg"
PANELS = (("heating_kwh_m2", "Heating demand [kWh/m²·yr]", "YlOrRd"),
          ("cooling_kwh_m2", "Cooling demand [kWh/m²·yr]", "PuBu"))
CLIP_LO, CLIP_HI = 0.02, 0.98       # robust colour-scale percentiles
FRAME_QLO, FRAME_QHI = 0.02, 0.995  # map frame holds the central ~97% of buildings

def render_heatmap(gpkg: Path, png: Path) -> None:
    """Render one 2-panel (heating/cooling) building level heat map."""
    gdf = gpd.read_file(gpkg)
    n = len(gdf)
    heat_gwh = gdf["heating_kwh"].sum() / 1e6
    cool_gwh = gdf["cooling_kwh"].sum() / 1e6

    # frame on the central mass of buildings: the municipality includes far 
    # detached settlements (El Palmar strip ~15 km south) that would shrink
    # the urban core to unreadability; totals still include EVERY building
    cent = gdf.geometry.centroid
    pad = 400.0
    x0, x1 = cent.x.quantile(FRAME_QLO) - pad, cent.x.quantile(FRAME_QHI) + pad
    y0, y1 = cent.y.quantile(FRAME_QLO) - pad, cent.y.quantile(FRAME_QHI) + pad
    n_out = int((~(cent.x.between(x0, x1) & cent.y.between(y0, y1))).sum())

    # panel size follows the frame's aspect ratio
    aspect = (y1 - y0) / max(x1 - x0, 1e-9)
    panel_h = 9.0
    panel_w = max(3.0, panel_h / max(aspect, 0.4))
    fig, axes = plt.subplots(1, 2, figsize=(2 * panel_w + 3.0, panel_h + 1.2))
    for ax, (col, title, cmap) in zip(axes, PANELS):
        vmin = gdf[col].quantile(CLIP_LO)
        vmax = gdf[col].quantile(CLIP_HI)
        gdf.plot(column=col, ax=ax, cmap=cmap, vmin=vmin, vmax=vmax,
                 linewidth=0.05, edgecolor="grey",
                 legend=True, legend_kwds={"shrink": 0.6, "label": title})
        ax.set_title(title)
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        ax.set_axis_off()
    frame_note = (f"; {n_out:,} outlying buildings outside the frame "
                  f"(included in all totals)" if n_out else "")
    fig.suptitle(f"{png.parent.name}: {n:,} buildings — "
                 f"heating {heat_gwh:.2f} / cooling {cool_gwh:.2f} GWh/yr "
                 f"(colour scale clipped to P2–P98{frame_note})", fontsize=13)
    fig.tight_layout()
    fig.savefig(png, dpi=200, bbox_inches="tight")
    plt.close(fig)
    logger.info("[map] %s (%s buildings)", png, f"{n:,}")

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out-dir", type=Path, default=None,
                   help="render a single folder (must contain results_buildings.gpkg); "
                        "default: scan the standard out/ folders")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--quiet", action="store_true", help="warnings only")
    return p.parse_args(argv)

def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet
                                                else logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")

    if args.out_dir is not None:
        targets = [args.out_dir]
    else:
        targets = [mb.OUT_DIR / d for d in STANDARD_DIRS]

    made = 0
    for folder in targets:
        gpkg = folder / RESULT_GPKG
        if not gpkg.exists():
            logger.info("[skip] no %s in %s", RESULT_GPKG, folder)
            continue
        render_heatmap(gpkg, folder / "heatmap.png")
        made += 1

    if made == 0:
        logger.error("No results_buildings.gpkg found — run a pipeline first.")
        return 2
    logger.info("Done: %d heat map(s).", made)
    return 0

if __name__ == "__main__":
    sys.exit(main())
