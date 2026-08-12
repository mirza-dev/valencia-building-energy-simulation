"""What the buildings without a result do to the number that got published.

`coverage_block` has always reported how much of the scope produced a result.
It also carried a sentence about which WAY the missing buildings skew the
intensity - and that sentence was a constant, measured once on the Benicalap
v3 ledger and printed next to every run since.

On 2026-08-03 the footprint ceiling went from 5 000 to 20 000 m2.  Before that
the gates fell hardest on large buildings, footprint anti-correlated with EUI
at Spearman -0.552, and the surviving subset really was biased upward.  After
it, the 123 large buildings run instead of being refused, what remains is the
handful the simplifier cannot hold - and those are SMALL.  The skew reversed
and the sentence did not, so a stale claim outlived the run it described.

The fix is not a better sentence.  It is to stop asserting a remembered
constant and measure the run's own exclusions:

  * how much CADASTRAL AREA is missing, not just how many buildings - a count
    hides the thing that matters when the missing ones are unusually small or
    unusually large (the lesson from 2026-08-03, where exclusions were 5.5 %
    of buildings and 12.5 % of floor area);
  * which way the missing ones skew, from their own size distribution;
  * a BOUND on the total, by imputing the missing buildings two defensible
    ways and reporting the interval rather than a point;
  * the residual bias on the published intensity.

Contract follows `allocation_block`: report what the rows support, say plainly
when they support nothing, and never raise.  A stock with no cadastral area
column is not a stock with no bias - it is one where the bias cannot be sized,
and the block says which of the two it is instead of printing a zero.
"""

from __future__ import annotations

import math

import pandas as pd


# Energy is reconstructed the way `aggregate()` reconstructs it - intensity
# times residential area - and not from the ledger's own `total_site_kwh`.
# The two differ in the fifth decimal, and one package must not write the same
# quantity two ways (the reconciliation that closed on 2026-08-12).
INTENSITY_FIELD = "total_site_kwh_m2"
AREA_FIELD = "res_area_m2"
CADASTRAL_FIELD = "tipo15_res_area_m2"

# Below this many measured buildings a quantile band carries no information
# and a rank correlation is noise, so the block reports the flat imputation
# alone rather than dressing a small sample as a distribution.
MIN_ROWS_FOR_BANDS = 40
BAND_QUANTILES = (0.0, 0.25, 0.5, 0.75, 0.9, 1.0)

# A skew this small is not worth a direction: naming one would invite a
# correction nobody should apply.
NEGLIGIBLE_INTENSITY_PCT = 0.25


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cadastral_area_by_reference(stock) -> dict[str, float] | None:
    """Cadastral residential area per reference, or None if the stock has none.

    Duplicate rows for one reference are SUMMED, not taken first: the stock
    preparation apportions a shared parcel's area across its rows, so first()
    would silently drop the rest of a building.
    """
    if stock is None or getattr(stock, "columns", None) is None:
        return None
    if "refparcela" not in stock.columns or CADASTRAL_FIELD not in stock.columns:
        return None
    frame = pd.DataFrame({
        "refparcela": stock["refparcela"].astype(str).str.strip(),
        "area": pd.to_numeric(stock[CADASTRAL_FIELD], errors="coerce"),
    }).dropna()
    if frame.empty:
        return None
    return frame.groupby("refparcela")["area"].sum().to_dict()


def _footprint_by_reference(stock) -> dict[str, float] | None:
    if stock is None or getattr(stock, "columns", None) is None:
        return None
    if "refparcela" not in stock.columns:
        return None
    if "footprint_area_m2" not in stock.columns:
        return None
    frame = pd.DataFrame({
        "refparcela": stock["refparcela"].astype(str).str.strip(),
        "foot": pd.to_numeric(stock["footprint_area_m2"], errors="coerce"),
    }).dropna()
    if frame.empty:
        return None
    return frame.groupby("refparcela")["foot"].sum().to_dict()


