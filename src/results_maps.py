"""Publication-oriented heat maps for per-building stock results.

The GeoPackage is the analytical GIS product. This module freezes one
auditable visual interpretation for reports and slides. It never changes an
energy value: it reads the settled building layer, applies a disclosed P2-P98
display scale, keeps every geometry in exactly one geographic view, and writes
the scale and coverage evidence beside the PNG.

The geographic split is data-driven. A Euclidean proximity graph is built
from representative points in the projected CRS; its largest connected
component becomes the main view and distant components become at most two
insets. No city, district, run, or reference name is hard-coded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MAP_FILENAME = "results_heatmap.png"
METADATA_FILENAME = "results_heatmap.json"

HEATMAP_EVIDENCE_VERSION = "bsew-heatmap-evidence-v1"
HEATMAP_PANEL_STATISTICS_FIELDS = (
    "min", "p02", "p50", "p98", "max", "below_scale", "above_scale",
    "valid", "missing",
)
HEATMAP_STATUS_CLASSES = (
    "successful", "excluded", "failed", "metric_missing",
)

PANELS = (
    ("total_site_kwh_m2", "Total site energy", "YlOrRd"),
    ("space_heating_kwh_m2", "Space heating", "OrRd"),
    ("cooling_kwh_m2", "Cooling", "PuBu"),
)
DEFAULT_UNIT = "kWh/m²·yr"
UNKNOWN_UNIT = "period unknown"
DENOMINATOR = "whole-site energy / geometric residential floor area"
CLIP_LO, CLIP_HI = 0.02, 0.98

_STATUS_STYLE = {
    "excluded": {
        "label": "Excluded before simulation", "facecolor": "#707070",
        "edgecolor": "#151515", "hatch": "///", "linewidth": 0.25,
    },
    "failed": {
        "label": "Simulation/QA failure", "facecolor": "#ffffff",
        "edgecolor": "#151515", "hatch": "xxx", "linewidth": 0.35,
    },
    "metric_missing": {
        "label": "Result recorded, metric missing", "facecolor": "#b5b5b5",
        "edgecolor": "#151515", "hatch": "...", "linewidth": 0.25,
    },
}


def _finite_series(series):
    """Numeric finite values only; booleans and infinities are not evidence."""
    import numpy as np
    import pandas as pd

    # ``bool`` is an ``int`` subclass in Python.  Mask it explicitly before
    # numeric coercion so True/False can never become publishable 1/0 data.
    masked = series.map(
        lambda value: float("nan") if isinstance(value, (bool, np.bool_))
        else value)
    values = pd.to_numeric(masked, errors="coerce")
    values = values.where(np.isfinite(values))
    return values


def _status_class(frame, column: str):
    """Return one explicit evidence class per geometry for this metric."""
    import pandas as pd

    status = (frame["status"].astype(str).str.lower()
              if "status" in frame.columns
              else pd.Series("ok", index=frame.index))
    values = (_finite_series(frame[column]) if column in frame.columns
              else pd.Series(float("nan"), index=frame.index))
    classes = pd.Series("failed", index=frame.index, dtype="object")
    classes.loc[status.eq("excluded")] = "excluded"
    classes.loc[status.eq("ok") & values.notna()] = "successful"
    classes.loc[status.eq("ok") & values.isna()] = "metric_missing"
    return classes, values


def _union_find_components(xy, link_distance: float) -> list[list[int]]:
    """Connected components of the metric-coordinate proximity graph.

    Delaunay edges are sufficient for Euclidean connectivity and keep the
    graph linear-sized. The fallback handles two points, collinear points and
    numerically degenerate inputs without changing the scientific data.
    """
    import numpy as np
    from scipy.spatial import Delaunay, QhullError, cKDTree

    count = len(xy)
    if count < 2:
        return [list(range(count))]
    parent = list(range(count))

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    edges: set[tuple[int, int]] = set()
    try:
        simplices = Delaunay(xy).simplices
        for triangle in simplices:
            for left, right in ((triangle[0], triangle[1]),
                                (triangle[1], triangle[2]),
                                (triangle[2], triangle[0])):
                a, b = sorted((int(left), int(right)))
                edges.add((a, b))
    except (QhullError, ValueError):
        edges.update(map(tuple, cKDTree(xy).query_pairs(
            link_distance, output_type="ndarray")))

    threshold2 = link_distance * link_distance
    for left, right in edges:
        delta = xy[left] - xy[right]
        if float(np.dot(delta, delta)) <= threshold2:
            union(left, right)

    groups: dict[int, list[int]] = {}
    for item in range(count):
        groups.setdefault(find(item), []).append(item)
    return sorted(groups.values(), key=lambda group: (-len(group), group[0]))


def _split_satellites(components: list[list[int]], xy,
                      link_distance: float) -> list[list[int]]:
    """Combine distant components into at most two deterministic inset views."""
    import numpy as np

    satellites = components[1:]
    if not satellites:
        return []
    if len(satellites) == 1:
        return [satellites[0]]
    centres = np.array([xy[group].mean(axis=0) for group in satellites])
    weights = np.array([len(group) for group in satellites], dtype=float)
    centred = centres - np.average(centres, axis=0, weights=weights)
    try:
        axis = np.linalg.svd(centred, full_matrices=False)[2][0]
    except np.linalg.LinAlgError:
        axis = np.array([0.0, 1.0])
    projection = centred @ axis
    order = np.argsort(projection, kind="stable")
    ordered = projection[order]
    gaps = np.diff(ordered)
    if not len(gaps) or float(gaps.max()) <= link_distance * 1.5:
        return [[item for group in satellites for item in group]]
    split = int(gaps.argmax()) + 1
    sides = (order[:split], order[split:])
    return [sorted(item for component_index in side
                   for item in satellites[int(component_index)])
            for side in sides if len(side)]


def geographic_views(frame) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Assign every valid geometry to one and only one data-driven view."""
    import numpy as np
    from scipy.spatial import cKDTree

    centres = frame.geometry.representative_point()
    xy = np.column_stack((centres.x.to_numpy(), centres.y.to_numpy()))
    count = len(xy)
    if count < 2:
        link_distance = 0.0
        groups = [list(range(count))]
    else:
        nearest = cKDTree(xy).query(xy, k=2)[0][:, 1]
        finite = nearest[np.isfinite(nearest)]
        p99 = float(np.quantile(finite, 0.99)) if len(finite) else 0.0
        link_distance = max(250.0, min(1200.0, p99 * 20.0))
        components = _union_find_components(xy, link_distance)
        # A road, river or sparse industrial belt can break a component while
        # leaving it inside the main urban envelope. Keep those fragments in
        # the main view; only components whose centres fall outside the main
        # component's own extent become insets. This prevents an inset from
        # spanning a central fragment and a genuinely distant settlement.
        main = list(components[0])
        main_xy = xy[main]
        x0, y0 = main_xy.min(axis=0)
        x1, y1 = main_xy.max(axis=0)
        remote: list[list[int]] = []
        for component in components[1:]:
            centre = xy[component].mean(axis=0)
            if x0 <= centre[0] <= x1 and y0 <= centre[1] <= y1:
                main.extend(component)
            else:
                remote.append(component)
        grouped_components = [sorted(main), *remote]
        groups = [grouped_components[0],
                  *_split_satellites(grouped_components, xy, link_distance)]

    assigned = [item for group in groups for item in group]
    if sorted(assigned) != list(range(count)):
        raise ValueError("geographic view assignment is not a complete partition")

    views = []
    for number, group in enumerate(groups):
        subset = frame.iloc[group]
        bounds = subset.total_bounds
        span_x = float(bounds[2] - bounds[0])
        span_y = float(bounds[3] - bounds[1])
        pad = max(120.0, max(span_x, span_y) * 0.035)
        districts: list[str] = []
        if "nombre" in subset.columns:
            counts = subset["nombre"].dropna().astype(str).value_counts()
            districts = [str(name) for name in counts.index[:3]]
        if number == 0:
            label = "Main urban extent"
        elif districts:
            label = " / ".join(districts[:2])
        else:
            label = f"Distant stock group {number}"
        views.append({
            "id": "main" if number == 0 else f"inset_{number}",
            "label": label,
            "buildings": int(len(subset)),
            "districts": districts,
            "indices": list(map(int, group)),
            "extent": [float(bounds[0] - pad), float(bounds[1] - pad),
                       float(bounds[2] + pad), float(bounds[3] + pad)],
        })
    evidence = {
        "method": "Delaunay proximity components grouped into at most two insets",
        "link_distance_m": round(link_distance, 3),
        "outside_frame": 0,
        "views": [{key: value for key, value in view.items()
                   if key != "indices"} for view in views],
    }
    return views, evidence


