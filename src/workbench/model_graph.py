"""Read-only OpenStudio model graph extraction and artifact resolution."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

import openstudio


CONTEXT_SHADING_DISABLED_SCHEDULE = "Workbench Context Shading Disabled"

from workbench import db, integrity


PROJECT = Path(__file__).resolve().parents[2]


def _round(value: Any) -> float | int | bool | str | None:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 6)
    return str(value)


def _optional(value: Any) -> Any | None:
    if hasattr(value, "isNull"):
        if value.isNull():
            return None
        return value.get()
    if hasattr(value, "empty"):
        if value.empty():
            return None
        return value.get()
    return value


def _value(obj: Any, method: str) -> Any | None:
    member = getattr(obj, method, None)
    if not callable(member):
        return None
    try:
        return _round(_optional(member()))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


def _type(obj: Any) -> str:
    return str(obj.iddObjectType().valueName()).removeprefix("OS_").replace("_", " ")


def _identity(obj: Any) -> dict[str, Any]:
    return {"id": str(obj.handle()), "name": obj.nameString(), "type": _type(obj)}


def _reference(optional: Any) -> dict[str, str] | None:
    obj = _optional(optional)
    return None if obj is None else {"id": str(obj.handle()), "name": obj.nameString()}


def _cast(obj: Any, method: str) -> Any | None:
    caster = getattr(obj, method, None)
    if not callable(caster):
        return None
    try:
        return _optional(caster())
    except (AttributeError, RuntimeError, TypeError):
        return None


def _properties(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    output = {name: _value(obj, name) for name in names}
    return {key: value for key, value in output.items() if value is not None}


MATERIAL_PROPERTIES = (
    "roughness", "thickness", "conductivity", "thermalConductivity", "density",
    "specificHeat", "thermalResistance", "thermalAbsorptance", "solarAbsorptance",
    "visibleAbsorptance", "uFactor", "solarHeatGainCoefficient", "visibleTransmittance",
    "solarTransmittance", "solarTransmittanceatNormalIncidence",
    "frontSideSolarReflectanceAtNormalIncidence",
    "backSideSolarReflectanceAtNormalIncidence", "infraredTransmittance",
    "frontSideInfraredHemisphericalEmissivity", "backSideInfraredHemisphericalEmissivity",
)


def _serialize_material(material: Any) -> dict[str, Any]:
    typed = material
    # Calling an incompatible OpenStudio material downcast is not harmless:
    # AirGap -> StandardOpaqueMaterial, for example, terminates the Python
    # process in SDK 3.11. Select the one valid cast from the IDD type first.
    idd_type = material.iddObjectType().valueName()
    caster = {
        "OS_Material": "to_StandardOpaqueMaterial",
        "OS_Material_NoMass": "to_MasslessOpaqueMaterial",
        "OS_Material_AirGap": "to_AirGap",
        "OS_WindowMaterial_SimpleGlazingSystem": "to_SimpleGlazing",
        "OS_WindowMaterial_Glazing": "to_StandardGlazing",
        "OS_WindowMaterial_Blind": "to_Blind",
        "OS_WindowMaterial_Shade": "to_Shade",
    }.get(idd_type)
    if caster:
        typed = _cast(material, caster) or material
    safe_properties = {
        "OS_Material": (
            "roughness", "thickness", "thermalConductivity", "density", "specificHeat",
            "thermalAbsorptance", "solarAbsorptance", "visibleAbsorptance",
        ),
        "OS_Material_NoMass": (
            "roughness", "thermalResistance", "thermalAbsorptance",
            "solarAbsorptance", "visibleAbsorptance",
        ),
        "OS_Material_AirGap": ("thermalResistance",),
        "OS_WindowMaterial_SimpleGlazingSystem": (
            "uFactor", "solarHeatGainCoefficient", "visibleTransmittance",
        ),
        "OS_WindowMaterial_Glazing": (
            "thickness", "solarTransmittanceatNormalIncidence",
            "frontSideSolarReflectanceAtNormalIncidence",
            "backSideSolarReflectanceAtNormalIncidence", "infraredTransmittance",
            "frontSideInfraredHemisphericalEmissivity",
            "backSideInfraredHemisphericalEmissivity",
        ),
    }.get(idd_type, ())
    return _identity(material) | {"properties": _properties(typed, safe_properties)}


def _day_profile(day: Any) -> dict[str, Any]:
    points: list[dict[str, float]] = []
    try:
        points = [
            {"hour": round(float(time.totalHours()), 6), "value": round(float(value), 6)}
            for time, value in zip(day.times(), day.values(), strict=True)
        ]
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return _identity(day) | {"points": points}


def _date(optional: Any) -> dict[str, int] | None:
    date = _optional(optional)
    if date is None:
        return None
    try:
        return {"month": int(date.monthOfYear().value()), "day": int(date.dayOfMonth())}
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


def _type_limits(schedule: Any) -> dict[str, Any] | None:
    limits = _optional(schedule.scheduleTypeLimits())
    if limits is None:
        return None
    return _identity(limits) | {
        "lower": _value(limits, "lowerLimitValue"),
        "upper": _value(limits, "upperLimitValue"),
        "numeric_type": _value(limits, "numericType"),
        "unit_type": _value(limits, "unitType"),
    }


def _serialize_schedule(schedule: Any) -> dict[str, Any]:
    item = _identity(schedule) | {"type_limits": _type_limits(schedule)}
    ruleset = _cast(schedule, "to_ScheduleRuleset")
    if ruleset is not None:
        profiles: list[dict[str, Any]] = [{"role": "default", **_day_profile(ruleset.defaultDaySchedule())}]
        for role, method in (
            ("summer_design", "summerDesignDaySchedule"),
            ("winter_design", "winterDesignDaySchedule"),
            ("holiday", "holidaySchedule"),
        ):
            try:
                profiles.append({"role": role, **_day_profile(getattr(ruleset, method)())})
            except (AttributeError, RuntimeError):
                pass
        rules = []
        for rule in ruleset.scheduleRules():
            rules.append(_identity(rule) | {
                "start": _date(rule.startDate()),
                "end": _date(rule.endDate()),
                "days": [day for day, method in (
                    ("mon", "applyMonday"), ("tue", "applyTuesday"),
                    ("wed", "applyWednesday"), ("thu", "applyThursday"),
                    ("fri", "applyFriday"), ("sat", "applySaturday"),
                    ("sun", "applySunday"),
                ) if bool(getattr(rule, method)())],
                "profile": _day_profile(rule.daySchedule()),
            })
        item.update({"profiles": profiles, "rules": rules})
    else:
        constant = _cast(schedule, "to_ScheduleConstant")
        item.update({"profiles": [], "rules": [], "value": _value(constant, "value") if constant else None})
    return item


def _load_instance(instance: Any) -> dict[str, Any]:
    schedule = None
    try:
        schedule = _reference(instance.schedule())
    except (AttributeError, RuntimeError):
        pass
    definition = None
    try:
        definition = _reference(instance.definition())
    except (AttributeError, RuntimeError):
        pass
    return _identity(instance) | {"definition": definition, "schedule": schedule}


def _infiltration(item: Any) -> dict[str, Any]:
    return _identity(item) | {
        "calculation_method": _value(item, "designFlowRateCalculationMethod"),
        "design_flow_rate_m3_s": _value(item, "designFlowRate"),
        "flow_per_floor_area_m3_s_m2": _value(item, "flowperSpaceFloorArea"),
        "flow_per_exterior_area_m3_s_m2": _value(item, "flowperExteriorSurfaceArea"),
        "flow_per_exterior_wall_area_m3_s_m2": _value(item, "flowperExteriorWallArea"),
        "air_changes_per_hour": _value(item, "airChangesperHour"),
        "schedule": _reference(item.schedule()),
    }


def _outdoor_air(optional: Any) -> dict[str, Any] | None:
    item = _optional(optional)
    if item is None:
        return None
    return _identity(item) | {
        "method": _value(item, "outdoorAirMethod"),
        "flow_per_person_m3_s": _value(item, "outdoorAirFlowperPerson"),
        "flow_per_floor_area_m3_s_m2": _value(item, "outdoorAirFlowperFloorArea"),
        "flow_rate_m3_s": _value(item, "outdoorAirFlowRate"),
        "air_changes_per_hour": _value(item, "outdoorAirFlowAirChangesperHour"),
        "fraction_schedule": _reference(item.outdoorAirFlowRateFractionSchedule()),
    }


def _serialize_space_type(space_type: Any) -> dict[str, Any]:
    load_groups: tuple[tuple[str, Callable[[], Any]], ...] = (
        ("people", space_type.people), ("lights", space_type.lights),
        ("electric_equipment", space_type.electricEquipment),
        ("gas_equipment", space_type.gasEquipment),
        ("other_equipment", space_type.otherEquipment),
    )
    loads = [
        {"category": category, **_load_instance(instance)}
        for category, getter in load_groups for instance in getter()
    ]
    loads.sort(key=lambda item: (item["category"], item["name"].casefold(), item["id"]))
    aggregate = _properties(space_type, (
        "peoplePerFloorArea", "lightingPowerPerFloorArea", "electricEquipmentPowerPerFloorArea",
        "gasEquipmentPowerPerFloorArea", "hotWaterEquipmentPowerPerFloorArea",
    ))
    return _identity(space_type) | {
        "standards_building_type": _value(space_type, "standardsBuildingType"),
        "standards_space_type": _value(space_type, "standardsSpaceType"),
        "floor_area_m2": _value(space_type, "floorArea"),
        "loads": loads,
        "load_summary": aggregate,
        "infiltration": _sorted(space_type.spaceInfiltrationDesignFlowRates(), _infiltration),
        "outdoor_air": _outdoor_air(space_type.designSpecificationOutdoorAir()),
        "default_schedule_set": _reference(space_type.defaultScheduleSet()),
    }


def _serialize_space(space: Any) -> dict[str, Any]:
    return _identity(space) | {
        "floor_area_m2": _value(space, "floorArea"),
        "volume_m3": _value(space, "volume"),
        "multiplier": _value(space, "multiplier"),
        "part_of_total_floor_area": _value(space, "partofTotalFloorArea"),
        "space_type": _reference(space.spaceType()),
        "thermal_zone": _reference(space.thermalZone()),
        "story": _reference(space.buildingStory()),
    }


def _thermostat(zone: Any) -> dict[str, Any] | None:
    thermostat = _optional(zone.thermostatSetpointDualSetpoint())
    if thermostat is None:
        return None
    return _identity(thermostat) | {
        "heating_schedule": _reference(thermostat.heatingSetpointTemperatureSchedule()),
        "cooling_schedule": _reference(thermostat.coolingSetpointTemperatureSchedule()),
    }


def _serialize_zone(zone: Any) -> dict[str, Any]:
    return _identity(zone) | {
        "floor_area_m2": _value(zone, "floorArea"),
        "volume_m3": _value(zone, "airVolume"),
        "multiplier": _value(zone, "multiplier"),
        "use_ideal_air_loads": _value(zone, "useIdealAirLoads"),
        "thermostat": _thermostat(zone),
        "equipment": _sorted(zone.equipment(), _identity),
        "spaces": [_reference(space) for space in sorted(zone.spaces(), key=lambda item: (item.nameString().casefold(), str(item.handle())))],
    }


HVAC_COMPONENT_PROPERTIES = {
    "OS_Fan_ConstantVolume": {
        "capacity": ("maximumFlowRate", "isMaximumFlowRateAutosized", "m³/s"),
        "efficiency": ("fanEfficiency", None, "fraction"),
        "pressure_rise_pa": ("pressureRise", None, "Pa"),
        "motor_efficiency": ("motorEfficiency", None, "fraction"),
    },
    "OS_Fan_VariableVolume": {
        "capacity": ("maximumFlowRate", "isMaximumFlowRateAutosized", "m³/s"),
        "efficiency": ("fanEfficiency", None, "fraction"),
        "pressure_rise_pa": ("pressureRise", None, "Pa"),
        "motor_efficiency": ("motorEfficiency", None, "fraction"),
    },
    "OS_Coil_Heating_Electric": {
        "capacity": ("nominalCapacity", "isNominalCapacityAutosized", "W"),
        "efficiency": ("efficiency", None, "fraction"),
    },
    "OS_Coil_Heating_Gas": {
        "capacity": ("nominalCapacity", "isNominalCapacityAutosized", "W"),
        "efficiency": ("gasBurnerEfficiency", None, "fraction"),
    },
    "OS_Coil_Cooling_DX_SingleSpeed": {
        "capacity": ("ratedTotalCoolingCapacity", "isRatedTotalCoolingCapacityAutosized", "W"),
        "efficiency": ("ratedCOP", None, "COP"),
    },
    "OS_Coil_Heating_Water": {
        "capacity": ("ratedCapacity", "isRatedCapacityAutosized", "W"),
    },
    "OS_Coil_Cooling_Water": {
        "capacity": ("designWaterFlowRate", "isDesignWaterFlowRateAutosized", "m³/s"),
    },
    "OS_Boiler_HotWater": {
        "capacity": ("nominalCapacity", "isNominalCapacityAutosized", "W"),
        "efficiency": ("nominalThermalEfficiency", None, "fraction"),
        "design_water_flow_m3_s": ("designWaterFlowRate", "isDesignWaterFlowRateAutosized", "m³/s"),
    },
    "OS_Chiller_Electric_EIR": {
        "capacity": ("referenceCapacity", "isReferenceCapacityAutosized", "W"),
        "efficiency": ("referenceCOP", None, "COP"),
    },
    "OS_CoolingTower_SingleSpeed": {
        "capacity": ("nominalCapacity", None, "W"),
        "design_water_flow_m3_s": ("designWaterFlowRate", "isDesignWaterFlowRateAutosized", "m³/s"),
    },
    "OS_Pump_VariableSpeed": {
        "efficiency": ("motorEfficiency", None, "fraction"),
    },
}
HVAC_COMPONENT_CASTERS = {
    "OS_Fan_ConstantVolume": "to_FanConstantVolume",
    "OS_Fan_VariableVolume": "to_FanVariableVolume",
    "OS_Coil_Heating_Electric": "to_CoilHeatingElectric",
    "OS_Coil_Heating_Gas": "to_CoilHeatingGas",
    "OS_Coil_Cooling_DX_SingleSpeed": "to_CoilCoolingDXSingleSpeed",
    "OS_Coil_Heating_Water": "to_CoilHeatingWater",
    "OS_Coil_Cooling_Water": "to_CoilCoolingWater",
    "OS_Boiler_HotWater": "to_BoilerHotWater",
    "OS_Chiller_Electric_EIR": "to_ChillerElectricEIR",
    "OS_CoolingTower_SingleSpeed": "to_CoolingTowerSingleSpeed",
    "OS_Pump_VariableSpeed": "to_PumpVariableSpeed",
}


def _hvac_component(item: Any, index: int, side: str) -> dict[str, Any]:
    idd = item.iddObjectType().valueName()
    typed = _cast(item, HVAC_COMPONENT_CASTERS[idd]) if idd in HVAC_COMPONENT_CASTERS else item
    typed = typed or item
    properties = {}
    for key, (getter, autosized, unit) in HVAC_COMPONENT_PROPERTIES.get(idd, {}).items():
        properties[key] = {
            "value": _value(typed, getter),
            "autosized": bool(_value(typed, autosized)) if autosized else False,
            "unit": unit,
        }
    return _identity(item) | {
        "index": index,
        "side": side,
        "node": idd == "OS_Node",
        "editable": idd in HVAC_COMPONENT_PROPERTIES,
        "properties": properties,
    }


def _loop_summary(loop: Any) -> dict[str, Any]:
    supply = [_hvac_component(item, index, "supply") for index, item in enumerate(loop.supplyComponents())]
    demand = [_hvac_component(item, index, "demand") for index, item in enumerate(loop.demandComponents())]
    air = _cast(loop, "to_AirLoopHVAC")
    plant = _cast(loop, "to_PlantLoop")
    sizing: dict[str, Any] = {}
    zones: list[dict[str, str] | None] = []
    kind = "loop"
    if air is not None:
        kind = "air"
        zones = [_reference(zone) for zone in sorted(air.thermalZones(), key=lambda item: (item.nameString().casefold(), str(item.handle())))]
        sizing = _properties(air.sizingSystem(), (
            "typeofLoadtoSizeOn", "designOutdoorAirFlowRate", "centralHeatingDesignSupplyAirTemperature",
            "centralCoolingDesignSupplyAirTemperature", "minimumSystemAirFlowRatio",
        ))
    elif plant is not None:
        kind = "plant"
        sizing = _properties(plant.sizingPlant(), (
            "loopType", "designLoopExitTemperature", "loopDesignTemperatureDifference",
            "sizingOption", "zoneTimestepsinAveragingWindow",
        ))
    return _identity(loop) | {
        "kind": kind,
        "supply_components": supply,
        "demand_components": demand,
        "zones": zones,
        "sizing": sizing,
    }


def _simulation_settings(model: Any) -> dict[str, Any]:
    run_period = model.getRunPeriod()
    control = model.getSimulationControl()
    sizing = model.getSizingParameters()
    outputs = sorted(({
        **_identity(item),
        "variable": item.variableName(),
        "key": item.keyValue(),
        "frequency": item.reportingFrequency(),
    } for item in model.getOutputVariables()), key=lambda item: (item["variable"], item["key"], item["id"]))
    return {
        "run_period": _identity(run_period) | {
            "begin": {"month": int(run_period.getBeginMonth()), "day": int(run_period.getBeginDayOfMonth())},
            "end": {"month": int(run_period.getEndMonth()), "day": int(run_period.getEndDayOfMonth())},
        },
        "timesteps_per_hour": int(model.getTimestep().numberOfTimestepsPerHour()),
        "simulation_control": _properties(control, (
            "runSimulationforSizingPeriods", "runSimulationforWeatherFileRunPeriods",
            "doZoneSizingCalculation", "doSystemSizingCalculation", "doPlantSizingCalculation",
            "maximumNumberofWarmupDays", "minimumNumberofWarmupDays",
        )),
        "sizing": _properties(sizing, ("heatingSizingFactor", "coolingSizingFactor")),
        "design_days": [_identity(item) for item in sorted(model.getDesignDays(), key=lambda obj: (obj.nameString(), str(obj.handle())))],
        "output_variables": outputs,
    }


def _sorted(objects: Any, serializer: Callable[[Any], dict[str, Any]]) -> list[dict[str, Any]]:
    return [serializer(obj) for obj in sorted(objects, key=lambda item: (item.nameString().casefold(), str(item.handle())))]


def _active_construction(constructions: list[dict[str, Any]], surface_types: tuple[str, ...],
                         preferred_name: str | None = None) -> dict[str, Any] | None:
    candidates = []
    for item in constructions:
        usage = item["surface_usage"]["surface_types"]
        count = sum(int(usage.get(surface_type, 0)) for surface_type in surface_types)
        if count:
            preferred = bool(preferred_name and preferred_name.casefold() in item["name"].casefold())
            candidates.append((preferred, count, float(item["surface_usage"]["area_m2"]), item))
    if not candidates:
        return None
    return max(candidates, key=lambda value: (value[0], value[1], value[2], value[3]["name"]))[3]


def _parameter_binding(item: dict[str, Any] | None, property_path: str, *,
                       object_type: str | None = None, status: str = "bound") -> dict[str, Any]:
    return {
        "status": status,
        "object_id": item.get("id") if item else None,
        "object_name": item.get("name") if item else None,
        "object_type": object_type or (item.get("type") if item else None),
        "property_path": property_path,
    }


def _project_parameter(key: str, current_value: Any, unit: str | None,
                       warn_bounds: tuple[float, float] | None, binding: dict[str, Any],
                       *, value_kind: str = "number", evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": f"project:{key}",
        "name": key,
        "type": "Project Parameter",
        "key": key,
        "current_value": current_value,
        "unit": unit,
        "value_kind": value_kind,
        "warn_bounds": None if warn_bounds is None else {"minimum": warn_bounds[0], "maximum": warn_bounds[1]},
        "binding": binding,
        "evidence": evidence or {},
        "read_only": True,
    }


def _project_parameters(model: Any, constructions: list[dict[str, Any]],
                        materials: list[dict[str, Any]], spaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map the project's LHS knobs to the exact PlantillaOS-derived objects in this OSM."""
    material_by_id = {item["id"]: item for item in materials}
    wall = _active_construction(constructions, ("Wall",), "muro ive")
    roof = _active_construction(constructions, ("RoofCeiling",), "cubierta")
    window = _active_construction(constructions, ("FixedWindow", "OperableWindow", "GlassDoor"))
    window_material = None
    if window and window["layers"]:
        window_material = material_by_id.get(window["layers"][0]["id"])

    wall_value = wall.get("u_factor_w_m2k") if wall else None
    thermal_bridge = None
    if wall:
        base_match = re.search(r"U_base\s*=\s*([0-9.]+)", wall["name"], re.IGNORECASE)
        bridge_match = re.search(r"dU\s*=\s*([0-9.]+)", wall["name"], re.IGNORECASE)
        if bridge_match:
            thermal_bridge = round(float(bridge_match.group(1)), 6)
        if base_match:
            # BuildConfig/LHS expose the base wall U and the thermal-bridge
            # increment as independent knobs; do not fold dU into wall_u.
            wall_value = round(float(base_match.group(1)), 6)

    infiltration_objects = sorted(
        model.getSpaceInfiltrationDesignFlowRates(), key=lambda item: (item.nameString().casefold(), str(item.handle()))
    )
    infiltration = next(
        (item for item in infiltration_objects if item.nameString() == "Infitracion Aire constante 0,2ACH Viv CTE"),
        None,
    )
    infiltration_item = _infiltration(infiltration) if infiltration is not None else None
    buffer_names = [
        item.nameString() for item in infiltration_objects
        if item.nameString() in {"Infitracion Aire constante 1ACH", "Infitracion Aire constante 3ACH"}
    ]

    controls = sorted(model.getShadingControls(), key=lambda item: (item.nameString().casefold(), str(item.handle())))
    shade = next((item for item in controls if "persiana" in item.nameString().casefold()), controls[0] if controls else None)
    shade_item = _identity(shade) if shade is not None else None

    site_groups = sorted(
        (item for item in model.getShadingSurfaceGroups() if item.shadingSurfaceType() == "Site"),
        key=lambda item: (item.nameString().casefold(), str(item.handle())),
    )
    context = next((item for item in site_groups if "neighbor" in item.nameString().casefold()), site_groups[0] if site_groups else None)
    context_item = _identity(context) if context is not None else None
    context_count = len(context.shadingSurfaces()) if context is not None else 0
    context_disabled_count = sum(
        1 for surface in (context.shadingSurfaces() if context is not None else [])
        if (schedule := _optional(surface.transmittanceSchedule())) is not None
        and schedule.nameString() == CONTEXT_SHADING_DISABLED_SCHEDULE
    )
    context_active_count = context_count - context_disabled_count

    wall_layer_types = [material_by_id.get(layer["id"], {}).get("type") for layer in (wall or {}).get("layers", [])]
    massless = len(wall_layer_types) == 1 and any("Massless" in str(value) for value in wall_layer_types)
    ground_space = next(
        (item for item in spaces if "ground" in item["name"].casefold() or "ground" in str(item.get("story", {}).get("name", "")).casefold()),
        spaces[0] if spaces else None,
    )
    ground_space_type = ground_space.get("space_type") if ground_space else None
    ground_unconditioned = bool(ground_space_type and "no habitable" in ground_space_type["name"].casefold())

    glazing_properties = window_material.get("properties", {}) if window_material else {}
    parameters = [
        _project_parameter(
            "wall_u", wall_value, "W/m²K", (1.2, 2.0), _parameter_binding(wall, "thermalConductance"),
            evidence={"sdk_thermal_conductance_w_m2k": wall.get("u_factor_w_m2k") if wall else None,
                      "derivation": "named U_base; thermal_bridge_du remains a separate project parameter"},
        ),
        _project_parameter("roof_u", roof.get("u_factor_w_m2k") if roof else None, "W/m²K", (1.4, 2.3),
                           _parameter_binding(roof, "thermalConductance")),
        _project_parameter("window_u", glazing_properties.get("uFactor"), "W/m²K", (4.5, 5.7),
                           _parameter_binding(window_material, "uFactor")),
        _project_parameter("window_g", glazing_properties.get("solarHeatGainCoefficient"), "SHGC", (0.70, 0.85),
                           _parameter_binding(window_material, "solarHeatGainCoefficient")),
        _project_parameter(
            "infiltration_ach", infiltration_item.get("air_changes_per_hour") if infiltration_item else None,
            "1/h", (0.1, 0.5), _parameter_binding(infiltration_item, "airChangesperHour"),
            evidence={"target_scope": "dwelling_only", "buffer_objects_untouched": buffer_names},
        ),
        _project_parameter("shade_setpoint", _value(shade, "setpoint") if shade is not None else None, "W/m²", (150.0, 400.0),
                           _parameter_binding(shade_item, "setpoint")),
        _project_parameter("thermal_bridge_du", thermal_bridge, "W/m²K", (0.0, 0.2),
                           _parameter_binding(wall, "named thermal-bridge increment")),
        _project_parameter("context_shading", context_active_count > 0, None, None,
                           _parameter_binding(context_item, "ShadingSurfaceGroup.enabled"), value_kind="boolean",
                           evidence={
                               "site_shading_surfaces": context_active_count,
                               "stored_site_shading_surfaces": context_count,
                               "disabled_site_shading_surfaces": context_disabled_count,
                           }),
        _project_parameter("massless", massless, None, None,
                           _parameter_binding(wall, "construction regime"), value_kind="boolean",
                           evidence={"active_layer_types": wall_layer_types}),
        _project_parameter("ground_unconditioned", ground_unconditioned, None, None,
                           _parameter_binding(ground_space_type, "Space.spaceType", object_type="SpaceType"),
                           value_kind="boolean", evidence={"space": _reference_dict(ground_space)}),
    ]
    for key, bounds, unit in (
        ("cop", (1.0, 3.0), "COP"), ("seer", (1.8, 3.5), "SEER"),
        ("emission_factor", (0.15, 0.331), "kgCO₂/kWh"),
    ):
        parameters.append(_project_parameter(
            key, None, unit, bounds,
            _parameter_binding(None, f"run.carbon_settings.{key}", object_type="Post-processing", status="run_setting"),
            evidence={"note": "Not stored in the OSM; supplied when simulation/carbon results are evaluated."},
        ))
    return parameters


