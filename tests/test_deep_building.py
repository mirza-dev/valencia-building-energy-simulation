"""Deep single-building pipeline: layer contracts and frozen-module guards."""

from __future__ import annotations

import math

import pytest

import deep_building as db
import model_builder as mb
import run_simulation as sim


PILOT = "4252702YJ2745A"
PILOT_OCCUPANTS = 62.0          # cadastre pob_total
PILOT_RES_AREA = 2809.94        # 5 residential floors x 561.99 m2


# ---------------------------------------------------------------------------
# Pure logic - no OpenStudio model needed
# ---------------------------------------------------------------------------
def test_resolve_occupants_uses_the_cadastre_head_count():
    people, source = db.resolve_occupants(62, 35, None, "literal_zero")
    assert people == 62.0
    assert source == "cadastre.pob_total"


def test_zero_population_literal_policy_keeps_the_building_empty():
    people, source = db.resolve_occupants(0, 4, 1.8, "literal_zero")
    assert people == 0.0
    assert source == "cadastre.pob_total(zero)"


def test_zero_population_impute_policy_uses_cluster_median_per_dwelling():
    people, source = db.resolve_occupants(0, 4, 1.8, "cluster_median_impute")
    assert people == pytest.approx(7.2)
    assert source == "imputed.cluster_median_per_dwelling"


def test_impute_policy_refuses_to_guess_without_a_cluster_median():
    with pytest.raises(ValueError):
        db.resolve_occupants(0, 4, None, "cluster_median_impute")


def test_rai_design_days_are_separate_from_the_frozen_builder_values():
    # the frozen builder keeps its own design days; the deep path must not
    # silently change Part C/D/F behaviour by editing them
    assert db.RAI_DESIGN_DAYS["heating"]["db"] == 1.0
    assert db.RAI_DESIGN_DAYS["cooling"]["db"] == 33.1
    assert mb.DESIGN_DAYS["heating"]["db"] == 2.6
    assert mb.DESIGN_DAYS["cooling"]["db"] == 32.4


def test_frozen_hvac_constants_untouched():
    assert mb.HVAC_HEATING_EFFICIENCY == 0.85
    assert mb.HVAC_COOLING_COP == 2.5
    assert db.PTHP_HEATING_COP == 4.07
    assert db.PTHP_COOLING_COP == 5.0


# ---------------------------------------------------------------------------
# Error scanning
# ---------------------------------------------------------------------------
def _write_err(tmp_path, body: str):
    (tmp_path / "eplusout.err").write_text(body, encoding="utf-8")
    return tmp_path


def test_scan_err_deep_matches_the_pattern_energyplus_actually_writes(tmp_path):
    # both scanners must see the label EnergyPlus really writes; the frozen
    # helper used to look for "**  Severe  **" (two spaces) and counted zero.
    body = ("   ** Severe  ** Missing shade or blind layer in window construction\n"
            "   **   ~~~   ** ...EMS Actuator cannot be set\n"
            "   ************* EnergyPlus Completed Successfully-- 17 Warning; 1 Severe Errors;\n")
    stats = db.scan_err_deep(_write_err(tmp_path, body))
    assert stats["severes"] == 1
    assert stats["severes_benign_shading_ems"] == 1
    assert stats["severes_unexplained"] == 0


def test_scan_err_deep_raises_on_an_unexplained_severe(tmp_path):
    body = ("   ** Severe  ** Something genuinely wrong happened\n"
            "   ************* EnergyPlus Completed Successfully-- 3 Warning; 1 Severe Errors;\n")
    with pytest.raises(RuntimeError, match="unexplained Severe"):
        db.scan_err_deep(_write_err(tmp_path, body))


def test_scan_err_deep_raises_on_fatal(tmp_path):
    body = ("   ** Fatal  ** EnergyPlus gave up\n"
            "   ************* EnergyPlus Completed Unsuccessfully-- 1 Warning; 0 Severe Errors;\n")
    with pytest.raises(RuntimeError):
        db.scan_err_deep(_write_err(tmp_path, body))


# ---------------------------------------------------------------------------
# Layer contracts - one real pilot model, reused
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def pilot_model():
    row = db.load_building_row(PILOT)
    geometry = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(geometry, PILOT, mb.NEIGHBORS_SHP)
    party = mb.find_party_walls(geometry, PILOT, mb.NEIGHBORS_SHP, neighbors=neighbors)
    osm, stats = db.build_deep_model(row, party, PILOT_OCCUPANTS, neighbors=neighbors)
    return osm, stats


@pytest.mark.integration
def test_real_occupancy_replaces_the_cte_norm(pilot_model):
    osm, stats = pilot_model
    occupancy = stats["deep_layers"]["occupancy"]

    assert occupancy["occupants"] == PILOT_OCCUPANTS
    assert occupancy["people_per_m2"] == pytest.approx(62.0 / PILOT_RES_AREA, rel=1e-4)
    assert occupancy["m2_per_person"] == pytest.approx(45.32, abs=0.05)
    # the CTE norm would have put 2.26x more people in the same floor area
    assert occupancy["cte_norm_people_per_m2"] == pytest.approx(0.05, rel=1e-6)


@pytest.mark.integration
def test_occupancy_schedule_and_metabolic_rate_are_untouched(pilot_model):
    osm, _ = pilot_model
    space_type = db._residential_space_type(osm)
    people = list(space_type.people())
    assert people, "residential space type lost its People object"
    for instance in people:
        schedule = instance.numberofPeopleSchedule()
        assert schedule.is_initialized()
        assert schedule.get().nameString() == "Ocupacion Vivienda CTE"


@pytest.mark.integration
def test_occupancy_driven_ventilation_is_rescaled_but_the_others_are_not(pilot_model):
    osm, stats = pilot_model
    space_type = db._residential_space_type(osm)
    # the contract is the coupling itself: ventilation = 0.004 m3/s per person,
    # expressed on the same density the People object received
    density = stats["deep_layers"]["occupancy"]["people_per_m2"]
    expected = db.CTE_VENTILATION_M3S_PER_PERSON * density

    by_name = {inf.nameString(): inf for inf in space_type.spaceInfiltrationDesignFlowRates()}
    occupancy_objects = [i for name, i in by_name.items() if "ocupacion" in name.lower()]
    assert occupancy_objects
    for obj in occupancy_objects:
        # rel tolerance absorbs the reporting rounding of people_per_m2
        assert obj.flowperSpaceFloorArea().get() == pytest.approx(expected, rel=1e-4)
        # and it really is 2.26x below the CTE norm value the template shipped
        assert obj.flowperSpaceFloorArea().get() < 0.0002

    # the constant airtightness and the summer-night ventilation keep the
    # template values - they are not occupancy driven
    constant = [i for name, i in by_name.items() if "constante" in name.lower()]
    nocturnal = [i for name, i in by_name.items() if "nocturna" in name.lower()]
    assert constant and constant[0].airChangesperHour().get() == pytest.approx(0.2)
    assert nocturnal and nocturnal[0].airChangesperHour().get() == pytest.approx(4.0)


