from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import openstudio
import pytest

from workbench import db, model_editor_service, service, storage
from workbench.measure_runner import (
    BUILTIN_ROOT, MeasureRejected, apply_model_measure, import_measure_archive,
)
from workbench.model_editor import apply_typed_patches, preflight_model
from workbench.model_graph import extract_model_graph
from workbench.scene import extract_scene_from_path


PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((PROJECT / "tests/fixtures/model_editor_smoke.json").read_text(encoding="utf-8"))
PILOT_OSM = PROJECT / FIXTURE["artifact"]
PILOT_ROOT = PILOT_OSM.parent


def _model(path: Path):
    translated = openstudio.osversion.VersionTranslator().loadModel(openstudio.toPath(str(path)))
    assert not translated.isNull()
    return translated.get()


def _named_infiltration(path: Path) -> dict[str, float]:
    return {
        item.nameString(): float(item.airChangesperHour().get())
        for item in _model(path).getSpaceInfiltrationDesignFlowRates()
        if not item.airChangesperHour().isNull()
    }


def _thermostat_profiles(path: Path) -> tuple[list[float], list[float], list[float], list[float]]:
    model = _model(path)
    thermostat = next(
        zone.thermostatSetpointDualSetpoint().get()
        for zone in model.getThermalZones()
        if not zone.thermostatSetpointDualSetpoint().isNull()
    )
    heating = thermostat.heatingSetpointTemperatureSchedule().get().to_ScheduleRuleset().get()
    cooling = thermostat.coolingSetpointTemperatureSchedule().get().to_ScheduleRuleset().get()
    heating_rules = [float(value) for rule in heating.scheduleRules() for value in rule.daySchedule().values()]
    cooling_rules = [float(value) for rule in cooling.scheduleRules() for value in rule.daySchedule().values()]
    return (
        [float(value) for value in heating.defaultDaySchedule().values()],
        [float(value) for value in cooling.defaultDaySchedule().values()],
        heating_rules,
        cooling_rules,
    )


def test_project_parameter_and_thermostat_patch_preserve_source_and_sentinels(tmp_path):
    output = tmp_path / "authored.osm"
    source_hash = hashlib.sha256(PILOT_OSM.read_bytes()).hexdigest()
    before_heating_default, before_cooling_default, before_heating, before_cooling = _thermostat_profiles(PILOT_OSM)
    before_openings = len(_model(PILOT_OSM).getSubSurfaces())
    result = apply_typed_patches(PILOT_OSM, output, FIXTURE["smoke"]["patches"])
    assert result["applied"] == FIXTURE["smoke"]["applied"] and result["rejected"] == 0
    assert hashlib.sha256(PILOT_OSM.read_bytes()).hexdigest() == source_hash == FIXTURE["artifact_sha256"]
    graph = extract_model_graph(output)
    assert graph["graph_sha256"] != FIXTURE["before_graph_sha256"]
    assert next(item for item in graph["project_parameters"] if item["key"] == "wall_u")["current_value"] == 1.5
    after_heating_default, after_cooling_default, after_heating, after_cooling = _thermostat_profiles(output)
    assert after_heating_default == before_heating_default
    assert after_cooling_default == before_cooling_default
    assert [value for value in after_heating if value <= -9] == [value for value in before_heating if value <= -9]
    assert [value for value in after_cooling if value >= 49] == [value for value in before_cooling if value >= 49]
    assert all(after == pytest.approx(before + 1) for before, after in zip(before_heating, after_heating, strict=True) if before > -9)
    assert all(after == pytest.approx(before - 1) for before, after in zip(before_cooling, after_cooling, strict=True) if before < 49)
    assert len(_model(output).getSubSurfaces()) - before_openings == FIXTURE["smoke"]["opening_count_delta"]
    assert preflight_model(output)["ready"] is True


def test_extreme_thermostat_offset_warns_without_blocking_and_preserves_sentinels(tmp_path):
    output = tmp_path / "extreme-comfort.osm"
    before_default, _, before_rules, _ = _thermostat_profiles(PILOT_OSM)
    result = apply_typed_patches(PILOT_OSM, output, [{
        "op": "thermostat.set_setpoints", "target_id": "first_conditioned_zone",
        "payload": {"heating_delta_c": 16.0},
    }])
    assert result["applied"] == 1 and result["rejected"] == 0
    assert {item["code"] for item in result["reports"][0]["warnings"]} == {
        "outside_comfort_reference", "extreme_comfort_offset",
    }
    after_default, _, after_rules, _ = _thermostat_profiles(output)
    assert after_default == before_default
    assert [value for value in after_rules if value <= -9] == [value for value in before_rules if value <= -9]
    assert all(after == pytest.approx(before + 16)
               for before, after in zip(before_rules, after_rules, strict=True) if before > -9)
    assert preflight_model(output)["ready"] is True


