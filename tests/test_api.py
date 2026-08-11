import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from workbench import db
from workbench import api as api_module
from workbench.api import app

# The product ships no city; these acceptance tests provision one the way an
# operator would, so every assertion below still describes a real install.
pytestmark = pytest.mark.usefixtures("reference_city")


def test_health_config_building_and_code_contracts():
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        health_payload = health.json()
        assert health_payload["ok"] is True, json.dumps(health_payload, indent=2)
        assert health_payload["readiness"] in {"READY", "WARNING"}

        config = client.get("/api/config/schema")
        assert config.status_code == 200
        payload = config.json()
        assert payload["metadata_labels"]["demanda__1"]["en"] == "Unconfirmed · likely post-intervention heating demand"
        assert payload["default"]["geometry"]["context_radius_m"] == 50.0

        building = client.get("/api/buildings/4252702YJ2745A")
        assert building.status_code == 200
        assert building.json()["properties"]["cluster"] == "BlocPluriP04"

        code = client.get("/api/code/symbols/build_model_with_config")
        assert code.status_code == 200
        assert "BuildResult" in code.json()["source"]

def test_storage_inventory_cleanup_plan_and_stale_confirmation():
    with TestClient(app) as client:
        overview = client.get("/api/storage")
        assert overview.status_code == 200
        payload = overview.json()
        assert payload["status"] in {"READY", "WARNING", "BLOCKED"}
        assert payload["capacity"]["reserve_floor_bytes"] > 0
        assert "immutable_runs" in payload["categories"]
        assert set(payload["job_admissions"]) >= {"preview", "simulation", "city", "lhs"}

        plan = client.post("/api/storage/cleanup/plan")
        assert plan.status_code == 200
        assert len(plan.json()["plan_token"]) == 64
        stale = client.post("/api/storage/cleanup", json={
            "plan_token": "0" * 64,
            "categories": ["export_cache"],
        })
        assert stale.status_code == 409

def test_storage_admission_errors_are_structured_507(monkeypatch):
    evidence = {"allowed": False, "reason": "protected_reserve_would_be_crossed"}

    def blocked():
        raise api_module.storage.StorageAdmissionError("disk reserve", evidence)

    monkeypatch.setattr(api_module.storage, "storage_overview", blocked)
    with TestClient(app) as client:
        response = client.get("/api/storage")
    assert response.status_code == 507
    assert response.json()["detail"]["storage"] == evidence

def test_geometry_endpoint_reports_party_wall():
    with TestClient(app) as client:
        config = client.get("/api/config/schema").json()["default"]
        result = client.post("/api/geometry/validate", json={
            "building_ref": "4252702YJ2745A",
            "config": config,
        })
        assert result.status_code == 200, result.text
        payload = result.json()
        assert payload["valid"] is True
        assert payload["ready"] is False
        assert payload["requires_approval"] is True
        assert any(item["action"] == "simplify" and item["approved"] is False
                   for item in payload["actions"])
        assert payload["party_length_m"] == pytest.approx(20.04, abs=0.02)
        assert len(payload["neighbors"]["features"]) == 11

def test_project_settings_expose_active_inputs():
    with TestClient(app) as client:
        response = client.get("/api/project/settings")
        assert response.status_code == 200
        settings = response.json()
        assert settings["building_dataset_id"] == "valencia-city"
        assert settings["template_dataset_id"] == "plantilla-v2"
        assert settings["datasets"]["weather_dataset_id"]["kind"] == "weather"

