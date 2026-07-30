"""Typed, expert-first OpenStudio edits for PlantillaOS-derived model artifacts.

This adapter deliberately does not import the user-owned model builder.  The
small calibration and object-targeting rules below mirror the production
pipeline while keeping browser-authored OSM artifacts provenance-distinct.
"""

from __future__ import annotations

import calendar
import math
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable

import openstudio


PROJECT_PARAMETER_BOUNDS: dict[str, tuple[float, float]] = {
    "wall_u": (1.2, 2.0),
    "roof_u": (1.4, 2.3),
    "window_u": (4.5, 5.7),
    "window_g": (0.70, 0.85),
    "infiltration_ach": (0.1, 0.5),
    "shade_setpoint": (150.0, 400.0),
    "thermal_bridge_du": (0.0, 0.2),
}
POST_PROCESSING_PARAMETERS = frozenset({"cop", "seer", "emission_factor"})
SUPPORTED_PATCHES = frozenset({
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
})
HVAC_SYSTEMS = {
    "ideal_loads": {"label": "Ideal Loads", "system_type": None},
    "ptac_gas_dx": {"label": "System Type 1 · PTAC + gas/hot-water heat + DX", "system_type": 1},
    "pthp": {"label": "System Type 2 · Packaged terminal heat pump", "system_type": 2},
    "gas_furnace": {"label": "System Type 9 · Gas warm-air furnace", "system_type": 9},
    "electric_furnace": {"label": "System Type 10 · Electric warm-air furnace", "system_type": 10},
}
HVAC_AIR_LOOP_TEMPLATES = {
    "psz_ac": {"label": "Type 3 · Packaged rooftop AC", "system_type": 3},
    "psz_hp": {"label": "Type 4 · Packaged rooftop heat pump", "system_type": 4},
    "vav_reheat_dx": {"label": "Type 5 · VAV reheat + DX + hot-water plant", "system_type": 5},
    "vav_pfp_dx": {"label": "Type 6 · VAV parallel fan-powered boxes + DX", "system_type": 6},
    "vav_chw_hw": {"label": "Type 7 · VAV + chilled/hot/condenser-water plants", "system_type": 7},
    "vav_pfp_chw": {"label": "Type 8 · VAV fan-powered boxes + chilled-water plant", "system_type": 8},
}
VALENCIA_HVAC_DESIGN_DAYS = {
    "heating": {"month": 1, "day": 21, "db": 2.6, "range": 0.0, "wb": 2.6, "day_type": "WinterDesignDay"},
    "cooling": {"month": 7, "day": 21, "db": 32.4, "range": 8.7, "wb": 22.8, "day_type": "SummerDesignDay"},
}
CONTEXT_SHADING_GROUP = "Neighbor shadow masses"
CONTEXT_SHADING_DISABLED_SCHEDULE = "Workbench Context Shading Disabled"
CONTEXT_SHADING_ORIGINAL_SCHEDULE = "workbench_context_shading_original_schedule"
HVAC_EQUIPMENT_LIBRARY = {
    "fan_constant_volume": {"label": "Constant-volume fan", "class_name": "FanConstantVolume", "loop_kinds": ["air"]},
    "fan_variable_volume": {"label": "Variable-volume fan", "class_name": "FanVariableVolume", "loop_kinds": ["air"]},
    "coil_heating_electric": {"label": "Electric heating coil", "class_name": "CoilHeatingElectric", "loop_kinds": ["air"]},
    "coil_heating_gas": {"label": "Gas heating coil", "class_name": "CoilHeatingGas", "loop_kinds": ["air"]},
    "coil_cooling_dx": {"label": "Single-speed DX cooling coil", "class_name": "CoilCoolingDXSingleSpeed", "loop_kinds": ["air"]},
    "coil_heating_water": {"label": "Hot-water heating coil", "class_name": "CoilHeatingWater", "loop_kinds": ["air"], "requires_plant": "heating"},
    "coil_cooling_water": {"label": "Chilled-water cooling coil", "class_name": "CoilCoolingWater", "loop_kinds": ["air"], "requires_plant": "cooling"},
    "pump_variable_speed": {"label": "Variable-speed pump", "class_name": "PumpVariableSpeed", "loop_kinds": ["plant"]},
    "boiler_hot_water": {"label": "Hot-water boiler", "class_name": "BoilerHotWater", "loop_kinds": ["plant"]},
    "chiller_electric_eir": {"label": "Electric EIR chiller", "class_name": "ChillerElectricEIR", "loop_kinds": ["plant"]},
    "cooling_tower_single_speed": {"label": "Single-speed cooling tower", "class_name": "CoolingTowerSingleSpeed", "loop_kinds": ["plant"]},
}

HVAC_COMPONENT_SETTINGS = {
    "OS_Fan_ConstantVolume": {"cast": "to_FanConstantVolume", "capacity": ("maximumFlowRate", "setMaximumFlowRate", "autosizeMaximumFlowRate"), "efficiency": ("fanEfficiency", "setFanEfficiency", "fraction"), "extra": {"pressure_rise_pa": ("pressureRise", "setPressureRise", "positive"), "motor_efficiency": ("motorEfficiency", "setMotorEfficiency", "fraction")}},
    "OS_Fan_VariableVolume": {"cast": "to_FanVariableVolume", "capacity": ("maximumFlowRate", "setMaximumFlowRate", "autosizeMaximumFlowRate"), "efficiency": ("fanEfficiency", "setFanEfficiency", "fraction"), "extra": {"pressure_rise_pa": ("pressureRise", "setPressureRise", "positive"), "motor_efficiency": ("motorEfficiency", "setMotorEfficiency", "fraction")}},
    "OS_Coil_Heating_Electric": {"cast": "to_CoilHeatingElectric", "capacity": ("nominalCapacity", "setNominalCapacity", "autosizeNominalCapacity"), "efficiency": ("efficiency", "setEfficiency", "fraction")},
    "OS_Coil_Heating_Gas": {"cast": "to_CoilHeatingGas", "capacity": ("nominalCapacity", "setNominalCapacity", "autosizeNominalCapacity"), "efficiency": ("gasBurnerEfficiency", "setGasBurnerEfficiency", "fraction")},
    "OS_Coil_Cooling_DX_SingleSpeed": {"cast": "to_CoilCoolingDXSingleSpeed", "capacity": ("ratedTotalCoolingCapacity", "setRatedTotalCoolingCapacity", "autosizeRatedTotalCoolingCapacity"), "efficiency": ("ratedCOP", "setRatedCOP", "positive")},
    "OS_Coil_Heating_Water": {"cast": "to_CoilHeatingWater", "capacity": ("ratedCapacity", "setRatedCapacity", "autosizeRatedCapacity")},
    "OS_Coil_Cooling_Water": {"cast": "to_CoilCoolingWater", "capacity": ("designWaterFlowRate", "setDesignWaterFlowRate", "autosizeDesignWaterFlowRate")},
    "OS_Boiler_HotWater": {"cast": "to_BoilerHotWater", "capacity": ("nominalCapacity", "setNominalCapacity", "autosizeNominalCapacity"), "efficiency": ("nominalThermalEfficiency", "setNominalThermalEfficiency", "fraction"), "extra": {"design_water_flow_m3_s": ("designWaterFlowRate", "setDesignWaterFlowRate", "positive")}},
    "OS_Chiller_Electric_EIR": {"cast": "to_ChillerElectricEIR", "capacity": ("referenceCapacity", "setReferenceCapacity", "autosizeReferenceCapacity"), "efficiency": ("referenceCOP", "setReferenceCOP", "positive")},
    "OS_CoolingTower_SingleSpeed": {"cast": "to_CoolingTowerSingleSpeed", "capacity": ("nominalCapacity", "setNominalCapacity", None), "extra": {"design_water_flow_m3_s": ("designWaterFlowRate", "setDesignWaterFlowRate", "positive")}},
    "OS_Pump_VariableSpeed": {"cast": "to_PumpVariableSpeed", "efficiency": ("motorEfficiency", "setMotorEfficiency", "fraction")},
}