@pytest.mark.integration
def test_rai_ground_regime_conditions_the_ground_but_keeps_it_out_of_the_area(pilot_model):
    osm, stats = pilot_model
    ground = stats["deep_layers"]["ground"]
    assert ground["ground_spaces_conditioned"] == 1
    assert ground["in_floor_area_basis"] is False

    terciario = db._by_name(osm.getSpaceTypes(), db.GROUND_TERCIARIO_SPACE_TYPE)
    spaces = list(terciario.spaces())
    assert len(spaces) == 1
    assert spaces[0].partofTotalFloorArea() is False
    assert spaces[0].thermalZone().is_initialized()

    # residential area basis is unchanged, the conditioned basis grows by one floor
    assert stats["res_area_m2"] == pytest.approx(PILOT_RES_AREA, abs=0.1)
    assert stats["total_conditioned_area_m2"] == pytest.approx(562.0 * 6, abs=0.5)


@pytest.mark.integration
def test_dhw_is_rai_aligned_28l_at_50c_and_metered_separately(pilot_model):
    """28 l/person/day delivered at 50 C - Rai-aligned, ~80 % of the CTE 60 C
    energy reference, deliberately NOT a CTE equivalence (see the constant)."""
    osm, stats = pilot_model
    dhw = stats["deep_layers"]["dhw"]

    assert dhw["dhw_litres_per_person_day"] == 28.0
    assert dhw["dhw_litres_per_day"] == pytest.approx(28.0 * PILOT_OCCUPANTS)
    assert dhw["dhw_equipment_count"] == 5          # one per residential floor
    assert dhw["dhw_boiler_efficiency"] == 0.97

    boilers = osm.getBoilerHotWaters()
    assert len(boilers) == 1
    assert boilers[0].nominalThermalEfficiency() == pytest.approx(0.97)
    # without the subcategory the boiler gas hides inside the space-heating row
    assert boilers[0].endUseSubcategory() == db.DHW_END_USE_SUBCATEGORY
    assert len(osm.getWaterUseEquipments()) == 5
    assert len(osm.getPlantLoops()) == 1


@pytest.mark.integration
def test_pthp_replaces_ideal_loads_on_every_zone(pilot_model):
    osm, stats = pilot_model
    hvac = stats["deep_layers"]["hvac"]

    assert hvac["hvac_zones"] == 6                  # 5 residential + conditioned ground
    assert hvac["residential_zones"] == 5
    assert not osm.getZoneHVACIdealLoadsAirSystems()
    assert len(osm.getZoneHVACPackagedTerminalHeatPumps()) == 6
    assert not osm.getZoneHVACPackagedTerminalAirConditioners()

    heating_cops = sorted(c.ratedCOP() for c in osm.getCoilHeatingDXSingleSpeeds())
    assert heating_cops == pytest.approx([3.0] + [4.07] * 5)
    for coil in osm.getCoilCoolingDXSingleSpeeds():
        assert coil.ratedCOP() == pytest.approx(5.0)

    sizing = osm.getSizingParameters()
    assert sizing.heatingSizingFactor() == pytest.approx(1.25)
    assert sizing.coolingSizingFactor() == pytest.approx(1.15)
    ground = osm.getSiteGroundTemperatureBuildingSurface()
    assert ground.januaryGroundTemperature() == pytest.approx(18.0)
    assert ground.julyGroundTemperature() == pytest.approx(18.0)


@pytest.mark.integration
def test_persiana_and_context_shading_survive_the_deep_layers(pilot_model):
    osm, stats = pilot_model
    # the deep pipeline must not silently drop the shading that drives cooling
    assert stats["n_shading_surfaces"] == 69
    assert osm.getShadingControls()
    assert stats["n_balcony_doors"] == 30
    ground = stats["deep_layers"]["ground_glazing"]
    assert ground["ground_facades_glazed"] > 0


@pytest.mark.integration
def test_every_glazed_opening_is_framed(pilot_model):
    osm, stats = pilot_model
    frames = stats["deep_layers"]["window_frames"]

    glazed = [s for s in osm.getSubSurfaces()
              if s.subSurfaceType() in db.GLAZED_SUBSURFACE_TYPES]
    framed = [s for s in glazed if not s.windowPropertyFrameAndDivider().isNull()]
    # balcony doors carry the same carpentry as the windows
    assert len(framed) == len(glazed) == frames["windows_with_frame"]
    assert frames["window_frame"] == db.WINDOW_FRAME_NAME
    assert frames["frame_width_m"] == pytest.approx(db.WINDOW_FRAME_WIDTH_M)

    # EnergyPlus reports the opening; the glass sits inside it
    assert frames["opening_area_m2"] > frames["glass_area_m2"]
    assert stats["window_area_m2"] == frames["opening_area_m2"]
    assert stats["window_glass_area_m2"] == frames["glass_area_m2"]
    # and the WWR handed to the frozen builder was shrunk to compensate
    assert 0.75 < frames["wwr_rescaled_by"] < 0.85


@pytest.mark.integration
def test_ground_zone_has_no_thermostat_like_rai(pilot_model):
    osm, stats = pilot_model
    # Rai's own sizing table reports 0.000 W design load on the ground zone:
    # the terminal unit is attached but nothing ever calls for it.
    assert stats["deep_layers"]["ground"]["ground_thermostat"] is False
    terciario = db._by_name(osm.getSpaceTypes(), db.GROUND_TERCIARIO_SPACE_TYPE)
    zone = list(terciario.spaces())[0].thermalZone().get()
    assert zone.thermostatSetpointDualSetpoint().isNull()


