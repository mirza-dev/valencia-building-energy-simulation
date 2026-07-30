"""Convert the exact OpenStudio model to browser and report artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
os.environ.setdefault("MPLCONFIGDIR", str(PROJECT / "var/matplotlib"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Poly3DCollection


OBC_COLORS = {
    "Outdoors": "#348a9b",
    "Adiabatic": "#d34f5f",
    "Surface": "#74a66a",
    "Ground": "#9c7653",
}
CONTEXT_SHADING_DISABLED_SCHEDULE = "Workbench Context Shading Disabled"


def _vertices(obj) -> list[list[float]]:
    return [[round(v.x(), 5), round(v.y(), 5), round(v.z(), 5)] for v in obj.vertices()]


def _optional_name(optional) -> str | None:
    if optional.isNull():
        return None
    return optional.get().nameString()


def _optional_handle(optional) -> str | None:
    if optional.isNull():
        return None
    return str(optional.get().handle())


def _angle_distance(left: float, right: float) -> float:
    return abs((left - right + 180.0) % 360.0 - 180.0)


def _current_facade_qa(surfaces: list[dict[str, Any]], subsurfaces: list[dict[str, Any]],
                       baseline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recalculate glazing QA from the current authored OSM, retaining target WWRs."""
    if not baseline:
        return []
    groups = [{"template": item, "walls": []} for item in baseline]
    for surface in surfaces:
        if surface.get("surface_type") != "Wall" or surface.get("boundary_condition") != "Outdoors":
            continue
        # model_builder facade QA is intentionally residential-only. Ground-floor
        # buffer/service spaces remain visible in the editor but do not belong to
        # the calibrated dwelling glazing denominator.
        if "Vivienda" not in str(surface.get("space_type") or ""):
            continue
        index = min(range(len(groups)), key=lambda item: _angle_distance(
            float(surface.get("azimuth_deg") or 0.0), float(groups[item]["template"]["azimut"]),
        ))
        groups[index]["walls"].append(surface)
    output: list[dict[str, Any]] = []
    for group in groups:
        template = group["template"]
        wall_ids = {item["id"] for item in group["walls"]}
        openings = [item for item in subsurfaces if item.get("parent_id") in wall_ids]
        glass = [item for item in openings if item.get("subsurface_type") in {"FixedWindow", "OperableWindow", "GlassDoor"}]
        wall_area = sum(float(item.get("area_m2") or 0.0) for item in group["walls"])
        glass_area = sum(float(item.get("area_m2") or 0.0) for item in glass)
        window_count = sum(item.get("subsurface_type") in {"FixedWindow", "OperableWindow"} for item in glass)
        door_count = sum(item.get("subsurface_type") == "GlassDoor" for item in glass)
        unchanged = (
            round(wall_area, 1) == float(template.get("wall_m2") or 0.0)
            and round(glass_area, 1) == float(template.get("glass_m2") or 0.0)
            and window_count == int(template.get("window") or 0)
            and door_count == int(template.get("door") or 0)
        )
        if unchanged:
            output.append(dict(template))
            continue

        # Retain the pipeline's facade target area per unit wall area when an
        # authored geometry edit changes the denominator.
        template_wall = float(template.get("wall_m2") or 0.0)
        target_area = (
            float(template.get("target_m2") or 0.0) * wall_area / template_wall
            if template_wall else 0.0
        )
        target_ratio = target_area / wall_area if wall_area else 0.0
        output.append({
            "azimut": float(template["azimut"]),
            "wwr_target": round(target_ratio, 3),
            "wall_m2": round(wall_area, 1),
            "target_m2": round(target_area, 1),
            "glass_m2": round(glass_area, 1),
            "window": window_count,
            "door": door_count,
            "wwr_real": round(glass_area / wall_area, 3) if wall_area else 0.0,
            "lapse_pct": round(100.0 * (glass_area - target_area) / target_area, 1) if target_area else 0.0,
        })
    return output


