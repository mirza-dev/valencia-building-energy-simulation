"""Build a multi-mass parcel as several masses instead of one prism, and measure the difference.

WHY THIS EXISTS
---------------
`eu_footprint_flags` measured that 98 Benicalap parcels - 10.1 % of the
buildings, 21.6 % of the dwelling area, 19.3 % of the site energy - hold two or
more separate EU building footprints inside one `refparcela`.  The chain models
each of them as ONE footprint extruded to ONE storey count.  The flag says how
much of the published total rests on that assumption; it deliberately says
nothing about what the assumption costs.  This module answers that, and only
that.

It is a measurement, not a proposed replacement.  Nothing here runs inside a
stock run, nothing is written into `stock_runner`, and the published v8 numbers
are read as the baseline and never rewritten.

THE FROZEN MODULES ARE NOT TOUCHED
----------------------------------
`model_builder.build_model_with_config` takes its geometry and its storey count
from the row it is handed, and `verified_model.simulate_verified_building`
already accepts a `gis_path` (where the target row is read) separately from a
`neighbours_path` (where the surrounding mass is read).  So a mass is simulated
by handing the same frozen engine a different row - exactly the trick the v8
storey cap used.  `model_builder.py`, `run_simulation.py` and `deep_building.py`
are byte-identical after this module as before it, and every run still passes
through `assert_profile_intact()`.

HOW A PARCEL IS SPLIT
---------------------
Only the EU footprints the engine could actually build are kept: `prepare_footprint`
refuses anything under 50 m2 or over 20 000 m2, or whose simplification moves the
area too far.  Measured on Benicalap, 98 of 255 masses fall under the 50 m2 floor
- those are sheds and stair cores, not buildings - and 68 of the 98 flagged
parcels are left with fewer than two buildable masses.  Those parcels are NOT
split: one real mass plus outbuildings is a case the single prism already
describes.  What remains is 30 parcels, 13.3 % of the district's energy.

The cadastral dwelling area is *conserved*, not re-derived:

    b_i     storeys the EU layer implies for mass i (see `mass_storeys`)
    w_i     f_i * b_i / sum(f_j * b_j)          - volume share
    A_i     A_cadastral * w_i                    - allocated dwelling area
    people  padron head count * w_i

Each mass then goes through the *existing* cap and mixed-use rules unchanged, so
the storeys it ends up conditioning are decided the same way they are for every
other building in the stock.  Conserving A means the comparison isolates the
question actually asked - what does the SHAPE cost - instead of confounding it
with a bigger or smaller building.

WHAT THE COMPARISON CAN AND CANNOT SEE
--------------------------------------
It can see envelope-to-volume: several slim masses have more exposed wall and
roof per m2 than one squat prism of the same floor area, and they shade each
other.  It cannot see anything the EU layer gets wrong: the masses are OSM
traces, and where they are drawn with a gap between adjacent blocks the party
wall between them will not be detected, which pushes heating up.  That is
measured per parcel (`sibling_contact_m` against `party_detected_m`) and
reported beside the result rather than silently repaired, because snapping the
geometry would be inventing the very thing under test.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deep_building as db
import eu_footprint_flags as euf
import model_builder as mb
import stock_runner as sr
import verified_model as vm

log = logging.getLogger("eu_mass_split")

# Bump when the split rule below changes: it travels in every record, so two
# rules can never be averaged together by accident.
SPLIT_RULE_VERSION = 1

# `altura_max` counts the highest planta that holds a dwelling and EXCLUDES the
# bajo; the EU `HEI` counts storeys including it.  Measured across the city on
# 2026-08-10: raw median difference +2, but with this correction |diff| <= 1 on
# 85.8 % of buildings, and on the 539 buildings whose floor count we imputed the
# median difference is 0.  So the offset is the data's, not a fitted constant.
HEI_TO_ALTURA_MAX_OFFSET = 1
# The EU's own metres-per-storey, recovered from its own file: HIM/HEI has a
# median of exactly 2.50 there.  Converting HIM with our 3.0 m storey would
# understate the EU's storey count by about a sixth and quietly flatten the
# taller masses - the opposite of what is being measured.
EU_METRES_PER_STOREY = 2.50

# Nearby cadastre kept in a parcel's context file.  `CONTEXT_RADIUS` is 50 m;
# the margin is wider so a mass near the parcel edge still sees everything the
# baseline building saw.
CONTEXT_MARGIN_M = 250.0

MASS_SUFFIX = "_M{index}"


class SplitError(RuntimeError):
    """The parcel cannot be split as asked.  Never guess a massing."""


# ---------------------------------------------------------------------------
# Planning: which parcels, which masses, how much of the parcel each one gets
# ---------------------------------------------------------------------------
def mass_storeys(taxonomy, parent_altura_max: int) -> tuple[int, str]:
    """`altura_max` for one EU mass, and where the number came from.

    `HEI` is the EU's own storey count and is preferred wherever it exists.
    `HIM` is a height, and 99.5 % of its values are multiples of 2.5 m, so it is
    converted with the EU's own 2.5 m storey rather than with ours.  With
    neither, the mass inherits the parcel's cadastral count: the split then
    carries no height information at all and says so, instead of inventing one.
    """
    hei = euf._taxonomy_value(taxonomy, "HEI", "H:")
    if hei is not None and hei >= 1:
        return max(1, int(round(hei)) - HEI_TO_ALTURA_MAX_OFFSET), "eu_hei"
    him = euf._taxonomy_value(taxonomy, "HIM", "HHT:")
    if him is not None and him > 0:
        storeys = int(round(him / EU_METRES_PER_STOREY))
        return max(1, storeys - HEI_TO_ALTURA_MAX_OFFSET), "eu_him"
    return max(1, int(parent_altura_max)), "parent_cadastre"


def buildable_masses(parcel_geom, eu_masses: gpd.GeoDataFrame,
                     parent_altura_max: int) -> tuple[list[dict], list[dict]]:
    """Split the EU footprints of one parcel into (buildable, refused).

    The gate is the engine's own `prepare_footprint`, not a threshold rewritten
    here: whatever the chain refuses to build for a whole building it must also
    refuse for a mass, or the split would quietly widen the model's own limits.
    """
    kept: list[dict] = []
    refused: list[dict] = []
    for _, mass in eu_masses.iterrows():
        storeys, source = mass_storeys(mass.get("taxonomy"), parent_altura_max)
        record = {"eu_area_m2": round(float(mass.geometry.area), 1),
                  "altura_max": storeys, "height_source": source,
                  "geometry": mass.geometry}
        try:
            polygon = mb.clean_polygon(mass.geometry)
            _, footprint_m2 = mb.prepare_footprint(polygon)
        except Exception as exc:                       # noqa: BLE001
            refused.append({**record, "reason": str(exc)[:120]})
            continue
        # The single-part polygon the builder will extrude, kept so the contact
        # measurement below is made on the same shape that gets built.
        record["polygon"] = polygon
        record["footprint_m2"] = round(footprint_m2, 1)
        kept.append(record)
    return kept, refused


def allocate(parent: pd.Series, masses: list[dict]) -> list[dict]:
    """Share the parcel's cadastral area, head count and dwellings across masses.

    Weighted by built volume (footprint x EU storeys), which is the only thing
    the EU layer actually tells us about relative size.  The totals are
    conserved exactly - the split redistributes the parcel, it does not create
    or destroy any of it - so the comparison against the single prism is not
    confounded by a different amount of building.
    """
    if len(masses) < 2:
        raise SplitError("a split needs at least two buildable masses")
    volumes = [m["footprint_m2"] * m["altura_max"] for m in masses]
    total = sum(volumes)
    if total <= 0:
        raise SplitError("every mass has zero volume")

    def _share(value, weight: float) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number * weight if number > 0 else number

    out = []
    for mass, volume in zip(masses, volumes):
        weight = volume / total
        allocated = {**mass, "volume_share": round(weight, 6)}
        allocated["tipo15_res_area_m2"] = _share(parent.get("tipo15_res_area_m2"), weight)
        allocated["pob_total"] = _share(parent.get("pob_total"), weight)
        allocated["num_vivend"] = _share(parent.get("num_vivend"), weight)
        # The rule the chain itself will apply, precomputed so the sibling
        # shading heights match what each mass is actually extruded to.  Checked
        # against the run afterwards; see `_verify_storeys`.
        built = allocated["altura_max"] + (1 if parent.get("ground_use") == "residential" else 0)
        kept = db.residential_storeys_from_cadastre(
            allocated["tipo15_res_area_m2"], allocated["footprint_m2"], built)
        allocated["expected_storeys_effective"] = kept
        allocated["modelled_altura_max"] = max(
            1, kept - (1 if parent.get("ground_use") == "residential" else 0))
        out.append(allocated)
    return out


def plan_parcels(prepared: gpd.GeoDataFrame, eu_path: Path,
                 references: list[str] | None = None) -> list[dict]:
    """Everything decided before any EnergyPlus runs: which parcels split how."""
    eu = euf.load_eu_footprints(eu_path)
    parcels = prepared[["refparcela", "geometry"]].copy()
    points = eu.copy()
    points["geometry"] = points.geometry.representative_point()
    joined = gpd.sjoin(points, parcels, how="inner", predicate="within")
    eu = eu.loc[joined.index].copy()
    eu["_parent"] = joined["refparcela"].values

    indexed = prepared.set_index("refparcela")
    plans = []
    for ref, group in eu.groupby("_parent"):
        if references is not None and ref not in references:
            continue
        if len(group) < 2 or ref not in indexed.index:
            continue
        parent = indexed.loc[ref]
        if isinstance(parent, pd.DataFrame):       # duplicate refparcela
            continue
        kept, refused = buildable_masses(parent.geometry, group, int(parent["altura_max"]))
        entry = {"refparcela": ref, "eu_masses": len(group),
                 "buildable_masses": len(kept),
                 "refused": [{k: v for k, v in r.items() if k != "geometry"}
                             for r in refused]}
        if len(kept) < 2:
            entry["skipped"] = "fewer than two buildable masses"
            plans.append(entry)
            continue
        entry["masses"] = allocate(parent, kept)
        entry.update(sibling_geometry(kept))
        plans.append(entry)
    return plans


def sibling_geometry(masses: list[dict]) -> dict:
    """How the kept masses sit relative to each other: touching, or how far apart.

    This decides whether the result can be believed.  Masses that touch get a
    party wall and lose the facade between them; masses drawn with a small gap
    get two fully glazed facades a metre apart, which is a tracing artefact, not
    a building.  Measured on Benicalap: the nearest-sibling gap has a median of
    7.3 m and only three parcels of thirty sit between 0.5 m and 2 m - so these
    are genuinely separate blocks around a courtyard, and the extra exposed
    envelope the split reports is real rather than an artefact of the trace.
    The numbers travel with every result so that judgement can be re-made.
    """
    contact = 0.0
    gaps = []
    for i, a in enumerate(masses):
        for b in masses[i + 1:]:
            shared = a["polygon"].exterior.intersection(b["polygon"].exterior)
            contact += getattr(shared, "length", 0.0)
            gaps.append(a["polygon"].distance(b["polygon"]))
    return {"sibling_contact_m": round(contact, 2),
            "sibling_gap_min_m": round(min(gaps), 2) if gaps else None,
            "sibling_gap_max_m": round(max(gaps), 2) if gaps else None}


# ---------------------------------------------------------------------------
# Writing the two files the frozen chain reads
# ---------------------------------------------------------------------------
def _mass_reference(parent_ref: str, index: int) -> str:
    """A reference that is safe as a directory name - the run dir is `<ref>_deep`."""
    return f"{parent_ref}{MASS_SUFFIX.format(index=index)}"


def write_targets(prepared: gpd.GeoDataFrame, plans: list[dict],
                  path: Path) -> gpd.GeoDataFrame:
    """One row per mass, carrying the parent's full schema with the split values."""
    indexed = prepared.set_index("refparcela")
    rows = []
    for plan in plans:
        if "masses" not in plan:
            continue
        parent = indexed.loc[plan["refparcela"]]
        for index, mass in enumerate(plan["masses"], start=1):
            row = parent.to_dict()
            row.update({
                "refparcela": _mass_reference(plan["refparcela"], index),
                "geometry": mass["polygon"],
                "altura_max": int(mass["altura_max"]),
                "tipo15_res_area_m2": mass["tipo15_res_area_m2"],
                "pob_total": mass["pob_total"],
                "num_vivend": mass["num_vivend"],
                "footprint_area_m2": mass["footprint_m2"],
            })
            rows.append(row)
    frame = gpd.GeoDataFrame(rows, geometry="geometry", crs=prepared.crs)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_file(path, driver="GPKG")
    return frame


