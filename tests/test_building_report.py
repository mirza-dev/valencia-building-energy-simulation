"""Academic-contract tests for the readable building report."""
from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
import math

import pytest

from workbench import building_report


def _record(*, ground_use: str = "residential") -> tuple[dict, dict]:
    results = {
        "space_heating_kwh": 100.0,
        "cooling_kwh": 50.0,
        "dhw_kwh": 30.0,
        "total_site_kwh": 500.0,
        "space_heating_kwh_m2": 10.0,
        "cooling_kwh_m2": 5.0,
        "dhw_kwh_m2": 3.0,
        "total_site_kwh_m2": 50.0,
        "space_heating_kwh_m2_conditioned": 8.0,
        "cooling_kwh_m2_conditioned": 4.0,
        "total_site_kwh_m2_conditioned": 40.0,
        "residential_site_kwh": 480.0,
        "terciario_site_kwh": 20.0,
        "residential_split_basis": (
            "lights and equipment attributed by space type; HVAC and DHW "
            "remain in the residential figure"
        ),
    }
    carbon = {"hvac_co2_t_yr": 0.04, "total_site_co2_t_yr": 0.12}
    summary = {
        "refparcela": "REF-1",
        "status": "ok",
        "ground_use": ground_use,
        "n_floors_total": 2,
        "n_floors_residential": 1,
        "residential_storeys_effective": 1.7,
        "res_area_m2": 10.0,
        "tipo15_res_area_m2": 9.5,
        "total_conditioned_area_m2": 12.5,
        "footprint_m2": 6.25,
        "occupants_applied": 2.0,
        "padron_occupants": 2.0,
        "occupants_source": "cadastre.pob_total",
        "zero_policy": "literal_zero",
        "n_windows": 4,
        "n_balcony_doors": 1,
        "window_area_m2": 3.0,
        "n_party_surfaces": 2,
        "n_shading_surfaces": 5,
        "qa_all_passed": True,
        "warnings": 1,
        "severes": 0,
        "severes_benign_shading_ems": 0,
        "severes_unexplained": 0,
        "fatals": 0,
        "climate": "test climate",
        "climate_fingerprint": "c" * 64,
        "target_gis_path": "/private/machine/prepared-stock.gpkg",
        "context_gis_path": "/private/machine/context.shp",
        "verified_profile": {
            "profile_id": "profile-1",
            "schema_version": 1,
            "verified_on": "2026-07-27",
            "verified_against": "Rai EdiPluriP04 evidence",
            "fingerprint": "p" * 64,
            "locked_source_hashes": {"model.py": "a" * 64},
            "live_source_hashes": {"model.py": "a" * 64},
        },
        **results,
        **carbon,
    }
    layers = {
        "summary": summary,
        "results": results,
        "carbon": carbon,
        "qa": [
            {"check": "conditioned_area_m2", "model": 12.5,
             "eplus": 12.49, "tolerance": 0.005, "passed": True},
            {"check": "glazing_area_m2", "model": 3.0,
             "eplus": 2.98, "tolerance": 0.02, "passed": True},
            {"check": "zone_count", "model": 2,
             "eplus": 2, "tolerance": 0, "passed": True},
            {"check": "unmet_hours", "model": 0,
             "eplus": 20, "tolerance": 500, "passed": True},
            {"check": "plausible_band_total_site_kwh_m2", "model": 50,
             "eplus": "[1.0-200.0]", "tolerance": "-", "passed": True},
        ],
        "layers": {
            "ground": {
                "ground_space_type": (
                    "Dwelling" if ground_use == "residential" else "Terciario"
                ),
                "ground_thermostat": True,
                "in_floor_area_basis": ground_use == "residential",
            },
            "ground_glazing": {
                "ground_windows": 2, "ground_glass_area_m2": 1.2,
            },
            "occupancy": {
                "occupants": 2.0, "padron_occupants": 2.0,
                "m2_per_person": 5.0, "people_per_m2": 0.2,
                "plausibility": {"status": "plausible", "note": ""},
            },
            "mixed_use": {
                "residential_storeys": 2, "storeys_converted": 0,
            },
            "partial_top_storey": {
                "fraction": 0.7, "space": "Space 1 - 1th floor",
            },
            "dhw": {
                "dhw_litres_per_person_day": 28.0,
                "dhw_setpoint_c": 50.0,
                "dhw_equipment_count": 2,
            },
            "terciario_metering": {
                "spaces": 0 if ground_use == "residential" else 1,
                "tagged_lights": 1, "tagged_equipment": 1,
            },
            "weather": {
                "climate": "test climate",
                "epw_file": "/private/machine/weather.epw",
                "climate_fingerprint": "c" * 64,
            },
        },
    }
    row = {
        "refparcela": "REF-1", "status": "ok", "runner_schema": 2,
        "run_identity": "r" * 64, "climate_fingerprint": "c" * 64,
        "template_fingerprint": "t" * 64, "policy_fingerprint": "q" * 64,
        "stock_source_fingerprint": "s" * 64,
    }
    return layers, row