def extract_scene(osm, stats: dict[str, Any]) -> dict[str, Any]:
    surfaces: list[dict[str, Any]] = []
    subsurfaces: list[dict[str, Any]] = []
    shading: list[dict[str, Any]] = []

    for surface in osm.getSurfaces():
        space_name = space_type_name = zone_name = story_name = None
        space_id = space_type_id = zone_id = story_id = None
        space = surface.space()
        if not space.isNull():
            sp = space.get()
            space_name = sp.nameString()
            space_id = str(sp.handle())
            space_type = sp.spaceType()
            zone = sp.thermalZone()
            story = sp.buildingStory()
            space_type_name = _optional_name(space_type)
            zone_name = _optional_name(zone)
            story_name = _optional_name(story)
            space_type_id = _optional_handle(space_type)
            zone_id = _optional_handle(zone)
            story_id = _optional_handle(story)
        construction = _optional_name(surface.construction())
        construction_id = _optional_handle(surface.construction())
        item = {
            "id": str(surface.handle()),
            "name": surface.nameString(),
            "category": "surface",
            "vertices": _vertices(surface),
            "surface_type": surface.surfaceType(),
            "boundary_condition": surface.outsideBoundaryCondition(),
            "construction": construction,
            "construction_id": construction_id,
            "area_m2": round(surface.grossArea(), 3),
            "azimuth_deg": round(float(surface.azimuth()) * 180.0 / np.pi, 2),
            "space": space_name,
            "space_id": space_id,
            "space_type": space_type_name,
            "space_type_id": space_type_id,
            "zone": zone_name,
            "zone_id": zone_id,
            "story": story_name,
            "story_id": story_id,
        }
        surfaces.append(item)
        for sub in surface.subSurfaces():
            subsurfaces.append({
                "id": str(sub.handle()),
                "parent_id": item["id"],
                "name": sub.nameString(),
                "category": "door" if sub.subSurfaceType() in {"Door", "GlassDoor"} else "window",
                "vertices": _vertices(sub),
                "subsurface_type": sub.subSurfaceType(),
                "construction": _optional_name(sub.construction()),
                "construction_id": _optional_handle(sub.construction()),
                "area_m2": round(sub.grossArea(), 3),
                "azimuth_deg": item["azimuth_deg"],
                "space": space_name,
                "space_id": space_id,
                "space_type": space_type_name,
                "space_type_id": space_type_id,
                "zone": zone_name,
                "zone_id": zone_id,
                "story": story_name,
                "story_id": story_id,
            })

    for shade in osm.getShadingSurfaces():
        group = shade.shadingSurfaceGroup()
        group_type = None if group.isNull() else group.get().shadingSurfaceType()
        group_name = None if group.isNull() else group.get().nameString()
        schedule = shade.transmittanceSchedule()
        if (
            group_type == "Site"
            and not schedule.isNull()
            and schedule.get().nameString() == CONTEXT_SHADING_DISABLED_SCHEDULE
        ):
            continue
        shading.append({
            "id": str(shade.handle()),
            "name": shade.nameString(),
            "category": "context" if group_type == "Site" else "overhang",
            "vertices": _vertices(shade),
            "group_type": group_type,
            "group_name": group_name,
            "area_m2": round(shade.grossArea(), 3),
        })

    return {
        "schema_version": 1,
        "refparcela": stats["refparcela"],
        "origin_epsg25830": [stats["origin_x"], stats["origin_y"]],
        "north_axis_deg": 0.0,
        "surfaces": surfaces,
        "subsurfaces": subsurfaces,
        "shading": shading,
        "facade_qa": _current_facade_qa(surfaces, subsurfaces, stats.get("facade_qa", [])),
    }


def extract_scene_from_path(osm_path: str | Path, stats: dict[str, Any]) -> dict[str, Any]:
    """Load a session OSM and regenerate the exact browser geometry after authored edits."""
    import openstudio

    translated = openstudio.osversion.VersionTranslator().loadModel(openstudio.toPath(str(Path(osm_path).resolve())))
    if translated.isNull():
        raise ValueError(f"OpenStudio could not load {Path(osm_path).name}")
    return extract_scene(translated.get(), stats)


def write_scene(scene: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8")


def render_scene_png(scene: dict[str, Any], path: Path) -> None:
    building = scene["surfaces"] + scene["subsurfaces"]
    all_items = building + scene["shading"]
    points = np.array([p for item in scene["surfaces"] for p in item["vertices"]])
    mins, maxs = points.min(axis=0), points.max(axis=0)
    center = (mins + maxs) / 2
    span = max(float((maxs - mins).max()) / 2 * 1.35, 5.0)
    views = [(25, -120), (25, -30), (25, 60), (88, -90)]

    fig = plt.figure(figsize=(15, 10), facecolor="#f4f5f2")
    for index, (elev, azim) in enumerate(views, 1):
        ax = fig.add_subplot(2, 2, index, projection="3d")
        ax.set_facecolor("#f4f5f2")
        for item in all_items:
            if item["category"] == "surface":
                color = OBC_COLORS.get(item["boundary_condition"], "#8a8e8b")
                alpha = 0.22 if item["boundary_condition"] == "Surface" else 0.72
            elif item["category"] == "window":
                color, alpha = "#39b9c6", 0.92
            elif item["category"] == "door":
                color, alpha = "#2365a8", 0.94
            elif item["category"] == "context":
                color, alpha = "#aeb5b0", 0.08
            else:
                color, alpha = "#565e5b", 0.82
            ax.add_collection3d(Poly3DCollection(
                [item["vertices"]], facecolor=color, alpha=alpha,
                edgecolor="#26312e", linewidth=0.35,
            ))
        ax.set_xlim(center[0] - span, center[0] + span)
        ax.set_ylim(center[1] - span, center[1] + span)
        ax.set_zlim(0, max(2 * span, maxs[2] * 1.12))
        ax.view_init(elev=elev, azim=azim)
        ax.set_box_aspect((1, 1, 0.8))
        ax.grid(False)
        ax.set_xlabel("x (m)")
        ax.set_ylabel("y / North (m)")
        ax.set_zlabel("z (m)")
    fig.suptitle(
        f"{scene['refparcela']} · exact OpenStudio geometry · boundary-condition view",
        fontsize=14, color="#17201d",
    )
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
