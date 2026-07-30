"""Climate as a validated bundle, not a bare file path.

An EnergyPlus run takes its climate from more than one place, and only some of
them travel with the EPW:

* Latitude, longitude, elevation, time zone and standard pressure come from the
  EPW header.  OpenStudio writes no ``Site:Location`` when the model's Site is
  at defaults, so EnergyPlus reads them straight out of the weather file - swap
  the EPW and the site follows automatically.
* The **design days** do not.  They size the heating and cooling equipment, and
  until now they were hand-transcribed constants pinned to Valencia.  Loading a
  different EPW while leaving them alone gives a building that lives in one
  climate with equipment sized for another, and nothing detects it.

2026-07-28 showed how quietly that fails: an unset barometric pressure
(OpenStudio's 31 000 Pa default, roughly 9 000 m of altitude), a July instead of
an August cooling day and a sunless sky model moved the cooling design load by a
factor of 3.6 while the annual total still looked plausible.

So a climate here is one object that carries *everything* climate-dependent, is
read from files rather than typed, and refuses to exist if its parts disagree.

The design days are read from the ``.ddy`` that ships with the EPW.  They are
verified against the EPW's own ``DESIGN CONDITIONS`` header line, which states
the same numbers independently - two sources have to agree before a run starts.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openstudio


class ClimateError(ValueError):
    """A climate bundle is missing, inconsistent or internally contradictory."""


# Fields every bundle must state outright.  Ground and mains water temperature
# are climate-dependent but cannot be read from an EPW, so they are declared
# rather than derived - a bundle that omits them is rejected instead of quietly
# inheriting Valencia's values.
REQUIRED_KEYS = ("name", "epw", "ddy", "heating_design_day", "cooling_design_day",
                 "ground_temperature_c", "water_mains_temperature_c")

# Tolerances for the cross-checks.  They are deliberately tight: these are two
# statements of the same measurement, not two estimates of it.
SITE_LATLON_TOLERANCE_DEG = 0.05
SITE_ELEVATION_TOLERANCE_M = 5.0
DESIGN_TEMPERATURE_TOLERANCE_C = 0.15
DESIGN_WIND_TOLERANCE = 0.15
# Barometric pressure follows elevation by the ISA barometric formula; 2 % is
# wide enough for the difference between a station's measured mean and the
# standard atmosphere, narrow enough to catch a pressure left at a default.
PRESSURE_TOLERANCE_FRACTION = 0.02


@dataclass(frozen=True)
class ClimateSet:
    """One validated climate: weather file, design days and the site values."""

    name: str
    epw_path: Path
    epw_sha256: str
    ddy_path: Path
    ddy_sha256: str
    heating_design_day: str
    cooling_design_day: str
    ground_temperature_c: float
    water_mains_temperature_c: float
    barometric_pressure_pa: float
    site: dict[str, Any]
    design_days: dict[str, dict[str, Any]]
    cross_check: str
    fingerprint: str

    def record(self) -> dict[str, Any]:
        """The provenance stamp written into every run."""
        return {
            "name": self.name,
            "fingerprint": self.fingerprint,
            "epw": self.epw_path.name, "epw_sha256": self.epw_sha256,
            "ddy": self.ddy_path.name, "ddy_sha256": self.ddy_sha256,
            "heating_design_day": self.heating_design_day,
            "cooling_design_day": self.cooling_design_day,
            "ground_temperature_c": self.ground_temperature_c,
            "water_mains_temperature_c": self.water_mains_temperature_c,
            "barometric_pressure_pa": self.barometric_pressure_pa,
            "site": self.site,
            "design_days": self.design_days,
            "cross_check": self.cross_check,
        }


# ---------------------------------------------------------------------------
# Reading the files
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_epw_header(epw_path: Path) -> dict[str, Any]:
    """Pull LOCATION and DESIGN CONDITIONS out of an EPW header.

    Only the first few lines are read; an EPW is 8760 rows long and none of
    them are needed here.
    """
    location: list[str] | None = None
    design: list[str] | None = None
    with open(epw_path, "r", encoding="latin-1") as handle:
        for _ in range(8):
            line = handle.readline()
            if not line:
                break
            fields = [f.strip() for f in line.split(",")]
            head = fields[0].upper()
            if head == "LOCATION":
                location = fields
            elif head == "DESIGN CONDITIONS":
                design = fields

    if not location or len(location) < 10:
        raise ClimateError(
            f"{epw_path.name}: no usable LOCATION line - this is not a valid EPW header")

    site = {
        "city": location[1],
        "country": location[3],
        "source": location[4],
        "wmo": location[5],
        "latitude": float(location[6]),
        "longitude": float(location[7]),
        "time_zone": float(location[8]),
        "elevation_m": float(location[9]),
    }
    return {"site": site, "design_conditions": design}


def _design_conditions_block(fields: list[str], keyword: str,
                             count: int) -> list[float] | None:
    """Numbers following `Heating` or `Cooling` in the DESIGN CONDITIONS line."""
    try:
        start = next(i for i, f in enumerate(fields) if f.strip().lower() == keyword)
    except StopIteration:
        return None
    values: list[float] = []
    for raw in fields[start + 1: start + 1 + count]:
        try:
            values.append(float(raw))
        except ValueError:
            break
    return values or None


def _value(raw: Any) -> float | None:
    """Unwrap an OpenStudio getter that may return a double or an OptionalDouble.

    Which one you get varies by field and by SDK version, so neither form is
    assumed.
    """
    if raw is None:
        return None
    if hasattr(raw, "is_initialized"):
        return float(raw.get()) if raw.is_initialized() else None
    return float(raw)


def read_design_days(ddy_path: Path) -> dict[str, dict[str, Any]]:
    """Reverse-translate a .ddy and return every design day it holds, by name.

    The .ddy is an IDF fragment, so OpenStudio's own translator reads it; the
    numbers therefore arrive exactly as EnergyPlus would see them, with no
    hand transcription in between.
    """
    loaded = openstudio.IdfFile.load(openstudio.toPath(str(ddy_path)),
                                     openstudio.IddFileType("EnergyPlus"))
    if not loaded.is_initialized():
        raise ClimateError(f"{ddy_path.name}: could not be parsed as an EnergyPlus .ddy")
    model = openstudio.energyplus.ReverseTranslator().translateWorkspace(
        openstudio.Workspace(loaded.get()))

    days: dict[str, dict[str, Any]] = {}
    for day in model.getDesignDays():
        entry = {
            "month": int(day.month()),
            "day": int(day.dayOfMonth()),
            "db": _value(day.maximumDryBulbTemperature()),
            "range": _value(day.dailyDryBulbTemperatureRange()),
            "wb": _value(day.wetBulbOrDewPointAtMaximumDryBulb()),
            "humidity_type": str(day.humidityConditionType()),
            "day_type": str(day.dayType()),
            "pressure": _value(day.barometricPressure()),
            "wind_speed": _value(day.windSpeed()),
            "wind_direction": _value(day.windDirection()),
            "solar_model": str(day.solarModelIndicator()),
        }
        if entry["solar_model"] == "ASHRAETau":
            entry["taub"] = _value(day.ashraeTaub())
            entry["taud"] = _value(day.ashraeTaud())
        else:
            entry["clearness"] = _value(day.skyClearness()) or 0.0
        days[day.nameString()] = entry

    site = model.getSite()
    days["__site__"] = {
        "latitude": float(site.latitude()),
        "longitude": float(site.longitude()),
        "elevation_m": float(site.elevation()),
        "time_zone": float(site.timeZone()),
    }
    return days


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------
def _standard_pressure_pa(elevation_m: float) -> float:
    """ISA barometric formula - what the pressure at this elevation should be."""
    return 101325.0 * (1.0 - 2.25577e-5 * elevation_m) ** 5.2559


def _check_site_agreement(epw_site: dict, ddy_site: dict, name: str) -> None:
    """The .ddy and the EPW must describe the same place.

    This is the gate that catches the most damaging mistake of all: an EPW and
    a .ddy from different stations, which produces a building simulated in one
    city with equipment sized for another.
    """
    problems = []
    for key, tolerance in (("latitude", SITE_LATLON_TOLERANCE_DEG),
                           ("longitude", SITE_LATLON_TOLERANCE_DEG),
                           ("elevation_m", SITE_ELEVATION_TOLERANCE_M)):
        delta = abs(epw_site[key] - ddy_site[key])
        if delta > tolerance:
            problems.append(f"{key}: EPW {epw_site[key]} vs .ddy {ddy_site[key]} "
                            f"(difference {delta:.3f}, allowed {tolerance})")
    if problems:
        raise ClimateError(
            f"climate '{name}': the EPW and the .ddy describe different sites, so "
            f"they are not a matched pair.\n  " + "\n  ".join(problems))


def _check_design_conditions(day: dict, block: list[float] | None,
                             kind: str, name: str) -> list[str]:
    """Compare a design day against the EPW header's own statement of it.

    The DESIGN CONDITIONS layout is fixed by the EPW data dictionary:
    heating gives coldest month, DB99.6, ... , mean coincident wind speed and
    prevailing direction last; cooling gives hottest month, daily range,
    DB0.4 %, mean coincident WB, ... , wind speed and direction.
    """
    if not block:
        return []
    issues: list[str] = []

    def compare(label: str, expected: float, actual: float, tolerance: float) -> None:
        if abs(expected - actual) > tolerance:
            issues.append(f"{kind} {label}: .ddy {actual} vs EPW header {expected}")

    if kind == "heating" and len(block) >= 15:
        compare("month", block[0], day["month"], 0.0)
        compare("dry bulb 99.6 %", block[1], day["db"], DESIGN_TEMPERATURE_TOLERANCE_C)
        compare("wind speed", block[13], day["wind_speed"], DESIGN_WIND_TOLERANCE)
        compare("wind direction", block[14], day["wind_direction"], 1.0)
    elif kind == "cooling" and len(block) >= 16:
        compare("month", block[0], day["month"], 0.0)
        compare("daily range", block[1], day["range"], DESIGN_TEMPERATURE_TOLERANCE_C)
        compare("dry bulb 0.4 %", block[2], day["db"], DESIGN_TEMPERATURE_TOLERANCE_C)
        if day["wb"] is not None:
            compare("mean coincident wet bulb", block[3], day["wb"],
                    DESIGN_TEMPERATURE_TOLERANCE_C)
        compare("wind speed", block[14], day["wind_speed"], DESIGN_WIND_TOLERANCE)
        compare("wind direction", block[15], day["wind_direction"], 1.0)
    return issues


def _check_pressure(pressure_pa: float, elevation_m: float, name: str) -> None:
    expected = _standard_pressure_pa(elevation_m)
    if abs(pressure_pa - expected) > expected * PRESSURE_TOLERANCE_FRACTION:
        raise ClimateError(
            f"climate '{name}': design-day barometric pressure {pressure_pa:.0f} Pa does "
            f"not match an elevation of {elevation_m:.0f} m (expected about "
            f"{expected:.0f} Pa). OpenStudio's default of 31 000 Pa - roughly 9 000 m "
            f"of altitude - looks exactly like this.")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _resolve(reference: str, bundle_path: Path) -> Path:
    """Find a file named by a bundle: absolute, next to the bundle, or in the project."""
    candidate = Path(reference)
    if candidate.is_absolute():
        return candidate
    tried = [bundle_path.parent / candidate]
    root = bundle_path.parent.resolve()
    for parent in [root, *root.parents]:
        if (parent / "data" / "weather").is_dir():
            tried.append(parent / candidate)
            break
    for option in tried:
        if option.exists():
            return option
    raise ClimateError(
        f"{bundle_path.name}: '{reference}' was not found. Tried: "
        + ", ".join(str(p) for p in tried))


def load_climate(bundle_path: str | Path) -> ClimateSet:
    """Load and fully validate a climate bundle.

    Every gate is a refusal rather than a warning: in an energy simulation a
    single wrong climate value moves the answer without moving anything that
    looks obviously broken.
    """
    bundle_path = Path(bundle_path)
    if not bundle_path.exists():
        raise ClimateError(f"climate bundle not found: {bundle_path}")
    try:
        spec = json.loads(bundle_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ClimateError(f"{bundle_path.name}: invalid JSON - {exc}") from exc

    missing = [key for key in REQUIRED_KEYS if key not in spec]
    if missing:
        raise ClimateError(
            f"{bundle_path.name}: missing required keys {missing}. Ground and mains "
            f"water temperature must be stated for every climate - they are "
            f"climate-dependent and cannot be read from an EPW, so they are never "
            f"inherited from another climate.")

    name = str(spec["name"])
    epw_path = _resolve(str(spec["epw"]), bundle_path)
    ddy_path = _resolve(str(spec["ddy"]), bundle_path)

    header = read_epw_header(epw_path)
    site = header["site"]
    all_days = read_design_days(ddy_path)
    ddy_site = all_days.pop("__site__")

    _check_site_agreement(site, ddy_site, name)

    design_days: dict[str, dict[str, Any]] = {}
    for kind, key in (("heating", "heating_design_day"),
                      ("cooling", "cooling_design_day")):
        wanted = str(spec[key])
        if wanted not in all_days:
            available = "\n  ".join(sorted(all_days))
            raise ClimateError(
                f"climate '{name}': {ddy_path.name} has no design day named "
                f"'{wanted}'.\nAvailable:\n  {available}")
        design_days[kind] = all_days[wanted]

    issues: list[str] = []
    for kind, keyword in (("heating", "heating"), ("cooling", "cooling")):
        block = _design_conditions_block(header["design_conditions"] or [], keyword,
                                         20)
        issues += _check_design_conditions(design_days[kind], block, kind, name)
    if issues:
        raise ClimateError(
            f"climate '{name}': the .ddy design days contradict the EPW's own "
            f"DESIGN CONDITIONS header, so one of the two files has been edited.\n  "
            + "\n  ".join(issues))
    cross_check = ("epw_design_conditions"
                   if header["design_conditions"] else
                   "skipped: this EPW has no DESIGN CONDITIONS header line")

    pressures = {design_days["heating"]["pressure"], design_days["cooling"]["pressure"]}
    if len(pressures) != 1:
        raise ClimateError(
            f"climate '{name}': the two design days state different barometric "
            f"pressures {sorted(pressures)} - they describe the same site and cannot.")
    pressure = pressures.pop()
    _check_pressure(pressure, site["elevation_m"], name)

    identity = {
        "name": name,
        "epw_sha256": _sha256(epw_path), "ddy_sha256": _sha256(ddy_path),
        "heating_design_day": spec["heating_design_day"],
        "cooling_design_day": spec["cooling_design_day"],
        "ground_temperature_c": float(spec["ground_temperature_c"]),
        "water_mains_temperature_c": float(spec["water_mains_temperature_c"]),
        "design_days": design_days,
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    return ClimateSet(
        name=name,
        epw_path=epw_path, epw_sha256=identity["epw_sha256"],
        ddy_path=ddy_path, ddy_sha256=identity["ddy_sha256"],
        heating_design_day=str(spec["heating_design_day"]),
        cooling_design_day=str(spec["cooling_design_day"]),
        ground_temperature_c=identity["ground_temperature_c"],
        water_mains_temperature_c=identity["water_mains_temperature_c"],
        barometric_pressure_pa=pressure,
        site=site, design_days=design_days,
        cross_check=cross_check, fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# The reference climate
# ---------------------------------------------------------------------------
def _project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "data" / "weather").is_dir():
            return parent
    raise ClimateError("project root not found (no data/weather directory above src/)")


VALENCIA_BUNDLE = _project_root() / "climates" / "valencia_iwec.json"

_cache: dict[str, ClimateSet] = {}


def valencia_iwec() -> ClimateSet:
    """The climate every verified number in this project was produced with.

    Cached: the bundle is read once per process, so the stock runner does not
    re-parse the .ddy for each of 26 452 buildings.
    """
    key = str(VALENCIA_BUNDLE)
    if key not in _cache:
        _cache[key] = load_climate(VALENCIA_BUNDLE)
    return _cache[key]


def main(argv: list[str] | None = None) -> int:
    """Inspect a bundle: `python src/climate.py climates/valencia_iwec.json`"""
    import argparse
    parser = argparse.ArgumentParser(description="Validate and print a climate bundle")
    parser.add_argument("bundle", nargs="?", default=str(VALENCIA_BUNDLE), type=Path)
    args = parser.parse_args(argv)
    try:
        climate = load_climate(args.bundle)
    except ClimateError as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(json.dumps(climate.record(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
