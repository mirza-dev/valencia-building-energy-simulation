"""Drive an EnergyPlus event run from a PALM microclimate slice.

A microclimate slice is one PALM job's post-processed output: a directory of
georeferenced rasters describing what the air actually did over a real city on
a real day.  This module turns that into something the frozen simulation chain
can consume, without touching the frozen chain.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not paste PALM's absolute temperatures into the weather file.  PALM was
driven by boundary conditions this project never saw, on a calendar day that has
no counterpart in a TMY weather year; splicing one into the other would produce
a number that belongs to neither.  What the slice does carry, and carries well,
is the *spatial* structure: at a single instant, one part of the city is several
kelvin warmer than another.  That difference is what gets extracted here.

So every building is described by an offset from its own domain, not by an
absolute temperature:

    delta(building) = Ta(cells around the building) - Ta(domain reference)

and that offset is applied to the weather file the city already runs on.  PALM
supplies the spatial pattern, the EPW supplies the temporal statistics, and
neither is asked to do the other's job.

THE TWO FIELDS, AND WHAT LIES BETWEEN THEM
------------------------------------------
A slice stores per-cell extrema over its time series, not a full time history:
one field at the domain-wide temperature maximum and one at the minimum.  Two
instants is enough to place a building on a warm-to-cool axis, and not enough to
describe its whole day.  Between them the offset is interpolated using the EPW's
*own* diurnal curve, so the shape of the day comes from measured climatology and
only its spatial spread comes from PALM.

Outside the hours the slice covers, the offset is held at the cooler field's
value.  That is an assumption, not a measurement - a slice that stops in the
evening cannot describe the night - and it is recorded as such in the
provenance, because night-time is exactly when the urban heat island is usually
strongest and this data cannot speak to it.

WHY THE OFFSET IS APPLIED AT CONSTANT DEW POINT
-----------------------------------------------
Urban warming of this kind is sensible heat: the fabric releases stored energy,
it does not add water.  Dry bulb therefore moves and dew point does not, which
means relative humidity has to be recomputed rather than carried over unchanged.
Leaving the stale humidity in the file would hand EnergyPlus three mutually
contradictory columns.

Everything read is hashed, and the resulting identity is folded into a
fingerprint that is stamped on every run, in the same way `climate.py` stamps
its own.  A slice cannot be swapped underneath a set of results unnoticed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import run_simulation as sim

log = logging.getLogger(__name__)

# The height above ground the slice is read at.  PALM writes several; 2 m is the
# canonical near-surface reference and the one that describes the air a facade
# actually sees.  The taller cuts are kept for cross-checking the gradient - they
# are NOT summed with it, because EnergyPlus applies its own wind and temperature
# height profile above the weather-station reference and would count it twice.
DEFAULT_HEIGHT_TOKEN = "2m"

# Search radii, in metres, for finding valid cells around a footprint.  PALM
# masks building cells, so a building smaller than the grid can sit entirely
# inside masked ground; the sampler grows the ring until it finds real air.
SAMPLE_RADII_M = (0.0, 10.0, 20.0, 30.0, 50.0)

# Offsets are rounded before an event weather file is derived from them, so that
# buildings sharing a thermal environment share a file instead of each writing
# their own near-identical copy.  0.05 K is far below the uncertainty of the
# underlying field and well below any effect on annual energy.
DELTA_QUANTUM_K = 0.05

# Days of unmodified weather simulated before the event, so the construction
# thermal mass reaches a state that belongs to the season rather than to
# EnergyPlus's initialisation.
DEFAULT_SPINUP_DAYS = 6
DEFAULT_TRAILING_DAYS = 1

_EPW_HEADER_LINES = 8
_EPW_DRYBULB = 6
_EPW_DEWPOINT = 7
_EPW_RELHUM = 8
_EPW_MISSING_DRYBULB = 99.9


class MicroclimateError(ValueError):
    """A slice is missing, malformed, or does not describe this city."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _time_label(path: Path) -> str:
    """`ta_max__15_30.tif` -> `15:30`; the instant the field was taken."""
    match = re.search(r"__(\d{2})_(\d{2})\.tif$", path.name)
    if not match:
        raise MicroclimateError(f"{path.name}: no time stamp in the file name")
    return f"{match.group(1)}:{match.group(2)}"