def test_capabilities_global_search_and_vector_tile():
    with TestClient(app) as client:
        capabilities = client.get("/api/capabilities")
        assert capabilities.status_code == 200
        items = capabilities.json()["capabilities"]
        assert items["model_builder"]["runtime_ready"] is True
        assert items["simulation"]["runtime_ready"] is True
        assert all(items["simulation"]["contract"]["checks"].values())
        neighborhood = items["neighborhood"]
        neighborhood_contract_ready = all(neighborhood["contract"]["checks"].values())
        assert neighborhood["contract"]["ok"] is neighborhood_contract_ready
        assert neighborhood["runtime_ready"] is (
            neighborhood["declared_ready"]
            and neighborhood["inspection"]["ok"]
            and neighborhood_contract_ready
        )
        if not neighborhood["runtime_ready"]:
            assert neighborhood["diagnostic"]["state"] in {
                "source_changed", "contract_failed", "dependency_blocked",
            }
        city = items["city"]
        city_contract_ready = all(city["contract"]["checks"].values())
        assert city["contract"]["ok"] is city_contract_ready
        assert city["runtime_ready"] is (
            city["declared_ready"] and city["inspection"]["ok"] and city_contract_ready
        )
        lhs = items["lhs"]
        lhs_contract_ready = all(lhs["contract"]["checks"].values())
        assert lhs["contract"]["ok"] is lhs_contract_ready
        assert lhs["runtime_ready"] is (
            lhs["declared_ready"] and lhs["inspection"]["ok"] and lhs_contract_ready
        )
        if not lhs["runtime_ready"]:
            assert lhs["diagnostic"]["state"] in {
                "source_changed", "contract_failed", "dependency_blocked",
            }
        editor = items["model_editor"]
        assert editor["runtime_ready"] is True
        assert all(editor["contract"]["checks"].values())

        search = client.get("/api/buildings/search", params={"query": "4252702YJ2745A"})
        assert search.status_code == 200
        assert search.json()["items"][0]["refparcela"] == "4252702YJ2745A"

        tile = client.get("/api/map/tiles/14/8174/6232.mvt")
        assert tile.status_code == 200
        assert tile.headers["content-type"].startswith("application/vnd.mapbox-vector-tile")
        assert len(tile.content) > 20

def test_model_inspector_graph_and_scene_routes_are_read_only(monkeypatch, tmp_path):
    osm = tmp_path / "model_python.osm"
    scene = tmp_path / "scene.json"
    osm.write_text("OSM", encoding="utf-8")
    scene.write_text(json.dumps({"surfaces": [{"id": "surface-a"}], "subsurfaces": []}), encoding="utf-8")
    artifact = {
        "requested_id": "simulation-child", "model_id": "model-parent",
        "source_kind": "committed_run", "refparcela": "pilot", "scenario_name": "Baseline",
        "verification_status": "VERIFIED", "immutable": True,
        "osm_path": str(osm), "scene_path": str(scene), "osm_sha256": "abc",
    }
    monkeypatch.setattr(api_module, "capability_status", lambda: {
        "capabilities": {"model_editor": {"runtime_ready": True}},
    })
    monkeypatch.setattr(api_module, "resolve_model_artifact", lambda model_id: artifact | {"requested_id": model_id})
    monkeypatch.setattr(api_module, "extract_model_graph", lambda path: {
        "schema_version": 1, "graph_sha256": "g" * 64, "counts": {"zones": 1},
    })
    monkeypatch.setattr(api_module, "enrich_scene_construction_ids", lambda path, payload: {
        **payload, "surfaces": [{**payload["surfaces"][0], "construction_id": "construction-a"}],
    })
    with TestClient(app) as client:
        graph = client.get("/api/models/simulation-child/graph")
        assert graph.status_code == 200
        assert graph.json()["model"]["model_id"] == "model-parent"
        assert "osm_path" not in graph.json()["model"]
        loaded_scene = client.get("/api/models/simulation-child/scene")
        assert loaded_scene.status_code == 200
        assert loaded_scene.json()["surfaces"][0]["construction_id"] == "construction-a"
    assert osm.read_text(encoding="utf-8") == "OSM"