def _panel_statistics(frame, column: str) -> dict[str, Any]:
    classes, values = _status_class(frame, column)
    valid = values[classes.eq("successful")]
    missing = int((~classes.eq("successful")).sum())
    if valid.empty:
        return {field: (0 if field in {"below_scale", "above_scale",
                                      "valid", "missing"} else None)
                for field in HEATMAP_PANEL_STATISTICS_FIELDS} | {
                    "missing": missing}
    p02 = float(valid.quantile(CLIP_LO))
    p98 = float(valid.quantile(CLIP_HI))
    return {
        "min": float(valid.min()), "p02": p02,
        "p50": float(valid.quantile(0.5)), "p98": p98,
        "max": float(valid.max()),
        "below_scale": int((valid < p02).sum()),
        "above_scale": int((valid > p98).sum()),
        "valid": int(valid.notna().sum()), "missing": missing,
    }


def _profile_fingerprint(frame) -> str | None:
    if "profile_fingerprint" not in frame.columns:
        return None
    values = sorted({str(value) for value in frame["profile_fingerprint"].dropna()
                     if str(value).strip()})
    return values[0] if len(values) == 1 else None


def _period_label(unit: str, period: dict[str, Any] | None) -> str:
    if period and period.get("period") == "annual":
        return "Annual simulation"
    if period and period.get("period") == "microclimate_event":
        days = period.get("event_days")
        return f"{days}-day microclimate event" if days else "Microclimate event"
    if unit == DEFAULT_UNIT:
        return "Annual simulation"
    if unit == UNKNOWN_UNIT:
        return "Simulation period not recorded"
    return str(unit)