def test_context_shading_toggle_is_reversible_without_deleting_plantilla_geometry(tmp_path):
    disabled = tmp_path / "context-disabled.osm"
    result = apply_typed_patches(PILOT_OSM, disabled, [{
        "op": "project_parameter.update", "payload": {"key": "context_shading", "value": False},
    }])
    assert result["applied"] == 1 and result["rejected"] == 0
    disabled_model = _model(disabled)
    context = next(group for group in disabled_model.getShadingSurfaceGroups()
                   if group.nameString() == "Neighbor shadow masses")
    assert len(context.shadingSurfaces()) == 69
    parameter = next(item for item in extract_model_graph(disabled)["project_parameters"]
                     if item["key"] == "context_shading")
    assert parameter["current_value"] is False
    assert parameter["evidence"] == {
        "site_shading_surfaces": 0,
        "stored_site_shading_surfaces": 69,
        "disabled_site_shading_surfaces": 69,
    }
    stats = json.loads((PILOT_ROOT / "stats.json").read_text(encoding="utf-8"))
    assert not [item for item in extract_scene_from_path(disabled, stats)["shading"]
                if item["category"] == "context"]

    restored = tmp_path / "context-restored.osm"
    result = apply_typed_patches(disabled, restored, [{
        "op": "project_parameter.update", "payload": {"key": "context_shading", "value": True},
    }])
    assert result["applied"] == 1 and result["rejected"] == 0
    restored_model = _model(restored)
    restored_context = next(group for group in restored_model.getShadingSurfaceGroups()
                            if group.nameString() == "Neighbor shadow masses")
    assert len(restored_context.shadingSurfaces()) == 69
    assert all(surface.transmittanceSchedule().isNull() for surface in restored_context.shadingSurfaces())
    parameter = next(item for item in extract_model_graph(restored)["project_parameters"]
                     if item["key"] == "context_shading")
    assert parameter["current_value"] is True
    assert len([item for item in extract_scene_from_path(restored, stats)["shading"]
                if item["category"] == "context"]) == 69
    assert preflight_model(restored)["ready"] is True


def test_warn_not_block_and_impossible_values_are_separated(tmp_path):
    output = tmp_path / "guardrail.osm"
    result = apply_typed_patches(PILOT_OSM, output, [
        {"op": "project_parameter.update", "payload": {"key": "wall_u", "value": 0.56}},
        {"op": "project_parameter.update", "payload": {"key": "window_g", "value": 1.2}},
        {"op": "schedule.update_day", "target_id": "missing", "payload": {"points": [{"hour": 12, "value": 20}]}},
    ])
    assert result["reports"][0]["status"] == "applied"
    assert result["reports"][0]["warnings"][0]["code"] == "outside_lhs_band"
    assert result["reports"][1]["status"] == "rejected"
    assert result["reports"][1]["code"] == "physical_limit"
    assert result["reports"][2]["status"] == "rejected"


def test_dwelling_infiltration_edit_does_not_touch_buffer_objects(tmp_path):
    output = tmp_path / "infiltration.osm"
    before = _named_infiltration(PILOT_OSM)
    result = apply_typed_patches(PILOT_OSM, output, [{
        "op": "project_parameter.update", "payload": {"key": "infiltration_ach", "value": 0.35},
    }])
    assert result["rejected"] == 0
    after = _named_infiltration(output)
    assert after["Infitracion Aire constante 0,2ACH Viv CTE"] == pytest.approx(0.35)
    assert after["Infitracion Aire constante 1ACH"] == before["Infitracion Aire constante 1ACH"]
    assert after["Infitracion Aire constante 3ACH"] == before["Infitracion Aire constante 3ACH"]


