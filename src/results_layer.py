"""The run's results as a layer somebody opens in QGIS.

A finished stock run leaves `ledger.jsonl` (one row per building) and
`aggregate.json` (the rollups).  Both carry the energy; neither carries the
geometry, so the one thing a reader usually wants from a building-by-building
result - a map of it - was the one thing the package could not be opened to.
The retired representative-cluster pipelines already wrote exactly this file
and said so in their own log (`results_buildings.gpkg (open in QGIS)`,
`city_pipeline.py:485`); the per-building runner never carried it across.  The
name is kept deliberately: continuity with what the project already published.

The join is the whole module.  Geometry comes from the prepared stock the run
actually simulated - not from the raw cadastre - so what is drawn is what was
modelled, and the identity fingerprints ride along on every feature: a layer
that ends up in somebody else's QGIS project can still say which run produced
it.

Two decisions are worth stating because they are what makes a total taken off
this layer correct:

  * **One feature per `refparcela`.**  A cadastral reference can hold several
    footprint polygons.  Joining the ledger onto the raw stock would repeat the
    building's energy once per polygon, and any sum over the layer would be
    wrong by exactly that multiplicity - the same trap `aggregate()` avoids
    with `drop_duplicates`.  The footprints are dissolved into one
    (Multi)Polygon instead, so the parcel keeps all of its geometry while one
    row carries one building's one result.
  * **Every status is drawn, not only `ok`.**  A building that failed or was
    excluded appears with its `status` and `reason` and NULL energies, which
    QGIS renders as a visible hole rather than as absence.  Dropping them would
    turn a coverage gap into a blank patch of map indistinguishable from
    "no building here" - and on this stock the gaps are not small (Lecco: 1,145
    of 5,152 screened out).  A NULL is a measurement that says "not measured";
    a missing feature says nothing at all.

Like `zoning_block` and `allocation_block`, this reports what the inputs
support and never raises: a run aggregated without its prepared stock has no
geometry to draw, and that must not cost the reader the whole `aggregate.json`.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

LAYER_FILENAME = "results_buildings.gpkg"
LAYER_NAME = "results"

# What the per-area numbers are *per*.  An annual run and a microclimate event
# run write the same column names, so the period has to be carried explicitly:
# measured 2026-08-12, an 8-day Lecco event produced 1.02-1.26 kWh/m2 in
# `total_site_kwh_m2` and the map labelled all three panels `kWh/m2·yr`.
ANNUAL_UNIT = "kWh/m²·yr"
EVENT_MODE = "microclimate_event"


def energy_period(rows: list[dict]) -> dict:
    """The period the energy columns cover, read from the rows themselves.

    A ledger written before the run mode was stamped is annual - that is what
    those runs were - so a missing field is not treated as unknown.  A ledger
    holding both modes is refused rather than resolved by majority: two runs
    concatenated by hand are not one run, and the caller is told so (the rule
    `allocation_block` follows for mixed profiles).
    """
    if not rows:
        # No numbers, so no period.  Calling an empty ledger annual would be a
        # claim about nothing.
        return {"period": "no_rows", "unit": None}
    # Deliberately every row, superseded ones included.  A review suggested
    # deduping by reference first, so that a `--retry-failed` could not leave a
    # stale row deciding the mode; it was tried and reverted, because two
    # ledgers of the SAME city concatenated by hand share their references, and
    # deduping made that pair collapse into whichever run was appended last -
    # silently reporting two runs as one, which is the exact thing this
    # function refuses to do.  Keeping the stale row can only ever raise the
    # alarm (`mixed`), never suppress it, and the caller is told to look.  The
    # inconsistency the review actually found - `aggregate()` answering from
    # deduped rows while the layer answered from raw ones - is closed at the
    # call site instead, by giving both the raw ledger (2026-08-12).
    modes = {str(row.get("run_mode") or "annual") for row in rows}
    if len(modes) > 1:
        return {"period": "mixed", "unit": None,
                "note": "this ledger holds more than one run mode; the energy "
                        "columns of the two are not per the same period"}
    mode = modes.pop() if modes else "annual"
    if mode != EVENT_MODE:
        return {"period": "annual", "unit": ANNUAL_UNIT}
    days = next((row.get("event_days") for row in rows
                 if row.get("event_days")), None)
    window = next((row.get("event_window") for row in rows
                   if row.get("event_window")), None)
    span = f"{int(days)}-day event" if days else "event window"
    return {"period": EVENT_MODE, "unit": f"kWh/m² over the {span}",
            "event_days": int(days) if days else None,
            "event_window": window,
            "note": "these are event-window totals, not annual: any field named "
                    "per-year (`total_site_co2_t_yr`, `hvac_co2_t_yr`) holds a "
                    "figure for this window only"}

# Bulk or path-valued row fields.  `traceback` is up to 1,200 characters of
# Python on every failed row and means nothing on a map; `model_osm` is a path
# into the run directory that stops being true the moment the file is copied
# somewhere else.  Everything else on the row is carried, including the
# fingerprints, so the layer stays self-describing.
EXCLUDED_FIELDS = ("traceback", "model_osm")

# Ordered first so the attribute table opens on what a reader came for.  Any
# other ledger field follows, sorted, so a new metric appears in the layer
# without this list having to learn about it.
LEADING_FIELDS = (
    "refparcela", "status", "reason", "cluster", "nombre",
    "total_site_kwh_m2", "space_heating_kwh_m2", "cooling_kwh_m2",
    "dhw_kwh_m2", "total_site_kwh", "total_site_kwh_per_person",
    "total_site_kwh_per_dwelling", "res_area_m2", "tipo15_res_area_m2",
    "total_site_co2_kg_m2", "qa_all_passed",
)

# What the building *is*, as opposed to what the run measured.  These live on
# the prepared stock, and a run whose ledger predates the runner writing
# `cluster` into its rows - Benicalap v8 is one - has typology and district
# nowhere else at all.  Without them the layer cannot answer "which typology"
# or "which district" although the answer is sitting in the file it was joined
# against.
#
# How duplicates collapse is not cosmetic, and it was measured rather than
# assumed.  A multi-footprint reference has one stock row per polygon; across
# those rows `cluster`, `nombre`, `num_vivend` and `altura_max` are repeated
# identically, but `pob_total` is *split* (0 + 15, 0 + 22 in Benicalap v8).
# Taking the first row would report a building with residents as unoccupied;
# summing `num_vivend` would double its dwellings.  So population sums and
# everything else takes the first value.
CONTEXT_FIELDS = ("cluster", "nombre", "pob_total", "num_vivend", "altura_max")
CONTEXT_SUMMED = ("pob_total",)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _column_order(rows: list[dict]) -> list[str]:
    present: set[str] = set()
    for row in rows:
        for key, value in row.items():
            if key in EXCLUDED_FIELDS or key == "geometry":
                continue
            if _is_scalar(value):
                present.add(key)
    leading = [name for name in LEADING_FIELDS if name in present]
    return leading + sorted(present - set(leading))


def _clean(value: Any) -> Any:
    """Keep a column one type.

    The GPKG driver types a column from its values, and a column that mixes
    text with NaN - which is what a partly-populated optional field looks like
    after pandas has seen it - is written inconsistently or refused outright.
    Booleans are left alone (OGR has a boolean type), numbers become float, and
    anything else becomes text.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and math.isnan(value) else float(value)
    return str(value)


