from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import MultiPolygon, Polygon

from workbench import db, integrity, service


def _dataset(tmp_path: Path, monkeypatch, gdf: gpd.GeoDataFrame) -> str:
    monkeypatch.setattr(db, "VAR_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "workbench.sqlite3")
    monkeypatch.setattr(service, "IMPORT_ROOT", tmp_path / "imports")
    db.init_db()
    source = tmp_path / "contract.geojson"
    source.write_text("{}", encoding="utf-8")
    snapshot = integrity.ensure_snapshot(source, kind="gis")
    db.upsert_dataset({
        "id": "contract", "kind": "gis", "name": "Contract fixture",
        "path": str(source), "sha256": snapshot["snapshot_hash"],
        "snapshot_hash": snapshot["snapshot_hash"], "verification_status": "VERIFIED",
        "metadata": {"columns": list(gdf.columns), "crs": str(gdf.crs)},
    })
    monkeypatch.setattr(service.gpd, "read_file", lambda _path: gdf)
    return "contract"


def _codes(exc: pytest.ExceptionInfo[service.GISValidationError]) -> set[str]:
    return {item["code"] for item in exc.value.errors}


def test_duplicate_id_and_fractional_floor_stop_normalization(tmp_path, monkeypatch):
    square = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)])
    gdf = gpd.GeoDataFrame(
        {"building": ["A", "A"], "floors": [2.5, 3]}, geometry=[square, square], crs=25830,
    )
    dataset_id = _dataset(tmp_path, monkeypatch, gdf)
    with pytest.raises(service.GISValidationError) as exc:
        service.normalize_gis_dataset(dataset_id, {
            "reference_field": "building", "floors_field": "floors", "cluster_field": None,
        })
    assert {"ID_DUPLICATE", "FLOOR_FRACTIONAL"} <= _codes(exc)


@pytest.mark.parametrize(
    ("geometry", "code"),
    [
        (Polygon([(0, 0), (4, 4), (0, 4), (4, 0)]), "GEOMETRY_INVALID"),
        (Polygon([(0, 0), (5, 0), (5, 5), (0, 5)], holes=[[(1, 1), (2, 1), (2, 2), (1, 2)]]), "GEOMETRY_HOLES"),
        (MultiPolygon([Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
                       Polygon([(3, 0), (5, 0), (5, 2), (3, 2)])]), "GEOMETRY_TYPE"),
    ],
)
def test_unsupported_geometry_requires_separate_review(tmp_path, monkeypatch, geometry, code):
    gdf = gpd.GeoDataFrame({"building": ["A"], "floors": [3]}, geometry=[geometry], crs=25830)
    dataset_id = _dataset(tmp_path, monkeypatch, gdf)
    with pytest.raises(service.GISValidationError) as exc:
        service.normalize_gis_dataset(dataset_id, {
            "reference_field": "building", "floors_field": "floors", "cluster_field": None,
        })
    assert code in _codes(exc)


def test_missing_crs_stops_normalization(tmp_path, monkeypatch):
    square = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)])
    gdf = gpd.GeoDataFrame({"building": ["A"], "floors": [3]}, geometry=[square])
    dataset_id = _dataset(tmp_path, monkeypatch, gdf)
    with pytest.raises(service.GISValidationError) as exc:
        service.normalize_gis_dataset(dataset_id, {
            "reference_field": "building", "floors_field": "floors", "cluster_field": None,
        })
    assert "CRS_MISSING" in _codes(exc)