def test_geometry_window_patch_updates_exact_scene_and_glazing_qa_basis(tmp_path):
    model = _model(PILOT_OSM)
    wall = next(surface for surface in model.getSurfaces()
                if surface.surfaceType() == "Wall" and surface.outsideBoundaryCondition() == "Outdoors"
                and not surface.space().isNull() and not surface.space().get().spaceType().isNull()
                and "Vivienda" in surface.space().get().spaceType().get().nameString())
    output = tmp_path / "window.osm"
    result = apply_typed_patches(PILOT_OSM, output, [{
        "op": "subsurface.create", "target_id": str(wall.handle()),
        "payload": {"name": "Faz 2 test window", "normalized_rect": {"u_min": 0.105, "u_max": 0.145, "v_min": 0.75, "v_max": 0.95}},
    }])
    assert result["applied"] == 1 and result["rejected"] == 0
    authored = _model(output)
    created = next(item for item in authored.getSubSurfaces() if item.nameString() == "Faz 2 test window")
    assert created.grossArea() > 0.25
    assert len(authored.getSubSurfaces()) == len(model.getSubSurfaces()) + 1
    assert preflight_model(output)["ready"] is True
    stats = json.loads((PILOT_ROOT / "stats.json").read_text(encoding="utf-8"))
    scene = extract_scene_from_path(output, stats)
    assert any(item["name"] == "Faz 2 test window" for item in scene["subsurfaces"])
    assert scene["facade_qa"] != stats["facade_qa"]
    assert sum(item["window"] for item in scene["facade_qa"]) == sum(item["window"] for item in stats["facade_qa"]) + 1


def test_unedited_scene_preserves_pipeline_residential_facade_qa():
    stats = json.loads((PILOT_ROOT / "stats.json").read_text(encoding="utf-8"))
    scene = extract_scene_from_path(PILOT_OSM, stats)
    assert scene["facade_qa"] == stats["facade_qa"]


def test_geometry_invalid_draw_is_rejected_and_moved_wall_blocks_commit(tmp_path):
    model = _model(PILOT_OSM)
    wall = next(surface for surface in model.getSurfaces()
                if surface.surfaceType() == "Wall" and surface.outsideBoundaryCondition() == "Outdoors" and not surface.subSurfaces())
    rejected_path = tmp_path / "rejected.osm"
    rejected = apply_typed_patches(PILOT_OSM, rejected_path, [{
        "op": "subsurface.create", "target_id": str(wall.handle()),
        "payload": {"normalized_rect": {"u_min": 0.8, "u_max": 0.2, "v_min": 0.2, "v_max": 0.8}},
    }])
    assert rejected["reports"][0]["code"] == "opening_bounds"
    moved_path = tmp_path / "moved.osm"
    moved = apply_typed_patches(PILOT_OSM, moved_path, [{
        "op": "surface.move", "target_id": str(wall.handle()), "payload": {"dx": 0.25, "dy": 0, "dz": 0},
    }])
    assert moved["applied"] == 1
    check = preflight_model(moved_path)
    assert check["ready"] is False
    assert check["checks"]["space_enclosure"] is False


def test_duplicate_top_story_rematches_geometry_and_remains_simulatable(tmp_path):
    model = _model(PILOT_OSM)
    story = max(model.getBuildingStorys(), key=lambda item: float(item.nominalZCoordinate().get() if not item.nominalZCoordinate().isNull() else 0))
    output = tmp_path / "extra-story.osm"
    result = apply_typed_patches(PILOT_OSM, output, [{
        "op": "space.duplicate_story", "target_id": str(story.handle()),
        "payload": {"name": "Faz 2 extra floor", "height_m": 3.0},
    }])
    assert result["applied"] == 1
    authored = _model(output)
    assert len(authored.getSpaces()) == len(model.getSpaces()) + len(story.spaces())
    assert len(authored.getBuildingStorys()) == len(model.getBuildingStorys()) + 1
    assert preflight_model(output)["ready"] is True