# Rolled-up companions written into the same GeoPackage as extra layers, so
# one file dragged into QGIS offers the building map and the zone maps at once
# rather than three files that can drift apart.  A dissolved cluster is a
# scattered multipolygon and a dissolved district is a contiguous one; both are
# honest, they answer different questions.
ZONE_AXES = (("cluster", "by_cluster"), ("nombre", "by_district"))

# Averaged over floor area, never over buildings: a plain mean of intensities
# lets a 64 m2 house weigh as much as a 51,000 m2 block, which is the mistake
# `aggregate()` avoids by dividing summed energy by summed area.
ZONE_INTENSITIES = ("total_site_kwh_m2", "space_heating_kwh_m2",
                    "cooling_kwh_m2", "dhw_kwh_m2", "total_site_co2_kg_m2")


def build_zone_frames(frame):
    """Dissolve the building layer into per-cluster and per-district polygons.

    Every building is dissolved into its zone, including the ones that failed
    or were screened out, because the zone's outline is where the stock is -
    not where the model happened to succeed.  The energy columns come only from
    the buildings that produced one, and `buildings` / `buildings_measured`
    say how far apart those two are, so a zone whose colour rests on half its
    stock cannot be read as if it rested on all of it.
    """
    import pandas as pd

    frames: dict[str, Any] = {}
    for axis, layer in ZONE_AXES:
        if axis not in frame.columns:
            continue
        labelled = frame[frame[axis].notna() & (frame[axis].astype(str) != "")]
        if labelled.empty or labelled[axis].nunique() < 1:
            continue

        zones = labelled.dissolve(by=axis, as_index=False)[[axis, "geometry"]]
        # Only `ok` counts towards a zone total, exactly as `aggregate()` does.
        # A `failed_qa` row carries the full metric set - the metrics are
        # copied onto the row whatever its status - but they are numbers that
        # failed their own cross-check against EnergyPlus.  Admitting them here
        # made the same signed package's .gpkg and aggregate.json report
        # different totals for the same district, which is the one thing this
        # module exists to prevent (review finding, 2026-08-12; invisible in
        # v8, which had no QA rejection).
        measured = labelled
        if "status" in measured.columns:
            measured = measured[measured["status"].astype(str) == "ok"]
        measured = (measured[measured["total_site_kwh"].notna()]
                    if "total_site_kwh" in measured.columns
                    else measured.iloc[0:0])

        stats = []
        for label, group in labelled.groupby(axis):
            got = measured[measured[axis] == label]
            area = got["res_area_m2"].sum() if "res_area_m2" in got.columns else 0.0
            row = {axis: label,
                   "buildings": int(len(group)),
                   "buildings_measured": int(len(got)),
                   "res_area_m2": round(float(area), 1)}
            # Rebuilt from intensity x area rather than summed from the row's
            # own `total_site_kwh`, because that is what `aggregate()` does and
            # the two part company in the fifth decimal (0.0005 %).  Either is
            # defensible on its own; the same quantity printed two ways inside
            # one package is not, so the layer follows the published number.
            if "total_site_kwh_m2" in got.columns and area > 0:
                energy = float((got["total_site_kwh_m2"] * got["res_area_m2"]).sum())
                row["total_site_kwh"] = round(energy, 1)
                row["total_site_gwh"] = round(energy / 1e6, 5)
            for name in ZONE_INTENSITIES:
                if name in got.columns and area > 0:
                    weighted = float((got[name] * got["res_area_m2"]).sum()) / area
                    row[name] = round(weighted, 3)
            for name in ("pob_total", "num_vivend"):
                if name in group.columns:
                    row[name] = float(group[name].sum())
            stats.append(row)

        merged = zones.merge(pd.DataFrame(stats), on=axis, how="left")
        frames[layer] = merged.set_geometry("geometry")
    return frames


