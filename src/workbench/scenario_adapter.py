"""Thin Part G adapter for immutable thermostat and weather variants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openstudio

import model_builder as mb


THERMOSTAT_NAME = "Termostat Vivienda CTE (pipeline)"


def _load_model(path: Path):
    translator = openstudio.osversion.VersionTranslator()
    loaded = translator.loadModel(openstudio.toPath(str(path)))
    if loaded.isNull():
        raise ValueError(f"OpenStudio model could not be loaded: {path}")
    return loaded.get()


def _thermostat(model):
    matches = [
        item for item in model.getThermostatSetpointDualSetpoints()
        if item.nameString() == THERMOSTAT_NAME
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one {THERMOSTAT_NAME!r} thermostat, found {len(matches)}"
        )
    return matches[0]


def _day_values(day_schedule) -> list[float]:
    return [float(value) for value in day_schedule.values()]


def _ruleset_state(schedule) -> dict[str, Any]:
    ruleset = schedule.to_ScheduleRuleset()
    if ruleset.isNull():
        raise TypeError("Part G thermostat schedule must be a ScheduleRuleset")
    ruleset = ruleset.get()
    winter_defaulted = ruleset.isWinterDesignDayScheduleDefaulted()
    summer_defaulted = ruleset.isSummerDesignDayScheduleDefaulted()
    return {
        "name": ruleset.nameString(),
        "default": _day_values(ruleset.defaultDaySchedule()),
        "rules": [_day_values(rule.daySchedule()) for rule in ruleset.scheduleRules()],
        "winter_defaulted": winter_defaulted,
        "winter": None if winter_defaulted else _day_values(ruleset.winterDesignDaySchedule()),
        "summer_defaulted": summer_defaulted,
        "summer": None if summer_defaulted else _day_values(ruleset.summerDesignDaySchedule()),
    }


def _schedule_state(model) -> dict[str, dict[str, Any]]:
    thermostat = _thermostat(model)
    heating = thermostat.heatingSetpointTemperatureSchedule()
    cooling = thermostat.coolingSetpointTemperatureSchedule()
    if heating.isNull() or cooling.isNull():
        raise RuntimeError("Part G thermostat has no heating or cooling schedule")
    return {
        "heating": _ruleset_state(heating.get()),
        "cooling": _ruleset_state(cooling.get()),
    }


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= 1e-9


def _same_values(left: list[float], right: list[float]) -> bool:
    return len(left) == len(right) and all(_close(a, b) for a, b in zip(left, right))


def _audit_schedule(before: dict[str, Any], after: dict[str, Any], delta: float,
                    *, mode: str) -> dict[str, Any]:
    threshold = mb.COMFORT_HEAT_MIN_C if mode == "heating" else mb.COMFORT_COOL_MAX_C
    default_unchanged = _same_values(before["default"], after["default"])
    structure_unchanged = (
        len(before["rules"]) == len(after["rules"])
        and before["winter_defaulted"] == after["winter_defaulted"]
        and before["summer_defaulted"] == after["summer_defaulted"]
    )
    before_profiles = list(before["rules"])
    after_profiles = list(after["rules"])
    for key in ("winter", "summer"):
        if before[key] is not None or after[key] is not None:
            before_profiles.append(before[key] or [])
            after_profiles.append(after[key] or [])

    shifted = 0
    sentinel_count = 0
    expected_values = structure_unchanged and len(before_profiles) == len(after_profiles)
    sentinels_unchanged = expected_values
    if expected_values:
        for old_values, new_values in zip(before_profiles, after_profiles):
            if len(old_values) != len(new_values):
                expected_values = False
                sentinels_unchanged = False
                break
            for old, new in zip(old_values, new_values):
                in_band = old > threshold if mode == "heating" else old < threshold
                expected = old + delta if in_band else old
                expected_values = expected_values and _close(new, expected)
                if in_band and delta != 0.0:
                    shifted += 1
                elif not in_band:
                    sentinel_count += 1
                    sentinels_unchanged = sentinels_unchanged and _close(new, old)

    return {
        "delta_c": delta,
        "default_day_unchanged": default_unchanged,
        "schedule_structure_unchanged": structure_unchanged,
        "all_values_expected": expected_values,
        "sentinel_values_unchanged": sentinels_unchanged,
        "sentinel_value_count": sentinel_count,
        "shifted_value_count": shifted,
        "passed": (
            default_unchanged
            and structure_unchanged
            and expected_values
            and sentinels_unchanged
            and (delta == 0.0 or shifted > 0)
        ),
    }


def create_model_variant(osm_path, output_path, settings) -> dict[str, Any]:
    """Apply real Part G mutations to an exact parent OSM and save one variant."""
    source = Path(osm_path).resolve()
    destination = Path(output_path).resolve()
    model = _load_model(source)
    before = _schedule_state(model)
    heat_delta = float(settings.get("heat_delta_c", 0.0))
    cool_delta = float(settings.get("cool_delta_c", 0.0))

    mutation = mb.apply_comfort_offsets(
        model, heat_delta=heat_delta, cool_delta=cool_delta,
    )
    weather = None
    if settings.get("_weather_path"):
        weather = mb.set_weather_file(model, Path(settings["_weather_path"]))
    after = _schedule_state(model)

    destination.parent.mkdir(parents=True, exist_ok=True)
    if not model.save(openstudio.toPath(str(destination)), True):
        raise RuntimeError(f"Scenario model could not be saved: {destination}")

    heating_audit = _audit_schedule(before["heating"], after["heating"], heat_delta, mode="heating")
    cooling_audit = _audit_schedule(before["cooling"], after["cooling"], cool_delta, mode="cooling")
    return {
        "schema_version": 1,
        "model_path": str(destination),
        "mutation": mutation,
        "weather": weather,
        "audit": {
            "heating": heating_audit,
            "cooling": cooling_audit,
            "all_pass": heating_audit["passed"] and cooling_audit["passed"],
        },
    }