class PatchRejected(ValueError):
    """An edit that would be structurally invalid or EnergyPlus-impossible."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _load_model(path: str | Path) -> openstudio.model.Model:
    source = Path(path).expanduser().resolve()
    translated = openstudio.osversion.VersionTranslator().loadModel(openstudio.toPath(str(source)))
    if translated.isNull():
        raise ValueError(f"OpenStudio could not load {source.name}")
    return translated.get()


def _save_model(model: openstudio.model.Model, path: str | Path) -> None:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not model.save(openstudio.toPath(str(destination)), True):
        raise IOError(f"OpenStudio could not save {destination.name}")


def _optional(value: Any) -> Any | None:
    if hasattr(value, "isNull") and value.isNull():
        return None
    if hasattr(value, "empty") and value.empty():
        return None
    return value.get() if hasattr(value, "get") else value


def _cast(value: Any, name: str) -> Any | None:
    method = getattr(value, name, None)
    if not callable(method):
        return None
    try:
        return _optional(method())
    except (AttributeError, RuntimeError, TypeError):
        return None


def _idd_name(value: Any) -> str:
    try:
        return str(value.iddObjectType().valueName())
    except (AttributeError, RuntimeError, TypeError):
        return ""


def _material_cast(value: Any) -> tuple[Any | None, str]:
    """Return the only safe concrete material cast for this IDD type."""
    caster = {
        "OS_Material": "to_StandardOpaqueMaterial",
        "OS_Material_NoMass": "to_MasslessOpaqueMaterial",
        "OS_Material_AirGap": "to_AirGap",
        "OS_WindowMaterial_SimpleGlazingSystem": "to_SimpleGlazing",
        "OS_WindowMaterial_Glazing": "to_StandardGlazing",
        "OS_WindowMaterial_Blind": "to_Blind",
        "OS_WindowMaterial_Shade": "to_Shade",
    }.get(_idd_name(value), "")
    return (_cast(value, caster) if caster else None), caster


def _object(model: openstudio.model.Model, handle: Any, cast: str | None = None) -> Any:
    if not isinstance(handle, str) or not handle:
        raise PatchRejected("target_required", "A stable OpenStudio object handle is required")
    try:
        optional = model.getModelObject(openstudio.toUUID(handle))
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise PatchRejected("target_invalid", f"Invalid OpenStudio handle: {handle}") from exc
    item = _optional(optional)
    if item is None:
        raise PatchRejected("target_missing", f"OpenStudio object no longer exists: {handle}")
    if cast:
        typed = _cast(item, cast)
        if typed is None:
            raise PatchRejected("target_type", f"Object {handle} is not {cast.removeprefix('to_')}")
        return typed
    return item


def _identity(item: Any) -> dict[str, str]:
    return {"id": str(item.handle()), "name": item.nameString()}


def _finite(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PatchRejected("number_required", f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise PatchRejected("number_required", f"{field} must be a finite number")
    return result


def _positive(value: Any, field: str, *, allow_zero: bool = False) -> float:
    result = _finite(value, field)
    if result < 0 if allow_zero else result <= 0:
        comparator = "non-negative" if allow_zero else "greater than zero"
        raise PatchRejected("physical_limit", f"{field} must be {comparator}")
    return result


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise PatchRejected("boolean_required", f"{field} must be true or false")
    return value


def _warning(key: str, value: float) -> list[dict[str, Any]]:
    bounds = PROJECT_PARAMETER_BOUNDS.get(key)
    if bounds is None or bounds[0] <= value <= bounds[1]:
        return []
    return [{
        "severity": "warning", "code": "outside_lhs_band",
        "message": f"{key}={value:g} is outside the LHS reference band {bounds[0]:g}–{bounds[1]:g}; the expert value was applied",
        "bounds": {"minimum": bounds[0], "maximum": bounds[1]}, "value": value,
    }]


def _layer_resistance(material: Any) -> float | None:
    typed, caster = _material_cast(material)
    if typed is not None and caster == "to_StandardOpaqueMaterial":
        return float(typed.thickness()) / float(typed.thermalConductivity())
    if typed is not None and caster in {"to_MasslessOpaqueMaterial", "to_AirGap"}:
        return float(typed.thermalResistance())
    return None


def _clone_template_material(model: openstudio.model.Model, name: str) -> Any:
    candidates = [*model.getStandardOpaqueMaterials(), *model.getAirGaps()]
    source = next((item for item in candidates if item.nameString() == name), None)
    if source is None:
        raise PatchRejected("plantilla_binding_missing", f"PlantillaOS material is missing: {name}")
    cloned = _cast(source.clone(model), "to_Material")
    if cloned is None:
        raise PatchRejected("plantilla_clone_failed", f"PlantillaOS material could not be cloned: {name}")
    return cloned


def _assemble_calibrated(model: openstudio.model.Model, name: str, target_u: float,
                         film_r: float, fixed_layers: list[Any], calibration: Any,
                         insert_at: int) -> Any:
    resistances = [_layer_resistance(layer) for layer in fixed_layers]
    if any(value is None for value in resistances):
        raise PatchRejected("layer_type", f"{name} contains a layer with no thermal resistance")
    needed = 1.0 / target_u - film_r - sum(float(value) for value in resistances)
    if needed <= 0.005:
        # A high expert-specified U-value can be physically valid even when
        # the fixed PlantillaOS layers are already too resistive. Preserve
        # that value with an explicit equivalent instead of hard-rejecting it.
        return _massless_construction(model, f"{name} (layered fallback)", target_u, film_r)
    standard = _cast(calibration, "to_StandardOpaqueMaterial")
    if standard is None:
        raise PatchRejected("calibration_layer", "The PlantillaOS calibration layer is not a standard opaque material")
    if not standard.setThickness(needed * float(standard.thermalConductivity())):
        raise PatchRejected("calibration_impossible", "The calibration thickness was rejected by OpenStudio")
    standard.setName(f"{standard.nameString()} (Calibre {standard.thickness() * 1000:.0f}mm)")
    ordered = list(fixed_layers)
    ordered.insert(insert_at, calibration)
    vector = openstudio.model.MaterialVector()
    for layer in ordered:
        vector.append(layer)
    construction = openstudio.model.Construction(model)
    construction.setName(name)
    if not construction.setLayers(vector):
        raise PatchRejected("invalid_layers", f"OpenStudio rejected the calibrated layers for {name}")
    return construction


def _build_layered_wall(model: openstudio.model.Model, target_u: float) -> Any:
    mortar = _clone_template_material(model, "Mortero de cemento referencia")
    gypsum = _clone_template_material(model, "Enlucido de yeso d < 1000_15mm")
    if target_u >= 1.5:
        calibration = _clone_template_material(model, "Ladrillo Perforado Referencia")
        return _assemble_calibrated(model, f"Muro macizo ladrillo (U_base={target_u:.2f})", target_u, 0.17, [mortar, gypsum], calibration, 1)
    perforated = _clone_template_material(model, "Ladrillo Perforado Referencia")
    airgap = _clone_template_material(model, "Camara de aire en paredes R 0.18")
    if target_u >= 1.0:
        calibration = _clone_template_material(model, "Ladrillo Hueco Referencia")
        return _assemble_calibrated(model, f"Muro IVE ladrillo (U_base={target_u:.2f})", target_u, 0.17, [mortar, perforated, airgap, gypsum], calibration, 3)
    hollow = _clone_template_material(model, "Ladrillo Doble Hueco Referencia")
    calibration = _clone_template_material(model, "Aislante Medianera Referencia B")
    return _assemble_calibrated(model, f"Muro ladrillo aislado (U_base={target_u:.2f})", target_u, 0.17, [mortar, perforated, airgap, hollow, gypsum], calibration, 3)


def _build_layered_roof(model: openstudio.model.Model, target_u: float) -> Any:
    mortar = _clone_template_material(model, "Mortero de cemento o cal para albañileria y para revoco/enlucido 1600 < d < 1800_2cm")
    gypsum = _clone_template_material(model, "Enlucido de yeso d < 1000_15mm")
    if target_u >= 3.5:
        calibration = _clone_template_material(model, "FU Entrevigado de hormigon aligerado -Canto 300 mm")
        return _assemble_calibrated(model, f"Cubierta forjado desnudo (U={target_u:.2f})", target_u, 0.14, [mortar, gypsum], calibration, 1)
    gravel = _clone_template_material(model, "Arena y grava [1700 < d < 2200]  6 cm")
    asphalt = _clone_template_material(model, "Asfalto 10mm")
    if target_u >= 1.0:
        calibration = _clone_template_material(model, "FU Entrevigado de hormigon aligerado -Canto 300 mm")
        return _assemble_calibrated(model, f"Cubierta plana ladrillo (U={target_u:.2f})", target_u, 0.14, [gravel, asphalt, mortar, gypsum], calibration, 3)
    slab = _clone_template_material(model, "FU Entrevigado de hormigon aligerado -Canto 300 mm")
    calibration = _clone_template_material(model, "Aislante Medianera Referencia B")
    return _assemble_calibrated(model, f"Cubierta plana aislada (U={target_u:.2f})", target_u, 0.14, [gravel, asphalt, mortar, slab, gypsum], calibration, 3)


def _with_delta_u(model: openstudio.model.Model, base: Any, delta_u: float,
                  film_r: float, name: str) -> Any:
    layers = list(base.layers())
    resistances = [_layer_resistance(layer) for layer in layers]
    if any(value is None for value in resistances):
        raise PatchRejected("layer_type", f"{base.nameString()} contains an unsupported thermal layer")
    total = sum(float(value) for value in resistances)
    old_u = 1.0 / (total + film_r)
    target = 1.0 / (old_u + delta_u) - film_r
    reduction = total - target
    index = max(range(len(resistances)), key=lambda item: float(resistances[item]))
    new_r = float(resistances[index]) - reduction
    if new_r <= 0.01:
        raise PatchRejected("thermal_bridge_impossible", f"ΔU={delta_u:g} leaves a non-physical calibration layer")
    vector = openstudio.model.MaterialVector()
    for layer_index, material in enumerate(layers):
        clone = _cast(material.clone(model), "to_Material")
        if clone is None:
            raise PatchRejected("plantilla_clone_failed", material.nameString())
        if layer_index == index:
            typed, caster = _material_cast(clone)
            if typed is not None and caster == "to_StandardOpaqueMaterial":
                typed.setThermalConductivity(float(typed.thickness()) / new_r)
                typed.setName(f"{typed.nameString()} + TB(dU={delta_u:.2f})")
            else:
                if typed is None or caster not in {"to_MasslessOpaqueMaterial", "to_AirGap"} or not typed.setThermalResistance(new_r):
                    raise PatchRejected("thermal_bridge_impossible", "The calibration layer could not accept ΔU")
        vector.append(clone)
    construction = openstudio.model.Construction(model)
    construction.setName(name)
    if not construction.setLayers(vector):
        raise PatchRejected("invalid_layers", "OpenStudio rejected the thermal-bridge construction")
    return construction


def _massless_construction(model: openstudio.model.Model, name: str, u_value: float,
                           film_r: float) -> Any:
    layer_r = 1.0 / u_value - film_r
    if layer_r <= 0.001:
        raise PatchRejected(
            "physical_limit",
            f"U={u_value:g} W/m²K exceeds the maximum compatible with the {film_r:g} m²K/W film resistance",
        )
    material = openstudio.model.MasslessOpaqueMaterial(model, "Rough", layer_r)
    material.setName(f"{name} material (R={layer_r:.3f})")
    construction = openstudio.model.Construction(model)
    construction.setName(name)
    if not construction.insertLayer(0, material):
        raise PatchRejected("invalid_layers", "OpenStudio rejected the massless construction")
    return construction


def _active_construction(model: openstudio.model.Model, surface_type: str) -> Any:
    candidates: dict[str, tuple[int, float, Any]] = {}
    for surface in model.getSurfaces():
        if surface.surfaceType() != surface_type or surface.outsideBoundaryCondition() != "Outdoors":
            continue
        construction = _optional(surface.construction())
        if construction is None:
            continue
        construction = _cast(construction, "to_Construction")
        if construction is None:
            continue
        key = str(construction.handle())
        count, area, _ = candidates.get(key, (0, 0.0, construction))
        candidates[key] = (count + 1, area + float(surface.grossArea()), construction)
    if not candidates:
        raise PatchRejected("plantilla_binding_missing", f"No active exterior {surface_type} construction exists")
    return max(candidates.values(), key=lambda value: (value[0], value[1]))[2]


def _named_value(construction: Any, pattern: str, fallback_film: float) -> float:
    match = re.search(pattern, construction.nameString())
    if match:
        return float(match.group(1))
    resistances = [_layer_resistance(layer) for layer in construction.layers()]
    if any(value is None for value in resistances):
        raise PatchRejected("layer_type", f"Cannot infer U-value from {construction.nameString()}")
    return 1.0 / (sum(float(value) for value in resistances) + fallback_film)


def _wall_state(model: openstudio.model.Model) -> tuple[float, float, bool]:
    wall = _active_construction(model, "Wall")
    delta = re.search(r"dU=([0-9.]+)", wall.nameString())
    du = float(delta.group(1)) if delta else 0.0
    base = _named_value(wall, r"U_base=([0-9.]+)", 0.17)
    massless = any(_idd_name(layer) == "OS_Material_NoMass" for layer in wall.layers())
    return base, du, massless


def _roof_state(model: openstudio.model.Model) -> tuple[float, bool]:
    roof = _active_construction(model, "RoofCeiling")
    value = _named_value(roof, r"(?:U|U_base)=([0-9.]+)", 0.14)
    massless = any(_idd_name(layer) == "OS_Material_NoMass" for layer in roof.layers())
    return value, massless


def _assign_exterior(model: openstudio.model.Model, surface_type: str, construction: Any) -> int:
    count = 0
    for surface in model.getSurfaces():
        if surface.surfaceType() == surface_type and surface.outsideBoundaryCondition() == "Outdoors":
            if not surface.setConstruction(construction):
                raise PatchRejected("construction_assignment", f"OpenStudio rejected {surface.nameString()}")
            count += 1
    return count


def _rebuild_wall(model: openstudio.model.Model, base_u: float, du: float, massless: bool) -> Any:
    effective = base_u + du
    if effective <= 0.05:
        raise PatchRejected("physical_limit", "Wall U + ΔU must exceed 0.05 W/m²K")
    if massless:
        construction = _massless_construction(model, f"Wall U_base={base_u:.2f}+TB(dU={du:.2f}) (authored massless)", effective, 0.17)
    else:
        base = _build_layered_wall(model, base_u)
        construction = _with_delta_u(model, base, du, 0.17, f"{base.nameString()} + TB(dU={du:.2f})") if du else base
    _assign_exterior(model, "Wall", construction)
    return construction


def _rebuild_roof(model: openstudio.model.Model, roof_u: float, massless: bool) -> Any:
    construction = _massless_construction(model, f"Roof U={roof_u:.2f} (authored massless)", roof_u, 0.14) if massless else _build_layered_roof(model, roof_u)
    _assign_exterior(model, "RoofCeiling", construction)
    return construction


def _active_simple_glazing(model: openstudio.model.Model) -> Any:
    for subsurface in model.getSubSurfaces():
        construction = _optional(subsurface.construction())
        layered = _cast(construction, "to_LayeredConstruction") if construction else None
        if layered is None:
            continue
        for layer in layered.layers():
            glazing = _cast(layer, "to_SimpleGlazing") if _idd_name(layer) == "OS_WindowMaterial_SimpleGlazingSystem" else None
            if glazing is not None:
                return glazing
    raise PatchRejected("plantilla_binding_missing", "Active SimpleGlazing material was not found")


def _project_parameter(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = patch.get("payload") or {}
    key = str(payload.get("key") or patch.get("key") or "")
    value = payload.get("value", patch.get("value"))
    if key in POST_PROCESSING_PARAMETERS:
        raise PatchRejected("run_setting", f"{key} is a run setting and must be stored by the edit session")
    if key in {"wall_u", "roof_u", "window_u", "window_g", "infiltration_ach", "shade_setpoint", "thermal_bridge_du"}:
        number = _positive(value, key, allow_zero=key == "thermal_bridge_du")
        warnings = _warning(key, number)
        if key == "wall_u":
            _, du, massless = _wall_state(model)
            target = _rebuild_wall(model, number, du, massless)
            if not massless and any(_idd_name(layer) == "OS_Material_NoMass" for layer in target.layers()):
                warnings.append({
                    "severity": "warning", "code": "layered_massless_fallback",
                    "message": f"wall_u={number:g} cannot retain the fixed PlantillaOS layers; an equivalent massless construction was applied",
                })
        elif key == "roof_u":
            _, massless = _roof_state(model)
            target = _rebuild_roof(model, number, massless)
            if not massless and any(_idd_name(layer) == "OS_Material_NoMass" for layer in target.layers()):
                warnings.append({
                    "severity": "warning", "code": "layered_massless_fallback",
                    "message": f"roof_u={number:g} cannot retain the fixed PlantillaOS layers; an equivalent massless construction was applied",
                })
        elif key in {"window_u", "window_g"}:
            if key == "window_g" and not 0.0 <= number <= 1.0:
                raise PatchRejected("physical_limit", "window_g / SHGC must be between 0 and 1")
            target = _active_simple_glazing(model)
            accepted = target.setUFactor(number) if key == "window_u" else target.setSolarHeatGainCoefficient(number)
            if not accepted:
                raise PatchRejected("sdk_rejected", f"OpenStudio rejected {key}={number:g}")
        elif key == "infiltration_ach":
            target = next((item for item in model.getSpaceInfiltrationDesignFlowRates()
                           if item.nameString() == "Infitracion Aire constante 0,2ACH Viv CTE"), None)
            if target is None:
                raise PatchRejected("plantilla_binding_missing", "Dwelling infiltration object '...Viv CTE' is missing")
            if not target.setAirChangesperHour(number):
                raise PatchRejected("sdk_rejected", f"OpenStudio rejected infiltration_ach={number:g}")
        elif key == "shade_setpoint":
            target = next((item for item in model.getShadingControls() if "Persiana check" in item.nameString()), None)
            if target is None or not target.setSetpoint(number):
                raise PatchRejected("plantilla_binding_missing", "The PlantillaOS persiana ShadingControl is unavailable")
        else:
            wall_u, _, massless = _wall_state(model)
            target = _rebuild_wall(model, wall_u, number, massless)
        return _identity(target) | {"key": key, "value": number}, warnings

    enabled = _boolean(value, key)
    if key == "massless":
        wall_u, du, _ = _wall_state(model)
        roof_u, _ = _roof_state(model)
        wall = _rebuild_wall(model, wall_u, du, enabled)
        roof = _rebuild_roof(model, roof_u, enabled)
        return {"key": key, "value": enabled, "wall": _identity(wall), "roof": _identity(roof)}, []
    if key == "ground_unconditioned":
        ground = next((space for space in model.getSpaces() if "Ground" in space.nameString()), None)
        target_name = "No habitable 1ACH" if enabled else "Vivienda CTE"
        target = next((space_type for space_type in model.getSpaceTypes() if space_type.nameString() == target_name), None)
        if ground is None or target is None or not ground.setSpaceType(target):
            raise PatchRejected("plantilla_binding_missing", f"Ground space or {target_name} SpaceType is unavailable")
        return _identity(ground) | {"key": key, "value": enabled, "space_type": _identity(target)}, []
    if key == "context_shading":
        groups = [group for group in model.getShadingSurfaceGroups()
                  if group.nameString() == CONTEXT_SHADING_GROUP]
        if enabled and not groups:
            raise PatchRejected("plantilla_binding_missing", "The PlantillaOS context shading group is unavailable")
        if not groups:
            return {
                "key": key, "value": False,
                "disabled_surfaces": 0, "restored_surfaces": 0,
            }, []
        disabled = next(
            (item for item in model.getScheduleConstants()
             if item.nameString() == CONTEXT_SHADING_DISABLED_SCHEDULE),
            None,
        )
        changed = 0
        restored = 0
        if enabled:
            for group in groups:
                for surface in group.shadingSurfaces():
                    properties = surface.additionalProperties()
                    original = _optional(properties.getFeatureAsString(CONTEXT_SHADING_ORIGINAL_SCHEDULE))
                    schedule = None
                    if original:
                        candidate = _optional(model.getModelObject(openstudio.toUUID(str(original))))
                        schedule = _cast(candidate, "to_Schedule") if candidate is not None else None
                    current = _optional(surface.transmittanceSchedule())
                    if schedule is not None:
                        surface.setTransmittanceSchedule(schedule)
                    elif current is not None and current.nameString() == CONTEXT_SHADING_DISABLED_SCHEDULE:
                        surface.resetTransmittanceSchedule()
                    else:
                        continue
                    properties.resetFeature(CONTEXT_SHADING_ORIGINAL_SCHEDULE)
                    restored += 1
            still_disabled = any(
                schedule is not None and schedule.nameString() == CONTEXT_SHADING_DISABLED_SCHEDULE
                for group in groups for surface in group.shadingSurfaces()
                if (schedule := _optional(surface.transmittanceSchedule())) is not None
            )
            if disabled is not None and not still_disabled:
                disabled.remove()
        else:
            if disabled is None:
                disabled = openstudio.model.ScheduleConstant(model)
                disabled.setName(CONTEXT_SHADING_DISABLED_SCHEDULE)
                disabled.setValue(1.0)
            for group in groups:
                for surface in group.shadingSurfaces():
                    current = _optional(surface.transmittanceSchedule())
                    if current is not None and current.nameString() != CONTEXT_SHADING_DISABLED_SCHEDULE:
                        surface.additionalProperties().setFeature(
                            CONTEXT_SHADING_ORIGINAL_SCHEDULE, str(current.handle()),
                        )
                    if current is None or current.nameString() != CONTEXT_SHADING_DISABLED_SCHEDULE:
                        if not surface.setTransmittanceSchedule(disabled):
                            raise PatchRejected("sdk_rejected", f"Context shading toggle failed for {surface.nameString()}")
                        changed += 1
        return {
            "key": key, "value": enabled,
            "disabled_surfaces": changed, "restored_surfaces": restored,
        }, []
    raise PatchRejected("unknown_project_parameter", f"Unsupported project parameter: {key}")


def _set_construction_layers(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    construction = _object(model, patch.get("target_id"), "to_Construction")
    layer_ids = (patch.get("payload") or {}).get("layer_ids")
    if not isinstance(layer_ids, list) or not layer_ids:
        raise PatchRejected("layers_required", "A construction must contain at least one material layer")
    vector = openstudio.model.MaterialVector()
    for handle in layer_ids:
        vector.append(_object(model, handle, "to_Material"))
    if not construction.setLayers(vector):
        raise PatchRejected("invalid_layers", "OpenStudio rejected this material order")
    return _identity(construction) | {"layer_ids": [str(item) for item in layer_ids]}


def _material_properties(material: Any) -> tuple[Any, dict[str, str]]:
    typed, valid_cast = _material_cast(material)
    for cast, setters in (
        ("to_StandardOpaqueMaterial", {
            "roughness": "setRoughness", "thickness": "setThickness",
            "conductivity": "setThermalConductivity", "thermalConductivity": "setThermalConductivity",
            "density": "setDensity", "specificHeat": "setSpecificHeat",
            "thermalAbsorptance": "setThermalAbsorptance", "solarAbsorptance": "setSolarAbsorptance",
            "visibleAbsorptance": "setVisibleAbsorptance",
        }),
        ("to_MasslessOpaqueMaterial", {
            "roughness": "setRoughness", "thermalResistance": "setThermalResistance",
            "thermalAbsorptance": "setThermalAbsorptance", "solarAbsorptance": "setSolarAbsorptance",
            "visibleAbsorptance": "setVisibleAbsorptance",
        }),
        ("to_AirGap", {"thermalResistance": "setThermalResistance"}),
        ("to_SimpleGlazing", {
            "uFactor": "setUFactor", "solarHeatGainCoefficient": "setSolarHeatGainCoefficient",
            "visibleTransmittance": "setVisibleTransmittance",
        }),
    ):
        if typed is not None and valid_cast == cast:
            return typed, setters
    raise PatchRejected("material_type", "This material type is not editable in Phase 1")


def _update_material(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    material = _object(model, patch.get("target_id"), "to_Material")
    typed, setters = _material_properties(material)
    payload = patch.get("payload") or {}
    properties = payload.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise PatchRejected("properties_required", "At least one material property is required")
    changed: dict[str, Any] = {}
    for key, raw in properties.items():
        setter_name = setters.get(key)
        if setter_name is None:
            raise PatchRejected("property_unsupported", f"{key} is not valid for {material.nameString()}")
        if key == "roughness":
            value: Any = str(raw)
        else:
            value = _positive(raw, key)
            if key in {"solarHeatGainCoefficient", "visibleTransmittance", "thermalAbsorptance", "solarAbsorptance", "visibleAbsorptance"} and not 0.0 <= value <= 1.0:
                raise PatchRejected("physical_limit", f"{key} must be between 0 and 1")
        accepted = getattr(typed, setter_name)(value)
        if accepted is False:
            raise PatchRejected("sdk_rejected", f"OpenStudio rejected {key}={value}")
        changed[key] = value
    if "name" in payload:
        material.setName(str(payload["name"]))
    return _identity(material) | {"properties": changed}


def _create_material(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    payload = patch.get("payload") or {}
    kind = str(payload.get("kind") or "StandardOpaqueMaterial")
    name = str(payload.get("name") or "Authored material").strip()
    if len(name) < 2:
        raise PatchRejected("name_required", "Material name must contain at least two characters")
    constructors: dict[str, Callable[[], Any]] = {
        "StandardOpaqueMaterial": lambda: openstudio.model.StandardOpaqueMaterial(model),
        "MasslessOpaqueMaterial": lambda: openstudio.model.MasslessOpaqueMaterial(model),
        "AirGap": lambda: openstudio.model.AirGap(model),
        "SimpleGlazing": lambda: openstudio.model.SimpleGlazing(model),
    }
    if kind not in constructors:
        raise PatchRejected("material_kind", f"Unsupported material kind: {kind}")
    material = constructors[kind]()
    material.setName(name)
    update = {"target_id": str(material.handle()), "payload": {"properties": payload.get("properties") or {}}}
    if update["payload"]["properties"]:
        _update_material(model, update)
    return _identity(material) | {"kind": kind}


def _delete_material(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    material = _object(model, patch.get("target_id"), "to_Material")
    used_by = [construction.nameString() for construction in model.getConstructions()
               if any(str(layer.handle()) == str(material.handle()) for layer in construction.layers())]
    if used_by:
        raise PatchRejected("material_in_use", f"Material is still used by: {', '.join(used_by[:5])}")
    identity = _identity(material)
    material.remove()
    return identity | {"deleted": True}


def _validated_points(raw: Any) -> list[tuple[float, float]]:
    if not isinstance(raw, list) or not raw:
        raise PatchRejected("schedule_points", "A day schedule needs at least one point")
    points: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise PatchRejected("schedule_points", "Every schedule point needs hour and value")
        hour = _finite(item.get("hour"), "hour")
        value = _finite(item.get("value"), "value")
        if hour <= 0 or hour > 24:
            raise PatchRejected("schedule_hour", "Schedule hours must be greater than 0 and at most 24")
        points.append((hour, value))
    if any(points[index][0] <= points[index - 1][0] for index in range(1, len(points))):
        raise PatchRejected("schedule_order", "Schedule hours must be strictly increasing")
    if abs(points[-1][0] - 24.0) > 1e-6:
        raise PatchRejected("schedule_coverage", "The final day-schedule point must end at hour 24")
    return points


def _write_day(day: Any, points: list[tuple[float, float]]) -> None:
    day.clearValues()
    for hour, value in points:
        whole = int(hour)
        minute = int(round((hour - whole) * 60))
        if minute == 60:
            whole += 1
            minute = 0
        if not day.addValue(openstudio.Time(0, whole, minute, 0), value):
            raise PatchRejected("schedule_rejected", f"OpenStudio rejected schedule point {hour:g} h")


def _update_schedule_day(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    payload = patch.get("payload") or {}
    schedule = _object(model, patch.get("target_id"), "to_ScheduleRuleset")
    profile_id = payload.get("profile_id")
    if profile_id:
        day = _object(model, profile_id, "to_ScheduleDay")
    else:
        role = str(payload.get("role") or "default")
        methods = {
            "default": "defaultDaySchedule", "summer_design": "summerDesignDaySchedule",
            "winter_design": "winterDesignDaySchedule", "holiday": "holidaySchedule",
        }
        if role not in methods:
            raise PatchRejected("schedule_role", f"Unknown schedule profile role: {role}")
        day = getattr(schedule, methods[role])()
    points = _validated_points(payload.get("points"))
    _write_day(day, points)
    return _identity(schedule) | {"profile": _identity(day), "points": [{"hour": h, "value": v} for h, v in points]}


def _calendar_date(raw: Any, field: str) -> tuple[int, int]:
    if not isinstance(raw, dict):
        raise PatchRejected("date_required", f"{field} requires month and day")
    month, day = int(raw.get("month", 0)), int(raw.get("day", 0))
    try:
        date(2024, month, day)
    except ValueError as exc:
        raise PatchRejected("date_invalid", f"Invalid {field} date") from exc
    return month, day


def _add_schedule_rule(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    schedule = _object(model, patch.get("target_id"), "to_ScheduleRuleset")
    payload = patch.get("payload") or {}
    start = _calendar_date(payload.get("start"), "start")
    end = _calendar_date(payload.get("end"), "end")
    if date(2024, *end) < date(2024, *start):
        raise PatchRejected("date_order", "Schedule-rule end date must not precede the start date")
    days = payload.get("days")
    valid_days = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
    if not isinstance(days, list) or not days or set(days) - set(valid_days):
        raise PatchRejected("rule_days", "Select at least one valid weekday")
    points = _validated_points(payload.get("points"))
    rule = openstudio.model.ScheduleRule(schedule)
    rule.setName(str(payload.get("name") or "Authored schedule rule"))
    rule.setStartDate(openstudio.Date(openstudio.MonthOfYear(start[0]), start[1]))
    rule.setEndDate(openstudio.Date(openstudio.MonthOfYear(end[0]), end[1]))
    for key, suffix in valid_days.items():
        getattr(rule, f"setApply{suffix}")(key in days)
    _write_day(rule.daySchedule(), points)
    return _identity(rule) | {"schedule": _identity(schedule)}


def _delete_schedule_rule(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    rule = _object(model, patch.get("target_id"), "to_ScheduleRule")
    identity = _identity(rule)
    rule.remove()
    return identity | {"deleted": True}


def _space_type_loads(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    space_type = _object(model, patch.get("target_id"), "to_SpaceType")
    payload = patch.get("payload") or {}
    setters = {
        "people_per_floor_area": "setPeoplePerFloorArea",
        "lighting_power_per_floor_area": "setLightingPowerPerFloorArea",
        "electric_equipment_power_per_floor_area": "setElectricEquipmentPowerPerFloorArea",
        "gas_equipment_power_per_floor_area": "setGasEquipmentPowerPerFloorArea",
    }
    changed = {}
    for key, method in setters.items():
        if key not in payload:
            continue
        value = _positive(payload[key], key, allow_zero=True)
        if getattr(space_type, method)(value) is False:
            raise PatchRejected("sdk_rejected", f"OpenStudio rejected {key}={value:g}")
        changed[key] = value
    if not changed:
        raise PatchRejected("loads_required", "At least one load density is required")
    return _identity(space_type) | {"loads": changed}


def _space_type_infiltration(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    payload = patch.get("payload") or {}
    target = _object(model, payload.get("infiltration_id") or patch.get("target_id"))
    infiltration = _cast(target, "to_SpaceInfiltrationDesignFlowRate")
    if infiltration is None:
        space_type = _cast(target, "to_SpaceType")
        values = list(space_type.spaceInfiltrationDesignFlowRates()) if space_type else []
        infiltration = values[0] if values else None
    if infiltration is None:
        raise PatchRejected("infiltration_missing", "The selected object has no DesignFlowRate infiltration")
    methods = {
        "air_changes_per_hour": "setAirChangesperHour",
        "design_flow_rate_m3_s": "setDesignFlowRate",
        "flow_per_floor_area_m3_s_m2": "setFlowperSpaceFloorArea",
        "flow_per_exterior_area_m3_s_m2": "setFlowperExteriorSurfaceArea",
        "flow_per_exterior_wall_area_m3_s_m2": "setFlowperExteriorWallArea",
    }
    selected = [(key, method) for key, method in methods.items() if key in payload]
    if len(selected) != 1:
        raise PatchRejected("infiltration_method", "Set exactly one infiltration calculation value")
    key, method = selected[0]
    value = _positive(payload[key], key, allow_zero=True)
    if getattr(infiltration, method)(value) is False:
        raise PatchRejected("sdk_rejected", f"OpenStudio rejected {key}={value:g}")
    return _identity(infiltration) | {key: value}


def _space_type_dsoa(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    space_type = _object(model, patch.get("target_id"), "to_SpaceType")
    dsoa = _optional(space_type.designSpecificationOutdoorAir())
    if dsoa is None:
        dsoa = openstudio.model.DesignSpecificationOutdoorAir(model)
        dsoa.setName(f"{space_type.nameString()} authored outdoor air")
        space_type.setDesignSpecificationOutdoorAir(dsoa)
    payload = patch.get("payload") or {}
    methods = {
        "flow_per_person_m3_s": "setOutdoorAirFlowperPerson",
        "flow_per_floor_area_m3_s_m2": "setOutdoorAirFlowperFloorArea",
        "flow_rate_m3_s": "setOutdoorAirFlowRate",
        "air_changes_per_hour": "setOutdoorAirFlowAirChangesperHour",
    }
    changed = {}
    for key, method in methods.items():
        if key in payload:
            value = _positive(payload[key], key, allow_zero=True)
            if getattr(dsoa, method)(value) is False:
                raise PatchRejected("sdk_rejected", f"OpenStudio rejected {key}={value:g}")
            changed[key] = value
    if not changed:
        raise PatchRejected("dsoa_required", "At least one outdoor-air value is required")
    return _identity(dsoa) | {"space_type": _identity(space_type), "values": changed}


def _shift_schedule(schedule: Any, delta: float, *, heating: bool) -> int:
    ruleset = _cast(schedule, "to_ScheduleRuleset")
    if ruleset is None:
        raise PatchRejected("schedule_type", f"{schedule.nameString()} is not a ScheduleRuleset")
    changed = 0
    seen: set[str] = set()
    for rule in ruleset.scheduleRules():
        day = rule.daySchedule()
        if str(day.handle()) in seen:
            continue
        seen.add(str(day.handle()))
        points = [(float(time.totalHours()), float(value)) for time, value in zip(day.times(), day.values(), strict=True)]
        shifted = []
        for hour, value in points:
            sentinel = value <= -9.0 if heating else value >= 49.0
            shifted.append((hour, value if sentinel else value + delta))
            changed += 0 if sentinel else 1
        _write_day(day, shifted)
    return changed


def _thermostat_setpoints(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    target_id = patch.get("target_id")
    if target_id == "first_conditioned_zone":
        target = next((zone for zone in model.getThermalZones()
                       if not zone.thermostatSetpointDualSetpoint().isNull()), None)
        if target is None:
            raise PatchRejected("thermostat_missing", "No conditioned zone has a dual-setpoint thermostat")
    else:
        target = _object(model, target_id)
    thermostat = _cast(target, "to_ThermostatSetpointDualSetpoint")
    if thermostat is None:
        zone = _cast(target, "to_ThermalZone")
        thermostat = _optional(zone.thermostatSetpointDualSetpoint()) if zone else None
    if thermostat is None:
        raise PatchRejected("thermostat_missing", "The selected object has no dual-setpoint thermostat")
    payload = patch.get("payload") or {}
    warnings: list[dict[str, Any]] = []
    changed: dict[str, Any] = {}
    if "heating_schedule_id" in payload:
        schedule = _object(model, payload["heating_schedule_id"], "to_Schedule")
        if not thermostat.setHeatingSetpointTemperatureSchedule(schedule):
            raise PatchRejected("sdk_rejected", "Heating schedule assignment failed")
        changed["heating_schedule"] = _identity(schedule)
    if "cooling_schedule_id" in payload:
        schedule = _object(model, payload["cooling_schedule_id"], "to_Schedule")
        if not thermostat.setCoolingSetpointTemperatureSchedule(schedule):
            raise PatchRejected("sdk_rejected", "Cooling schedule assignment failed")
        changed["cooling_schedule"] = _identity(schedule)
    for key, getter, heating in (
        ("heating_delta_c", "heatingSetpointTemperatureSchedule", True),
        ("cooling_delta_c", "coolingSetpointTemperatureSchedule", False),
    ):
        if key not in payload:
            continue
        delta = _finite(payload[key], key)
        if abs(delta) > 3:
            warnings.append({"severity": "warning", "code": "outside_comfort_reference", "message": f"{key}={delta:g} K exceeds the ±3 K project reference band"})
        if abs(delta) > 15:
            warnings.append({
                "severity": "warning", "code": "extreme_comfort_offset",
                "message": f"{key}={delta:g} K is an extreme expert override; EnergyPlus preflight still applies",
            })
        schedule = _optional(getattr(thermostat, getter)())
        if schedule is None:
            raise PatchRejected("schedule_missing", f"Thermostat has no schedule for {key}")
        changed[key] = delta
        changed[f"{key}_points"] = _shift_schedule(schedule, delta, heating=heating)
    if not changed:
        raise PatchRejected("setpoint_required", "Provide a schedule binding or heating/cooling delta")
    return _identity(thermostat) | changed, warnings


def _hvac_system(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    system = str((patch.get("payload") or {}).get("system") or "")
    if system not in HVAC_SYSTEMS:
        raise PatchRejected("hvac_system", f"Unsupported curated HVAC system: {system}")
    zones = [zone for zone in model.getThermalZones() if not zone.thermostatSetpointDualSetpoint().isNull()]
    if not zones:
        raise PatchRejected("conditioned_zones", "No thermostat-controlled zones are available")
    for loop in [*list(model.getAirLoopHVACs()), *list(model.getPlantLoops())]:
        loop.remove()
    for zone in model.getThermalZones():
        zone.setUseIdealAirLoads(False)
        for equipment in list(zone.equipment()):
            equipment.remove()
    system_type = HVAC_SYSTEMS[system]["system_type"]
    if system_type is None:
        for zone in zones:
            zone.setUseIdealAirLoads(True)
    else:
        if system_type in {1, 2}:
            getattr(openstudio.model, f"addSystemType{system_type}")(model, zones)
        else:
            generic = getattr(openstudio.model, f"addSystemType{system_type}")(model)
            loop = _cast(generic, "to_AirLoopHVAC")
            if loop is None:
                raise PatchRejected("sdk_rejected", f"OpenStudio did not create System Type {system_type}")
            for zone in zones:
                if not loop.addBranchForZone(zone):
                    loop.remove()
                    raise PatchRejected("sdk_rejected", f"OpenStudio could not connect zone {zone.nameString()}")
        _ensure_hvac_sizing(model)
    return {"system": system, "label": HVAC_SYSTEMS[system]["label"], "zones": len(zones)}


def _selected_zones(model: openstudio.model.Model, raw_ids: Any, *, default_conditioned: bool) -> list[Any]:
    if raw_ids is None and default_conditioned:
        zones = [zone for zone in model.getThermalZones() if not zone.thermostatSetpointDualSetpoint().isNull()]
    else:
        if not isinstance(raw_ids, list) or not raw_ids:
            raise PatchRejected("zone_selection", "Select at least one thermal zone")
        zones = [_object(model, item, "to_ThermalZone") for item in raw_ids]
    unique = {str(zone.handle()): zone for zone in zones}
    if not unique:
        raise PatchRejected("zone_selection", "No thermal zones are available")
    return list(unique.values())


def _detach_zone_from_air(model: openstudio.model.Model, zone: Any, *, except_loop: Any | None = None) -> None:
    zone_id = str(zone.handle())
    for loop in model.getAirLoopHVACs():
        if except_loop is not None and str(loop.handle()) == str(except_loop.handle()):
            continue
        if any(str(item.handle()) == zone_id for item in loop.thermalZones()):
            loop.removeBranchForZone(zone)


def _clear_zone_equipment(zone: Any) -> None:
    for equipment in list(zone.equipment()):
        equipment.remove()


def _ensure_hvac_sizing(model: openstudio.model.Model) -> dict[str, Any]:
    """Materialize the Valencia sizing contract required by autosized loops."""
    existing = {item.nameString() for item in model.getDesignDays()}
    created = 0
    for label, values in VALENCIA_HVAC_DESIGN_DAYS.items():
        name = f"Valencia {label} design day"
        if name in existing:
            continue
        design_day = openstudio.model.DesignDay(model)
        design_day.setName(name)
        design_day.setMaximumDryBulbTemperature(values["db"])
        design_day.setDailyDryBulbTemperatureRange(values["range"])
        design_day.setHumidityConditionType("Wetbulb")
        design_day.setWetBulbOrDewPointAtMaximumDryBulb(values["wb"])
        design_day.setMonth(values["month"])
        design_day.setDayOfMonth(values["day"])
        design_day.setDayType(values["day_type"])
        created += 1
    control = model.getSimulationControl()
    control.setDoZoneSizingCalculation(True)
    control.setDoSystemSizingCalculation(bool(model.getAirLoopHVACs()))
    control.setDoPlantSizingCalculation(bool(model.getPlantLoops()))
    control.setRunSimulationforSizingPeriods(False)
    control.setRunSimulationforWeatherFileRunPeriods(True)
    return {"design_days_created": created, "design_days": len(model.getDesignDays())}


def _air_loop_create(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = patch.get("payload") or {}
    template = str(payload.get("template") or "")
    definition = HVAC_AIR_LOOP_TEMPLATES.get(template)
    if definition is None:
        raise PatchRejected("hvac_template", f"Unsupported air-loop template: {template}")
    zones = _selected_zones(model, payload.get("zone_ids"), default_conditioned=True)
    created_plant_ids = {str(loop.handle()) for loop in model.getPlantLoops()}
    generic = getattr(openstudio.model, f"addSystemType{definition['system_type']}")(model)
    loop = _cast(generic, "to_AirLoopHVAC")
    if loop is None:
        raise PatchRejected("sdk_rejected", "OpenStudio did not create the requested AirLoopHVAC")
    name = str(payload.get("name") or definition["label"]).strip()
    if name:
        loop.setName(name[:100])
    for zone in zones:
        _detach_zone_from_air(model, zone, except_loop=loop)
        _clear_zone_equipment(zone)
        zone.setUseIdealAirLoads(False)
        if not loop.addBranchForZone(zone):
            loop.remove()
            raise PatchRejected("sdk_rejected", f"OpenStudio could not connect zone {zone.nameString()}")
    sizing = _ensure_hvac_sizing(model)
    created_plants = [item for item in model.getPlantLoops() if str(item.handle()) not in created_plant_ids]
    warnings = []
    thermostat_missing = [zone.nameString() for zone in zones if zone.thermostatSetpointDualSetpoint().isNull()]
    if thermostat_missing:
        warnings.append({
            "severity": "warning", "code": "zone_without_thermostat",
            "message": f"{len(thermostat_missing)} connected zone(s) have no dual-setpoint thermostat; the topology was kept for expert review",
        })
    return {
        "template": template, "label": definition["label"], "air_loop": _identity(loop),
        "plant_loops": [_identity(item) for item in created_plants],
        "zones": [_identity(zone) for zone in zones],
        "sizing": sizing,
    }, warnings


def _plant_loop_create(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = patch.get("payload") or {}
    loop_type = str(payload.get("loop_type") or "Heating")
    defaults = {
        "Heating": (82.0, 11.0), "Cooling": (6.7, 5.6), "Condenser": (29.4, 5.6),
    }
    if loop_type not in defaults:
        raise PatchRejected("plant_loop_type", "loop_type must be Heating, Cooling, or Condenser")
    exit_default, delta_default = defaults[loop_type]
    exit_temperature = _finite(payload.get("design_exit_temperature_c", exit_default), "design_exit_temperature_c")
    delta = _positive(payload.get("design_delta_temperature_k", delta_default), "design_delta_temperature_k")
    loop = openstudio.model.PlantLoop(model)
    loop.setName(str(payload.get("name") or f"Authored {loop_type} Plant Loop")[:100])
    sizing = loop.sizingPlant()
    if not sizing.setLoopType(loop_type):
        loop.remove()
        raise PatchRejected("sdk_rejected", f"OpenStudio rejected plant loop type {loop_type}")
    sizing.setDesignLoopExitTemperature(exit_temperature)
    sizing.setLoopDesignTemperatureDifference(delta)
    pump = openstudio.model.PumpVariableSpeed(model)
    pump.setName(f"{loop.nameString()} Pump")
    if not pump.addToNode(loop.supplyInletNode()):
        loop.remove()
        raise PatchRejected("sdk_rejected", "OpenStudio could not add the plant-loop pump")
    for side, add_branch, outlet in (
        ("Supply", loop.addSupplyBranchForComponent, loop.supplyOutletNode()),
        ("Demand", loop.addDemandBranchForComponent, loop.demandOutletNode()),
    ):
        bypass = openstudio.model.PipeAdiabatic(model)
        bypass.setName(f"{loop.nameString()} {side} Bypass")
        add_branch(bypass)
        outlet_pipe = openstudio.model.PipeAdiabatic(model)
        outlet_pipe.setName(f"{loop.nameString()} {side} Outlet Pipe")
        outlet_pipe.addToNode(outlet)
    schedule = openstudio.model.ScheduleRuleset(model)
    schedule.setName(f"{loop.nameString()} Setpoint")
    schedule.defaultDaySchedule().addValue(openstudio.Time(0, 24, 0, 0), exit_temperature)
    manager = openstudio.model.SetpointManagerScheduled(model, schedule)
    manager.setName(f"{loop.nameString()} Setpoint Manager")
    manager.addToNode(loop.supplyOutletNode())
    return {
        "plant_loop": _identity(loop), "loop_type": loop_type,
        "design_exit_temperature_c": exit_temperature, "design_delta_temperature_k": delta,
        "pump": _identity(pump),
    }, [{
        "severity": "warning", "code": "plant_loop_unconnected",
        "message": "The new plant loop is structurally initialized; connect coils and supply equipment before simulation",
    }]


def _air_loop(model: openstudio.model.Model, target_id: Any) -> Any:
    return _object(model, target_id, "to_AirLoopHVAC")


def _loop_delete(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    target = _object(model, patch.get("target_id"))
    air = _cast(target, "to_AirLoopHVAC")
    if air is not None:
        zones = list(air.thermalZones())
        result = _identity(air) | {"kind": "air", "zones": [_identity(zone) for zone in zones]}
        air.remove()
        for zone in zones:
            if not zone.equipment():
                zone.setUseIdealAirLoads(True)
        return result, [{
            "severity": "warning", "code": "ideal_loads_fallback",
            "message": f"{len(zones)} disconnected zone(s) were returned to Ideal Loads",
        }]
    plant = _cast(target, "to_PlantLoop")
    if plant is None:
        raise PatchRejected("target_type", "Target must be an AirLoopHVAC or PlantLoop")
    active_demand = [item for item in plant.demandComponents() if _idd_name(item) not in {
        "OS_Node", "OS_Connector_Splitter", "OS_Connector_Mixer", "OS_Pipe_Adiabatic",
    }]
    if active_demand:
        raise PatchRejected("plant_loop_connected", "Disconnect/remove active plant demand components before deleting this loop")
    result = _identity(plant) | {"kind": "plant"}
    plant.remove()
    return result, []


def _zone_connect(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    loop = _air_loop(model, patch.get("target_id"))
    zone = _object(model, (patch.get("payload") or {}).get("zone_id"), "to_ThermalZone")
    if any(str(item.handle()) == str(zone.handle()) for item in loop.thermalZones()):
        raise PatchRejected("zone_connected", f"{zone.nameString()} is already connected to {loop.nameString()}")
    _detach_zone_from_air(model, zone)
    _clear_zone_equipment(zone)
    zone.setUseIdealAirLoads(False)
    if not loop.addBranchForZone(zone):
        raise PatchRejected("sdk_rejected", "OpenStudio rejected the zone branch")
    warnings = [] if not zone.thermostatSetpointDualSetpoint().isNull() else [{
        "severity": "warning", "code": "zone_without_thermostat",
        "message": "The connected zone has no dual-setpoint thermostat",
    }]
    return {"air_loop": _identity(loop), "zone": _identity(zone)}, warnings


def _zone_disconnect(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    loop = _air_loop(model, patch.get("target_id"))
    zone = _object(model, (patch.get("payload") or {}).get("zone_id"), "to_ThermalZone")
    if not any(str(item.handle()) == str(zone.handle()) for item in loop.thermalZones()):
        raise PatchRejected("zone_not_connected", f"{zone.nameString()} is not connected to {loop.nameString()}")
    if not loop.removeBranchForZone(zone):
        raise PatchRejected("sdk_rejected", "OpenStudio could not remove the zone branch")
    zone.setUseIdealAirLoads(True)
    return {"air_loop": _identity(loop), "zone": _identity(zone), "fallback": "ideal_loads"}, [{
        "severity": "warning", "code": "ideal_loads_fallback", "message": f"{zone.nameString()} now uses Ideal Loads",
    }]


def _fraction(value: Any, field: str) -> float:
    result = _positive(value, field)
    if result > 1:
        raise PatchRejected("physical_limit", f"{field} must be no greater than 1")
    return result


def _configure_component(component: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    definition = HVAC_COMPONENT_SETTINGS.get(_idd_name(component))
    if definition is None:
        if any(key in payload for key in ("autosize", "capacity", "efficiency")):
            raise PatchRejected("component_sizing", f"Sizing is not exposed for {_idd_name(component)}")
        return _identity(component), []
    typed = _cast(component, definition["cast"])
    if typed is None:
        raise PatchRejected("target_type", "HVAC component cannot be safely downcast")
    changed: dict[str, Any] = {}
    warnings: list[dict[str, Any]] = []
    capacity = definition.get("capacity")
    if capacity and "autosize" in payload:
        autosize = _boolean(payload["autosize"], "autosize")
        if autosize:
            if capacity[2] is None:
                raise PatchRejected("component_autosize", "This component does not expose autosizing")
            getattr(typed, capacity[2])()
            changed["autosize"] = True
    if capacity and "capacity" in payload and payload.get("autosize") is not True:
        value = _positive(payload["capacity"], "capacity")
        if getattr(typed, capacity[1])(value) is False:
            raise PatchRejected("sdk_rejected", "OpenStudio rejected component capacity")
        changed["capacity"] = value
        if value > 10_000_000:
            warnings.append({"severity": "warning", "code": "large_capacity", "message": f"Capacity {value:g} is above 10 MW; expert value retained"})
    efficiency = definition.get("efficiency")
    if efficiency and "efficiency" in payload:
        value = _fraction(payload["efficiency"], "efficiency") if efficiency[2] == "fraction" else _positive(payload["efficiency"], "efficiency")
        if getattr(typed, efficiency[1])(value) is False:
            raise PatchRejected("sdk_rejected", "OpenStudio rejected component efficiency/COP")
        changed["efficiency"] = value
        if efficiency[2] == "positive" and value > 10:
            warnings.append({"severity": "warning", "code": "high_cop", "message": f"COP {value:g} is unusual; expert value retained"})
    for field, (_, setter, validator) in (definition.get("extra") or {}).items():
        if field not in payload:
            continue
        value = _fraction(payload[field], field) if validator == "fraction" else _positive(payload[field], field)
        if getattr(typed, setter)(value) is False:
            raise PatchRejected("sdk_rejected", f"OpenStudio rejected {field}")
        changed[field] = value
    return _identity(component) | {"settings": changed}, warnings


def _component_add(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = patch.get("payload") or {}
    equipment_type = str(payload.get("equipment_type") or "")
    definition = HVAC_EQUIPMENT_LIBRARY.get(equipment_type)
    if definition is None:
        raise PatchRejected("equipment_type", f"Unsupported HVAC equipment: {equipment_type}")
    target = _object(model, patch.get("target_id"))
    air = _cast(target, "to_AirLoopHVAC")
    plant = _cast(target, "to_PlantLoop")
    loop_kind = "air" if air is not None else "plant" if plant is not None else ""
    if loop_kind not in definition["loop_kinds"]:
        raise PatchRejected("equipment_loop", f"{definition['label']} cannot be added to this loop")
    component = getattr(openstudio.model, definition["class_name"])(model)
    component.setName(str(payload.get("name") or f"Authored {definition['label']}")[:100])
    attached = False
    if air is not None:
        attached = bool(component.addToNode(air.supplyOutletNode()))
        required_plant = definition.get("requires_plant")
        if required_plant:
            plant_id = payload.get("plant_loop_id")
            if not plant_id:
                component.remove()
                raise PatchRejected("plant_loop_required", f"{definition['label']} requires plant_loop_id")
            water_loop = _object(model, plant_id, "to_PlantLoop")
            attached = attached and bool(water_loop.addDemandBranchForComponent(component))
    elif equipment_type == "pump_variable_speed":
        attached = bool(component.addToNode(plant.supplyInletNode()))
    else:
        attached = bool(plant.addSupplyBranchForComponent(component))
    if not attached:
        component.remove()
        raise PatchRejected("sdk_rejected", "OpenStudio could not attach the HVAC component")
    result, warnings = _configure_component(component, payload)
    return result | {"equipment_type": equipment_type, "loop": _identity(target)}, warnings


def _component_update(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    component = _object(model, patch.get("target_id"))
    if _idd_name(component) not in HVAC_COMPONENT_SETTINGS:
        raise PatchRejected("component_sizing", f"Sizing is not exposed for {_idd_name(component)}")
    payload = patch.get("payload") or {}
    if "name" in payload:
        component.setName(str(payload["name"])[:100])
    result, warnings = _configure_component(component, payload)
    if not result.get("settings") and "name" not in payload:
        raise PatchRejected("component_sizing", "Provide a capacity, efficiency/COP, flow, pressure, or autosize change")
    return result, warnings


def _component_remove(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    component = _object(model, patch.get("target_id"))
    if _idd_name(component) not in HVAC_COMPONENT_SETTINGS and _idd_name(component) not in {
        "OS_Coil_Heating_Water", "OS_Coil_Cooling_Water",
    }:
        raise PatchRejected("component_remove", "Only equipment-library components can be removed directly")
    result = _identity(component) | {"type": _idd_name(component)}
    component.remove()
    return result


def _output_variables(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    variables = (patch.get("payload") or {}).get("variables")
    if not isinstance(variables, list) or not variables:
        raise PatchRejected("output_variables", "At least one output variable is required")
    normalized = []
    for item in variables:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise PatchRejected("output_variable_name", "Every output variable needs a name")
        normalized.append({
            "name": str(item["name"]).strip(), "key": str(item.get("key") or "*"),
            "frequency": str(item.get("frequency") or "RunPeriod"),
        })
    for variable in list(model.getOutputVariables()):
        variable.remove()
    for item in normalized:
        variable = openstudio.model.OutputVariable(item["name"], model)
        variable.setKeyValue(item["key"])
        if not variable.setReportingFrequency(item["frequency"]):
            raise PatchRejected("reporting_frequency", f"Unsupported frequency: {item['frequency']}")
    return {"variables": normalized}


def _run_period(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    payload = patch.get("payload") or {}
    begin = _calendar_date(payload.get("begin"), "begin")
    end = _calendar_date(payload.get("end"), "end")
    period = model.getRunPeriod()
    accepted = all((period.setBeginMonth(begin[0]), period.setBeginDayOfMonth(begin[1]), period.setEndMonth(end[0]), period.setEndDayOfMonth(end[1])))
    if not accepted:
        raise PatchRejected("run_period", "OpenStudio rejected the requested run period")
    return _identity(period) | {"begin": {"month": begin[0], "day": begin[1]}, "end": {"month": end[0], "day": end[1]}}


def _point3d_vector(raw: Any, field: str = "vertices") -> Any:
    if not isinstance(raw, list) or len(raw) < 3:
        raise PatchRejected("geometry_vertices", f"{field} requires at least three [x, y, z] points")
    points = openstudio.Point3dVector()
    for index, item in enumerate(raw):
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise PatchRejected("geometry_point", f"{field}[{index}] must be [x, y, z]")
        points.append(openstudio.Point3d(*(_finite(value, f"{field}[{index}]") for value in item)))
    return points


def _translated_vertices(item: Any, dx: float, dy: float, dz: float) -> Any:
    points = openstudio.Point3dVector()
    for vertex in item.vertices():
        points.append(openstudio.Point3d(vertex.x() + dx, vertex.y() + dy, vertex.z() + dz))
    return points


def _copy_construction(source: Any, target: Any) -> None:
    construction = _optional(source.construction())
    if construction is not None and target.setConstruction(construction) is False:
        raise PatchRejected("construction_assignment", f"Could not copy construction from {source.nameString()}")


def _repair_geometry(model: openstudio.model.Model) -> None:
    spaces = openstudio.model.SpaceVector()
    for space in model.getSpaces():
        spaces.append(space)
    openstudio.model.intersectSurfaces(spaces)
    openstudio.model.matchSurfaces(spaces)


def _surface_create(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    space = _object(model, patch.get("target_id"), "to_Space")
    payload = patch.get("payload") or {}
    surface = openstudio.model.Surface(_point3d_vector(payload.get("vertices")), model)
    surface.setName(str(payload.get("name") or f"Authored surface in {space.nameString()}"))
    if not surface.setSpace(space):
        surface.remove()
        raise PatchRejected("space_assignment", "OpenStudio rejected the surface-to-space assignment")
    surface_type = str(payload.get("surface_type") or "Wall")
    if surface_type not in {"Wall", "Floor", "RoofCeiling"} or not surface.setSurfaceType(surface_type):
        surface.remove()
        raise PatchRejected("surface_type", f"Unsupported surface type: {surface_type}")
    boundary = str(payload.get("boundary_condition") or "Outdoors")
    if boundary not in {"Outdoors", "Ground", "Adiabatic"} or not surface.setOutsideBoundaryCondition(boundary):
        surface.remove()
        raise PatchRejected("boundary_condition", f"Unsupported boundary condition: {boundary}")
    construction_id = payload.get("construction_id")
    construction = _object(model, construction_id, "to_ConstructionBase") if construction_id else _active_construction(model, surface_type)
    if construction is not None and surface.setConstruction(construction) is False:
        surface.remove()
        raise PatchRejected("construction_assignment", "OpenStudio rejected the surface construction")
    _repair_geometry(model)
    return _identity(surface) | {"surface_type": surface_type, "boundary_condition": boundary, "area_m2": float(surface.grossArea())}, [{
        "severity": "warning", "code": "enclosure_review",
        "message": "A single surface can open or overlap a space; geometry preflight must pass before commit",
    }]


def _surface_move(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    surface = _object(model, patch.get("target_id"), "to_Surface")
    payload = patch.get("payload") or {}
    dx, dy, dz = (_finite(payload.get(key, 0.0), key) for key in ("dx", "dy", "dz"))
    if abs(dx) + abs(dy) + abs(dz) < 1e-9:
        raise PatchRejected("geometry_noop", "Surface translation must move at least one axis")
    children = [(sub, _translated_vertices(sub, dx, dy, dz)) for sub in surface.subSurfaces()]
    if not surface.setVertices(_translated_vertices(surface, dx, dy, dz)):
        raise PatchRejected("geometry_rejected", "OpenStudio rejected the translated surface vertices")
    for sub, vertices in children:
        if not sub.setVertices(vertices):
            raise PatchRejected("geometry_rejected", f"OpenStudio rejected translated opening {sub.nameString()}")
    _repair_geometry(model)
    return _identity(surface) | {"translation_m": {"x": dx, "y": dy, "z": dz}}, [{
        "severity": "warning", "code": "topology_changed",
        "message": "Moving one surface can break enclosure; matching/intersection was rerun and preflight remains mandatory",
    }]


def _surface_delete(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    surface = _object(model, patch.get("target_id"), "to_Surface")
    result = _identity(surface) | {"deleted_subsurfaces": len(surface.subSurfaces())}
    surface.remove()
    _repair_geometry(model)
    return result | {"deleted": True}, [{
        "severity": "warning", "code": "enclosure_review",
        "message": "Deleting a heat-transfer surface normally opens its space; commit is blocked unless geometry preflight still passes",
    }]


def _surface_basis(surface: Any) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    vertices = [(float(point.x()), float(point.y()), float(point.z())) for point in surface.vertices()]
    if len(vertices) < 3 or not surface.isConvex():
        raise PatchRejected("surface_shape", "Drawn openings currently require a convex parent surface")
    edges = []
    for index, point in enumerate(vertices):
        nxt = vertices[(index + 1) % len(vertices)]
        delta = tuple(nxt[axis] - point[axis] for axis in range(3))
        edges.append((sum(value * value for value in delta), point, delta))
    length_sq, origin, edge = max(edges, key=lambda item: item[0])
    if length_sq <= 1e-10:
        raise PatchRejected("surface_shape", "Parent surface has no usable edge")
    length = math.sqrt(length_sq)
    u_axis = tuple(value / length for value in edge)
    normal_value = surface.outwardNormal()
    normal = (float(normal_value.x()), float(normal_value.y()), float(normal_value.z()))
    v_axis = (
        normal[1] * u_axis[2] - normal[2] * u_axis[1],
        normal[2] * u_axis[0] - normal[0] * u_axis[2],
        normal[0] * u_axis[1] - normal[1] * u_axis[0],
    )
    v_length = math.sqrt(sum(value * value for value in v_axis))
    if v_length <= 1e-10:
        raise PatchRejected("surface_shape", "Could not derive a surface-local drawing plane")
    return origin, u_axis, tuple(value / v_length for value in v_axis), normal


def _normalized_opening_vertices(surface: Any, payload: dict[str, Any]) -> Any:
    origin, u_axis, v_axis, normal = _surface_basis(surface)
    projected = []
    for point in surface.vertices():
        delta = (point.x() - origin[0], point.y() - origin[1], point.z() - origin[2])
        projected.append((sum(delta[i] * u_axis[i] for i in range(3)), sum(delta[i] * v_axis[i] for i in range(3))))
    u0, u1 = min(value[0] for value in projected), max(value[0] for value in projected)
    v0, v1 = min(value[1] for value in projected), max(value[1] for value in projected)
    rect = payload.get("normalized_rect") or {}
    values = {
        "u_min": _finite(rect.get("u_min", 0.30), "u_min"), "u_max": _finite(rect.get("u_max", 0.70), "u_max"),
        "v_min": _finite(rect.get("v_min", 0.25), "v_min"), "v_max": _finite(rect.get("v_max", 0.75), "v_max"),
    }
    if not (0.0 < values["u_min"] < values["u_max"] < 1.0 and 0.0 < values["v_min"] < values["v_max"] < 1.0):
        raise PatchRejected("opening_bounds", "Normalized opening bounds must be ordered strictly inside 0–1")
    bounds = [
        (u0 + (u1 - u0) * values["u_min"], v0 + (v1 - v0) * values["v_min"]),
        (u0 + (u1 - u0) * values["u_max"], v0 + (v1 - v0) * values["v_min"]),
        (u0 + (u1 - u0) * values["u_max"], v0 + (v1 - v0) * values["v_max"]),
        (u0 + (u1 - u0) * values["u_min"], v0 + (v1 - v0) * values["v_max"]),
    ]
    points = [tuple(origin[i] + u * u_axis[i] + v * v_axis[i] for i in range(3)) for u, v in bounds]
    first = tuple(points[1][i] - points[0][i] for i in range(3))
    second = tuple(points[2][i] - points[1][i] for i in range(3))
    cross = (first[1] * second[2] - first[2] * second[1], first[2] * second[0] - first[0] * second[2], first[0] * second[1] - first[1] * second[0])
    if sum(cross[i] * normal[i] for i in range(3)) < 0:
        points.reverse()
    return _point3d_vector([list(point) for point in points])


def _subsurface_create(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    surface = _object(model, patch.get("target_id"), "to_Surface")
    if surface.surfaceType() != "Wall" or surface.outsideBoundaryCondition() != "Outdoors":
        raise PatchRejected("opening_parent", "Windows and doors can only be drawn on exterior walls")
    payload = patch.get("payload") or {}
    vertices = _point3d_vector(payload["vertices"]) if "vertices" in payload else _normalized_opening_vertices(surface, payload)
    opening = openstudio.model.SubSurface(vertices, model)
    opening.setName(str(payload.get("name") or f"Authored opening on {surface.nameString()}"))
    kind = str(payload.get("subsurface_type") or "FixedWindow")
    if kind not in {"FixedWindow", "OperableWindow", "Door", "GlassDoor"} or not opening.setSubSurfaceType(kind):
        opening.remove()
        raise PatchRejected("subsurface_type", f"Unsupported opening type: {kind}")
    if not opening.setSurface(surface):
        opening.remove()
        raise PatchRejected("opening_geometry", "The opening does not fit on the selected surface")
    construction_id = payload.get("construction_id")
    if construction_id:
        construction = _object(model, construction_id, "to_ConstructionBase")
    else:
        construction = next((_optional(item.construction()) for item in model.getSubSurfaces()
                             if item.handle() != opening.handle() and item.subSurfaceType() == kind and _optional(item.construction()) is not None), None)
        if construction is None and kind in {"FixedWindow", "OperableWindow", "GlassDoor"}:
            construction = next((_optional(item.construction()) for item in model.getSubSurfaces()
                                 if item.handle() != opening.handle() and item.subSurfaceType() != "Door" and _optional(item.construction()) is not None), None)
    if construction is not None and opening.setConstruction(construction) is False:
        opening.remove()
        raise PatchRejected("construction_assignment", "OpenStudio rejected the opening construction")
    return _identity(opening) | {"parent": _identity(surface), "subsurface_type": kind, "area_m2": float(opening.grossArea())}


def _subsurface_delete(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    opening = _object(model, patch.get("target_id"), "to_SubSurface")
    result = _identity(opening) | {"area_m2": float(opening.grossArea())}
    opening.remove()
    return result | {"deleted": True}


def _surface_set_wwr(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    surface = _object(model, patch.get("target_id"), "to_Surface")
    if surface.surfaceType() != "Wall" or surface.outsideBoundaryCondition() != "Outdoors":
        raise PatchRejected("wwr_parent", "WWR can only be applied to an exterior wall")
    payload = patch.get("payload") or {}
    ratio = _finite(payload.get("ratio"), "ratio")
    sill = _positive(payload.get("sill_m", 0.9), "sill_m", allow_zero=True)
    if ratio <= 0 or ratio >= 0.95:
        raise PatchRejected("wwr_limit", "WWR must be greater than 0 and below 0.95")
    for opening in list(surface.subSurfaces()):
        if opening.subSurfaceType() not in {"Door", "GlassDoor"}:
            opening.remove()
    created = _optional(surface.setWindowToWallRatio(ratio, sill, True))
    if created is None:
        raise PatchRejected("wwr_rejected", "OpenStudio could not place a window at this WWR and sill")
    construction = next((_optional(item.construction()) for item in model.getSubSurfaces()
                         if item.handle() != created.handle() and item.subSurfaceType() != "Door" and _optional(item.construction()) is not None), None)
    if construction is not None:
        created.setConstruction(construction)
    created.setName(str(payload.get("name") or f"Authored WWR {ratio:.2f} on {surface.nameString()}"))
    return _identity(created) | {"parent": _identity(surface), "ratio": ratio, "sill_m": sill, "area_m2": float(created.grossArea())}


def _add_overhang(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    opening = _object(model, patch.get("target_id"), "to_SubSurface")
    payload = patch.get("payload") or {}
    depth = _positive(payload.get("depth_m"), "depth_m")
    offset = _finite(payload.get("offset_m", 0.1), "offset_m")
    overhang = _optional(opening.addOverhang(depth, offset))
    if overhang is None:
        raise PatchRejected("overhang_rejected", "OpenStudio could not create the overhang")
    overhang.setName(str(payload.get("name") or f"Authored overhang for {opening.nameString()}"))
    return _identity(overhang) | {"opening": _identity(opening), "depth_m": depth, "offset_m": offset}


def _story_add(model: openstudio.model.Model, patch: dict[str, Any]) -> dict[str, Any]:
    payload = patch.get("payload") or {}
    story = openstudio.model.BuildingStory(model)
    story.setName(str(payload.get("name") or "Authored empty story"))
    if "z_m" in payload and not story.setNominalZCoordinate(_finite(payload["z_m"], "z_m")):
        story.remove()
        raise PatchRejected("story_elevation", "OpenStudio rejected the story elevation")
    if "height_m" in payload and not story.setNominalFloortoFloorHeight(_positive(payload["height_m"], "height_m")):
        story.remove()
        raise PatchRejected("story_height", "OpenStudio rejected the story height")
    return _identity(story) | {"space_count": 0}


def _copy_zone_for_story(model: openstudio.model.Model, source_space: Any, name: str) -> Any:
    zone = openstudio.model.ThermalZone(model)
    zone.setName(name)
    zone.setUseIdealAirLoads(True)
    source_zone = _optional(source_space.thermalZone())
    source_thermostat = _optional(source_zone.thermostatSetpointDualSetpoint()) if source_zone is not None else None
    if source_thermostat is not None:
        thermostat = openstudio.model.ThermostatSetpointDualSetpoint(model)
        thermostat.setName(f"{name} thermostat")
        heating = _optional(source_thermostat.heatingSetpointTemperatureSchedule())
        cooling = _optional(source_thermostat.coolingSetpointTemperatureSchedule())
        if heating is not None:
            thermostat.setHeatingSetpointTemperatureSchedule(heating)
        if cooling is not None:
            thermostat.setCoolingSetpointTemperatureSchedule(cooling)
        zone.setThermostatSetpointDualSetpoint(thermostat)
    return zone


def _duplicate_story(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    story = _object(model, patch.get("target_id"), "to_BuildingStory")
    source_spaces = list(story.spaces())
    if not source_spaces:
        raise PatchRejected("story_empty", "The selected story has no spaces to duplicate")
    story_elevations = [float(_optional(item.nominalZCoordinate()) or 0.0) for item in model.getBuildingStorys()]
    source_z = float(_optional(story.nominalZCoordinate()) or min(vertex.z() for space in source_spaces for surface in space.surfaces() for vertex in surface.vertices()))
    if source_z < max(story_elevations, default=source_z) - 1e-6:
        raise PatchRejected("top_story_required", "Add-floor duplication currently requires the highest occupied story")
    payload = patch.get("payload") or {}
    inferred_height = max(vertex.z() for space in source_spaces for surface in space.surfaces() for vertex in surface.vertices()) - min(vertex.z() for space in source_spaces for surface in space.surfaces() for vertex in surface.vertices())
    height = _positive(payload.get("height_m", _optional(story.nominalFloortoFloorHeight()) or inferred_height), "height_m")
    target_story = openstudio.model.BuildingStory(model)
    target_story.setName(str(payload.get("name") or f"{story.nameString()} · authored copy"))
    target_story.setNominalZCoordinate(source_z + height)
    target_story.setNominalFloortoFloorHeight(height)
    created_spaces = []
    for source_space in source_spaces:
        new_space = openstudio.model.Space(model)
        new_space.setName(f"{source_space.nameString()} · authored +1")
        new_space.setBuildingStory(target_story)
        source_type = _optional(source_space.spaceType())
        if source_type is not None:
            new_space.setSpaceType(source_type)
        new_zone = _copy_zone_for_story(model, source_space, f"{new_space.nameString()} zone")
        new_space.setThermalZone(new_zone)
        for source_surface in source_space.surfaces():
            new_surface = openstudio.model.Surface(_translated_vertices(source_surface, 0.0, 0.0, height), model)
            new_surface.setName(f"{source_surface.nameString()} · authored +1")
            new_surface.setSpace(new_space)
            new_surface.setSurfaceType(source_surface.surfaceType())
            boundary = source_surface.outsideBoundaryCondition()
            new_surface.setOutsideBoundaryCondition("Outdoors" if boundary == "Surface" else boundary)
            _copy_construction(source_surface, new_surface)
            for source_opening in source_surface.subSurfaces():
                opening = openstudio.model.SubSurface(_translated_vertices(source_opening, 0.0, 0.0, height), model)
                opening.setName(f"{source_opening.nameString()} · authored +1")
                opening.setSubSurfaceType(source_opening.subSurfaceType())
                opening.setSurface(new_surface)
                _copy_construction(source_opening, opening)
        created_spaces.append(new_space)
    _repair_geometry(model)
    return _identity(target_story) | {"source_story": _identity(story), "height_m": height, "space_count": len(created_spaces)}, [{
        "severity": "warning", "code": "new_zone_ideal_loads",
        "message": "Duplicated spaces receive new thermostat-linked Ideal Loads zones; review HVAC before Part B if a detailed system is required",
    }]


def _apply(model: openstudio.model.Model, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    operation = str(patch.get("op") or "")
    if operation not in SUPPORTED_PATCHES:
        raise PatchRejected("operation_unsupported", f"Unsupported typed patch: {operation}")
    if operation == "project_parameter.update":
        return _project_parameter(model, patch)
    if operation in {"construction.set_layers", "construction.reorder"}:
        return _set_construction_layers(model, patch), []
    if operation == "material.update":
        return _update_material(model, patch), []
    if operation == "material.create":
        return _create_material(model, patch), []
    if operation == "material.delete":
        return _delete_material(model, patch), []
    if operation == "schedule.update_day":
        return _update_schedule_day(model, patch), []
    if operation == "schedule.add_rule":
        return _add_schedule_rule(model, patch), []
    if operation == "schedule.delete_rule":
        return _delete_schedule_rule(model, patch), []
    if operation == "space_type.set_loads":
        return _space_type_loads(model, patch), []
    if operation == "space_type.set_infiltration":
        return _space_type_infiltration(model, patch), []
    if operation == "space_type.set_dsoa":
        return _space_type_dsoa(model, patch), []
    if operation == "thermostat.set_setpoints":
        return _thermostat_setpoints(model, patch)
    if operation == "hvac.set_system":
        return _hvac_system(model, patch), []
    if operation == "hvac.air_loop.create":
        return _air_loop_create(model, patch)
    if operation == "hvac.plant_loop.create":
        return _plant_loop_create(model, patch)
    if operation == "hvac.loop.delete":
        return _loop_delete(model, patch)
    if operation == "hvac.zone.connect":
        return _zone_connect(model, patch)
    if operation == "hvac.zone.disconnect":
        return _zone_disconnect(model, patch)
    if operation == "hvac.component.add":
        return _component_add(model, patch)
    if operation == "hvac.component.update":
        return _component_update(model, patch)
    if operation == "hvac.component.remove":
        return _component_remove(model, patch), []
    if operation == "measure.apply":
        raise PatchRejected("measure_session", "Measures are applied only inside a provenance-scoped edit session")
    if operation == "sim.set_output_variables":
        return _output_variables(model, patch), []
    if operation == "sim.set_run_period":
        return _run_period(model, patch), []
    if operation == "surface.create":
        return _surface_create(model, patch)
    if operation == "surface.move":
        return _surface_move(model, patch)
    if operation == "surface.delete":
        return _surface_delete(model, patch)
    if operation == "surface.set_wwr":
        return _surface_set_wwr(model, patch), []
    if operation == "subsurface.create":
        return _subsurface_create(model, patch), []
    if operation == "subsurface.delete":
        return _subsurface_delete(model, patch), []
    if operation == "subsurface.add_overhang":
        return _add_overhang(model, patch), []
    if operation == "story.add":
        return _story_add(model, patch), []
    return _duplicate_story(model, patch)


def apply_typed_patches(osm_path: str | Path, output_path: str | Path,
                        patches: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply supported patches to a fresh SDK model and atomically materialize a new OSM."""
    if not isinstance(patches, list) or not patches:
        raise ValueError("At least one typed patch is required")
    model = _load_model(osm_path)
    reports = []
    for index, patch in enumerate(patches):
        if not isinstance(patch, dict):
            reports.append({"index": index, "op": None, "status": "rejected", "severity": "error", "code": "patch_object", "message": "Patch must be an object"})
            continue
        operation = str(patch.get("op") or "")
        try:
            result, warnings = _apply(model, patch)
            reports.append({
                "index": index, "op": operation, "status": "applied",
                "severity": "warning" if warnings else "success", "code": "applied",
                "message": f"{operation} applied", "result": result, "warnings": warnings,
            })
        except PatchRejected as exc:
            reports.append({
                "index": index, "op": operation, "status": "rejected", "severity": "error",
                "code": exc.code, "message": str(exc), "warnings": [],
            })
    _save_model(model, output_path)
    return {
        "schema_version": 1,
        "reports": reports,
        "applied": sum(item["status"] == "applied" for item in reports),
        "rejected": sum(item["status"] == "rejected" for item in reports),
    }


