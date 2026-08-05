"""HTTP request contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from model_config import BuildConfig


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreviewRequest(StrictRequest):
    building_ref: str = Field(min_length=3)
    config: BuildConfig
    geometry_actions: list[dict[str, Any]] = Field(default_factory=list)


class CommitRequest(StrictRequest):
    view_state: dict[str, Any] | None = None
    screenshot_data_url: str | None = None


class GeometryRequest(StrictRequest):
    building_ref: str = Field(min_length=3)
    config: BuildConfig


class FieldMappingRequest(StrictRequest):
    reference_field: str = Field(min_length=1)
    floors_field: str = Field(min_length=1)
    cluster_field: str | None = None


class DatasetFieldNoteRequest(StrictRequest):
    note: str = Field("", max_length=1000)
    semantic_label: str = Field("", max_length=80)


class ProjectSettingsRequest(StrictRequest):
    building_dataset_id: str | None = None
    neighbor_dataset_id: str | None = None
    tipo15_dataset_id: str | None = None
    template_dataset_id: str | None = None
    weather_dataset_id: str | None = None
    ddy_dataset_id: str | None = None


class StockInputPolicyFields(StrictRequest):
    schema_version: int = 1
    gis_dataset_id: str = Field(min_length=1)
    reference_field: str = Field(min_length=1)
    district_field: str = Field(min_length=1)
    footprint_area_mode: Literal["field", "geometry_epsg25830"]
    footprint_area_field: str | None = None
    floors_field: str = Field(min_length=1)
    cluster_field: str = Field(min_length=1)
    cluster_mapping: dict[str, str] = Field(default_factory=dict)
    floor_invalid_policy: Literal[
        "cluster_family_one", "exclude_invalid", "block_run", "fixed_fallback",
    ]
    floor_fixed_fallback: int | None = Field(None, ge=1, le=100)
    ground_floor_mode: Literal[
        "tipo15_family_fallback", "family_default", "force_unconditioned", "force_conditioned",
    ]
    residential_area_mode: Literal["tipo15_proxy", "field_proxy", "proxy_only"]
    residential_area_field: str | None = None


class StockInputPolicyPreflightRequest(StrictRequest):
    input_policy_override: dict[str, Any] | None = None
    district: str | None = Field(None, min_length=2, max_length=100)
    building_ref: str | None = Field(None, min_length=3, max_length=32)


class BatchRequest(StrictRequest):
    name: str = Field(min_length=2)
    building_refs: list[str] = Field(min_length=1, max_length=500)
    config: BuildConfig
    geometry_actions: list[dict[str, Any]] = Field(default_factory=list)
    profile_assignments: dict[str, str] = Field(default_factory=dict)
    geometry_actions_by_ref: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class BatchPreflightRequest(StrictRequest):
    building_refs: list[str] = Field(min_length=1, max_length=500)


class SimulationRequest(StrictRequest):
    parent_run_id: str = Field(min_length=8)


class ModelEditPatch(StrictRequest):
    op: Literal[
        "project_parameter.update",
        "construction.set_layers", "construction.reorder",
        "material.update", "material.create", "material.delete",
        "schedule.update_day", "schedule.add_rule", "schedule.delete_rule",
        "space_type.set_loads", "space_type.set_infiltration", "space_type.set_dsoa",
        "thermostat.set_setpoints", "hvac.set_system",
        "hvac.air_loop.create", "hvac.plant_loop.create", "hvac.loop.delete",
        "hvac.zone.connect", "hvac.zone.disconnect",
        "hvac.component.add", "hvac.component.update", "hvac.component.remove",
        "measure.apply",
        "sim.set_output_variables", "sim.set_run_period",
        "surface.create", "surface.move", "surface.delete", "surface.set_wwr",
        "subsurface.create", "subsurface.delete", "subsurface.add_overhang",
        "story.add", "space.duplicate_story",
    ]
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ModelEditRequest(StrictRequest):
    token: str = Field(min_length=32)
    patches: list[ModelEditPatch] = Field(min_length=1, max_length=100)


class ModelEditTokenRequest(StrictRequest):
    token: str = Field(min_length=32)


class ModelEditCommitRequest(ModelEditTokenRequest):
    scenario_name: str | None = Field(None, min_length=2, max_length=100)


class ScenarioRequest(StrictRequest):
    parent_run_id: str = Field(min_length=8)
    name: str = Field(min_length=2, max_length=80)
    heat_delta_c: float = Field(0.0, ge=-3.0, le=3.0)
    cool_delta_c: float = Field(0.0, ge=-3.0, le=3.0)
    weather_dataset_id: str | None = None
    reason: str = Field(min_length=3, max_length=500)
    source_type: Literal[
        "human_judgement", "dataset", "publication", "supervisor", "other"
    ]
    source_ref: str | None = Field(None, max_length=500)


class CapabilityRevalidationRequest(StrictRequest):
    plan_token: str = Field(min_length=64, max_length=64)


class StockScenarioFields(StrictRequest):
    mode: Literal["baseline", "scenario"] = "baseline"
    name: str | None = Field(None, min_length=2, max_length=80)
    heat_delta_c: float = Field(0.0, ge=-3.0, le=3.0)
    cool_delta_c: float = Field(0.0, ge=-3.0, le=3.0)
    weather_dataset_id: str | None = None
    reason: str | None = Field(None, max_length=500)
    source_type: Literal[
        "human_judgement", "dataset", "publication", "supervisor", "other"
    ] | None = None
    source_ref: str | None = Field(None, max_length=500)
    input_policy_override: dict[str, Any] | None = None


class NeighborhoodRunRequest(StockScenarioFields):
    scope_mode: Literal["boundary", "district", "building"] = "boundary"
    district: str | None = Field(None, min_length=2, max_length=100)
    building_ref: str | None = Field(None, min_length=3, max_length=32)


class CityRunRequest(StockScenarioFields):
    pass


class StorageCleanupRequest(StrictRequest):
    plan_token: str = Field(min_length=64, max_length=64)
    categories: list[Literal[
        "abandoned_scratch", "expired_previews", "export_cache", "map_cache",
        "object_store_gc",
    ]] = Field(min_length=1)