def _context_frame(stock):
    """One row per reference carrying the stock's descriptive fields, or None.

    Collapsed by `CONTEXT_SUMMED`: population is split across a reference's
    footprint rows and must be added back up, everything else is repeated and
    must not be.
    """
    import pandas as pd

    columns = getattr(stock, "columns", [])
    available = [name for name in CONTEXT_FIELDS if name in columns]
    if not available or "refparcela" not in columns:
        return None, []
    frame = pd.DataFrame(stock)[["refparcela", *available]].copy()
    frame["refparcela"] = frame["refparcela"].astype(str).str.strip()
    how = {name: ("sum" if name in CONTEXT_SUMMED else "first")
           for name in available}
    return frame.groupby("refparcela", as_index=False).agg(how), available


def _attach_context(joined, stock):
    """Fill the stock's descriptive fields in; never overwrite the ledger.

    Where the ledger already carries a value it stays, because the ledger is
    the record of what the run actually used and the stock is only context -
    the same rule, for the same reason, as
    `stock_adapter._cluster_lookup_for_run`.
    """
    context, available = _context_frame(stock)
    if context is None:
        return joined, []
    merged = joined.merge(context, on="refparcela", how="left",
                          suffixes=("", "_from_stock"))
    for name in available:
        borrowed = f"{name}_from_stock"
        if borrowed in merged.columns:
            merged[name] = merged[name].where(merged[name].notna(),
                                              merged[borrowed])
            merged = merged.drop(columns=[borrowed])
    return merged, available