def test_simulation_artifact_viewer_serves_only_verified_manifest_files(monkeypatch, tmp_path):
    report = tmp_path / "eplustbl.htm"
    diagnostics = tmp_path / "eplusout.err"
    report.write_text("<html><style>body{color:green}</style><body>EnergyPlus report</body></html>", encoding="utf-8")
    diagnostics.write_text("EnergyPlus completed successfully", encoding="utf-8")

    def artifact(path):
        return {
            "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }

    run = {
        "id": "verified-simulation",
        "run_type": "simulation",
        "verification_status": "VERIFIED",
        "artifact_dir": str(tmp_path),
        "artifacts": [artifact(report), artifact(diagnostics)],
    }
    monkeypatch.setattr(db, "get_run", lambda run_id: run if run_id == run["id"] else None)

    with TestClient(app) as client:
        html = client.get(f"/api/runs/{run['id']}/artifacts/eplustbl.htm")
        assert html.status_code == 200
        assert html.text.startswith("<html>")
        assert html.headers["content-type"].startswith("text/html")
        assert html.headers["content-disposition"] == 'inline; filename="eplustbl.htm"'
        assert html.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in html.headers["content-security-policy"]
        assert "default-src 'none'" in html.headers["content-security-policy"]

        text = client.get(f"/api/runs/{run['id']}/artifacts/eplusout.err")
        assert text.status_code == 200
        assert text.text == "EnergyPlus completed successfully"
        assert text.headers["content-type"].startswith("text/plain")
        assert "content-security-policy" not in text.headers

        assert client.get(
            f"/api/runs/{run['id']}/artifacts/manifest.json",
        ).status_code == 404
        assert client.get(
            f"/api/runs/{run['id']}/artifacts/model.idf",
        ).status_code == 404

def test_simulation_artifact_viewer_blocks_unverified_tampered_and_escaped_files(
    monkeypatch, tmp_path,
):
    artifact_root = tmp_path / "run"
    artifact_root.mkdir()
    diagnostics = artifact_root / "eplusout.err"
    diagnostics.write_text("original", encoding="utf-8")
    run = {
        "id": "simulation",
        "run_type": "simulation",
        "verification_status": "TAMPERED",
        "artifact_dir": str(artifact_root),
        "artifacts": [{
            "name": diagnostics.name,
            "sha256": hashlib.sha256(diagnostics.read_bytes()).hexdigest(),
            "size_bytes": diagnostics.stat().st_size,
        }],
    }
    monkeypatch.setattr(db, "get_run", lambda _run_id: run)

    with TestClient(app) as client:
        blocked = client.get("/api/runs/simulation/artifacts/eplusout.err")
        assert blocked.status_code == 409

        run["verification_status"] = "VERIFIED"
        diagnostics.write_text("changed", encoding="utf-8")
        changed = client.get("/api/runs/simulation/artifacts/eplusout.err")
        assert changed.status_code == 409
        assert "SHA-256" in changed.json()["detail"]

        outside = tmp_path / "qa_report.txt"
        outside.write_text("outside", encoding="utf-8")
        diagnostics.unlink()
        diagnostics.symlink_to(outside)
        run["artifacts"] = [{
            "name": diagnostics.name,
            "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            "size_bytes": outside.stat().st_size,
        }]
        escaped = client.get("/api/runs/simulation/artifacts/eplusout.err")
        assert escaped.status_code == 409
        assert "escaped" in escaped.json()["detail"]

        run["run_type"] = "model"
        wrong_type = client.get("/api/runs/simulation/artifacts/eplusout.err")
        assert wrong_type.status_code == 404

def test_model_editor_session_routes_are_typed_and_token_scoped(monkeypatch):
    monkeypatch.setattr(api_module, "capability_status", lambda: {
        "capabilities": {"model_editor": {"runtime_ready": True}},
    })
    calls = []
    monkeypatch.setattr(api_module, "editor_options", lambda: {"mode_default": "advanced", "guardrail": "warn_not_block"})
    monkeypatch.setattr(api_module, "create_session", lambda model_id: {"session": {"id": "a" * 32}, "model_id": model_id, "token": "t" * 32})
    monkeypatch.setattr(api_module, "session_detail", lambda session_id, token: {"session_id": session_id, "token_length": len(token)})
    monkeypatch.setattr(api_module, "apply_session_edits", lambda session_id, token, patches: calls.append((session_id, token, patches)) or {"reports": [{"status": "applied"}]})
    monkeypatch.setattr(api_module, "import_session_measure", lambda session_id, token, path: {"session_id": session_id, "token_length": len(token), "archive_size": path.stat().st_size})
    monkeypatch.setattr(api_module, "preflight_session", lambda session_id, token: {"ready": True, "session_id": session_id, "token_length": len(token)})
    monkeypatch.setattr(api_module, "commit_session", lambda session_id, token, name: {"id": "authored-run", "provenance": "authored", "scenario_name": name})
    monkeypatch.setattr(api_module, "discard_session", lambda session_id, token: {"session_id": session_id, "discarded": True})
    token = "t" * 32
    with TestClient(app) as client:
        assert client.get("/api/models/editor/options").json()["mode_default"] == "advanced"
        created = client.post("/api/models/source-model/session")
        assert created.status_code == 200 and created.json()["token"] == token
        recovered = client.post("/api/models/session/" + "a" * 32, json={"token": token})
        assert recovered.status_code == 200 and recovered.json()["token_length"] == 32
        edited = client.post("/api/models/session/" + "a" * 32 + "/edits", json={
            "token": token,
            "patches": [
                {"op": "project_parameter.update", "payload": {"key": "wall_u", "value": 1.5}},
                {"op": "subsurface.create", "target_id": "surface-a", "payload": {"normalized_rect": {"u_min": 0.3, "u_max": 0.7, "v_min": 0.25, "v_max": 0.75}}},
            ],
        })
        assert edited.status_code == 200 and calls[0][2][0]["op"] == "project_parameter.update"
        assert calls[0][2][1]["op"] == "subsurface.create"
        invalid = client.post("/api/models/session/" + "a" * 32 + "/edits", json={
            "token": token, "patches": [{"op": "arbitrary.python", "payload": {}}],
        })
        assert invalid.status_code == 422
        untrusted = client.post("/api/models/session/" + "a" * 32 + "/measures", data={"token": token, "trusted": "false"}, files={"file": ("measure.zip", b"zip", "application/zip")})
        assert untrusted.status_code == 422
        uploaded = client.post("/api/models/session/" + "a" * 32 + "/measures", data={"token": token, "trusted": "true"}, files={"file": ("measure.zip", b"zip", "application/zip")})
        assert uploaded.status_code == 200 and uploaded.json()["archive_size"] == 3
        assert client.post("/api/models/session/" + "a" * 32 + "/preflight", json={"token": token}).json()["ready"] is True
        committed = client.post("/api/models/session/" + "a" * 32 + "/commit", json={"token": token, "scenario_name": "Authored test"})
        assert committed.json()["provenance"] == "authored"
        assert client.post("/api/models/session/" + "a" * 32 + "/discard", json={"token": token}).json()["discarded"] is True

def test_simulation_job_recovery_is_typed_and_reports_active_job(monkeypatch):
    simulation = {
        "id": "simulation-job", "kind": "simulation", "status": "running",
        "payload": {"parent_run_id": "parent-a"},
    }
    monkeypatch.setattr(db, "find_current_simulation_job", lambda parent=None: simulation)
    monkeypatch.setattr(
        db, "get_job",
        lambda job_id: simulation if job_id == "simulation-job"
        else {"id": job_id, "kind": "preview", "status": "running"},
    )
    with TestClient(app) as client:
        active = client.get("/api/simulations/jobs/active")
        assert active.status_code == 200
        assert active.json()["job"]["id"] == "simulation-job"
        restored = client.get("/api/simulations/jobs/simulation-job")
        assert restored.status_code == 200
        assert restored.json()["payload"]["parent_run_id"] == "parent-a"
        wrong_kind = client.get("/api/simulations/jobs/preview-job")
        assert wrong_kind.status_code == 422

def test_authored_simulation_pair_route_preserves_automatic_baseline_link(monkeypatch):
    notified = []
    monkeypatch.setattr(api_module, "create_authored_simulation_pair", lambda parent: {
        "schema_version": 1,
        "refparcela": "9876501YJ2797F",
        "automatic_baseline_model_id": "automatic-model",
        "authored_model_id": parent,
        "baseline": {"model_id": "automatic-model", "simulation_run_id": "baseline-sim", "job": None, "status": "completed"},
        "authored": {"model_id": parent, "simulation_run_id": None, "job": {"id": "authored-job"}, "status": "queued"},
    })
    monkeypatch.setattr(api_module.manager, "notify", lambda: notified.append(True))
    with TestClient(app) as client:
        notified.clear()
        response = client.post("/api/simulations/paired", json={"parent_run_id": "authored-model"})
    assert response.status_code == 202
    assert response.json()["baseline"]["simulation_run_id"] == "baseline-sim"
    assert response.json()["authored"]["job"]["id"] == "authored-job"
    assert notified == [True]

def test_preview_recovery_routes_are_typed_and_expose_ready_ledger(monkeypatch):
    preview = {
        "id": "preview-job", "kind": "preview", "status": "ready",
        "refparcela": "4252702YJ2745A", "stage": "Preview ready for review",
    }
    ledger = {
        "active": None,
        "items": [{
            **preview, "scenario_name": "Pilot baseline", "baseline_profile": "pilot",
            "created_at": "2026-07-15T00:00:00Z", "updated_at": "2026-07-15T00:01:00Z",
            "queue_position": None,
            "artifact_state": {"status": "AVAILABLE", "recoverable": True, "issues": []},
        }],
        "ready_count": 1,
    }
    monkeypatch.setattr(api_module, "recoverable_previews", lambda limit=20: ledger)
    monkeypatch.setattr(db, "find_current_job", lambda kind: preview if kind == "preview" else None)
    monkeypatch.setattr(
        db, "get_job",
        lambda job_id: preview if job_id == "preview-job"
        else {"id": job_id, "kind": "simulation", "status": "running"},
    )
    with TestClient(app) as client:
        recovery = client.get("/api/previews/recoverable")
        assert recovery.status_code == 200
        assert recovery.json()["items"][0]["artifact_state"]["status"] == "AVAILABLE"
        active = client.get("/api/previews/jobs/active")
        assert active.status_code == 200
        assert active.json()["job"]["id"] == "preview-job"
        wrong_kind = client.get("/api/previews/simulation-job")
        assert wrong_kind.status_code == 422

def test_neighborhood_map_resource_is_lightweight_cached_and_conditional(tmp_path, monkeypatch):
    map_data = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"refparcela": "A", "cluster": "BlocPluriP04", "padding": "x" * 3000},
            "geometry": {"type": "Polygon", "coordinates": [[
                [-0.4, 39.49], [-0.399, 39.49], [-0.399, 39.491], [-0.4, 39.491], [-0.4, 39.49],
            ]]},
        }],
    }
    payload = json.dumps(map_data, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(payload).hexdigest()
    path = tmp_path / f"{fingerprint}.geojson"
    path.write_bytes(payload)
    descriptor = {
        "fingerprint": fingerprint, "sha256": fingerprint, "feature_count": 1,
        "bounds": [-0.4, 39.49, -0.399, 39.491], "crs": "EPSG:4326",
        "size_bytes": len(payload), "url": f"/api/neighborhood/maps/{fingerprint}.geojson",
    }

    def preflight(*, include_map=True):
        return {
            "schema_version": 1, "summary": {"buildings": 1}, "representatives": [],
            "map": map_data if include_map else None, "map_descriptor": descriptor,
        }

    def lookup(value):
        if value != fingerprint:
            raise FileNotFoundError(value)
        return path

    monkeypatch.setattr(api_module, "neighborhood_preflight", preflight)
    monkeypatch.setattr(api_module, "cached_map_path", lookup)
    with TestClient(app) as client:
        light = client.get("/api/neighborhood/preflight", params={"include_map": "false"})
        assert light.status_code == 200
        assert light.json()["map"] is None
        assert light.json()["map_descriptor"]["feature_count"] == 1
        legacy = client.get("/api/neighborhood/preflight")
        assert legacy.status_code == 200
        assert len(legacy.json()["map"]["features"]) == 1

        resource = client.get(
            descriptor["url"], headers={"Accept-Encoding": "gzip"},
        )
        assert resource.status_code == 200
        assert resource.json()["features"][0]["properties"]["refparcela"] == "A"
        assert resource.headers["etag"] == f'"{fingerprint}"'
        assert resource.headers["cache-control"].endswith("immutable")
        assert resource.headers.get("content-encoding") == "gzip"
        unchanged = client.get(descriptor["url"], headers={"If-None-Match": f'"{fingerprint}"'})
        assert unchanged.status_code == 304
        assert client.get(f"/api/neighborhood/maps/{'0' * 64}.geojson").status_code == 404

def test_neighborhood_run_map_is_conditional_and_blocks_unverified_artifacts(tmp_path, monkeypatch):
    path = tmp_path / "map.geojson"
    path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()

    def lookup(run_id):
        if run_id == "tampered":
            raise PermissionError("Neighborhood map is not verified: TAMPERED")
        return path

    monkeypatch.setattr(api_module, "neighborhood_map", lookup)
    with TestClient(app) as client:
        response = client.get("/api/neighborhood/runs/verified/map")
        assert response.status_code == 200
        assert response.headers["etag"] == f'"{fingerprint}"'
        unchanged = client.get(
            "/api/neighborhood/runs/verified/map",
            headers={"If-None-Match": f'"{fingerprint}"'},
        )
        assert unchanged.status_code == 304
        assert client.get("/api/neighborhood/runs/tampered/map").status_code == 409
