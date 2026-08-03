"""Phase 2 stock-input policy: domain rules, preflight and immutability."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import geopandas as gpd
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

import model_builder as mb
import neighborhood_pipeline as part_c
import city_pipeline as part_d
from stock_input_policy import (
    EXCLUDE_CLUSTER, StockInputPolicy, StockPolicyError,
    ground_rule_for_representative, prepare_stock,
)
from workbench import city_service, db, stock_input_policy_service
from workbench.api import app
from workbench.stock_input_policy_service import save_project_policy


def _synthetic_inputs(tmp_path: Path) -> tuple[Path, Path]:
    gis_path = tmp_path / "stock.gpkg"
    frame = gpd.GeoDataFrame({
        "parcel": ["A", "A", "B", "C"],
        "district": ["ONE", "ONE", "ONE", "TWO"],
        "floor_raw": [2, 2, "bad", 1],
        "class_raw": ["OLD", "OLD", "NEW", "DROP"],
        "area_raw": [100.0, 300.0, "bad", 100.0],
        "res_raw": [100.0, 300.0, None, 50.0],
        "name": ["alpha", "beta", "gamma", "delta"],
    }, geometry=[
        box(0, 0, 10, 10), box(12, 0, 32, 15),
        box(35, 0, 45, 10), box(50, 0, 60, 10),
    ], crs="EPSG:25830")
    frame.to_file(gis_path, driver="GPKG")
    tipo15_path = tmp_path / "tipo15.csv"
    pd.DataFrame({
        "31_pc": ["A", "B", "C"],
        "442_sup_Residencial": [400.0, None, 50.0],
        "252_planta": ["0", "1", "1"],
    }).to_csv(tipo15_path, sep=";", encoding="latin-1", index=False)
    return gis_path, tipo15_path


def _policy(**updates) -> StockInputPolicy:
    value = StockInputPolicy(
        gis_dataset_id="synthetic",
        reference_field="parcel",
        district_field="district",
        footprint_area_field="area_raw",
        floors_field="floor_raw",
        cluster_field="class_raw",
        cluster_mapping={
            "OLD": "VivUniP04", "NEW": "EdiPluriP05", "DROP": EXCLUDE_CLUSTER,
        },
        floor_invalid_policy="fixed_fallback",
        floor_fixed_fallback=7,
    ).to_dict()
    return StockInputPolicy.from_dict(value | updates)


def test_policy_resolves_fallback_exclusion_proxy_and_duplicate_counters(tmp_path: Path):
    gis_path, tipo15_path = _synthetic_inputs(tmp_path)
    stock, resolved, report = prepare_stock(
        gis_path, tipo15_path, _policy(), duplicate_parcel_apportioning=True,
    )

    assert resolved.cluster_mapping["DROP"] == EXCLUDE_CLUSTER
    assert list(stock["cluster"]) == ["VivUniP04", "VivUniP04", "EdiPluriP05"]
    assert list(stock["nombre"]) == ["ONE", "ONE", "ONE"]
    assert list(stock["altura_max"]) == [2, 2, 7]
    assert list(stock["res_area_m2"].round(3)) == [100.0, 300.0, 100.0]
    for key, expected in {
        "source_buildings": 4, "scoped_buildings": 4, "retained_buildings": 3,
        "fixed_floor_fallback_buildings": 1, "excluded_cluster_buildings": 1,
        "footprint_geometry_fallback_buildings": 1,
        "residential_area_proxy_buildings": 1, "duplicate_parcel_rows": 2,
        "duplicate_parcel_apportioned_rows": 2,
    }.items():
        assert report[key] == expected


def test_floor_policy_modes_and_structural_blockers(tmp_path: Path):
    gis_path, tipo15_path = _synthetic_inputs(tmp_path)

    with pytest.raises(StockPolicyError, match="invalid floor") as blocked_floor:
        prepare_stock(gis_path, tipo15_path, _policy(
            floor_invalid_policy="block_run", floor_fixed_fallback=None,
        ))
    assert blocked_floor.value.code == "invalid_floors"

    excluded, _, report = prepare_stock(gis_path, tipo15_path, _policy(
        floor_invalid_policy="exclude_invalid", floor_fixed_fallback=None,
    ))
    assert len(excluded) == 2
    assert report["excluded_floor_buildings"] == 1

    with pytest.raises(StockPolicyError) as numeric:
        prepare_stock(gis_path, tipo15_path, _policy(floors_field="name"))
    assert numeric.value.code == "invalid_numeric_field"

    with pytest.raises(StockPolicyError) as scope:
        prepare_stock(gis_path, tipo15_path, _policy(), district="MISSING")
    assert scope.value.code == "empty_scope"

    one_building, _, building_report = prepare_stock(
        gis_path, tipo15_path, _policy(), reference="B",
    )
    assert list(one_building["refparcela"]) == ["B"]
    assert building_report["scope"] == "B (building)"

    all_excluded = {key: EXCLUDE_CLUSTER for key in ("OLD", "NEW", "DROP")}
    with pytest.raises(StockPolicyError) as clusters:
        prepare_stock(gis_path, tipo15_path, _policy(cluster_mapping=all_excluded))
    assert clusters.value.code == "zero_valid_clusters"

    missing_ref = gpd.read_file(gis_path)
    missing_ref.loc[0, "parcel"] = ""
    missing_path = tmp_path / "missing-reference.gpkg"
    missing_ref.to_file(missing_path, driver="GPKG")
    with pytest.raises(StockPolicyError) as reference:
        prepare_stock(missing_path, tipo15_path, _policy())
    assert reference.value.code == "missing_reference"

    with pytest.raises(StockPolicyError) as fractional_fallback:
        _policy(floor_fixed_fallback=2.5)
    assert fractional_fallback.value.code == "invalid_floor_fallback"


def test_ground_floor_modes_are_explicit(tmp_path: Path):
    _, tipo15_path = _synthetic_inputs(tmp_path)
    defaults = {"VivUni": False, "EdiPluri": True, "BlocPluri": True}
    assert ground_rule_for_representative(
        "A", "EdiPluri", tipo15_path, "tipo15_family_fallback", defaults,
    ) == (False, "tipo15")
    assert ground_rule_for_representative(
        "UNKNOWN", "EdiPluri", tipo15_path, "tipo15_family_fallback", defaults,
    ) == (True, "family_fallback")
    assert ground_rule_for_representative(
        "A", "EdiPluri", tipo15_path, "force_conditioned", defaults,
    ) == (False, "forced_conditioned")
    assert ground_rule_for_representative(
        "A", "VivUni", tipo15_path, "force_unconditioned", defaults,
    ) == (True, "forced_unconditioned")


def test_default_policy_preserves_real_part_c_and_d_stock_baseline():
    neighborhood = part_c.load_stock(input_policy=StockInputPolicy())
    city = part_d.load_stock_city(input_policy=StockInputPolicy())
    assert (len(neighborhood), neighborhood.cluster.nunique()) == (959, 18)
    assert (int(neighborhood.imputed_floors.sum()), int(neighborhood.res_area_proxy.sum())) == (51, 11)
    assert neighborhood.res_area_m2.sum() == pytest.approx(1_953_038.187326063)
    assert len(part_c.select_representatives(neighborhood)) == 18
    assert (len(city), city.cluster.nunique()) == (26_452, 21)
    assert (int(city.imputed_floors.sum()), int(city.res_area_proxy.sum())) == (1_648, 192)
    assert city.res_area_m2.sum() == pytest.approx(44_406_368.526234426)
    assert int(city.dup_refparcela.sum()) == 13
    assert len(part_c.select_representatives(city)) == 21


def test_preflight_api_separates_blockers_from_warnings():
    with TestClient(app) as client:
        cache_hits = stock_input_policy_service._cached_preflight.cache_info().hits
        baseline = client.get("/api/workflows/city/input-policy").json()
        assert baseline["ready"] is True
        assert baseline["cluster_targets"]
        assert baseline["field_options"]["cluster_values"]
        assert baseline["resolved_policy"]["cluster_mapping"]["EdiPluriP04"] == "EdiPluriP04"
        assert baseline["override_diff"] == {}
        contract_values = {item["key"]: item["value"] for item in baseline["inputs"]}
        assert contract_values["stock"] == "Valencia city buildings"
        assert contract_values["footprint"] == "Shape_Area"
        assert baseline["locked"] is False
        baseline["coverage"]["retained_buildings"] = -1
        cached_baseline = client.get("/api/workflows/city/input-policy").json()
        assert cached_baseline["coverage"]["retained_buildings"] == 26_452
        assert stock_input_policy_service._cached_preflight.cache_info().hits > cache_hits

        invalid_numeric = client.post(
            "/api/workflows/city/input-policy/preflight",
            json={"input_policy_override": {"floors_field": "nombre"}},
        ).json()
        assert invalid_numeric["ready"] is False
        assert {item["code"] for item in invalid_numeric["blockers"]} == {"invalid_numeric_field"}
        assert invalid_numeric["warnings"] == []

        empty_scope = client.post(
            "/api/workflows/neighborhood/input-policy/preflight",
            json={"district": "NOT A DISTRICT"},
        ).json()
        assert empty_scope["ready"] is False
        assert {item["code"] for item in empty_scope["blockers"]} == {"empty_scope"}

        forced = client.post(
            "/api/workflows/city/input-policy/preflight",
            json={"input_policy_override": {"ground_floor_mode": "force_conditioned"}},
        ).json()
        assert forced["ready"] is True
        assert forced["override_diff"] == {
            "ground_floor_mode": {
                "project_default": "tipo15_family_fallback",
                "requested": "force_conditioned",
            }
        }
        warning = next(item for item in forced["warnings"] if item["code"] == "forced_ground_mode")
        assert warning["count"] == 21

        building = client.post(
            "/api/workflows/neighborhood/input-policy/preflight",
            json={
                "building_ref": "4750502YJ2744H",
                "input_policy_override": {"ground_floor_mode": "force_conditioned"},
            },
        ).json()
        assert building["ready"] is True
        assert building["coverage"]["scope"] == "4750502YJ2744H (building)"
        assert building["coverage"]["retained_buildings"] == 1
        assert building["coverage"]["representatives"] == 1
        assert building["tipo15_dataset"]["snapshot_hash"]


def _register_real_policy_inputs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "var/workbench.sqlite3")
    db.init_db()
    for dataset_id, kind, name, path in (
        ("valencia-city", "gis", "Valencia city", Path(mb.NEIGHBORS_SHP)),
        ("tipo15-ledger", "companion", "Tipo15", Path(part_c.TIPO15_CSV)),
    ):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        db.upsert_dataset({
            "id": dataset_id, "kind": kind, "name": name, "path": str(path),
            "sha256": digest, "snapshot_hash": digest,
        })
    db.update_project_settings({"building_dataset_id": "valencia-city"})


def test_schema_v6_policy_row_migrates_to_append_only_v7(tmp_path: Path, monkeypatch):
    database = tmp_path / "var/workbench.sqlite3"
    database.parent.mkdir(parents=True)
    policy = StockInputPolicy().to_dict()
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                locale TEXT NOT NULL DEFAULT 'tr', created_at TEXT NOT NULL
            );
            INSERT INTO projects VALUES ('valencia','Valencia','tr','2026-07-22');
            CREATE TABLE workflow_input_policies (
                project_id TEXT NOT NULL, workflow TEXT NOT NULL,
                schema_version INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                policy_json TEXT NOT NULL, fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id,workflow),
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
        """)
        connection.execute(
            "INSERT INTO workflow_input_policies VALUES (?,?,?,?,?,?,?)",
            ("valencia", "city", 1, 1, json.dumps(policy), "old-fingerprint", "2026-07-22"),
        )
        connection.execute("PRAGMA user_version=6")
    monkeypatch.setattr(db, "VAR_DIR", database.parent)
    monkeypatch.setattr(db, "DB_PATH", database)
    db.init_db()
    with sqlite3.connect(database) as connection:
        primary_key = [
            row[1] for row in sorted(
                (row for row in connection.execute("PRAGMA table_info(workflow_input_policies)") if row[5]),
                key=lambda row: row[5],
            )
        ]
        row = connection.execute(
            "SELECT workflow,revision,fingerprint FROM workflow_input_policies"
        ).fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert primary_key == ["project_id", "workflow", "revision"]
    assert row == ("city", 1, "old-fingerprint")
    assert version == 7


