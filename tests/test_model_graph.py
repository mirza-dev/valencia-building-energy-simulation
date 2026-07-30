import hashlib
import json
from pathlib import Path

from workbench import db
from workbench.model_graph import (
    enrich_scene_construction_ids, extract_model_graph, resolve_model_artifact,
)


PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((PROJECT / "tests/fixtures/model_editor_smoke.json").read_text(encoding="utf-8"))
PILOT_OSM = PROJECT / FIXTURE["artifact"]


def test_pilot_model_graph_is_stable_complete_and_hashable():
    first = extract_model_graph(PILOT_OSM)
    second = extract_model_graph(PILOT_OSM)
    assert first["graph_sha256"] == second["graph_sha256"] == FIXTURE["before_graph_sha256"]
    assert first["counts"] == FIXTURE["expected_counts"]
    assert first["osm_sha256"] == FIXTURE["artifact_sha256"]
    assert all(item["id"].startswith("{") for item in first["constructions"])
    assert any(item["layers"] and item["surface_usage"]["surface_count"] for item in first["constructions"])
    assert any(item["properties"] for item in first["materials"])
    assert any(item["profiles"] for item in first["schedules"])
    assert any(item["loads"] for item in first["space_types"])
    assert all("thermostat" in item for item in first["zones"])
    assert "run_period" in first["simulation"]
    assert first["counts"]["project_parameters"] == 13
    parameters = {item["key"]: item for item in first["project_parameters"]}
    assert parameters["wall_u"]["current_value"] == 1.33
    assert parameters["window_u"]["current_value"] == 5.7
    assert parameters["window_g"]["current_value"] == 0.82
    assert parameters["infiltration_ach"]["current_value"] == 0.2
    assert parameters["infiltration_ach"]["binding"]["object_name"] == "Infitracion Aire constante 0,2ACH Viv CTE"
    assert parameters["infiltration_ach"]["evidence"]["buffer_objects_untouched"] == [
        "Infitracion Aire constante 1ACH", "Infitracion Aire constante 3ACH",
    ]
    assert parameters["cop"]["binding"]["status"] == "run_setting"
    assert first["project_parameter_context"]["base_template"].endswith("PlantillaOS_v2.osm")


def test_scene_is_enriched_in_memory_without_mutating_source_payload():
    graph = extract_model_graph(PILOT_OSM)
    construction = next(item for item in graph["constructions"] if item["surface_usage"]["surface_count"])
    surface_id = None
    import openstudio
    model = openstudio.osversion.VersionTranslator().loadModel(
        openstudio.toPath(str(PILOT_OSM))
    ).get()
    for surface in [*model.getSurfaces(), *model.getSubSurfaces()]:
        value = surface.construction()
        if not value.isNull() and str(value.get().handle()) == construction["id"]:
            surface_id = str(surface.handle())
            break
    assert surface_id
    source = {"surfaces": [{"id": surface_id}], "subsurfaces": []}
    enriched = enrich_scene_construction_ids(PILOT_OSM, source)
    assert "construction_id" not in source["surfaces"][0]
    assert enriched["surfaces"][0]["construction_id"] == construction["id"]


def test_child_run_resolves_to_verified_parent_model(monkeypatch, tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    osm = root / "model_python.osm"
    scene = root / "scene.json"
    osm.write_bytes(PILOT_OSM.read_bytes())
    scene.write_text("{}", encoding="utf-8")
    parent = {
        "id": "model-parent", "run_type": "model", "parent_run_id": None,
        "artifact_dir": str(root), "refparcela": "pilot", "scenario_name": "Baseline",
        "verification_status": "VERIFIED", "raw_model_sha256": hashlib.sha256(osm.read_bytes()).hexdigest(),
    }
    child = {"id": "simulation-child", "run_type": "simulation", "parent_run_id": parent["id"]}
    monkeypatch.setattr(db, "get_run", lambda run_id: child if run_id == child["id"] else parent if run_id == parent["id"] else None)
    resolved = resolve_model_artifact(child["id"])
    assert resolved["requested_id"] == child["id"]
    assert resolved["model_id"] == parent["id"]
    assert resolved["immutable"] is True
