"""Deep single-building pipeline: layer contracts and frozen-module guards."""

from __future__ import annotations

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
def test_dhw_follows_cte_he4_and_is_metered_separately(pilot_model):
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
