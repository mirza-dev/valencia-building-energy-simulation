"""How much modelled floor area sits above the cadastral record, and why.

A finished run reports two intensities over the same energy: one divided by the
floor area the model built, one divided by the dwelling area the cadastre
records.  On Benicalap v8 they are 46.904 and 50.867 kWh/m2, so the model
conditions 8.45 % more floor than the cadastre records dwellings for, and the
cadastral-basis headline carries all of it.  That ratio has always been
visible.  What was never measured is where it comes from, and the answer
decides whether it is an artefact or a fact about the stock.

Two mechanisms produce it and they are not the same thing:

  * `residential_storeys_from_cadastre` rounds UP.  Where the rule decides the
    height - fewer storeys than were built - the model builds `f * storeys` and
    the remainder `f * storeys - c` is floor area no record asks for.  It is
    one-sided by construction and bounded by one footprint per building.  Since
    2026-08-22 the rule snaps to a whole storey where `c/f` sits within the
    measurement band, so the excess is read from the rule rather than restated
    as `ceil` here: a measurement that rewrites the thing it measures stops
    measuring it.
  * Everywhere else the geometry decides the height, and modelled minus
    cadastral is the two sources disagreeing.  That term has no preferred sign.

Separating them matters because only the first is ours.  Measured here: the
rounding accounts for 95.8 % of the gap, and the disagreement terms very nearly
cancel (+69,230 against -61,588 m2, netting 4.2 % of the gap).

Whether the rounding excess is spurious turns on one data-semantics fact: is
Tipo15 `442_sup_Residencial` a net or a gross area?  The rule's docstring
defends `ceil` as grossing net `sfc` up to a gross plate.  The thesis annex
names the field `442_sfc` beside `452_sfs`, which the source CSV expands as
`supSolar` - superficie solar - so `sfc` reads as superficie construida, a
gross measure already.  The stock agrees: on 174 buildings the recorded area
exceeds the entire modelled envelope, which cannot happen if `sfc` were net of
common areas.  So the gross-up argument does not hold and the excess is real.

This module only reads.  It changes no rule, no published number and no frozen
module; `residential_storeys_from_cadastre` is covered by `LOCKED_SOURCE_HASHES`
and any change to it needs its own verify/relock/golden cycle and its own
approval.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
from pathlib import Path

# The fields a row must carry before it can be counted.  A row missing any of
# them is an error rather than a skip: silently dropping buildings would shrink
# the very gap this module exists to size.
REQUIRED_FIELDS = (
    "refparcela", "footprint_m2", "tipo15_res_area_m2", "res_area_m2",
    "built_storeys", "residential_storeys_effective", "ground_use",
    "total_site_kwh_m2", "lighting_kwh_m2", "equipment_kwh_m2", "dhw_kwh_m2",
)

# Split for `allocation_block`, which reports whatever a ledger can support
# instead of refusing the whole aggregate.  The area terms need only geometry
# and the cadastral record; the energy band additionally needs the per-end-use
# columns, and a ledger written before those existed can still be sized.
AREA_FIELDS = ("refparcela", "footprint_m2", "tipo15_res_area_m2", "res_area_m2",
               "built_storeys", "ground_use")
ENERGY_FIELDS = ("total_site_kwh_m2", "lighting_kwh_m2", "equipment_kwh_m2",
                 "dhw_kwh_m2")

# Which published fields carry the excess, named so a reader does not have to
# work it out.  Every one of these divides by cadastral dwelling area.
AFFECTED_FIELDS = ("totals.cadastral_total_site_kwh_m2",
                   "by_cluster[].cadastral_kwh_m2",
                   "by_cluster[].vs_rai_pct",
                   "by_cluster[].vs_rai_energy_ratio")


class AllocationError(RuntimeError):
    """The ledger cannot be measured as asked.  Never guess a missing term."""


def _rows(ledger_path: Path) -> list[dict]:
    path = Path(ledger_path)
    if not path.exists():
        raise AllocationError(f"ledger not found: {path}")
    ok = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") != "ok":
            continue
        missing = [f for f in REQUIRED_FIELDS if row.get(f) is None]
        if missing:
            raise AllocationError(
                f"{row.get('refparcela', '?')}: ledger row lacks {missing}; "
                "this ledger predates the fields this measurement needs")
        ok.append(row)
    if not ok:
        raise AllocationError(f"{path.name}: no rows with status 'ok'")
    return ok


def _annotate(row: dict) -> dict:
    """Per building: which mechanism set the height, and what it left over."""
    f = float(row["footprint_m2"])
    c = float(row["tipo15_res_area_m2"])
    if not (f > 0 and c > 0):
        raise AllocationError(f"{row['refparcela']}: footprint {f}, cadastral {c}")
    ground_residential = row["ground_use"] == "residential"
    built = int(row["built_storeys"]) + (1 if ground_residential else 0)
    # `res_area_m2` is the basis every published number already uses, and
    # `total_site_kwh_m2 * res_area_m2` is how `aggregate()` forms energy.
    # Recomputing from the footprint instead would drift by the ledger's own
    # 0.1 m2 rounding and stop reproducing the headline.
    area = float(row["res_area_m2"])
    exact = c / f
    # What the run actually did, read from its own row - not the rule
    # recomputed here.  Restating `ceil` made this module describe a rule
    # instead of a run, and re-deriving it from today's engine would be worse
    # still: it would describe a historical ledger with a rule that ledger
    # never used.  `residential_storeys_effective` is a REQUIRED_FIELD, so it
    # is always there, and it stays right across every future profile change.
    applied = int(row["residential_storeys_effective"])
    rule_bound = applied < built
    # `measure` validates REQUIRED_FIELDS first, so it always has an intensity.
    # `allocation_block` may not: a ledger can carry geometry without the
    # per-end-use columns, and sizing its area gap is still worth doing.
    intensity = row.get("total_site_kwh_m2")
    return {
        **row,
        "_f": f, "_c": c, "_exact_storeys": exact,
        "_area": area,
        "_energy": float(intensity) * area if intensity is not None else None,
        "_built_storeys": built, "_applied_storeys": applied,
        "_rule_bound": rule_bound,
        # Where the rule bound, the excess is rounding and - measured over the
        # 449 bound rows of Benicalap v8 - does not reach a full storey (max
        # 0.9937 of one, min 0.0006, none negative).  `ceil` guaranteed that by
        # construction; reading the recorded area does not, so it is stated as a
        # measurement.  Where the rule did not bind, the excess is the two
        # sources disagreeing.
        "_excess": area - c,
    }


def _proxy_references(prepared_stock: Path | None) -> set[str] | None:
    """Which buildings' `tipo15_res_area_m2` was constructed rather than recorded.

    `stock_input_policy` fills a missing Tipo15 area from the cluster ratio and
    marks the row `res_area_proxy`.  That flag never reaches the ledger, so a
    measurement reading the ledger alone cannot tell a recorded area from a
    constructed one - and comparing modelled area against a constructed one
    measures the filling rule, not the stock.  Returning None means "not
    checked", which the report states rather than hides.
    """
    if prepared_stock is None:
        return None
    import geopandas as gpd  # only needed on this path
    frame = gpd.read_file(prepared_stock)
    if "res_area_proxy" not in frame.columns:
        raise AllocationError(f"{Path(prepared_stock).name}: no 'res_area_proxy' "
                              "column; this is not a prepared stock file")
    return set(frame.loc[frame["res_area_proxy"] == True,  # noqa: E712
                         "refparcela"].astype(str))


def measure(ledger_path: Path, prepared_stock: Path | None = None) -> dict:
    """Decompose the modelled-versus-cadastral gap on a finished ledger.

    Pass the run's prepared stock to separate buildings whose cadastral area was
    proxied; without it the report says the separation was not made.
    """
    proxies = _proxy_references(prepared_stock)
    every = [_annotate(r) for r in _rows(ledger_path)]
    if proxies is None:
        ok, proxied = every, []
    else:
        ok = [r for r in every if r["refparcela"] not in proxies]
        proxied = [r for r in every if r["refparcela"] in proxies]
        if not ok:
            raise AllocationError("every building's cadastral area is a proxy; "
                                  "there is nothing recorded to measure against")

    modelled = sum(r["_area"] for r in ok)
    cadastral = sum(r["_c"] for r in ok)
    energy = sum(r["_energy"] for r in ok)
    gap = modelled - cadastral
    if gap == 0:
        raise AllocationError("modelled area equals cadastral area exactly; "
                              "that cannot happen with integer storeys")

    bound = [r for r in ok if r["_rule_bound"]]
    free = [r for r in ok if not r["_rule_bound"]]
    over = [r for r in free if r["_excess"] > 0]
    under = [r for r in free if r["_excess"] <= 0]
    rounding = sum(r["_excess"] for r in bound)

    # Energy standing on the rounding excess.  Lighting and equipment are fixed
    # W/m2 and scale with area exactly.  DHW is per person and does not scale at
    # all, so charging it to the excess would overstate the correction.  What is
    # left - heating, cooling, fans, pumps - moves with geometry rather than in
    # proportion, and is the reason this is reported as a band and not a number.
    proportional = sum((r["lighting_kwh_m2"] + r["equipment_kwh_m2"]) * r["_excess"]
                       for r in bound)
    at_full_intensity = sum(r["total_site_kwh_m2"] * r["_excess"] for r in bound)
    dhw = sum(r["dhw_kwh_m2"] * r["_excess"] for r in bound)

    def pct(x: float, y: float) -> float:
        return round(100.0 * x / y, 3)

    def group(rows: list[dict]) -> dict:
        return {"buildings": len(rows),
                "excess_m2": round(sum(r["_excess"] for r in rows), 1),
                "share_of_gap_pct": pct(sum(r["_excess"] for r in rows), gap)}

    return {
        "ledger": str(Path(ledger_path)),
        "buildings_ok": len(ok),
        "cadastral_area_provenance": _provenance(proxies, proxied, cadastral),
        "area": {
            "modelled_m2": round(modelled, 1),
            "cadastral_m2": round(cadastral, 1),
            "gap_m2": round(gap, 1),
            "gap_pct_of_cadastral": pct(gap, cadastral),
            "geometric_kwh_m2": round(energy / modelled, 3),
            "cadastral_kwh_m2": round(energy / cadastral, 3),
        },
        "decomposition": {
            "integer_storey_rounding": {
                **group(bound),
                "pct_of_cadastral_area": pct(rounding, cadastral),
                # None, not zero: a run where the rule never bound has no
                # fraction to report, and 0.0 would read as "it always rounded
                # to nothing" - the opposite of "it never applied".
                "median_fraction_of_a_storey":
                    round(st.median([r["_excess"] / r["_f"] for r in bound]), 3)
                    if bound else None,
                "mechanism": "modelled area - cadastral, with the storey count "
                             "as the run recorded it; one-sided, under one "
                             "footprint",
            },
            "geometry_above_cadastre": group(over),
            "cadastre_above_geometry": {
                **group(under),
                "note": "recorded dwelling area exceeds the whole modelled "
                        "envelope; impossible if 442_sfc were net of commons",
            },
            "disagreement_net": group(free),
        },
        "energy": {
            "total_gwh": round(energy / 1e6, 5),
            "on_excess_proportional_gwh": round(proportional / 1e6, 4),
            "on_excess_at_full_intensity_gwh": round(at_full_intensity / 1e6, 4),
            "dhw_component_pct": pct(dhw, energy),
            "band_pct": [pct(proportional, energy),
                         pct(at_full_intensity - dhw, energy)],
            "basis": "lighting+equipment scale exactly; DHW is per person and "
                     "does not scale; HVAC is the non-linear remainder",
        },
        "alternative_rules": _alternative_rules(ok, cadastral),
        "concentration_by_footprint": {
            label: _band(rows)
            for label, rows in (("<=300", [r for r in ok if r["_f"] <= 300]),
                                ("300-1000", [r for r in ok if 300 < r["_f"] <= 1000]),
                                (">1000", [r for r in ok if r["_f"] > 1000]))
        },
    }


def _provenance(proxies: set[str] | None, proxied: list[dict],
                measured_cadastral: float) -> dict:
    """State whether proxied rows were separated - never let silence imply zero."""
    if proxies is None:
        return {"checked": False,
                "note": "no prepared stock supplied; buildings whose cadastral "
                        "area was proxied could not be separated and are "
                        "included in every figure below"}
    excluded_area = sum(r["_c"] for r in proxied)
    return {
        "checked": True,
        "excluded_buildings": len(proxied),
        "excluded_cadastral_m2": round(excluded_area, 1),
        "excluded_share_of_cadastral_pct":
            round(100.0 * excluded_area / (measured_cadastral + excluded_area), 2)
            if measured_cadastral + excluded_area else 0.0,
        "note": "their Tipo15 area came from the cluster ratio, so comparing "
                "modelled area against it would measure the filling rule",
    }


def _band(rows: list[dict]) -> dict:
    area = sum(r["_area"] for r in rows)
    cadastral = sum(r["_c"] for r in rows)
    return {"buildings": len(rows),
            "modelled_over_cadastral": round(area / cadastral, 4) if cadastral else None,
            "rounding_excess_m2": round(sum(r["_excess"] for r in rows
                                            if r["_rule_bound"]), 1)}


def _alternative_rules(ok: list[dict], cadastral: float) -> dict:
    """What each rounding rule would have modelled, on the same buildings.

    Reported so the cost of the current rule can be read against the cost of
    changing it.  `floor` is included because it is the obvious opposite and is
    measurably worse, not because it is a candidate.
    """
    rules = {"ceil": math.ceil,
             "round": lambda v: max(1, round(v)),
             "floor": lambda v: max(1, math.floor(v))}
    out = {}
    # What the run actually modelled, so the hypotheticals below can be read
    # against it.  This is NOT `ceil` recomputed: on Benicalap v8 the recorded
    # storey count already differs from a fresh `ceil` for 134 of 967 buildings,
    # a divergence that predates the measurement band entirely.
    applied_total = sum(r["_area"] for r in ok)
    out["applied"] = {"modelled_m2": round(applied_total, 1),
                      "vs_cadastral_pct": round(
                          100.0 * (applied_total - cadastral) / cadastral, 2),
                      "note": "what the run modelled, read from its own rows; "
                              "the rows below are hypotheticals recomputed here"}
    for label, fn in rules.items():
        total = sum(r["_f"] * max(1, min(r["_built_storeys"], int(fn(r["_exact_storeys"]))))
                    for r in ok)
        out[label] = {"modelled_m2": round(total, 1),
                      "vs_cadastral_pct": round(100.0 * (total - cadastral) / cadastral, 2)}
    out["exact_fractional"] = {"modelled_m2": round(cadastral, 1),
                               "vs_cadastral_pct": 0.0,
                               "note": "not buildable: storeys are integers"}
    return out


def allocation_block(rows: list[dict], stock=None) -> dict:
    """The area gap, carried inside the aggregate a reader actually opens.

    `measure()` is the standalone analysis and refuses anything it cannot size,
    which is right for a study and wrong here: `aggregate()` must survive a
    ledger that predates these fields and a city whose stock has no cadastral
    record at all.  So this follows the `zoning_block` contract - report what
    the rows support, say so plainly when they support nothing, and never raise.

    The distinction that matters: a stock with no Tipo15 is NOT a stock with no
    excess.  It is one where the rule never fired, so there is nothing to
    correct - and the block says which of the two it is rather than printing a
    zero that reads as "measured, and it was none".
    """
    block: dict = {"measured": False,
                   "rule": "residential_storeys_effective, as recorded by the "
                           "run that wrote this ledger - not a rule recomputed "
                           "here, which would describe an old run with a rule it "
                           "never used"}
    if not rows:
        block["reason"] = "no_rows"
        return block

    missing = [f for f in AREA_FIELDS if all(r.get(f) is None for r in rows)]
    if missing:
        block["reason"] = "ledger_predates_fields"
        block["missing_fields"] = missing
        block["note"] = ("this ledger carries no cadastral area record, so the "
                         "storey rule never bound and there is no excess to "
                         "size; this is not a measurement of zero")
        return block

    proxies = None
    if stock is not None and getattr(stock, "columns", None) is not None:
        if "res_area_proxy" in stock.columns:
            proxies = set(stock.loc[stock["res_area_proxy"] == True,  # noqa: E712
                                    "refparcela"].astype(str))

    usable, skipped = [], 0
    for row in rows:
        if any(row.get(f) is None for f in AREA_FIELDS):
            skipped += 1
            continue
        try:
            usable.append(_annotate(row))
        except AllocationError:
            # footprint or cadastral area at zero: the rule cannot have bound.
            skipped += 1

    proxy_refs = proxies or set()
    proxied = [r for r in usable if str(r["refparcela"]) in proxy_refs]
    ok = [r for r in usable if str(r["refparcela"]) not in proxy_refs]
    if not ok:
        block["reason"] = "no_building_has_a_recorded_cadastral_area"
        block["buildings_without_cadastral_area"] = skipped
        block["buildings_proxied"] = len(proxied)
        return block

    modelled = sum(r["_area"] for r in ok)
    cadastral = sum(r["_c"] for r in ok)
    gap = modelled - cadastral
    bound = [r for r in ok if r["_rule_bound"]]
    rounding = sum(r["_excess"] for r in bound)

    block.update({
        "measured": True,
        "buildings_measured": len(ok),
        "buildings_without_cadastral_area": skipped,
        "cadastral_area_provenance": _provenance(proxies, proxied, cadastral),
        "modelled_m2": round(modelled, 1),
        "cadastral_m2": round(cadastral, 1),
        "gap_m2": round(gap, 1),
        "gap_pct_of_cadastral": round(100.0 * gap / cadastral, 3) if cadastral else None,
        "integer_storey_rounding": {
            "buildings": len(bound),
            "excess_m2": round(rounding, 1),
            # Same precision as `measure()`'s own `pct`: the two files report
            # this quantity side by side and must not disagree in the decimal.
            "share_of_gap_pct": round(100.0 * rounding / gap, 3) if gap else None,
            "median_fraction_of_a_storey":
                round(st.median([r["_excess"] / r["_f"] for r in bound]), 3)
                if bound else None,
            # Same wording as `measure()`'s own mechanism string above: the two
            # report this quantity side by side and must not describe it
            # differently, any more than they may disagree in the decimal.
            "mechanism": "modelled area - cadastral, with the storey count as "
                         "the run recorded it; one-sided, under one footprint",
        },
        "alternative_rules": _alternative_rules(ok, cadastral),
        "affects": list(AFFECTED_FIELDS),
        "note": ("every kWh/m2 on the cadastral basis divides by the recorded "
                 "dwelling area while the numerator carries energy delivered to "
                 "the modelled excess above; the rounding term is the part the "
                 "rule produces, the remainder is the two sources disagreeing"),
    })

    # Whether the rows were produced with the partial top storey scaled.  The
    # KEY decides it, not its value: a building the rule never bound carries
    # `top_storey_fraction: null` under that profile and no key at all before
    # it.  This changes what the gap MEANS - the floor is still modelled, but
    # it no longer carries dwelling loads - so the block must not keep quoting
    # a band that was true of a different model.
    profile = _load_profile(ok)
    block["loads_on_rounding_excess"] = profile
    if profile["dwelling_loads_on_excess"] is not True:
        # Withdrawn for `False` AND for `None`: the band is only meaningful if
        # these rows are KNOWN to carry dwelling loads on the excess.  An
        # unresolved profile is not a licence to publish it anyway.
        if profile["dwelling_loads_on_excess"] is False:
            block["note"] = (
                "the modelled floor still exceeds the record, but the excess is "
                "the unscaled part of a partial top storey and carries no "
                "dwelling lighting, equipment or occupants; what remains on it "
                "is envelope and the thermostat holding a storey that exists")
            reason = ("loads on the excess were scaled out at build time; "
                      "the pre-correction band does not describe these rows")
        else:
            reason = ("these rows are not one run: some carry the partial top "
                      "storey and some do not, so no single band describes them")
        block["energy_on_rounding_excess"] = {"measured": False, "reason": reason}
        return block

    if all(r.get(f) is not None for f in ENERGY_FIELDS for r in ok):
        energy = sum(r["_energy"] for r in ok)
        proportional = sum((r["lighting_kwh_m2"] + r["equipment_kwh_m2"]) * r["_excess"]
                           for r in bound)
        full = sum(r["total_site_kwh_m2"] * r["_excess"] for r in bound)
        dhw = sum(r["dhw_kwh_m2"] * r["_excess"] for r in bound)
        block["energy_on_rounding_excess"] = {
            "band_pct": [round(100.0 * proportional / energy, 3),
                         round(100.0 * (full - dhw) / energy, 3)] if energy else None,
            "band_gwh": [round(proportional / 1e6, 4), round((full - dhw) / 1e6, 4)],
            "basis": ("lighting+equipment scale exactly with area; DHW is per "
                      "person and does not scale; HVAC is the non-linear "
                      "remainder, which is why this is a band"),
        }
    else:
        block["energy_on_rounding_excess"] = {
            "measured": False,
            "reason": "ledger lacks the per-end-use columns the band needs"}
    return block


def _load_profile(rows: list[dict]) -> dict:
    """Did these rows come from a model that scales the partial top storey?

    A mixed answer is reported as unknown rather than resolved to a majority.
    It should be unreachable - the profile fingerprint changes with the source,
    so a ledger cannot be resumed across the two - and if it ever appears, the
    honest reading is that something merged two runs, not that most rows win.
    """
    marked = sum(1 for r in rows if "top_storey_fraction" in r)
    if marked == len(rows):
        return {"dwelling_loads_on_excess": False,
                "basis": "every row carries `top_storey_fraction`",
                "scaled_buildings": sum(1 for r in rows
                                        if r.get("top_storey_fraction") is not None)}
    if marked == 0:
        return {"dwelling_loads_on_excess": True,
                "basis": "no row carries `top_storey_fraction`: built before "
                         "the partial top storey existed"}
    return {"dwelling_loads_on_excess": None,
            "basis": f"{marked} of {len(rows)} rows carry `top_storey_fraction` - "
                     "these are not one run and must not be read as one"}


def reference_ratio(report: dict, aggregate_path: Path) -> dict:
    """The published reference ratio, and what it is once the excess is removed.

    Both sides divide by cadastral dwelling area and both carry their commercial
    energy in the numerator, so the comparison is symmetric - except that ours
    also carries floor area the cadastre does not record.

    The band is measured on buildings with a recorded area but applied to the
    whole aggregate, which also contains proxied ones.  Those carry rounding
    excess too, so the correction here is a floor: the true ratio is at or below
    what this returns.
    """
    aggregate = json.loads(Path(aggregate_path).read_text(encoding="utf-8"))
    covered = [b for b in aggregate.get("by_cluster", [])
               if b.get("rai_consume_kwh_m2") and b.get("cadastral_area_m2")]
    if not covered:
        raise AllocationError(f"{aggregate_path}: no cluster carries a reference constant")
    ours = sum(b["total_site_gwh"] for b in covered) * 1e6
    theirs = sum(b["rai_consume_kwh_m2"] * b["cadastral_area_m2"] for b in covered)
    lo, hi = report["energy"]["band_pct"]
    # Against the aggregate's own total, not the report's: the report may have
    # excluded proxied buildings, and dividing by that subset would read as
    # more than 100 % coverage.
    district = sum(b["total_site_gwh"] for b in aggregate["by_cluster"]) * 1e6
    return {
        "clusters_covered": len(covered),
        "share_of_district_energy_pct": round(100.0 * ours / district, 1),
        "reference_gwh": round(theirs / 1e6, 4),
        "published_ratio": round(ours / theirs, 4),
        "ratio_without_rounding_excess": [round(ours * (1 - hi / 100) / theirs, 4),
                                          round(ours * (1 - lo / 100) / theirs, 4)],
        "correction_is_a_floor": not report["cadastral_area_provenance"]["checked"]
                                 or bool(report["cadastral_area_provenance"]
                                         ["excluded_buildings"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True,
                        help="finished run directory holding ledger.jsonl")
    parser.add_argument("--stock", type=Path, default=None,
                        help="the run's prepared stock, so buildings whose "
                             "cadastral area was proxied can be separated")
    parser.add_argument("--out", type=Path, default=None,
                        help="where to write the report (default: <run>/floor_area_allocation.json)")
    args = parser.parse_args(argv)

    report = measure(args.run / "ledger.jsonl", args.stock)
    aggregate = args.run / "aggregate.json"
    if aggregate.exists():
        report["vs_reference"] = reference_ratio(report, aggregate)

    out = args.out or (args.run / "floor_area_allocation.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    area, energy = report["area"], report["energy"]
    rounding = report["decomposition"]["integer_storey_rounding"]
    provenance = report["cadastral_area_provenance"]
    if not provenance["checked"]:
        print("WARNING: proxied cadastral areas were not separated (--stock not given)")
    else:
        print(f"excluded {provenance['excluded_buildings']} buildings whose cadastral "
              f"area was proxied ({provenance['excluded_share_of_cadastral_pct']:.2f}% of area)")
    print(f"modelled {area['modelled_m2']:,.0f} m2 against {area['cadastral_m2']:,.0f} "
          f"recorded = +{area['gap_pct_of_cadastral']:.2f}%")
    print(f"  integer-storey rounding: {rounding['excess_m2']:,.0f} m2 "
          f"({rounding['share_of_gap_pct']:.1f}% of the gap) on {rounding['buildings']} buildings")
    print(f"  energy standing on it: {energy['band_pct'][0]:.2f}-{energy['band_pct'][1]:.2f}% "
          f"of {energy['total_gwh']:.3f} GWh")
    if "vs_reference" in report:
        r = report["vs_reference"]
        print(f"  reference ratio {r['published_ratio']:.4f} -> "
              f"{r['ratio_without_rounding_excess'][0]:.4f}-"
              f"{r['ratio_without_rounding_excess'][1]:.4f} without it")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