def _draw_status(axis, subset, classes) -> None:
    for status in ("excluded", "failed", "metric_missing"):
        indices = classes[classes.eq(status)].index
        layer = subset.loc[subset.index.intersection(indices)]
        if layer.empty:
            continue
        style = _STATUS_STYLE[status]
        layer.plot(ax=axis, facecolor=style["facecolor"],
                   edgecolor=style["edgecolor"], hatch=style["hatch"],
                   linewidth=style["linewidth"], zorder=4)


def _district_outlines(axis, subset) -> int:
    if "nombre" not in subset.columns:
        return 0
    labelled = subset[subset["nombre"].notna() &
                      subset["nombre"].astype(str).str.strip().ne("")]
    if labelled.empty:
        return 0
    count = int(labelled["nombre"].nunique())
    for _, group in labelled.groupby("nombre"):
        try:
            envelope = group.geometry.buffer(45.0).union_all().buffer(-45.0)
            if not envelope.is_empty:
                boundary = subset.iloc[:0].copy()
                boundary.loc[0, "geometry"] = envelope
                boundary.set_geometry("geometry").boundary.plot(
                    ax=axis, color="#343434", linewidth=0.34, zorder=5)
        except Exception:  # noqa: BLE001 - outlines may never cost the map
            continue
    return count