def _render(layers: dict, row: dict | None) -> str:
    return building_report.render(
        layers, run="academic-run", reference="REF-1", ledger_row=row,
        metadata={
            "energyplus_version": "25.2.0-test",
            "verified_profile": layers["summary"]["verified_profile"],
        },
    )


def test_every_visible_metric_has_a_complete_scientific_contract():
    assert building_report.METRICS
    for spec in building_report.METRICS.values():
        assert spec.label and spec.unit and spec.period and spec.denominator
        assert spec.description and spec.missing == "—"
        assert spec.source[0] in {"results", "carbon"}
        assert spec.precision >= 0


@pytest.mark.parametrize(
    ("ground_use", "expected", "forbidden"),
    [
        (
            "residential",
            "represented as a dwelling space",
            "Terciario regime: conditioned commercial space",
        ),
        (
            "commercial",
            "Terciario regime: conditioned commercial space",
            "represented as a dwelling space",
        ),
    ],
)
def test_ground_storey_explanation_follows_preserved_use(
    ground_use, expected, forbidden,
):
    layers, row = _record(ground_use=ground_use)
    page = _render(layers, row)
    assert expected in page
    assert forbidden not in page
    assert (
        "Ground-storey openings" in page
        if ground_use == "residential"
        else "Commercial ground-storey glazing" in page
    )


def test_commercial_tags_are_configuration_not_claimed_consumption():
    layers, row = _record()
    page = _render(layers, row)
    assert "Commercial spaces</th><td>0" in page
    assert "meter configuration, not proof of commercial consumption" in page
    assert "residential-attributed upper bound" in page.lower()
    assert "exact residential share" not in page.lower()


@pytest.mark.parametrize(
    ("source", "padron", "applied", "status", "expected"),
    [
        ("cadastre.pob_total", 0.0, 0.0, "unoccupied",
         "Padrón is an administrative population record"),
        ("imputed.cluster_median_per_dwelling", 0.0, 3.0, "plausible",
         "imputed from the cluster-median people-per-dwelling policy"),
        ("cadastre.pob_total", 26.0, 10.0, "implausible_dense_capped",
         "was capped before simulation"),
    ],
)
def test_occupancy_policy_is_explicit(
    source, padron, applied, status, expected,
):
    layers, row = _record()
    layers["summary"].update({
        "occupants_source": source,
        "padron_occupants": padron,
        "occupants_applied": applied,
    })
    layers["layers"]["occupancy"].update({
        "padron_occupants": padron,
        "occupants": applied,
        "plausibility": {"status": status, "note": "preserved note"},
    })
    page = _render(layers, row)
    assert expected in page
    assert "not real-time occupancy measurement" in page