# ---------------------------------------------------------------------------
# The frozen scanner used to miss every Severe - guard against a relapse
# ---------------------------------------------------------------------------
def test_frozen_scanner_counts_the_label_energyplus_actually_writes(tmp_path):
    body = ("   ** Severe  ** Missing shade or blind layer in window construction\n"
            "   ************* EnergyPlus Completed Successfully-- 5 Warning; 1 Severe Errors;\n")
    (tmp_path / "eplusout.err").write_text(body, encoding="utf-8")
    with pytest.raises(RuntimeError, match="1 Severe"):
        sim.scan_err_file(tmp_path)


def test_frozen_scanner_is_insensitive_to_the_label_spacing(tmp_path):
    # a future E+ release may pad differently; the guard must not go quiet again
    for label in ("** Severe  **", "**  Severe  **", "**Severe**"):
        (tmp_path / "eplusout.err").write_text(
            f"   {label} something\n   ***** Completed Successfully-- 1 Warning; 1 Severe\n",
            encoding="utf-8")
        with pytest.raises(RuntimeError):
            sim.scan_err_file(tmp_path)


def test_frozen_scanner_still_passes_a_clean_run(tmp_path):
    body = ("   ** Warning ** something harmless\n"
            "   ************* EnergyPlus Completed Successfully-- 11 Warning; 0 Severe Errors;\n")
    (tmp_path / "eplusout.err").write_text(body, encoding="utf-8")
    assert sim.scan_err_file(tmp_path) == {"warnings": 11, "severes": 0}


# ---------------------------------------------------------------------------
# Occupancy plausibility: advisory flag, never a blocker
# ---------------------------------------------------------------------------
def test_plausible_density_is_reported_as_such():
    verdict = db.occupancy_plausibility(62.0, 2809.94, 35)
    assert verdict["status"] == "plausible"
    assert verdict["m2_per_person"] == pytest.approx(45.32, abs=0.05)
    assert verdict["people_per_dwelling"] == pytest.approx(1.77, abs=0.01)


def test_overcrowded_cadastre_record_is_flagged():
    # 9525403YJ2792F: 26 residents, 2 dwellings, altura_max=1 over 148 m2
    verdict = db.occupancy_plausibility(26.0, 148.2, 2)
    assert verdict["status"] == "implausible_dense"
    assert verdict["m2_per_person"] < db.OCCUPANCY_DENSITY_MIN_M2
    assert "altura_max" in verdict["note"]


def test_nearly_empty_building_is_flagged_as_sparse():
    verdict = db.occupancy_plausibility(1.0, 400.0, 1)
    assert verdict["status"] == "implausible_sparse"


def test_zero_population_is_its_own_status_not_a_failure():
    verdict = db.occupancy_plausibility(0.0, 300.0, 2)
    assert verdict["status"] == "unoccupied"
    assert verdict["m2_per_person"] is None


@pytest.mark.integration
def test_pilot_records_a_plausible_occupancy_verdict(pilot_model):
    _osm, stats = pilot_model
    assert stats["deep_layers"]["occupancy"]["plausibility"]["status"] == "plausible"


# ---------------------------------------------------------------------------
# Padron density cap (option A): habitability floor, fully recorded
# ---------------------------------------------------------------------------
def test_density_cap_leaves_a_plausible_head_count_alone():
    assert db.cap_occupants_to_density_floor(62.0, 2809.94) == 62.0


def test_density_cap_limits_an_overcrowded_padron_record():
    # 9525403YJ2792F: 26 registered residents over 148.2 m2
    capped = db.cap_occupants_to_density_floor(26.0, 148.2)
    assert capped == pytest.approx(148.2 / db.OCCUPANCY_DENSITY_MIN_M2)
    assert capped == pytest.approx(9.88, abs=0.01)
    assert 148.2 / capped == pytest.approx(db.OCCUPANCY_DENSITY_MIN_M2)


def test_density_cap_never_invents_occupants():
    assert db.cap_occupants_to_density_floor(0.0, 300.0) == 0.0
    assert db.cap_occupants_to_density_floor(2.0, 400.0) == 2.0


def test_capped_verdict_keeps_the_original_padron_value_visible():
    verdict = db.occupancy_plausibility(9.88, 148.2, 2, capped_from=26.0)
    assert verdict["status"] == "implausible_dense_capped"
    assert verdict["padron_occupants"] == 26.0
    assert verdict["capped_to"] == 9.9
    assert "habitability floor" in verdict["note"]


def test_uncapped_dense_record_is_still_flagged_plainly():
    verdict = db.occupancy_plausibility(26.0, 148.2, 2)
    assert verdict["status"] == "implausible_dense"


# ---------------------------------------------------------------------------
# Single-zone caveat
#
# The chain gives every storey one well-mixed zone.  Raising the footprint
# ceiling from 5 000 to 20 000 m2 on 2026-08-03 admitted 119 Valencia buildings
# carrying 11.00 % of the city's floor area - the right call, since dropping
# them was the larger error, but the weaker assumption they run under has to be
# recorded rather than blended into a total.  On 2026-08-22 the ceiling was
# lifted again, to 1 000 000 m2, so it now excludes nothing at all; the flag
# below is the ONLY thing carrying that caveat, which makes it more load-bearing
# than before, not less.
# ---------------------------------------------------------------------------
def test_single_zone_threshold_is_the_ceiling_that_used_to_exclude():
    """The threshold is deliberately the OLD ceiling, not a fresh guess."""
    assert db.LARGE_FOOTPRINT_SINGLE_ZONE_M2 == 5000.0
    # and it is a flag, not a gate: the config now admits three orders of
    # magnitude beyond it, so nothing is dropped for being large - it is only
    # ever marked.
    assert mb.DEFAULT_BUILD_CONFIG.geometry.footprint_max_m2 == 1000000.0
    assert db.LARGE_FOOTPRINT_SINGLE_ZONE_M2 < \
        mb.DEFAULT_BUILD_CONFIG.geometry.footprint_max_m2


@pytest.mark.integration
def test_pilot_records_its_zoning_and_is_not_flagged(pilot_model):
    """562 m2 over 5 storeys is a normal block - flagged False, but stated."""
    _, stats = pilot_model
    assert stats["large_footprint_single_zone"] is False

    zoning = stats["deep_layers"]["zoning"]
    assert zoning["scheme"] == "one_well_mixed_zone_per_storey"
    assert zoning["single_zone_threshold_m2"] == db.LARGE_FOOTPRINT_SINGLE_ZONE_M2
    assert zoning["footprint_m2"] == pytest.approx(stats["footprint_m2"], abs=0.1)
    assert zoning["footprint_m2"] < db.LARGE_FOOTPRINT_SINGLE_ZONE_M2
    assert zoning["large_footprint_single_zone"] is False


