from pathlib import Path
import zipfile

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from workbench import db, service


def _isolated_workbench(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "workbench.sqlite3")
    monkeypatch.setattr(db, "VAR_DIR", tmp_path)
    monkeypatch.setattr(service, "IMPORT_ROOT", tmp_path / "imports")
    monkeypatch.setattr(service, "PREVIEW_ROOT", tmp_path / "previews")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(service, "EXPORT_ROOT", tmp_path / "exports")
    service.bootstrap()


def test_field_mapping_creates_canonical_derivative_and_can_activate(tmp_path, monkeypatch):
    _isolated_workbench(tmp_path, monkeypatch)
    source = tmp_path / "external.geojson"
    gdf = gpd.GeoDataFrame(
        {"building_id": ["A-1"], "levels": [4], "stock_group": ["BlocPluriP04"]},
        geometry=[Polygon([(-0.39, 39.48), (-0.389, 39.48), (-0.389, 39.481), (-0.39, 39.481)])],
        crs=4326,
    )
    gdf.to_file(source, driver="GeoJSON")

    imported = service.import_dataset("gis", "External buildings", source)
    normalized = service.normalize_gis_dataset(imported["id"], {
        "reference_field": "building_id",
        "floors_field": "levels",
        "cluster_field": "stock_group",
    })
    normalized_path = Path(normalized["path"])
    result = gpd.read_file(normalized_path)

    assert normalized_path != source
    assert normalized["metadata"]["source_dataset_id"] == imported["id"]
    assert result.crs.to_epsg() == 25830
    assert result.loc[0, "refparcela"] == "A-1"
    assert result.loc[0, "altura_max"] == 4
    assert result.loc[0, "cluster"] == "BlocPluriP04"

    settings = service.set_project_settings({
        "building_dataset_id": normalized["id"],
        "neighbor_dataset_id": normalized["id"],
    })
    assert settings["building_dataset_id"] == normalized["id"]
    assert service.workbench_base_config().data.building_path == normalized_path

    alternate = service.normalize_gis_dataset(imported["id"], {
        "reference_field": "building_id",
        "floors_field": "levels",
        "cluster_field": None,
    })
    assert alternate["id"] != normalized["id"]
    assert Path(alternate["path"]) != normalized_path
    assert normalized_path.exists()


def test_geometry_actions_are_explicit_and_ordered():
    bow_tie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0)])
    assert bow_tie.is_valid is False
    assert service.apply_geometry_actions(bow_tie, [{"action": "make_valid", "approved": False}]) == bow_tie

    fixed_part = service.apply_geometry_actions(bow_tie, [
        {"action": "make_valid", "approved": True},
        {"action": "select_multipolygon_part", "part_index": 0, "approved": True},
    ])
    assert fixed_part.geom_type == "Polygon"
    assert fixed_part.is_valid is True


def test_zipped_shapefile_is_snapshotted_as_one_sidecar_set(tmp_path, monkeypatch):
    _isolated_workbench(tmp_path, monkeypatch)
    archive_path = tmp_path / "stock.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("nested/stock.shp", b"geometry")
        archive.writestr("nested/stock.dbf", b"attributes")
        archive.writestr("nested/stock.shx", b"index")
        archive.writestr("nested/stock.prj", b"projection")
        archive.writestr("nested/stock.cpg", b"UTF-8")
    dataset = service.import_dataset("gis", "Zipped stock", archive_path)
    assert Path(dataset["path"]).suffix == ".shp"
    assert {item["name"] for item in dataset["metadata"]["snapshot_components"]} == {
        "stock.shp", "stock.dbf", "stock.shx", "stock.prj", "stock.cpg",
    }


def test_zipped_shapefile_rejects_path_traversal(tmp_path, monkeypatch):
    _isolated_workbench(tmp_path, monkeypatch)
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../stock.shp", b"geometry")
        archive.writestr("../stock.dbf", b"attributes")
        archive.writestr("../stock.shx", b"index")
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        service.import_dataset("gis", "Unsafe", archive_path)
