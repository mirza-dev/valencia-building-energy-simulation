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
    height - `ceil(c/f) < built storeys` - the model builds `f*ceil(c/f)` and
    the remainder `f*(ceil(c/f) - c/f)` is floor area no record asks for.  It
    is one-sided by construction and bounded by one footprint per building.
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
    rule_bound = math.ceil(exact) < built
    return {
        **row,
        "_f": f, "_c": c, "_exact_storeys": exact,
        "_area": area, "_energy": float(row["total_site_kwh_m2"]) * area,
        "_built_storeys": built, "_rule_bound": rule_bound,
        # Where the rule bound, the excess is pure rounding and cannot reach a
        # full storey.  Where it did not, the excess is the sources disagreeing.
        "_excess": f * (math.ceil(exact) - exact) if rule_bound else area - c,
    }


def measure(ledger_path: Path) -> dict:
    """Decompose the modelled-versus-cadastral gap on a finished ledger."""
    ok = [_annotate(r) for r in _rows(ledger_path)]

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
                "mechanism": "f * (ceil(c/f) - c/f); one-sided, under one footprint",
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
    for label, fn in rules.items():
        total = sum(r["_f"] * max(1, min(r["_built_storeys"], int(fn(r["_exact_storeys"]))))
                    for r in ok)
        out[label] = {"modelled_m2": round(total, 1),
                      "vs_cadastral_pct": round(100.0 * (total - cadastral) / cadastral, 2)}
    out["exact_fractional"] = {"modelled_m2": round(cadastral, 1),
                               "vs_cadastral_pct": 0.0,
                               "note": "not buildable: storeys are integers"}
    return out


def reference_ratio(report: dict, aggregate_path: Path) -> dict:
    """The published reference ratio, and what it is once the excess is removed.

    Both sides divide by cadastral dwelling area and both carry their commercial
    energy in the numerator, so the comparison is symmetric - except that ours
    also carries floor area the cadastre does not record.
    """
    aggregate = json.loads(Path(aggregate_path).read_text(encoding="utf-8"))
    covered = [b for b in aggregate.get("by_cluster", [])
               if b.get("rai_consume_kwh_m2") and b.get("cadastral_area_m2")]
    if not covered:
        raise AllocationError(f"{aggregate_path}: no cluster carries a reference constant")
    ours = sum(b["total_site_gwh"] for b in covered) * 1e6
    theirs = sum(b["rai_consume_kwh_m2"] * b["cadastral_area_m2"] for b in covered)
    lo, hi = report["energy"]["band_pct"]
    return {
        "clusters_covered": len(covered),
        "share_of_district_energy_pct": round(
            100.0 * ours / (report["energy"]["total_gwh"] * 1e6), 1),
        "reference_gwh": round(theirs / 1e6, 4),
        "published_ratio": round(ours / theirs, 4),
        "ratio_without_rounding_excess": [round(ours * (1 - hi / 100) / theirs, 4),
                                          round(ours * (1 - lo / 100) / theirs, 4)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True,
                        help="finished run directory holding ledger.jsonl")
    parser.add_argument("--out", type=Path, default=None,
                        help="where to write the report (default: <run>/floor_area_allocation.json)")
    args = parser.parse_args(argv)

    report = measure(args.run / "ledger.jsonl")
    aggregate = args.run / "aggregate.json"
    if aggregate.exists():
        report["vs_reference"] = reference_ratio(report, aggregate)

    out = args.out or (args.run / "floor_area_allocation.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    area, energy = report["area"], report["energy"]
    rounding = report["decomposition"]["integer_storey_rounding"]
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