def write_context(cadastre: gpd.GeoDataFrame, plan: dict, targets: gpd.GeoDataFrame,
                  path: Path) -> None:
    """The surrounding mass one parcel's own masses stand in.

    The parent polygon is REMOVED: leaving it in would shade every mass with a
    phantom prism of the whole parcel at its uncapped height - the very object
    the split exists to replace.  The siblings are added at the storey count
    they are actually extruded to, so a mass is shaded by what its neighbours
    really are.  Every other parcel nearby stays as its single cadastral prism,
    including other flagged ones: only the parcel under test is split, so the
    difference measured is that parcel's alone.
    """
    own = targets[targets["refparcela"].str.startswith(plan["refparcela"] + MASS_SUFFIX[:2])]
    bounds = own.total_bounds
    minx, miny, maxx, maxy = bounds
    window = cadastre.cx[minx - CONTEXT_MARGIN_M:maxx + CONTEXT_MARGIN_M,
                         miny - CONTEXT_MARGIN_M:maxy + CONTEXT_MARGIN_M]
    window = window[window["refparcela"] != plan["refparcela"]]

    siblings = gpd.GeoDataFrame(
        {"refparcela": own["refparcela"].tolist(),
         "altura_max": [m["modelled_altura_max"] for m in plan["masses"]]},
        geometry=own.geometry.tolist(), crs=cadastre.crs)
    keep = [c for c in ("refparcela", "altura_max", "geometry") if c in window.columns]
    merged = pd.concat([window[keep], siblings], ignore_index=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame(merged, geometry="geometry", crs=cadastre.crs).to_file(
        path, driver="GPKG")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
_WORKER: dict = {}


def _init_worker(config: dict) -> None:
    tmp = Path(config["tmp_root"]) / f"w{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    for key in ("TMPDIR", "TMP", "TEMP"):
        os.environ[key] = str(tmp)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    _WORKER.update(config)


def run_mass(task: tuple[str, str, str]) -> dict:
    """Simulate one mass.  Never raises: a failure is a record like any other."""
    reference, context_path, parent_ref = task
    started = time.time()
    out_dir = Path(_WORKER["out_dir"])
    try:
        summary, qa_passed = vm.simulate_verified_building(
            reference, out_dir,
            zero_policy=_WORKER["zero_policy"],
            gis_path=Path(_WORKER["targets"]),
            neighbors_path=Path(context_path))
        row = {**summary, "status": "ok", "qa_all_passed": qa_passed}
    except Exception as exc:                            # noqa: BLE001
        row = {"refparcela": reference, "status": "failed",
               "error": f"{type(exc).__name__}: {exc}"[:400]}
    row["parent_refparcela"] = parent_ref
    row["seconds"] = round(time.time() - started, 1)
    if _WORKER.get("prune"):
        # The same list the stock runner prunes, not a wider one: the error log,
        # the layer dump, the profile stamp and the model stay, so every mass
        # here is auditable exactly as a stock building is.
        run_dir = out_dir / f"{reference}_deep"
        for name in sr.PRUNABLE_ARTIFACTS:
            target = run_dir / name
            if target.exists():
                target.unlink()
    return row


def _verify_storeys(rows: list[dict], plans: list[dict]) -> list[str]:
    """The storeys the chain kept must equal the ones the siblings were shaded at.

    If these ever disagree, a mass was shaded by a neighbour of a different
    height than the one that was built, and the comparison is measuring two
    geometries at once.
    """
    expected = {}
    for plan in plans:
        for index, mass in enumerate(plan.get("masses", []), start=1):
            expected[_mass_reference(plan["refparcela"], index)] = \
                mass["expected_storeys_effective"]
    problems = []
    for row in rows:
        if row.get("status") != "ok":
            continue
        want = expected.get(row["refparcela"])
        got = row.get("residential_storeys_effective")
        if want is not None and got is not None and int(want) != int(got):
            problems.append(f"{row['refparcela']}: shaded at {want}, built {got}")
    return problems


# ---------------------------------------------------------------------------
# Comparison against the published baseline
# ---------------------------------------------------------------------------
def compare(rows: list[dict], baseline: dict[str, dict],
            plans: list[dict]) -> dict:
    """Per parcel and in total: what the split costs against the single prism.

    THE SPLIT CHANGES TWO THINGS AT ONCE, so the result is reported three ways.

    The first is size.  The EU footprints of a parcel do not sum to the
    cadastral footprint - they are traced from a different source - and each
    mass rounds its own storey count up independently, so the split can build
    measurably more or less floor area than the single prism.  That is a real
    disagreement between the two sources, but it is not a massing effect.

    The second is shape: several slim masses have more exposed wall and roof per
    m2 than one squat prism, and they shade each other.

    So: absolute energy (what a district total would actually change by), energy
    per m2 of MODELLED floor area (the shape effect with the size effect divided
    out), and the modelled-area ratio itself (the size effect on its own).
    Reading only the first would credit the massing with a change that is mostly
    the two footprint sources disagreeing.
    """
    by_parent: dict[str, list[dict]] = {}
    for row in rows:
        by_parent.setdefault(row.get("parent_refparcela"), []).append(row)
    contacts = {p["refparcela"]: p for p in plans}

    parcels, incomplete = [], []
    for parent, masses in sorted(by_parent.items()):
        base = baseline.get(parent)
        if base is None:
            continue
        failed = [m for m in masses if m.get("status") != "ok"]
        if failed:
            incomplete.append({"refparcela": parent,
                               "failed": [m["refparcela"] for m in failed],
                               "errors": [m.get("error") for m in failed]})
            continue
        cadastral = float(base.get("tipo15_res_area_m2") or 0.0)
        split_kwh = sum(float(m["total_site_kwh"]) for m in masses)
        split_heat = sum(float(m["space_heating_kwh_m2"]) * float(m["res_area_m2"])
                         for m in masses)
        split_cool = sum(float(m["cooling_kwh_m2"]) * float(m["res_area_m2"])
                         for m in masses)
        base_kwh = float(base["total_site_kwh"])
        base_area = float(base["res_area_m2"])
        split_area = sum(float(m["res_area_m2"]) for m in masses)
        base_footprint = float(base.get("footprint_m2") or 0.0)
        split_footprint = sum(float(m["footprint_m2"]) for m in masses)
        plan = contacts.get(parent, {})
        parcels.append({
            "refparcela": parent,
            "masses": len(masses),
            "eu_masses": plan.get("eu_masses"),
            "height_sources": sorted({m["height_source"] for m in plan.get("masses", [])}),
            "cadastral_area_m2": round(cadastral, 1),
            "baseline_modelled_area_m2": round(base_area, 1),
            "split_modelled_area_m2": round(split_area, 1),
            "modelled_area_ratio": round(split_area / base_area, 4) if base_area else None,
            "baseline_footprint_m2": round(base_footprint, 1),
            "split_footprint_sum_m2": round(split_footprint, 1),
            "footprint_ratio": round(split_footprint / base_footprint, 4) if base_footprint else None,
            "baseline_total_site_kwh": round(base_kwh, 1),
            "split_total_site_kwh": round(split_kwh, 1),
            "delta_pct": round(100 * (split_kwh - base_kwh) / base_kwh, 2) if base_kwh else None,
            "baseline_kwh_per_modelled_m2": round(base_kwh / base_area, 2) if base_area else None,
            "split_kwh_per_modelled_m2": round(split_kwh / split_area, 2) if split_area else None,
            "intensity_delta_pct": round(
                100 * ((split_kwh / split_area) - (base_kwh / base_area)) / (base_kwh / base_area), 2)
            if base_area and split_area else None,
            "baseline_kwh_per_cadastral_m2": round(base_kwh / cadastral, 2) if cadastral else None,
            "split_kwh_per_cadastral_m2": round(split_kwh / cadastral, 2) if cadastral else None,
            "baseline_heating_kwh": round(float(base["space_heating_kwh_m2"]) * base_area, 1),
            "split_heating_kwh": round(split_heat, 1),
            "baseline_cooling_kwh": round(float(base["cooling_kwh_m2"]) * base_area, 1),
            "split_cooling_kwh": round(split_cool, 1),
            "baseline_party_surfaces": base.get("n_party_surfaces"),
            "split_party_surfaces": sum(int(m.get("n_party_surfaces") or 0) for m in masses),
            "sibling_contact_m": plan.get("sibling_contact_m"),
            "sibling_gap_min_m": plan.get("sibling_gap_min_m"),
            "qa_all_passed": all(bool(m.get("qa_all_passed")) for m in masses),
            "storey_cap_applied_baseline": bool(base.get("storey_cap_applied")),
        })

    district_total = sum(float(r["total_site_kwh"]) for r in baseline.values())
    covered_base = sum(p["baseline_total_site_kwh"] for p in parcels)
    covered_split = sum(p["split_total_site_kwh"] for p in parcels)
    area_base = sum(p["baseline_modelled_area_m2"] for p in parcels)
    area_split = sum(p["split_modelled_area_m2"] for p in parcels)
    intensity_base = covered_base / area_base if area_base else None
    intensity_split = covered_split / area_split if area_split else None
    return {
        "split_rule_version": SPLIT_RULE_VERSION,
        "parcels_compared": len(parcels),
        "parcels_incomplete": incomplete,
        "district_total_site_kwh": round(district_total, 1),
        "covered_baseline_kwh": round(covered_base, 1),
        "covered_split_kwh": round(covered_split, 1),
        "covered_share_of_district_pct": round(100 * covered_base / district_total, 3)
        if district_total else None,
        "delta_on_covered_pct": round(100 * (covered_split - covered_base) / covered_base, 2)
        if covered_base else None,
        "delta_on_district_pct": round(100 * (covered_split - covered_base) / district_total, 3)
        if district_total else None,
        "covered_baseline_modelled_area_m2": round(area_base, 1),
        "covered_split_modelled_area_m2": round(area_split, 1),
        "modelled_area_ratio": round(area_split / area_base, 4) if area_base else None,
        "baseline_kwh_per_modelled_m2": round(intensity_base, 2) if intensity_base else None,
        "split_kwh_per_modelled_m2": round(intensity_split, 2) if intensity_split else None,
        "intensity_delta_pct": round(
            100 * (intensity_split - intensity_base) / intensity_base, 2)
        if intensity_base and intensity_split else None,
        "parcels": sorted(parcels, key=lambda p: -p["baseline_total_site_kwh"]),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def load_baseline(ledger: Path) -> dict[str, dict]:
    rows = [json.loads(line) for line in Path(ledger).read_text().splitlines() if line.strip()]
    return {r["refparcela"]: r for r in rows if r.get("status") == "ok"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild multi-mass parcels as several masses and measure the difference")
    parser.add_argument("--prepared", type=Path, required=True,
                        help="prepared stock the baseline run used")
    parser.add_argument("--ledger", type=Path, required=True,
                        help="finished ledger.jsonl to compare against")
    parser.add_argument("--eu", type=Path, default=euf.DEFAULT_EU_SOURCE)
    parser.add_argument("--cadastre", type=Path, default=Path(mb.NEIGHBORS_SHP))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--top", type=int, default=None,
                        help="only the N parcels with the largest baseline energy")
    parser.add_argument("--only", nargs="*", default=None,
                        help="explicit refparcela list (overrides --top)")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--keep-runs", action="store_true",
                        help="keep each mass's EnergyPlus directory (large)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    baseline = load_baseline(args.ledger)
    prepared = gpd.read_file(args.prepared).drop_duplicates(
        subset="refparcela", keep="first")
    prepared = prepared[prepared["refparcela"].isin(baseline)].copy()
    log.info("[baseline] %s buildings from %s", len(baseline), args.ledger)

    references = args.only
    if references is None and args.top:
        flagged = sorted(baseline.values(),
                         key=lambda r: -float(r.get("total_site_kwh") or 0))
        references = [r["refparcela"] for r in flagged]
    plans = plan_parcels(prepared, args.eu, references=None)
    splittable = [p for p in plans if "masses" in p]
    if references is not None:
        order = {ref: i for i, ref in enumerate(references)}
        splittable = [p for p in splittable if p["refparcela"] in order]
        splittable.sort(key=lambda p: order[p["refparcela"]])
        if args.top:
            splittable = splittable[:args.top]

    log.info("[plan] %s flagged parcels, %s splittable, %s selected (%s masses)",
             len(plans), len([p for p in plans if "masses" in p]), len(splittable),
             sum(len(p["masses"]) for p in splittable))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "plan.json").write_text(
        json.dumps({"split_rule_version": SPLIT_RULE_VERSION,
                    "eu_fingerprint": euf.source_fingerprint(args.eu),
                    "parcels": [{k: v for k, v in p.items() if k != "masses"} |
                                {"masses": [{mk: mv for mk, mv in m.items()
                                             if mk not in ("geometry", "polygon")}
                                            for m in p.get("masses", [])]}
                                for p in plans]},
                   indent=1, ensure_ascii=False), encoding="utf-8")
    if args.plan_only or not splittable:
        return 0

    targets_path = args.out / "mass_targets.gpkg"
    targets = write_targets(prepared, splittable, targets_path)
    cadastre = gpd.read_file(args.cadastre)
    tasks = []
    for plan in splittable:
        context_path = args.out / "context" / f"{plan['refparcela']}.gpkg"
        write_context(cadastre, plan, targets, context_path)
        for index in range(1, len(plan["masses"]) + 1):
            tasks.append((_mass_reference(plan["refparcela"], index),
                          str(context_path), plan["refparcela"]))

    worker_config = {"out_dir": str(args.out / "runs"),
                     "targets": str(targets_path),
                     "zero_policy": "literal_zero",
                     "prune": not args.keep_runs,
                     "tmp_root": str(args.out / "tmp")}
    rows: list[dict] = []
    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker,
                             initargs=(worker_config,)) as pool:
        futures = {pool.submit(run_mass, task): task[0] for task in tasks}
        for done, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            rows.append(row)
            log.info("[%3d/%3d] %-24s %-7s %s kWh/m2  %.0fs", done, len(tasks),
                     row["refparcela"], row["status"],
                     row.get("total_site_kwh_m2", "-"), row["seconds"])
    (args.out / "masses.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    problems = _verify_storeys(rows, splittable)
    report = compare(rows, baseline, splittable)
    report["storey_consistency_problems"] = problems
    report["elapsed_minutes"] = round((time.time() - started) / 60, 1)
    report["baseline_ledger"] = str(args.ledger)
    report["eu_fingerprint"] = euf.source_fingerprint(args.eu)
    report["profile_fingerprint"] = vm.profile_record()["fingerprint"]
    (args.out / "comparison.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")

    log.info("")
    log.info("parcels compared      : %s", report["parcels_compared"])
    log.info("share of district     : %s %%", report["covered_share_of_district_pct"])
    log.info("split vs single prism : %+.2f %% energy on those parcels",
             report["delta_on_covered_pct"])
    log.info("  of which size       : x%.4f modelled floor area",
             report["modelled_area_ratio"])
    log.info("  of which shape      : %+.2f %% per modelled m2",
             report["intensity_delta_pct"])
    log.info("effect on district    : %+.3f %%", report["delta_on_district_pct"])
    if problems:
        log.warning("storey consistency problems: %s", problems)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
