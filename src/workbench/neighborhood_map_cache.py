"""Validated, content-addressed GeoJSON resources for the Part C map."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from workbench import db, integrity


FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


def _coordinates(geometry: dict[str, Any]) -> Iterator[tuple[float, float]]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"Neighborhood map geometry must be Polygon or MultiPolygon, got {geometry_type!r}")
    if not isinstance(coordinates, (list, tuple)) or not coordinates:
        raise ValueError("Neighborhood map geometry is empty")

    stack: list[Any] = [coordinates]
    found = False
    while stack:
        value = stack.pop()
        if (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value[:2])
        ):
            x, y = float(value[0]), float(value[1])
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("Neighborhood map contains a non-finite coordinate")
            found = True
            yield x, y
        elif isinstance(value, (list, tuple)):
            stack.extend(reversed(value))
        else:
            raise ValueError("Neighborhood map has malformed coordinates")
    if not found:
        raise ValueError("Neighborhood map geometry has no coordinate pairs")


def validate_feature_collection(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise ValueError("Neighborhood map must be a GeoJSON FeatureCollection")
    features = data["features"]
    if not features:
        raise ValueError("Neighborhood map contains no buildings")

    references: set[str] = set()
    feature_ids: set[str] = set()
    duplicate_references = 0
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError(f"Neighborhood map feature {index} is malformed")
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise ValueError(f"Neighborhood map feature {index} has no properties")
        reference = str(properties.get("refparcela") or "").strip()
        if not reference:
            raise ValueError(f"Neighborhood map feature {index} has no refparcela")
        if reference in references:
            duplicate_references += 1
        references.add(reference)
        feature_id = str(feature.get("id") or reference).strip()
        if not feature_id:
            raise ValueError(f"Neighborhood map feature {index} has no stable feature id")
        if feature_id in feature_ids:
            raise ValueError(f"Neighborhood map has duplicate feature id: {feature_id}")
        feature_ids.add(feature_id)
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError(f"Neighborhood map feature {reference} has no geometry")
        for x, y in _coordinates(geometry):
            if not -180 <= x <= 180 or not -90 <= y <= 90:
                raise ValueError("Neighborhood map coordinates are not EPSG:4326 longitude/latitude")
            min_x, min_y = min(min_x, x), min(min_y, y)
            max_x, max_y = max(max_x, x), max(max_y, y)

    return {
        "feature_count": len(features),
        "unique_refparcela_count": len(references),
        "duplicate_refparcela_count": duplicate_references,
        "bounds": [min_x, min_y, max_x, max_y],
        "crs": "EPSG:4326",
    }


def _cache_root() -> Path:
    return db.VAR_DIR / "cache" / "neighborhood" / "maps"


def cache_feature_collection(data: dict[str, Any]) -> dict[str, Any]:
    summary = validate_feature_collection(data)
    payload = integrity.canonical_json_bytes(data)
    fingerprint = hashlib.sha256(payload).hexdigest()
    root = _cache_root()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{fingerprint}.geojson"
    if destination.exists():
        if integrity.sha256_file(destination) != fingerprint:
            raise IOError(f"Neighborhood map cache is corrupted: {fingerprint}")
    else:
        handle, temporary_name = tempfile.mkstemp(prefix=f".{fingerprint}-", suffix=".tmp", dir=root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            destination.chmod(0o444)
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    return summary | {
        "fingerprint": fingerprint,
        "sha256": fingerprint,
        "size_bytes": len(payload),
        "url": f"/api/neighborhood/maps/{fingerprint}.geojson",
    }


def descriptor_for_file(path: Path, *, url: str) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = validate_feature_collection(data)
    fingerprint = integrity.sha256_file(path)
    return summary | {
        "fingerprint": fingerprint,
        "sha256": fingerprint,
        "size_bytes": path.stat().st_size,
        "url": url,
    }


def cached_map_path(fingerprint: str) -> Path:
    if not FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("Neighborhood map fingerprint must be a 64-character SHA-256")
    path = _cache_root() / f"{fingerprint}.geojson"
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(fingerprint)
    if integrity.sha256_file(path) != fingerprint:
        raise IOError(f"Neighborhood map cache is corrupted: {fingerprint}")
    return path