def _attach_derived(frame):
    """Energy per resident and per dwelling - the household-scale question.

    Neither is in the ledger: the runner reports intensity per floor area,
    which ranks buildings against each other but says nothing about what a
    household uses, and that is the number a reader outside the model asks for.

    Both stay NULL where the denominator is zero rather than becoming 0.0.
    Ninety-three of Benicalap v8's buildings have no registered residents; a
    zero there would read as "uses no energy per person" when what is true is
    "there is nobody to divide by".
    """
    added = []
    if "total_site_kwh" not in frame.columns:
        return frame, added
    for name, denominator in (("total_site_kwh_per_person", "pob_total"),
                              ("total_site_kwh_per_dwelling", "num_vivend")):
        if denominator not in frame.columns:
            continue
        divisor = frame[denominator].where(frame[denominator] > 0)
        frame[name] = (frame["total_site_kwh"] / divisor).round(1)
        added.append(name)
    return frame, added


def build_frame(rows: list[dict], stock):
    """Join the ledger onto one dissolved footprint per reference.

    Returns `(GeoDataFrame, block)`.  The frame is None when there is nothing
    to draw; the block always describes what happened.
    """
    import geopandas as gpd
    import pandas as pd

    block: dict = {"written": False, "layer": LAYER_FILENAME}
    if not rows:
        block["reason"] = "no_rows"
        return None, block
    if stock is None or getattr(stock, "geometry", None) is None:
        # Aggregating a ledger on its own is a supported thing to do; it simply
        # cannot produce a map.  Say which of the two it is.
        block["reason"] = "no_stock_geometry"
        block["note"] = ("this aggregate was produced without the prepared "
                         "stock, so no geometry was available to join; re-run "
                         "`--aggregate` with `--stock` to write the layer")
        return None, block
    if "refparcela" not in getattr(stock, "columns", []):
        block["reason"] = "stock_has_no_refparcela"
        return None, block

    columns = _column_order(rows)
    frame = pd.DataFrame([{name: _clean(row.get(name)) for name in columns}
                          for row in rows])
    frame["refparcela"] = frame["refparcela"].astype(str).str.strip()
    # The ledger is append-only; `--retry-failed` deliberately writes a second
    # row for a building.  The layer must show the outcome that stands.
    frame = frame.drop_duplicates(subset=["refparcela"], keep="last")

    geometry = stock[["refparcela", "geometry"]].copy()
    geometry["refparcela"] = geometry["refparcela"].astype(str).str.strip()
    multi = int(geometry["refparcela"].duplicated().sum())
    if multi:
        geometry = geometry.dissolve(by="refparcela", as_index=False)

    joined = geometry.merge(frame, on="refparcela", how="inner")
    missing = int(len(frame) - len(joined))

    block.update({
        "features": int(len(joined)),
        "ledger_rows": int(len(frame)),
        "rows_without_geometry": missing,
        "footprints_dissolved": multi,
        "crs": None if joined.crs is None else str(joined.crs),
        "by_status": {str(k): int(v) for k, v in
                      frame["status"].value_counts().items()}
        if "status" in frame.columns else {},
        "geometry_rule": ("one feature per refparcela; multi-footprint parcels "
                          "are dissolved so a sum over the layer is not "
                          "multiplied by footprint count"),
        "status_rule": ("every ledger row is drawn; failed and excluded "
                        "buildings carry NULL energies so a coverage gap is "
                        "visible rather than absent"),
    })
    if missing:
        block["note"] = (f"{missing} ledger rows had no footprint in the "
                         "prepared stock and are not in the layer")
    if joined.empty:
        block["reason"] = "no_reference_matched_the_stock"
        return None, block

    joined, context = _attach_context(joined, stock)
    joined, derived = _attach_derived(joined)
    block["context_fields"] = context
    block["derived_fields"] = derived
    if derived:
        block["derived_rule"] = ("per-person and per-dwelling energy are NULL, "
                                 "not zero, where the building has no "
                                 "registered residents or dwellings")

    # Rebuilt from the frame rather than from the ledger's own columns, so the
    # fields borrowed from the stock and the two derived ones are ordered by
    # the same rule as everything else.
    present = [c for c in joined.columns if c != "geometry"]
    leading = [name for name in LEADING_FIELDS if name in present]
    ordered = leading + sorted(set(present) - set(leading))
    return gpd.GeoDataFrame(joined[[*ordered, "geometry"]],
                            geometry="geometry", crs=geometry.crs), block


