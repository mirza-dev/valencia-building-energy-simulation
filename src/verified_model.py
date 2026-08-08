"""The frozen, verified building-energy model.

This module is the single entry point for running a building.  It does two
things and nothing else:

1. It **locks** every physical parameter that was verified against Rai's own
   EdiPluriP04 model on 2026-07-27 and refuses to run if any of them drifted.
2. It exposes ``simulate_verified_building()`` - call this instead of wiring the
   ``deep_building`` layers by hand, so every building in the stock is run with
   the same, provable configuration.

The verification itself is reproducible: ``run_rai_reference_replica()``
rebuilds Rai's own building with this engine and compares against the numbers
he published.  ``tests/test_verified_model.py`` keeps it honest.

What was verified (Rai's building, our engine):

    geometry (area, gross wall)            0.00 %
    envelope U (wall / roof / slab)       <=0.04 %
    internal gains (people/lights/equip)  <=0.63 %
    domestic hot water                     -0.06 %
    heating design load, zone by zone     <=0.9 %   (6032 vs 6032 on one zone)
    HVAC sensible heating delivered        -2.83 %
    HVAC sensible cooling removed          +5.19 %
    total site energy                      -1.50 %

What was NOT closed, and why (all three are recorded in
KNOWN_DIFFERENCES_FROM_RAI with the measurement behind them):

  * heating vs cooling *electricity* splits +22.8 % / -21.8 % while the zone
    loads themselves match.  The residual is equipment conversion, not the
    building: he ran EnergyPlus 9.5.0, we run 25.2.0, and the DX part-load
    curves and supplemental-heater logic moved across ~16 releases.  The two
    errors largely cancel in total site energy.
  * his roof reports solar reflectance 0.30 against our 0.07 -- the only
    surface property in the entire envelope that differs, wall and ground slab
    agree exactly.  That is where the +5.19 % delivered cooling comes from.
  * his window distribution favours the upper storeys; ours is a uniform WWR.
    Totals agree to -2.4 %.

History worth keeping: until 2026-07-28 this file's design days carried
OpenStudio's raw defaults -- 31 000 Pa (about 9 000 m of altitude), no wind, and
a July cooling day with the sky clearness left at 0.0, i.e. no sun at all.  That
undersized the cooling coils, which under-delivered cooling by roughly what our
dark roof over-demands, so the delivered-cooling gate passed at -0.52 % on two
cancelling errors.  Replacing them with the official ASHRAE pair fixed the
sizing (fans -11.3 % -> -3.9 %, unmet hours 388 -> 74) and exposed the roof.

Deliberate, documented differences from Rai in the PRODUCTION profile (they are
improvements, not drift):

    * real cadastre ``pob_total`` occupancy instead of the CTE 20 m2/person norm
    * persiana + balcony overhangs + neighbour shading (Rai models none)
    * building-specific WWR from the GIS footprint instead of a two-facade
      typological simplification
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path

import deep_building as db
import model_builder as mb
import model_config as mc


log = logging.getLogger("verified_model")

SCHEMA_VERSION = 1
VERIFIED_PROFILE_ID = "valencia-deep-v1"
VERIFIED_ON = "2026-07-27"
VERIFIED_AGAINST = "Rai EdiPluriP04 (informe_openstudio + eplustbl)"
# The climate the 13 gates were scored in.  A run in any other climate is still
# a valid simulation, but it is not the verified one, and every run record says
# which of the two it is.
VERIFIED_CLIMATE = "valencia_iwec"


# ---------------------------------------------------------------------------
# The locked parameter set
# ---------------------------------------------------------------------------
# Every value here was checked against Rai's model.  ``assert_profile_intact()``
# compares them with the live ``deep_building`` constants on every run, so an
# accidental edit surfaces immediately instead of silently changing results.
LOCKED_PARAMETERS: dict[str, object] = {
    # occupancy and the ventilation coupled to it
    "CTE_VENTILATION_M3S_PER_PERSON": 0.004,
    # domestic hot water
    "DHW_LITRES_PER_PERSON_DAY": 28.0,
    "DHW_BOILER_EFFICIENCY": 0.97,
    "DHW_LOOP_SETPOINT_C": 50.0,
    "DHW_LOOP_EXIT_TEMPERATURE_C": 82.0,
    "DHW_LOOP_DELTA_T_K": 11.0,
    "DHW_END_USE_SUBCATEGORY": "DHW",
    "WATER_MAINS_TEMPERATURE_C": 10.0,
    # heat pump
    "PTHP_HEATING_COP": 4.07,
    "PTHP_COOLING_COP": 5.0,
    "PTHP_GROUND_HEATING_COP": 3.0,
    "PTHP_FAN_EFFICIENCY": 0.90,
    "PTHP_GROUND_FAN_EFFICIENCY": 0.70,
    "PTHP_FAN_PRESSURE_RISE_PA": 250.0,
    "PTHP_FAN_MOTOR_EFFICIENCY": 0.90,
    # sizing and site
    "HEATING_SIZING_FACTOR": 1.25,
    "COOLING_SIZING_FACTOR": 1.15,
    # Not physics, but it decides whether a result is trusted at all, so it is
    # locked like one: a silent drop back to EnergyPlus's 25-day default would
    # start failing the heavy-masonry clusters again without saying why.
    "MAXIMUM_WARMUP_DAYS": 60,
    "GROUND_TEMPERATURE_C": 18.0,
    "SITE_BAROMETRIC_PRESSURE_PA": 100582.0,
    # template objects the layers bind to
    "RESIDENTIAL_SPACE_TYPE": "Espacio Tipo Vivienda CTE",
    "GROUND_TERCIARIO_SPACE_TYPE": "Espacio Tipo Terciario 8h Media CTE",
    "GROUND_SLAB_CONSTRUCTION": "Solera con aislante",
    "WINDOW_FRAME_NAME": "Carpinteria_Aluminio_simple",
}

# Modelling choices that are not single constants.
LOCKED_BEHAVIOUR: dict[str, object] = {
    # Since 2026-08-03 the ground regime is policy-resolved per building
    # (ground_use column: Tipo15 ground-dwelling evidence with a family
    # fallback).  "terciario" grounds keep exactly this verified regime; the
    # verification replica and the pilot both resolve to it, so the 13 gates
    # are unaffected.  "residential" grounds are a documented departure: the
    # bajo holds dwellings, so it is typed residential and enters the basis.
    "ground_floor_regime": ("policy_resolved: terciario -> conditioned Terciario "
                            "without thermostat (verified against Rai); "
                            "residential -> dwelling floor in the area basis"),
    "ground_floor_in_area_basis": "terciario: no (Rai basis); residential: yes (it is dwelling area)",
    "ground_floor_glazed": True,
    "occupancy_source": "cadastre.pob_total, area weighted over residential storeys",
    "occupancy_schedule": "Ocupacion Vivienda CTE (unchanged)",
    "metabolic_rate": "Actividad Ocupacion Vivienda 70 W/persona CTE (unchanged)",
    "mechanical_outdoor_air": "none - fresh air arrives through the infiltration objects",
    "design_days": ("official ASHRAE 2009 pair from ESP_Valencia.082840_IWEC.ddy, "
                    "the two Rai's Coil Sizing Summary names: Ann Htg 99.6 % DB "
                    "(1/21, ClearSky, clearness 0.0) and Ann Clg .4 % DB=>MWB "
                    "(8/21, ASHRAETau taub 0.505 / taud 1.864)"),
    "interzone_slabs": "template default (period appropriate); Rai's 2.119 available opt-in",
    # Which cluster envelope the per-building chain resolves against.  The
    # verification replica pins Rai's measured U-values directly and never
    # reaches this table, so the 13 gates are unaffected either way - but a
    # stock run's numbers move with it, so it is recorded here and the drift
    # guard fails if it changes without a re-verification.
    "cluster_envelope_source": mc.ENVELOPE_SOURCE,
    "shading": "persiana + balcony overhangs + neighbour context (Rai models none)",
    "area_basis": "residential floor area primary, total conditioned reported alongside",
}

# What Rai publishes for his own building, and the tolerance our replica has to
# stay inside.  These are the acceptance gates, not tunable knobs.
RAI_REFERENCE = {
    "residential_area_m2": 954.80,
    "gross_wall_area_m2": 465.00,
    "wall_u_with_film": 1.409,
    "roof_u_with_film": 2.495,
    "ground_slab_u_with_film": 0.501,
    "lighting_gj": 65.32,
    "equipment_gj": 56.56,
    "pumps_gj": 1.16,
    "dhw_gj": 36.08,
    "people_gain_gj": 73.07,
    "hvac_sensible_heating_gj": 27.30,
    "hvac_sensible_cooling_gj": -65.03,
    "total_site_kwh_m2": 54.50,
}

VERIFICATION_TOLERANCES = {
    "geometry_pct": 0.5,
    "envelope_u_pct": 1.0,
    "internal_gain_pct": 1.0,
    "hvac_sensible_load_pct": 5.0,
    # Delivered cooling gets its own, wider gate.  It is the one quantity the
    # roof reflectance difference lands on: his 0.30 against our 0.07 is worth
    # ~35 % of the top storey's cooling design load, and copying it alone was
    # measured to make agreement worse, not better (see the roof entry in
    # KNOWN_DIFFERENCES_FROM_RAI).  8 % keeps the gate meaningful -- the measured
    # swing from copying his reflectance was 11 % -- while not asking the replica
    # to match an envelope it deliberately does not share.  User decision
    # 2026-07-28.  Revisit when Rai answers on his construction layers.
    "hvac_cooling_delivered_pct": 8.0,
    "total_site_pct": 3.0,
}

# ---------------------------------------------------------------------------
# Where the verified replica and the production path deliberately differ
# ---------------------------------------------------------------------------
# The replica reproduces Rai's building; production runs real Valencia
# buildings.  Per-building VALUES differ by definition (footprint, cluster U,
# occupancy, DHW litres) - that is not a delta.  A delta is a LAYER or rule that
# is applied on one side and not the other.  Every one of them is listed here;
# ``tests/test_verified_model.py`` checks the list against the code.
REPLICA_VS_PRODUCTION_DELTAS = [
    {
        "item": "inter-storey slab",
        "replica": "Techo/Suelo Interior Referencia (EnergyPlus 2.24, matches Rai's 2.119)",
        "production": "template default Techo/Suelo sin aislante (EnergyPlus 1.674)",
        "why": ("Rai's 2.119 belongs to his EdiPluriP04 cluster; ours varies with "
                "the TABULA construction era and is period appropriate for the "
                "1974 stock. User decision 2026-07-27."),
        "applied_by": "deep_building.apply_rai_interzone_slabs",
        "in_production": False,
    },
    {
        "item": "shading",
        "replica": "none - matches Rai",
        "production": "persiana + balcony overhangs + neighbour context shading",
        "why": ("Rai models no shading at all (Shading Summary 0/0/0, Window "
                "Control None). We model more detail, not less; persiana is the "
                "top cooling driver in the LHS study."),
        "applied_by": "model_builder (context/balcony) + ShadingControl",
        "in_production": True,
    },
    {
        "item": "occupancy",
        "replica": "CTE norm, 20 m2/person - matches Rai",
        "production": "real cadastre pob_total, area weighted over residential storeys",
        "why": ("The CTE norm overstates Valencia by ~2.9x at city scale "
                "(2.22 M vs 775 k residents)."),
        "applied_by": "deep_building.apply_real_occupancy",
        "in_production": True,
    },
    {
        "item": "domestic hot water",
        "replica": "Rai's fixed 150 l/day per storey",
        "production": ("Rai-aligned 28 l/person/day at 50 C on the real head "
                       "count - ~80 % of the CTE DB-HE4 60 C energy reference, "
                       "not a CTE equivalence"),
        "why": ("Documented Spanish standard, follows the real occupancy, and "
                "lands within 5 % of Rai's per-m2 intensity."),
        "applied_by": "deep_building.add_dhw_loop",
        "in_production": True,
    },
    {
        "item": "window-wall ratio",
        "replica": "Rai's two-facade typological 13.42 / 26.14 %",
        "production": "building specific, from the GIS footprint and IVE typology",
        "why": "Rai simplifies to two facades; we place openings per real facade.",
        "applied_by": "model_builder.wwr_for_azimuth",
        "in_production": True,
    },
]

# Layers that MUST behave identically on both sides.  If one of these ever
# becomes a delta, the replica stops certifying the production engine.
# ---------------------------------------------------------------------------
# Known differences from Rai that we do NOT model away
# ---------------------------------------------------------------------------
# Distinct from REPLICA_VS_PRODUCTION_DELTAS: those are our own layers applied on
# one side and not the other.  These are places where the replica and Rai's model
# are genuinely not the same thing, measured and left standing on purpose.  They
# are what is left over after `--verify`, and they explain its residuals.
KNOWN_DIFFERENCES_FROM_RAI = [
    {
        "item": "EnergyPlus version",
        "rai": "9.5.0 (2024-05-23)",
        "ours": "25.2.0",
        "effect": ("DX coil part-load curves and the supplemental-heater logic "
                   "changed across ~16 releases. At matched zone loads our heat "
                   "pump draws +22.8 % heating and -21.8 % cooling electricity; "
                   "effective COP ours 3.11/4.55 against his 3.99/3.38 on a "
                   "nominal 4.07/5.0. The two largely cancel in total site energy."),
        "resolvable": False,
    },
    {
        "item": "roof solar absorptance",
        "rai": "0.70 (his Opaque Exterior row reports reflectance 0.30)",
        "ours": "0.93 (template material default, reflectance 0.07)",
        "effect": ("Wall and ground slab report 0.07 in BOTH models, so the roof "
                   "is the one surface property in the whole envelope that "
                   "differs. It is worth ~35 % of the top storey's cooling design "
                   "load, and it is why our delivered cooling sits +5.19 % above "
                   "his. Copying it alone was measured and made agreement WORSE: "
                   "heating -2.83 % -> +12.93 %, cooling +5.19 % -> -6.16 %. His "
                   "roof therefore also carries more thermal mass than ours at the "
                   "same U (2.495), and the two effects cancel in his model. "
                   "`deep_building.apply_rai_roof_absorptance` implements the "
                   "reflectance half and is deliberately not called: copying one "
                   "of a matched pair is less faithful than copying neither. "
                   "PRODUCTION STAYS AT 0.93 -- user decision 2026-07-28, taken "
                   "with the measurement above on the table."),
        "resolvable": ("Yes - Rai's pending answer on the material layer "
                       "composition of his cluster constructions."),
    },
    {
        "item": "window distribution",
        "rai": "91.96 m2 opening, 13.42 % north / 26.14 % south, 14.6 % on the ground storey",
        "ours": "89.78 m2 opening, 11.58 % north / 27.03 % south, 20.0 % on the ground storey",
        "effect": ("Totals agree to -2.4 %; we spread a uniform WWR over every "
                   "storey while he gives the ground storey proportionally less "
                   "glass. Second order against the roof."),
        "resolvable": False,
    },
]


SHARED_LAYERS = (
    "apply_real_occupancy",
    "apply_rai_ground_regime",
    "glaze_ground_floor",
    "apply_window_frames",
    "add_dhw_loop",
    "add_pthp_hvac",
)


# Locking the values alone is not enough: the code that consumes them can
# change underneath.  These are the SHA-256 hashes of the modules the
# verification actually exercised, recorded at the moment 13/13 gates passed.
# ``model_builder``/``run_simulation``/``model_config`` are the frozen domain
# scripts; ``deep_building`` is the layer chain the profile drives.
LOCKED_SOURCE_HASHES: dict[str, str] = {
    "model_builder.py": "729fc23aaa3558712ee7462cf5d7044a36fb23c27ac847a48da5780036abf100",
    "run_simulation.py": "0eec20827eeba66c78cac56c94780eee73f4393035d6d19527b0f6f38f779566",
    "model_config.py": "ab388f646b89d74834ff53af07454f559012082f48f50197bb4ddec30ddc91da",
    "deep_building.py": "a1f903a9006441f28a462b58bbd4dcbfd40d0e1f37c30bc63ee53791b18f8064",
}


class ProfileDrift(RuntimeError):
    """The live code no longer matches the verified profile."""


def _source_hashes() -> dict[str, str]:
    return {name: _source_sha256(name) for name in LOCKED_SOURCE_HASHES}


def assert_profile_intact(*, check_sources: bool = True) -> None:
    """Refuse to run if any verified parameter or source module drifted.

    ``check_sources=False`` is only for the unit tests that deliberately
    monkeypatch a constant to prove the value guard fires.
    """
    drifted = []
    for name, expected in LOCKED_PARAMETERS.items():
        actual = getattr(db, name, None)
        if actual != expected:
            drifted.append(f"parameter {name}: verified {expected!r}, live {actual!r}")

    if check_sources:
        for name, expected in LOCKED_SOURCE_HASHES.items():
            if expected == "PLACEHOLDER":
                drifted.append(
                    f"source {name}: no verified hash recorded - run "
                    "`python src/verified_model.py --relock` after a passing --verify")
                continue
            actual = _source_sha256(name)
            if actual != expected:
                drifted.append(
                    f"source {name}: verified {expected[:16]}..., live {actual[:16]}...")

    if drifted:
        raise ProfileDrift(
            f"{VERIFIED_PROFILE_ID} was verified on {VERIFIED_ON} against "
            f"{VERIFIED_AGAINST}, but the live code has drifted:\n  "
            + "\n  ".join(drifted)
            + "\nEither revert the change, or re-run "
              "`python src/verified_model.py --verify` and, once every gate "
              "passes, `--relock` to re-record the profile.")


def profile_fingerprint() -> str:
    """Stable SHA-256 over the locked profile AND the sources it was verified on.

    Including the source hashes is what makes the stamp meaningful: editing the
    logic in ``deep_building`` changes the fingerprint even when every locked
    value stays the same.
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "profile_id": VERIFIED_PROFILE_ID,
        "verified_on": VERIFIED_ON,
        "parameters": LOCKED_PARAMETERS,
        "behaviour": LOCKED_BEHAVIOUR,
        "source_hashes": LOCKED_SOURCE_HASHES,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def profile_record(climate=None, config=None) -> dict:
    """Everything a run needs to record about which model produced it.

    The climate is recorded separately from the profile fingerprint on purpose.
    Swapping the weather file is not profile drift - the locked physics is
    unchanged - but it does mean the run is no longer the one the 13 gates were
    scored on, and a reader has to be able to see that at a glance.
    """
    record = {
        "profile_id": VERIFIED_PROFILE_ID,
        "schema_version": SCHEMA_VERSION,
        "verified_on": VERIFIED_ON,
        "verified_against": VERIFIED_AGAINST,
        "fingerprint": profile_fingerprint(),
        "locked_source_hashes": dict(LOCKED_SOURCE_HASHES),
        "live_source_hashes": _source_hashes(),
        "climate": VERIFIED_CLIMATE,
        "climate_scenario": False,
        "verification_anchor": VERIFIED_CLIMATE,
    }
    if climate is not None:
        record["climate"] = climate.record()
        record["climate_scenario"] = climate.name != VERIFIED_CLIMATE
    if config is not None:
        record["build_config"] = {
            "template": str(getattr(config.data, "template_path", "")),
            "epw": str(getattr(config.data, "epw_path", "")),
        }
    return record