def test_scan_err_deep_needs_both_markers_in_the_same_block(tmp_path):
    """An unrelated Severe carrying only ONE of the two markers is not excused.

    EnergyPlus emits "EMS Actuator cannot be set" for other actuators too; with
    `any` those rode through as benign (review finding, 2026-08-03).  The
    documented PTHP+persiana message always carries both markers in one block.
    """
    body = ("   ** Severe  ** Some other problem entirely\n"
            "   **   ~~~   ** ...'Availability' EMS Actuator cannot be set here\n"
            "   ************* EnergyPlus Completed Successfully-- 0 Warning; 1 Severe Errors;\n")
    with pytest.raises(RuntimeError, match="unexplained Severe"):
        db.scan_err_deep(_write_err(tmp_path, body))

    body = ("   ** Severe  ** Missing shade or blind layer in window construction\n"
            "   ************* EnergyPlus Completed Successfully-- 0 Warning; 1 Severe Errors;\n")
    with pytest.raises(RuntimeError, match="unexplained Severe"):
        db.scan_err_deep(_write_err(tmp_path, body))


# ---------------------------------------------------------------------------
# Ground use (review finding ③, 2026-08-03: policy now reaches the engine)
# ---------------------------------------------------------------------------
def test_unknown_ground_use_is_refused():
    with pytest.raises(ValueError, match="unknown ground_use"):
        row = db.load_building_row(PILOT)
        db.build_deep_model(row, None, 10.0, ground_use="atrium")


@pytest.mark.integration
def test_pilot_defaults_to_the_terciario_ground(pilot_model):
    """The raw cadastre has no ground_use column -> historical Rai regime."""
    _, stats = pilot_model
    assert stats["ground_use"] == "terciario"
    assert stats["res_area_m2"] == pytest.approx(2809.9, abs=0.1)
    ground = stats["deep_layers"]["ground"]
    assert ground["in_floor_area_basis"] is False


@pytest.fixture(scope="module")
def residential_ground_model():
    """A real Benicalap block with a Tipo15 ground dwelling (B0 planta)."""
    ref = "4149124YJ2745A"
    row = db.load_building_row(ref).copy()
    row["ground_use"] = "residential"
    row["ground_use_source"] = "tipo15"
    geometry = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(geometry, ref, mb.NEIGHBORS_SHP)
    party = mb.find_party_walls(geometry, ref, mb.NEIGHBORS_SHP, neighbors=neighbors)
    return db.build_deep_model(row, party, 20.0, neighbors=neighbors)


@pytest.mark.integration
def test_residential_ground_is_a_dwelling_floor_in_the_basis(residential_ground_model):
    osm, stats = residential_ground_model
    assert stats["ground_use"] == "residential"
    # the bajo enters the area basis: fp x (n_res + 1), not fp x n_res
    expected = round(stats["footprint_m2"] * (stats["n_floors_residential"] + 1), 1)
    assert stats["res_area_m2"] == pytest.approx(expected, abs=0.1)

    spaces = sorted(osm.getSpaces(),
                    key=lambda s: min(v.z() for srf in s.surfaces() for v in srf.vertices()))
    ground = spaces[0]
    assert ground.spaceType().get().nameString() == db.RESIDENTIAL_SPACE_TYPE
    assert ground.partofTotalFloorArea() is True
    zone = ground.thermalZone().get()
    assert not zone.thermostatSetpointDualSetpoint().isNull()


@pytest.mark.integration
def test_residential_ground_gets_the_residential_pthp(residential_ground_model):
    """Space-type-driven COP split: the dwelling bajo gets 4.07, not 3.0."""
    osm, _ = residential_ground_model
    spaces = sorted(osm.getSpaces(),
                    key=lambda s: min(v.z() for srf in s.surfaces() for v in srf.vertices()))
    ground_zone = spaces[0].thermalZone().get().nameString()
    for pthp in osm.getZoneHVACPackagedTerminalHeatPumps():
        zone = pthp.thermalZone()
        if zone.is_initialized() and zone.get().nameString() == ground_zone:
            coil = pthp.heatingCoil().to_CoilHeatingDXSingleSpeed().get()
            assert coil.ratedCOP() == pytest.approx(db.PTHP_HEATING_COP)
            return
    raise AssertionError("no PTHP found on the ground zone")


@pytest.mark.integration
def test_residential_ground_is_still_glazed(residential_ground_model):
    _, stats = residential_ground_model
    glazing = stats["deep_layers"]["ground_glazing"]
    assert glazing["ground_windows"] > 0
    assert glazing["ground_glass_area_m2"] > 0


# ---------------------------------------------------------------------------
# Mixed-use storeys: altura_max says where the top dwelling is, not that every
# storey below it is housing (measured 2026-08-04, Benicalap v5).
# ---------------------------------------------------------------------------
def test_cadastral_storeys_leave_a_fully_residential_building_alone():
    # Pilot: 2910 m2 of dwelling over a 562 m2 plate = 5.18 -> ceil 6, capped at
    # the 5 storeys actually built.  The frozen deep baseline must not move.
    assert db.residential_storeys_from_cadastre(2910.0, 562.0, 5) == 5


def test_cadastral_storeys_convert_a_mixed_use_block():
    # 4648906YJ2744H: 324 m2 of dwelling on a 190.5 m2 plate over 4 built
    # residential storeys -> only 2 can be housing.
    assert db.residential_storeys_from_cadastre(324.0, 190.5, 4) == 2


def test_cadastral_storeys_tolerate_the_gross_to_net_gap():
    # A purely residential block still shows ~11 % more gross plate area than
    # the net `sfc` recorded inside it.  ceil() must not strip a storey for that.
    assert db.residential_storeys_from_cadastre(1734.0, 385.0, 5) == 5


@pytest.mark.parametrize("cadastral", [None, 0, -1, "", "abc"])
def test_cadastral_storeys_never_shrink_without_evidence(cadastral):
    # A failed Tipo15 join must leave the building as built, never silently
    # smaller.  This is the single-building CLI path.
    assert db.residential_storeys_from_cadastre(cadastral, 562.0, 5) == 5


def test_cadastral_storeys_never_exceed_what_was_built():
    assert db.residential_storeys_from_cadastre(99999.0, 562.0, 5) == 5