STYLE_FILENAME = "results_buildings.qml"
STYLE_FIELD = "total_site_kwh_m2"

# ColorBrewer YlOrRd, and a grey that is deliberately not on that ramp: a
# building with no result must not look like a cool one.
_STYLE_RAMP = ("255,255,178", "254,204,92", "253,141,60", "240,59,32", "189,0,38")
_STYLE_GREY = "205,205,205"


def _style_document(breaks: list[float]) -> str:
    """A rule-based QGIS style for the building layer.

    Rule-based rather than graduated on purpose.  A graduated renderer draws
    nothing at all where the attribute is NULL, which would hide exactly the
    buildings this layer goes out of its way to keep - the failed and screened
    out ones - and hand back the blank-patch problem the layer was built to
    avoid.  A rule can name them, so they are drawn grey and labelled.

    If QGIS ever declines to read this the layer simply opens unstyled, which
    is what it did before there was a style at all: the failure is a lost
    convenience, not a wrong map.
    """
    import uuid
    import xml.etree.ElementTree as ET

    root = ET.Element("qgis", {"version": "3.34.0",
                               "styleCategories": "Symbology"})
    renderer = ET.SubElement(root, "renderer-v2", {
        "type": "RuleRenderer", "symbollevels": "0",
        "forceraster": "0", "enableorderby": "0"})
    rules = ET.SubElement(renderer, "rules", {"key": f"{{{uuid.uuid4()}}}"})
    symbols = ET.SubElement(renderer, "symbols")

    def add(index: str, colour: str, filt: str, label: str) -> None:
        ET.SubElement(rules, "rule", {"key": f"{{{uuid.uuid4()}}}",
                                      "filter": filt, "label": label,
                                      "symbol": index})
        symbol = ET.SubElement(symbols, "symbol", {
            "type": "fill", "name": index, "alpha": "1",
            "clip_to_extent": "1", "force_rhr": "0"})
        layer = ET.SubElement(symbol, "layer", {
            "class": "SimpleFill", "enabled": "1", "locked": "0", "pass": "0"})
        options = ET.SubElement(layer, "Option", {"type": "Map"})
        for name, value in (("color", f"{colour},255"),
                            ("outline_color", "110,110,110,190"),
                            ("outline_width", "0.06"),
                            ("outline_style", "solid"),
                            ("style", "solid")):
            ET.SubElement(options, "Option",
                          {"name": name, "type": "QString", "value": value})

    add("0", _STYLE_GREY, f'"{STYLE_FIELD}" IS NULL', "not simulated (see reason)")
    edges = [None, *breaks, None]
    for position, colour in enumerate(_STYLE_RAMP):
        low, high = edges[position], edges[position + 1]
        clauses = [f'"{STYLE_FIELD}" IS NOT NULL']
        if low is not None:
            clauses.append(f'"{STYLE_FIELD}" >= {low:g}')
        if high is not None:
            clauses.append(f'"{STYLE_FIELD}" < {high:g}')
        label = (f"< {high:g}" if low is None else
                 f">= {low:g}" if high is None else f"{low:g} - {high:g}")
        add(str(position + 1), colour, " AND ".join(clauses), label)

    return ET.tostring(root, encoding="unicode")