def test_queued_job_keeps_resolved_policy_after_project_default_changes(tmp_path: Path, monkeypatch):
    _register_real_policy_inputs(tmp_path, monkeypatch)
    first = save_project_policy("city", StockInputPolicy().to_dict())
    assert first["project_revision"] == 1

    monkeypatch.setattr(city_service, "_require_ready", lambda **_kwargs: {"version": "test"})
    monkeypatch.setattr(city_service, "validate_request", lambda *_args, **_kwargs: {
        "run_mode": "full_baseline", "scenario": None, "selected_weather": None,
    })
    monkeypatch.setattr(city_service.storage, "reserve_payload", lambda _kind, payload: payload)
    job = city_service.create_city_job({
        "input_policy_override": {"ground_floor_mode": "force_conditioned"},
    })
    queued = db.get_job(job["id"])
    original_context = queued["payload"]["stock_input_policy"]
    assert original_context["source"] == "run_override"
    assert original_context["resolved_policy"]["ground_floor_mode"] == "force_conditioned"
    assert original_context["policy_fingerprint"]
    assert original_context["dataset"]["snapshot_hash"]
    assert original_context["tipo15_dataset"]["snapshot_hash"]

    changed = save_project_policy("city", StockInputPolicy(
        ground_floor_mode="family_default",
    ).to_dict())
    assert changed["project_revision"] == 2
    assert changed["policy_fingerprint"] != original_context["policy_fingerprint"]

    still_queued = db.get_job(job["id"])["payload"]["stock_input_policy"]
    assert still_queued == original_context
    with db.connect() as connection:
        revisions = connection.execute(
            "SELECT revision FROM workflow_input_policies WHERE workflow='city' ORDER BY revision"
        ).fetchall()
    assert [row[0] for row in revisions] == [1, 2]