def write_heatmap(frame, out_dir: Path, title: str = "",
                  unit: str | None = None,
                  period: dict[str, Any] | None = None) -> dict:
    """Render a publication PNG and evidence JSON; never raise."""
    block: dict[str, Any] = {"written": False, "image": MAP_FILENAME,
                             "metadata": METADATA_FILENAME}
    unit = unit or UNKNOWN_UNIT
    block["unit"] = unit
    if frame is None or len(frame) == 0:
        block["reason"] = "no_features"
        return block

    flat: dict[str, float] = {}
    panels: list[tuple[str, str, str]] = []
    for spec in PANELS:
        column = spec[0]
        if column not in frame.columns:
            continue
        classes, values = _status_class(frame, column)
        valid = values[classes.eq("successful")]
        if valid.empty:
            continue
        if valid.nunique() < 2:
            flat[column] = float(valid.iloc[0])
            continue
        panels.append(spec)
    if flat:
        block["panels_omitted_constant"] = flat
    if not panels:
        block["reason"] = ("no_varying_energy_columns" if flat
                           else "no_energy_columns")
        return block

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.cm import ScalarMappable
        from matplotlib.colors import Normalize
        from matplotlib.patches import Patch

        drawn = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
        if drawn.empty:
            block["reason"] = "no_geometry"
            return block
        views, geography = geographic_views(drawn)
        panel_stats = {column: _panel_statistics(drawn, column)
                       for column, _, _ in panels}
        raw_status = (drawn["status"].astype(str).str.lower()
                      if "status" in drawn.columns else None)
        ok_count = (int(raw_status.eq("ok").sum())
                    if raw_status is not None else len(drawn))
        metric_missing = max(
            (panel_stats[column]["missing"]
             - (int(raw_status.eq("excluded").sum())
                if raw_status is not None else 0)
             - (int((~raw_status.isin(("ok", "excluded"))).sum())
                if raw_status is not None else 0)
             for column, _, _ in panels), default=0)
        status_counts = {
            "successful": max(0, ok_count - metric_missing),
            "excluded": (int(raw_status.eq("excluded").sum())
                         if raw_status is not None else 0),
            "failed": (int((~raw_status.isin(("ok", "excluded"))).sum())
                       if raw_status is not None else 0),
            "metric_missing": metric_missing,
        }

        figure = plt.figure(figsize=(19.8, 10.6), layout="constrained")
        figure.get_layout_engine().set(rect=(0.015, 0.13, 0.97, 0.80),
                                       h_pad=0.035, w_pad=0.035)
        subfigures = figure.subfigures(1, len(panels), wspace=0.035)
        if len(panels) == 1:
            subfigures = [subfigures]
        district_count = 0
        for subfigure, (column, name, cmap) in zip(subfigures, panels):
            if len(views) == 1:
                axes = [subfigure.subplots(1, 1)]
            else:
                grid = subfigure.add_gridspec(2, 2,
                                              height_ratios=(4.2, 1.75))
                axes = [subfigure.add_subplot(grid[0, :])]
                if len(views) == 2:
                    axes.append(subfigure.add_subplot(grid[1, :]))
                else:
                    axes.extend((subfigure.add_subplot(grid[1, 0]),
                                 subfigure.add_subplot(grid[1, 1])))
            stats = panel_stats[column]
            norm = Normalize(vmin=stats["p02"], vmax=stats["p98"])
            classes, values = _status_class(drawn, column)
            drawn[f"__{column}"] = values
            for axis, view in zip(axes, views):
                subset = drawn.iloc[view["indices"]]
                good = subset.loc[classes.loc[subset.index].eq("successful")]
                if not good.empty:
                    good.plot(column=f"__{column}", ax=axis, cmap=cmap,
                              norm=norm, linewidth=0.025,
                              edgecolor="#6b6b6b", zorder=2)
                _draw_status(axis, subset, classes)
                district_count = max(district_count,
                                     _district_outlines(axis, subset))
                x0, y0, x1, y1 = view["extent"]
                axis.set_xlim(x0, x1)
                axis.set_ylim(y0, y1)
                axis.set_aspect("equal")
                axis.set_axis_off()
                axis.set_title(
                    f"{view['label']} · {view['buildings']:,} buildings",
                    fontsize=7.4 if view["id"] != "main" else 8.5,
                    fontweight="bold", pad=3)
            colourbar = subfigure.colorbar(
                ScalarMappable(norm=norm, cmap=cmap), ax=axes,
                orientation="horizontal", fraction=0.035, pad=0.025,
                aspect=32, extend="both")
            colourbar.set_label(
                f"{name} [{unit}] · P2 {stats['p02']:.3g} · "
                f"P98 {stats['p98']:.3g} · {stats['below_scale']:,} below / "
                f"{stats['above_scale']:,} above", fontsize=7.4)
            colourbar.ax.tick_params(labelsize=7)
            subfigure.suptitle(f"{name}\n{DENOMINATOR}", fontsize=10.6,
                               fontweight="bold")

        legend_handles = [
            Patch(facecolor="#f1b642", edgecolor="#444444",
                  label="Successful result"),
            *[Patch(facecolor=style["facecolor"],
                    edgecolor=style["edgecolor"], hatch=style["hatch"],
                    label=style["label"]) for style in _STATUS_STYLE.values()],
        ]
        figure.legend(handles=legend_handles, loc="lower center", ncol=4,
                      frameon=False, fontsize=8.2,
                      bbox_to_anchor=(0.5, 0.018))
        period_label = _period_label(unit, period)
        caption = (f"{title or Path(out_dir).name} · {period_label} · "
                   f"{len(drawn):,} buildings · status coverage: "
                   f"{status_counts['successful']:,} successful, "
                   f"{status_counts['excluded']:,} excluded, "
                   f"{status_counts['failed']:,} failed")
        if flat:
            caption += (" · omitted constant panel(s): "
                        + ", ".join(f"{key} = {value:g}"
                                    for key, value in flat.items()))
        figure.suptitle(caption, fontsize=12.4, fontweight="bold")
        profile = _profile_fingerprint(drawn)
        crs = None if drawn.crs is None else str(drawn.crs)
        footer = ("P2-P98 controls display colour only; values outside the "
                  "displayed range retain their original values. "
                  f"CRS {crs or 'not recorded'} · profile "
                  f"{(profile[:16] + '…') if profile else 'not uniquely recorded'} · "
                  f"derived district extents: {district_count}. "
                  "simulated, not measured consumption.")
        figure.text(0.5, 0.072, footer, ha="center", va="bottom",
                    fontsize=7.6)

        target = Path(out_dir) / MAP_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(f"{target.stem}.partial{target.suffix}")
        figure.savefig(staging, dpi=190, bbox_inches="tight", pad_inches=0.16,
                       metadata={
                           "Title": f"{title or Path(out_dir).name} stock heat map",
                           "Author": "Mirza Saribiyik",
                           "Description": footer,
                       })
        plt.close(figure)
        staging.replace(target)

        evidence = {
            "schema": HEATMAP_EVIDENCE_VERSION,
            "run": title or Path(out_dir).name,
            "period": period or {"period": "unknown", "unit": unit},
            "unit": unit,
            "denominator": DENOMINATOR,
            "display_scale": {
                "lower_quantile": CLIP_LO, "upper_quantile": CLIP_HI,
                "values_are_not_modified": True,
            },
            "panels": panel_stats,
            "panels_omitted_constant": flat,
            "status_classes": status_counts,
            "geography": geography,
            "districts": district_count,
            "crs": crs,
            "profile_fingerprint": profile,
            "simulated_not_measured": True,
            "png": {
                "file": MAP_FILENAME, "bytes": int(target.stat().st_size),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            },
        }
        metadata_target = Path(out_dir) / METADATA_FILENAME
        metadata_target.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - map may never cost the aggregate
        block["reason"] = (f"render_failed: {type(exc).__name__}: "
                           f"{str(exc)[:200]}")
        return block

    block.update({
        "written": True,
        "panels": [column for column, _, _ in panels],
        "panels_omitted_constant": flat,
        "panel_statistics": panel_stats,
        "status_classes": status_counts,
        "buildings_drawn": int(len(drawn)),
        "buildings_without_result": int(status_counts["excluded"]
                                        + status_counts["failed"]
                                        + status_counts["metric_missing"]),
        "outside_frame": 0,
        "views": geography["views"],
        "denominator": DENOMINATOR,
        "crs": crs,
        "profile_fingerprint": profile,
        "bytes": int(target.stat().st_size),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    })
    return block
