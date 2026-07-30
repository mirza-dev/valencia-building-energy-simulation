"""Immutable provenance for the browser renderer and its derived geometry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from workbench import integrity


PROJECT = Path(__file__).resolve().parents[2]
RENDERER_VERSION = "site-viewer/1.0.0"
DERIVED_GEOMETRY_ALGORITHM = "context-roof-loops/1"

_SOURCE_FILES = (
    "frontend/src/components/ModelViewer.tsx",
    "frontend/src/components/FeedbackProvider.tsx",
    "frontend/src/lib/sceneMassing.ts",
    "frontend/src/lib/types.ts",
    "frontend/src/lib/i18n.ts",
    "frontend/src/lib/locale.ts",
    "frontend/src/styles.css",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.app.json",
    "frontend/vite.config.ts",
)


def renderer_source_paths() -> list[Path]:
    """Return the authored viewer sources and the exact built assets served locally."""
    paths = [PROJECT / relative for relative in _SOURCE_FILES]
    dist = PROJECT / "frontend/dist"
    if dist.exists():
        paths.extend(
            path for path in sorted(dist.rglob("*"))
            if path.is_file() and path.name != ".DS_Store"
        )
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Renderer provenance source is missing: " + ", ".join(str(path) for path in missing)
        )
    return paths


def _relative_name(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT).as_posix()
    except ValueError:
        return path.name


def _point_key(point: list[float]) -> str:
    return f"{point[0]:.3f}:{point[1]:.3f}"


def _top_edge(item: dict[str, Any]) -> tuple[list[float], list[float], float] | None:
    vertices = item.get("vertices", [])
    if item.get("category") != "context" or len(vertices) < 4:
        return None
    height = max(float(vertex[2]) for vertex in vertices)
    top = [
        [float(value) for value in vertex]
        for vertex in vertices if abs(float(vertex[2]) - height) < 1e-3
    ]
    if len(top) != 2 or _point_key(top[0]) == _point_key(top[1]):
        return None
    return top[0], top[1], height


def _polygon_area(vertices: list[list[float]]) -> float:
    return abs(sum(
        vertex[0] * vertices[(index + 1) % len(vertices)][1]
        - vertices[(index + 1) % len(vertices)][0] * vertex[1]
        for index, vertex in enumerate(vertices)
    )) / 2.0


def _canonical_ring(vertices: list[list[float]]) -> list[list[float]]:
    rounded = [[round(float(value), 5) for value in vertex] for vertex in vertices]
    candidates: list[list[list[float]]] = []
    for sequence in (rounded, list(reversed(rounded))):
        candidates.extend(sequence[index:] + sequence[:index] for index in range(len(sequence)))
    return min(candidates, key=lambda item: integrity.canonical_json_bytes(item))


def derived_context_roofs(scene: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild deterministic visual roof loops from exact context-wall top edges."""
    groups: dict[str, list[tuple[list[float], list[float], float]]] = {}
    for item in scene.get("shading", []):
        edge = _top_edge(item)
        if edge is not None:
            groups.setdefault(f"{edge[2]:.3f}", []).append(edge)

    roofs: list[dict[str, Any]] = []
    for height_key in sorted(groups):
        edges = groups[height_key]
        unused = set(range(len(edges)))
        while unused:
            first_index = min(unused)
            unused.remove(first_index)
            first = edges[first_index]
            start_key = _point_key(first[0])
            current_key = _point_key(first[1])
            ring = [first[0], first[1]]

            while current_key != start_key and unused:
                next_index = next((
                    index for index in sorted(unused)
                    if _point_key(edges[index][0]) == current_key
                    or _point_key(edges[index][1]) == current_key
                ), None)
                if next_index is None:
                    break
                unused.remove(next_index)
                edge = edges[next_index]
                next_point = edge[1] if _point_key(edge[0]) == current_key else edge[0]
                ring.append(next_point)
                current_key = _point_key(next_point)

            if current_key != start_key or len(ring) < 4:
                continue
            ring.pop()
            area = _polygon_area(ring)
            if area < 0.1:
                continue
            canonical = _canonical_ring(ring)
            roofs.append({
                "height_m": round(float(height_key), 3),
                "vertices": canonical,
                "area_m2": round(area, 3),
            })
    return sorted(roofs, key=lambda roof: integrity.canonical_json_bytes(roof))


