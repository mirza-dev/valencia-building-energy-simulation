"""The run's result as a picture, for the reader who will not open QGIS.

`results_layer.py` writes the map somebody can style themselves; this writes
the one that goes into a slide, an email or a report without anybody installing
anything.  They answer different questions and neither replaces the other: a
PNG is one person's styling frozen, which is exactly what makes it portable and
exactly what makes it unsuitable for exploring.

Deliberately not `make_heatmaps.py`.  That module renders the same idea for the
retired representative-cluster pipelines, and `city_adapter.py:64` records its
source hash as part of a capability - editing it to also serve the per-building
runner would force a capability re-acceptance for a route that gains nothing
from the change.  It also expects `heating_kwh_m2`, a column the per-building
ledger does not have (it reports `space_heating_kwh_m2`), so the two would have
to learn about each other's schemas.  New path, new module; the old one keeps
its hash.

The one rule carried across from the layer: a building with no result is drawn
grey, not omitted.  A missing polygon on a printed map is indistinguishable
from open ground, and on this stock the gaps are large enough to change what
the picture says (Lecco screens out 1,145 of 5,152).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

MAP_FILENAME = "results_heatmap.png"

# Site consumption first because it is the number the rest are components of.
# The unit is not baked into the label: an annual run and a microclimate event
# run write the same columns, and until 2026-08-12 an 8-day event was drawn
# under `kWh/m²·yr`.  The caller passes what the numbers are per.
PANELS = (
    ("total_site_kwh_m2", "Total site energy", "YlOrRd"),
    ("space_heating_kwh_m2", "Space heating", "OrRd"),
    ("cooling_kwh_m2", "Cooling", "PuBu"),
)
DEFAULT_UNIT = "kWh/m²·yr"
# Said on the panels when the period could not be established, so that a
# picture never silently claims the annual basis it was not given.
UNKNOWN_UNIT = "period unknown"

# The colour scale is clipped rather than stretched: a handful of extreme
# buildings would otherwise compress every other building into one shade, and
# the map would say "they are all the same" about a stock that is not.
CLIP_LO, CLIP_HI = 0.02, 0.98
# The frame holds the central mass for the same reason - an outlying building
# kilometres away would shrink the built-up part to unreadability - but every
# building stays in the totals, and the caption says how many fell outside.
FRAME_LO, FRAME_HI = 0.005, 0.995
MISSING_COLOUR = "#cdcdcd"


def write_heatmap(frame, out_dir: Path, title: str = "",
                  unit: str | None = None) -> dict:
    """Render the panels beside the layer; never raise.

    Returns a block for `aggregate.json`, in the same shape and with the same
    contract as `results_layer.write_results_layer`: report what was drawn, say
    plainly when nothing could be, and never cost the caller its own output.

    `unit` says what the per-area numbers are per, and is written onto every
    panel.  It is not optional information: the same three columns hold annual
    intensities in one run and event-window totals in another.
    """
    block: dict[str, Any] = {"written": False, "image": MAP_FILENAME}
    # `None` is not "annual", it is "the caller could not establish the
    # period" - a ledger holding more than one run mode refuses to guess, and
    # defaulting it here would assert on the picture exactly what the caller
    # declined to assert (review finding, 2026-08-12).
    unit = unit or UNKNOWN_UNIT
    block["unit"] = unit
    if frame is None or len(frame) == 0:
        block["reason"] = "no_features"
        return block

    # A panel whose values are all the same carries no information, and
    # matplotlib will invent a range around the constant to colour it with -
    # measured on an August event run, where every building's space heating is
    # 0.0 and the colour bar advertised ±0.1.  Dropped, and the omission is
    # reported so it cannot be mistaken for a column that was never there.
    panels, flat = [], {}
    for spec in PANELS:
        if spec[0] not in frame.columns:
            continue
        values = frame[spec[0]].dropna()
        if values.empty:
            continue
        if values.nunique() < 2:
            flat[spec[0]] = float(values.iloc[0])
            continue
        panels.append(spec)
    if flat:
        block["panels_omitted_constant"] = flat
    if not panels:
        block["reason"] = "no_varying_energy_columns" if flat else "no_energy_columns"
        return block

    try:
        import matplotlib
        matplotlib.use("Agg")                      # no display on a worker
        import matplotlib.pyplot as plt

        drawn = frame[frame.geometry.notna() & ~frame.geometry.is_empty]
        if drawn.empty:
            block["reason"] = "no_geometry"
            return block

        centres = drawn.geometry.representative_point()
        pad = 400.0
        x0 = float(centres.x.quantile(FRAME_LO)) - pad
        x1 = float(centres.x.quantile(FRAME_HI)) + pad
        y0 = float(centres.y.quantile(FRAME_LO)) - pad
        y1 = float(centres.y.quantile(FRAME_HI)) + pad
        outside = int((~(centres.x.between(x0, x1)
                         & centres.y.between(y0, y1))).sum())

        # The panel is sized to the map's own proportions - equal-axis plotting
        # will not stretch geography to fill a box, so a panel wider than the
        # ground it shows is whitespace, not margin.  The allowance is for the
        # colour bar, which takes its width out of the axes.
        height = 8.0
        span = (x1 - x0) / max(y1 - y0, 1e-9)
        width = min(max(height * span, 3.2), height * 2.5) * 1.02
        figure, axes = plt.subplots(
            1, len(panels), figsize=(len(panels) * width, height + 0.9),
            layout="constrained")
        if len(panels) == 1:
            axes = [axes]

        for axis, (column, name, cmap) in zip(axes, panels):
            label = f"{name} [{unit}]"
            values = drawn[column].dropna()
            drawn.plot(column=column, ax=axis, cmap=cmap,
                       vmin=float(values.quantile(CLIP_LO)),
                       vmax=float(values.quantile(CLIP_HI)),
                       linewidth=0.04, edgecolor="#8a8a8a", legend=True,
                       legend_kwds={"shrink": 0.55, "label": label},
                       missing_kwds={"color": MISSING_COLOUR,
                                     "edgecolor": "#8a8a8a",
                                     "linewidth": 0.04,
                                     "label": "no result"})
            axis.set_title(label, fontsize=11)
            axis.set_xlim(x0, x1)
            axis.set_ylim(y0, y1)
            axis.set_axis_off()

        total = drawn[panels[0][0]].notna().sum()
        gaps = int(len(drawn) - total)
        caption = (f"{title or out_dir.name}: {len(drawn):,} buildings, "
                   f"{total:,} with a result")
        if gaps:
            caption += f", {gaps:,} drawn grey (no result)"
        caption += f" — colour scale clipped to P{CLIP_LO:.0%}–P{CLIP_HI:.0%}"
        if outside:
            caption += f"; {outside:,} outside the frame, still in every total"
        if unit == UNKNOWN_UNIT:
            caption += ("\nthe energy period could not be established from the "
                        "ledger — do not read these as annual")
        elif unit != DEFAULT_UNIT:
            # Said on the picture, not only in the legend: a panel cropped into
            # a slide keeps its title but loses the caption's neighbours.
            caption += f"\nenergy is {unit} — not annual"
        if flat:
            caption += ("; omitted (constant across the stock): "
                        + ", ".join(f"{c} = {v:g}" for c, v in flat.items()))
        figure.suptitle(caption, fontsize=12)

        target = Path(out_dir) / MAP_FILENAME
        staging = target.with_name(f"{target.stem}.partial{target.suffix}")
        # `constrained_layout` places the colour bars; cropping to the drawn
        # content afterwards is compatible with it (it is `tight_layout`
        # that fights it) and removes the band a diagonal district leaves
        # under its own bounding box.
        figure.savefig(staging, dpi=170, bbox_inches="tight", pad_inches=0.15)
        plt.close(figure)
        staging.replace(target)
    except Exception as exc:                        # noqa: BLE001
        block["reason"] = f"render_failed: {type(exc).__name__}: {str(exc)[:200]}"
        return block

    block.update({"written": True,
                  "panels": [column for column, _, _ in panels],
                  "buildings_drawn": int(len(drawn)),
                  "buildings_without_result": gaps,
                  "bytes": int(target.stat().st_size)})
    return block