def test_cadastral_storeys_keep_at_least_one_dwelling_storey():
    assert db.residential_storeys_from_cadastre(10.0, 562.0, 5) == 1


def test_storey_rule_is_stable_across_the_integer_boundary():
    """`2255712YJ2725C`: 441.0 m2 of dwellings on a 145.7 m2 plate.

    A 1 % move in the footprint used to move the answer by a whole storey,
    because `ceil` is discontinuous exactly where the two measurements stop
    being able to tell the difference.  Both sides of the boundary must now
    give the same answer - that is the property, not either number on its own.
    """
    assert db.residential_storeys_from_cadastre(441.0, 145.7, 10) == 3   # ratio 3.0268
    assert db.residential_storeys_from_cadastre(441.0, 147.2, 10) == 3   # ratio 2.9959


def test_a_genuine_partial_storey_is_still_rounded_up():
    # the snap is not a change of rule: outside the measurement band `ceil`
    # still applies, so a half-full top storey is still built and then loaded
    # for the part the record accounts for
    assert db.residential_storeys_from_cadastre(525.0, 150.0, 10) == 4    # ratio 3.5
    assert db.residential_storeys_from_cadastre(457.5, 150.0, 10) == 4    # ratio 3.05


def test_the_storey_band_scales_with_the_storey_count():
    # the error in a ratio is relative, so a 10-storey building legitimately
    # gets five times the absolute slack of a 2-storey one
    # values are chosen clearly inside and clearly outside the band: asserting
    # exactly on it tests floating-point representation, not the rule (2.02 - 2
    # is 0.020000000000000018, which is not <= 0.02)
    assert db.residential_storeys_from_cadastre(201.5, 100.0, 20) == 2    # 2.015, in
    assert db.residential_storeys_from_cadastre(203.0, 100.0, 20) == 3    # 2.03, out
    assert db.residential_storeys_from_cadastre(1009.0, 100.0, 20) == 10  # 10.09, in
    assert db.residential_storeys_from_cadastre(1012.0, 100.0, 20) == 11  # 10.12, out


def test_the_snap_can_only_remove_a_storey_never_add_one():
    # a rule that could add a storey would be inventing dwelling area; sweep the
    # whole ratio range rather than trusting the two worked examples above
    for hundredths in range(1, 2000):
        ratio = hundredths / 100.0
        answer = db.residential_storeys_from_cadastre(ratio * 200.0, 200.0, 100)
        assert answer <= math.ceil(ratio)
        assert answer >= math.ceil(ratio) - 1


def test_the_snap_flag_measures_the_outcome_not_the_branch():
    """2,077 Valencia buildings sit inside the band; 473 change because of it.

    A building already limited by what was built cannot lose a storey it never
    had, and a ratio just below a whole storey lands on the same answer either
    way.  Reporting either as a snap would overstate the effect more than
    fourfold.
    """
    # inside the band AND above the integer, with room to move: a real snap
    assert db.storey_rule_snapped(441.0, 145.7, 10) is True
    # the same ratio, but only three storeys were ever built: the cap already
    # gave the answer, so the band changed nothing
    assert db.storey_rule_snapped(441.0, 145.7, 3) is False
    # inside the band but BELOW the integer: `ceil` and the band agree
    assert db.storey_rule_snapped(441.0, 147.2, 10) is False
    # outside the band entirely
    assert db.storey_rule_snapped(525.0, 150.0, 10) is False
    # no cadastral evidence at all
    assert db.storey_rule_snapped(None, 150.0, 10) is False


def test_storey_rule_margin_reports_absence_as_absence():
    # a building whose Tipo15 join failed has no margin, not a margin of nought
    assert db.storey_rule_margin(None, 100.0) is None
    assert db.storey_rule_margin(0, 100.0) is None
    assert db.storey_rule_margin("abc", 100.0) is None
    assert db.storey_rule_margin(441.0, 145.7) == pytest.approx(0.00892, abs=1e-5)


def test_the_block_parcel_that_forced_the_geometric_cap():
    # 3748901YJ2734H: 17,272 m2 of footprint at altura_max 15, against 38,158 m2
    # of dwellings across 342 flats.  Extruded whole it conditions 259,000 m2 -
    # 5.6 % of Benicalap's energy in one record.  The footprint is the block's,
    # the height is its tallest point; only about 2.2 storeys of housing exist.
    assert db.residential_storeys_from_cadastre(38158.0, 17272.5, 15) == 3


# ---------------------------------------------------------------------------
# The cap has to reach the GEOMETRY, not just the space types: floor that is
# re-typed to Terciario is still lit, heated and conditioned (2026-08-08).
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_cadastral_evidence_cuts_the_storeys_before_they_are_extruded():
    ref = "4648906YJ2744H"
    row = db.load_building_row(ref).copy()
    geometry = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(geometry, ref, mb.NEIGHBORS_SHP)
    party = mb.find_party_walls(geometry, ref, mb.NEIGHBORS_SHP, neighbors=neighbors)

    built = int(row["altura_max"])
    footprint = mb.prepare_footprint(geometry)[1]
    # Give it dwelling area for two storeys of a taller building.
    row["tipo15_res_area_m2"] = footprint * 2 - 1.0

    osm, stats = db.build_deep_model(row, party, 20.0, neighbors=neighbors)
    assert stats["built_storeys"] == built
    assert stats["storey_cap_applied"] is True
    assert stats["built_storeys"] > stats["n_floors_residential"]
    assert stats["n_floors_residential"] == 2
    # and the floor is gone from the model, not merely renamed
    assert len(osm.getSpaces()) == 3                      # 2 dwellings + the bajo
    assert stats["res_area_m2"] == pytest.approx(round(footprint * 2, 1), abs=0.2)


@pytest.mark.integration
def test_a_building_too_short_to_shrink_reports_no_cap():
    """The smallest model the builder can extrude is a bajo plus one storey.

    4149106YJ2745A has a residential ground and one storey above it, and here the
    cadastre supports only one of the two.  The geometry cannot shrink, so the
    flag has to say so - reporting "capped" whenever the rule merely bound would
    overstate how many buildings this touched, and its energy is unchanged.
    """
    ref = "4149106YJ2745A"
    row = db.load_building_row(ref).copy()
    row["ground_use"] = "residential"
    geometry = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(geometry, ref, mb.NEIGHBORS_SHP)
    party = mb.find_party_walls(geometry, ref, mb.NEIGHBORS_SHP, neighbors=neighbors)
    row["tipo15_res_area_m2"] = mb.prepare_footprint(geometry)[1] * 0.5

    _, stats = db.build_deep_model(row, party, 8.0, neighbors=neighbors)
    assert stats["storey_cap_applied"] is False
    assert stats["built_storeys"] == stats["n_floors_residential"] == int(row["altura_max"])
    # the excess is still taken out of the dwellings the only way left: re-typing
    assert stats["mixed_use_storeys_converted"] == 1