# ---------------------------------------------------------------------------
# Ground use resolution (review finding ③, 2026-08-03)
#
# ground_floor_mode entered the run fingerprint but never reached the engine:
# four modes, four identities, one physics.  resolve_ground_use is the
# stock-scale resolver the deep chain now consumes, with the corrected token
# set (the legacy per-representative set missed "B0" - 8,756 records - and
# "OD" - 7,901, this dataset's two most common ground codes).
# ---------------------------------------------------------------------------
def _ground_stock() -> pd.DataFrame:
    return pd.DataFrame({
        "refparcela": ["GROUND_B0", "UPPER_ONLY", "WHOLE_HOUSE", "NOT_IN_TIPO15"],
        "family": ["BlocPluri", "BlocPluri", "VivUni", "VivUni"],
    })


def _ground_tipo15(tmp_path: Path) -> Path:
    path = tmp_path / "tipo15_ground.csv"
    pd.DataFrame({
        "31_pc":      ["GROUND_B0", "GROUND_B0", "UPPER_ONLY", "WHOLE_HOUSE"],
        "252_planta": ["B0",        "1",         "1",          "OD"],
        "442_sup_Residencial": [80.0, 80.0, 90.0, 120.0],
    }).to_csv(path, sep=";", encoding="latin-1", index=False)
    return path


