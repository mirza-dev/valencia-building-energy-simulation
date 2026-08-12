"""FastAPI application for the local Model Builder Research Workbench."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from model_config import BuildConfig, config_for_profile, flatten_config, profile_catalog
from workbench import __version__, db, integrity, renderer_provenance, stock_adapter, storage
from workbench.capabilities import capability_status
from workbench.capability_sync import revalidation_plan
from workbench.environment import TEST_REQUEST_HEADER, runtime_environment
from workbench.jobs import log_paths, manager, read_log_chunk
from workbench.schemas import (
    BatchPreflightRequest, BatchRequest, CapabilityRevalidationRequest, CityRunRequest,
    CommitRequest, DatasetFieldNoteRequest, FieldMappingRequest, GeometryRequest,
    NeighborhoodRunRequest, PreviewRequest,
    ModelEditCommitRequest, ModelEditRequest, ModelEditTokenRequest, ProjectSettingsRequest,
    ScenarioRequest, SimulationRequest, StockInputPolicyFields,
    StockInputPolicyPreflightRequest, StorageCleanupRequest,
)
from workbench.data_dictionary import (
    dataset_dictionary, save_field_note, workflow_input_contract,
)
from workbench.stock_input_policy_service import (
    get_policy as get_stock_input_policy,
    preflight_policy as preflight_stock_input_policy,
    save_project_policy as save_stock_input_policy,
)
from stock_input_policy import StockPolicyError
from workbench.simulation_service import (
    compare_simulations, create_authored_simulation_pair, create_simulation_job, eligible_models,
    list_simulations, simulation_detail,
)
from workbench.neighborhood_service import (
    create_neighborhood_job, list_neighborhood_run_summaries, list_neighborhood_runs, neighborhood_detail,
    neighborhood_map, neighborhood_options, neighborhood_preflight,
)
from workbench.neighborhood_map_cache import cached_map_path
from workbench.city_service import (
    city_detail, city_map_metrics, city_options, city_preflight, city_tile_source,
    create_city_job, list_city_run_summaries, list_city_runs,
)
from workbench.lhs_service import (
    compare_lhs, create_lhs_job, lhs_artifact, lhs_detail, lhs_figure,
    lhs_preflight, list_lhs_runs,
)
from workbench.model_graph import (
    enrich_scene_construction_ids, extract_model_graph, public_artifact_metadata,
    resolve_model_artifact,
)
from workbench.model_editor_service import (
    apply_session_edits, commit_session, create_session, discard_session,
    editor_options, import_session_measure, preflight_session, session_detail,
)
from workbench.scenario_service import (
    create_scenario_job, scenario_detail, scenario_options,
)
from workbench.stock_compare import compare_stock_runs
from workbench.service import (
    bootstrap,
    building_vector_tile,
    city_focus_view,
    city_vector_tile,
    code_symbol,
    commit_preview,
    export_run,
    import_dataset,
    ingest_eu_database as service_ingest_eu_database,
    get_project_settings,
    normalize_gis_dataset,
    preview_detail,
    recoverable_previews,
    query_buildings,
    search_buildings,
    read_building,
    read_gdf,
    run_with_renderer_summary,
    serialize_row,
    set_project_settings,
    system_health,
    verify_run_artifacts,
    validate_geometry,
    workbench_base_config,
    GISValidationError,
)


PROJECT = Path(__file__).resolve().parents[2]

RUN_ARTIFACT_VIEW_TYPES = {
    "eplustbl.htm": "text/html; charset=utf-8",
    "model.idf": "text/plain; charset=utf-8",
    "model_python.osm": "text/plain; charset=utf-8",
    "parent_model.osm": "text/plain; charset=utf-8",
    "eplusout.err": "text/plain; charset=utf-8",
    "qa_report.txt": "text/plain; charset=utf-8",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bootstrap()
    manager.start()
    manager.notify()
    yield
    manager.stop()


app = FastAPI(
    title="Model Builder Research Workbench",
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.middleware("http")
async def enforce_test_runtime_identity(request: Request, call_next):
    """Prevent browser tests from ever reaching a production Workbench process."""
    environment = runtime_environment()
    supplied = request.headers.get(TEST_REQUEST_HEADER)
    if supplied and environment["mode"] != "test":
        return JSONResponse(
            status_code=409,
            content={"detail": "Test-tagged requests are forbidden on the production Workbench"},
        )
    if (
        environment["mode"] == "test"
        and environment["test_request_header_required"]
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and supplied != environment["test_run_id"]
    ):
        return JSONResponse(
            status_code=403,
            content={"detail": f"Valid {TEST_REQUEST_HEADER} header required in test mode"},
        )
    return await call_next(request)


@app.exception_handler(storage.StorageAdmissionError)
async def storage_admission_error(_request: Request, exc: storage.StorageAdmissionError):
    return JSONResponse(
        status_code=507,
        content={"detail": {"message": str(exc), "storage": exc.evidence}},
    )


def not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


def _verified_run_artifact(run_id: str, artifact_name: str) -> tuple[bytes, str]:
    """Load one explicitly viewable, manifest-bound simulation artifact."""
    run = db.get_run(run_id)
    if run is None:
        raise not_found(run_id)
    if run.get("run_type") != "simulation":
        raise not_found("Artifact viewer is only available for simulation runs")
    if run.get("verification_status") != "VERIFIED":
        raise HTTPException(
            status_code=409,
            detail="Artifact viewing requires a VERIFIED immutable run",
        )
    media_type = RUN_ARTIFACT_VIEW_TYPES.get(artifact_name)
    if media_type is None:
        raise not_found("Artifact is not available in the read-only viewer")
    manifest_item = next(
        (
            item for item in run.get("artifacts", [])
            if item.get("name") == artifact_name
        ),
        None,
    )
    if manifest_item is None:
        raise not_found("Artifact is not present in this run manifest")

    artifact_root = Path(run["artifact_dir"]).resolve()
    artifact_path = (artifact_root / artifact_name).resolve()
    try:
        artifact_path.relative_to(artifact_root)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Artifact path escaped its immutable run") from exc
    if not artifact_path.is_file():
        raise HTTPException(status_code=409, detail="Manifest artifact is missing from disk")

    try:
        payload = artifact_path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=409, detail="Manifest artifact could not be read") from exc
    expected_size = int(manifest_item.get("size_bytes", -1))
    expected_sha256 = str(manifest_item.get("sha256", ""))
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if len(payload) != expected_size or actual_sha256 != expected_sha256:
        raise HTTPException(
            status_code=409,
            detail="Manifest artifact failed size or SHA-256 verification",
        )
    return payload, media_type


@app.get("/api/health")
def health():
    return system_health()


@app.get("/api/storage")
def storage_status():
    return storage.storage_overview()


@app.post("/api/storage/cleanup/plan")
def storage_cleanup_plan():
    return storage.build_cleanup_plan()


@app.post("/api/storage/cleanup")
def storage_cleanup(request: StorageCleanupRequest):
    try:
        return storage.execute_cleanup(request.plan_token, request.categories)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/storage/archive/{run_id}")
def storage_archive_run(run_id: str):
    run = db.get_run(run_id)
    if run is None:
        raise not_found(run_id)
    verification = verify_run_artifacts(run_id)
    if verification["status"] != "VERIFIED":
        raise HTTPException(status_code=409, detail="Only VERIFIED runs can be archived")
    try:
        return storage.archive_export(run_id, export_run(run_id))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/capabilities")
def capabilities():
    return JSONResponse(capability_status(), headers={"Cache-Control": "no-store"})


@app.post("/api/capabilities/refresh")
def refresh_capabilities():
    return JSONResponse(capability_status(), headers={"Cache-Control": "no-store"})


@app.get("/api/capabilities/revalidation/active")
def active_capability_revalidation():
    jobs = []
    for kind in ("neighborhood", "city"):
        job = db.find_current_job(kind)
        if job and job.get("payload", {}).get("revalidation_candidate"):
            jobs.append(job)
    return {"jobs": jobs}


@app.post("/api/capabilities/{name}/revalidation/plan")
def capability_revalidation_plan(name: str):
    item = capability_status()["capabilities"].get(name)
    if item is None:
        raise not_found(name)
    return revalidation_plan(name, item)


@app.post("/api/capabilities/{name}/revalidation", status_code=202)
def start_capability_revalidation(name: str, request: CapabilityRevalidationRequest):
    item = capability_status()["capabilities"].get(name)
    if item is None:
        raise not_found(name)
    plan = revalidation_plan(name, item)
    if not plan.get("eligible"):
        raise HTTPException(status_code=409, detail=plan)
    if request.plan_token != plan["plan_token"]:
        raise HTTPException(status_code=409, detail="Revalidation plan is stale; review the current source again")
    try:
        if name == "neighborhood":
            job = create_neighborhood_job(revalidation=True)
        elif name == "city":
            job = create_city_job(revalidation=True)
        else:
            raise HTTPException(status_code=422, detail="Full revalidation is not supported for this capability")
        manager.notify()
        return job
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/datasets")
def datasets():
    return db.list_datasets()


@app.post("/api/datasets")
async def upload_dataset(
    kind: Literal[
        "gis", "tipo15", "template", "weather", "ddy",
        "stock", "eu_database", "microclimate",
    ] = Form(...),
    name: str = Form(...),
    file: UploadFile = File(...),
):
    filename = Path(file.filename or "upload.bin").name
    suffix = Path(filename).suffix.lower()
    if kind == "gis" and suffix == ".shp":
        raise HTTPException(
            status_code=422,
            detail="Upload Shapefiles as one ZIP containing .shp/.dbf/.shx/.prj/.cpg sidecars",
        )
    allowed = {
        "gis": {".gpkg", ".geojson", ".json", ".zip"},
        "tipo15": {".csv"},
        "template": {".osm"},
        "weather": {".epw"},
        "ddy": {".ddy"},
        # A stock file is self-describing: it carries the fields the engine
        # reads, so no companion ledger is required alongside it.
        "stock": {".gpkg", ".geojson", ".json"},
        "eu_database": {".db", ".sqlite", ".sqlite3", ".gpkg"},
        "microclimate": {".zip"},
    }
    if suffix not in allowed[kind]:
        raise HTTPException(status_code=422, detail=f"Unsupported {kind} file type: {suffix}")
    import_capacity = storage.admission("dataset_import", requested_bytes=1280 * 1024 ** 2)
    if not import_capacity["allowed"]:
        raise storage.StorageAdmissionError(
            f"Insufficient protected disk capacity for dataset import: {import_capacity['reason']}",
            import_capacity,
        )
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            temp_path = Path(handle.name)
            received = 0
            while chunk := await file.read(1024 * 1024):
                received += len(chunk)
                if received > 512 * 1024 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail="Dataset exceeds the 512 MB local import limit",
                    )
                handle.write(chunk)
        return import_dataset(kind, name, temp_path, original_name=filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


@app.post("/api/datasets/{dataset_id}/field-map")
def field_map_dataset(dataset_id: str, request: FieldMappingRequest):
    try:
        mapping_capacity = storage.admission("gis_normalization", requested_bytes=512 * 1024 ** 2)
        if not mapping_capacity["allowed"]:
            raise storage.StorageAdmissionError(
                f"Insufficient protected disk capacity for GIS normalization: {mapping_capacity['reason']}",
                mapping_capacity,
            )
        return normalize_gis_dataset(dataset_id, request.model_dump())
    except KeyError as exc:
        raise not_found(dataset_id) from exc
    except GISValidationError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc), "errors": exc.errors}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/datasets/{dataset_id}/dictionary")
def data_dictionary(dataset_id: str):
    try:
        return dataset_dictionary(dataset_id)
    except KeyError as exc:
        raise not_found(dataset_id) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/datasets/{dataset_id}/dictionary/{field_name}/note")
def update_dictionary_note(
    dataset_id: str, field_name: str, request: DatasetFieldNoteRequest,
):
    try:
        return save_field_note(
            dataset_id, field_name, request.note, request.semantic_label,
        )
    except KeyError as exc:
        raise not_found(dataset_id) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _policy_aware_contract(
    contract: dict[str, Any], policy_context: dict[str, Any],
) -> dict[str, Any]:
    """Show the resolved project default in the compact contract cards."""
    policy = policy_context.get("resolved_policy") or policy_context.get("requested_policy") or {}
    dataset = policy_context.get("dataset") or {}
    ground_labels = {
        "tipo15_family_fallback": "Tipo15-derived → family fallback",
        "family_default": "Family default only",
        "force_unconditioned": "Force all unconditioned buffer",
        "force_conditioned": "Force all conditioned",
    }
    residential_labels = {
        "tipo15_proxy": "Tipo15 → cluster-ratio proxy",
        "field_proxy": f"{policy.get('residential_area_field') or 'Selected field'} → proxy",
        "proxy_only": "Cluster-ratio proxy only",
    }
    footprint = (
        "EPSG:25830 geometry area"
        if policy.get("footprint_area_mode") == "geometry_epsg25830"
        else str(policy.get("footprint_area_field") or "Selected numeric field")
    )
    values = {
        "stock": str(dataset.get("name") or policy.get("gis_dataset_id") or "Not selected"),
        "reference": str(policy.get("reference_field") or "refparcela"),
        "scope": str(policy.get("district_field") or "nombre"),
        "floors": str(policy.get("floors_field") or "altura_max"),
        "cluster": str(policy.get("cluster_field") or "cluster"),
        "footprint": footprint,
        "ground": ground_labels.get(str(policy.get("ground_floor_mode")), str(policy.get("ground_floor_mode"))),
        "residential_area": residential_labels.get(
            str(policy.get("residential_area_mode")), str(policy.get("residential_area_mode")),
        ),
    }
    inputs = [item | ({"value": values[item["key"]]} if item["key"] in values else {})
              for item in contract.get("inputs", [])]
    return contract | {
        "inputs": inputs,
        "locked": False,
        "summary": (
            "Part C/D opens the pipeline-filled stock contract. Project defaults and reviewed "
            "run overrides are resolved into immutable job inputs."
        ),
    }


@app.get("/api/workflows/{workflow}/input-policy")
def workflow_policy(workflow: str):
    try:
        contract = workflow_input_contract(workflow)
        if workflow in {"neighborhood", "city"}:
            policy_context = get_stock_input_policy(workflow)
            return _policy_aware_contract(contract, policy_context) | policy_context | {
                "phase": 2, "locked": False,
                "status": "Project default policy — configurable; every run resolves an immutable copy.",
            }
        return contract
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/workflows/{workflow}/input-policy")
def update_workflow_policy(workflow: str, request: StockInputPolicyFields):
    try:
        contract = workflow_input_contract(workflow)
        policy_context = save_stock_input_policy(workflow, request.model_dump())
        return _policy_aware_contract(contract, policy_context) | policy_context | {
            "phase": 2, "locked": False,
            "status": "Project default policy — configurable; every run resolves an immutable copy.",
        }
    except ValueError as exc:
        raise HTTPException(status_code=404 if workflow not in {"neighborhood", "city"} else 422, detail=str(exc)) from exc


@app.post("/api/workflows/{workflow}/input-policy/preflight")
def preflight_workflow_policy(workflow: str, request: StockInputPolicyPreflightRequest):
    try:
        return preflight_stock_input_policy(
            workflow, request.input_policy_override, district=request.district,
            building_ref=request.building_ref,
        )
    except (ValueError, StockPolicyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/project/settings")
def project_settings():
    return get_project_settings()


@app.patch("/api/project/settings")
def update_project_settings(request: ProjectSettingsRequest):
    try:
        return set_project_settings(request.model_dump(exclude_unset=True))
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/buildings")
def buildings(
    query: str | None = None,
    bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat"),
    limit: int = Query(1000, ge=1, le=2500),
):
    parsed_bbox = None
    if bbox:
        try:
            values = tuple(float(value) for value in bbox.split(","))
            if len(values) != 4:
                raise ValueError
            parsed_bbox = values
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="bbox must contain four comma-separated numbers") from exc
    return query_buildings(query=query, bbox_4326=parsed_bbox, limit=limit)


@app.get("/api/buildings/search")
def building_search(query: str = Query(..., min_length=2), limit: int = Query(20, ge=1, le=50)):
    return search_buildings(query, limit)


@app.get("/api/map/tiles/{z}/{x}/{y}.mvt")
def map_tile(z: int, x: int, y: int):
    if not 0 <= z <= 22 or not 0 <= x < 2 ** z or not 0 <= y < 2 ** z:
        raise HTTPException(status_code=422, detail="Invalid tile coordinate")
    path = workbench_base_config().data.building_path
    payload = building_vector_tile(z, x, y, str(path), path.stat().st_mtime_ns)
    return Response(payload, media_type="application/vnd.mapbox-vector-tile",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/buildings/{refparcela}")
def building(refparcela: str):
    path = workbench_base_config().data.building_path
    try:
        row = read_building(refparcela, path)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    return serialize_row(row, read_gdf(path).crs)


@app.get("/api/config/schema")
def config_schema():
    base = workbench_base_config()
    return {
        "schema": BuildConfig.model_json_schema(),
        "default": base.model_dump(mode="json"),
        "profiles": profile_catalog(base),
        "metadata_labels": {
            "demanda_ca": {"tr": "Mevcut durum ısıtma talebi", "en": "Baseline heating demand"},
            "demanda__1": {"tr": "Teyitsiz · olası müdahale sonrası ısıtma talebi", "en": "Unconfirmed · likely post-intervention heating demand"},
            "calificaci": {"tr": "Mevcut ısıtma sınıfı", "en": "Baseline heating rating"},
            "califica_1": {"tr": "Müdahale sonrası ısıtma sınıfı", "en": "Post-intervention heating rating"},
        },
        "read_only_system_fields": [
            "shading.blind_name", "operation.output_variables",
            "template.space_type_names", "template.schedule_names",
        ],
    }


@app.get("/api/config/profiles/{profile_id}")
def profile(profile_id: str):
    try:
        config = config_for_profile(workbench_base_config(), profile_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    return config.model_dump(mode="json")


@app.post("/api/geometry/validate")
def geometry_validate(request: GeometryRequest):
    try:
        return validate_geometry(request.building_ref, request.config)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/previews", status_code=202)
def create_preview(request: PreviewRequest):
    payload = request.model_dump(mode="json")
    reservation = storage.reserve_payload("preview", {})["storage_reservation"]
    scenario_id = db.create_scenario(
        request.config.provenance.baseline_profile,
        request.config.provenance.scenario_name,
        request.config.model_dump(mode="json"),
        [item.model_dump(mode="json") for item in request.config.provenance.overrides],
        request.geometry_actions,
    )
    payload["scenario_id"] = scenario_id
    payload["storage_reservation"] = reservation
    job_id = db.create_job("preview", request.building_ref, payload)
    manager.notify()
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/previews/recoverable")
def preview_recovery(limit: int = Query(20, ge=1, le=100)):
    return recoverable_previews(limit)


@app.get("/api/previews/jobs/active")
def active_preview_job():
    return {"job": db.find_current_job("preview")}


@app.get("/api/previews/{job_id}")
def get_preview(job_id: str):
    try:
        return preview_detail(job_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/previews/{job_id}/commit")
def commit(job_id: str, request: CommitRequest | None = None):
    try:
        request = request or CommitRequest()
        return commit_preview(
            job_id, view_state=request.view_state,
            screenshot_data_url=request.screenshot_data_url,
        )
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise not_found(job_id)
    return job


@app.get("/api/jobs/{job_id}/events")
async def job_events(
    job_id: str, after_id: int = 0, stdout_offset: int = 0, stderr_offset: int = 0,
):
    if db.get_job(job_id) is None:
        raise not_found(job_id)

    async def stream():
        last_id = max(0, after_id)
        log_offsets = {
            "stdout": max(0, stdout_offset),
            "stderr": max(0, stderr_offset),
        }
        stdout_path, stderr_path = log_paths(job_id)
        paths = {"stdout": stdout_path, "stderr": stderr_path}
        idle_terminal = 0
        while True:
            events = db.list_events(job_id, last_id)
            for event in events:
                last_id = event["id"]
                yield f"id: {last_id}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            log_activity = False
            for stream_name, path in paths.items():
                requested_offset = log_offsets[stream_name]
                next_offset, content = read_log_chunk(path, requested_offset)
                log_offsets[stream_name] = next_offset
                if content:
                    log_activity = True
                    payload = json.dumps(
                        {
                            "stream": stream_name,
                            "offset": next_offset,
                            "reset": next_offset < requested_offset,
                            "text": content,
                        },
                        ensure_ascii=False,
                    )
                    yield f"event: log\ndata: {payload}\n\n"
            job = db.get_job(job_id)
            terminal = job and job["status"] in {"ready", "completed", "failed", "canceled"}
            idle_terminal = idle_terminal + 1 if terminal and not events and not log_activity else 0
            if idle_terminal >= 2:
                break
            yield ": keepalive\n\n"
            await asyncio.sleep(0.35)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/log")
def job_log(job_id: str, stream: Literal["stdout", "stderr"] = "stderr"):
    if db.get_job(job_id) is None:
        raise not_found(job_id)
    stdout_path, stderr_path = log_paths(job_id)
    path = stdout_path if stream == "stdout" else stderr_path
    if not path.exists():
        raise not_found(f"{stream} log for {job_id}")
    return FileResponse(path, filename=path.name, media_type="text/plain")


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = db.request_cancel(job_id)
    if job is None:
        raise not_found(job_id)
    return job


@app.post("/api/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str):
    try:
        job = db.retry_failed_job(job_id)
    except KeyError as exc:
        raise not_found(job_id) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    manager.notify()
    return job


@app.get("/api/runs")
def runs():
    listed = db.list_runs()
    current = renderer_provenance.current_source_descriptor(snapshot=False)["fingerprint"]
    return [
        run_with_renderer_summary(run, current_fingerprint=current)
        if run["run_type"] == "model" else run | {"renderer": None}
        for run in listed
    ]


@app.get("/api/runs/compare")
def compare_runs(left: str, right: str):
    if left == right:
        raise HTTPException(status_code=422, detail="A run cannot be compared with itself")
    left_run, right_run = db.get_run(left), db.get_run(right)
    if left_run is None or right_run is None:
        raise not_found("One or both runs were not found")
    if left_run["run_type"] != "model" or right_run["run_type"] != "model":
        raise HTTPException(status_code=422, detail="Use the simulation comparison endpoint")
    flat_left, flat_right = flatten_config(BuildConfig.model_validate(left_run["config"])), flatten_config(
        BuildConfig.model_validate(right_run["config"]))
    keys = sorted(set(flat_left) | set(flat_right))
    differences = [
        {"field": key, "left": flat_left.get(key), "right": flat_right.get(key)}
        for key in keys if flat_left.get(key) != flat_right.get(key)
    ]

    def part_g(run):
        path = Path(run["artifact_dir"]) / "scenario_settings.json"
        if path.exists():
            item = json.loads(path.read_text(encoding="utf-8"))
            return {
                "heat_delta_c": item.get("heat_delta_c", 0.0),
                "cool_delta_c": item.get("cool_delta_c", 0.0),
                "weather_snapshot_hash": item.get("weather_snapshot_hash"),
            }
        input_path = Path(run["artifact_dir"]) / "input_manifest.json"
        inputs = json.loads(input_path.read_text(encoding="utf-8")) if input_path.exists() else {}
        return {
            "heat_delta_c": 0.0,
            "cool_delta_c": 0.0,
            "weather_snapshot_hash": inputs.get("weather", {}).get("snapshot_hash"),
        }

    part_g_left, part_g_right = part_g(left_run), part_g(right_run)
    differences.extend(
        {"field": f"part_g.{key}", "left": part_g_left.get(key), "right": part_g_right.get(key)}
        for key in ("heat_delta_c", "cool_delta_c", "weather_snapshot_hash")
        if part_g_left.get(key) != part_g_right.get(key)
    )
    if left_run.get("patch_journal") != right_run.get("patch_journal"):
        differences.append({
            "field": "editor.patch_journal",
            "left": left_run.get("patch_journal", []),
            "right": right_run.get("patch_journal", []),
        })
    if left_run.get("provenance") != right_run.get("provenance"):
        differences.append({
            "field": "editor.provenance",
            "left": left_run.get("provenance", "pipeline"),
            "right": right_run.get("provenance", "pipeline"),
        })
    current_renderer = renderer_provenance.current_source_descriptor(snapshot=False)["fingerprint"]

    def evidence(run):
        input_path = Path(run["artifact_dir"]) / "input_manifest.json"
        inputs = json.loads(input_path.read_text(encoding="utf-8")) if input_path.exists() else {}
        return {
            "verification_status": run["verification_status"],
            "input_snapshots": inputs,
            "provenance": run["config"].get("provenance", {}),
            "stats": run["stats"], "qa": run["qa"],
            "facade_qa": run["stats"].get("facade_qa", []),
            "artifacts": run["artifacts"],
            "renderer": run_with_renderer_summary(
                run, current_fingerprint=current_renderer,
            )["renderer"],
            "scenario_settings": part_g(run),
        }
    return {
        "left": left_run, "right": right_run, "differences": differences,
        "evidence": {"left": evidence(left_run), "right": evidence(right_run)},
    }


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    run = db.get_run(run_id)
    if run is None:
        raise not_found(run_id)
    return run_with_renderer_summary(run) if run["run_type"] == "model" else run | {"renderer": None}


@app.get("/api/runs/{run_id}/artifacts/{artifact_name}")
def run_artifact_view(run_id: str, artifact_name: str):
    payload, media_type = _verified_run_artifact(run_id, artifact_name)
    headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Disposition": f'inline; filename="{artifact_name}"',
        "X-Content-Type-Options": "nosniff",
    }
    if media_type.startswith("text/html"):
        headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data:; base-uri 'none'; form-action 'none'"
        )
    return Response(payload, media_type=media_type, headers=headers)


@app.get("/api/runs/{run_id}/scene")
def run_scene(run_id: str):
    run = db.get_run(run_id)
    if run is None:
        raise not_found(run_id)
    path = Path(run["artifact_dir"]) / "scene.json"
    if not path.exists():
        raise not_found("scene.json")
    scene = json.loads(path.read_text(encoding="utf-8"))
    osm = Path(run["artifact_dir"]) / "model_python.osm"
    return enrich_scene_construction_ids(osm, scene) if run["run_type"] == "model" and osm.is_file() else scene


def _model_artifact(model_id: str):
    capability = capability_status()["capabilities"].get("model_editor")
    if not capability or not capability["runtime_ready"]:
        raise HTTPException(status_code=503, detail="Model inspector capability is not ready")
    try:
        return resolve_model_artifact(model_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except FileNotFoundError as exc:
        raise not_found(str(exc)) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/models/{model_id}/graph")
def model_graph(model_id: str):
    artifact = _model_artifact(model_id)
    try:
        graph = extract_model_graph(artifact["osm_path"])
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"model": public_artifact_metadata(artifact), "graph": graph}


@app.get("/api/models/{model_id}/scene")
def model_scene(model_id: str):
    artifact = _model_artifact(model_id)
    try:
        scene = json.loads(Path(artifact["scene_path"]).read_text(encoding="utf-8"))
        return enrich_scene_construction_ids(artifact["osm_path"], scene)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/models/editor/options")
def model_editor_options():
    _model_artifact_capability()
    return editor_options()


def _model_artifact_capability() -> None:
    capability = capability_status()["capabilities"].get("model_editor")
    if not capability or not capability["runtime_ready"]:
        raise HTTPException(status_code=503, detail="Model editor capability is not ready")


def _editor_call(callback, *args):
    _model_artifact_capability()
    try:
        return callback(*args)
    except storage.StorageAdmissionError:
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/models/{model_id}/session")
def model_edit_session_create(model_id: str):
    return _editor_call(create_session, model_id)


@app.post("/api/models/session/{session_id}")
def model_edit_session_detail(session_id: str, request: ModelEditTokenRequest):
    return _editor_call(session_detail, session_id, request.token)


@app.post("/api/models/session/{session_id}/edits")
def model_edit_session_edits(session_id: str, request: ModelEditRequest):
    patches = [item.model_dump(mode="json", exclude_none=True) for item in request.patches]
    return _editor_call(apply_session_edits, session_id, request.token, patches)


@app.post("/api/models/session/{session_id}/measures")
async def model_edit_measure_import(
    session_id: str,
    token: str = Form(..., min_length=32),
    trusted: bool = Form(False),
    file: UploadFile = File(...),
):
    if not trusted:
        raise HTTPException(status_code=422, detail="Explicit trust is required because a Measure executes local code")
    filename = Path(file.filename or "measure.zip").name
    if Path(filename).suffix.casefold() != ".zip":
        raise HTTPException(status_code=422, detail="Upload an OpenStudio Measure as a ZIP")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
        received = 0
        while chunk := await file.read(1024 * 1024):
            received += len(chunk)
            if received > 25 * 1024 * 1024:
                temporary.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Measure ZIP exceeds 25 MB")
            handle.write(chunk)
    try:
        return _editor_call(import_session_measure, session_id, token, temporary)
    finally:
        temporary.unlink(missing_ok=True)


@app.post("/api/models/session/{session_id}/preflight")
def model_edit_session_preflight(session_id: str, request: ModelEditTokenRequest):
    return _editor_call(preflight_session, session_id, request.token)


@app.post("/api/models/session/{session_id}/commit")
def model_edit_session_commit(session_id: str, request: ModelEditCommitRequest):
    return _editor_call(commit_session, session_id, request.token, request.scenario_name)


@app.post("/api/models/session/{session_id}/discard")
def model_edit_session_discard(session_id: str, request: ModelEditTokenRequest):
    return _editor_call(discard_session, session_id, request.token)


@app.get("/api/runs/{run_id}/export")
def run_export(run_id: str):
    try:
        path = export_run(run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name, media_type="application/zip")


@app.post("/api/runs/{run_id}/verify")
def run_verify(run_id: str):
    try:
        return verify_run_artifacts(run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc


@app.get("/api/simulations/eligible-models")
def simulation_eligible_models():
    return eligible_models()


@app.get("/api/simulations")
def simulations():
    return list_simulations()


@app.get("/api/simulations/jobs/active")
def active_simulation_job(parent_run_id: str | None = None):
    return {"job": db.find_current_simulation_job(parent_run_id)}


@app.get("/api/simulations/jobs/{job_id}")
def get_simulation_job(job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise not_found(job_id)
    if job["kind"] != "simulation":
        raise HTTPException(status_code=422, detail="Job is not a simulation")
    return job


@app.post("/api/simulations", status_code=202)
def create_simulation(request: SimulationRequest):
    try:
        job = create_simulation_job(request.parent_run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    manager.notify()
    return job


@app.post("/api/simulations/paired", status_code=202)
def create_paired_simulation(request: SimulationRequest):
    try:
        pair = create_authored_simulation_pair(request.parent_run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    manager.notify()
    return pair


@app.get("/api/simulations/compare")
def simulation_compare(left: str, right: str):
    try:
        return compare_simulations(left, right)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/simulations/{run_id}")
def get_simulation(run_id: str):
    try:
        return simulation_detail(run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scenarios/options")
def get_scenario_options():
    return scenario_options()


@app.get("/api/scenarios/jobs/active")
def active_scenario_job():
    return {"job": db.find_current_scenario_job()}


@app.get("/api/scenarios/jobs/{job_id}")
def get_scenario_job(job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise not_found(job_id)
    if job["kind"] != "scenario":
        raise HTTPException(status_code=422, detail="Job is not a Part G scenario")
    return job


@app.post("/api/scenarios", status_code=202)
def create_scenario(request: ScenarioRequest):
    try:
        job = create_scenario_job(request.model_dump())
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    manager.notify()
    return job


@app.get("/api/scenarios/{scenario_id}")
def get_scenario(scenario_id: str):
    try:
        return scenario_detail(scenario_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc


@app.get("/api/neighborhood/preflight")
def get_neighborhood_preflight(
    include_map: bool = Query(True), district: str | None = Query(None),
):
    try:
        if district:
            return neighborhood_preflight(include_map=include_map, district=district)
        return neighborhood_preflight(include_map=include_map)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IOError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/neighborhood/options")
def get_neighborhood_options():
    try:
        return neighborhood_options()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/neighborhood/maps/{fingerprint}.geojson")
def get_neighborhood_stock_map(fingerprint: str, request: Request):
    try:
        path = cached_map_path(fingerprint)
    except FileNotFoundError as exc:
        raise not_found(fingerprint) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IOError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    etag = f'"{fingerprint}"'
    headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "ETag": etag,
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="application/geo+json", headers=headers)


@app.get("/api/neighborhood/runs")
def neighborhood_runs():
    return list_neighborhood_runs()


@app.get("/api/neighborhood/run-summaries")
def neighborhood_run_summaries():
    return list_neighborhood_run_summaries()


@app.get("/api/neighborhood/compare")
def neighborhood_compare(left: str, right: str):
    try:
        return compare_stock_runs("neighborhood", left, right)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/neighborhood/jobs/active")
def active_neighborhood_job():
    return {"job": db.find_current_job("neighborhood")}


@app.get("/api/neighborhood/jobs/{job_id}")
def get_neighborhood_job(job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise not_found(job_id)
    if job["kind"] != "neighborhood":
        raise HTTPException(status_code=422, detail="Job is not a neighborhood run")
    return job


@app.post("/api/neighborhood/runs", status_code=202)
def create_neighborhood_run(request: NeighborhoodRunRequest | None = None):
    try:
        job = create_neighborhood_job(request.model_dump() if request else None)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    manager.notify()
    return job


@app.get("/api/neighborhood/runs/{run_id}/map")
def get_neighborhood_map(run_id: str, request: Request):
    try:
        path = neighborhood_map(run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except FileNotFoundError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    fingerprint = integrity.sha256_file(path)
    etag = f'"{fingerprint}"'
    headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "ETag": etag,
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(
        path, media_type="application/geo+json", headers=headers,
    )


@app.get("/api/neighborhood/runs/{run_id}")
def get_neighborhood_run(run_id: str):
    try:
        return neighborhood_detail(run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/city/preflight")
def get_city_preflight():
    try:
        return city_preflight()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/city/options")
def get_city_options():
    try:
        return city_options()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/city/runs")
def city_runs():
    return list_city_runs()


@app.get("/api/city/run-summaries")
def city_run_summaries():
    return list_city_run_summaries()


@app.get("/api/city/compare")
def city_compare(left: str, right: str):
    try:
        return compare_stock_runs("city", left, right)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/city/jobs/active")
def active_city_job():
    return {"job": db.find_current_job("city")}


@app.get("/api/city/jobs/{job_id}")
def get_city_job(job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise not_found(job_id)
    if job["kind"] != "city":
        raise HTTPException(status_code=422, detail="Job is not a city run")
    return job


@app.post("/api/city/runs", status_code=202)
def create_city_run(request: CityRunRequest | None = None):
    try:
        job = create_city_job(request.model_dump() if request else None)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    manager.notify()
    return job


def _validate_tile_coordinate(z: int, x: int, y: int) -> None:
    if not 0 <= z <= 22 or not 0 <= x < 2 ** z or not 0 <= y < 2 ** z:
        raise HTTPException(status_code=422, detail="Invalid tile coordinate")


@app.get("/api/city/map/tiles/{z}/{x}/{y}.mvt")
def city_preflight_tile(z: int, x: int, y: int):
    _validate_tile_coordinate(z, x, y)
    path = city_tile_source()
    payload = city_vector_tile(z, x, y, str(path), path.stat().st_mtime_ns)
    return Response(payload, media_type="application/vnd.mapbox-vector-tile",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/city/runs/{run_id}/tiles/{z}/{x}/{y}.mvt")
def city_run_tile(run_id: str, z: int, x: int, y: int):
    _validate_tile_coordinate(z, x, y)
    try:
        path = city_tile_source(run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = city_vector_tile(z, x, y, str(path), path.stat().st_mtime_ns)
    return Response(payload, media_type="application/vnd.mapbox-vector-tile",
                    headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/city/runs/{run_id}/map-metrics")
def get_city_map_metrics(run_id: str, response: Response):
    try:
        metrics_path = city_map_metrics(run_id)
        source_path = city_tile_source(run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except FileNotFoundError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    focus = city_focus_view(str(source_path), source_path.stat().st_mtime_ns)
    metrics["focus_bounds"] = focus["bounds"]
    metrics["focus_buildings"] = focus["buildings"]
    metrics["focus_coverage"] = focus["coverage"]
    response.headers["Cache-Control"] = "no-store"
    return metrics


@app.get("/api/city/runs/{run_id}")
def get_city_run(run_id: str):
    try:
        return city_detail(run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/lhs/preflight")
def get_lhs_preflight():
    try:
        return lhs_preflight()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/lhs/runs")
def lhs_runs():
    return list_lhs_runs()


@app.get("/api/lhs/jobs/active")
def active_lhs_job():
    return {"job": db.find_current_job("lhs")}


@app.get("/api/lhs/jobs/{job_id}")
def get_lhs_job(job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise not_found(job_id)
    if job["kind"] != "lhs":
        raise HTTPException(status_code=422, detail="Job is not an LHS run")
    return job


@app.post("/api/lhs/runs", status_code=202)
def create_lhs_run():
    try:
        job = create_lhs_job()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    manager.notify()
    return job


@app.get("/api/lhs/compare")
def lhs_compare(left: str, right: str):
    try:
        return compare_lhs(left, right)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/lhs/runs/{run_id}/figures/{name}")
def get_lhs_figure(run_id: str, name: str):
    try:
        path = lhs_figure(run_id, name)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except FileNotFoundError as exc:
        raise not_found(str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(path, filename=name, media_type="image/png")


@app.get("/api/lhs/runs/{run_id}/artifacts/{name}")
def get_lhs_artifact(run_id: str, name: str):
    try:
        path, media_type = lhs_artifact(run_id, name)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except FileNotFoundError as exc:
        raise not_found(str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(path, filename=name, media_type=media_type)


@app.get("/api/lhs/runs/{run_id}")
def get_lhs_run(run_id: str):
    try:
        return lhs_detail(run_id)
    except KeyError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/batches", status_code=202)
def create_batch(request: BatchRequest):
    base = workbench_base_config()
    items = []
    for ref in request.building_refs:
        profile_id = request.profile_assignments.get(ref)
        config = config_for_profile(base, profile_id) if profile_id else request.config
        items.append((ref, {
            "building_ref": ref, "config": config.model_dump(mode="json"),
            "geometry_actions": request.geometry_actions_by_ref.get(ref, request.geometry_actions),
        }))
    batch_id, jobs = db.create_batch_jobs(
        request.name, storage.attach_batch_reservations(items),
    )
    manager.notify()
    return {"batch_id": batch_id, "job_ids": jobs, "status": "queued"}


@app.post("/api/batches/preflight")
def batch_preflight(request: BatchPreflightRequest):
    profiles = {item["id"] for item in db.list_profiles()}
    rows = []
    for ref in request.building_refs:
        try:
            row = read_building(ref)
            cluster = str(row.get("cluster", "")).strip()
            suggested = f"tabula_{cluster}" if f"tabula_{cluster}" in profiles else "pilot_ive_1974"
            geometry = validate_geometry(ref, config_for_profile(workbench_base_config(), suggested))
            rows.append({"refparcela": ref, "cluster": cluster or None,
                         "suggested_profile": suggested, "valid": geometry["valid"],
                         "geometry_actions": geometry["actions"], "error": None})
        except Exception as exc:
            rows.append({"refparcela": ref, "cluster": None, "suggested_profile": None,
                         "geometry_actions": [], "valid": False, "error": str(exc)})
    return {"items": rows, "ready": all(item["valid"] for item in rows)}


@app.get("/api/batches")
def batches():
    return db.list_batches()


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: str):
    batch = db.get_batch(batch_id)
    if batch is None:
        raise not_found(batch_id)
    return batch


@app.get("/api/code/symbols/{name}")
def code_trace(name: str):
    try:
        return code_symbol(name)
    except KeyError as exc:
        raise not_found(name) from exc


# ---------------------------------------------------------------------------
# Stock: the per-building pipeline.  Three surfaces - Files, Run, Outputs.
#
# These endpoints hold no physics and no thresholds; every number is asked of
# `stock_adapter`, which in turn asks `stock_runner`.  A run is a subprocess, so
# stopping it is a signal and its ledger is the durable record.
# ---------------------------------------------------------------------------
def _stock_bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


@app.get("/api/stock/profile")
def stock_profile():
    """Which verified model this installation runs - shown in the header."""
    inputs = stock_adapter.default_inputs()
    return {"profile": stock_adapter.profile(),
            "inputs": {
                "gis": str(inputs.gis) if inputs.gis else None,
                "tipo15": str(inputs.tipo15) if inputs.tipo15 else None,
                "stock": str(inputs.stock) if inputs.stock else None,
                "microclimate": str(inputs.microclimate) if inputs.microclimate else None,
                "climate": str(inputs.climate) if inputs.climate else None,
                "template": str(inputs.template) if inputs.template else None,
            },
            "prepared_stock": inputs.prepared,
            "missing_inputs": inputs.missing(),
            "entrypoints": stock_adapter.entrypoints_present()}


@app.post("/api/datasets/{dataset_id}/ingest")
def ingest_dataset(dataset_id: str, payload: dict):
    """Derive an engine-ready stock file from a registered source database."""
    population = payload.get("population")
    crs = str(payload.get("crs") or "").strip()
    if not isinstance(population, int) or population <= 0:
        raise HTTPException(
            status_code=422,
            detail="a resident population is required: it is allocated across the "
                   "stock, so the city total decides every building's occupancy")
    if not crs:
        raise HTTPException(
            status_code=422,
            detail="a metric CRS is required: footprint area, party-wall overlap "
                   "and the neighbour radius are planar measurements in metres")
    try:
        return service_ingest_eu_database(
            dataset_id, str(payload.get("name") or f"stock from {dataset_id}"),
            population=population, crs=crs,
            include_mixed=bool(payload.get("include_mixed")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/stock/districts")
def stock_districts():
    try:
        return {"districts": stock_adapter.district_options()}
    except FileNotFoundError as exc:
        raise _stock_bad_request(f"GIS dataset unavailable: {exc}") from exc


@app.post("/api/stock/preflight")
def stock_preflight(payload: dict):
    scope = str(payload.get("scope") or "")
    if scope not in ("all", "clusters", "district", "references"):
        raise _stock_bad_request(f"unknown scope: {scope!r}")
    references = payload.get("references") or None
    if scope == "references" and not references:
        raise _stock_bad_request("scope 'references' needs at least one refparcela")
    if scope == "district" and not payload.get("district"):
        raise _stock_bad_request("scope 'district' needs a district name")
    try:
        return stock_adapter.preflight(
            scope,
            district=payload.get("district"),
            references=references,
            keep=str(payload.get("keep") or "full"),
            workers=int(payload.get("workers") or 6))
    except (FileNotFoundError, ValueError) as exc:
        raise _stock_bad_request(str(exc)) from exc


@app.get("/api/stock/runs")
def stock_runs():
    runs = stock_adapter.list_runs()
    for item in runs:
        item["running"] = stock_adapter.active_process(item["run"]) is not None
    return {"runs": runs}


@app.post("/api/stock/runs")
def stock_start(payload: dict):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise _stock_bad_request("a run needs a name")
    scope = str(payload.get("scope") or "")
    if scope not in ("all", "clusters", "district", "references"):
        raise _stock_bad_request(f"unknown scope: {scope!r}")
    try:
        if stock_adapter.active_process(name) is not None:
            raise _stock_bad_request(f"run {name!r} is already going")
        # The engine's own admission gate, not a second opinion invented here.
        estimate = stock_adapter.preflight(
            scope, district=payload.get("district"),
            references=payload.get("references") or None,
            keep=str(payload.get("keep") or "full"),
            workers=int(payload.get("workers") or 6))
        if not estimate.get("ok"):
            raise _stock_bad_request(
                f"inputs missing: {', '.join(estimate.get('missing_inputs', []))}")
        capacity = storage.admission("stock_run",
                                     requested_bytes=int(estimate["estimated_bytes"]))
        if not capacity["allowed"]:
            raise _stock_bad_request(
                f"not enough protected disk for this run: {capacity['reason']}")
        run_mode = str(payload.get("run_mode") or "annual")
        if run_mode not in ("annual", "microclimate_event"):
            raise _stock_bad_request(f"unknown run mode: {run_mode!r}")
        started = stock_adapter.start_run(
            name, scope,
            district=payload.get("district"),
            references=payload.get("references") or None,
            workers=int(payload.get("workers") or 6),
            keep=str(payload.get("keep") or "full"),
            resume=bool(payload.get("resume")),
            run_mode=run_mode)
    except ValueError as exc:
        raise _stock_bad_request(str(exc)) from exc
    return {"started": started, "estimate": estimate}


@app.get("/api/stock/runs/{name}")
def stock_run_detail(name: str):
    try:
        progress = stock_adapter.progress(name)
    except ValueError as exc:
        raise _stock_bad_request(str(exc)) from exc
    if not progress.get("started"):
        raise not_found(name)
    detail = {"progress": progress,
              "running": stock_adapter.active_process(name) is not None}
    try:
        detail["summary"] = stock_adapter.summary(name)
    except (FileNotFoundError, KeyError):
        detail["summary"] = None          # still running, nothing to total yet
    return detail


@app.post("/api/stock/runs/{name}/stop")
def stock_stop(name: str):
    record = stock_adapter.active_process(name)
    if record is None:
        raise not_found(name)
    stopped = stock_adapter.stop_run(int(record["pid"]))
    # Worth saying plainly: nothing is lost, and the same name resumes.
    return {"stopped": stopped, "resumable": True, "run": name}


@app.get("/api/stock/runs/{name}/ledger.csv")
def stock_ledger_csv(name: str):
    try:
        rows = stock_adapter.ledger_rows(name)
    except (FileNotFoundError, ValueError) as exc:
        raise not_found(name) from exc
    if not rows:
        raise not_found(name)
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    def stream():
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()
        for row in rows:
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(row)
            yield buffer.getvalue()

    return StreamingResponse(
        stream(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}_buildings.csv"'})


@app.get("/api/stock/runs/{name}/results.gpkg")
def stock_results_layer(name: str):
    """The run's results as a GeoPackage, ready to open in QGIS."""
    try:
        path = stock_adapter.results_layer(name)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path, media_type="application/geopackage+sqlite3",
        filename=f"{name}_buildings.gpkg")