def preflight_model(osm_path: str | Path) -> dict[str, Any]:
    """Translate the authored model and report commit blockers separately from warnings."""
    model = _load_model(osm_path)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    required_outputs = {
        "Zone Ideal Loads Supply Air Total Heating Energy",
        "Zone Ideal Loads Supply Air Total Cooling Energy",
    }
    declared = {item.variableName() for item in model.getOutputVariables()}
    conditioned_zones = [zone for zone in model.getThermalZones() if not zone.thermostatSetpointDualSetpoint().isNull()]
    ideal_zones = [zone for zone in conditioned_zones if zone.useIdealAirLoads()]
    air_zone_ids = {
        str(zone.handle()) for loop in model.getAirLoopHVACs() for zone in loop.thermalZones()
    }
    detailed_zones = [
        zone for zone in conditioned_zones
        if str(zone.handle()) in air_zone_ids
        or any(_idd_name(item) != "OS_ZoneHVAC_IdealLoadsAirSystem" for item in zone.equipment())
    ]
    detailed_hvac = bool(model.getAirLoopHVACs() or model.getPlantLoops() or detailed_zones)
    mixed_hvac = detailed_hvac and bool(ideal_zones)
    if mixed_hvac:
        errors.append({
            "code": "mixed_hvac_basis",
            "message": "Conditioned zones mix Ideal Loads with detailed HVAC; connect every conditioned zone or return the whole model to Ideal Loads",
        })
    orphan_air_loops = [loop.nameString() for loop in model.getAirLoopHVACs() if not loop.thermalZones()]
    if orphan_air_loops:
        errors.append({"code": "air_loop_zones", "message": f"{len(orphan_air_loops)} air loop(s) have no connected thermal zones"})
    orphan_plant_only = bool(model.getPlantLoops()) and not bool(model.getAirLoopHVACs())
    if orphan_plant_only:
        errors.append({"code": "plant_loop_demand", "message": "Plant loops exist without an air loop; connect a demand coil before commit"})
    sizing_ready = True
    if detailed_hvac:
        control = model.getSimulationControl()
        design_day_types = {item.dayType() for item in model.getDesignDays()}
        sizing_ready = (
            control.doZoneSizingCalculation()
            and (not model.getAirLoopHVACs() or control.doSystemSizingCalculation())
            and (not model.getPlantLoops() or control.doPlantSizingCalculation())
            and {"WinterDesignDay", "SummerDesignDay"} <= design_day_types
        )
        if not sizing_ready:
            errors.append({
                "code": "hvac_sizing",
                "message": "Detailed HVAC requires zone/system/plant autosizing controls and both winter and summer design days",
            })
    missing = sorted(required_outputs - declared)
    if missing and not detailed_hvac:
        errors.append({"code": "required_outputs", "message": "Part B requires: " + ", ".join(missing)})
    elif missing:
        warnings.append({"code": "detailed_hvac_outputs", "message": "Detailed HVAC uses the EnergyPlus End Uses table; Ideal Loads output variables are not required"})
    assigned = [surface for surface in [*model.getSurfaces(), *model.getSubSurfaces()] if surface.construction().isNull()]
    if assigned:
        errors.append({"code": "unassigned_construction", "message": f"{len(assigned)} surface(s) have no construction"})
    open_spaces = [space.nameString() for space in model.getSpaces() if not space.isEnclosedVolume()]
    if open_spaces:
        errors.append({"code": "space_enclosure", "message": f"{len(open_spaces)} space(s) are not enclosed: " + ", ".join(open_spaces[:8])})
    invalid_surfaces = [surface.nameString() for surface in model.getSurfaces()
                        if len(surface.vertices()) < 3 or surface.grossArea() <= 1e-6]
    if invalid_surfaces:
        errors.append({"code": "surface_geometry", "message": f"{len(invalid_surfaces)} surface(s) have invalid vertices or area"})
    orphan_openings = [opening.nameString() for opening in model.getSubSurfaces() if opening.surface().isNull()]
    invalid_openings = [opening.nameString() for opening in model.getSubSurfaces()
                        if len(opening.vertices()) < 3 or opening.grossArea() <= 1e-6]
    if orphan_openings or invalid_openings:
        errors.append({"code": "opening_geometry", "message": f"{len(orphan_openings)} orphan and {len(invalid_openings)} invalid opening(s)"})
    nonconvex = [surface.nameString() for surface in model.getSurfaces() if not surface.isConvex()]
    if nonconvex:
        warnings.append({"code": "nonconvex_surface", "message": f"{len(nonconvex)} non-convex surface(s) rely on EnergyPlus triangulation"})
    try:
        loggers = [openstudio.Logger.instance().standardOutLogger(), openstudio.Logger.instance().standardErrLogger()]
        logger_states = [logger.isEnabled() for logger in loggers]
        for logger in loggers:
            logger.disable()
        translator = openstudio.energyplus.ForwardTranslator()
        workspace = translator.translateModel(model)
        if len(workspace.objects()) == 0:
            errors.append({"code": "empty_idf", "message": "Forward translation produced no EnergyPlus objects"})
        errors.extend({"code": "forward_translation", "message": str(item.logMessage())} for item in translator.errors())
        warning_messages = list(dict.fromkeys(str(item.logMessage()) for item in translator.warnings()))
        warnings.extend({"code": "forward_translation", "message": message} for message in warning_messages[:50])
    except Exception as exc:
        errors.append({"code": "forward_translation", "message": str(exc)})
    finally:
        for logger, was_enabled in zip(locals().get("loggers", []), locals().get("logger_states", []), strict=True):
            if was_enabled:
                logger.enable()
    return {
        "schema_version": 1, "ready": not errors, "errors": errors, "warnings": warnings,
        "checks": {
            "openstudio_load": True, "required_outputs": not missing or detailed_hvac,
            "surface_constructions": not assigned, "forward_translation": not any(item["code"] == "forward_translation" for item in errors),
            "space_enclosure": not open_spaces, "surface_geometry": not invalid_surfaces,
            "opening_geometry": not orphan_openings and not invalid_openings,
            "hvac_topology": not mixed_hvac and not orphan_air_loops and not orphan_plant_only,
            "hvac_sizing": sizing_ready,
        },
        "energy_basis": "detailed_hvac_consumption" if detailed_hvac else "ideal_loads_demand",
    }
