"""Deep single-building pipeline: real occupancy, DHW, heat pump, Rai ground regime.

The representative-cluster chain (Part A-D) stays frozen.  This module never
edits ``model_builder``, ``run_simulation`` or ``model_config``; it reuses the
geometry/envelope builder as-is and then applies additive post-build mutations,
exactly like ``mb.add_real_hvac`` / ``mb.apply_comfort_offsets`` already do.

Four layers are added on top of a normally built model:

1. Real occupancy from the cadastre ``pob_total`` instead of the CTE
   20 m2/person norm, plus the coupled rescaling of the occupancy-driven
   ventilation object.
2. Rai's ground-floor regime: the ground space becomes a conditioned
   ``Terciario`` zone that stays outside the reported floor-area basis.
3. A domestic hot water plant loop (Rai-aligned 28 l/person/day at 50 C,
   ~80 % of the CTE DB-HE4 60 C energy reference; 97 % boiler).
4. Rai's packaged terminal heat pump instead of the gas-burner PTAC.

Reference for every constant below: Rai's own EdiPluriP04 model, extracted from
``informe_openstudio_EdiPluriP04.html`` and ``eplustbl_EdiPluriP04.html``.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path
from typing import Literal

import openstudio

import model_builder as mb
import model_config as mc
import run_simulation as sim


log = logging.getLogger("deep_building")

ZeroPolicy = Literal["literal_zero", "cluster_median_impute"]

# ---------------------------------------------------------------------------
# Template object names (PlantillaOS_v2 = Rai's own library)
# ---------------------------------------------------------------------------
RESIDENTIAL_SPACE_TYPE = "Espacio Tipo Vivienda CTE"
GROUND_BUFFER_SPACE_TYPE = "Espacio Tipo No habitable 1ACH"
GROUND_TERCIARIO_SPACE_TYPE = "Espacio Tipo Terciario 8h Media CTE"
GROUND_SLAB_CONSTRUCTION = "Solera con aislante"          # Rai: U 0.501 W/m2K
OCCUPANCY_INFILTRATION_MARKER = "ocupacion"               # "Infiltracion Aire ocupacion 0,004/20 ..."
DHW_DEMAND_SCHEDULE = "Demanda ACS Vivienda"
DHW_REFERENCE_DEFINITION = "Demanda ACS 150l/dia"         # used only to read the template's l/day -> m3/s scale
THERMOSTAT_HEATING_SCHEDULE = "T Calefaccion vivienda CTE"
THERMOSTAT_COOLING_SCHEDULE = "T refrigeracion vivienda CTE"

# ---------------------------------------------------------------------------
# Rai's simulation parameters
# ---------------------------------------------------------------------------
CTE_VENTILATION_M3S_PER_PERSON = 0.004    # CTE hygienic air, modelled as occupancy-scheduled infiltration
# CTE DB-HE4 states 28 l/person/day at a 60 C reference. This model delivers the
# same VOLUME at the 50 C Rai runs his loop at, which is about 80 % of the 60 C
# energy demand - the energy-equivalent volume at 50 C from 10 C mains would be
# 28 x (60-10)/(50-10) = 35 l. Keeping 28 l at 50 C is a measured decision
# (2026-07-30): Rai also runs 50 C, and our DHW lands within 1.6 % of his
# (10.32 vs 10.49 kWh/m2), while 35 l would put us 23 % above the only external
# reference we have. Report it as ~80 % of the CTE 60 C energy reference.
DHW_LITRES_PER_PERSON_DAY = 28.0          # volume delivered at DHW_LOOP_SETPOINT_C
DHW_BOILER_EFFICIENCY = 0.97              # Rai: Boiler:HotWater nominal thermal efficiency
DHW_LOOP_SETPOINT_C = 50.0                # Rai: SetpointManager:Scheduled
DHW_LOOP_EXIT_TEMPERATURE_C = 82.0
DHW_LOOP_DELTA_T_K = 11.0
DHW_END_USE_SUBCATEGORY = "DHW"           # keeps the boiler gas OUT of the space-heating row
TERCIARIO_END_USE_SUBCATEGORY = "Terciario"   # commercial lights/equipment, reported apart from the dwellings

PTHP_HEATING_COP = 4.07                   # Rai: residential Coil:Heating:DX:SingleSpeed
PTHP_COOLING_COP = 5.0                    # Rai: Coil:Cooling:DX:SingleSpeed
PTHP_GROUND_HEATING_COP = 3.0             # Rai gives the Terciario ground zone a lower COP
PTHP_FAN_EFFICIENCY = 0.90
PTHP_GROUND_FAN_EFFICIENCY = 0.70
PTHP_FAN_PRESSURE_RISE_PA = 250.0
PTHP_FAN_MOTOR_EFFICIENCY = 0.90

HEATING_SIZING_FACTOR = 1.25              # derived from Rai's calculated vs design load
COOLING_SIZING_FACTOR = 1.15

# EnergyPlus warms the model up until the zone loads and temperatures settle,
# then starts the annual run from that state.  Its default ceiling is 25 days.
#
# Connecting the TABULA cluster envelopes (2026-07-30) gave the pre-1940 clusters
# solid masonry - `Muro macizo ladrillo`, high thermal mass - and 12 of 959
# Benicalap buildings stopped converging inside 25 days, every one of them a
# `CheckWarmupConvergence` Severe.  Heavy mass simply needs longer to settle.
#
# Raising the ceiling is a simulation-control setting, not a physics change, and
# it cannot move a result that already converged: EnergyPlus stops warming up the
# moment the tolerances are met, so only runs that were hitting the ceiling see
# any difference.  Loosening the convergence tolerances instead would have
# accepted a less settled state, which is the opposite of what is wanted.
MAXIMUM_WARMUP_DAYS = 60
GROUND_TEMPERATURE_C = 18.0               # Rai: Site:GroundTemperature:BuildingSurface, flat all year
WATER_MAINS_TEMPERATURE_C = 10.0          # Rai: FixedDefault

# The chain gives every storey ONE well-mixed thermal zone.  On a deep plan that
# stops being defensible: the core is driven by internal gains rather than by
# the envelope, and a single zone averages the two together.  This is the
# footprint above which the result is flagged - not refused, flagged, so a total
# can say how much of itself rests on the weaker assumption.
#
# 5 000 m2 is the ceiling that stood until 2026-08-03, kept here as the
# threshold it always implicitly was.  Raising the ceiling to 20 000 admitted
# 119 Valencia buildings carrying 11.00 % of the city's floor area; dropping
# them was the worse error, but blending them in silently would be too.
LARGE_FOOTPRINT_SINGLE_ZONE_M2 = 5000.0

# Site barometric pressure from the official ASHRAE design-condition file for
# Valencia 082840 (elevation 62 m).  OpenStudio's DesignDay default is 31 000 Pa
# -- roughly 9 000 m of altitude -- so this MUST be set explicitly, or the sizing
# run uses air a third as dense as the real thing.
SITE_BAROMETRIC_PRESSURE_PA = 100582.0

# Rai's design days, transcribed field for field from
# ``data/weather/ESP_Valencia.082840_IWEC.ddy`` (ASHRAE 2009 Handbook conditions,
# distributed alongside the EPW).  His Coil Sizing Summary names them outright:
# "VALENCIA ANN HTG 99.6% CONDNS DB" and "VALENCIA ANN CLG .4% CONDNS DB=>MWB".
#
# The two days use different solar models, which is easy to get wrong:
#   * heating is ASHRAEClearSky with clearness 0.0 -- sunless by definition,
#   * cooling is ASHRAETau with the station's own taub/taud.
# Leaving the cooling day on OpenStudio's default (ClearSky, clearness 0.0)
# strips all solar gain out of the summer sizing run and undersizes the cooling
# coils; forcing clearness 1.0 instead overshoots it.  Only tau reproduces it.
RAI_DESIGN_DAYS = {
    "heating": {
        "month": 1, "day": 21, "db": 1.0, "range": 0.0, "wb": 1.0,
        "day_type": "WinterDesignDay", "wind_speed": 2.0, "wind_direction": 280.0,
        "solar_model": "ASHRAEClearSky", "clearness": 0.0,
    },
    "cooling": {
        "month": 8, "day": 21, "db": 33.1, "range": 9.4, "wb": 21.4,
        "day_type": "SummerDesignDay", "wind_speed": 5.2, "wind_direction": 120.0,
        "solar_model": "ASHRAETau", "taub": 0.505, "taud": 1.864,
    },
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _by_name(objects, name: str):
    for obj in objects:
        if obj.nameString() == name:
            return obj
    return None


def _require(objects, name: str, what: str):
    found = _by_name(objects, name)
    if found is None:
        raise RuntimeError(f"{what} '{name}' not found in the model/template")
    return found


def _residential_space_type(osm):
    return _require(osm.getSpaceTypes(), RESIDENTIAL_SPACE_TYPE, "Space type")


# ---------------------------------------------------------------------------
# Layer 1 - real occupancy + coupled ventilation
# ---------------------------------------------------------------------------
def apply_real_occupancy(osm, occupants: float, res_area_m2: float) -> dict:
    """Replace the CTE 20 m2/person norm with the real cadastre head count.

    The residential floors are extrusions of one footprint, so an area-weighted
    split over the residential zones is identical to an equal-people-per-floor
    split; setting one density on the shared space type implements both.

    The occupancy schedule and the 70 W/person activity level are untouched -
    only the head count changes.  The occupancy-driven ventilation object is
    rescaled with it (it is the CTE 4 l/s/person hygienic requirement modelled
    as infiltration, so it must follow the real number of people).  The constant
    0.2 ACH airtightness and the 4 ACH summer-night objects are left alone.
    """
    if occupants < 0:
        raise ValueError("occupants must be >= 0")
    if res_area_m2 <= 0:
        raise ValueError("res_area_m2 must be > 0")

    space_type = _residential_space_type(osm)
    density = occupants / res_area_m2          # person/m2
    people_objects = list(space_type.people())
    if not people_objects:
        raise RuntimeError(
            f"'{RESIDENTIAL_SPACE_TYPE}' carries no People object - "
            "the template contract changed")

    norm_density = None
    for people in people_objects:
        definition = people.peopleDefinition()
        if norm_density is None:
            existing = definition.peopleperSpaceFloorArea()
            if existing.is_initialized():
                norm_density = existing.get()
            else:
                # the template states the norm as m2/person, not person/m2
                per_person = definition.spaceFloorAreaperPerson()
                if per_person.is_initialized() and per_person.get() > 0:
                    norm_density = 1.0 / per_person.get()
        clone = definition.clone(osm).to_PeopleDefinition().get()
        clone.setName(f"{definition.nameString()} (real pob_total)")
        if not clone.setPeopleperSpaceFloorArea(density):
            raise RuntimeError("could not set the real occupancy density")
        people.setPeopleDefinition(clone)

    # Coupled ventilation: 0.004 m3/s per person, expressed per floor area.
    ventilation_flow = CTE_VENTILATION_M3S_PER_PERSON * density
    rescaled = 0
    for inf in space_type.spaceInfiltrationDesignFlowRates():
        if OCCUPANCY_INFILTRATION_MARKER not in inf.nameString().lower():
            continue
        if not inf.setFlowperSpaceFloorArea(ventilation_flow):
            raise RuntimeError(f"could not rescale '{inf.nameString()}'")
        rescaled += 1
    if rescaled == 0:
        raise RuntimeError(
            "occupancy-driven infiltration object not found on "
            f"'{RESIDENTIAL_SPACE_TYPE}' - the template contract changed")

    return {
        "occupants": round(float(occupants), 3),
        "residential_area_m2": round(float(res_area_m2), 2),
        "people_per_m2": round(density, 6),
        "m2_per_person": round(res_area_m2 / occupants, 2) if occupants else None,
        "cte_norm_people_per_m2": round(norm_density, 6) if norm_density else None,
        "occupancy_ventilation_m3s_m2": round(ventilation_flow, 9),
        "people_objects_updated": len(people_objects),
        "infiltration_objects_rescaled": rescaled,
    }


# ---------------------------------------------------------------------------
# Layer 2 - Rai's ground-floor regime
# ---------------------------------------------------------------------------
def apply_rai_ground_regime(osm, *, thermostat: bool = False) -> dict:
    """Turn the unconditioned ground buffer into Rai's conditioned Terciario zone.

    Rai models the ground floor as a conditioned tertiary space that is kept
    OUT of the reported floor-area basis, over an insulated slab.  The builder
    already creates the ground space (ground_unconditioned=True), so this is a
    post-build re-typing rather than a geometry change.
    """
    buffer_type = _by_name(osm.getSpaceTypes(), GROUND_BUFFER_SPACE_TYPE)
    if buffer_type is None:
        raise RuntimeError(
            f"'{GROUND_BUFFER_SPACE_TYPE}' not in the model - build with "
            "ground_unconditioned=True before applying the Rai ground regime")
    ground_spaces = [sp for sp in buffer_type.spaces()]
    if not ground_spaces:
        raise RuntimeError("no ground buffer space found in the model")

    terciario = _require(osm.getSpaceTypes(), GROUND_TERCIARIO_SPACE_TYPE, "Space type")
    slab = _require(osm.getConstructions(), GROUND_SLAB_CONSTRUCTION, "Construction")

    # Rai attaches a terminal unit to the ground zone but NO thermostat: his
    # own sizing table reports 0.000 W design load for both heating and cooling
    # on THERMAL ZONE 1.  The zone therefore reads "Conditioned = Yes" while it
    # is in practice a passive commercial buffer that only carries its internal
    # gains.  Giving it a dwelling thermostat instead adds a whole extra
    # conditioned storey (4.4 kW of design heating on the pilot) and is the
    # single biggest source of excess heating against his results.
    thermostat_object = None
    if thermostat:
        sch_heat = _require(osm.getScheduleRulesets(), THERMOSTAT_HEATING_SCHEDULE, "Schedule")
        sch_cool = _require(osm.getScheduleRulesets(), THERMOSTAT_COOLING_SCHEDULE, "Schedule")
        thermostat_object = openstudio.model.ThermostatSetpointDualSetpoint(osm)
        thermostat_object.setName("Termostat Terciario planta baja")
        thermostat_object.setHeatingSetpointTemperatureSchedule(sch_heat)
        thermostat_object.setCoolingSetpointTemperatureSchedule(sch_cool)

    conditioned = 0
    slabs = 0
    for space in ground_spaces:
        space.setSpaceType(terciario)
        # Rai keeps the ground floor conditioned but OUT of the area basis.
        # This is what makes his EUI 54.5 kWh/m2 instead of 43.6, and it also
        # keeps the E+ "Net Conditioned Building Area" equal to the residential
        # area, so the existing QA cross-check stays valid.
        space.setPartofTotalFloorArea(False)
        for srf in space.surfaces():
            if srf.surfaceType() == "Floor" and srf.outsideBoundaryCondition() == "Ground":
                srf.setConstruction(slab)
                slabs += 1
        zone_opt = space.thermalZone()
        if zone_opt.isNull():
            raise RuntimeError(f"ground space '{space.nameString()}' has no thermal zone")
        zone = zone_opt.get()
        ideal = openstudio.model.ZoneHVACIdealLoadsAirSystem(osm)
        ideal.setName(f"Ideal Loads {zone.nameString()} (Rai ground)")
        ideal.addToThermalZone(zone)
        dsoa = terciario.designSpecificationOutdoorAir()
        if not dsoa.isNull():
            ideal.setDesignSpecificationOutdoorAirObject(dsoa.get())
        if thermostat_object is not None:
            zone.setThermostatSetpointDualSetpoint(thermostat_object)
        conditioned += 1

    return {
        "ground_spaces_conditioned": conditioned,
        "ground_thermostat": bool(thermostat),
        "ground_space_type": GROUND_TERCIARIO_SPACE_TYPE,
        "ground_slab_construction": GROUND_SLAB_CONSTRUCTION,
        "ground_slab_surfaces": slabs,
        "in_floor_area_basis": False,
    }


def apply_residential_ground(osm) -> dict:
    """Make the ground storey a dwelling floor instead of Rai's Terciario.

    For 9,829 Valencia buildings (37.2 %, measured 2026-08-03) Tipo15 records a
    dwelling at ground level - "B0"/"OD" planta codes - and for a detached
    VivUni the ground floor IS the house.  Typing those grounds as a commercial
    Terciario buffer misstates both the physics and the area basis.

    The geometry is untouched: `altura_max` counts the numbered storeys and
    excludes the bajo in both groups (measured, mode +0), so the frozen builder
    keeps producing ground + altura_max levels either way.  This layer only
    re-types the ground space:

      * residential space type -> occupancy, hygienic ventilation, internal
        gains and the PTHP COP split all follow from the space type, so
        `apply_real_occupancy` and `add_pthp_hvac` MUST run after this
      * kept IN the floor-area basis (it is dwelling area; Tipo15's own net
        area for these buildings includes it)
      * dwelling thermostat, like every other residential storey
      * the ground slab keeps the template default - period-appropriate, same
        reasoning as the interzone-slab decision (2026-07-27); Rai's insulated
        `Solera` belongs to his Terciario regime, not to a 1970s dwelling
    """
    buffer_type = _by_name(osm.getSpaceTypes(), GROUND_BUFFER_SPACE_TYPE)
    if buffer_type is None:
        raise RuntimeError(
            f"'{GROUND_BUFFER_SPACE_TYPE}' not in the model - build with "
            "ground_unconditioned=True before applying the residential ground")
    ground_spaces = list(buffer_type.spaces())
    if not ground_spaces:
        raise RuntimeError("no ground buffer space found in the model")

    residential = _require(osm.getSpaceTypes(), RESIDENTIAL_SPACE_TYPE, "Space type")
    sch_heat = _require(osm.getScheduleRulesets(), THERMOSTAT_HEATING_SCHEDULE, "Schedule")
    sch_cool = _require(osm.getScheduleRulesets(), THERMOSTAT_COOLING_SCHEDULE, "Schedule")
    thermostat = openstudio.model.ThermostatSetpointDualSetpoint(osm)
    thermostat.setName("Termostat Vivienda planta baja")
    thermostat.setHeatingSetpointTemperatureSchedule(sch_heat)
    thermostat.setCoolingSetpointTemperatureSchedule(sch_cool)

    converted = 0
    for space in ground_spaces:
        space.setSpaceType(residential)
        space.setPartofTotalFloorArea(True)
        zone_opt = space.thermalZone()
        if zone_opt.isNull():
            raise RuntimeError(f"ground space '{space.nameString()}' has no thermal zone")
        zone = zone_opt.get()
        ideal = openstudio.model.ZoneHVACIdealLoadsAirSystem(osm)
        ideal.setName(f"Ideal Loads {zone.nameString()} (residential ground)")
        ideal.addToThermalZone(zone)
        zone.setThermostatSetpointDualSetpoint(thermostat)
        converted += 1

    return {
        "ground_spaces_converted": converted,
        "ground_space_type": RESIDENTIAL_SPACE_TYPE,
        "ground_thermostat": True,
        "in_floor_area_basis": True,
    }


def residential_storeys_from_cadastre(cadastral_area_m2, footprint_m2,
                                      built_storeys: int) -> int:
    """How many storeys the recorded dwelling area can actually fill.

    `altura_max` is the highest planta that HOLDS a dwelling, not a statement
    that every storey below it is housing.  Measured on Benicalap v5: 85
    BlocPluri buildings carry 36.2 % of the district's floor area at 0.37
    dwellings per 100 m2 against 0.98 in the rest - same median storey count
    (5), same dwelling size (104 vs 96 m2 of cadastral area per dwelling), far
    fewer dwellings.  The cadastre is not under-reporting; that floor area is
    commercial or office.  The frozen builder types all of it as housing, so it
    receives dwelling occupancy, schedules, thermostat and DHW.  Across the
    district 842,462 m2 - 24.6 % of the modelled floor area - is affected.

    The rule is the fewest whole storeys whose gross floor plate can contain the
    cadastral dwelling area.  `ceil` is deliberate: a gross footprint storey is
    larger than the net `sfc` recorded inside it, so rounding down would strip
    housing from buildings that are entirely residential.  The tolerance this
    implies scales with height on its own - a 5-storey block must be 25 % over
    before it loses a storey, an 8-storey one 14 % - which is the correct
    geometry, and it is why no calibration constant appears here.

    Without cadastral evidence the built storey count is returned unchanged, so
    a building whose Tipo15 join failed is never silently shrunk.
    """
    try:
        cadastral = float(cadastral_area_m2)
        footprint = float(footprint_m2)
    except (TypeError, ValueError):
        return int(built_storeys)
    if not (cadastral > 0 and footprint > 0):
        return int(built_storeys)
    needed = math.ceil(cadastral / footprint)
    return max(1, min(int(built_storeys), needed))


def apply_mixed_use_storeys(osm, keep_residential: int) -> dict:
    """Re-type the lowest excess dwelling storeys to Rai's conditioned Terciario.

    Commercial floor sits at the bottom of a Spanish mixed-use block, so the
    conversion runs upward from the lowest dwelling storey.  The converted
    storeys get exactly the regime the ground floor already gets under
    `apply_rai_ground_regime` - conditioned Terciario, no dwelling thermostat,
    OUT of the floor-area basis - because that treatment was decided against
    Rai's own model (2026-07-27) and this is the same kind of space.

    Must run BEFORE `apply_real_occupancy` and `add_pthp_hvac`: both resolve
    through the residential space type, so a storey converted afterwards would
    keep dwelling occupants and a dwelling-COP heat pump.
    """
    residential = _residential_space_type(osm)
    spaces = sorted(residential.spaces(),
                    key=lambda s: min(v.z() for srf in s.surfaces()
                                      for v in srf.vertices()))
    excess = len(spaces) - int(keep_residential)
    if excess <= 0:
        return {"storeys_converted": 0, "residential_storeys": len(spaces),
                "space_type": None, "in_floor_area_basis": True}

    terciario = _require(osm.getSpaceTypes(), GROUND_TERCIARIO_SPACE_TYPE, "Space type")
    dsoa = terciario.designSpecificationOutdoorAir()
    converted = []
    for space in spaces[:excess]:
        space.setSpaceType(terciario)
        space.setPartofTotalFloorArea(False)
        zone_opt = space.thermalZone()
        if zone_opt.isNull():
            raise RuntimeError(f"space '{space.nameString()}' has no thermal zone")
        zone = zone_opt.get()
        # Passive commercial floor, like Rai's ground: it carries its internal
        # gains but no dwelling setpoint.
        zone.resetThermostatSetpointDualSetpoint()
        for equip in zone.equipment():
            ideal = equip.to_ZoneHVACIdealLoadsAirSystem()
            if not ideal.isNull() and not dsoa.isNull():
                ideal.get().setDesignSpecificationOutdoorAirObject(dsoa.get())
        converted.append(space.nameString())

    return {
        "storeys_converted": len(converted),
        "residential_storeys": len(spaces) - len(converted),
        "space_type": GROUND_TERCIARIO_SPACE_TYPE,
        "converted_spaces": converted,
        "in_floor_area_basis": False,
    }


def tag_terciario_end_uses(osm) -> dict:
    """Meter the commercial storeys' lights and equipment under their own subcategory.

    The template already gives Terciario its own `Iluminacion Terciario Media`
    and equipment objects - they were simply reported in the same
    `Interior Lighting:General` row as the dwellings, so a reader could not tell
    how much of a mixed-use block's electricity was shops.  This is the pattern
    `add_dhw_loop` uses to keep boiler gas out of the space-heating row, applied
    to the other place where two uses share one meter.

    Only lights and equipment can be attributed this way.  Heating, cooling,
    fans and pumps are metered per end use, not per zone, so they stay in one
    bucket - that limit is stated where the numbers are published rather than
    papered over.  Run this after every re-typing layer, so it sees the space
    types the model finished with.
    """
    terciario = _by_name(osm.getSpaceTypes(), GROUND_TERCIARIO_SPACE_TYPE)
    if terciario is None:
        return {"tagged_lights": 0, "tagged_equipment": 0, "subcategory": None}

    lights = list(terciario.lights())
    equipment = list(terciario.electricEquipment())
    for load in lights + equipment:
        load.setEndUseSubcategory(TERCIARIO_END_USE_SUBCATEGORY)
    return {
        "tagged_lights": len(lights),
        "tagged_equipment": len(equipment),
        "subcategory": TERCIARIO_END_USE_SUBCATEGORY,
        "spaces": len(list(terciario.spaces())),
    }


def glaze_ground_floor(osm, config=None) -> dict:
    """Glaze the ground floor with the same WWR as the residential storeys.

    ``model_builder._add_facade_openings`` is only called for residential
    spaces, so the ground storey comes out with no windows at all.  Rai applies
    one window-to-wall ratio to every storey - his own report gives
    31.19 m2 north / 60.76 m2 south over the full 232.50 m2 per side, i.e.
    exactly 13.42 % / 26.14 % including the ground floor.  A ground floor that
    is typed as a commercial space but carries zero glazing is also physically
    wrong: this is where the shops are.

    Returns the counters so the caller can record what was added.
    """
    cfg = config or mb.DEFAULT_BUILD_CONFIG
    # Take whichever candidate type actually CARRIES the ground space: in the
    # Rai flow the re-typing runs first so it sits on Terciario, in the
    # residential flow glazing runs first so it is still on the buffer.  Both
    # types exist in the template, so "first non-null" would find an empty one.
    ground_spaces: list = []
    for name in (GROUND_TERCIARIO_SPACE_TYPE, GROUND_BUFFER_SPACE_TYPE):
        space_type = _by_name(osm.getSpaceTypes(), name)
        if space_type is not None:
            ground_spaces = list(space_type.spaces())
            if ground_spaces:
                break
    if not ground_spaces:
        raise RuntimeError("no ground space found to glaze")

    window_construction = None
    for sub in osm.getSubSurfaces():
        if sub.subSurfaceType() in GLAZED_SUBSURFACE_TYPES and sub.construction().is_initialized():
            window_construction = sub.construction().get().to_Construction().get()
            break
    if window_construction is None:
        raise RuntimeError("no existing window construction to reuse for the ground floor")

    # shopfront glazing, not dwelling balconies
    ground_cfg = cfg.model_copy(deep=True)
    ground_cfg.openings.balcony_doors_per_facade_floor = 0

    windows = 0
    glass_area = 0.0
    facades = 0
    for space in ground_spaces:
        for srf in space.surfaces():
            if srf.surfaceType() != "Wall" or srf.outsideBoundaryCondition() != "Outdoors":
                continue
            azimuth = openstudio.radToDeg(srf.azimuth())
            ratio = mb.wwr_for_azimuth(azimuth, config=ground_cfg)
            n_w, _n_d, glass, _target = mb._add_facade_openings(
                osm, srf, ratio, window_construction, None, config=ground_cfg)
            windows += n_w
            glass_area += glass
            facades += 1

    return {
        "ground_windows": windows,
        "ground_glass_area_m2": round(glass_area, 1),
        "ground_facades_glazed": facades,
    }


INTERZONE_CEILING_CONSTRUCTION = "Techo Interior Referencia"
INTERZONE_FLOOR_CONSTRUCTION = "Suelo Interior Referencia"


def apply_rai_interzone_slabs(osm) -> dict:
    """Use the template's interior reference slab between storeys.

    Rai's `EdiPluriP04_CEILING` / `_FLOOR` report U = 2.119 W/m2K.  The default
    construction set gives us `Techo/Suelo sin aislante`, which EnergyPlus
    reports at 1.674 - about 21 % less conductive.  That matters because the
    ground storey carries tertiary internal gains without a thermostat: a
    tighter slab traps that heat downstairs instead of letting it help the
    dwellings above, which pushes heating up and cooling down.

    `Techo/Suelo Interior Referencia` is the template's matched interior pair
    and lands at roughly 2.24 - within ~6 % of Rai instead of ~21 %.
    """
    ceiling = _require(osm.getConstructions(), INTERZONE_CEILING_CONSTRUCTION, "Construction")
    floor = _require(osm.getConstructions(), INTERZONE_FLOOR_CONSTRUCTION, "Construction")
    ceilings = floors = 0
    for srf in osm.getSurfaces():
        if srf.outsideBoundaryCondition() != "Surface":
            continue
        if srf.surfaceType() == "RoofCeiling":
            srf.setConstruction(ceiling)
            ceilings += 1
        elif srf.surfaceType() == "Floor":
            srf.setConstruction(floor)
            floors += 1
    return {
        "interzone_ceiling": INTERZONE_CEILING_CONSTRUCTION,
        "interzone_floor": INTERZONE_FLOOR_CONSTRUCTION,
        "interzone_ceilings": ceilings,
        "interzone_floors": floors,
    }


RAI_ROOF_SOLAR_ABSORPTANCE = 0.70   # his Opaque Exterior row: reflectance 0.30


def apply_rai_roof_absorptance(osm, absorptance: float = RAI_ROOF_SOLAR_ABSORPTANCE) -> dict:
    """Give the exposed roof Rai's solar absorptance.

    His envelope matches ours surface for surface -- wall and ground slab both
    report reflectance 0.07 in either model -- with one exception: the roof.
    Rai reports reflectance 0.30 (absorptance 0.70), a light flat roof; our
    template materials default to 0.07 / 0.93, a dark one.

    That single property is worth ~35 % of the top storey's cooling design load,
    because it is the only horizontal surface taking full summer sun.  It does
    not show up in the winter comparison at all: the ASHRAE 99.6 % heating day
    has a 0.0 K daily range and no solar, so it is a steady-state test that
    absorptance and thermal mass cannot influence.

    The outer layer is cloned before it is edited, so shared template materials
    used by other constructions keep their own values.
    """
    touched, materials = 0, set()
    for srf in osm.getSurfaces():
        if srf.surfaceType() != "RoofCeiling" or srf.outsideBoundaryCondition() != "Outdoors":
            continue
        construction = srf.construction()
        if not construction.is_initialized():
            continue
        layered = construction.get().to_LayeredConstruction()
        if not layered.is_initialized():
            continue
        layered = layered.get()
        outer = layered.layers()[0]
        if outer.nameString() in materials:
            touched += 1
            continue
        clone = outer.clone(osm)
        # The generic OpaqueMaterial setter takes boost::optional<double>; the
        # concrete subclasses take a plain double, so cast before setting.
        opaque = None
        for cast in ("to_StandardOpaqueMaterial", "to_MasslessOpaqueMaterial"):
            candidate = getattr(clone, cast)()
            if candidate.is_initialized():
                opaque = candidate.get()
                break
        if opaque is None:
            clone.remove()
            raise RuntimeError(
                f"apply_rai_roof_absorptance: outer roof layer {outer.nameString()!r} "
                "is neither a standard nor a massless opaque material")
        opaque.setName(f"{outer.nameString()} (Rai absorptance {absorptance})")
        if not opaque.setSolarAbsorptance(absorptance):
            raise RuntimeError(
                f"apply_rai_roof_absorptance: {opaque.nameString()!r} rejected "
                f"solar absorptance {absorptance}")
        layered.setLayer(0, opaque)
        materials.add(opaque.nameString())
        touched += 1
    if not touched:
        raise RuntimeError("apply_rai_roof_absorptance: no exterior roof surface found")
    return {"roof_solar_absorptance": absorptance, "roof_surfaces": touched}


WINDOW_FRAME_NAME = "Carpinteria_Aluminio_simple"   # U 5.7, matches the IVE single glazing
WINDOW_FRAME_WIDTH_M = 0.070                        # all six template carpentry types
# Balcony doors are glazed openings with the same aluminium carpentry as the
# windows, so they are framed too - leaving them out would keep a quarter of
# the glazing frameless.
GLAZED_SUBSURFACE_TYPES = ("FixedWindow", "OperableWindow", "GlassDoor")


def glass_fraction_for_frames(config) -> float:
    """How much of the architectural opening is still glass once framed.

    EnergyPlus treats the sub-surface polygon as GLASS and grows the frame
    outwards from it, so a window drawn at the target WWR ends up with an
    opening about 25 % larger than intended.  Our WWR comes from the IVE
    typology and describes the hole in the facade, not the glazing, so the
    polygon has to be shrunk by this factor before the frame is added.

    Rai's own numbers are the sanity check: 73.80 m2 of glass inside 91.96 m2
    of opening = 0.8025, and the 1.2 x 1.2 m window with a 70 mm frame gives
    1.44 / (1.34 x 1.34) = 0.8020.
    """
    openings = config.openings
    width, height = openings.window_width_m, openings.window_height_m
    frame = WINDOW_FRAME_WIDTH_M
    return (width * height) / ((width + 2 * frame) * (height + 2 * frame))


def apply_window_frames(osm, frame_name: str = WINDOW_FRAME_NAME) -> dict:
    """Attach Rai's window carpentry to every glazed sub-surface.

    In EnergyPlus the sub-surface polygon is the GLASS; the frame is added
    outside it.  Rai's model carries `WindowProperty:FrameAndDivider`, so his
    91.96 m2 of opening is only 73.80 m2 of glass.  Without frames our whole
    opening is glass, which is why our window heat gain and loss both run about
    18 % above his.  The template already ships six carpentry types.
    """
    frame = _require(osm.getWindowPropertyFrameAndDividers(), frame_name, "Frame and divider")
    if abs(frame.frameWidth() - WINDOW_FRAME_WIDTH_M) > 1e-6:
        raise RuntimeError(
            f"'{frame_name}' is {frame.frameWidth():.3f} m wide but the WWR "
            f"rescaling assumes {WINDOW_FRAME_WIDTH_M:.3f} m - the template changed")
    width = frame.frameWidth()
    applied = 0
    glass_area = 0.0
    opening_area = 0.0
    for sub in osm.getSubSurfaces():
        if sub.subSurfaceType() not in GLAZED_SUBSURFACE_TYPES:
            continue
        if not sub.setWindowPropertyFrameAndDivider(frame):
            continue
        applied += 1
        area = sub.grossArea()
        glass_area += area
        # the frame is a ring of constant width around the polygon, so the
        # opening EnergyPlus reports is area + width*perimeter + 4*width^2
        vertices = list(sub.vertices())
        perimeter = sum(
            (vertices[i] - vertices[i - 1]).length() for i in range(len(vertices)))
        opening_area += area + width * perimeter + 4.0 * width ** 2
    if applied == 0:
        raise RuntimeError("no glazed sub-surface accepted the frame and divider")
    return {
        "window_frame": frame_name,
        "frame_width_m": round(width, 3),
        "windows_with_frame": applied,
        "glass_area_m2": round(glass_area, 1),
        "opening_area_m2": round(opening_area, 1),
    }


# ---------------------------------------------------------------------------
# Layer 3 - domestic hot water
# ---------------------------------------------------------------------------
def add_dhw_loop(osm, occupants: float, litres_per_person_day: float | None = None,
                 climate=None) -> dict:
    """Add Rai's DHW plant loop, sized from the real head count.

    The demand is Rai-aligned: 28 l/person/day delivered at the 50 C loop
    setpoint, which is ~80 % of the CTE DB-HE4 energy reference stated at
    60 C - a measured decision, not a CTE equivalence (see the constant).

    Structure copied from Rai's EdiPluriP04: PlantLoop + Boiler:HotWater +
    Pump:ConstantSpeed + SetpointManager:Scheduled, with one WaterUse:Equipment
    per residential space.

    The boiler carries an explicit end-use subcategory.  Without it EnergyPlus
    reports the DHW gas inside 'End Uses -> Heating: Natural Gas' and it becomes
    indistinguishable from space heating - which is exactly why Rai's monthly
    heating profile is flat through the summer.

    `climate` supplies the mains water temperature, which is climate-dependent:
    how much energy a litre of hot water costs depends on how cold it arrives.
    Left unset, Valencia's verified 10 C is used.
    """
    per_person = (DHW_LITRES_PER_PERSON_DAY if litres_per_person_day is None
                  else float(litres_per_person_day))
    litres_per_day = float(occupants) * per_person

    space_type = _residential_space_type(osm)
    spaces = sorted(space_type.spaces(), key=lambda s: s.nameString())
    if not spaces:
        raise RuntimeError("no residential spaces found for the DHW loop")

    # The template encodes its own l/day -> m3/s scale; read it instead of
    # hard-coding, so a template update stays consistent.
    reference = _require(osm.getWaterUseEquipmentDefinitions(),
                         DHW_REFERENCE_DEFINITION, "WaterUse definition")
    scale = reference.peakFlowRate() / 150.0
    demand_schedule = _require(osm.getScheduleRulesets(), DHW_DEMAND_SCHEDULE, "Schedule")

    # No target-temperature schedule on the equipment: Rai's own template
    # definitions carry none, and pinning it to exactly the loop setpoint makes
    # EnergyPlus emit a float-epsilon "target > hot water" warning per instance.

    loop = openstudio.model.PlantLoop(osm)
    loop.setName("DHW Plant Loop (deep)")
    loop.setMaximumLoopTemperature(100.0)
    loop.setMinimumLoopTemperature(0.0)
    sizing = loop.sizingPlant()
    sizing.setLoopType("Heating")
    sizing.setDesignLoopExitTemperature(DHW_LOOP_EXIT_TEMPERATURE_C)
    sizing.setLoopDesignTemperatureDifference(DHW_LOOP_DELTA_T_K)

    boiler = openstudio.model.BoilerHotWater(osm)
    boiler.setName("DHW Boiler (deep)")
    boiler.setFuelType("NaturalGas")
    boiler.setNominalThermalEfficiency(DHW_BOILER_EFFICIENCY)
    boiler.setEndUseSubcategory(DHW_END_USE_SUBCATEGORY)
    loop.addSupplyBranchForComponent(boiler)

    pump = openstudio.model.PumpConstantSpeed(osm)
    pump.setName("DHW Pump (deep)")
    pump.setMotorEfficiency(0.90)
    pump.addToNode(loop.supplyInletNode())

    setpoint_schedule = openstudio.model.ScheduleConstant(osm)
    setpoint_schedule.setName(f"DHW loop setpoint {DHW_LOOP_SETPOINT_C:.0f} C (deep)")
    setpoint_schedule.setValue(DHW_LOOP_SETPOINT_C)
    manager = openstudio.model.SetpointManagerScheduled(osm, setpoint_schedule)
    manager.setName("DHW Setpoint Manager (deep)")
    manager.addToNode(loop.supplyOutletNode())

    connections = openstudio.model.WaterUseConnections(osm)
    connections.setName("DHW Connections (deep)")

    per_space_litres = litres_per_day / len(spaces)
    for space in spaces:
        definition = openstudio.model.WaterUseEquipmentDefinition(osm)
        definition.setName(f"Demanda ACS {per_space_litres:.1f}l/dia ({space.nameString()})")
        definition.setPeakFlowRate(per_space_litres * scale)
        definition.setEndUseSubcategory(DHW_END_USE_SUBCATEGORY)

        equipment = openstudio.model.WaterUseEquipment(definition)
        equipment.setName(f"DemandaACS {space.nameString()}")
        equipment.setFlowRateFractionSchedule(demand_schedule)
        equipment.setSpace(space)
        connections.addWaterUseEquipment(equipment)

    loop.addDemandBranchForComponent(connections)

    # Rai's report shows "FixedDefault 10.0 C", which is what EnergyPlus falls
    # back to when no Site:WaterMainsTemperature object exists.  OpenStudio has
    # no FixedDefault method, so pin the same 10 C explicitly via a schedule -
    # same physics, but reproducible instead of relying on a program default.
    mains_temperature = (climate.water_mains_temperature_c if climate is not None
                         else WATER_MAINS_TEMPERATURE_C)
    mains_schedule = openstudio.model.ScheduleConstant(osm)
    mains_schedule.setName(f"Water mains {mains_temperature:.0f} C (deep)")
    mains_schedule.setValue(mains_temperature)
    water_mains = osm.getSiteWaterMainsTemperature()
    water_mains.setCalculationMethod("Schedule")
    water_mains.setTemperatureSchedule(mains_schedule)

    return {
        "dhw_litres_per_person_day": round(per_person, 2),
        "dhw_litres_per_day": round(litres_per_day, 1),
        "dhw_litres_per_day_per_space": round(per_space_litres, 1),
        "dhw_equipment_count": len(spaces),
        "dhw_boiler_efficiency": DHW_BOILER_EFFICIENCY,
        "dhw_setpoint_c": DHW_LOOP_SETPOINT_C,
        "dhw_end_use_subcategory": DHW_END_USE_SUBCATEGORY,
    }


# ---------------------------------------------------------------------------
# Layer 4 - Rai's packaged terminal heat pump
# ---------------------------------------------------------------------------
def add_pthp_hvac(osm, heating_cop: float | None = None,
                  cooling_cop: float | None = None, climate=None) -> dict:
    """Swap ideal loads for Rai's PTHP on every conditioned zone.

    ``mb.add_real_hvac`` (gas-burner PTAC) is deliberately left in place: the
    Part C/D/F baselines depend on it.  This is the heat-pump sibling.

    Like the PTAC path, the units supply NO mechanical outdoor air - Rai's own
    model delivers 0.014-0.026 ACH mechanical ventilation, i.e. effectively
    zero, and the fresh air arrives through the infiltration objects.

    `climate` supplies the design days, the site barometric pressure and the
    ground temperature - the values that decide how the equipment is sized.
    They are the reason a climate has to be swapped as a whole: an EPW alone
    changes the weather the building lives in, while the coils stay sized for
    wherever the design days came from.  Left unset, Valencia's verified set
    is used.
    """
    heat_cop = PTHP_HEATING_COP if heating_cop is None else float(heating_cop)
    cool_cop = PTHP_COOLING_COP if cooling_cop is None else float(cooling_cop)

    residential_zones = set()
    space_type = _by_name(osm.getSpaceTypes(), RESIDENTIAL_SPACE_TYPE)
    if space_type is not None:
        for space in space_type.spaces():
            zone_opt = space.thermalZone()
            if zone_opt.is_initialized():
                residential_zones.add(zone_opt.get().nameString())

    zones = []
    for unit in list(osm.getZoneHVACIdealLoadsAirSystems()):
        zone_opt = unit.thermalZone()
        if zone_opt.is_initialized():
            zones.append(zone_opt.get())
        unit.remove()
    if not zones:
        raise RuntimeError("add_pthp_hvac: no ideal-loads zones found - build the model first")

    always_on = osm.alwaysOnDiscreteSchedule()
    for zone in zones:
        is_residential = zone.nameString() in residential_zones
        zone_heat_cop = heat_cop if is_residential else PTHP_GROUND_HEATING_COP
        fan_efficiency = (PTHP_FAN_EFFICIENCY if is_residential
                          else PTHP_GROUND_FAN_EFFICIENCY)

        fan = openstudio.model.FanOnOff(osm, always_on)
        fan.setName(f"PTHP Fan {zone.nameString()}")
        fan.setFanEfficiency(fan_efficiency)
        fan.setPressureRise(PTHP_FAN_PRESSURE_RISE_PA)
        fan.setMotorEfficiency(PTHP_FAN_MOTOR_EFFICIENCY)

        heating_coil = openstudio.model.CoilHeatingDXSingleSpeed(osm)
        heating_coil.setName(f"PTHP DX Heat {zone.nameString()}")
        heating_coil.setRatedCOP(zone_heat_cop)

        cooling_coil = openstudio.model.CoilCoolingDXSingleSpeed(osm)
        cooling_coil.setName(f"PTHP DX Cool {zone.nameString()}")
        cooling_coil.setRatedCOP(cool_cop)

        supplemental = openstudio.model.CoilHeatingElectric(osm, always_on)
        supplemental.setName(f"PTHP Supplemental Heat {zone.nameString()}")

        unit = openstudio.model.ZoneHVACPackagedTerminalHeatPump(
            osm, always_on, fan, heating_coil, cooling_coil, supplemental)
        unit.setName(f"PTHP {zone.nameString()}")
        unit.setOutdoorAirFlowRateDuringCoolingOperation(0.0)
        unit.setOutdoorAirFlowRateDuringHeatingOperation(0.0)
        unit.setOutdoorAirFlowRateWhenNoCoolingorHeatingisNeeded(0.0)
        if not unit.addToThermalZone(zone):
            raise RuntimeError(f"add_pthp_hvac: PTHP could not be added to {zone.nameString()}")

    # Everything below this line is climate-dependent.  It used to be pinned to
    # Valencia by hand; it now travels with the climate so that swapping the
    # weather file cannot leave the equipment sized for a different place.
    design_days = climate.design_days if climate is not None else RAI_DESIGN_DAYS
    pressure = (climate.barometric_pressure_pa if climate is not None
                else SITE_BAROMETRIC_PRESSURE_PA)
    ground_temperature = (climate.ground_temperature_c if climate is not None
                          else GROUND_TEMPERATURE_C)
    site_label = climate.name if climate is not None else "Valencia"

    for label, day in design_days.items():
        design_day = openstudio.model.DesignDay(osm)
        design_day.setName(f"{site_label} {label} design day")
        design_day.setMaximumDryBulbTemperature(day["db"])
        design_day.setDailyDryBulbTemperatureRange(day["range"])
        design_day.setHumidityConditionType(day.get("humidity_type", "Wetbulb"))
        design_day.setWetBulbOrDewPointAtMaximumDryBulb(day["wb"])
        design_day.setMonth(day["month"])
        design_day.setDayOfMonth(day["day"])
        design_day.setDayType(day["day_type"])
        design_day.setBarometricPressure(day.get("pressure", pressure))
        design_day.setWindSpeed(day["wind_speed"])
        design_day.setWindDirection(day["wind_direction"])
        design_day.setRainIndicator(False)
        design_day.setSnowIndicator(False)
        design_day.setDaylightSavingTimeIndicator(False)
        design_day.setSolarModelIndicator(day["solar_model"])
        if day["solar_model"] == "ASHRAETau":
            design_day.setAshraeTaub(day["taub"])
            design_day.setAshraeTaud(day["taud"])
        else:
            design_day.setSkyClearness(day["clearness"])

    sizing_parameters = osm.getSizingParameters()
    sizing_parameters.setHeatingSizingFactor(HEATING_SIZING_FACTOR)
    sizing_parameters.setCoolingSizingFactor(COOLING_SIZING_FACTOR)

    ground = osm.getSiteGroundTemperatureBuildingSurface()
    ground.setAllMonthlyTemperatures([ground_temperature] * 12)

    control = osm.getSimulationControl()
    control.setDoZoneSizingCalculation(True)
    control.setRunSimulationforSizingPeriods(False)
    control.setRunSimulationforWeatherFileRunPeriods(True)
    control.setMaximumNumberofWarmupDays(MAXIMUM_WARMUP_DAYS)

    return {
        "hvac_system": "ZoneHVAC:PackagedTerminalHeatPump",
        "hvac_zones": len(zones),
        "residential_zones": len(residential_zones),
        "heating_cop": heat_cop,
        "cooling_cop": cool_cop,
        "ground_heating_cop": PTHP_GROUND_HEATING_COP,
        "heating_sizing_factor": HEATING_SIZING_FACTOR,
        "cooling_sizing_factor": COOLING_SIZING_FACTOR,
        "ground_temperature_c": ground_temperature,
        "climate": climate.name if climate is not None else "valencia_iwec (built in)",
    }


# ---------------------------------------------------------------------------
# Results - DHW split and the two area bases
# ---------------------------------------------------------------------------
def read_end_uses_split(sql_path: Path, res_area_m2: float,
                        total_conditioned_area_m2: float | None = None) -> dict:
    """Annual end uses with DHW separated from space heating.

    ``sim.read_end_uses`` assumes there is no DHW plant, so its Heating row is
    pure space heating.  Once a DHW boiler exists that assumption breaks: the
    boiler gas lands in the same row.  The subcategory written by
    ``add_dhw_loop`` lets us subtract it again here.

    Reports both area bases: the residential-only one (primary, comparable with
    Rai and with ConsumE) and the total conditioned one.
    """
    import sqlite3

    total_area = float(total_conditioned_area_m2 or res_area_m2)
    con = sqlite3.connect(sql_path)
    try:
        def gj(table: str, row_name: str, column: str) -> float:
            value = sim._tabular_value(
                con, "AnnualBuildingUtilityPerformanceSummary", table, row_name, column)
            return (value or 0.0) * sim.GJ_TO_KWH

        cool_elec = gj("End Uses", "Cooling", "Electricity")
        fans_elec = gj("End Uses", "Fans", "Electricity")
        pumps_elec = gj("End Uses", "Pumps", "Electricity")
        lighting = gj("End Uses", "Interior Lighting", "Electricity")
        equipment = gj("End Uses", "Interior Equipment", "Electricity")
        site_elec = gj("End Uses", "Total End Uses", "Electricity")
        site_gas = gj("End Uses", "Total End Uses", "Natural Gas")

        # The commercial storeys' own lights and equipment, tagged by
        # `tag_terciario_end_uses`.  A model with no Terciario space simply has
        # no such row and these come back zero.
        terciario_lighting = gj("End Uses By Subcategory",
                                f"Interior Lighting:{TERCIARIO_END_USE_SUBCATEGORY}", "Electricity")
        terciario_equipment = gj("End Uses By Subcategory",
                                 f"Interior Equipment:{TERCIARIO_END_USE_SUBCATEGORY}", "Electricity")

        # EnergyPlus meters the DHW boiler under the *Heating* end use, so the
        # split has to come from the subcategory rows ("<end use>:<subcategory>").
        # Reading them directly is safer than subtracting one from the other.
        space_heat_gas = gj("End Uses By Subcategory", "Heating:General", "Natural Gas")
        heat_elec = gj("End Uses By Subcategory", "Heating:General", "Electricity")
        dhw_gas = (gj("End Uses By Subcategory", f"Heating:{DHW_END_USE_SUBCATEGORY}", "Natural Gas")
                   + gj("End Uses By Subcategory", f"Water Systems:{DHW_END_USE_SUBCATEGORY}", "Natural Gas"))
        dhw_elec = (gj("End Uses By Subcategory", f"Heating:{DHW_END_USE_SUBCATEGORY}", "Electricity")
                    + gj("End Uses By Subcategory", f"Water Systems:{DHW_END_USE_SUBCATEGORY}", "Electricity"))
    finally:
        con.close()

    total_site = site_elec + site_gas
    if total_site <= 0:
        raise RuntimeError(f"End Uses table empty or zero in SQL output: {sql_path}")

    dhw = dhw_gas + dhw_elec
    space_heating = space_heat_gas + heat_elec
    hvac = space_heating + cool_elec + fans_elec + pumps_elec

    # What can honestly be attributed to the commercial storeys, and what is
    # left.  HVAC is metered per end use rather than per zone, so it cannot be
    # split and stays with the dwellings: `residential_site_kwh` is therefore an
    # upper bound on the dwellings, not a clean sub-total, and it is named and
    # published as such.
    terciario_site = terciario_lighting + terciario_equipment
    residential_site = total_site - terciario_site

    def per_res(value: float) -> float:
        return round(value / res_area_m2, 2)

    def per_total(value: float) -> float:
        return round(value / total_area, 2)

    return {
        "space_heating_kwh": round(space_heating, 1),
        "cooling_kwh": round(cool_elec, 1),
        "dhw_kwh": round(dhw, 1),
        "total_site_kwh": round(total_site, 1),
        # primary basis: residential floor area (Rai / ConsumE comparable)
        "space_heating_kwh_m2": per_res(space_heating),
        "space_heating_gas_kwh_m2": per_res(space_heat_gas),
        "space_heating_elec_kwh_m2": per_res(heat_elec),
        "cooling_kwh_m2": per_res(cool_elec),
        "fans_kwh_m2": per_res(fans_elec),
        "pumps_kwh_m2": per_res(pumps_elec),
        "dhw_kwh_m2": per_res(dhw),
        "hvac_kwh_m2": per_res(hvac),
        "lighting_kwh_m2": per_res(lighting),
        "equipment_kwh_m2": per_res(equipment),
        "site_elec_kwh_m2": per_res(site_elec),
        "site_gas_kwh_m2": per_res(site_gas),
        "total_site_kwh_m2": per_res(total_site),
        # secondary basis: every conditioned zone, ground floor included
        "total_conditioned_area_m2": round(total_area, 2),
        "total_site_kwh_m2_conditioned": per_total(total_site),
        "space_heating_kwh_m2_conditioned": per_total(space_heating),
        "cooling_kwh_m2_conditioned": per_total(cool_elec),
        "dhw_share_pct": round(100.0 * dhw / total_site, 1),
        # the dwellings on their own, against the floor area that is theirs
        "terciario_site_kwh": round(terciario_site, 1),
        "terciario_lighting_kwh": round(terciario_lighting, 1),
        "terciario_equipment_kwh": round(terciario_equipment, 1),
        "residential_site_kwh": round(residential_site, 1),
        "residential_total_site_kwh_m2": per_res(residential_site),
        "terciario_share_pct": round(100.0 * terciario_site / total_site, 1),
        "area_basis": "residential_only (primary) + total_conditioned (secondary)",
        "residential_split_basis": (
            "lights and equipment attributed by space type; HVAC and DHW are "
            "metered per end use and remain in the residential figure"),
    }


# ---------------------------------------------------------------------------
# Error scanning
# ---------------------------------------------------------------------------
# EnergyPlus 25.2 writes "** Severe  **" (ONE space after the first asterisks).
# ``sim.scan_err_file`` searches for "**  Severe  **" with two, so its counter is
# always zero and its Severe guard never fires.  That module is frozen for the
# Workbench capability hashes, so the deep pipeline scans the file itself.
_SEVERE_PATTERN = "** Severe  **"
_FATAL_PATTERN = "** Fatal  **"

# Known-benign Severe: with a ZoneHVAC:PackagedTerminalHeatPump in the model,
# EnergyPlus tries to register the window-shading EMS actuators before the
# auto-generated "<construction>:<blind>:EXT" shaded construction is populated,
# and reports one Severe per glazed sub-surface.  We never use EMS, and the
# shading physics is unaffected: with the persiana the pilot cools with
# 26.37 GJ, without it 33.94 GJ (-22.3 %), matching the documented -24 %.
# Anything that is NOT this message still invalidates the run.
_BENIGN_SEVERE_MARKERS = (
    "Missing shade or blind layer in window construction",
    "EMS Actuator cannot be set",
)


def _severe_blocks(text: str) -> list[str]:
    """Split eplusout.err into one string per Severe message.

    A Severe message is its first line plus any `~~~` continuation lines that
    follow it, which is where EnergyPlus puts the detail a classification has to
    read.  The block ends at the next message of any level.
    """
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if _SEVERE_PATTERN in line:
            if current is not None:
                blocks.append("\n".join(current))
            current = [line]
        elif current is not None:
            # EnergyPlus writes continuations as "   **   ~~~   ** detail...",
            # so after stripping they start with "**", not with "~~~".  The old
            # startswith("~~~") never matched, every block silently collapsed
            # to its first line, and the `any` marker rule hid it - the switch
            # to requiring BOTH markers in the block exposed it (2026-08-03).
            if stripped.startswith("**") and "~~~" in stripped:
                current.append(line)
            else:
                blocks.append("\n".join(current))
                current = None
    if current is not None:
        blocks.append("\n".join(current))
    return blocks


def scan_err_deep(run_dir: Path) -> dict:
    """Scan eplusout.err with the pattern EnergyPlus actually writes.

    Raises on any Severe/Fatal that is not the documented PTHP + window-shading
    EMS registration message.
    """
    err_path = Path(run_dir) / "eplusout.err"
    if not err_path.exists():
        raise RuntimeError(f"eplusout.err missing in {run_dir} - run may not have started")
    text = err_path.read_text(errors="replace")

    # Severe messages are classified one block at a time.  Counting markers
    # across the whole file and subtracting could go negative - three benign
    # markers against two Severes - and `max(..., 0)` would then hide a real
    # one.  A block is benign only if it actually contains a known marker.
    severe_blocks = _severe_blocks(text)
    n_severe = len(severe_blocks)
    n_fatal = text.count(_FATAL_PATTERN)
    # A block is benign only when it carries BOTH markers - the documented
    # message always does (the shading line plus its EMS continuation).  With
    # `any`, an unrelated Severe that merely mentioned "EMS Actuator cannot be
    # set" - a message EnergyPlus emits for other actuators too - would have
    # been silently excused (review finding, 2026-08-03).
    benign = sum(1 for block in severe_blocks
                 if all(marker in block for marker in _BENIGN_SEVERE_MARKERS))
    unexplained = n_severe - benign

    # The authoritative totals live on the LAST summary line (Warmup and Sizing
    # each write their own); taking the first would read Warmup's zero.
    import re as _re
    matches = _re.findall(r"(\d+)\s+Warning;\s+(\d+)\s+Severe", text)
    n_warning = int(matches[-1][0]) if matches else text.count("** Warning **")

    if n_fatal or unexplained > 0:
        raise RuntimeError(
            f"EnergyPlus produced {unexplained} unexplained Severe / {n_fatal} Fatal "
            f"errors - results invalid. See {err_path}")
    return {
        "warnings": n_warning,
        "severes": n_severe,
        "severes_benign_shading_ems": benign,
        "severes_unexplained": max(unexplained, 0),
        "fatals": n_fatal,
    }


# ---------------------------------------------------------------------------
# Occupancy resolution
# ---------------------------------------------------------------------------
# The cadastre occasionally records a head count that cannot be reconciled with
# the recorded floor area - e.g. 9525403YJ2792F: 26 residents, 2 dwellings and
# altura_max=1 over a 148 m2 footprint, i.e. 5.7 m2/person and 13 people per
# dwelling.  Such a building still simulates cleanly (DHW scales with the head
# count and reached 51 % of its total energy), so nothing fails - the result is
# just not believable.  These bounds only FLAG the run; they never block it.
OCCUPANCY_DENSITY_MIN_M2 = 15.0     # denser than this is implausible for housing
OCCUPANCY_DENSITY_MAX_M2 = 250.0    # sparser than this is effectively unoccupied


def cap_occupants_to_density_floor(occupants: float, res_area_m2: float) -> float:
    """Cap a head count that cannot physically fit in the recorded floor area.

    ``pob_total`` is the padron - a municipal *registration*, not a measurement
    of who sleeps there.  In Spain registration is what unlocks healthcare and
    residency paperwork, so addresses in poor, immigrant-dense districts
    accumulate registrations far beyond their capacity.  The effect is sharply
    localised: of the 255 residential buildings below the density floor, 155
    sit in Poblats Maritims alone, and their profile is consistent - built
    around 1932, one storey, two dwellings, 4.2 people per dwelling against a
    stock median of 1.90.

    Cross-checking one of them against Tipo15 shows the geometry is fine and
    the head count is the outlier: 9525403YJ2792F has exactly two dwellings on
    planta 1 (62 + 63 m2) and 26 registered residents.

    Capping at OCCUPANCY_DENSITY_MIN_M2 is a habitability argument, not a
    measurement: below it a dwelling is overcrowded by any standard.  The cap
    keeps the "this building is crowded" signal while removing the impossible
    part, and it moves the city head count by only -0.21 %.  The original value
    and the verdict are always recorded, so the assumption stays visible.
    """
    if occupants <= 0 or res_area_m2 <= 0:
        return occupants
    return min(occupants, res_area_m2 / OCCUPANCY_DENSITY_MIN_M2)


def occupancy_plausibility(occupants: float, res_area_m2: float,
                           dwellings: int | None = None,
                           *, capped_from: float | None = None) -> dict:
    """Judge whether the cadastre head count fits the recorded floor area.

    Never blocks a run.  ``capped_from`` records the pre-cap head count when
    ``cap_occupants_to_density_floor`` has already been applied.
    """
    if occupants <= 0:
        return {"status": "unoccupied", "m2_per_person": None,
                "people_per_dwelling": 0.0 if dwellings else None,
                "note": "no registered residents; DHW and occupant gains are zero"}

    density = res_area_m2 / occupants
    per_dwelling = (occupants / dwellings) if dwellings else None
    if capped_from is not None and capped_from > occupants:
        return {"status": "implausible_dense_capped",
                "m2_per_person": round(density, 2),
                "people_per_dwelling": round(per_dwelling, 2) if per_dwelling else None,
                "padron_occupants": round(capped_from, 1),
                "capped_to": round(occupants, 1),
                "note": (f"padron records {capped_from:.0f} residents "
                         f"({res_area_m2 / capped_from:.1f} m2/person); capped to the "
                         f"{OCCUPANCY_DENSITY_MIN_M2:.0f} m2/person habitability floor")}
    if density < OCCUPANCY_DENSITY_MIN_M2:
        status, note = "implausible_dense", (
            f"{density:.1f} m2/person is below the {OCCUPANCY_DENSITY_MIN_M2:.0f} m2 "
            "floor - check altura_max, a collective residence, or a misassigned "
            "population record")
    elif density > OCCUPANCY_DENSITY_MAX_M2:
        status, note = "implausible_sparse", (
            f"{density:.1f} m2/person is above the {OCCUPANCY_DENSITY_MAX_M2:.0f} m2 "
            "ceiling - the building is nearly empty on paper")
    else:
        status, note = "plausible", ""
    return {"status": status, "m2_per_person": round(density, 2),
            "people_per_dwelling": round(per_dwelling, 2) if per_dwelling else None,
            "note": note}


def resolve_occupants(pob_total, dwellings, cluster_people_per_dwelling: float | None,
                      zero_policy: ZeroPolicy) -> tuple[float, str]:
    """Return (occupants, source) honouring the zero-population policy.

    12.2 % of the residential stock (3,229 buildings) has dwellings but no
    registered residents.  Both policies are meant to be run and compared, so
    the applied one is always recorded in the run metadata.
    """
    people = float(pob_total or 0.0)
    if people > 0:
        return people, "cadastre.pob_total"
    if zero_policy == "literal_zero":
        return 0.0, "cadastre.pob_total(zero)"
    if cluster_people_per_dwelling is None or not dwellings:
        raise ValueError(
            "cluster_median_impute needs both a cluster median people/dwelling "
            "and a dwelling count")
    return (float(dwellings) * float(cluster_people_per_dwelling),
            "imputed.cluster_median_per_dwelling")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def config_for_building(row, base=None):
    """Resolve the period envelope for this building's TABULA cluster.

    Until 2026-07-30 the deep chain handed `DEFAULT_BUILD_CONFIG` to every
    building.  With `wall_u`/`roof_u` unset the frozen builder falls back to
    1.33 W/m2K - BlocPluriP04's own value - and to the template's uninsulated
    flat roof, so all 21 clusters came out with an identical envelope and
    differed only in geometry and occupancy.  Measured on the stage-1 ledger:
    21 representatives, 1 distinct wall, 1 distinct roof.

    `mc.config_for_profile` already holds the IVE/TABULA table; it was only ever
    called from the Workbench.  This is the stock chain's call site.

    One deliberate override: `config_for_profile` sets
    `ground_unconditioned=False` for VivUni, on the reasoning that a detached
    house has no commercial ground floor.  The deep chain applies Rai's ground
    regime *after* the build and needs that space to exist, and the decision on
    2026-07-30 was to keep Rai's regime for every typology - there is no
    comparability cost, because his 13 published `ConsumE` constants contain no
    VivUni entry at all.
    """
    base = base or mb.DEFAULT_BUILD_CONFIG
    # A caller that has already pinned the envelope means it: the Rai reference
    # replica builds a synthetic row with no cluster at all and states his
    # measured U-values directly, and overriding those would break the very
    # verification this module exists for.
    if base.envelope.wall_u is not None and base.envelope.roof_u is not None:
        return base

    cluster = str(row.get("cluster") or "").strip()
    if not cluster:
        raise KeyError(
            "building has no cluster and the caller did not pin wall_u/roof_u; "
            "refusing to fall back to a default envelope silently")
    config = mc.config_for_profile(base, f"tabula_{cluster}")
    config.geometry.ground_unconditioned = True
    return config


def build_deep_model(row, party_geom, occupants: float, *, config=None,
                     neighbors=None, litres_per_person_day: float | None = None,
                     glaze_ground: bool = True, window_frames: bool = True,
                     climate=None, ground_use: str | None = None):
    """Build the geometry/envelope with the frozen builder, then add the layers.

    `climate` is a `climate.ClimateSet`.  Left unset, every climate-dependent
    value stays at the verified Valencia one and the weather file the frozen
    builder embedded is kept - that is, the historical behaviour, byte for byte.

    `ground_use` decides what the ground storey IS: "terciario" (Rai's regime -
    conditioned commercial buffer, out of the area basis) or "residential" (a
    dwelling floor, in the basis).  Left unset it is read from the row - a
    prepared stock carries the policy-resolved `ground_use` column - and falls
    back to "terciario", the historical behaviour byte for byte.
    """
    # The cluster envelope is resolved here, on top of whatever template/weather
    # config the caller supplied, so a `--template` or `--climate` override never
    # costs a building its period-correct walls and roof.
    build_config = config_for_building(row, base=config)
    if not build_config.geometry.ground_unconditioned:
        raise ValueError(
            "the ground regime is applied post-build and needs the ground "
            "space to exist: build with ground_unconditioned=True")

    if ground_use is None:
        candidate = row.get("ground_use") if hasattr(row, "get") else None
        ground_use = candidate if isinstance(candidate, str) and candidate else "terciario"
    if ground_use not in ("terciario", "residential"):
        raise ValueError(f"unknown ground_use {ground_use!r}: "
                         "expected 'terciario' or 'residential'")
    ground_is_residential = ground_use == "residential"

    # The frame grows outwards from the sub-surface polygon, so the WWR handed
    # to the frozen builder has to be shrunk first - otherwise the finished
    # opening overshoots the typological target by about a quarter.
    glass_fraction = 1.0
    if window_frames:
        glass_fraction = glass_fraction_for_frames(build_config)
        build_config = build_config.model_copy(deep=True)
        for side in ("north", "east", "south", "west"):
            field = f"wwr_{side}"
            setattr(build_config.openings, field,
                    getattr(build_config.openings, field) * glass_fraction)

    # Storeys the cadastre cannot fill are cut from the geometry before it is
    # extruded, not merely re-typed afterwards.  On a parcel that covers a whole
    # block the two inputs describe different things - the footprint is the
    # block's, `altura_max` is its tallest point - so their product is floor
    # area that does not exist.  3748901YJ2734H is 17,272 m2 at 15 storeys
    # against 38,158 m2 of recorded dwellings across 342 flats: 259,000 m2
    # simulated, 5.6 % of Benicalap's energy in one building.  Re-typing that
    # floor to Terciario left it lit, heated and conditioned all the same.
    #
    # The frozen builder reads its storey count from the row rather than from a
    # constant, so the cap travels on a copy of the row and the module itself is
    # untouched.  Neighbour shading is unaffected: `resolve_neighbour_source`
    # reads the surrounding mass from a separate file and every neighbour brings
    # its own `altura_max`, so trimming the target cannot change what it stands
    # among.  Without a Tipo15 area nothing is cut - the single-building CLI
    # path reads the raw GIS row and stays byte for byte as it was.
    built_storeys_raw = int(row["altura_max"]) + (1 if ground_is_residential else 0)
    footprint_pre_m2 = mb.prepare_footprint(
        mb.clean_polygon(row.geometry), config=build_config)[1]
    capped_storeys = residential_storeys_from_cadastre(
        row.get("tipo15_res_area_m2"), footprint_pre_m2, built_storeys_raw)
    # The builder counts residential storeys ABOVE the bajo; with a residential
    # ground the kept total includes it.  One storey is the smallest model it can
    # extrude, so a two-level building whose cadastre supports one cannot shrink;
    # the remainder is re-typed downstream as before, and the flag says the
    # geometry DID NOT change, because it did not.  Reporting "capped" whenever
    # the rule merely bound would overstate how many buildings this touched.
    capped_altura_max = max(1, capped_storeys - (1 if ground_is_residential else 0))
    storey_cap_applied = capped_altura_max < int(row["altura_max"])
    if storey_cap_applied:
        row = row.copy()
        row["altura_max"] = capped_altura_max

    result = mb.build_model_with_config(row, party_geom, build_config, neighbors=neighbors)
    osm, stats = result.osm, dict(result.stats)
    # On the builder's own definition, so it reads against n_floors_residential
    # directly: residential storeys above the bajo, before the cap.
    stats["built_storeys"] = built_storeys_raw - (1 if ground_is_residential else 0)
    stats["storey_cap_applied"] = storey_cap_applied

    res_area_m2 = float(stats["res_area_m2"])
    footprint_m2 = float(stats["footprint_m2"])
    n_res = int(stats["n_floors_residential"])

    if ground_is_residential:
        # The bajo is dwelling area, so it enters the basis BEFORE the density
        # cap and the occupancy density are computed from it.
        res_area_m2 = round(footprint_m2 * (n_res + 1), 1)
        stats["res_area_m2"] = res_area_m2

    # The same rule, now on what was actually extruded.  After the geometric cap
    # above this normally finds nothing left to do; it still runs because the
    # cap has a floor of one storey, and because a caller that reaches this
    # function with an already-short building must get the same answer either
    # way.  The area basis follows the conversion, so it settles before the
    # density cap.
    built_res_storeys = n_res + (1 if ground_is_residential else 0)
    cadastral_area = row.get("tipo15_res_area_m2")
    try:
        has_cadastral = float(cadastral_area) > 0
    except (TypeError, ValueError):
        has_cadastral = False
    keep_res_storeys = residential_storeys_from_cadastre(
        cadastral_area, footprint_m2, built_res_storeys)
    if keep_res_storeys < built_res_storeys:
        res_area_m2 = round(footprint_m2 * keep_res_storeys, 1)
        stats["res_area_m2"] = res_area_m2
    # `n_floors_residential` keeps the builder's meaning - residential storeys
    # ABOVE the bajo - because the ledger, the tests and every past run are on
    # that definition.  The count actually simulated as housing travels in its
    # own field.  "unchecked" and "0 converted" are different outcomes and must
    # not look alike: the single-building CLI reads the raw GIS row, which
    # carries no Tipo15 area, so no mixed-use check is possible there.
    stats["residential_storeys_effective"] = keep_res_storeys
    stats["mixed_use_storeys_converted"] = built_res_storeys - keep_res_storeys
    stats["mixed_use_basis"] = "cadastral_tipo15" if has_cadastral else "unchecked_no_tipo15"

    # A padron head count that cannot fit the recorded floor area is capped
    # before it reaches the model, so DHW and occupant gains stay physical.
    padron_occupants = occupants
    occupants = cap_occupants_to_density_floor(occupants, res_area_m2)

    if ground_is_residential:
        # Order matters: glazing looks the ground space up through its buffer
        # space type, and occupancy/PTHP resolve through the residential space
        # type - so glaze first, re-type second, occupancy third.
        layers = {}
        if glaze_ground:
            layers["ground_glazing"] = glaze_ground_floor(osm, config=build_config)
            stats["window_area_m2"] = round(
                stats["window_area_m2"] + layers["ground_glazing"]["ground_glass_area_m2"], 1)
            stats["n_windows"] += layers["ground_glazing"]["ground_windows"]
        layers["ground"] = apply_residential_ground(osm)
        layers["mixed_use"] = apply_mixed_use_storeys(osm, keep_res_storeys)
        layers["occupancy"] = apply_real_occupancy(osm, occupants, res_area_m2)
    else:
        # Mixed-use re-typing first: occupancy and the PTHP COP split both
        # resolve through the residential space type, so a storey converted
        # after them would keep dwelling occupants and a dwelling heat pump.
        layers = {"mixed_use": apply_mixed_use_storeys(osm, keep_res_storeys)}
        layers["occupancy"] = apply_real_occupancy(osm, occupants, res_area_m2)
        layers["ground"] = apply_rai_ground_regime(osm)
    stats["ground_use"] = ground_use
    plausibility = occupancy_plausibility(
        occupants, res_area_m2, row.get("num_vivend"), capped_from=padron_occupants)
    layers["occupancy"]["plausibility"] = plausibility
    layers["occupancy"]["padron_occupants"] = round(padron_occupants, 1)
    stats["occupancy_plausibility"] = plausibility["status"]
    stats["padron_occupants"] = round(padron_occupants, 1)
    # The number the model was actually built with, after the density cap.  The
    # ledger used to carry only the padron figure, so a capped building looked
    # as if it had been simulated with its full registered head count.
    stats["occupants_applied"] = round(occupants, 1)
    if glaze_ground and not ground_is_residential:
        layers["ground_glazing"] = glaze_ground_floor(osm, config=build_config)
        stats["window_area_m2"] = round(
            stats["window_area_m2"] + layers["ground_glazing"]["ground_glass_area_m2"], 1)
        stats["n_windows"] += layers["ground_glazing"]["ground_windows"]
    if window_frames:
        frames = apply_window_frames(osm)
        frames["wwr_rescaled_by"] = round(glass_fraction, 4)
        layers["window_frames"] = frames
        # EnergyPlus reports the OPENING (glass + frame) in its fenestration
        # summary, and the frozen QA cross-check compares against
        # stats["window_area_m2"].  Keep that field on the same basis and record
        # the glass separately.
        stats["window_glass_area_m2"] = frames["glass_area_m2"]
        stats["window_area_m2"] = frames["opening_area_m2"]
    # After every re-typing layer, so it meters the space types the model
    # finished with rather than the ones it started from.
    layers["terciario_metering"] = tag_terciario_end_uses(osm)
    layers["dhw"] = add_dhw_loop(osm, occupants,
                                 litres_per_person_day=litres_per_person_day,
                                 climate=climate)
    layers["hvac"] = add_pthp_hvac(osm, climate=climate)
    if climate is not None:
        # the weather file the frozen builder embedded is replaced here, in the
        # same post-build pattern as every other layer
        layers["weather"] = mb.set_weather_file(osm, climate.epw_path)
        layers["weather"]["climate"] = climate.name
        layers["weather"]["climate_fingerprint"] = climate.fingerprint
    # One well-mixed zone per storey is the chain's standing assumption; on a
    # deep plan it is a weaker one.  Recorded per building so an aggregate can
    # state how much of a total rests on it instead of hiding the mix.
    single_zone_caveat = footprint_m2 > LARGE_FOOTPRINT_SINGLE_ZONE_M2
    stats["large_footprint_single_zone"] = single_zone_caveat
    layers["zoning"] = {
        "scheme": "one_well_mixed_zone_per_storey",
        "footprint_m2": round(footprint_m2, 1),
        "single_zone_threshold_m2": LARGE_FOOTPRINT_SINGLE_ZONE_M2,
        "large_footprint_single_zone": single_zone_caveat,
        "note": ("no core/perimeter split: above the threshold the zone averages "
                 "an internally-driven core with an envelope-driven perimeter"),
    }
    stats["deep_layers"] = layers
    stats["total_conditioned_area_m2"] = round(footprint_m2 * (n_res + 1), 1)
    # How much more floor the model conditions than the cadastre records as
    # dwellings.  Rai's own regime is one commercial storey under the flats, so
    # a value near (n+1)/n is his; far above it means the prism still carries
    # floor the cadastre never accounted for.
    try:
        cadastral = float(row.get("tipo15_res_area_m2"))
    except (TypeError, ValueError):
        cadastral = 0.0
    stats["conditioned_to_cadastral_ratio"] = (
        round(stats["total_conditioned_area_m2"] / cadastral, 3) if cadastral > 0 else None)
    return osm, stats


def load_building_row(refparcela: str, gis_path: Path | None = None):
    """Read exactly one cadastre building, with the columns the layers need."""
    import geopandas as gpd

    path = Path(gis_path or mb.NEIGHBORS_SHP)
    escaped = str(refparcela).replace("'", "''")
    frame = gpd.read_file(path, where=f"refparcela = '{escaped}'")
    if frame.empty:
        raise ValueError(f"refparcela {refparcela!r} not found in {path}")
    if len(frame) != 1:
        raise ValueError(
            f"refparcela {refparcela!r} has {len(frame)} rows; the deep pipeline "
            "needs an explicit multipart decision first")
    return frame.iloc[0]


def cluster_people_per_dwelling(cluster: str, gis_path: Path | None = None) -> float | None:
    """Median people-per-dwelling of one TABULA cluster, for the zero policy."""
    import geopandas as gpd
    import pandas as pd

    path = Path(gis_path or mb.NEIGHBORS_SHP)
    frame = gpd.read_file(path, columns=["cluster", "pob_total", "num_vivend"],
                          ignore_geometry=True)
    people = pd.to_numeric(frame["pob_total"], errors="coerce")
    dwellings = pd.to_numeric(frame["num_vivend"], errors="coerce")
    mask = (frame["cluster"].astype(str) == str(cluster)) & (people > 0) & (dwellings > 0)
    if not mask.any():
        return None
    return float((people[mask] / dwellings[mask]).median())


def resolve_neighbour_source(gis_path: Path | None,
                             neighbors_path: Path | None) -> Path:
    """Pick the GIS file the shading/party-wall context is read from.

    Kept separate from the target building's own source on purpose.  A stock run
    hands the target a *prepared* file (invalid `altura_max` imputed) so the
    building can be modelled at all, but the context has to keep reading the raw
    cadastre: `model_builder` skips neighbours with `altura_max < 1` when it
    builds context shading, so an imputed file would suddenly make ~1 600
    previously shadeless buildings cast shadows and silently move every result.

    Passing nothing reproduces the historical behaviour exactly.
    """
    return Path(neighbors_path or gis_path or mb.NEIGHBORS_SHP)


def simulate_deep_building(refparcela: str, out_dir: Path, *,
                           zero_policy: ZeroPolicy = "literal_zero",
                           litres_per_person_day: float | None = None,
                           gis_path: Path | None = None,
                           neighbors_path: Path | None = None,
                           provenance: dict | None = None,
                           climate=None, config=None) -> tuple[dict, bool]:
    """Full deep chain for ONE building: model -> layers -> E+ -> results -> QA.

    Mirrors ``sim.simulate_building`` but always runs the real PTHP system, so
    the numbers are consumption, not ideal-loads demand.

    `neighbors_path` overrides where the shading/party-wall context is read
    from; see ``resolve_neighbour_source``.  Leaving it unset keeps the target
    and the context on the same file, which is the historical behaviour.

    `climate` swaps the weather file together with everything sized from it;
    `config` swaps the template and geometry settings.  Both default to the
    verified Valencia set-up.
    """
    import shutil

    row = load_building_row(refparcela, gis_path=gis_path)
    log.info("===== DEEP BUILDING: %s =====", refparcela)

    median_ppd = None
    if zero_policy == "cluster_median_impute":
        median_ppd = cluster_people_per_dwelling(row.get("cluster"), gis_path=gis_path)
    occupants, occupants_source = resolve_occupants(
        row.get("pob_total"), row.get("num_vivend"), median_ppd, zero_policy)
    log.info("[occupancy] %s people (source: %s)", occupants, occupants_source)

    geom = mb.clean_polygon(row.geometry)
    context_path = resolve_neighbour_source(gis_path, neighbors_path)
    neighbors = mb.load_neighbors(geom, refparcela, context_path)
    party = mb.find_party_walls(geom, refparcela, context_path, neighbors=neighbors)

    osm, stats = build_deep_model(row, party, occupants, neighbors=neighbors,
                                  litres_per_person_day=litres_per_person_day,
                                  climate=climate, config=config)
    stats["occupants_source"] = occupants_source
    stats["zero_policy"] = zero_policy
    stats["climate"] = climate.name if climate is not None else "valencia_iwec (built in)"
    stats["climate_fingerprint"] = climate.fingerprint if climate is not None else None
    # Reporting only: the cadastral net area from Tipo15, carried alongside the
    # geometric area the simulation actually runs on.  Never used as a
    # denominator here - see the note in stock_runner.prepare_stock_file.
    tipo15 = row.get("tipo15_res_area_m2")
    stats["tipo15_res_area_m2"] = (round(float(tipo15), 1)
                                   if tipo15 is not None and float(tipo15) > 0 else None)
    # Which rule decided the ground use (tipo15 / family_fallback / forced) -
    # stamped by the stock policy into the prepared file; absent on raw cadastre.
    source = row.get("ground_use_source")
    stats["ground_use_source"] = source if isinstance(source, str) and source else None
    # Not `area_basis`: `read_end_uses_split` already returns a field of that
    # name and merges after stats, so it would silently shadow this one.
    stats["res_area_source"] = "geometry: footprint x residential storeys"
    stats["target_gis_path"] = str(gis_path) if gis_path else str(mb.NEIGHBORS_SHP)
    stats["context_gis_path"] = str(context_path)
    layers = stats["deep_layers"]
    if stats["occupancy_plausibility"].startswith("implausible"):
        log.warning("[occupancy] %s: %s", stats["occupancy_plausibility"],
                    layers["occupancy"]["plausibility"]["note"])
    log.info("[model] %s m² × %s storeys | %s m²/person | DHW %s l/day | PTHP COP %s/%s",
             stats["footprint_m2"], stats["n_floors_total"],
             layers["occupancy"]["m2_per_person"], layers["dhw"]["dhw_litres_per_day"],
             layers["hvac"]["heating_cop"], layers["hvac"]["cooling_cop"])

    run_dir = out_dir / f"{refparcela}_deep"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    sql_path = sim.run_energyplus(
        osm, run_dir, epw_path=climate.epw_path if climate is not None else None)

    err_stats = scan_err_deep(run_dir)
    log.info("[err] %s warning | %s severe (%s benign shading-EMS, %s unexplained) | %s fatal",
             err_stats["warnings"], err_stats["severes"],
             err_stats["severes_benign_shading_ems"],
             err_stats["severes_unexplained"], err_stats["fatals"])

    res = read_end_uses_split(sql_path, stats["res_area_m2"],
                              stats["total_conditioned_area_m2"])
    log.info("[result] space heating %s | cooling %s | DHW %s | total site %s kWh/m²",
             res["space_heating_kwh_m2"], res["cooling_kwh_m2"],
             res["dhw_kwh_m2"], res["total_site_kwh_m2"])
    log.info("[result] total site on conditioned basis: %s kWh/m² (DHW share %s%%)",
             res["total_site_kwh_m2_conditioned"], res["dhw_share_pct"])

    checks = (sim.crosscheck_energyplus(sql_path, stats,
                                        unmet_max=sim.QA_UNMET_HOURS_MAX_HVAC)
              + sim.check_plausibility_cons({"total_site_kwh_m2": res["total_site_kwh_m2"]}))
    carbon = sim.carbon_from_enduses(
        {"cons_heating_gas_kwh_m2": res["space_heating_gas_kwh_m2"],
         "cons_heating_elec_kwh_m2": res["space_heating_elec_kwh_m2"],
         "cons_cooling_kwh_m2": res["cooling_kwh_m2"],
         "cons_fans_kwh_m2": res["fans_kwh_m2"],
         "site_gas_kwh_m2": res["site_gas_kwh_m2"],
         "site_elec_kwh_m2": res["site_elec_kwh_m2"]},
        stats["res_area_m2"])
    log.info("[carbon] HVAC %s kgCO₂/m² | total site %s kgCO₂/m²",
             carbon["hvac_co2_kg_m2"], carbon["total_site_co2_kg_m2"])

    qa_passed = all(check["passed"] for check in checks)
    for check in checks:
        log.info("[qa] %-24s model=%s eplus=%s -> %s", check["check"], check["model"],
                 check["eplus"], "PASS" if check["passed"] else "FAIL")

    summary = {"refparcela": refparcela, **{k: v for k, v in stats.items()
                                            if k not in ("facade_qa", "deep_layers")},
               **res, **carbon, **err_stats,
               "qa_all_passed": qa_passed}
    if provenance:
        # the caller says which frozen model produced this run; it belongs in
        # the main record, not only in a sidecar file
        summary["verified_profile"] = provenance
    (run_dir / "deep_layers.json").write_text(
        json.dumps({"layers": layers, "results": res, "carbon": carbon,
                    "qa": checks, "summary": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return summary, qa_passed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deep single-building model: real occupancy + DHW + PTHP + Rai ground")
    parser.add_argument("--refparcela", required=True)
    parser.add_argument("--zero-policy", choices=["literal_zero", "cluster_median_impute"],
                        default="literal_zero",
                        help="what to do when the cadastre records zero residents")
    parser.add_argument("--out-dir", type=Path, default=Path("out/deep"))
    parser.add_argument("--litres-per-person-day", type=float,
                        default=DHW_LITRES_PER_PERSON_DAY,
                        help="Rai-aligned DHW demand at 50 C, ~80 %% of the CTE 60 C "
                             "energy reference (default 28 l/person/day)")
    parser.add_argument("--gis", type=Path, help="cadastre shapefile override")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    level = logging.WARNING if args.quiet else (logging.DEBUG if args.verbose else logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary, qa_passed = simulate_deep_building(
            args.refparcela, out_dir,
            zero_policy=args.zero_policy,
            litres_per_person_day=args.litres_per_person_day,
            gis_path=args.gis)
    except Exception as exc:                       # noqa: BLE001 - CLI boundary
        log.error("deep run failed: %s", exc)
        return 2

    log.info("QA: %s", "ALL PASSED" if qa_passed else "FAILED")
    return 0 if qa_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