@app.get("/api/stock/runs/{name}/results.png")
def stock_results_heatmap(name: str):
    """The run's heat map, for a reader who will not open a GIS."""
    try:
        path = stock_adapter.results_heatmap(name)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="image/png",
                        filename=f"{name}_heatmap.png")


@app.get("/api/stock/runs/{name}/ledger")
def stock_ledger_page(
    name: str,
    q: str = Query(default="", max_length=160),
    status: str = Query(default="", max_length=32),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    try:
        return stock_adapter.ledger_page(
            name, query=q, status=status, offset=offset, limit=limit)
    except (FileNotFoundError, ValueError) as exc:
        raise not_found(name) from exc


@app.get("/api/stock/runs/{name}/log")
def stock_run_log(name: str):
    try:
        content = stock_adapter.log_tail(name)
    except ValueError as exc:
        raise _stock_bad_request(str(exc)) from exc
    return PlainTextResponse(
        content,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/stock/runs/{name}/export-plan")
def stock_export_plan(name: str, references: list[str] | None = Query(default=None)):
    try:
        return stock_adapter.package_plan(name, references)
    except FileNotFoundError as exc:
        raise not_found(name) from exc
    except ValueError as exc:
        raise _stock_bad_request(str(exc)) from exc


@app.get("/api/stock/runs/{name}/export.zip")
def stock_export(name: str, references: list[str] | None = Query(default=None)):
    if stock_adapter.active_process(name) is not None:
        raise _stock_bad_request("stop the run before creating a signed package")
    try:
        plan = stock_adapter.package_plan(name, references)
        evidence = storage.admission("export", requested_bytes=int(plan["uncompressed_bytes"]))
        if not evidence["allowed"]:
            raise storage.StorageAdmissionError(
                f"Insufficient protected disk capacity for export: {evidence['reason']}", evidence,
            )
        path = stock_adapter.export_package(name, references)
    except FileNotFoundError as exc:
        raise not_found(name) from exc
    except ValueError as exc:
        raise _stock_bad_request(str(exc)) from exc
    return FileResponse(
        path, media_type="application/zip", filename=path.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/stock/runs/{name}/buildings/{refparcela}/{filename}")
def stock_building_artifact(name: str, refparcela: str, filename: str):
    try:
        path = stock_adapter.artifact_path(name, refparcela, filename)
    except ValueError as exc:
        raise _stock_bad_request(str(exc)) from exc
    except FileNotFoundError as exc:
        raise not_found(f"{refparcela}/{filename}") from exc
    media_type = stock_adapter.SERVABLE_ARTIFACTS[filename]
    headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
    }
    if media_type.startswith("text/html"):
        # EnergyPlus tables are third-party HTML: render them, run nothing.
        headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data:; base-uri 'none'; form-action 'none'"
        )
    return FileResponse(path, media_type=media_type, headers=headers)


DIST = PROJECT / "frontend/dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