def test_resolve_ground_use_reads_tipo15_with_the_corrected_tokens(tmp_path):
    from stock_input_policy import resolve_ground_use
    stock = _ground_stock()
    use, source = resolve_ground_use(stock, _ground_tipo15(tmp_path),
                                     "tipo15_family_fallback")
    resolved = dict(zip(stock["refparcela"], zip(use, source)))
    # "B0" and "OD" are ground codes the legacy set missed
    assert resolved["GROUND_B0"] == ("residential", "tipo15")
    assert resolved["WHOLE_HOUSE"] == ("residential", "tipo15")
    assert resolved["UPPER_ONLY"] == ("terciario", "tipo15")
    # absent from Tipo15 -> family default (VivUni lives on its own ground)
    assert resolved["NOT_IN_TIPO15"] == ("residential", "family_fallback")


def test_resolve_ground_use_forced_modes_apply_to_everything(tmp_path):
    from stock_input_policy import resolve_ground_use
    stock = _ground_stock()
    tipo15 = _ground_tipo15(tmp_path)
    for mode, expected in (("force_unconditioned", "terciario"),
                           ("force_conditioned", "residential")):
        use, source = resolve_ground_use(stock, tipo15, mode)
        assert set(use) == {expected}
        assert set(source) == {"forced"}


def test_prepare_stock_carries_the_ground_columns(tmp_path):
    gis_path, tipo15_path = _synthetic_inputs(tmp_path)
    stock, _, report = prepare_stock(gis_path, tipo15_path, _policy())
    assert set(stock["ground_use"]) <= {"residential", "terciario"}
    assert "ground_use_source" in stock.columns
    assert report["ground_residential_buildings"] + \
        report["ground_terciario_buildings"] == len(stock)