def _write_style(target: Path, frame) -> dict:
    """Write the style beside the layer and inside it; never raise.

    Both, because they fail differently.  The sidecar `.qml` is the one a
    person can open and edit; the `layer_styles` table is the one that survives
    being packed into the signed ZIP, since only the GeoPackage travels.
    """
    import sqlite3

    block: dict = {"written": False}
    try:
        values = frame[STYLE_FIELD].dropna() if STYLE_FIELD in frame.columns else None
        if values is None or len(values) < len(_STYLE_RAMP):
            block["reason"] = "too_few_values_to_classify"
            return block
        # The precision is taken from the spread, not fixed at one decimal.
        # One decimal suits an annual EUI of 30-90; on an 8-day event run the
        # same column spans 1.02-1.32, every quintile rounded to the same
        # number, and the style was refused as "not distinct" when the data was
        # perfectly classifiable (measured 2026-08-12).
        raw = [float(values.quantile(q)) for q in (0.2, 0.4, 0.6, 0.8)]
        for decimals in (1, 2, 3, 4):
            breaks = sorted(set(round(v, decimals) for v in raw))
            if len(breaks) == len(_STYLE_RAMP) - 1:
                break
        block["decimals"] = decimals
        if len(breaks) != len(_STYLE_RAMP) - 1:
            block["reason"] = "class_breaks_not_distinct"
            return block

        document = _style_document(breaks)
        (target.with_name(STYLE_FILENAME)).write_text(document, encoding="utf-8")

        with sqlite3.connect(target) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS layer_styles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                f_table_catalog TEXT, f_table_schema TEXT, f_table_name TEXT,
                f_geometry_column TEXT, styleName TEXT, styleQML TEXT,
                styleSLD TEXT, useAsDefault BOOLEAN, description TEXT,
                owner TEXT, ui TEXT, update_time DATETIME)""")
            db.execute("DELETE FROM layer_styles WHERE f_table_name = ?",
                       (LAYER_NAME,))
            db.execute(
                "INSERT INTO layer_styles (f_table_catalog, f_table_schema, "
                "f_table_name, f_geometry_column, styleName, styleQML, "
                "useAsDefault, description, update_time) "
                "VALUES ('', '', ?, 'geom', ?, ?, 1, ?, datetime('now'))",
                (LAYER_NAME, "run result",
                 document,
                 f"graduated on {STYLE_FIELD}; buildings with no result grey"))
        block.update({"written": True, "field": STYLE_FIELD, "breaks": breaks,
                      "sidecar": STYLE_FILENAME})
    except Exception as exc:                        # noqa: BLE001
        block["reason"] = f"style_failed: {type(exc).__name__}: {str(exc)[:200]}"
    return block


def write_results_layer(rows: list[dict], stock, out_dir: Path) -> dict:
    """Write `results_buildings.gpkg` beside the ledger; never raise.

    The block it returns goes into `aggregate.json`, so a reader who never
    opens the directory can still tell whether a layer exists and what it is
    safe to compute from it.
    """
    frame, block = build_frame(rows, stock)
    period = energy_period(rows)
    if rows:
        block["energy_period"] = period
    if frame is None:
        return block
    target = Path(out_dir) / LAYER_FILENAME
    try:
        # Written whole, then moved into place: a half-written GeoPackage that
        # keeps the expected name is worse than none, because the export packs
        # whatever is in the run directory.
        # `.partial.gpkg`, not `.gpkg.partial`: the driver types the file from
        # its extension and warns when it is asked to write a GeoPackage under
        # any other one.
        staging = target.with_name(f"{target.stem}.partial{target.suffix}")
        staging.unlink(missing_ok=True)
        frame.to_file(staging, driver="GPKG", layer=LAYER_NAME)

        # Rolled up into the same file so one drag into QGIS offers all three.
        # Guarded on its own: a zone roll-up that fails must not cost the
        # building map, which is the thing that was actually asked for.
        try:
            zones = build_zone_frames(frame)
            for name, zone in zones.items():
                zone.to_file(staging, driver="GPKG", layer=name, mode="a")
            block["zone_layers"] = {name: int(len(zone))
                                    for name, zone in zones.items()}
        except Exception as exc:                    # noqa: BLE001
            block["zone_layers"] = {}
            block["zone_reason"] = f"{type(exc).__name__}: {str(exc)[:160]}"

        staging.replace(target)
    except Exception as exc:                        # noqa: BLE001
        block["reason"] = f"write_failed: {type(exc).__name__}: {str(exc)[:200]}"
        return block
    block["written"] = True
    block["style"] = _write_style(target, frame)
    block["bytes"] = int(target.stat().st_size)

    # The printable companion.  Called from here so both entry points - a
    # finished run and a re-aggregation of an old one - get it without the
    # runner having to know there are two artefacts.  Its own contract is the
    # same as this one's: it never raises, so a rendering problem costs the
    # picture and nothing else.
    try:
        import results_maps
        block["heatmap"] = results_maps.write_heatmap(
            frame, Path(out_dir), unit=period.get("unit"))
    except Exception as exc:                        # noqa: BLE001
        block["heatmap"] = {"written": False,
                            "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return block
