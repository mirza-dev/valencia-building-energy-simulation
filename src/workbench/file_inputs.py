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
    try:
        reference = climate.valencia_iwec()
    except (climate.ClimateError, OSError, ValueError):
        # A source-only checkout without the reference bundle still uploads fine.
        return f"managed_{city}"
    if _digest(epw) == reference.epw_sha256 and _digest(ddy) == reference.ddy_sha256:
        return reference.name
    return f"managed_{city}"


def climate_bundle_spec(epw: Path, ddy: Path, *, name: str | None = None) -> dict[str, Any]:
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
    return {
        "name": name or _bundle_name(epw, ddy, city),
        "epw": str(epw.resolve()),
        "ddy": str(ddy.resolve()),
        "heating_design_day": heating,
        "cooling_design_day": cooling,
        # These two constants are part of the verified Valencia profile and are
        # not recoverable from EPW/DDY.  They remain explicit in the generated
        # bundle so a run identity records them rather than inheriting silently.
        "ground_temperature_c": 18.0,
        "water_mains_temperature_c": 10.0,
    }


def build_climate_bundle(epw: Path, ddy: Path, target_root: Path) -> tuple[Path, dict[str, Any]]:
    """Create one content-addressed bundle and validate the pair end to end."""
    spec = climate_bundle_spec(epw, ddy)
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