def _find_one(directory: Path, prefix: str) -> Path:
    """The single raster whose name starts with `prefix`, or a clear failure.

    Deliberately strict.  Two candidates means the slice layout is not what this
    module was written against, and silently taking the first would pick a field
    by alphabetical accident.
    """
    if not directory.is_dir():
        raise MicroclimateError(f"missing directory: {directory}")
    found = sorted(p for p in directory.glob(f"{prefix}*.tif")
                   if not p.name.endswith(".aux.xml"))
    if not found:
        raise MicroclimateError(f"{directory}: no raster named {prefix}*.tif")
    if len(found) > 1:
        names = ", ".join(p.name for p in found)
        raise MicroclimateError(
            f"{directory}: expected one {prefix}*.tif, found {len(found)}: {names}")
    return found[0]


def _covered_hours(horizontal: Path, height_token: str, meta: dict,
                   base_label: str, peak_label: str) -> tuple[tuple[str, str], str]:
    """The hours the slice actually delivers, which is not always what it claims.

    `meta.json` describes the PALM *run*, and a run can be post-processed into a
    narrower set of outputs than it simulated.  The Lecco slice declares a window
    beginning at 04:00 while the frames it ships begin at 10:00 - six hours of
    the claim have no data behind them.  Believing the declaration would let a
    report say the night was covered when the field for it was never written, so
    the frame file names are treated as the record and the declaration is kept
    only as a note about the disagreement.
    """
    frames = horizontal / height_token / "thermal" / "hourly_frames" / "ta"
    observed = sorted(p.stem.replace("_", ":") for p in frames.glob("*.png")) \
        if frames.is_dir() else []
    declared = (str(meta.get("start_local_iso", ""))[11:16],
                str(meta.get("end_local_iso", ""))[11:16])

    if not observed:
        span = (min(base_label, peak_label), max(base_label, peak_label))
        return span, ("no frame series shipped; the window is inferred from the "
                      "two stored fields alone")

    span = (observed[0], observed[-1])
    if declared[0] and declared[0] != span[0]:
        return span, (f"meta.json declares the run started at {declared[0]} but "
                      f"the earliest field shipped is {span[0]}; the delivered "
                      "data is what is recorded here")
    return span, "matches the declared run window"


@dataclass(frozen=True)
class MicroclimateSlice:
    """One validated PALM slice, ready to offset a weather file with."""

    name: str
    root: Path
    height_token: str
    crs: str
    cell_size_m: float
    peak_path: Path
    peak_sha256: str
    peak_label: str
    base_path: Path
    base_sha256: str
    base_label: str
    domain_peak_c: float
    domain_base_c: float
    peak_spread_k: float
    valid_cell_fraction: float
    covered_hours: tuple[str, str]
    coverage_note: str
    meta: dict[str, Any]
    fingerprint: str

    def record(self) -> dict[str, Any]:
        """The provenance stamp written into every run that uses this slice."""
        return {
            "name": self.name,
            "fingerprint": self.fingerprint,
            "height_token": self.height_token,
            "crs": self.crs,
            "cell_size_m": self.cell_size_m,
            "peak_raster": self.peak_path.name,
            "peak_sha256": self.peak_sha256,
            "peak_label_local": self.peak_label,
            "base_raster": self.base_path.name,
            "base_sha256": self.base_sha256,
            "base_label_local": self.base_label,
            "domain_peak_c": round(self.domain_peak_c, 3),
            "domain_base_c": round(self.domain_base_c, 3),
            "peak_spread_p05_p95_k": round(self.peak_spread_k, 3),
            "valid_cell_fraction": round(self.valid_cell_fraction, 4),
            "covered_hours_local": list(self.covered_hours),
            "coverage_note": self.coverage_note,
            "reference_statistic": "spatial median of valid domain cells",
            "night_offset_assumption":
                "held at the cooler field's value; the slice does not cover night",
            "absolute_temperatures_used": False,
        }