def bias_block(rows: list[dict], ok: list[dict], stock=None) -> dict:
    """Size what the missing buildings do to the total and to the intensity."""
    block: dict = {"measured": False,
                   "basis": "cadastral_residential_area",
                   "energy_convention": "intensity_times_residential_area"}

    in_scope = {str(r.get("refparcela")) for r in rows if r.get("refparcela")}
    produced = {str(r["refparcela"]) for r in ok if r.get("refparcela")}
    missing = sorted(in_scope - produced)
    block["buildings_without_result"] = len(missing)

    if not in_scope:
        block["reason"] = "no_rows"
        return block

    if not missing:
        # A real measurement, not an absence: every building in scope produced
        # a result, so the total is not truncated at all.
        block.update({
            "measured": True,
            "complete_coverage": True,
            "total_truncation_pct": {"low": 0.0, "high": 0.0},
            "note": ("every building in scope produced a result, so the total "
                     "is not truncated and the intensity is the whole scope's"),
        })
        return block

    cadastral = _cadastral_area_by_reference(stock)
    footprint = _footprint_by_reference(stock)

    measured = []
    for row in ok:
        reference = str(row.get("refparcela"))
        intensity = _finite(row.get(INTENSITY_FIELD))
        area = _finite(row.get(AREA_FIELD))
        if intensity is None or area is None:
            continue
        measured.append({
            "refparcela": reference,
            "energy_kwh": intensity * area,
            "cadastral_m2": (cadastral or {}).get(reference),
            "footprint_m2": (footprint or {}).get(reference),
        })

    if not measured:
        block["reason"] = "no_measured_building_carries_energy"
        return block

    if cadastral is None:
        block["reason"] = "stock_has_no_cadastral_area_column"
        block["note"] = ("the missing buildings cannot be sized against a "
                         "common area basis, so the truncation is unquantified; "
                         "this is not a measurement of no bias")
        if footprint is not None:
            block["footprint_skew"] = _skew(measured, missing, footprint)
        return block

    sized = [m for m in measured if _finite(m["cadastral_m2"])]
    missing_area = [(_finite(cadastral.get(ref)) or 0.0) for ref in missing]
    unsized_missing = sum(1 for ref in missing if _finite(cadastral.get(ref)) is None)
    if not sized:
        block["reason"] = "no_measured_building_has_a_cadastral_area"
        return block

    energy_kwh = sum(m["energy_kwh"] for m in sized)
    area_measured = sum(float(m["cadastral_m2"]) for m in sized)
    area_missing = sum(missing_area)
    area_total = area_measured + area_missing
    if area_measured <= 0 or area_total <= 0:
        block["reason"] = "cadastral_area_is_zero"
        return block

    intensity_subset = energy_kwh / area_measured

    # Imputation A - every missing building runs at the subset's own intensity.
    # Imputation B - each runs at the intensity of its own footprint band, which
    # is what makes the estimate sensitive to the skew rather than blind to it.
    energy_flat = energy_kwh + intensity_subset * area_missing
    energy_band, band_table = _band_imputation(sized, missing, cadastral, footprint,
                                               energy_kwh, intensity_subset)

    low_kwh, high_kwh = sorted((energy_flat, energy_band))
    intensity_full = energy_band / area_total
    intensity_bias_pct = 100.0 * (intensity_subset / intensity_full - 1.0)

    block.update({
        "measured": True,
        "complete_coverage": False,
        "buildings_in_scope": len(in_scope),
        "cadastral_area_measured_m2": round(area_measured, 1),
        "cadastral_area_missing_m2": round(area_missing, 1),
        "cadastral_area_coverage_pct": round(100.0 * area_measured / area_total, 3),
        "total_measured_gwh": round(energy_kwh / 1e6, 5),
        "total_truncation_pct": {
            "low": round(100.0 * (low_kwh / energy_kwh - 1.0), 3),
            "high": round(100.0 * (high_kwh / energy_kwh - 1.0), 3),
        },
        "total_full_scope_gwh": {
            "low": round(low_kwh / 1e6, 5),
            "high": round(high_kwh / 1e6, 5),
        },
        "intensity_subset_kwh_m2": round(intensity_subset, 3),
        "intensity_full_scope_kwh_m2": round(intensity_full, 3),
        "intensity_bias_pct": round(intensity_bias_pct, 3),
        "intensity_bias_direction": _direction(intensity_bias_pct),
    })
    if unsized_missing:
        block["missing_without_cadastral_area"] = unsized_missing
    if band_table:
        block["footprint_bands"] = band_table
    skew = _skew(measured, missing, footprint) if footprint is not None else None
    if skew:
        block["footprint_skew"] = skew
    block["note"] = _note(block)
    return block


def _direction(bias_pct: float) -> str:
    if abs(bias_pct) < NEGLIGIBLE_INTENSITY_PCT:
        return "negligible"
    return "upward" if bias_pct > 0 else "downward"


def _skew(measured: list[dict], missing: list[str], footprint: dict[str, float]) -> dict | None:
    """Are the buildings without a result bigger or smaller than the rest?"""
    have = [float(m["footprint_m2"]) for m in measured if _finite(m["footprint_m2"])]
    gone = [f for f in ((_finite(footprint.get(ref))) for ref in missing) if f is not None]
    if not have or not gone:
        return None
    have_series, gone_series = pd.Series(have), pd.Series(gone)
    skew = {
        "measured_median_footprint_m2": round(float(have_series.median()), 1),
        "missing_median_footprint_m2": round(float(gone_series.median()), 1),
        "missing_are": ("smaller" if gone_series.median() < have_series.median()
                        else "larger" if gone_series.median() > have_series.median()
                        else "the_same_size"),
    }
    return skew


