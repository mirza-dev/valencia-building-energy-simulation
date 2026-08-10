"""Flag how many separate building masses the EU database sees inside one cadastral parcel.

WHAT THIS MEASURES, AND WHY IT IS THE ONE THING WORTH TAKING FROM THE EU EXPORT
------------------------------------------------------------------------------
The chain models one cadastral parcel as one prism: a single footprint extruded
to a single storey count.  That is what the cadastre describes, and for three
parcels in four it is right.  For the rest it is not: the EU Building Database
sees several distinct footprints inside the same `refparcela`, at different
heights, sometimes with different uses.

That is the *only* field-level thing the EU export adds.  Everything else in it
was measured on 2026-08-10 and found to be either derived or a processed copy of
our own source:

  - `net_floor_area` / `gross_floor_area` are not measurements.  `net/gross` is
    the same constant 0.7368 on all 11 768 matched buildings, and
    `gross/(footprint x storeys)` has a median of 0.9496 with 79.9 % of buildings
    inside 0.1 % of it.  The EU area IS footprint x storeys, rescaled twice.
    Tipo15 `442_sup_Residencial` is the cadastre's dwelling-by-dwelling record.
  - `HIM` height is derived the same way: 99.51 % of values are multiples of
    2.5 m, 100 % of the EUBucco-sourced ones are, and `HIM/HEI` has a median of
    exactly 2.50.  Genuinely surveyed heights exist for 114 buildings (0.43 %).
  - `DAT` year agrees with the cadastre on 92.43 % of buildings - but EUBUCCO's
    Spanish source IS the Spanish cadastre (Sci Data, 2023), so that agreement
    is shared provenance, not independent confirmation, and there is exactly one
    building in the city whose year the cadastre is missing.

The sub-footprint count is different because it comes from OpenStreetMap
geometry (`OCC_source` is OSM on 99.9 % of accepted matches), which is the one
genuinely independent layer in the file.

NOTHING HERE ENTERS THE SIMULATION
----------------------------------
This module writes a flag, not a physics input.  No geometry, storey count,
area or construction is changed by it; the same building simulated with and
without the flag produces byte-identical energy.  It exists so a total can say
what share of itself rests on the one-prism assumption - the same job
`large_footprint_single_zone` does for the single-zone assumption, and built to
the same pattern deliberately.

Whether to *act* on the flag - to build several masses per parcel instead of one
- is a separate decision that needs its own measurement, and that measurement is
what this flag is for.

THE ASSIGNMENT RULE
-------------------
An EU footprint belongs to a cadastral parcel when its representative point (a
point guaranteed to lie inside the polygon, unlike a centroid on a concave
shape) falls within that parcel.  Each EU footprint is therefore assigned to at
most one parcel, and a parcel's count is the number of footprints that landed in
it.  The rule is versioned, and its version is part of the fingerprint, so a
changed rule cannot be mistaken for changed data.

WHAT THE FLAG DOES NOT SAY
--------------------------
It does not say the EU geometry is right.  It says the two sources disagree
about how many buildings are there.  On `3748901YJ2734H` - 17 272 m2 of
cadastral polygon at `altura_max` 15 - the EU file shows ten footprints, two of
them at 42.5 m and three at 17.5 m.  The cadastre's single 15-storey prism is
wrong and the chain's capped 3-storey prism is also wrong; the flag marks the
building as one where neither source can be trusted to describe the massing, and
says nothing about what the right massing is.

Note also that this signal is NOT gated on attribute matching.  That same
building is `unmatched` under the guarded IoU rule in
`hybrid_building_profile.py` (IoU 0.091) precisely because its shape disagrees
with ours - which is the fragmentation itself.  Fusing attributes and counting
masses are two different jobs with two different gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

log = logging.getLogger("eu_footprint_flags")

# Bump when the assignment rule below changes.  It is part of the fingerprint,
# so results produced under two different rules can never be silently merged.
RULE_VERSION = 1
ASSIGNMENT_RULE = ("an EU footprint belongs to the cadastral parcel that "
                   "contains its representative point; each footprint is "
                   "assigned to at most one parcel")

DEFAULT_EU_SOURCE = Path("data/gis/all_bldg_category.gpkg")
CADASTRE_CRS = "EPSG:25830"
# Category 1 is the EU export's own "building" class; 2 is auxiliary geometry
# and 0 is incomplete.  Mixing them would count sheds as separate masses.
EU_BUILDING_CATEGORY = 1

FLAG_COLUMNS = ("eu_subfootprint_count", "eu_multi_footprint",
                "eu_height_spread_m", "eu_subfootprint_area_m2",
                "eu_distinct_use_classes")


class EuSourceError(RuntimeError):
    """The EU source cannot be used as given.  Never fall back to a guess."""


def _taxonomy_value(raw, key: str, prefix: str) -> float | None:
    """Pull a number out of the EU taxonomy JSON, e.g. HIM 'HHT:17.50' -> 17.5."""
    if not isinstance(raw, str):
        return None
    try:
        taxonomy = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(taxonomy, dict):
        return None
    value = taxonomy.get(key)
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    try:
        return float(value[len(prefix):])
    except ValueError:
        return None


def _taxonomy_text(raw, key: str) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        taxonomy = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return taxonomy.get(key) if isinstance(taxonomy, dict) else None


def source_fingerprint(path: Path) -> str:
    """Content-addressed identity of the EU source AND of the rule applied to it.

    Stamped on every annotated row, so the layer behind a published share cannot
    be swapped without the number changing with it.
    """
    path = Path(path)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    identity = {"file": digest.hexdigest(), "rule_version": RULE_VERSION,
                "category": EU_BUILDING_CATEGORY}
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


def load_eu_footprints(path: Path, target_crs: str = CADASTRE_CRS) -> gpd.GeoDataFrame:
    """Read the EU building footprints, refusing anything ambiguous.

    Fail-closed on purpose: a silently empty or unprojected layer would report
    "no fragmentation anywhere", which is the most misleading answer available.
    """
    path = Path(path)
    if not path.exists():
        raise EuSourceError(f"EU source not found: {path}")
    frame = gpd.read_file(path)
    if frame.crs is None:
        raise EuSourceError(f"{path.name}: layer has no CRS; cannot be reprojected")
    if "category" not in frame.columns:
        raise EuSourceError(f"{path.name}: no 'category' column; "
                            "cannot separate buildings from auxiliary geometry")
    frame = frame[frame["category"] == EU_BUILDING_CATEGORY].copy()
    if frame.empty:
        raise EuSourceError(
            f"{path.name}: no category-{EU_BUILDING_CATEGORY} features")
    frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    if frame.empty:
        raise EuSourceError(f"{path.name}: every category-1 feature is empty")
    return frame.to_crs(target_crs)


def subfootprint_flags(cadastre: gpd.GeoDataFrame,
                       eu_footprints: gpd.GeoDataFrame) -> pd.DataFrame:
    """One row per `refparcela`: how many EU masses sit inside it, and how tall.

    `eu_height_spread_m` is the range of *surveyed-or-derived* EU heights within
    the parcel, and is None when fewer than two of the parcel's footprints carry
    a height.  A large spread means the parcel holds masses of visibly different
    size - the case a single extruded prism cannot represent at all.
    """
    if cadastre.crs is None:
        raise EuSourceError("cadastre frame has no CRS")
    if eu_footprints.crs is None:
        raise EuSourceError("EU frame has no CRS")
    if eu_footprints.crs != cadastre.crs:
        eu_footprints = eu_footprints.to_crs(cadastre.crs)

    parcels = cadastre[["refparcela", "geometry"]].copy()
    references = pd.Index(parcels["refparcela"].astype(str).unique(), name="refparcela")

    points = eu_footprints.copy()
    points["_eu_area_m2"] = points.geometry.area
    points["_eu_height_m"] = [_taxonomy_value(t, "HIM", "HHT:")
                              for t in points.get("taxonomy", pd.Series(dtype=object))]
    points["_eu_use"] = [_taxonomy_text(t, "OCC")
                         for t in points.get("taxonomy", pd.Series(dtype=object))]
    points = points[["_eu_area_m2", "_eu_height_m", "_eu_use", "geometry"]].copy()
    points["geometry"] = points.geometry.representative_point()

    joined = gpd.sjoin(points, parcels, predicate="within", how="inner")
    if joined.empty:
        grouped = pd.DataFrame(index=references)
        grouped["eu_subfootprint_count"] = 0
        grouped["eu_subfootprint_area_m2"] = 0.0
        grouped["eu_height_spread_m"] = None
        grouped["eu_distinct_use_classes"] = 0
        grouped["eu_multi_footprint"] = False
        return grouped.reset_index()

    joined["refparcela"] = joined["refparcela"].astype(str)
    grouped = joined.groupby("refparcela").agg(
        eu_subfootprint_count=("_eu_area_m2", "size"),
        eu_subfootprint_area_m2=("_eu_area_m2", "sum"),
        _height_n=("_eu_height_m", "count"),
        _height_min=("_eu_height_m", "min"),
        _height_max=("_eu_height_m", "max"),
        eu_distinct_use_classes=("_eu_use", "nunique"),
    )
    spread = (grouped["_height_max"] - grouped["_height_min"]).where(
        grouped["_height_n"] >= 2)
    grouped["eu_height_spread_m"] = spread.round(2)
    grouped = grouped.drop(columns=["_height_n", "_height_min", "_height_max"])

    grouped = grouped.reindex(references)
    grouped["eu_subfootprint_count"] = (
        grouped["eu_subfootprint_count"].fillna(0).astype(int))
    grouped["eu_subfootprint_area_m2"] = (
        grouped["eu_subfootprint_area_m2"].fillna(0.0).round(1))
    grouped["eu_distinct_use_classes"] = (
        grouped["eu_distinct_use_classes"].fillna(0).astype(int))
    # A parcel the EU file does not cover at all (count 0) is NOT multi-mass; it
    # is unmeasured, and the coverage figure in the block below says how many
    # such parcels a share was computed over.
    grouped["eu_multi_footprint"] = grouped["eu_subfootprint_count"] >= 2
    return grouped.reset_index()


def flags_by_reference(cadastre: gpd.GeoDataFrame, eu_path: Path) -> dict[str, dict]:
    """`refparcela` -> flag dict, ready to merge into a ledger row."""
    eu = load_eu_footprints(eu_path, target_crs=str(cadastre.crs or CADASTRE_CRS))
    frame = subfootprint_flags(cadastre, eu)
    fingerprint = source_fingerprint(eu_path)
    out: dict[str, dict] = {}
    for record in frame.to_dict("records"):
        reference = record.pop("refparcela")
        spread = record.get("eu_height_spread_m")
        record["eu_height_spread_m"] = (None if spread is None or pd.isna(spread)
                                        else float(spread))
        record["eu_multi_footprint"] = bool(record["eu_multi_footprint"])
        record["eu_source_fingerprint"] = fingerprint
        out[str(reference)] = record
    return out


def fragmentation_block(frame: pd.DataFrame) -> dict:
    """What share of a total rests on the one-prism-per-parcel assumption.

    Deliberately shaped like `zoning_block`: buildings, area and energy, so the
    three assumptions a total leans on can be read side by side rather than one
    of them being invisible.
    """
    block = {"rule_version": RULE_VERSION, "assignment_rule": ASSIGNMENT_RULE,
             "scheme": "one_extruded_prism_per_cadastral_parcel"}
    if "eu_subfootprint_count" not in frame or frame.empty:
        return block

    counts = pd.to_numeric(frame["eu_subfootprint_count"], errors="coerce")
    covered = counts.notna() & (counts > 0)
    flagged = counts.fillna(0) >= 2
    area = frame["res_area_m2"]
    energy = frame["total_site_kwh_m2"] * area
    total_area = float(area.sum())
    total_energy = float(energy.sum())

    block.update({
        "buildings_measured": int(covered.sum()),
        "buildings_measured_pct": round(100.0 * float(covered.sum()) / len(frame), 2),
        "buildings": int(flagged.sum()),
        "buildings_pct": round(100.0 * float(flagged.sum()) / len(frame), 2),
        "residential_area_pct": (round(100.0 * float(area[flagged].sum()) / total_area, 2)
                                 if total_area else 0.0),
        "total_site_pct": (round(100.0 * float(energy[flagged].sum()) / total_energy, 2)
                           if total_energy else 0.0),
    })
    if "eu_height_spread_m" in frame:
        spread = pd.to_numeric(frame["eu_height_spread_m"], errors="coerce")
        measured = spread.notna()
        if measured.any():
            block["height_spread_measured"] = int(measured.sum())
            block["height_spread_median_m"] = round(float(spread[measured].median()), 2)
            block["height_spread_p75_m"] = round(float(spread[measured].quantile(0.75)), 2)
            block["height_spread_ge_10m"] = int((spread.fillna(0) >= 10).sum())
    if "storey_cap_applied" in frame:
        capped = frame["storey_cap_applied"].fillna(False).astype(bool)
        if capped.any():
            block["multi_footprint_pct_of_capped"] = round(
                100.0 * float((flagged & capped).sum()) / float(capped.sum()), 2)
        if (~capped).any():
            block["multi_footprint_pct_of_uncapped"] = round(
                100.0 * float((flagged & ~capped).sum()) / float((~capped).sum()), 2)
    if "eu_source_fingerprint" in frame:
        stamps = sorted({str(v) for v in frame["eu_source_fingerprint"].dropna()})
        block["source_fingerprint"] = stamps[0] if len(stamps) == 1 else stamps
    block["note"] = (
        "the chain models each cadastral parcel as ONE extruded prism.  These "
        "are the buildings where the EU footprint layer sees two or more "
        "separate masses inside that parcel, so neither the cadastre's single "
        "prism nor the chain's capped version describes the real massing.  "
        "Their energy is included in every total above; this is the share they "
        "account for.  The flag changes no physics.")
    return block


def annotate_ledger(ledger_path: Path, cadastre_path: Path, eu_path: Path) -> dict:
    """Measure fragmentation for an existing run WITHOUT touching its ledger.

    A committed ledger is evidence; re-writing it to add a column would destroy
    that.  The measurement lands in a sidecar beside it instead, and carries the
    ledger's own SHA-256 so the pair can always be shown to belong together.
    """
    import stock_runner as sr           # local: keeps this module importable alone

    rows = sr.read_ledger(Path(ledger_path))
    ok = [r for r in sr.latest_per_reference(rows) if r.get("status") == "ok"]
    if not ok:
        raise EuSourceError(f"{ledger_path}: no ok rows to annotate")

    cadastre = gpd.read_file(cadastre_path)
    if cadastre.crs is None:
        raise EuSourceError(f"{cadastre_path}: cadastre has no CRS")
    flags = flags_by_reference(cadastre, Path(eu_path))

    frame = pd.DataFrame(ok)
    for column in FLAG_COLUMNS:
        frame[column] = [flags.get(str(r), {}).get(column) for r in frame["refparcela"]]
    frame["eu_source_fingerprint"] = source_fingerprint(Path(eu_path))

    block = fragmentation_block(frame)
    flagged = pd.to_numeric(frame["eu_subfootprint_count"], errors="coerce").fillna(0) >= 2
    worst = frame[flagged].copy()
    worst["_energy"] = worst["total_site_kwh_m2"] * worst["res_area_m2"]
    worst = worst.nlargest(min(10, len(worst)), "_energy")
    return {
        "schema_version": 1,
        "ledger": {"path": str(ledger_path), "sha256": sr.file_sha256(Path(ledger_path)),
                   "ok_rows": len(ok)},
        "eu_source": {"path": str(eu_path),
                      "fingerprint": source_fingerprint(Path(eu_path))},
        "cadastre": {"path": str(cadastre_path)},
        "fragmentation": block,
        "top_by_energy": [
            {"refparcela": r["refparcela"],
             "eu_subfootprint_count": int(r["eu_subfootprint_count"]),
             "eu_height_spread_m": (None if pd.isna(r["eu_height_spread_m"])
                                    else float(r["eu_height_spread_m"])),
             "storey_cap_applied": bool(r.get("storey_cap_applied", False)),
             "res_area_m2": float(r["res_area_m2"]),
             "total_site_kwh_m2": float(r["total_site_kwh_m2"]),
             "share_of_total_site_pct": round(
                 100.0 * float(r["_energy"])
                 / float((frame["total_site_kwh_m2"] * frame["res_area_m2"]).sum()), 3)}
            for r in worst.to_dict("records")],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure EU sub-footprint fragmentation for a finished run")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--cadastre", type=Path,
                        default=Path("data/gis/DatosRai_ciudadValencia.shp"))
    parser.add_argument("--eu", type=Path, default=DEFAULT_EU_SOURCE)
    parser.add_argument("--output", type=Path,
                        help="default: eu_fragmentation.json beside the ledger")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    report = annotate_ledger(args.ledger, args.cadastre, args.eu)
    out = args.output or Path(args.ledger).parent / "eu_fragmentation.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    block = report["fragmentation"]
    log.info("measured %s of %s ok buildings",
             block.get("buildings_measured"), report["ledger"]["ok_rows"])
    log.info("multi-mass parcels: %s (%.2f %% of buildings, %.2f %% of area, "
             "%.2f %% of site energy)",
             block.get("buildings"), block.get("buildings_pct", 0.0),
             block.get("residential_area_pct", 0.0), block.get("total_site_pct", 0.0))
    log.info("written %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
