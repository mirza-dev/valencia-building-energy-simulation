"""Phase 1 data-dictionary and read-only workflow-contract acceptance tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import geopandas as gpd
from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from workbench import db
from workbench.api import app


def _columns(payload: dict) -> dict[str, dict]:
    return {item["field"]: item for item in payload["columns"]}


def test_authoritative_semantics_and_usable_coverage_are_exposed():
    with TestClient(app) as client:
        response = client.get("/api/datasets/valencia-city/dictionary")
        assert response.status_code == 200
        payload = response.json()
        columns = _columns(payload)

        heating = columns["demanda_ca"]
        assert "External-source heating demand" in heating["meaning"]["en"]
        assert "floor-area basis" in heating["meaning"]["en"]
        assert heating["effect"] == "validation_only"

        consumption = columns["ConsumE"]
        assert "energy-consumption intensity" in consumption["meaning"]["en"]
        assert consumption["evidence"] == {
            "dataset_median": 47.0,
            "author_reference": 47.0,
            "unit": "kWh/m²",
        }
        assert "DHW" in consumption["scope"]["en"]
        assert "ground floors" in consumption["scope"]["en"]

        total = columns["ConsumETot"]
        assert total["definition_status"] == "source_confirmed_values_unusable"
        assert total["present_pct"] == 100.0
        assert total["usable_pct"] == 0.0
        assert total["zero_count"] == payload["dataset"]["rows"]
        assert total["evidence"] == {}

        intervention = columns["demanda__1"]
        assert intervention["definition_status"] == "unconfirmed"
        assert "suggests post-intervention heating" in intervention["meaning"]["en"]
        assert "cooling demand" in intervention["meaning"]["en"]


def test_stock_method_microcopy_and_tipo15_join_basis_are_explicit():
    with TestClient(app) as client:
        payload = client.get("/api/datasets/valencia-city/dictionary").json()
        columns = _columns(payload)

        cluster = columns["cluster"]
        assert cluster["effect"] == "physical_model"
        for excluded_input in ("infiltration", "schedules", "setpoints", "HVAC"):
            assert excluded_input in cluster["meaning"]["en"]

        area = columns["Shape_Area"]
        assert area["effect"] == "stock_method"
        assert "representative selection" in area["meaning"]["en"]
        assert "duplicate-parcel apportioning" in area["meaning"]["en"]

        assert columns["nombre"]["effect"] == "stock_method"
        assert columns["uso_princi"]["effect"] == "reporting_only"
        assert "does not actively filter" in columns["uso_princi"]["meaning"]["en"]

        companion = payload["companion_links"][0]
        assert companion["dataset_id"] == "tipo15-ledger"
        assert companion["join"]["coverage_basis"] == "gis_building_rows_joined_by_refparcela_to_tipo15_31_pc"
        assert companion["join"]["residential_area"]["joined_count"] > 26_000
        assert companion["join"]["ground_rule"]["joined_count"] > 26_000

        tipo15 = client.get("/api/datasets/tipo15-ledger/dictionary").json()
        assert tipo15["dataset"]["kind"] == "companion"
        assert tipo15["dataset"]["rows"] == 411_273
        tipo15_columns = _columns(tipo15)
        assert tipo15_columns["442_sup_Residencial"]["coverage_basis"] == "dataset_rows"
        assert tipo15_columns["442_sup_Residencial"]["effect"] == "scaling"


def test_unknown_fields_are_unclassified_and_missing_tipo15_key_does_not_fail(tmp_path: Path):
    path = tmp_path / "unmapped.geojson"
    frame = gpd.GeoDataFrame(
        {"mystery": ["known-to-user", ""]},
        geometry=[
            Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
            Polygon([(2, 0), (3, 0), (3, 1), (2, 1)]),
        ],
        crs="EPSG:25830",
    )
    frame.to_file(path, driver="GeoJSON")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    with TestClient(app) as client:
        db.upsert_dataset({
            "id": "unmapped-gis", "kind": "gis", "name": "Unmapped GIS",
            "path": str(path), "sha256": digest, "snapshot_hash": digest,
            "metadata": {"columns": ["mystery", "geometry"], "rows": 2},
        })
        response = client.get("/api/datasets/unmapped-gis/dictionary")
        assert response.status_code == 200
        payload = response.json()
        mystery = _columns(payload)["mystery"]
        assert mystery["definition_status"] == "unclassified"
        assert mystery["effect"] == "not_used"
        assert "Not used" not in mystery["meaning"]["en"]
        assert payload["companion_links"][0]["join"] == {
            "available": False, "reason": "refparcela_not_available",
        }
        with db.connect() as connection:
            connection.execute(
                "DELETE FROM snapshot_refs WHERE owner_type='dataset' AND owner_id='unmapped-gis'"
            )
            connection.execute("DELETE FROM datasets WHERE id='unmapped-gis'")


def test_project_annotation_is_separate_from_system_truth():
    with TestClient(app) as client:
        before = _columns(client.get("/api/datasets/valencia-city/dictionary").json())["demanda_ca"]
        response = client.put(
            "/api/datasets/valencia-city/dictionary/demanda_ca/note",
            json={"note": "Check floor-area basis with Rai", "semantic_label": "UPV validation"},
        )
        assert response.status_code == 200
        after = _columns(client.get("/api/datasets/valencia-city/dictionary").json())["demanda_ca"]
        assert after["meaning"] == before["meaning"]
        assert after["definition_status"] == before["definition_status"]
        assert after["user_note"] == "Check floor-area basis with Rai"
        assert after["user_semantic_label"] == "UPV validation"

        invalid = client.put(
            "/api/datasets/valencia-city/dictionary/not-a-field/note",
            json={"note": "x", "semantic_label": ""},
        )
        assert invalid.status_code == 422


def test_workflow_contracts_expose_only_controls_that_exist_today():
    with TestClient(app) as client:
        builder = client.get("/api/workflows/builder/input-policy").json()
        simulation = client.get("/api/workflows/simulation/input-policy").json()
        neighborhood = client.get("/api/workflows/neighborhood/input-policy").json()
        city = client.get("/api/workflows/city/input-policy").json()
        lhs = client.get("/api/workflows/lhs/input-policy").json()

        assert builder["locked"] is False
        assert {item["key"] for item in builder["inputs"]} >= {
            "geometry", "reference", "floors", "cluster", "neighbors", "template",
        }
        assert simulation["locked"] is True
        assert "never rebuilds the model from GIS" in simulation["summary"]
        for stock in (neighborhood, city):
            assert stock["phase"] == 2
            assert stock["locked"] is False
            assert stock["status"] == "Project default policy — configurable; every run resolves an immutable copy."
            assert stock["ready"] is True
            assert stock["resolved_policy"]["schema_version"] == 1
            assert stock["policy_fingerprint"]
            assert {item["key"] for item in stock["inputs"]} >= {
                "stock", "floors", "cluster", "footprint", "ground", "residential_area",
            }
            passive = {item["field"]: item for item in stock["passive_fields"]}
            assert passive["demanda_ca"]["effect"] == "validation_only"
            assert passive["ConsumE"]["effect"] == "validation_only"
        assert lhs["locked"] is True
        assert lhs["inputs"][0]["value"] == "N=50 · seed=42"

        assert client.get("/api/workflows/not-real/input-policy").status_code == 404


def test_current_schema_contains_dataset_annotation_and_versioned_policy_tables():
    with db.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.LATEST_SCHEMA_VERSION
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "dataset_field_notes" in tables
    assert "workflow_input_policies" in tables
    with db.connect() as connection:
        primary_key = [row[1] for row in connection.execute(
            "PRAGMA table_info(workflow_input_policies)"
        ) if row[5]]
    assert primary_key == ["project_id", "workflow", "revision"]