@pytest.mark.integration
def test_the_cap_does_not_change_what_the_building_stands_among():
    """Neighbour shading reads a separate file, so trimming the target is safe.

    The two paths were deliberately separated on 2026-07-28; this pins it, because
    a regression here would move every shaded building's cooling without failing
    anything else.
    """
    ref = "4648906YJ2744H"
    geometry = mb.clean_polygon(db.load_building_row(ref).geometry)
    neighbors = mb.load_neighbors(geometry, ref, mb.NEIGHBORS_SHP)
    party = mb.find_party_walls(geometry, ref, mb.NEIGHBORS_SHP, neighbors=neighbors)
    footprint = mb.prepare_footprint(geometry)[1]

    uncapped = db.build_deep_model(db.load_building_row(ref).copy(), party, 20.0,
                                   neighbors=neighbors)[1]
    capped_row = db.load_building_row(ref).copy()
    capped_row["tipo15_res_area_m2"] = footprint * 2 - 1.0
    capped = db.build_deep_model(capped_row, party, 20.0, neighbors=neighbors)[1]

    assert capped["n_floors_residential"] < uncapped["n_floors_residential"]
    # the context is read from the neighbours' own file and does not move
    assert capped["n_shading_surfaces"] == uncapped["n_shading_surfaces"]
    # party walls are the target's OWN walls, so they follow its height - the
    # same count per storey, against the same neighbours
    assert (capped["n_party_surfaces"] / capped["n_floors_total"]
            == uncapped["n_party_surfaces"] / uncapped["n_floors_total"])


@pytest.mark.integration
def test_without_cadastral_area_the_building_is_built_exactly_as_before(pilot_model):
    """The single-building CLI path reads the raw GIS row, which has no Tipo15."""
    _, stats = pilot_model
    assert stats["storey_cap_applied"] is False
    assert stats["built_storeys"] == stats["n_floors_residential"] == 5
    assert stats["mixed_use_basis"] == "unchecked_no_tipo15"
    assert stats["res_area_m2"] == pytest.approx(PILOT_RES_AREA, abs=0.1)
    assert stats["conditioned_to_cadastral_ratio"] is None


# ---------------------------------------------------------------------------
# Metering the commercial storeys apart from the dwellings
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_terciario_loads_carry_their_own_end_use_subcategory(pilot_model):
    osm, stats = pilot_model
    tagged = stats["deep_layers"]["terciario_metering"]
    assert tagged["subcategory"] == db.TERCIARIO_END_USE_SUBCATEGORY
    assert tagged["tagged_lights"] > 0
    assert tagged["tagged_equipment"] > 0

    terciario = db._by_name(osm.getSpaceTypes(), db.GROUND_TERCIARIO_SPACE_TYPE)
    for load in list(terciario.lights()) + list(terciario.electricEquipment()):
        assert load.endUseSubcategory() == db.TERCIARIO_END_USE_SUBCATEGORY
    # the dwellings keep the default, so the two never share a row
    residential = db._by_name(osm.getSpaceTypes(), db.RESIDENTIAL_SPACE_TYPE)
    for load in list(residential.lights()) + list(residential.electricEquipment()):
        assert load.endUseSubcategory() != db.TERCIARIO_END_USE_SUBCATEGORY


# ---------------------------------------------------------------------------
# The partial top storey
# ---------------------------------------------------------------------------
def test_top_storey_fraction_is_the_part_the_record_fills():
    # 1500 m2 over a 562 m2 plate = 2.669 storeys, loaded as three (the rule
    # rounds up), so the third is 0.669 full.
    assert db.top_storey_fraction(1500.0, 562.0, 3) == pytest.approx(0.66904, abs=1e-5)


def test_the_question_is_asked_about_the_storeys_actually_loaded():
    """The bug the first version of this shipped with, pinned.

    The geometric cap has already lowered the building to `ceil(c/f)` by the
    time this runs, so asking against the BUILT count makes the rule look as
    if it never bound and the layer silently never fires.  Same building, two
    counts, and only the housing one gives an answer.
    """
    assert db.top_storey_fraction(1500.0, 562.0, 3) is not None   # as loaded
    assert db.top_storey_fraction(1500.0, 562.0, 5) is None       # as built


def test_top_storey_fraction_is_none_when_the_record_exceeds_the_model():
    # The pilot: 5.18 storeys of record against 5 loaded.  The cadastre claims
    # MORE dwelling than the model provides - the two-sided disagreement, a
    # different question - so nothing here may be scaled away.
    assert db.top_storey_fraction(2910.0, 562.0, 5) is None


def test_top_storey_fraction_is_none_on_an_exact_integer_fill():
    # Three storeys exactly: the top one is whole and must not be scaled.
    assert db.top_storey_fraction(3 * 562.0, 562.0, 3) is None


@pytest.mark.parametrize("cadastral", [None, 0, -1, "", "abc"])
def test_top_storey_fraction_never_guesses_without_a_record(cadastral):
    # A failed Tipo15 join, the single-building CLI, the Rai replica, a stock
    # with no cadastre at all: none of them may have a storey emptied.
    assert db.top_storey_fraction(cadastral, 562.0, 5) is None


def test_the_block_parcel_keeps_a_fifth_of_its_top_storey():
    # 3748901YJ2734H again: 38,158 m2 on a 17,272.5 m2 plate = 2.2092 storeys,
    # capped to 3 and loaded as 3 - so the third is only 21 % dwelling.
    assert db.residential_storeys_from_cadastre(38158.0, 17272.5, 15) == 3
    assert db.top_storey_fraction(38158.0, 17272.5, 3) == pytest.approx(0.209176, abs=1e-6)


