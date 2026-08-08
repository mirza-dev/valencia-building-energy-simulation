"""Typed configuration shared by the CLI builder and the local workbench.

The original project grew from a teaching script, so several scientifically
meaningful settings lived as module constants.  This module gives those values
one validated representation without changing the legacy ``build_model`` API.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


OUTPUT_VARIABLES = {
    "heating": "Zone Ideal Loads Supply Air Total Heating Energy",
    "cooling": "Zone Ideal Loads Supply Air Total Cooling Energy",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class DataSourceConfig(StrictModel):
    building_path: Path
    neighbor_path: Path
    template_path: Path
    epw_path: Path
    output_root: Path


class GeometryConfig(StrictModel):
    floor_height_m: float = Field(3.0, ge=1.5, le=10.0)
    ground_unconditioned: bool = True
    neighbor_assume_ground: bool = True
    simplify_tolerance_m: float = Field(0.3, ge=0.0, le=5.0)
    max_area_delta_fraction: float = Field(0.01, ge=0.0, le=0.25)
    footprint_min_m2: float = Field(50.0, gt=0.0)
    # Not an engine limit - a modelling-validity guard.  The chain gives each
    # storey one well-mixed thermal zone, which stops being defensible on a very
    # deep plan where the core is driven by internal gains rather than by the
    # envelope.  The ceiling sat at 5 000 m2 until 2026-08-03, which excluded 123
    # Valencia buildings carrying 11.55 % of the city's floor area - too much to
    # drop from a city total over a threshold nothing had measured.  20 000 m2
    # recovers 11.00 of those 11.55 points and leaves 4 outliers (the largest is
    # 32 172 m2 over 2 storeys, which is not a dwelling at all).  Buildings above
    # LARGE_FOOTPRINT_SINGLE_ZONE_M2 are flagged, never silently blended in.
    footprint_max_m2: float = Field(20000.0, gt=0.0)
    party_wall_tolerance_m: float = Field(0.3, ge=0.0, le=5.0)
    min_shared_edge_m: float = Field(1.0, ge=0.0)
    party_overlap_ratio: float = Field(0.5, ge=0.0, le=1.0)
    context_radius_m: float = Field(50.0, ge=0.0, le=500.0)

    @model_validator(mode="after")
    def validate_footprint_range(self):
        if self.footprint_max_m2 <= self.footprint_min_m2:
            raise ValueError("footprint_max_m2 must be larger than footprint_min_m2")
        return self


class EnvelopeConfig(StrictModel):
    wall_u: float | None = Field(None, gt=0.05, le=10.0)
    roof_u: float | None = Field(None, gt=0.05, le=10.0)
    window_u: float = Field(5.7, gt=0.05, le=10.0)
    window_g: float = Field(0.82, ge=0.0, le=1.0)
    thermal_bridge_du: float = Field(0.10, ge=0.0, le=1.0)
    massless: bool = False


class OpeningConfig(StrictModel):
    wwr_north: float = Field(0.12, ge=0.0, le=0.95)
    wwr_east: float = Field(0.18, ge=0.0, le=0.95)
    wwr_south: float = Field(0.25, ge=0.0, le=0.95)
    wwr_west: float = Field(0.18, ge=0.0, le=0.95)
    window_width_m: float = Field(1.2, gt=0.0, le=10.0)
    window_height_m: float = Field(1.2, gt=0.0, le=10.0)
    window_sill_m: float = Field(0.9, ge=0.0, le=10.0)
    door_width_m: float = Field(1.2, gt=0.0, le=10.0)
    door_height_m: float = Field(2.1, gt=0.0, le=10.0)
    door_sill_m: float = Field(0.01, ge=0.0, le=2.0)
    balcony_doors_per_facade_floor: int = Field(2, ge=0, le=10)
    balcony_depth_m: float = Field(1.0, ge=0.0, le=10.0)

    def cardinal_wwr(self) -> dict[int, float]:
        return {
            0: self.wwr_north,
            90: self.wwr_east,
            180: self.wwr_south,
            270: self.wwr_west,
        }


class ShadingConfig(StrictModel):
    context_enabled: bool = True
    blind_name: str = "Lamas Horizontales 25mm cada 20mm"
    setpoint_w_m2: float = Field(250.0, ge=0.0, le=5000.0)
    summer_start_month: int = Field(6, ge=1, le=12)
    summer_end_month: int = Field(9, ge=1, le=12)
    max_shadow_figures: int = Field(200000, ge=15000, le=1000000)


class OperationConfig(StrictModel):
    infiltration_ach: float | None = Field(None, ge=0.0, le=20.0)
    output_variables: dict[str, str] = Field(default_factory=lambda: dict(OUTPUT_VARIABLES))


class QAConfig(StrictModel):
    facade_wwr_warning_pct: float = Field(15.0, ge=0.0, le=100.0)


class OverrideRecord(StrictModel):
    field: str
    reason: str = Field(min_length=3)
    source_type: Literal["human_judgement", "dataset", "publication", "supervisor", "other"]
    source_ref: str | None = None


class ProvenanceConfig(StrictModel):
    baseline_profile: str = "pilot_ive_1974"
    scenario_name: str = "Pilot baseline"
    locale: Literal["tr", "en"] = "tr"
    overrides: list[OverrideRecord] = Field(default_factory=list)


class BuildConfig(StrictModel):
    data: DataSourceConfig
    geometry: GeometryConfig = Field(default_factory=GeometryConfig)
    envelope: EnvelopeConfig = Field(default_factory=EnvelopeConfig)
    openings: OpeningConfig = Field(default_factory=OpeningConfig)
    shading: ShadingConfig = Field(default_factory=ShadingConfig)
    operation: OperationConfig = Field(default_factory=OperationConfig)
    qa: QAConfig = Field(default_factory=QAConfig)
    provenance: ProvenanceConfig = Field(default_factory=ProvenanceConfig)

    @classmethod
    def for_project(cls, project: Path) -> "BuildConfig":
        return cls(
            data=DataSourceConfig(
                building_path=project / "data/gis/benicalap_bina_choosen.gpkg",
                neighbor_path=project / "data/gis/DatosRai_ciudadValencia.shp",
                template_path=project / "data/templates/PlantillaOS_v2.osm",
                epw_path=project / "data/weather/ESP_Valencia.082840_IWEC.epw",
                output_root=project / "out",
            )
        )

    def with_legacy_params(self, params: dict[str, Any] | None) -> "BuildConfig":
        if not params:
            return self.model_copy(deep=True)
        valid = set(self.to_legacy_params())
        unknown = set(params) - valid
        if unknown:
            raise ValueError(f"Unknown parameter keys: {sorted(unknown)}; valid: {sorted(valid)}")
        out = self.model_copy(deep=True)
        mapping = {
            "wall_u": ("envelope", "wall_u"),
            "roof_u": ("envelope", "roof_u"),
            "window_u": ("envelope", "window_u"),
            "window_g": ("envelope", "window_g"),
            "infiltration_ach": ("operation", "infiltration_ach"),
            "shade_setpoint": ("shading", "setpoint_w_m2"),
            "context_shading": ("shading", "context_enabled"),
            "thermal_bridge_du": ("envelope", "thermal_bridge_du"),
            "massless": ("envelope", "massless"),
            "ground_unconditioned": ("geometry", "ground_unconditioned"),
        }
        for key, value in params.items():
            section, field = mapping[key]
            setattr(getattr(out, section), field, value)
        return BuildConfig.model_validate(out.model_dump())

    def to_legacy_params(self) -> dict[str, Any]:
        return {
            "wall_u": self.envelope.wall_u,
            "roof_u": self.envelope.roof_u,
            "window_u": self.envelope.window_u,
            "window_g": self.envelope.window_g,
            "infiltration_ach": self.operation.infiltration_ach,
            "shade_setpoint": self.shading.setpoint_w_m2,
            "context_shading": self.shading.context_enabled,
            "thermal_bridge_du": self.envelope.thermal_bridge_du,
            "massless": self.envelope.massless,
            "ground_unconditioned": self.geometry.ground_unconditioned,
        }


TABULA_ES: dict[tuple[str, str], dict[str, float]] = {
    ("VivUni", "P01"): dict(wall_u=2.56, roof_u=1.60, window_u=5.00),
    ("VivUni", "P02"): dict(wall_u=2.56, roof_u=4.17, window_u=4.59),
    ("VivUni", "P03"): dict(wall_u=2.56, roof_u=4.17, window_u=4.59),
    ("VivUni", "P04"): dict(wall_u=1.33, roof_u=1.67, window_u=5.70),
    ("VivUni", "P05"): dict(wall_u=0.72, roof_u=1.92, window_u=3.04),
    ("VivUni", "P06"): dict(wall_u=0.47, roof_u=0.48, window_u=2.92),
    ("EdiPluri", "P01"): dict(wall_u=2.56, roof_u=1.60, window_u=5.35),
    ("EdiPluri", "P02"): dict(wall_u=2.56, roof_u=3.08, window_u=5.35),
    ("EdiPluri", "P03"): dict(wall_u=2.94, roof_u=1.67, window_u=5.70),
    ("EdiPluri", "P04"): dict(wall_u=1.64, roof_u=1.61, window_u=5.70),
    ("EdiPluri", "P05"): dict(wall_u=0.62, roof_u=0.56, window_u=3.37),
    ("EdiPluri", "P06"): dict(wall_u=0.52, roof_u=0.45, window_u=3.54),
    ("BlocPluri", "P01"): dict(wall_u=2.56, roof_u=4.17, window_u=5.35),
    ("BlocPluri", "P02"): dict(wall_u=2.56, roof_u=3.08, window_u=5.35),
    ("BlocPluri", "P03"): dict(wall_u=2.27, roof_u=1.37, window_u=4.72),
    ("BlocPluri", "P04"): dict(wall_u=1.33, roof_u=1.92, window_u=5.70),
    ("BlocPluri", "P05"): dict(wall_u=0.58, roof_u=0.60, window_u=3.37),
    ("BlocPluri", "P06"): dict(wall_u=0.48, roof_u=0.47, window_u=3.29),
}


# The envelope Rai modelled, read out of the 41 OpenStudio files he sent on
# 2026-08-04 and remapped on 2026-08-06.
#
# His file names do not mean what the shapefile's `cluster` column means.  He
# stated the mapping himself - "EdiPluri there means BlocPluri", "VivUniAis
# means VivUni", "VivUniAdo means EdiPluri" - while warning that he could not
# open the files to check.  It was not taken on trust: for every model the
# construction actually *assigned* to the exterior surfaces was extracted and
# compared against the IVE table above.  His `EdiPluri` walls land on our
# BlocPluri column (P03 +0.6%, P04 +2.9%, P06 -2.9%) and miss our EdiPluri
# column by 10-22%, on four of four uncorrupted periods.  That is the mapping.
#
# Reading rule, because his library is not internally consistent: a value comes
# from the surface it is assigned to in the file whose name matches the period,
# latest revision, and retrofit assemblies are rejected.  The same construction
# *name* carries different assemblies in different files - `EdiPluriP03_Roof` is
# 0.453 in the P01/P02 files and 2.652 in the P03 file - and 0.453 sits next to
# `EdiPluriP04_Roof_Mejorada` = 0.40, so it is a retrofit that leaked into base
# models.  This is what made the P01/P02 roofs look physically impossible on
# 2026-08-04; the period-correct entries were in the library all along.
# Windows: where a file assigns two, the dominant one wins (8-12 openings
# against a single stray `U_4.5`).
#
# Four entries stay on the IVE values because his library has no counterpart:
# marked `# IVE` below.
RAI_ENVELOPE: dict[tuple[str, str], dict[str, float]] = {
    # <- VivUniAis*PTHP*.osm
    ("VivUni", "P01"): dict(wall_u=3.203, roof_u=2.184, window_u=4.9),
    ("VivUni", "P02"): dict(wall_u=1.728, roof_u=1.546, window_u=4.5),
    ("VivUni", "P03"): dict(wall_u=1.728, roof_u=1.310, window_u=4.5),
    ("VivUni", "P04"): dict(wall_u=1.013, roof_u=1.762, window_u=4.5),
    ("VivUni", "P05"): dict(wall_u=0.416, roof_u=0.610, window_u=3.10),
    # VivUniAisP06 carries a facade only; roof and window stay on IVE.
    ("VivUni", "P06"): dict(wall_u=0.320, roof_u=0.48, window_u=2.92),  # IVE roof+window
    # <- VivUniAdo*PTHP*.osm
    ("EdiPluri", "P01"): dict(wall_u=1.728, roof_u=2.184, window_u=4.9),
    ("EdiPluri", "P02"): dict(wall_u=1.728, roof_u=1.521, window_u=4.5),
    # The P03 file assigns VivUniAdoP04PitchedRoof - a label slip, so its roof
    # stays on IVE rather than borrowing the next period's.
    ("EdiPluri", "P03"): dict(wall_u=1.728, roof_u=1.67, window_u=4.5),  # IVE roof
    ("EdiPluri", "P04"): dict(wall_u=1.013, roof_u=1.521, window_u=5.7),
    # No VivUniAdoP05 facade or roof exists; only its window is his.
    ("EdiPluri", "P05"): dict(wall_u=0.62, roof_u=0.56, window_u=3.10),  # IVE wall+roof
    ("EdiPluri", "P06"): dict(wall_u=0.416, roof_u=0.449, window_u=2.9),
    # <- EdiPluri*.osm / EdiPluriHWater*.osm
    ("BlocPluri", "P01"): dict(wall_u=1.584, roof_u=1.786, window_u=5.3),
    ("BlocPluri", "P02"): dict(wall_u=1.584, roof_u=2.919, window_u=5.3),
    ("BlocPluri", "P03"): dict(wall_u=2.284, roof_u=2.652, window_u=5.7),
    ("BlocPluri", "P04"): dict(wall_u=1.369, roof_u=2.479, window_u=5.7),
    ("BlocPluri", "P05"): dict(wall_u=0.488, roof_u=0.633, window_u=3.3),
    ("BlocPluri", "P06"): dict(wall_u=0.466, roof_u=0.364, window_u=3.3),
}

# Which of the two tables the per-building chain resolves clusters against.
# "rai" is the current decision; "ive" restores the previous behaviour exactly,
# so a run can be reproduced on either envelope rather than argued about.  The
# value is part of the verified profile, so every ledger row records which
# envelope produced it.
ENVELOPE_SOURCE = "rai"

ENVELOPE_TABLES = {"ive": TABULA_ES, "rai": RAI_ENVELOPE}


def envelope_table(source: str | None = None) -> dict[tuple[str, str], dict[str, float]]:
    """The cluster envelope table currently in force."""
    key = source or ENVELOPE_SOURCE
    try:
        return ENVELOPE_TABLES[key]
    except KeyError:
        raise KeyError(
            f"unknown envelope source {key!r}; expected one of "
            f"{sorted(ENVELOPE_TABLES)}") from None


def config_for_profile(base: BuildConfig, profile_id: str) -> BuildConfig:
    """Return an immutable-baseline copy for the pilot or a TABULA cluster."""
    out = base.model_copy(deep=True)
    out.provenance.baseline_profile = profile_id
    out.provenance.scenario_name = profile_id
    if profile_id == "pilot_ive_1974":
        return out
    cluster = profile_id.removeprefix("tabula_")
    table = envelope_table()
    for family in ("VivUni", "EdiPluri", "BlocPluri"):
        if cluster.startswith(family):
            period = cluster[len(family):]
            period = "P06" if period == "P07" else period
            values = table.get((family, period))
            if values is None:
                break
            out.envelope.wall_u = values["wall_u"]
            out.envelope.roof_u = values["roof_u"]
            out.envelope.window_u = values["window_u"]
            out.envelope.window_g = 0.82 if values["window_u"] >= 4.0 else 0.75
            out.geometry.ground_unconditioned = family != "VivUni"
            return out
    raise KeyError(f"Unknown profile: {profile_id}")


def flatten_config(config: BuildConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def walk(prefix: str, value: Any):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(f"{prefix}.{key}" if prefix else key, child)
        elif isinstance(value, list):
            return
        else:
            out[prefix] = value

    walk("", config.model_dump(mode="json"))
    return out


def validate_override_provenance(config: BuildConfig, base: BuildConfig) -> list[str]:
    """Return changed scientific fields that have no per-field explanation."""
    ignored = {"provenance.scenario_name", "provenance.locale", "provenance.baseline_profile"}
    changed = {
        key for key, value in flatten_config(config).items()
        if key not in ignored and flatten_config(base).get(key) != value
    }
    explained = {item.field for item in config.provenance.overrides}
    return sorted(changed - explained)


def profile_catalog(base: BuildConfig) -> list[dict[str, Any]]:
    profiles = [{
        "id": "pilot_ive_1974",
        "label": "Pilot IVE 1974",
        "source": "IVE/TABULA + Javier wall-layer confirmation",
        "config": base.model_dump(mode="json"),
    }]
    source_label = {
        "ive": "TABULA España (IVE), estado original",
        "rai": "Rai's OpenStudio models (2026-08-04), IVE where he has no counterpart",
    }[ENVELOPE_SOURCE]
    for (family, period), _ in envelope_table().items():
        profile_id = f"tabula_{family}{period}"
        cfg = config_for_profile(base, profile_id)
        profiles.append({
            "id": profile_id,
            "label": f"{family} {period}",
            "source": source_label,
            "config": cfg.model_dump(mode="json"),
        })
    return deepcopy(profiles)