def _band_imputation(sized: list[dict], missing: list[str], cadastral: dict[str, float],
                     footprint: dict[str, float] | None, energy_kwh: float,
                     intensity_subset: float) -> tuple[float, list[dict]]:
    """Impute each missing building at the intensity of its own size band.

    Falls back to the flat imputation whenever the bands cannot carry meaning:
    no footprint column, too few measured buildings, or degenerate quantiles.
    """
    usable = [m for m in sized if _finite(m["footprint_m2"])]
    area_missing = sum((_finite(cadastral.get(ref)) or 0.0) for ref in missing)
    flat = energy_kwh + intensity_subset * area_missing
    if footprint is None or len(usable) < MIN_ROWS_FOR_BANDS:
        return flat, []

    frame = pd.DataFrame({
        "foot": [float(m["footprint_m2"]) for m in usable],
        "energy": [m["energy_kwh"] for m in usable],
        "area": [float(m["cadastral_m2"]) for m in usable],
    })
    edges = sorted(set(frame["foot"].quantile(list(BAND_QUANTILES)).tolist()))
    if len(edges) < 3:
        return flat, []
    # Open the outer edges so a missing building outside the measured range
    # still lands in a band instead of falling out of the imputation.
    edges[0], edges[-1] = -math.inf, math.inf

    frame["band"] = pd.cut(frame["foot"], edges)
    grouped = frame.groupby("band", observed=True)
    intensity_by_band, table = {}, []
    for band, group in grouped:
        area = float(group["area"].sum())
        if area <= 0:
            continue
        value = float(group["energy"].sum()) / area
        intensity_by_band[band] = value
        table.append({
            "footprint_from_m2": (None if math.isinf(band.left) else round(float(band.left), 1)),
            "footprint_to_m2": (None if math.isinf(band.right) else round(float(band.right), 1)),
            "buildings": int(len(group)),
            "intensity_kwh_m2": round(value, 3),
        })
    if not intensity_by_band:
        return flat, []

    imputed = energy_kwh
    for reference in missing:
        area = _finite(cadastral.get(reference))
        if area is None:
            continue
        foot = _finite((footprint or {}).get(reference))
        value = intensity_subset
        if foot is not None:
            placed = pd.cut(pd.Series([foot]), edges).iloc[0]
            value = intensity_by_band.get(placed, intensity_subset)
        imputed += value * area

    counts = {}
    for reference in missing:
        foot = _finite((footprint or {}).get(reference))
        if foot is None:
            continue
        placed = pd.cut(pd.Series([foot]), edges).iloc[0]
        counts[placed] = counts.get(placed, 0) + 1
    for entry, band in zip(table, intensity_by_band):
        entry["missing_buildings"] = int(counts.get(band, 0))
    return imputed, table


def _note(block: dict) -> str:
    truncation = block["total_truncation_pct"]
    skew = block.get("footprint_skew") or {}
    direction = block["intensity_bias_direction"]
    pieces = [
        f"totals are summed over the buildings that produced a result, which "
        f"carry {block['cadastral_area_coverage_pct']:.3f} % of the scope's "
        f"cadastral residential area, so the published total under-reports the "
        f"full scope by {truncation['low']:.3f}-{truncation['high']:.3f} %."
    ]
    if skew:
        pieces.append(
            f"The buildings without a result are {skew['missing_are']} than the "
            f"rest (median footprint {skew['missing_median_footprint_m2']:.1f} "
            f"against {skew['measured_median_footprint_m2']:.1f} m2)."
        )
    if direction == "negligible":
        pieces.append(
            f"Their intensity skew leaves the published "
            f"{block['intensity_subset_kwh_m2']:.3f} kWh/m2 within "
            f"{abs(block['intensity_bias_pct']):.3f} % of a full-scope estimate, "
            f"which is too small to correct for - but it is measured here rather "
            f"than assumed from the coverage percentage."
        )
    else:
        pieces.append(
            f"The published {block['intensity_subset_kwh_m2']:.3f} kWh/m2 is "
            f"therefore biased {direction} by "
            f"{abs(block['intensity_bias_pct']):.3f} % relative to a full-scope "
            f"estimate of {block['intensity_full_scope_kwh_m2']:.3f}."
        )
    return " ".join(pieces)