def _reference_dict(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {key: item.get(key) for key in ("id", "name", "type")}


def extract_model_graph(osm_path: str | Path) -> dict[str, Any]:
    """Load a materialized OSM and return a deterministic, JSON-safe object graph."""
    path = Path(osm_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    translator = openstudio.osversion.VersionTranslator()
    loaded = translator.loadModel(openstudio.toPath(str(path)))
    if loaded.isNull():
        raise ValueError(f"OpenStudio could not load {path.name}")
    model = loaded.get()

    surface_usage: dict[str, dict[str, Any]] = {}
    for surface in [*model.getSurfaces(), *model.getSubSurfaces()]:
        construction = _optional(surface.construction())
        if construction is None:
            continue
        key = str(construction.handle())
        usage = surface_usage.setdefault(key, {"surface_count": 0, "area_m2": 0.0, "surface_types": {}})
        surface_type = surface.surfaceType() if hasattr(surface, "surfaceType") else surface.subSurfaceType()
        usage["surface_count"] += 1
        usage["area_m2"] += float(surface.grossArea())
        usage["surface_types"][surface_type] = usage["surface_types"].get(surface_type, 0) + 1

    def construction_item(construction: Any) -> dict[str, Any]:
        layered = _cast(construction, "to_LayeredConstruction")
        layers = list(layered.layers()) if layered is not None else []
        usage = surface_usage.get(str(construction.handle()), {"surface_count": 0, "area_m2": 0.0, "surface_types": {}})
        conductance = _value(construction, "thermalConductance")
        return _identity(construction) | {
            "layers": [_reference(layer) for layer in layers],
            "u_factor_w_m2k": conductance,
            "surface_usage": usage | {"area_m2": round(float(usage["area_m2"]), 3)},
        }

    constructions = _sorted(model.getConstructions(), construction_item)
    materials = _sorted(model.getMaterials(), _serialize_material)
    spaces = _sorted(model.getSpaces(), _serialize_space)
    graph: dict[str, Any] = {
        "schema_version": 1,
        "openstudio_version": openstudio.openStudioVersion(),
        "osm_sha256": integrity.sha256_file(path),
        "constructions": constructions,
        "materials": materials,
        "schedules": _sorted(model.getSchedules(), _serialize_schedule),
        "space_types": _sorted(model.getSpaceTypes(), _serialize_space_type),
        "spaces": spaces,
        "zones": _sorted(model.getThermalZones(), _serialize_zone),
        "hvac": {
            "zone_equipment": _sorted(
                {str(item.handle()): item for zone in model.getThermalZones() for item in zone.equipment()}.values(),
                _identity,
            ),
            "air_loops": _sorted(model.getAirLoopHVACs(), _loop_summary),
            "plant_loops": _sorted(model.getPlantLoops(), _loop_summary),
        },
        "simulation": _simulation_settings(model),
        "project_parameters": _project_parameters(model, constructions, materials, spaces),
        "project_parameter_context": {
            "base_template": "data/templates/PlantillaOS_v2.osm",
            "reference_baseline_kwh_m2": {"heating": 11.21, "cooling": 16.6},
            "cadastre_reference_kwh_m2": {"demanda_ca": 27.97, "demanda__1": 6.63},
            "cadastre_definition_status": "pending Javier Q1",
        },
    }
    graph["counts"] = {
        "constructions": len(graph["constructions"]),
        "materials": len(graph["materials"]),
        "schedules": len(graph["schedules"]),
        "space_types": len(graph["space_types"]),
        "spaces": len(graph["spaces"]),
        "zones": len(graph["zones"]),
        "hvac_equipment": len(graph["hvac"]["zone_equipment"]),
        "air_loops": len(graph["hvac"]["air_loops"]),
        "plant_loops": len(graph["hvac"]["plant_loops"]),
        "output_variables": len(graph["simulation"]["output_variables"]),
        "project_parameters": len(graph["project_parameters"]),
    }
    graph["graph_sha256"] = hashlib.sha256(
        json.dumps(graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return graph


def enrich_scene_construction_ids(osm_path: str | Path, scene: dict[str, Any]) -> dict[str, Any]:
    """Add OSM construction handles to an existing immutable scene response in memory."""
    path = Path(osm_path).expanduser().resolve()
    translator = openstudio.osversion.VersionTranslator()
    loaded = translator.loadModel(openstudio.toPath(str(path)))
    if loaded.isNull():
        raise ValueError(f"OpenStudio could not load {path.name}")
    model = loaded.get()
    mapping: dict[str, str | None] = {}
    for surface in [*model.getSurfaces(), *model.getSubSurfaces()]:
        construction = _optional(surface.construction())
        mapping[str(surface.handle())] = None if construction is None else str(construction.handle())
    enriched = json.loads(json.dumps(scene))
    for collection in ("surfaces", "subsurfaces"):
        for item in enriched.get(collection, []):
            item["construction_id"] = mapping.get(str(item.get("id")))
    return enriched


def _artifact_metadata(requested_id: str, *, root: Path, model_id: str, source_kind: str,
                       refparcela: str | None, scenario_name: str | None,
                       verification_status: str, immutable: bool) -> dict[str, Any]:
    osm_path = root / "model_python.osm"
    scene_path = root / "scene.json"
    if not osm_path.is_file():
        raise FileNotFoundError("model_python.osm")
    if not scene_path.is_file():
        raise FileNotFoundError("scene.json")
    return {
        "requested_id": requested_id,
        "model_id": model_id,
        "source_kind": source_kind,
        "refparcela": refparcela,
        "scenario_name": scenario_name,
        "verification_status": verification_status,
        "immutable": immutable,
        "osm_path": str(osm_path),
        "scene_path": str(scene_path),
        "osm_sha256": integrity.sha256_file(osm_path),
    }


def resolve_model_artifact(model_id: str) -> dict[str, Any]:
    """Resolve a preview, model run, or child run to its materialized parent OSM."""
    requested_id = model_id
    run = db.get_run(model_id)
    visited: set[str] = set()
    while run is not None and run.get("run_type") not in {"model", "authored"}:
        parent_id = run.get("parent_run_id")
        if not parent_id or parent_id in visited:
            raise ValueError("Run has no materialized model parent")
        visited.add(parent_id)
        run = db.get_run(parent_id)
    if run is not None:
        root = Path(run["artifact_dir"])
        item = _artifact_metadata(
            requested_id, root=root, model_id=run["id"],
            source_kind="authored_run" if run.get("provenance") == "authored" else "committed_run",
            refparcela=run.get("refparcela"), scenario_name=run.get("scenario_name"),
            verification_status=run.get("verification_status", "UNKNOWN"), immutable=True,
        )
        expected_hash = run.get("raw_model_sha256")
        if expected_hash and item["osm_sha256"] != expected_hash:
            raise ValueError("Materialized OSM does not match its immutable run hash")
        return item

    job = db.get_job(model_id)
    if job is None:
        raise KeyError(model_id)
    if job.get("run_id"):
        return resolve_model_artifact(str(job["run_id"])) | {"requested_id": requested_id}
    if job.get("kind") != "preview" or job.get("status") != "ready" or not job.get("result_path"):
        raise ValueError("Only materialized, review-ready previews can be inspected")
    root = Path(job["result_path"])
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("Preview integrity manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for artifact in manifest.get("artifacts", []):
        path = root / str(artifact.get("name", ""))
        if not path.is_file() or integrity.sha256_file(path) != artifact.get("sha256"):
            raise ValueError("Preview artifact integrity failed")
    provenance = (job.get("payload", {}).get("config") or {}).get("provenance") or {}
    return _artifact_metadata(
        requested_id, root=root, model_id=job["id"], source_kind="preview",
        refparcela=job.get("refparcela"), scenario_name=provenance.get("scenario_name"),
        verification_status="REVIEW_READY", immutable=False,
    )


def public_artifact_metadata(artifact: dict[str, Any]) -> dict[str, Any]:
    """Remove local filesystem paths before returning artifact metadata to the browser."""
    return {key: value for key, value in artifact.items() if not key.endswith("_path")}
