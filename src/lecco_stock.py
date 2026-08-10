"""Lecco stock: the EU building database turned into a file the engine can read.

This is a data adapter, not a second pipeline.  No physics lives here: the
geometry, envelope, occupancy, DHW, HVAC and EnergyPlus chain all stay in the
frozen modules.  What this module does is translate one dataset's shape into
another's, because Valencia and Lecco arrive in genuinely different forms:

    Valencia (cadastre)            Lecco (EU building database)
    -------------------            ----------------------------
    ESRI shapefile, flat columns   SpatiaLite, attributes inside JSON blobs
    `refparcela`                   `building_id`
    `altura_max` (storey count)    height in metres
    `cluster` -> TABULA_ES         GEM taxonomy -> TABULA_IT, priced in the file
    EPSG:25830                     EPSG:4326

The engine reads `refparcela`, `altura_max`, `cluster`, `pob_total`,
`num_vivend` and `ground_use` off a row.  This module
produces exactly those, plus the Italian U-values as columns.

Why the U-values travel as columns
----------------------------------
`deep_building.config_for_building` resolves an unpinned envelope through
`model_config.TABULA_ES` - the Spanish IVE table - which would give Italian
buildings Spanish walls.  The same function states the way out:

    a caller that has already pinned the envelope means it

so `stock_runner` pins each building's own U-values from the columns this
module writes.  `model_config.py` is hash-locked; adding an Italian table to it would
drift the verified Valencia profile, and Lecco is not worth that.

What is measured, and what is assumed
-------------------------------------
Taken from the database: footprint geometry, height, net floor area, GEM use
class, GEM period, GEM dwelling band, and the TABULA_IT U-values.

Assumed, because the database does not carry them.  Each one is a column in the
output so it can be traced and reported rather than buried:

* `pob_total` - there is no per-building population.  The municipal total is
  allocated in proportion to recorded dwelling area, so the stock sums to
  Lecco's real population instead of an invented density.
* `num_vivend` - `OCD` is unset for ~78 % of the residential stock; those are
  inferred from floor area.
* `period` - 79 % are dated `YPRE:1975`, which is a GHSL built-up epoch rather
  than a construction date.  See `PRE1975_PERIOD`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path

import geopandas as gpd
import pandas as pd

log = logging.getLogger("lecco_stock")

# Lecco is at 9.4 E: UTM zone 32N.  Metres are not cosmetic - footprint area,
# party-wall overlap length and the 50 m context radius are planar measurements.
LECCO_CRS = "EPSG:32632"

# ISTAT resident population, comune di Lecco.  Scales the occupancy allocation.
LECCO_POPULATION = 47_500

MEAN_DWELLING_AREA_M2 = 100.0     # only where OCD gives no dwelling band
STOREY_HEIGHT_M = 3.0             # Italian residential floor-to-floor

# `net_floor_area` is this exact multiple of `gross_floor_area` for every
# building in the file - measured, not assumed.  Recorded so the gross figure
# can be recovered for reporting, and as evidence that the net one is derived.
NET_TO_GROSS_RATIO = 0.7368

RESIDENTIAL_OCC = ("RES", "RES1", "RES3", "RES4")

# 79 % of Lecco's residential buildings are dated `YPRE:1975` - GHSL's first
# built-up epoch, spanning three TABULA bands.  1946-1969 is chosen as the
# largest Italian housing cohort, and the choice is close to inconsequential:
# across the three pre-1975 bands the residential wall U-value spans
# 1.10-1.51 W/m2K, less than the spread the assumption is standing in for.
# Every building it applied to is flagged `period_assumed`.
PRE1975_PERIOD = "YBET:1946-1969"

# Dwelling count -> (family name, TABULA class fragment).  These three are the
# residential classes the database actually prices.
DWELLING_CLASSES = (
    ("SFH", "RES1+DWE:1", 1, 1),
    ("MFH", "RES1+DWEBET:2-4", 2, 4),
    ("AB", "RES1+DWEBET:5-", 5, 10_000),
)


class LeccoStockError(RuntimeError):
    """The source database cannot produce a usable stock file."""


def load_tabula_it(db_path: Path) -> dict[str, dict[str, float]]:
    """`{tabula_string: {wall_u, roof_u, floor_u, window_u}}`, read from the file.

    Classes whose attributes are null - this export leaves the TABULA
    SFH/MFH/TH/AB series unpriced - are dropped rather than defaulted.  A
    building that lands on one is refused later, not quietly given a guess.
    """
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT json_extract(taxonomy,'$.tabula_string'), attributes "
            "FROM taxonomies WHERE category = 1").fetchall()

    table: dict[str, dict[str, float]] = {}
    for tabula_string, attributes in rows:
        if not tabula_string or not attributes:
            continue
        try:
            values = json.loads(attributes)
            table[str(tabula_string)] = {
                "wall_u": float(values["U_WALL"]),
                "roof_u": float(values["U_ROOF"]),
                "floor_u": float(values["U_FLOOR"]),
                "window_u": float(values["U_WINDOW"]),
            }
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
    if not table:
        raise LeccoStockError(
            f"{db_path.name}: taxonomies prices no TABULA class; without "
            "U-values the stock has no envelope")
    log.info("[tabula] %d priced TABULA_IT classes", len(table))
    return table


def _parse_json(raw) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def period_from_dat(dat: str | None) -> tuple[str, bool]:
    """GEM `DAT` -> (TABULA period band, was it assumed)."""
    text = str(dat).strip() if dat else ""
    if not text:
        return PRE1975_PERIOD, True

    year: int | None = None
    if text.startswith("Y:"):
        year = _int_or_none(text[2:])
    elif text.startswith("YBET:"):
        year = _int_or_none(text[5:].split("-")[0])
    elif text.startswith("YPRE:"):
        edge = _int_or_none(text[5:])
        if edge is None or edge >= 1975:
            # The GHSL epoch: genuinely unresolved.
            return PRE1975_PERIOD, True
        return ("YPRE:1945" if edge <= 1945 else PRE1975_PERIOD), edge > 1945

    if year is None:
        return PRE1975_PERIOD, True
    for limit, band in ((1945, "YPRE:1945"), (1969, "YBET:1946-1969"),
                        (1979, "YBET:1970-1979"), (1989, "YBET:1980-1989"),
                        (1999, "YBET:1990-1999"), (2010, "YBET:2000-2010")):
        if year <= limit:
            return band, False
    return "YBET:2011-2020", False


def _int_or_none(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def dwellings_from_ocd(ocd: str | None,
                       net_floor_area: float | None) -> tuple[float, str]:
    """Dwelling count and its provenance.  `OCD` is a band, not a number."""
    text = str(ocd).strip() if ocd else ""
    if text.startswith("DWE:"):
        value = _int_or_none(text[4:])
        if value is not None:
            return float(value), "ocd_exact"
    if text.startswith("DWEBET:"):
        band = text[7:]
        if band.startswith("2-4") or band == "2-":
            return 3.0, "ocd_band"          # midpoint of 2-4
        if band.startswith("5-"):
            return 6.0, "ocd_band"
    area = float(net_floor_area or 0.0)
    if area > 0:
        return max(1.0, float(round(area / MEAN_DWELLING_AREA_M2))), "area_inferred"
    return 1.0, "assumed_single"


def family_for(dwellings: float) -> tuple[str, str]:
    for short, fragment, low, high in DWELLING_CLASSES:
        if low <= dwellings <= high:
            return short, fragment
    return DWELLING_CLASSES[-1][0], DWELLING_CLASSES[-1][1]


def storeys_from_height(height_m: float | None) -> tuple[int, bool]:
    """Height in metres -> `altura_max`, the storeys ABOVE the ground floor.

    The builder always adds a ground level and then `altura_max` more, and
    rejects anything below 1, so the smallest expressible building is
    ground + 1.  A height implying a single storey is modelled as two and
    flagged rather than dropped.
    """
    try:
        height = float(height_m)
    except (TypeError, ValueError):
        height = 0.0
    if height <= 0:
        return 1, True
    total = max(1, int(round(height / STOREY_HEIGHT_M)))
    return (1, True) if total < 2 else (total - 1, False)


def extract(db_path: Path, out_path: Path, *,
            population: int = LECCO_POPULATION,
            crs: str = LECCO_CRS,
            include_mixed: bool = False) -> gpd.GeoDataFrame:
    """Read the database, derive the engine's columns, write the GeoPackage.

    `population` and `crs` are arguments rather than constants because nothing
    in the translation below is specific to one city: the EU building database
    has the same shape wherever it covers, and what changes between cities is
    the resident total to allocate and the metric projection to measure in.
    Lecco's values remain the defaults so existing callers are unaffected.
    """
    db_path, out_path = Path(db_path), Path(out_path)
    tabula = load_tabula_it(db_path)

    frame = gpd.read_file(db_path, layer="entities")
    log.info("[read] %d entities from %s", len(frame), db_path.name)
    if frame.crs is None:
        raise LeccoStockError(f"{db_path.name}: entities layer has no CRS")

    taxonomy = frame["taxonomy"].apply(_parse_json)
    attributes = frame["attributes"].apply(_parse_json)
    for key in ("OCC", "DAT", "OCD"):
        frame[key] = taxonomy.apply(lambda d, k=key: d.get(k))
    frame["net_floor_area"] = pd.to_numeric(
        attributes.apply(lambda d: d.get("net_floor_area")), errors="coerce")
    frame["height_m"] = pd.to_numeric(
        attributes.apply(lambda d: d.get("height_3d_extrusion")), errors="coerce")

    occ = frame["OCC"].fillna("").astype(str)
    residential = occ.isin(RESIDENTIAL_OCC)
    if include_mixed:
        residential |= occ.str.startswith("MIX") & occ.str.contains("RES")
    stock = frame[residential & frame["building_id"].notna()].copy()
    log.info("[filter] %d residential buildings (include_mixed=%s)",
             len(stock), include_mixed)
    if stock.empty:
        raise LeccoStockError("no residential buildings found")

    stock = stock.to_crs(crs)
    stock["footprint_m2"] = stock.geometry.area

    stock["refparcela"] = stock["building_id"].astype(str)
    if stock["refparcela"].duplicated().any():
        raise LeccoStockError(
            "building_id is not unique; the deep chain addresses one building "
            "by reference and cannot resolve duplicates")

    storeys = stock["height_m"].apply(storeys_from_height)
    stock["altura_max"] = [value[0] for value in storeys]
    stock["storeys_raised"] = [value[1] for value in storeys]

    periods = stock["DAT"].apply(period_from_dat)
    stock["period"] = [value[0] for value in periods]
    stock["period_assumed"] = [value[1] for value in periods]

    dwellings = [dwellings_from_ocd(row.OCD, row.net_floor_area)
                 for row in stock.itertuples()]
    stock["num_vivend"] = [value[0] for value in dwellings]
    stock["dwellings_source"] = [value[1] for value in dwellings]

    families = stock["num_vivend"].apply(family_for)
    stock["family"] = [value[0] for value in families]
    stock["tabula_string"] = [
        f"IT.N.{fragment}.{period}.Gen"
        for (_, fragment), period in zip(families, stock["period"])]
    stock["cluster"] = [f"{family}_{period}" for family, period
                        in zip(stock["family"], stock["period"])]

    missing = sorted(set(stock["tabula_string"]) - set(tabula))
    if missing:
        raise LeccoStockError(
            f"the database prices no envelope for {missing}; refusing to "
            "substitute a default")
    for column in ("wall_u", "roof_u", "floor_u", "window_u"):
        stock[column] = [tabula[key][column] for key in stock["tabula_string"]]

    # ---- deliberately NOT written: `tipo15_res_area_m2` ---------------------
    # The deep chain uses that column to decide how many storeys are dwellings
    # and converts the rest to commercial floor.  In Valencia it holds the
    # cadastral Tipo15 dwelling surface - a real, per-building survey figure.
    # This database has no equivalent, and passing one of its floor areas off as
    # one produces a fabricated mixed-use split.  Measured 2026-08-06:
    #
    #   * `net_floor_area` is exactly 0.7368 x `gross_floor_area` for every
    #     building, in total and at the median - a fixed multiplier, so it is
    #     derived and carries no information the gross figure does not.
    #   * `gross_floor_area / footprint` sits at 2.85 at both the 50th and the
    #     75th percentile - a default storey count, so it is derived too.
    #   * The source already states the use: these rows were selected on
    #     `OCC in RES/RES1/RES3/RES4`, and buildings with commercial floor carry
    #     their own `MIX(...)` codes and were filtered out. Converting storeys
    #     to commercial here would contradict the classification we selected on.
    #
    # Feeding the net figure did exactly that: city-wide the conditioned area
    # came out 1.523x the residential area, against roughly 1.2 in Valencia,
    # and 61 % of buildings had storeys reclassified. Leaving the column out
    # makes the engine stamp `mixed_use_basis: unchecked_no_tipo15`, which is
    # the honest description of what we know.
    #
    # Both source areas travel as plainly-named columns for reporting and for
    # the coverage accounting, where they are floor-area weights and nothing more.
    stock["eu_net_floor_area_m2"] = stock["net_floor_area"].fillna(0.0)
    stock["eu_gross_floor_area_m2"] = (stock["net_floor_area"].fillna(0.0)
                                       / NET_TO_GROSS_RATIO).round(1)

    # No per-building population exists, so allocate the municipal total by
    # recorded dwelling area.  The stock then sums to Lecco's real population.
    # The net figure is used as a WEIGHT here, which the fixed net/gross ratio
    # leaves unaffected - proportions are identical either way.
    area = stock["eu_net_floor_area_m2"].clip(lower=0.0)
    total_area = float(area.sum())
    if total_area <= 0:
        raise LeccoStockError("no recorded dwelling area; occupancy cannot be allocated")
    stock["pob_total"] = (area / total_area * float(population)).round(1)
    stock["occupancy_source"] = "area_weighted_municipal_population"
    log.info("[occupancy] %s residents over %.0f m2 = %.1f m2/person",
             f"{population:,}", total_area, total_area / population)

    # Italian residential buildings normally have dwellings at street level,
    # unlike the Valencia stock where Rai's regime made the ground a commercial
    # buffer.  The engine already supports both; this states which one applies.
    stock["ground_use"] = "residential"
    stock["ground_use_source"] = "lecco_residential_default"

    stock = stock[["refparcela", "cluster", "family", "period", "period_assumed",
                   "tabula_string", "altura_max", "storeys_raised", "footprint_m2",
                   "eu_net_floor_area_m2", "eu_gross_floor_area_m2", "num_vivend",
                   "dwellings_source", "pob_total", "occupancy_source",
                   "ground_use", "ground_use_source",
                   "wall_u", "roof_u", "floor_u", "window_u",
                   "height_m", "OCC", "DAT", "geometry"]]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    stock.to_file(out_path, driver="GPKG", layer="lecco_stock")
    log.info("[write] %d buildings -> %s", len(stock), out_path)
    return stock


def summarise(stock: gpd.GeoDataFrame) -> str:
    lines = [
        f"buildings            {len(stock):>8,}",
        f"net floor area       {stock['eu_net_floor_area_m2'].sum():>8,.0f} m2",
        f"gross floor area     {stock['eu_gross_floor_area_m2'].sum():>8,.0f} m2",
        f"allocated residents  {stock['pob_total'].sum():>8,.0f}",
        f"period assumed       {int(stock['period_assumed'].sum()):>8,} "
        f"({stock['period_assumed'].mean() * 100:.1f} %)",
        f"storeys raised to 2  {int(stock['storeys_raised'].sum()):>8,}",
        "",
        f"{'cluster':<26}{'n':>6}  {'wall_u':>7}{'roof_u':>8}{'window_u':>10}",
    ]
    grouped = stock.groupby("cluster").agg(
        n=("refparcela", "size"), wall_u=("wall_u", "first"),
        roof_u=("roof_u", "first"), window_u=("window_u", "first"),
    ).sort_values("n", ascending=False)
    for cluster, row in grouped.iterrows():
        lines.append(f"{cluster:<26}{int(row['n']):>6}  {row['wall_u']:>7.3f}"
                     f"{row['roof_u']:>8.3f}{row['window_u']:>10.3f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn the Lecco EU building database into an engine-ready stock")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path,
                        default=Path("data/gis/lecco/lecco_stock.gpkg"))
    parser.add_argument("--population", type=int, default=LECCO_POPULATION)
    parser.add_argument("--crs", default=LECCO_CRS,
                        help="metric CRS to measure in (UTM zone for the city)")
    parser.add_argument("--include-mixed", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        stock = extract(args.db, args.out, population=args.population,
                        crs=args.crs, include_mixed=args.include_mixed)
    except (LeccoStockError, OSError) as exc:
        print(f"REFUSED: {exc}")
        return 2
    print()
    print(summarise(stock))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