def test_the_partial_storey_layer_runs_after_the_hvac_not_before():
    """The ordering is load-bearing and the opposite of `apply_mixed_use_storeys`.

    DHW and the PTHP COP split both resolve the dwelling space type by NAME, so
    a storey moved onto a clone before them silently loses its hot water and is
    sized as commercial.  Asserted against the source so that reordering the
    calls fails here rather than in a 5-hour run.
    """
    import inspect

    # Anchored on the CALL, not the name: the docstring above mentions this
    # layer too, and a bare `index` would match the prose and pass regardless.
    source = inspect.getsource(db.build_deep_model)
    call = source.index("apply_partial_top_storey(osm,")
    assert source.index("add_dhw_loop(osm,") < call
    assert source.index("add_pthp_hvac(osm,") < call


def _pilot_with_record(area: float, **kwargs):
    row = db.load_building_row(PILOT).copy()
    row["tipo15_res_area_m2"] = area
    geometry = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(geometry, PILOT, mb.NEIGHBORS_SHP)
    party = mb.find_party_walls(geometry, PILOT, mb.NEIGHBORS_SHP, neighbors=neighbors)
    return db.build_deep_model(row, party, PILOT_OCCUPANTS,
                               neighbors=neighbors, **kwargs)


# The pilot forced rule-bound, so the layer has something to act on.  Its real
# record covers 5.18 storeys and does not bind; 1500 m2 makes it 2.669, which
# is the ordinary case across the stock (median fraction 0.625).
PARTIAL_RECORD_M2 = 1500.0


@pytest.fixture(scope="module")
def partial_pilot():
    return _pilot_with_record(PARTIAL_RECORD_M2)


@pytest.fixture(scope="module")
def unscaled_twin():
    """The same building with the layer off - the pre-correction behaviour."""
    return _pilot_with_record(PARTIAL_RECORD_M2, partial_top_storey=False)


@pytest.mark.integration
def test_partial_storey_scales_the_top_floor_only(partial_pilot):
    osm, stats = partial_pilot
    record = stats["deep_layers"]["partial_top_storey"]
    fraction = record["fraction"]
    assert 0.0 < fraction < 1.0

    clone = db._by_name(osm.getSpaceTypes(),
                        db.RESIDENTIAL_SPACE_TYPE + db.PARTIAL_TOP_STOREY_SUFFIX)
    assert clone is not None, "the partial storey got no space type of its own"
    assert len(list(clone.spaces())) == 1

    # the scaled space really is the highest one
    full = db._residential_space_type(osm)
    def base(space):
        return min(v.z() for srf in space.surfaces() for v in srf.vertices())
    top = list(clone.spaces())[0]
    assert all(base(top) > base(s) for s in full.spaces())


@pytest.mark.integration
def test_scaling_the_clone_leaves_the_other_storeys_untouched(partial_pilot):
    """OpenStudio's `clone` SHARES load definitions - measured, not assumed.

    Scaling a definition in place would have quietly scaled every dwelling
    storey in the building.  This is the guard for that.
    """
    osm, stats = partial_pilot
    fraction = stats["deep_layers"]["partial_top_storey"]["fraction"]
    full = db._residential_space_type(osm)
    clone = db._by_name(osm.getSpaceTypes(),
                        db.RESIDENTIAL_SPACE_TYPE + db.PARTIAL_TOP_STOREY_SUFFIX)

    full_w = list(full.lights())[0].lightsDefinition().wattsperSpaceFloorArea().get()
    part_w = list(clone.lights())[0].lightsDefinition().wattsperSpaceFloorArea().get()
    assert full_w == pytest.approx(4.4, rel=1e-6)      # the CTE norm, unmoved
    assert part_w == pytest.approx(4.4 * fraction, rel=1e-6)

    full_e = list(full.electricEquipment())[0].electricEquipmentDefinition() \
        .wattsperSpaceFloorArea().get()
    part_e = list(clone.electricEquipment())[0].electricEquipmentDefinition() \
        .wattsperSpaceFloorArea().get()
    assert full_e == pytest.approx(4.4, rel=1e-6)
    assert part_e == pytest.approx(4.4 * fraction, rel=1e-6)


@pytest.mark.integration
def test_the_partial_storey_does_not_lose_people(partial_pilot):
    """The trap this layer exists to avoid creating.

    Density is set on the dwelling area, not the geometric one, precisely so
    that scaling the top storey redistributes the residents rather than
    deleting a quarter of them - which is what a geometric basis would do, and
    DHW (computed from the head count, not the area) would then no longer match
    the gains.
    """
    osm, stats = partial_pilot
    people = sum(space.numberOfPeople()
                 for space_type in (db._residential_space_type(osm),
                                    db._by_name(osm.getSpaceTypes(),
                                                db.RESIDENTIAL_SPACE_TYPE
                                                + db.PARTIAL_TOP_STOREY_SUFFIX))
                 for space in space_type.spaces())
    # rel tolerance absorbs the 6-decimal rounding of the reported density,
    # the same allowance the unscaled occupancy test makes
    assert people == pytest.approx(stats["occupants_applied"], rel=1e-4)
    assert stats["dwelling_area_m2"] == pytest.approx(PARTIAL_RECORD_M2, abs=0.5)


@pytest.mark.integration
def test_the_partial_storey_keeps_its_envelope_leakage(partial_pilot):
    """Only the occupant-coupled ventilation follows the people.

    The storey physically exists, so its airtightness and its summer-night
    purge are the same as any other floor's.
    """
    osm, stats = partial_pilot
    fraction = stats["deep_layers"]["partial_top_storey"]["fraction"]
    full = db._residential_space_type(osm)
    clone = db._by_name(osm.getSpaceTypes(),
                        db.RESIDENTIAL_SPACE_TYPE + db.PARTIAL_TOP_STOREY_SUFFIX)

    def by_kind(space_type):
        # Three objects, and BOTH envelope ones must be checked - collapsing
        # them under one key makes the result depend on iteration order.
        out = {}
        for i in space_type.spaceInfiltrationDesignFlowRates():
            name = i.nameString().lower()
            if db.OCCUPANCY_INFILTRATION_MARKER in name:
                out["occupancy"] = i
            elif "constante" in name:
                out["airtightness"] = i
            elif "nocturna" in name:
                out["summer_night"] = i
        return out

    a, b = by_kind(full), by_kind(clone)
    assert set(a) == set(b) == {"occupancy", "airtightness", "summer_night"}
    assert b["occupancy"].flowperSpaceFloorArea().get() == pytest.approx(
        a["occupancy"].flowperSpaceFloorArea().get() * fraction, rel=1e-6)
    # the storey is really there, so its shell behaves like any other floor's
    assert b["airtightness"].airChangesperHour().get() == pytest.approx(0.2, rel=1e-9)
    assert b["summer_night"].airChangesperHour().get() == pytest.approx(4.0, rel=1e-9)