def test_annual_and_event_periods_never_share_false_year_labels():
    layers, annual = _record()
    annual_page = _render(layers, annual)
    assert "Annual simulation" in annual_page
    assert "tCO₂/year" in annual_page

    event = annual | {
        "run_mode": "microclimate_event",
        "event_days": 8,
        "event_window": "08-16..08-23",
    }
    event_page = _render(layers, event)
    assert "Microclimate event · 08-16..08-23 · 8 days" in event_page
    assert "tCO₂/event period" in event_page
    assert "tCO₂/year" not in event_page
    assert "tCO₂/yr" not in event_page


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"severes": 2, "severes_benign_shading_ems": 2},
         "ACCEPTED WITH CLASSIFIED DIAGNOSTICS"),
        ({"severes": 1, "severes_unexplained": 1},
         "FAILED / DO NOT USE"),
        ({"fatals": 1}, "FAILED / DO NOT USE"),
        ({"qa_all_passed": False}, "FAILED / DO NOT USE"),
        ({"qa_all_passed": None}, "NOT ASSESSED"),
    ],
)
def test_qa_decision_matrix(changes, label):
    layers, row = _record()
    layers["summary"].update(changes)
    page = _render(layers, row)
    assert label in page


def test_success_with_missing_period_is_incomplete_and_not_citable():
    layers, row = _record()
    row["run_mode"] = "legacy_unknown"
    page = _render(layers, row)
    assert "INCOMPLETE EVIDENCE — DO NOT CITE" in page
    assert "Period not recorded" in page


@pytest.mark.parametrize("field", [
    "severes", "severes_benign_shading_ems", "severes_unexplained", "fatals",
])
def test_success_with_missing_diagnostic_count_is_incomplete(field):
    layers, row = _record()
    layers["summary"].pop(field)
    page = _render(layers, row)
    assert "INCOMPLETE EVIDENCE — DO NOT CITE" in page


def test_success_with_no_individual_qa_evidence_is_incomplete():
    layers, row = _record()
    layers["qa"] = []
    page = _render(layers, row)
    assert "INCOMPLETE EVIDENCE — DO NOT CITE" in page
    assert "No individual QA checks were preserved" in page


@pytest.mark.parametrize("bad", ["12.3", True, math.nan, math.inf, -math.inf])
def test_invalid_numeric_types_are_never_formatted_as_results(bad):
    layers, row = _record()
    layers["results"]["total_site_kwh"] = bad
    # Keep the duplicate equal so this test exercises numeric validation rather
    # than the separately tested evidence-conflict path.
    layers["summary"]["total_site_kwh"] = bad
    page = _render(layers, row)
    assert "INCOMPLETE EVIDENCE — DO NOT CITE" in page
    assert ">—</td>" in page
    assert ">nan<" not in page.lower()
    assert ">inf<" not in page.lower()


def test_measured_zero_is_preserved_while_missing_optional_value_is_dash():
    layers, row = _record()
    layers["results"]["terciario_site_kwh"] = 0.0
    layers["summary"]["terciario_site_kwh"] = 0.0
    layers["summary"].pop("tipo15_res_area_m2")
    page = _render(layers, row)
    assert "Commercial-attributed lighting and equipment</th><td>" in page
    assert ">0.0<span class=\"unit\"" in page
    assert "Cadastral Tipo15 dwelling area</th><td>—</td>" in page


def test_duplicate_result_conflict_is_visible_and_not_citable():
    layers, row = _record()
    layers["summary"]["total_site_kwh"] = 999.0
    page = _render(layers, row)
    assert "EVIDENCE CONFLICT — DO NOT CITE" in page
    assert "results.total_site_kwh disagrees with summary.total_site_kwh" in page
    assert "999.0" not in page


@pytest.mark.parametrize("where", ["summary", "ledger"])
def test_route_reference_identity_mismatch_refuses_report(where):
    layers, row = _record()
    if where == "summary":
        layers["summary"]["refparcela"] = "OTHER"
    else:
        row["refparcela"] = "OTHER"
    with pytest.raises(building_report.EvidenceIdentityError):
        _render(layers, row)