def _source_sha256(filename: str) -> str:
    path = Path(__file__).resolve().parent / filename
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# The one entry point for running a building
# ---------------------------------------------------------------------------
def simulate_verified_building(refparcela: str, out_dir: Path, *,
                               zero_policy: db.ZeroPolicy = "literal_zero",
                               gis_path: Path | None = None,
                               neighbors_path: Path | None = None,
                               climate=None, config=None) -> tuple[dict, bool]:
    """Run one building with the frozen, verified model.

    This is the call site for the whole stock: it guarantees that every
    building is simulated with the same configuration and stamps the profile
    fingerprint into the run record.

    A stock run passes `gis_path` (a prepared file whose invalid floor counts
    have been imputed) together with `neighbors_path` (the raw cadastre), so the
    target building becomes modellable without changing anybody's shading
    context.  Leaving both unset reproduces single-building behaviour exactly.

    `climate` and `config` swap the weather/design-day set and the
    template/geometry settings.  Passing a climate other than Valencia IWEC is
    allowed but is *not* covered by the verification: the 13 gates are scored
    against Rai's Valencia run, and he has no run in any other climate.  Such a
    run is therefore stamped `climate_scenario` and keeps a pointer back to the
    climate it was verified against, so the two can never be mistaken for each
    other in a ledger.
    """
    assert_profile_intact()
    record = profile_record(climate=climate, config=config)
    summary, qa_passed = db.simulate_deep_building(
        refparcela, Path(out_dir), zero_policy=zero_policy, gis_path=gis_path,
        neighbors_path=neighbors_path, provenance=record,
        climate=climate, config=config)
    run_dir = Path(out_dir) / f"{refparcela}_deep"
    if run_dir.exists():
        (run_dir / "verified_profile.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
    return summary, qa_passed


# ---------------------------------------------------------------------------
# The verification harness - Rai's own building, rebuilt with this engine
# ---------------------------------------------------------------------------
# Recovered from his surface areas: volume/area gives 3.00 m storeys, the
# 46.50 m2 facades give 15.50 m, the 46.20 m2 party walls give 15.40 m, and
# 15.50 x 15.40 = 238.70 m2 reproduces his floor area exactly.
RAI_REPLICA_GEOMETRY = {
    "facade_length_m": 15.50,
    "party_length_m": 15.40,
    "storey_height_m": 3.0,
    "residential_storeys": 4,
    "rotation_deg": 45,          # facades -> 135/315, party walls -> 45/225
}
# Rai's WWR is an OPENING ratio, exactly like ours: 31.19/232.50 = 13.42 % and
# 60.76/232.50 = 26.13 %, uniform over all five storeys.  The conversion from
# opening to glass is done by the shared frame mechanism in
# ``deep_building.build_deep_model``, so the replica passes his raw ratios.
RAI_REPLICA_ENVELOPE = {
    "wall_u_target": 1.37,       # -> EnergyPlus reports 1.409
    "roof_u_target": 2.48,       # -> EnergyPlus reports 2.496
    "thermal_bridge_du": 0.0,    # his construction is as-built
    "window_u": 5.7,
    "window_g": 0.85,
    "wwr_north": 0.1342,
    "wwr_south": 0.2614,
}
RAI_REPLICA_OPERATION = {
    "occupancy_m2_per_person": 20.0,     # his CTE norm, not the cadastre
    "dhw_litres_per_storey_day": 150.0,  # his fixed value, not CTE HE4
    "shading": False,                    # he models none
}


def build_rai_reference_replica():
    """Rebuild Rai's EdiPluriP04 building with this engine, for verification."""
    import geopandas as gpd
    from shapely.geometry import LineString, MultiLineString, Polygon
    from shapely import affinity

    g = RAI_REPLICA_GEOMETRY
    width, depth = g["facade_length_m"], g["party_length_m"]
    rect = affinity.rotate(Polygon([(0, 0), (width, 0), (width, depth), (0, depth)]),
                           g["rotation_deg"], origin=(0, 0))
    coords = list(rect.exterior.coords)[:-1]
    if Polygon(coords).exterior.is_ccw:
        coords = coords[::-1]                      # the builder wants clockwise
    footprint = Polygon(coords)
    points = list(footprint.exterior.coords)
    edges = [LineString([points[i], points[i + 1]]) for i in range(len(points) - 1)]
    party = MultiLineString([e for e in edges if abs(e.length - depth) < 0.05])

    row = gpd.GeoDataFrame([{
        "refparcela": "RAI_EdiPluriP04_replica",
        "altura_max": g["residential_storeys"],
        "geometry": footprint,
    }], crs="EPSG:25830").iloc[0]

    env, op = RAI_REPLICA_ENVELOPE, RAI_REPLICA_OPERATION
    config = mb.DEFAULT_BUILD_CONFIG.model_copy(deep=True)
    config.geometry.floor_height_m = g["storey_height_m"]
    config.geometry.ground_unconditioned = True
    config.envelope.wall_u = env["wall_u_target"]
    config.envelope.roof_u = env["roof_u_target"]
    config.envelope.thermal_bridge_du = env["thermal_bridge_du"]
    config.envelope.window_u = env["window_u"]
    config.envelope.window_g = env["window_g"]
    config.openings.wwr_north = config.openings.wwr_west = env["wwr_north"]
    config.openings.wwr_south = config.openings.wwr_east = env["wwr_south"]
    config.openings.balcony_doors_per_facade_floor = 0
    config.openings.balcony_depth_m = 0.01
    config.shading.context_enabled = False

    footprint_area = width * depth
    occupants = footprint_area * g["residential_storeys"] / op["occupancy_m2_per_person"]
    dhw_total = op["dhw_litres_per_storey_day"] * g["residential_storeys"]

    osm, stats = db.build_deep_model(
        row, party, occupants, config=config, neighbors=[],
        litres_per_person_day=dhw_total / occupants)
    # The only structural difference from the production path: Rai's own
    # inter-storey slab.  See REPLICA_VS_PRODUCTION_DELTAS -- in particular the
    # roof entry, which explains why his reflectance is deliberately NOT copied.
    db.apply_rai_interzone_slabs(osm)
    if not op["shading"]:
        for sub in osm.getSubSurfaces():
            sub.removeAllShadingControls()
        for control in list(osm.getShadingControls()):
            control.remove()
    stats["replica_occupants"] = occupants
    stats["replica_dhw_litres_per_day"] = dhw_total
    return osm, stats


def _eplustbl_tables(run_dir: Path) -> dict[str, list[list[str]]]:
    """Parse eplustbl.htm into {table title: grid}."""
    import html
    import re

    text = (Path(run_dir) / "eplustbl.htm").read_text(errors="replace")

    def plain(fragment: str) -> str:
        return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()

    tables: dict[str, list[list[str]]] = {}
    title = ""
    for part in re.split(r"(<b>.*?</b>)", text, flags=re.S):
        if re.match(r"^<b>", part):
            title = plain(part)
            continue
        match = re.search(r"<table.*?</table>", part, re.S | re.I)
        if not match or title in tables:
            continue
        tables[title] = [
            [re.sub(r"\s+", " ", plain(cell))
             for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
            for row in re.findall(r"<tr.*?</tr>", match.group(0), re.S | re.I)
        ]
    return tables


def _column(grid: list[list[str]], header_match: str) -> int | None:
    for index, header in enumerate(grid[0]):
        if header_match in header:
            return index
    return None


def read_verification_metrics(run_dir: Path) -> dict:
    """Pull the acceptance metrics straight out of the EnergyPlus report."""
    tables = _eplustbl_tables(run_dir)
    metrics: dict[str, float] = {}

    area = tables.get("Building Area", [])
    for row in area[1:]:
        if row and row[0].startswith("Total Building Area"):
            metrics["residential_area_m2"] = float(row[1])

    wwr = tables.get("Window-Wall Ratio", [])
    for row in wwr[1:]:
        if row and row[0].startswith("Gross Wall Area"):
            metrics["gross_wall_area_m2"] = float(row[1])

    opaque = tables.get("Opaque Exterior", [])
    if opaque:
        i_construction = _column(opaque, "Construction")
        i_u = _column(opaque, "U-Factor with Film")
        seen: dict[str, float] = {}
        for row in opaque[1:]:
            if i_construction is None or i_u is None or len(row) <= i_u:
                continue
            name = row[i_construction].upper()
            if name and name not in seen:
                seen[name] = float(row[i_u])
        for name, value in seen.items():
            if "MURO" in name or "WALL" in name:
                metrics["wall_u_with_film"] = value
            elif "CUBIERTA" in name or "ROOF" in name:
                metrics["roof_u_with_film"] = value
            elif "SOLERA" in name:
                metrics["ground_slab_u_with_film"] = value

    gains = tables.get("Annual Building Sensible Heat Gain Components", [])
    if gains:
        header = gains[0]
        row = next((r for r in gains if r and r[0] == "Total Facility"), None)
        if row:
            pairs = dict(zip(header[1:], row[1:]))
            wanted = {
                "people_gain_gj": "People Sensible Heat Addition",
                "hvac_sensible_heating_gj": "HVAC Zone Eq & Other Sensible Air Heating",
                "hvac_sensible_cooling_gj": "HVAC Zone Eq & Other Sensible Air Cooling",
            }
            for key, needle in wanted.items():
                for column, value in pairs.items():
                    if column.startswith(needle):
                        metrics[key] = float(value)
                        break
    return metrics


def score_verification(metrics: dict, results: dict) -> list[dict]:
    """Compare the replica against Rai's numbers, gate by gate."""
    gates = [
        ("residential_area_m2", metrics.get("residential_area_m2"), "geometry_pct"),
        ("gross_wall_area_m2", metrics.get("gross_wall_area_m2"), "geometry_pct"),
        ("wall_u_with_film", metrics.get("wall_u_with_film"), "envelope_u_pct"),
        ("roof_u_with_film", metrics.get("roof_u_with_film"), "envelope_u_pct"),
        ("ground_slab_u_with_film", metrics.get("ground_slab_u_with_film"), "envelope_u_pct"),
        ("people_gain_gj", metrics.get("people_gain_gj"), "internal_gain_pct"),
        ("lighting_gj", results.get("lighting_kwh_m2", 0) * 954.8 * 3.6 / 1000, "internal_gain_pct"),
        ("equipment_gj", results.get("equipment_kwh_m2", 0) * 954.8 * 3.6 / 1000, "internal_gain_pct"),
        ("dhw_gj", results.get("dhw_kwh_m2", 0) * 954.8 * 3.6 / 1000, "internal_gain_pct"),
        ("pumps_gj", results.get("pumps_kwh_m2", 0) * 954.8 * 3.6 / 1000, "internal_gain_pct"),
        ("hvac_sensible_heating_gj", metrics.get("hvac_sensible_heating_gj"), "hvac_sensible_load_pct"),
        ("hvac_sensible_cooling_gj", metrics.get("hvac_sensible_cooling_gj"), "hvac_cooling_delivered_pct"),
        ("total_site_kwh_m2", results.get("total_site_kwh_m2"), "total_site_pct"),
    ]
    scored = []
    for name, ours, tolerance_key in gates:
        reference = RAI_REFERENCE[name]
        tolerance = VERIFICATION_TOLERANCES[tolerance_key]
        if ours is None:
            scored.append({"gate": name, "ours": None, "rai": reference,
                           "tolerance_pct": tolerance, "delta_pct": None,
                           "passed": False, "reason": "metric not found in report"})
            continue
        delta = 100.0 * (ours - reference) / reference
        scored.append({"gate": name, "ours": round(ours, 3), "rai": reference,
                       "tolerance_pct": tolerance, "delta_pct": round(delta, 2),
                       "passed": abs(delta) <= tolerance})
    return scored


def run_rai_reference_replica(out_dir: Path) -> dict:
    """Build, simulate and score the replica against Rai's published numbers."""
    import shutil
    import run_simulation as sim

    osm, stats = build_rai_reference_replica()
    run_dir = Path(out_dir).resolve()
    # only ever clear a directory this function could have produced
    if run_dir.exists():
        if not (run_dir / "eplusout.err").exists() and any(run_dir.iterdir()):
            raise ValueError(
                f"{run_dir} is not empty and does not look like a previous "
                "replica run; refusing to delete it")
        shutil.rmtree(run_dir)
    sql_path = sim.run_energyplus(osm, run_dir)
    err_stats = db.scan_err_deep(run_dir)
    results = db.read_end_uses_split(sql_path, stats["res_area_m2"],
                                     stats["total_conditioned_area_m2"])
    metrics = read_verification_metrics(run_dir)
    gates = score_verification(metrics, results)
    return {
        "profile": profile_record(),
        "stats": {k: v for k, v in stats.items() if k not in ("facade_qa", "deep_layers")},
        "results": results,
        "metrics": metrics,
        "gates": gates,
        "verification_passed": all(gate["passed"] for gate in gates),
        "energyplus": err_stats,
        "rai_reference": RAI_REFERENCE,
        "tolerances": VERIFICATION_TOLERANCES,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
GOLDEN_PATH = (Path(__file__).resolve().parent.parent
               / "tests" / "golden" / "verified_model_rai_reference.json")


def _print_verification(report: dict) -> None:
    """The gate table, shared by --verify and --relock."""
    print(f"{'gate':30s} {'ours':>10s} {'Rai':>10s} {'delta':>8s} {'tol':>6s}")
    for gate in report["gates"]:
        delta = "n/a" if gate["delta_pct"] is None else f"{gate['delta_pct']:+.2f}%"
        ours = "n/a" if gate["ours"] is None else f"{gate['ours']:.3f}"
        print(f"  {gate['gate']:28s} {ours:>10s} {gate['rai']:>10.3f} "
              f"{delta:>8s} {gate['tolerance_pct']:>5.1f}%  "
              f"{'PASS' if gate['passed'] else 'FAIL'}")
    print(f"\nVERIFICATION: {'PASSED' if report['verification_passed'] else 'FAILED'}"
          f"   profile {report['profile']['fingerprint'][:16]}...")


def write_golden_fixture(report: dict, path: Path | None = None) -> Path:
    """Rewrite the golden fixture from a REAL verification report.

    Every number comes from the run that was just scored; nothing is typed by
    hand.  Descriptive fields the report does not carry (schema version, the
    runtime and replica descriptions, the regeneration note) are preserved from
    the existing file so this stays a refresh, not a rewrite from scratch.
    """
    path = Path(path or GOLDEN_PATH)
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    fixture = {
        "schema_version": previous.get("schema_version", SCHEMA_VERSION),
        "status": previous.get("status", "LOCKED_VERIFIED_MODEL"),
        "profile_id": VERIFIED_PROFILE_ID,
        "profile_fingerprint": report["profile"]["fingerprint"],
        "locked_source_hashes": dict(LOCKED_SOURCE_HASHES),
        "verified_on": VERIFIED_ON,
        "verified_against": VERIFIED_AGAINST,
        "runtime": previous.get("runtime", {}),
        "replica": previous.get("replica", {}),
        "replica_vs_production_deltas": REPLICA_VS_PRODUCTION_DELTAS,
        "known_differences_from_rai": KNOWN_DIFFERENCES_FROM_RAI,
        "shared_layers": list(SHARED_LAYERS),
        "expected_metrics": report["metrics"],
        "expected_results": report["results"],
        "rai_reference": dict(RAI_REFERENCE),
        "tolerances": dict(VERIFICATION_TOLERANCES),
        "gates": report["gates"],
        "verification_passed": report["verification_passed"],
        "note": previous.get(
            "note",
            "Regenerate: python src/verified_model.py --write-golden "
            "(after --verify passes; --relock first if the sources changed). "
            "A failing gate means the engine drifted away from Rai's model - "
            "investigate before editing this file."),
    }
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a building with the frozen, verified model")
    parser.add_argument("--refparcela")
    parser.add_argument("--out-dir", type=Path, default=Path("out/verified"))
    parser.add_argument("--zero-policy",
                        choices=["literal_zero", "cluster_median_impute"],
                        default="literal_zero")
    parser.add_argument("--show-profile", action="store_true",
                        help="print the frozen profile and its fingerprint")
    parser.add_argument("--verify", action="store_true",
                        help="rebuild Rai's building and score the engine against it")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--relock", action="store_true",
                        help="re-record LOCKED_SOURCE_HASHES from the current "
                             "sources (only after a passing --verify)")
    parser.add_argument("--write-golden", action="store_true",
                        help="rerun the verification and rewrite the golden "
                             "fixture from it (refuses to write a failing run)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.relock:
        # Re-locking used to write the new hashes on trust: the help text said
        # "only after a passing --verify" and nothing checked that it had
        # happened.  The verification is now part of the command, so a profile
        # can never be re-blessed without the evidence that earned it.
        print("Re-verifying against Rai before touching the lock...\n")
        assert_profile_intact(check_sources=False)
        report = run_rai_reference_replica(Path(args.out_dir) / "rai_reference_replica")
        _print_verification(report)
        if not report["verification_passed"]:
            print("\nREFUSED: verification did not pass - the lock is unchanged.")
            return 1

        live = _source_hashes()
        source = Path(__file__).resolve()
        text = source.read_text(encoding="utf-8")
        for name, digest in live.items():
            old = LOCKED_SOURCE_HASHES[name]
            text = text.replace(f'"{name}": "{old}"', f'"{name}": "{digest}"', 1)
        source.write_text(text, encoding="utf-8")
        print("\nLOCKED_SOURCE_HASHES re-recorded:")
        for name, digest in live.items():
            print(f"  {name:22s} {digest}")

        # The report's profile record was built during verification, i.e. before
        # the hashes above were rewritten. Bring the in-memory lock up to date
        # and restamp it, or the golden would pin the hashes this command just
        # replaced.
        LOCKED_SOURCE_HASHES.update(live)
        report["profile"] = profile_record()
        write_golden_fixture(report)
        print(f"Golden fixture rewritten from this same verification run "
              f"(profile {report['profile']['fingerprint'][:16]}...).")
        return 0

    if args.write_golden:
        assert_profile_intact()
        report = run_rai_reference_replica(Path(args.out_dir) / "rai_reference_replica")
        if not report["verification_passed"]:
            failed = [g["gate"] for g in report["gates"] if not g["passed"]]
            print(f"REFUSED: verification failed on {failed} - fix the engine "
                  "before recording a golden.")
            return 1
        path = write_golden_fixture(report)
        print(f"golden rewritten: {path}")
        print(f"  fingerprint : {report['profile']['fingerprint']}")
        print(f"  gates       : {sum(g['passed'] for g in report['gates'])}"
              f"/{len(report['gates'])}")
        return 0

    if args.show_profile:
        assert_profile_intact()
        print(json.dumps({"profile": profile_record(),
                          "parameters": LOCKED_PARAMETERS,
                          "behaviour": LOCKED_BEHAVIOUR}, indent=2, ensure_ascii=False))
        return 0

    if args.verify:
        # verification is what ESTABLISHES the source lock, so it only checks
        # the parameter values here; --relock records the hashes afterwards
        assert_profile_intact(check_sources=False)
        report = run_rai_reference_replica(Path(args.out_dir) / "rai_reference_replica")
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            _print_verification(report)
        return 0 if report["verification_passed"] else 1

    if not args.refparcela:
        parser.error("--refparcela is required unless --show-profile or --verify")
    _summary, qa_passed = simulate_verified_building(
        args.refparcela, Path(args.out_dir), zero_policy=args.zero_policy)
    log.info("QA: %s", "ALL PASSED" if qa_passed else "FAILED")
    return 0 if qa_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