@pytest.mark.integration
def test_the_partial_storey_changes_no_geometry(partial_pilot, unscaled_twin):
    """The whole point of scaling loads instead of the shell.

    Compared against the SAME building with the layer switched off, so the only
    difference between the two is the scaling.  Identical plate, storeys and
    openings means the frozen builder saw identical inputs and its hash cannot
    have moved.  (The pilot's own unmodified model is a different building here
    - its record does not bind, so it keeps all five storeys.)
    """
    _, partial = partial_pilot
    _, plain = unscaled_twin
    for key in ("footprint_m2", "n_floors_total", "n_floors_residential",
                "n_windows", "window_area_m2", "n_party_surfaces",
                "n_shading_surfaces", "res_area_m2"):
        assert partial[key] == plain[key], key
    assert partial["deep_layers"]["partial_top_storey"]["geometry_changed"] is False


@pytest.mark.integration
def test_the_layer_is_inert_where_the_rule_never_bound(pilot_model):
    """The pilot's own record does not bind, so nothing may have happened.

    This is the single-building CLI, the Rai replica and every Tipo15-less
    stock in one assertion: the layer must be invisible unless there is a
    recorded partial storey to act on.
    """
    osm, stats = pilot_model
    assert stats["top_storey_fraction"] is None
    assert stats["dwelling_area_m2"] == stats["res_area_m2"]
    assert "partial_top_storey" not in stats["deep_layers"]
    assert db._by_name(osm.getSpaceTypes(),
                       db.RESIDENTIAL_SPACE_TYPE + db.PARTIAL_TOP_STOREY_SUFFIX) is None


@pytest.mark.integration
def test_turning_the_layer_off_reproduces_the_unscaled_model(unscaled_twin):
    """The A/B off-state must be the pre-correction behaviour exactly.

    One decision point drives both the dwelling-area basis and the scaling, so
    switching it off has to restore the geometric basis too - otherwise a v8
    comparison would be measuring two changes at once.
    """
    osm, stats = unscaled_twin
    assert stats["top_storey_fraction"] is None
    assert stats["dwelling_area_m2"] == stats["res_area_m2"]
    assert "partial_top_storey" not in stats["deep_layers"]
    assert db._by_name(osm.getSpaceTypes(),
                       db.RESIDENTIAL_SPACE_TYPE + db.PARTIAL_TOP_STOREY_SUFFIX) is None


@pytest.mark.integration
def test_the_new_fields_survive_into_the_ledger(partial_pilot):
    """Writing a value is not the same as the ledger carrying it.

    The v5 ground-use near-miss: a policy resolved correctly, written into
    stats, and dropped by the runner's column allowlist one layer later.  This
    checks the model's own output against the actual allowlist.
    """
    import stock_runner as sr

    _, stats = partial_pilot
    for field in ("top_storey_fraction", "dwelling_area_m2"):
        assert field in stats, f"the model never wrote {field}"
        assert field in sr.LEDGER_METRICS, f"the ledger would drop {field}"


# ---------------------------------------------------------------------------
# The frame guard must tell "nothing to frame" apart from "framing broke"
# ---------------------------------------------------------------------------
class _StubFrame:
    def nameString(self): return db.WINDOW_FRAME_NAME
    def frameWidth(self): return db.WINDOW_FRAME_WIDTH_M


class _StubVertex:
    def __init__(self, x, y): self.x, self.y = x, y
    def __sub__(self, other): return _StubVertex(self.x - other.x, self.y - other.y)
    def length(self): return (self.x ** 2 + self.y ** 2) ** 0.5


class _StubSubSurface:
    def __init__(self, kind, accepts=True):
        self._kind, self._accepts = kind, accepts
    def subSurfaceType(self): return self._kind
    def setWindowPropertyFrameAndDivider(self, frame): return self._accepts
    def grossArea(self): return 1.44
    def vertices(self):
        return [_StubVertex(0, 0), _StubVertex(1.2, 0),
                _StubVertex(1.2, 1.2), _StubVertex(0, 1.2)]


class _StubModel:
    def __init__(self, subs): self._subs = subs
    def getWindowPropertyFrameAndDividers(self): return [_StubFrame()]
    def getSubSurfaces(self): return self._subs


def test_a_building_with_nothing_to_glaze_is_finished_not_rejected():
    """The inner-block case: four party walls, so no window ever gets drawn.

    Measured on the live stock: 70 of the 71 buildings that reach this step
    with no glazing have an exterior-wall share of exactly 0.000.  Refusing to
    invent a window for them is right; refusing to finish them is not.
    """
    # walls only - not one glazed opening in the model
    frames = db.apply_window_frames(_StubModel([_StubSubSurface("Door")]))

    assert frames["glazed_subsurfaces"] == 0
    assert frames["windows_with_frame"] == 0
    # the absence is stated, not left to be inferred from a zero area
    assert frames["glass_area_m2"] == 0.0
    assert frames["opening_area_m2"] == 0.0


def test_the_guard_still_fires_when_framing_actually_breaks():
    """Glazing exists and none of it took the frame - the case it guards."""
    subs = [_StubSubSurface("FixedWindow", accepts=False),
            _StubSubSurface("GlassDoor", accepts=False)]
    with pytest.raises(RuntimeError, match="none accepted the frame"):
        db.apply_window_frames(_StubModel(subs))


def test_a_glazed_building_takes_exactly_the_path_it_took_before():
    """The relaxation must be unreachable for any building that already works.

    A model with glazing has candidates >= 1, so the guard's new `candidates
    and` clause can never change its outcome - proved here rather than argued.
    """
    subs = [_StubSubSurface("FixedWindow"), _StubSubSurface("GlassDoor"),
            _StubSubSurface("Door")]
    frames = db.apply_window_frames(_StubModel(subs))

    assert frames["glazed_subsurfaces"] == 2       # the plain door is not glazing
    assert frames["windows_with_frame"] == 2
    assert frames["glass_area_m2"] == pytest.approx(2 * 1.44, abs=0.05)
    # the frame grows outwards, so EnergyPlus reports a larger opening
    assert frames["opening_area_m2"] > frames["glass_area_m2"]