def load_slice(root: str | Path, *,
               height_token: str = DEFAULT_HEIGHT_TOKEN) -> MicroclimateSlice:
    """Validate one slice directory and hash everything it will be read from.

    Fails closed.  Anything this module cannot confirm - a raster without a
    coordinate system, a domain with no valid air cells, a time stamp it cannot
    read - stops the load rather than being defaulted, because each of those
    silently produces a plausible-looking but meaningless offset field.
    """
    import rasterio

    root = Path(root)
    horizontal = root / "horizontal" if (root / "horizontal").is_dir() else root
    meta_path = horizontal / "meta.json"
    if not meta_path.is_file():
        raise MicroclimateError(f"{root}: no horizontal/meta.json - not a PALM slice")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    thermal = horizontal / height_token / "thermal" / "snapshots"
    peak_path = _find_one(thermal, "ta_max__")
    base_path = _find_one(thermal, "ta_min__")

    fields: dict[str, np.ndarray] = {}
    crs_seen: set[str] = set()
    cell_sizes: set[float] = set()
    for key, path in (("peak", peak_path), ("base", base_path)):
        with rasterio.open(path) as source:
            if source.crs is None:
                raise MicroclimateError(
                    f"{path.name}: no coordinate system; the slice cannot be "
                    "placed on the ground")
            crs_seen.add(str(source.crs))
            cell_sizes.add(round(float(source.res[0]), 3))
            fields[key] = source.read(1, masked=True).filled(np.nan)

    if len(crs_seen) != 1:
        raise MicroclimateError(f"{root}: rasters disagree on CRS: {sorted(crs_seen)}")
    if len(cell_sizes) != 1:
        raise MicroclimateError(
            f"{root}: rasters disagree on cell size: {sorted(cell_sizes)}")
    if fields["peak"].shape != fields["base"].shape:
        raise MicroclimateError(
            f"{root}: the two fields are on different grids "
            f"({fields['peak'].shape} vs {fields['base'].shape})")

    peak_valid = fields["peak"][np.isfinite(fields["peak"])]
    base_valid = fields["base"][np.isfinite(fields["base"])]
    if peak_valid.size == 0 or base_valid.size == 0:
        raise MicroclimateError(f"{root}: a field has no valid cells at all")

    valid_fraction = float(peak_valid.size) / float(fields["peak"].size)
    domain_peak = float(np.median(peak_valid))
    domain_base = float(np.median(base_valid))
    spread = float(np.percentile(peak_valid, 95) - np.percentile(peak_valid, 5))

    peak_label = _time_label(peak_path)
    base_label = _time_label(base_path)
    covered, coverage_note = _covered_hours(horizontal, height_token, meta,
                                            base_label, peak_label)

    identity = {
        "case": meta.get("case"),
        "run_label_local": meta.get("run_label_local"),
        "height_token": height_token,
        "crs": sorted(crs_seen)[0],
        "cell_size_m": sorted(cell_sizes)[0],
        "peak_raster": peak_path.name, "peak_sha256": _sha256(peak_path),
        "base_raster": base_path.name, "base_sha256": _sha256(base_path),
        "domain_peak_c": round(domain_peak, 6),
        "domain_base_c": round(domain_base, 6),
        "reference_statistic": "spatial median of valid domain cells",
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    name = str(meta.get("case") or root.name)
    log.info("[slice] %s %s: %d valid cells (%.1f %%), spatial spread %.2f K, "
             "reference %.2f C at %s",
             name, height_token, peak_valid.size, 100 * valid_fraction,
             spread, domain_peak, peak_label)

    return MicroclimateSlice(
        name=name, root=root, height_token=height_token,
        crs=identity["crs"], cell_size_m=identity["cell_size_m"],
        peak_path=peak_path, peak_sha256=identity["peak_sha256"],
        peak_label=peak_label,
        base_path=base_path, base_sha256=identity["base_sha256"],
        base_label=base_label,
        domain_peak_c=domain_peak, domain_base_c=domain_base,
        peak_spread_k=spread, valid_cell_fraction=valid_fraction,
        covered_hours=covered, coverage_note=coverage_note,
        meta=meta, fingerprint=fingerprint)


def sample_stock(slice_: MicroclimateSlice, stock, *,
                 radii_m: tuple[float, ...] = SAMPLE_RADII_M) -> pd.DataFrame:
    """Per-building temperature offsets, with the evidence behind each one.

    PALM masks the cells a building occupies, so a footprint smaller than the
    grid can sit entirely in masked ground.  The sampler therefore grows a ring
    around the footprint until it finds real air, and reports how far it had to
    go: a building resolved at 0 m is standing in its own measured air, one
    resolved at 50 m is being described by its neighbourhood.  Buildings the
    domain does not reach are returned with a null offset and a reason, never
    with a zero - a zero offset is a physical claim and absence of data is not.
    """
    import rasterio

    if stock.crs is None:
        raise MicroclimateError("the stock layer has no CRS")

    with rasterio.open(slice_.peak_path) as source:
        peak = source.read(1, masked=True).filled(np.nan)
        transform, width, height = source.transform, source.width, source.height
        bounds = source.bounds
    with rasterio.open(slice_.base_path) as source:
        base = source.read(1, masked=True).filled(np.nan)

    local = stock.to_crs(slice_.crs)
    inverse = ~transform
    rows = []

    for reference, geometry in zip(local["refparcela"], local.geometry):
        if geometry is None or geometry.is_empty:
            rows.append((reference, None, None, None, 0, "no geometry"))
            continue

        minx, miny, maxx, maxy = geometry.bounds
        if maxx < bounds.left or minx > bounds.right \
                or maxy < bounds.bottom or miny > bounds.top:
            rows.append((reference, None, None, None, 0, "outside slice domain"))
            continue

        resolved = None
        for radius in radii_m:
            area = geometry.buffer(radius) if radius else geometry
            ax0, ay0, ax1, ay1 = area.bounds
            cols_a, rows_a = inverse * (ax0, ay1)
            cols_b, rows_b = inverse * (ax1, ay0)
            c0, c1 = sorted((int(math.floor(cols_a)), int(math.ceil(cols_b))))
            r0, r1 = sorted((int(math.floor(rows_a)), int(math.ceil(rows_b))))
            c0, r0 = max(c0, 0), max(r0, 0)
            c1, r1 = min(c1, width), min(r1, height)
            if c1 <= c0 or r1 <= r0:
                continue

            window_peak = peak[r0:r1, c0:c1]
            window_base = base[r0:r1, c0:c1]
            valid = np.isfinite(window_peak) & np.isfinite(window_base)
            if not valid.any():
                continue
            resolved = (float(np.median(window_peak[valid])),
                        float(np.median(window_base[valid])),
                        float(radius), int(valid.sum()))
            break

        if resolved is None:
            rows.append((reference, None, None, None, 0,
                         "no valid cell within the search radius"))
            continue

        peak_c, base_c, radius, count = resolved
        rows.append((reference,
                     peak_c - slice_.domain_peak_c,
                     base_c - slice_.domain_base_c,
                     radius, count, "ok"))

    frame = pd.DataFrame(rows, columns=[
        "refparcela", "delta_peak_k", "delta_base_k",
        "sample_radius_m", "sample_cells", "sample_status"])
    inside = frame["sample_status"] == "ok"
    log.info("[sample] %d of %d buildings resolved inside the slice "
             "(median offset %.2f K, range %.2f to %.2f K)",
             int(inside.sum()), len(frame),
             float(frame.loc[inside, "delta_peak_k"].median()) if inside.any() else 0.0,
             float(frame.loc[inside, "delta_peak_k"].min()) if inside.any() else 0.0,
             float(frame.loc[inside, "delta_peak_k"].max()) if inside.any() else 0.0)
    return frame


# --------------------------------------------------------------------------
# Weather file derivation
# --------------------------------------------------------------------------

def _saturation_pressure_pa(temperature_c: float) -> float:
    """Magnus over water; used only to keep the humidity column consistent."""
    return 610.94 * math.exp(17.625 * temperature_c / (temperature_c + 243.04))


def read_epw(path: Path) -> tuple[list[str], pd.DataFrame]:
    """The eight header lines and the 8,760 data rows, kept as raw fields."""
    lines = path.read_text(encoding="latin-1").splitlines()
    header, body = lines[:_EPW_HEADER_LINES], lines[_EPW_HEADER_LINES:]
    rows = [line.split(",") for line in body if line.strip()]
    if len(rows) not in (8760, 8784):
        raise MicroclimateError(
            f"{path.name}: {len(rows)} data rows, expected 8760 or 8784")
    return header, pd.DataFrame(rows)


def hottest_window(frame: pd.DataFrame, *, spinup_days: int = DEFAULT_SPINUP_DAYS,
                   trailing_days: int = DEFAULT_TRAILING_DAYS
                   ) -> tuple[int, int, int, int, int]:
    """The weather year's own hottest day, and a run period bracketing it.

    Returns `(begin_month, begin_day, end_month, end_day, peak_day_of_year)`.

    The slice describes a hot summer afternoon, so it is applied to the hottest
    day this weather file actually contains rather than to the calendar date
    PALM happened to run - the two years have no relation to each other.
    """
    drybulb = pd.to_numeric(frame[_EPW_DRYBULB], errors="coerce")
    usable = drybulb.where(drybulb < _EPW_MISSING_DRYBULB)
    month = pd.to_numeric(frame[1], errors="coerce").astype(int)
    day = pd.to_numeric(frame[2], errors="coerce").astype(int)
    key = month * 100 + day
    daily_peak = usable.groupby(key).max()
    hottest = int(daily_peak.idxmax())

    index = pd.Index(sorted(key.unique()))
    position = int(index.get_loc(hottest))
    begin = index[max(position - spinup_days, 0)]
    end = index[min(position + trailing_days, len(index) - 1)]
    log.info("[event] hottest day in the weather file is %02d-%02d at %.1f C; "
             "run period %02d-%02d to %02d-%02d",
             hottest // 100, hottest % 100, daily_peak.max(),
             begin // 100, begin % 100, end // 100, end % 100)
    # Plain ints, not numpy scalars: this window is serialised to JSON when the
    # run plan is handed to the worker processes, and a numpy integer survives
    # that trip as a string, which then compares unequal against every date in
    # the file and silently selects nothing.
    return (int(begin // 100), int(begin % 100),
            int(end // 100), int(end % 100), int(hottest))


def _diurnal_weight(day_values: pd.Series) -> pd.Series:
    """Where each hour sits between the day's own coldest and warmest hour.

    This is what carries the offset from the cooler field to the peak field.
    The slice gives two instants; the weather file gives the shape between them.
    """
    low, high = day_values.min(), day_values.max()
    if not math.isfinite(low) or not math.isfinite(high) or high - low < 0.1:
        return pd.Series(0.0, index=day_values.index)
    return ((day_values - low) / (high - low)).clip(0.0, 1.0)


def write_event_epw(base_epw: Path, out_dir: Path, *,
                    delta_peak_k: float, delta_base_k: float,
                    window: tuple[int, int, int, int, int]) -> Path:
    """A weather file with the building's own offset applied, cached by content.

    Only the run period's hours are altered; the rest of the year is left byte
    for byte as it was, so the same file remains usable for anything else and
    the difference against the original is auditable with a plain diff.

    Written atomically, because the stock runner drives several worker processes
    that will ask for the same offsets at the same time.
    """
    peak_q = round(delta_peak_k / DELTA_QUANTUM_K) * DELTA_QUANTUM_K
    base_q = round(delta_base_k / DELTA_QUANTUM_K) * DELTA_QUANTUM_K
    tag = hashlib.sha256(
        f"{base_epw.name}|{peak_q:+.3f}|{base_q:+.3f}|{window}".encode()
    ).hexdigest()[:16]
    target = out_dir / f"event_{tag}.epw"
    if target.is_file():
        return target

    header, frame = read_epw(base_epw)
    month = pd.to_numeric(frame[1], errors="coerce").astype(int)
    day = pd.to_numeric(frame[2], errors="coerce").astype(int)
    drybulb = pd.to_numeric(frame[_EPW_DRYBULB], errors="coerce")
    dewpoint = pd.to_numeric(frame[_EPW_DEWPOINT], errors="coerce")

    begin_month, begin_day, end_month, end_day, _ = window
    key = month * 100 + day
    in_window = (key >= begin_month * 100 + begin_day) & \
                (key <= end_month * 100 + end_day)

    new_drybulb = drybulb.copy()
    new_dewpoint = dewpoint.copy()
    new_relhum = pd.to_numeric(frame[_EPW_RELHUM], errors="coerce").copy()

    for day_key in sorted(key[in_window].unique()):
        mask = key == day_key
        values = drybulb[mask].where(drybulb[mask] < _EPW_MISSING_DRYBULB)
        weight = _diurnal_weight(values)
        offset = base_q + weight * (peak_q - base_q)
        shifted = values + offset
        # Sensible heat only: the dew point does not move, so relative humidity
        # has to be recomputed or the file would carry three columns that
        # contradict each other.  Where a negative offset would push dry bulb
        # under the dew point, the dew point follows it down to saturation
        # rather than the row becoming non-physical.
        dew = dewpoint[mask].where(dewpoint[mask] < _EPW_MISSING_DRYBULB)
        dew = dew.combine(shifted, lambda d, t: min(d, t) if pd.notna(d) and pd.notna(t) else d)
        relative = 100.0 * dew.combine(
            shifted,
            lambda d, t: (_saturation_pressure_pa(d) / _saturation_pressure_pa(t))
            if pd.notna(d) and pd.notna(t) else np.nan)
        keep = shifted.notna()
        new_drybulb.loc[mask & keep] = shifted[keep].round(1)
        new_dewpoint.loc[mask & dew.notna()] = dew[dew.notna()].round(1)
        new_relhum.loc[mask & relative.notna()] = \
            relative[relative.notna()].clip(0, 100).round(0)

    # Reformat only the rows that were actually touched.  Rewriting the whole
    # column would re-render every untouched hour through Python's float
    # formatting - numerically identical, textually different ("2.70" -> "2.7")
    # - and a diff against the source would then show eight thousand spurious
    # changes, burying the two hundred real ones.
    out = frame.copy()
    touched = in_window.to_numpy()
    for column, series, fmt in ((_EPW_DRYBULB, new_drybulb, "{:.1f}"),
                                (_EPW_DEWPOINT, new_dewpoint, "{:.1f}"),
                                (_EPW_RELHUM, new_relhum, "{:.0f}")):
        rendered = series[touched].map(
            lambda v: fmt.format(v) if pd.notna(v) else "")
        out.loc[touched, column] = rendered

    out_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(",".join(row) for row in out.itertuples(index=False, name=None))
    temporary = out_dir / f".{target.name}.{os.getpid()}"
    temporary.write_text("\n".join(header) + "\n" + body + "\n", encoding="latin-1")
    os.replace(temporary, target)
    return target


def observed_outdoor_air(sql_path: Path) -> dict[str, float] | None:
    """What the simulation actually saw outdoors, read back from its own output.

    The event weather file keeps the source header untouched, so the station
    name EnergyPlus reports is identical for the baseline and the offset run and
    cannot tell them apart.  The temperatures themselves can, which is why the
    daily outdoor dry bulb is requested and read back: it is the one place the
    offset is visible from inside the finished run.
    """
    import sqlite3

    with sqlite3.connect(sql_path) as connection:
        rows = connection.execute(
            "SELECT rd.Value FROM ReportData rd "
            "JOIN ReportDataDictionary d ON d.ReportDataDictionaryIndex = "
            "rd.ReportDataDictionaryIndex "
            "WHERE d.Name = 'Site Outdoor Air Drybulb Temperature'").fetchall()
    values = [float(r[0]) for r in rows if r and r[0] is not None]
    if not values:
        return None
    return {"outdoor_max_c": round(max(values), 2),
            "outdoor_mean_c": round(sum(values) / len(values), 2),
            "outdoor_samples": len(values)}


def request_outdoor_air_output(osm) -> None:
    """Ask for the daily outdoor dry bulb, so the offset can be verified later."""
    import openstudio

    for existing in osm.getOutputVariables():
        if existing.variableName() == "Site Outdoor Air Drybulb Temperature":
            existing.setReportingFrequency("Daily")
            return
    variable = openstudio.model.OutputVariable(
        "Site Outdoor Air Drybulb Temperature", osm)
    variable.setReportingFrequency("Daily")


def read_site_energy_precise(sql_path: Path, area_m2: float) -> dict[str, float]:
    """Site energy without the annual reader's rounding.

    `deep_building.read_end_uses_split` rounds to two decimals, which is the
    right precision for a year and the wrong one for a week: an eight-day run
    lands near 1 kWh/m2, where two decimals quantise the answer into steps
    roughly a tenth of the effect being measured.  A baseline-versus-event
    difference read at that precision would be mostly rounding.

    This reads the same tabular cells through the same frozen helpers and simply
    does not round.  It is a presentation-precision change, not a second opinion
    on the physics - the accounting, including the DHW subcategory split, stays
    with the frozen reader, and both sets of numbers are reported.
    """
    import sqlite3

    with sqlite3.connect(sql_path) as connection:
        def kwh(row_name: str, column: str) -> float:
            value = sim._tabular_value(
                connection, "AnnualBuildingUtilityPerformanceSummary",
                "End Uses", row_name, column)
            return (value or 0.0) * sim.GJ_TO_KWH

        electricity = kwh("Total End Uses", "Electricity")
        gas = kwh("Total End Uses", "Natural Gas")
        cooling = kwh("Cooling", "Electricity")
        fans = kwh("Fans", "Electricity")
        heating_elec = kwh("Heating", "Electricity")

    return {"total_site_kwh_m2_precise": (electricity + gas) / area_m2,
            "cooling_kwh_m2_precise": cooling / area_m2,
            "fans_kwh_m2_precise": fans / area_m2,
            "heating_elec_kwh_m2_precise": heating_elec / area_m2}


# Unmet hours allowed over an event window, as a fraction of the window's hours.
#
# Measured, not assumed.  Three Lecco buildings were compared between their
# annual run and this event week: 42.0 -> 11.0, 52.7 -> 15.5 and 58.8 -> 20.0
# unmet cooling hours, so 26-34 % of a whole year's unmet hours fall inside this
# single week - a concentration of twelve to sixteen times over a flat
# pro-rating.  Scaling the annual allowance by days/365 therefore fails correct
# models, which is what it did on first run.
#
# Expressed against the window instead: those healthy buildings sit at 6-10 % of
# the window's hours.  The gate is set at 25 %, two and a half to four times
# observed healthy behaviour, which still catches the failure it exists for - a
# system that is not conditioning the building at all approaches 100 %.
EVENT_UNMET_HOURS_FRACTION = 0.25


def event_unmet_allowance(days: int) -> float:
    """Unmet-hour limit for an event window of this length."""
    return EVENT_UNMET_HOURS_FRACTION * days * 24.0


def event_qa(checks: list[dict], results: dict, *, days: int,
             baseline: dict | None = None) -> list[dict]:
    """Quality gates that mean something over a week instead of a year.

    The frozen chain's plausibility band is annual (1-200 kWh/m2), and an
    eight-day run lands near 2 - it would fail a gate that was never about it.
    Rather than widen the annual band and lose it for the annual runs, the band
    is restated per day here, and two checks are added that only an event run
    can make: that the run covered the days it was asked to, and that the
    microclimate offset actually reached the simulation.

    That last one is the reason the runs come in pairs.  An identical model over
    an identical period with an identical weather file except for the offset
    must not return an identical answer; if it does, the offset never landed,
    and every conclusion drawn from it would be an artefact.
    """
    kept = [c for c in checks
            if c["check"] != "plausible_band_total_site_kwh_m2"]
    per_day = results["total_site_kwh_m2"] / max(days, 1)
    # An occupied dwelling runs roughly 0.02-1.0 kWh/m2 per day across the year;
    # a hot-week slice sits in the upper part of that and must not be zero.
    kept.append({"check": "plausible_band_daily_site_kwh_m2",
                 "model": round(per_day, 4), "eplus": "[0.02-1.0]",
                 "tolerance": "-", "passed": 0.02 <= per_day <= 1.0})
    kept.append({"check": "event_days_simulated", "model": days,
                 "eplus": ">=2", "tolerance": "-", "passed": days >= 2})
    if baseline is not None:
        # Compared at full precision: the rounded annual figures can agree by
        # quantisation alone over a week, which would let a run that never
        # received its offset pass as one that did.
        key = ("total_site_kwh_m2_precise"
               if "total_site_kwh_m2_precise" in results else "total_site_kwh_m2")
        moved = abs(results[key] - baseline[key]) > 1e-9
        kept.append({"check": "microclimate_offset_reached_simulation",
                     "model": round(results[key], 6),
                     "eplus": round(baseline[key], 6),
                     "tolerance": "must differ", "passed": moved})
    return kept


def apply_event_run_period(osm, window: tuple[int, int, int, int, int]) -> dict:
    """Restrict the model to the event window.

    An additive mutation on a model the frozen builder has already finished,
    which is the pattern `deep_building.add_pthp_hvac` and
    `model_builder.apply_comfort_offsets` already use.  The builder itself is
    hash-locked and is not touched.
    """
    begin_month, begin_day, end_month, end_day, _ = window
    period = osm.getRunPeriod()
    period.setBeginMonth(int(begin_month))
    period.setBeginDayOfMonth(int(begin_day))
    period.setEndMonth(int(end_month))
    period.setEndDayOfMonth(int(end_day))
    return {"begin": f"{begin_month:02d}-{begin_day:02d}",
            "end": f"{end_month:02d}-{end_day:02d}"}