def test_phase3_air_and_plant_loops_connect_zones_and_expose_component_sizing(tmp_path):
    output = tmp_path / "phase3-vav.osm"
    result = apply_typed_patches(PILOT_OSM, output, [{
        "op": "hvac.air_loop.create",
        "payload": {"template": "vav_reheat_dx", "name": "Phase 3 VAV"},
    }])
    assert result["applied"] == 1 and result["rejected"] == 0
    graph = extract_model_graph(output)
    assert len(graph["hvac"]["air_loops"]) == 1
    assert len(graph["hvac"]["plant_loops"]) == 1
    air_loop = graph["hvac"]["air_loops"][0]
    assert air_loop["name"] == "Phase 3 VAV"
    assert len(air_loop["zones"]) == 5
    assert any(item["type"] == "Fan VariableVolume" and item["editable"] for item in air_loop["supply_components"])
    preflight = preflight_model(output)
    assert preflight["ready"] is True
    assert preflight["energy_basis"] == "detailed_hvac_consumption"

    fan = next(item for item in air_loop["supply_components"] if item["type"] == "Fan VariableVolume")
    sized = tmp_path / "phase3-vav-sized.osm"
    updated = apply_typed_patches(output, sized, [{
        "op": "hvac.component.update", "target_id": fan["id"],
        "payload": {"autosize": False, "capacity": 1.25, "efficiency": 0.72, "pressure_rise_pa": 650},
    }])
    assert updated["applied"] == 1
    sized_fan = next(item for item in extract_model_graph(sized)["hvac"]["air_loops"][0]["supply_components"] if item["id"] == fan["id"])
    assert sized_fan["properties"]["capacity"]["value"] == pytest.approx(1.25)
    assert sized_fan["properties"]["efficiency"]["value"] == pytest.approx(0.72)


@pytest.mark.parametrize("system", ["gas_furnace", "electric_furnace"])
def test_curated_furnace_systems_connect_every_conditioned_zone_and_preflight(tmp_path, system):
    output = tmp_path / f"{system}.osm"
    result = apply_typed_patches(PILOT_OSM, output, [{
        "op": "hvac.set_system", "payload": {"system": system},
    }])
    assert result["applied"] == 1 and result["rejected"] == 0
    graph = extract_model_graph(output)
    assert len(graph["hvac"]["air_loops"]) == 1
    assert len(graph["hvac"]["air_loops"][0]["zones"]) == 5
    preflight = preflight_model(output)
    assert preflight["ready"] is True
    assert preflight["energy_basis"] == "detailed_hvac_consumption"


def test_high_but_physical_wall_u_warns_and_uses_equivalent_massless_layer(tmp_path):
    output = tmp_path / "high-wall-u.osm"
    result = apply_typed_patches(PILOT_OSM, output, [{
        "op": "project_parameter.update", "payload": {"key": "wall_u", "value": 5.0},
    }])
    assert result["applied"] == 1 and result["rejected"] == 0
    assert {item["code"] for item in result["reports"][0]["warnings"]} >= {
        "outside_lhs_band", "layered_massless_fallback",
    }
    graph = extract_model_graph(output)
    assert next(item for item in graph["project_parameters"] if item["key"] == "wall_u")["current_value"] == pytest.approx(5.0)
    assert preflight_model(output)["ready"] is True


def test_phase3_partial_zone_disconnect_is_blocked_as_mixed_energy_basis(tmp_path):
    detailed = tmp_path / "detailed.osm"
    apply_typed_patches(PILOT_OSM, detailed, [{"op": "hvac.air_loop.create", "payload": {"template": "psz_hp"}}])
    graph = extract_model_graph(detailed)
    loop = graph["hvac"]["air_loops"][0]
    mixed = tmp_path / "mixed.osm"
    result = apply_typed_patches(detailed, mixed, [{
        "op": "hvac.zone.disconnect", "target_id": loop["id"], "payload": {"zone_id": loop["zones"][0]["id"]},
    }])
    assert result["applied"] == 1
    preflight = preflight_model(mixed)
    assert preflight["ready"] is False
    assert any(item["code"] == "mixed_hvac_basis" for item in preflight["errors"])


@pytest.mark.integration
def test_phase3_model_measure_runs_through_cli_and_materializes_source_provenance(tmp_path):
    output = tmp_path / "measure-authored.osm"
    result = apply_model_measure(
        PILOT_OSM, output, tmp_path, "builtin:set_building_north_axis", {"north_axis_deg": 17.5},
    )
    assert result["exit_code"] == 0
    assert result["step_result"]["step_result"] == "Success"
    assert result["stdout_tail"] == ""
    assert result["stderr_tail"] == ""
    assert result["source_archive_sha256"] == hashlib.sha256(Path(result["source_archive"]).read_bytes()).hexdigest()
    assert _model(output).getBuilding().northAxis() == pytest.approx(17.5)
    assert preflight_model(output)["ready"] is True