def visual_geometry_descriptor(scene: dict[str, Any]) -> dict[str, Any]:
    roofs = derived_context_roofs(scene)
    identity = {
        "algorithm": DERIVED_GEOMETRY_ALGORITHM,
        "context_roofs": roofs,
    }
    return identity | {
        "context_roof_count": len(roofs),
        "fingerprint": hashlib.sha256(integrity.canonical_json_bytes(identity)).hexdigest(),
    }


def _source_descriptor(paths: Iterable[Path], *, snapshot: bool) -> dict[str, Any]:
    sources = []
    for path in paths:
        item = {
            "path": _relative_name(path),
            "sha256": integrity.sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        if snapshot:
            item["snapshot_hash"] = integrity.ensure_snapshot(
                path, kind="renderer-source",
            )["snapshot_hash"]
        sources.append(item)
    sources.sort(key=lambda item: item["path"])
    identity = [{key: item[key] for key in ("path", "sha256", "size_bytes")} for item in sources]
    return {
        "sources": sources,
        "fingerprint": hashlib.sha256(integrity.canonical_json_bytes(identity)).hexdigest(),
    }


def current_source_descriptor(*, snapshot: bool = False) -> dict[str, Any]:
    return _source_descriptor(renderer_source_paths(), snapshot=snapshot)


def write_renderer_manifest(scene_path: Path, output_path: Path) -> dict[str, Any]:
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    source = current_source_descriptor(snapshot=True)
    manifest = {
        "schema_version": 1,
        "renderer_version": RENDERER_VERSION,
        "scene_sha256": integrity.sha256_file(scene_path),
        "source_fingerprint": source["fingerprint"],
        "sources": source["sources"],
        "visual_geometry": visual_geometry_descriptor(scene),
    }
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return manifest


def load_renderer_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_preview_renderer(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "renderer_manifest.json"
    scene_path = directory / "scene.json"
    if not manifest_path.exists():
        raise ValueError("Preview has no renderer provenance; rebuild the preview before commit")
    manifest = load_renderer_manifest(manifest_path)
    if manifest.get("renderer_version") != RENDERER_VERSION:
        raise ValueError("Renderer version changed after preview; rebuild before commit")
    if integrity.sha256_file(scene_path) != manifest.get("scene_sha256"):
        raise ValueError("Preview scene changed after renderer provenance was recorded")
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    visual = visual_geometry_descriptor(scene)
    if visual["fingerprint"] != manifest.get("visual_geometry", {}).get("fingerprint"):
        raise ValueError("Derived visual geometry no longer matches the preview")
    current = current_source_descriptor(snapshot=False)
    if current["fingerprint"] != manifest.get("source_fingerprint"):
        raise ValueError("Renderer changed after preview; rebuild before commit")
    snapshot_issues = verify_snapshot_references(manifest)
    if snapshot_issues:
        raise ValueError(
            "Renderer source snapshot is unavailable; rebuild the preview before commit: "
            + "; ".join(snapshot_issues)
        )
    return manifest


def verify_snapshot_references(manifest: dict[str, Any]) -> list[str]:
    issues = []
    for source in manifest.get("sources", []):
        try:
            files = integrity.snapshot_files(source["snapshot_hash"])
        except (KeyError, FileNotFoundError, IOError) as exc:
            issues.append(f"Renderer snapshot failed verification: {source.get('path', '?')} ({exc})")
            continue
        if len(files) != 1 or integrity.sha256_file(files[0][1]) != source.get("sha256"):
            issues.append(f"Renderer snapshot hash mismatch: {source.get('path', '?')}")
    return issues


def summary(directory: Path, *, current_fingerprint: str | None = None) -> dict[str, Any]:
    path = directory / "renderer_manifest.json"
    if not path.exists():
        return {"status": "UNVERSIONED", "current_match": False}
    manifest = load_renderer_manifest(path)
    if current_fingerprint is None:
        current_fingerprint = current_source_descriptor(snapshot=False)["fingerprint"]
    visual = manifest.get("visual_geometry", {})
    return {
        "status": "VERSIONED",
        "renderer_version": manifest.get("renderer_version"),
        "source_fingerprint": manifest.get("source_fingerprint"),
        "visual_geometry_fingerprint": visual.get("fingerprint"),
        "context_roof_count": visual.get("context_roof_count", 0),
        "current_match": manifest.get("source_fingerprint") == current_fingerprint,
    }