class _SemanticAudit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.h1 = 0
        self.headings: list[int] = []
        self.ids: list[str] = []
        self.captions = 0
        self.table_headers: list[dict[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "h1":
            self.h1 += 1
        if tag in {"h1", "h2", "h3"}:
            self.headings.append(int(tag[1]))
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag == "caption":
            self.captions += 1
        if tag == "th":
            self.table_headers.append(attributes)


def test_html_is_semantic_accessible_escaped_and_path_safe():
    layers, row = _record()
    layers["summary"]["ground_use_source"] = '<script>alert("x")</script>'
    page = _render(layers, row)
    audit = _SemanticAudit()
    audit.feed(page)

    assert audit.h1 == 1
    assert len(audit.ids) == len(set(audit.ids))
    assert audit.captions >= 8
    assert all(header.get("scope") in {"row", "col"} for header in audit.table_headers)
    assert all(
        next_level <= level + 1
        for level, next_level in zip(audit.headings, audit.headings[1:])
    )
    assert '<script>alert("x")</script>' not in page
    assert "/private/machine/" not in page
    assert "prepared-stock.gpkg" in page
    assert "context.shp" in page
    assert "weather.epw" in page
    assert 'aria-label="Report sections"' in page
    assert 'class="skip-link"' in page
    assert "<caption>" in page
    assert 'scope="row"' in page and 'scope="col"' in page


def test_critical_academic_terminology_and_sources_are_pinned():
    layers, row = _record()
    page = _render(layers, row)
    assert "simulation, not a utility-bill measurement" in page
    assert "not primary energy" in page
    assert "exclude embodied carbon" in page
    assert "0.331 kgCO₂/kWh_final" in page
    assert "0.252 kgCO₂/kWh_final" in page
    assert "28 L/person/day at 60 °C" in page
    assert "ABUPS totals" in page
    assert "user-defined end-use subcategories" in page
    assert "unpublished" in page.lower()
    assert "Raw model identifier" in page
    assert ">1th floor<" not in page
    assert "Rai&#x27;s regime" not in page
    assert "nothing on this page is recomputed" not in page


def _converted_ground(layers: dict) -> dict:
    """Re-type the ground storey the way `apply_mixed_use_storeys` does.

    `deep_building` runs `apply_residential_ground` first and
    `apply_mixed_use_storeys` after it, and the second stage takes the lowest
    excess dwelling storey back to conditioned Terciario.  `summary.ground_use`
    keeps carrying the input policy either way.
    """
    layers["layers"]["mixed_use"] = {
        "residential_storeys": 2,
        "storeys_converted": 1,
        "space_type": "Espacio Tipo Terciario 8h Media CTE",
        "in_floor_area_basis": False,
        "converted_spaces": ["Space 0 - Ground (commercial or buffer zone)"],
    }
    return layers


def test_converted_ground_storey_reports_what_energyplus_simulated():
    """Reading only the first stage misreported 1 654 buildings - 6.62 %.

    They were told the ground floor was a dwelling with its own thermostat,
    counted in the residential denominator, while the OSM handed to EnergyPlus
    says commercial, no thermostat, excluded.
    """
    layers, row = _record(ground_use="residential")
    page = _render(_converted_ground(layers), row)
    assert (
        "Final modelled ground-storey use</th>"
        "<td>commercial (converted by cadastral mixed-use allocation)</td>"
    ) in page
    assert "Espacio Tipo Terciario 8h Media CTE" in page
    assert "represented as a dwelling space" not in page
    # the input policy is surfaced, not overwritten: both stages stay readable
    assert "Initial ground-use policy</th><td>residential</td>" in page
    assert "EVIDENCE CONFLICT" not in page


def test_unconverted_ground_storey_keeps_the_recorded_narrative():
    """An absent or inert second stage is not a stage that disagrees."""
    layers, row = _record(ground_use="residential")
    page = _render(layers, row)
    assert "represented as a dwelling space" in page
    assert "converted by cadastral mixed-use allocation" not in page
    assert "EVIDENCE CONFLICT" not in page

    older, older_row = _record(ground_use="residential")
    del older["layers"]["mixed_use"]          # ledger written before the stage
    older_page = _render(older, older_row)
    assert "represented as a dwelling space" in older_page
    assert "EVIDENCE CONFLICT" not in older_page


def test_conversion_of_another_storey_leaves_the_ground_alone():
    layers, row = _record(ground_use="residential")
    layers["layers"]["mixed_use"] = {
        "residential_storeys": 3, "storeys_converted": 1,
        "space_type": "Espacio Tipo Terciario 8h Media CTE",
        "in_floor_area_basis": False,
        "converted_spaces": ["Space 1 - 1th floor"],
    }
    page = _render(layers, row)
    assert "represented as a dwelling space" in page
    assert "commercial (converted by cadastral mixed-use allocation)" not in page


def test_unrecorded_conversion_target_is_a_conflict_not_a_guess():
    """The engine converts upward, so a residential ground would have gone
    first - but that is inferred from today's ordering, not read from this
    run's evidence, and inferring it is how this project has had to retract
    published statements before."""
    layers, row = _record(ground_use="residential")
    layers["layers"]["mixed_use"] = {
        "residential_storeys": 2, "storeys_converted": 1,
    }
    page = _render(layers, row)
    assert "EVIDENCE CONFLICT" in page
    assert "layers.mixed_use converted a storey without recording which one" in page


def test_qa_area_row_names_the_basis_it_actually_checks():
    """`run_simulation` compares `res_area_m2` against the EnergyPlus tabular
    value "Net Conditioned Building Area", and `res_area_m2` follows the
    floor-area basis.  Labelling the row "Conditioned floor area" read as a
    contradiction of the total conditioned floor area, which counts a storey
    held outside that basis."""
    layers, row = _record()
    layers["summary"]["total_conditioned_area_m2"] = 25.0
    page = _render(_converted_ground(layers), row)
    assert "Residential floor area (EnergyPlus net conditioned basis)" in page
    assert "EnergyPlus Net Conditioned Building Area" in page
    assert (
        "Cross-check of model-reported and EnergyPlus-reported conditioned area"
        not in page
    )
    # terminology only: the recorded numbers are untouched and stay separate
    assert "12.50" in page and "12.49" in page
    assert "25.0" in page


def test_method_subsections_follow_their_parent_section_number():
    """Hard-coded `7.1`-`7.5` printed "6 Model preparation" above "7.1" and
    below "7 Provenance" on every successful report."""
    def parent(page: str) -> str:
        import re
        m = re.search(
            r'<span class="section-number">(\d+)</span> Model preparation', page)
        assert m, "the model-preparation section lost its number"
        return m.group(1)

    layers, row = _record()
    page = _render(layers, row)
    assert parent(page) == "6"
    assert "<h3>6.1 Ground-storey use and openings</h3>" in page
    assert "7.1 Ground-storey use and openings" not in page

    # a recorded failure inserts its own section, pushing the parent to 7
    failed, failed_row = _record()
    failed["summary"]["status"] = "failed"
    failed_row["status"] = "failed"
    failed_page = _render(failed, failed_row)
    assert parent(failed_page) == "7"
    assert "<h3>7.1 Ground-storey use and openings</h3>" in failed_page
    assert "6.1 Ground-storey use and openings" not in failed_page


@pytest.mark.parametrize(
    ("classified", "expected", "forbidden"),
    [
        (0, "No Severe message on this building was classified",
         "Severe messages matched that pattern"),
        (10, "10 Severe messages matched that pattern",
         "No Severe message on this building was classified"),
    ],
)
def test_classified_severe_note_states_its_extent_on_this_building(
    classified, expected, forbidden,
):
    layers, row = _record()
    layers["summary"]["severes"] = classified
    layers["summary"]["severes_benign_shading_ems"] = classified
    page = _render(layers, row)
    assert expected in page
    assert forbidden not in page


def test_classified_severe_note_names_the_pattern_and_never_excuses_it():
    layers, row = _record()
    layers["summary"]["severes"] = 10
    layers["summary"]["severes_benign_shading_ems"] = 10
    page = _render(layers, row)
    # both markers, because a block qualifies only by carrying both
    assert "Missing shade or blind layer in window construction" in page
    assert "EMS Actuator cannot be set" in page
    assert "EnergyPlus still reports them as Severe" in page
    assert "does not extend to any other Severe message" in page
    assert "benign" not in page.lower()
    assert "energyplus approved" not in page.lower()


def test_every_anchor_the_product_links_to_exists_in_the_report():
    """`#overview` resolved to no element, and so did `#diagnostics`: the
    browser silently stayed at the top of the report."""
    layers, row = _record()
    page = _render(layers, row)
    for fragment in ("interpretation", "energy", "geometry", "quality",
                     "diagnostics", "methods", "provenance"):
        assert f'id="{fragment}"' in page, fragment
    assert 'id="overview"' not in page