def test_phase3_measure_zip_import_rejects_path_traversal_and_accepts_one_model_measure(tmp_path):
    safe_zip = tmp_path / "safe.zip"
    source = BUILTIN_ROOT / "set_building_north_axis"
    with zipfile.ZipFile(safe_zip, "w") as archive:
        for path in source.iterdir():
            archive.write(path, f"set_building_north_axis/{path.name}")
    imported = import_measure_archive(tmp_path / "session", safe_zip)
    assert imported["source"] == "uploaded"
    assert imported["measure_type"] == "ModelMeasure"

    unsafe_zip = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe_zip, "w") as archive:
        archive.writestr("../measure.xml", "<measure />")
    with pytest.raises(MeasureRejected, match="Unsafe ZIP entry"):
        import_measure_archive(tmp_path / "unsafe-session", unsafe_zip)


@pytest.mark.integration
def test_recoverable_session_commits_selected_nonpilot_model_as_immutable_authored_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "var/workbench.sqlite3")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(model_editor_service, "SESSION_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(storage, "WARNING_FREE_BYTES", 0)
    db.init_db()
    selected_refparcela = "9876501YJ2797F"
    artifact = {
        "requested_id": "source-model", "model_id": "source-model", "source_kind": "preview",
        "refparcela": selected_refparcela, "scenario_name": "Automatic pipeline model",
        "verification_status": "REVIEW_READY", "immutable": False,
        "osm_path": str(PILOT_OSM), "scene_path": str(PILOT_ROOT / "scene.json"),
        "osm_sha256": FIXTURE["artifact_sha256"],
    }
    monkeypatch.setattr(model_editor_service, "resolve_model_artifact", lambda _model_id: artifact)
    created = model_editor_service.create_session("source-model")
    session_id, token = created["session"]["id"], created["token"]
    assert created["model"]["refparcela"] == selected_refparcela
    assert created["graph"]["project_parameters"]
    assert hashlib.sha256((tmp_path / "sessions" / session_id / "model_python.osm").read_bytes()).hexdigest() == FIXTURE["artifact_sha256"]
    recovered = model_editor_service.session_detail(session_id, token)
    assert recovered["session"]["patch_count"] == 0
    edited = model_editor_service.apply_session_edits(session_id, token, [
        *FIXTURE["smoke"]["patches"],
        {"op": "project_parameter.update", "payload": {"key": "cop", "value": 2.7}},
        {"op": "measure.apply", "payload": {"measure_id": "builtin:set_building_north_axis", "arguments": {"north_axis_deg": 12.0}}},
    ])
    assert edited["session"]["patch_count"] == 5
    assert model_editor_service.preflight_session(session_id, token)["ready"] is True
    committed = model_editor_service.commit_session(session_id, token, "Faz 1 authored acceptance")
    assert committed["verification_status"] == "VERIFIED"
    assert committed["provenance"] == "authored"
    assert committed["authored_from"] == "source-model"
    assert committed["refparcela"] == selected_refparcela
    assert len(committed["patch_journal"]) == 5
    destination = Path(committed["artifact_dir"])
    assert (destination / "editor_provenance.json").is_file()
    assert (destination / "patch_journal.json").is_file()
    measure_provenance = json.loads((destination / "measure_provenance.json").read_text(encoding="utf-8"))
    assert measure_provenance[0]["measure"]["id"] == "builtin:set_building_north_axis"
    assert measure_provenance[0]["source_archive_sha256"] == hashlib.sha256(
        (destination / measure_provenance[0]["source_archive"]).read_bytes()
    ).hexdigest()
    assert not (tmp_path / "sessions" / session_id).exists()
    assert db.get_job(committed["job_id"])["status"] == "completed"
    assert all(path.stat().st_mode & 0o222 == 0 for path in destination.iterdir() if path.is_file())


def test_schema_v7_immediate_job_is_worker_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "var/workbench.sqlite3")
    db.init_db()
    with db.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    assert {"provenance", "authored_from", "patch_journal_json"} <= columns
    job_id = db.create_immediate_job("model_edit", "pilot", {"patch_count": 1})
    assert db.get_job(job_id)["status"] == "running"
    assert db.claim_next_job() is None


def test_authored_config_metadata_preserves_base_values_when_graph_parameter_is_unresolved():
    base = json.loads((PILOT_ROOT / "config.json").read_text(encoding="utf-8"))
    graph = extract_model_graph(PILOT_OSM)
    window = next(item for item in graph["project_parameters"] if item["key"] == "window_u")
    window["current_value"] = None
    updated = model_editor_service._updated_build_config(base, graph, "Null-safe authored metadata")
    assert updated["envelope"]["window_u"] == base["envelope"]["window_u"]
