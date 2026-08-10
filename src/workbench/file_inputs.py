"""Validation and pairing for the four files exposed by the product UI.

This module contains no building physics.  It turns uploaded files into the
same validated inputs the command-line stock runner already consumes.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

import climate


TIPO15_REQUIRED_COLUMNS = (
    "31_pc", "252_planta", "428_uso", "442_sup_Residencial",
)

# The city-neutral boundary, written down.  These are the fields the frozen
# engine reads off a row, so a file carrying them can be simulated whatever
# city produced it - `lecco_stock.py` exists to map one source onto exactly
# this shape.  The names are Spanish because `model_builder`/`deep_building`
# read them by name and are hash-locked; renaming would drift the verified
# profile for a cosmetic gain.  They are roles, not a claim about the country:
# `refparcela` = building identity, `altura_max` = storey count.
STOCK_REQUIRED_COLUMNS = (
    "refparcela", "altura_max", "pob_total", "num_vivend",
)

# Either a cluster the Spanish TABULA table can resolve, or a complete pinned
# envelope.  A half-pinned envelope is refused rather than completed from the
# Spanish table: that is the exact path by which an Italian building would get
# Spanish walls without anyone being told.
STOCK_PINNED_ENVELOPE_COLUMNS = ("wall_u", "roof_u", "window_u")


def _metric_crs_or_refuse(frame, path: Path) -> str:
    """Planar metres, or nothing.

    Footprint area, party-wall overlap length and the 50 m context radius are
    all planar measurements.  A stock in degrees produces areas around 1e-8 and
    silently excludes every building on the footprint gate, which reads as "this
    city has no usable buildings" rather than as a projection mistake.
    """
    crs = getattr(frame, "crs", None)
    if crs is None:
        raise ValueError(
            f"{path.name} declares no coordinate reference system; a stock file "
            "must be in a projected CRS whose unit is the metre"
        )
    try:
        units = {axis.unit_name for axis in crs.axis_info}
    except AttributeError:                       # pragma: no cover - old pyproj
        units = set()
    if crs.is_geographic or not units <= {"metre", "meter"}:
        raise ValueError(
            f"{path.name} is in {crs.name!r}, whose units are "
            f"{sorted(units) or ['degree']}. Areas, party walls and the "
            "neighbour radius are planar measurements in metres: reproject the "
            "stock to a metric CRS (UTM for the city) before uploading it."
        )
    return str(crs.srs)


def inspect_stock(path: Path) -> dict[str, Any]:
    """Refuse a stock file the engine could not simulate, and say why.

    Fail-closed in the style of `inspect_tipo15`: every rule below is one the
    engine enforces anyway, moved to upload time so a person is told at once
    instead of watching a run produce thousands of identical failures.
    """
    import geopandas as gpd

    try:
        frame = gpd.read_file(path)
    except Exception as exc:                     # noqa: BLE001 - any driver error
        raise ValueError(f"{path.name} could not be read as a spatial dataset: {exc}") from exc
    if frame.empty:
        raise ValueError(f"{path.name} contains no buildings")

    missing = [name for name in STOCK_REQUIRED_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError(
            f"{path.name} is missing required column(s) {', '.join(missing)}. "
            f"A stock file must carry {', '.join(STOCK_REQUIRED_COLUMNS)}."
        )

    crs = _metric_crs_or_refuse(frame, path)

    pinned = [name for name in STOCK_PINNED_ENVELOPE_COLUMNS if name in frame.columns]
    has_cluster = "cluster" in frame.columns and frame["cluster"].notna().any()
    if len(pinned) == len(STOCK_PINNED_ENVELOPE_COLUMNS):
        envelope_source = "pinned"
    elif pinned:
        raise ValueError(
            f"{path.name} pins only {', '.join(pinned)}. Pin all of "
            f"{', '.join(STOCK_PINNED_ENVELOPE_COLUMNS)} or none: a partial "
            "envelope would be completed from the Spanish TABULA table, which "
            "is how a building outside Spain silently acquires Spanish walls."
        )
    elif has_cluster:
        envelope_source = "tabula_es"
    else:
        raise ValueError(
            f"{path.name} carries neither a usable 'cluster' column nor a pinned "
            f"envelope ({', '.join(STOCK_PINNED_ENVELOPE_COLUMNS)}). One of the "
            "two must state where the construction properties come from."
        )

    references = frame["refparcela"].astype("string").fillna("").str.strip()
    duplicates = int(len(references) - references[references.ne("")].nunique())
    return {
        "buildings": int(len(frame)),
        "unique_references": int(references[references.ne("")].nunique()),
        "duplicate_reference_rows": duplicates,
        "crs": crs,
        "envelope_source": envelope_source,
        "has_district_column": "nombre" in frame.columns,
        "has_ground_use": "ground_use" in frame.columns,
        "has_floor_u": "floor_u" in frame.columns,
        "has_cadastral_area": "tipo15_res_area_m2" in frame.columns,
        "columns": sorted(str(name) for name in frame.columns),
        "contract": "stock-v1",
    }


def inspect_tipo15(path: Path) -> dict[str, Any]:
    """Refuse a CSV that cannot drive occupancy, ground use and area evidence."""
    try:
        frame = pd.read_csv(
            path, sep=";", encoding="latin-1", usecols=list(TIPO15_REQUIRED_COLUMNS),
            dtype={"31_pc": "string", "252_planta": "string", "428_uso": "string"},
            low_memory=False,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(
            "Tipo15 must be a semicolon-delimited Latin-1 CSV containing "
            f"{', '.join(TIPO15_REQUIRED_COLUMNS)}: {exc}"
        ) from exc
    if frame.empty:
        raise ValueError("Tipo15 CSV contains no dwelling records")
    references = frame["31_pc"].fillna("").str.strip()
    if not references.ne("").any():
        raise ValueError("Tipo15 column 31_pc contains no cadastral references")
    area = pd.to_numeric(frame["442_sup_Residencial"], errors="coerce")
    usable_area = area.notna() & area.gt(0)
    if not usable_area.any():
        raise ValueError("Tipo15 column 442_sup_Residencial contains no positive numeric area")
    return {
        "rows": int(len(frame)),
        "columns": list(TIPO15_REQUIRED_COLUMNS),
        "unique_parcels": int(references[references.ne("")].nunique()),
        "usable_area_rows": int(usable_area.sum()),
        "usable_area_coverage_pct": round(float(usable_area.mean() * 100.0), 3),
        "residential_area_m2": round(float(area[usable_area].sum()), 3),
        "contract": "tipo15-v1",
    }


def inspect_weather(path: Path) -> dict[str, Any]:
    """Read the source EnergyPlus reads and require a complete annual EPW."""
    header = climate.read_epw_header(path)
    with open(path, "r", encoding="latin-1") as handle:
        line_count = sum(1 for _ in handle)
    data_rows = max(0, line_count - 8)
    if data_rows < 8760:
        raise ValueError(
            f"EPW contains {data_rows} hourly rows; a full annual file needs at least 8760"
        )
    return {
        "site": header["site"],
        "design_conditions_present": bool(header.get("design_conditions")),
        "hourly_rows": data_rows,
        "contract": "epw-annual-v1",
    }


def _design_day_candidates(days: dict[str, dict[str, Any]], day_type: str) -> list[str]:
    return sorted(
        name for name, item in days.items()
        if name != "__site__" and item.get("day_type") == day_type
    )


def inspect_ddy(path: Path) -> dict[str, Any]:
    days = climate.read_design_days(path)
    heating = _design_day_candidates(days, "WinterDesignDay")
    cooling = _design_day_candidates(days, "SummerDesignDay")
    if not heating or not cooling:
        raise ValueError(
            ".ddy must contain at least one WinterDesignDay and one SummerDesignDay"
        )
    return {
        "site": days["__site__"],
        "heating_design_days": heating,
        "cooling_design_days": cooling,
        "design_day_count": len(days) - 1,
        "contract": "ddy-design-days-v1",
    }


def _preferred_day(names: list[str], *, heating: bool) -> str:
    """Choose the standard annual sizing day without inventing temperatures."""
    def score(name: str) -> tuple[int, str]:
        upper = name.upper()
        points = 0
        if heating:
            points += 8 if "99.6" in upper else 0
            points += 3 if "HTG" in upper or "HEATING" in upper else 0
            points += 6 if "CONDNS DB" in upper else 0
            points -= 20 if "WIND" in upper or "HUM_" in upper else 0
        else:
            points += 8 if ".4%" in upper or "0.4%" in upper else 0
            points += 3 if "CLG" in upper or "COOLING" in upper else 0
            points += 2 if "MWB" in upper else 0
        points += 1 if "DB" in upper else 0
        return points, name

    return max(names, key=score)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reference_pair(epw: Path, ddy: Path) -> bool:
    """Is this byte-identical to the climate the project was verified on?

    Everything the reference pair is allowed to keep - its name and its two
    stated site temperatures - hangs off this one question, so it is asked in
    exactly one place.
    """
    try:
        reference = climate.valencia_iwec()
    except (climate.ClimateError, OSError, ValueError):
        # A source-only checkout without the reference bundle still uploads fine.
        return False
    return _digest(epw) == reference.epw_sha256 and _digest(ddy) == reference.ddy_sha256


def _bundle_name(epw: Path, ddy: Path, city: str) -> str:
    """Byte-identical uploads of the reference pair keep the reference name.

    `climate_fingerprint` hashes this name together with the two file digests,
    and it is one of the runner's resume identity fields.  Naming a second
    bundle differently for the same two files would split one physical climate
    into two identities: a run started from an upload could not resume one
    produced from the project's own bundle, and the finished Benicalap run
    shipped with the product would read as a different climate.  The bundle's
    `epw`/`ddy` paths still record which copy was activated.
    """
    if _is_reference_pair(epw, ddy):
        return climate.valencia_iwec().name
    return f"managed_{city}"


def _ground_temperatures_by_depth(epw: Path) -> dict[float, list[float]]:
    """The EPW's own GROUND TEMPERATURES line, parsed by depth.

    Layout: the count of depth blocks, then for each block a depth followed by
    three optional soil properties and twelve monthly means.
    """
    with open(epw, "r", encoding="latin-1") as handle:
        for _ in range(8):
            line = handle.readline()
            if not line:
                break
            fields = [f.strip() for f in line.split(",")]
            if fields[0].upper() != "GROUND TEMPERATURES":
                continue
            depths: dict[float, list[float]] = {}
            cursor = 2
            while cursor + 16 <= len(fields):
                try:
                    depth = float(fields[cursor])
                    monthly = [float(v) for v in fields[cursor + 4: cursor + 16]]
                except ValueError:
                    break
                depths[depth] = monthly
                cursor += 16
            return depths
    return {}


def propose_site_temperatures(epw: Path) -> dict[str, Any]:
    """What to offer an operator activating a climate for a new city.

    Neither value can be read out of an EPW directly, and they are the two ways
    Valencia's verified profile could reach another city unnoticed, so both are
    proposed with their reasoning rather than inherited.
    """
    depths = _ground_temperatures_by_depth(epw)
    # Mains are buried around two metres; that depth's annual mean is the
    # temperature of the water arriving from the network.  This is the method
    # the Lecco bundle documents and it reproduces its stated 13.7 C.
    monthly = depths.get(2.0) or depths.get(0.5) or []
    derived = round(sum(monthly) / len(monthly), 1) if monthly else None
    return {
        "water_mains_temperature_c": derived,
        "water_mains_basis": (
            f"annual mean of this EPW's own ground temperature at "
            f"{2.0 if 2.0 in depths else 0.5} m"
            if monthly else "not derivable: this EPW has no GROUND TEMPERATURES line"
        ),
        # Site:GroundTemperature:BuildingSurface is the temperature of the
        # ground IN CONTACT WITH THE SLAB, which EnergyPlus documents as running
        # close to the average indoor temperature.  It follows the operating
        # regime - the template's thermostat schedules - not the weather, so the
        # undisturbed values in the EPW are the wrong number for this field and
        # are offered only as context.
        "ground_temperature_c": None,
        "ground_temperature_basis": (
            "not derivable from weather: this field is the ground in contact "
            "with the slab, set by the indoor regime. Declare it. Running the "
            "Spanish CTE template unchanged is the case for keeping 18.0."
        ),
        "undisturbed_ground_c": {
            str(depth): round(sum(values) / len(values), 2)
            for depth, values in sorted(depths.items())
        },
    }


def climate_bundle_spec(epw: Path, ddy: Path, *, name: str | None = None,
                        ground_temperature_c: float | None = None,
                        water_mains_temperature_c: float | None = None) -> dict[str, Any]:
    epw_meta = inspect_weather(epw)
    days = climate.read_design_days(ddy)
    heating_candidates = _design_day_candidates(days, "WinterDesignDay")
    cooling_candidates = _design_day_candidates(days, "SummerDesignDay")
    if not heating_candidates or not cooling_candidates:
        raise ValueError(
            ".ddy must contain at least one WinterDesignDay and one SummerDesignDay"
        )
    heating = _preferred_day(heating_candidates, heating=True)
    cooling = _preferred_day(cooling_candidates, heating=False)
    city = str(epw_meta["site"].get("city") or epw.stem).lower().replace(" ", "_")
    resolved_name = name or _bundle_name(epw, ddy, city)

    # The two fields no weather file can supply.  Whether they may be defaulted
    # depends entirely on whether this IS the pair the project was verified on.
    if _is_reference_pair(epw, ddy):
        # Byte-identical to the shipped reference: reproduce its verified values
        # exactly.  Deriving them here instead would give 17.2 C mains from this
        # very EPW, change `climate_fingerprint`, and leave every published
        # Valencia run unable to resume or compare against an uploaded copy of
        # its own climate.
        ground = 18.0 if ground_temperature_c is None else float(ground_temperature_c)
        mains = 10.0 if water_mains_temperature_c is None else float(water_mains_temperature_c)
    else:
        proposal = propose_site_temperatures(epw)
        mains = (float(water_mains_temperature_c)
                 if water_mains_temperature_c is not None
                 else proposal["water_mains_temperature_c"])
        if mains is None:
            raise ValueError(
                f"{epw.name} carries no GROUND TEMPERATURES line, so the mains "
                "temperature cannot be derived from it. State "
                "water_mains_temperature_c for this city."
            )
        if ground_temperature_c is None:
            raise ValueError(
                "ground_temperature_c must be declared for a climate this "
                "project was not verified against. It is the ground in contact "
                "with the slab - set by the indoor regime, not by the weather - "
                "so it cannot be read from the EPW, and inheriting Valencia's "
                "verified 18.0 C without saying so would put a Valencia number "
                "inside another city's results. Running the Spanish CTE "
                "template unchanged is the case for declaring 18.0."
            )
        ground = float(ground_temperature_c)

    return {
        "name": resolved_name,
        "epw": str(epw.resolve()),
        "ddy": str(ddy.resolve()),
        "heating_design_day": heating,
        "cooling_design_day": cooling,
        # Not recoverable from EPW/DDY, so they stay explicit in the generated
        # bundle: the run identity records what was assumed instead of a later
        # reader having to guess where these came from.
        "ground_temperature_c": ground,
        "water_mains_temperature_c": mains,
    }


def build_climate_bundle(epw: Path, ddy: Path, target_root: Path, *,
                         ground_temperature_c: float | None = None,
                         water_mains_temperature_c: float | None = None,
                         ) -> tuple[Path, dict[str, Any]]:
    """Create one content-addressed bundle and validate the pair end to end."""
    spec = climate_bundle_spec(
        epw, ddy, ground_temperature_c=ground_temperature_c,
        water_mains_temperature_c=water_mains_temperature_c)
    identity = hashlib.sha256(
        json.dumps(spec, sort_keys=True).encode("utf-8")
    ).hexdigest()
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / f"managed-{identity[:20]}.json"
    if not target.exists():
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target_root, suffix=".json", delete=False,
        ) as handle:
            json.dump(spec, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, target)
        target.chmod(0o444)
    record = climate.load_climate(target).record()
    return target, record
