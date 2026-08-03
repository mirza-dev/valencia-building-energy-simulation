"""Tests for the template contract.

Two kinds of test here:

* one that checks the contract against the **real** template, which is what
  proves the table describes reality rather than my reading of the code;
* the rest against a synthetic minimal model, so each failure mode can be
  produced exactly and without a 7-second template load.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import openstudio
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import template_contract as tc  # noqa: E402

REAL_TEMPLATE = PROJECT_ROOT / "data" / "templates" / "PlantillaOS_v2.osm"


# ---------------------------------------------------------------------------
# A synthetic template that satisfies the contract
# ---------------------------------------------------------------------------
def _build_minimal_model(skip: set[str] | None = None,
                         rename: dict[str, str] | None = None,
                         infiltration_objects: int = 1) -> openstudio.model.Model:
    skip = skip or set()
    rename = rename or {}
    model = openstudio.model.Model()

    makers = {
        "getSpaceTypes": openstudio.model.SpaceType,
        "getConstructions": openstudio.model.Construction,
        "getMaterials": openstudio.model.StandardOpaqueMaterial,
        "getScheduleRulesets": openstudio.model.ScheduleRuleset,
        "getWaterUseEquipmentDefinitions": openstudio.model.WaterUseEquipmentDefinition,
        "getWindowPropertyFrameAndDividers":
            openstudio.model.WindowPropertyFrameAndDivider,
        "getBlinds": openstudio.model.Blind,
    }

    residential = None
    for key, role in tc.TEMPLATE_ROLES.items():
        if key in skip:
            continue
        obj = makers[role.collection](model)
        obj.setName(rename.get(key, role.canonical))
        if key == "residential_space_type":
            residential = obj

    if residential is not None:
        for index in range(infiltration_objects):
            suffix = "" if index == 0 else f" {index}"
            infiltration = openstudio.model.SpaceInfiltrationDesignFlowRate(model)
            infiltration.setName(f"Infitracion Aire constante 0,2ACH Viv CTE{suffix}")
            infiltration.setSpaceType(residential)
        # a decoy that must NOT be counted: the occupancy-coupled ventilation
        other = openstudio.model.SpaceInfiltrationDesignFlowRate(model)
        other.setName("Infiltracion Aire ocupacion 0,004/20 m3/s/persona/m2")
        other.setSpaceType(residential)
    return model


@pytest.fixture
def minimal_template(tmp_path: Path):
    def save(model: openstudio.model.Model, name: str = "template.osm") -> Path:
        path = tmp_path / name
        assert model.save(openstudio.toPath(str(path)), True)
        return path
    return save


# ---------------------------------------------------------------------------
# Against the real template - the lock that keeps the contract honest
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_the_real_template_satisfies_the_contract():
    """Every name in TEMPLATE_ROLES must exist in the template the verified
    numbers were produced with. If this fails, the contract is describing a
    template that does not exist."""
    report = tc.validate_template(REAL_TEMPLATE)
    required = {k for k, r in tc.TEMPLATE_ROLES.items() if r.required}
    assert required <= set(report["found"])
    assert report["infiltration_matches"] == ["Infitracion Aire constante 0,2ACH Viv CTE"]


@pytest.mark.slow
def test_the_real_template_binds_every_role_including_optional_ones():
    report = tc.validate_template(REAL_TEMPLATE)
    assert set(report["found"]) == set(tc.TEMPLATE_ROLES)
    assert report["missing"] == []


# ---------------------------------------------------------------------------
# The contract itself
# ---------------------------------------------------------------------------
def test_a_complete_template_passes(minimal_template):
    path = minimal_template(_build_minimal_model())
    report = tc.validate_template(path)
    assert set(report["found"]) == set(tc.TEMPLATE_ROLES)


def test_a_missing_required_role_is_refused_and_says_what_is_there(minimal_template):
    path = minimal_template(_build_minimal_model(skip={"ground_slab"}))
    with pytest.raises(tc.TemplateError) as excinfo:
        tc.validate_template(path)
    message = str(excinfo.value)
    assert "ground_slab" in message
    assert "Solera con aislante" in message
    # the error has to show what the template does contain, or it is not actionable
    assert "template has:" in message
    assert "Medianera Referencia B" in message


def test_a_missing_optional_role_does_not_block(minimal_template):
    # the interzone pair belongs to the opt-in Rai replica, not a production run
    path = minimal_template(_build_minimal_model(
        skip={"interzone_ceiling", "interzone_floor"}))
    report = tc.validate_template(path)
    assert {m["role"] for m in report["missing"]} == {"interzone_ceiling",
                                                     "interzone_floor"}


def test_every_required_role_is_individually_enforced(minimal_template):
    """No required role may be checked in name only - each one must block."""
    for key, role in tc.TEMPLATE_ROLES.items():
        if not role.required:
            continue
        path = minimal_template(_build_minimal_model(skip={key}), f"{key}.osm")
        with pytest.raises(tc.TemplateError, match=key):
            tc.validate_template(path)


# ---------------------------------------------------------------------------
# The infiltration rule - the one substring match in the frozen builder
# ---------------------------------------------------------------------------
def test_no_dwelling_infiltration_object_is_refused(minimal_template):
    """With none, the airtightness knob silently does nothing at all."""
    path = minimal_template(_build_minimal_model(infiltration_objects=0))
    with pytest.raises(tc.TemplateError, match="silently does nothing"):
        tc.validate_template(path)


def test_several_dwelling_infiltration_objects_are_refused(minimal_template):
    path = minimal_template(_build_minimal_model(infiltration_objects=3))
    with pytest.raises(tc.TemplateError, match="dwelling_infiltration"):
        tc.validate_template(path)


def test_the_occupancy_ventilation_object_is_not_counted(minimal_template):
    """It sits on the same space type but must keep its template value."""
    path = minimal_template(_build_minimal_model())
    report = tc.validate_template(path)
    assert report["infiltration_matches"] == ["Infitracion Aire constante 0,2ACH Viv CTE"]


def test_infiltration_is_scoped_to_the_residential_space_type():
    """Matching names on space types the pipeline never instantiates are ignored."""
    model = _build_minimal_model()
    stray = openstudio.model.SpaceType(model)
    stray.setName("Espacio Tipo Viv Plurif RT2012")
    other = openstudio.model.SpaceInfiltrationDesignFlowRate(model)
    other.setName("Infitracion Aire constante 0,2ACH Viv Plurif RT2012")
    other.setSpaceType(stray)

    assert tc._dwelling_infiltration(model, {}) == [
        "Infitracion Aire constante 0,2ACH Viv CTE"]


# ---------------------------------------------------------------------------
# Aliases and normalisation
# ---------------------------------------------------------------------------
def test_an_unknown_alias_key_is_refused(minimal_template):
    model = _build_minimal_model()
    with pytest.raises(tc.TemplateError, match="do not exist"):
        tc.check_roles(model, {"not_a_role": "whatever"})


def test_aliases_bind_a_differently_named_template(minimal_template):
    path = minimal_template(_build_minimal_model(
        rename={"ground_slab": "Slab With Insulation"}))
    # without the alias it is a refusal
    with pytest.raises(tc.TemplateError, match="ground_slab"):
        tc.validate_template(path)
    # with the alias, everything binds
    report = tc.check_roles(tc.load_model(path),
                            {"ground_slab": "Slab With Insulation"})
    assert report["missing"] == []


def test_normalising_renames_onto_the_canonical_names(minimal_template, tmp_path):
    """This is what lets a foreign template run without touching the frozen builder."""
    path = minimal_template(_build_minimal_model(
        rename={"ground_slab": "Slab With Insulation"}))
    normalised = tc.normalise_template(
        path, {"ground_slab": "Slab With Insulation"}, tmp_path / "cache")

    assert normalised != path
    tc.validate_template(normalised)          # canonical names now present
    names = [c.nameString() for c in tc.load_model(normalised).getConstructions()]
    assert "Solera con aislante" in names
    assert "Slab With Insulation" not in names
    # the source template is left exactly as it was
    assert "Slab With Insulation" in [
        c.nameString() for c in tc.load_model(path).getConstructions()]


def test_normalising_is_content_addressed_and_runs_once(minimal_template, tmp_path):
    path = minimal_template(_build_minimal_model(
        rename={"ground_slab": "Slab With Insulation"}))
    aliases = {"ground_slab": "Slab With Insulation"}
    cache = tmp_path / "cache"

    first = tc.normalise_template(path, aliases, cache)
    stamp = first.stat().st_mtime_ns
    second = tc.normalise_template(path, aliases, cache)

    assert first == second
    assert second.stat().st_mtime_ns == stamp      # not rewritten


def test_a_different_alias_map_gets_a_different_cached_file(minimal_template, tmp_path):
    model = _build_minimal_model(rename={"ground_slab": "Slab With Insulation",
                                         "party_wall": "Party Ref B"})
    path = minimal_template(model)
    cache = tmp_path / "cache"
    one = tc.normalise_template(path, {"ground_slab": "Slab With Insulation"}, cache)
    two = tc.normalise_template(path, {"ground_slab": "Slab With Insulation",
                                       "party_wall": "Party Ref B"}, cache)
    assert one != two


def test_an_alias_pointing_at_a_missing_object_is_refused(minimal_template, tmp_path):
    path = minimal_template(_build_minimal_model())
    with pytest.raises(tc.TemplateError, match="not among"):
        tc.normalise_template(path, {"ground_slab": "No Such Construction"},
                              tmp_path / "cache")


def test_a_rename_that_would_collide_is_refused(minimal_template, tmp_path):
    """Renaming onto a name another object already holds would make the builder
    bind to whichever came first - so it is refused instead."""
    model = _build_minimal_model()
    extra = openstudio.model.Construction(model)
    extra.setName("Some Other Slab")
    path = minimal_template(model)

    with pytest.raises(tc.TemplateError, match="already uses that name"):
        tc.normalise_template(path, {"ground_slab": "Some Other Slab"},
                              tmp_path / "cache")


# ---------------------------------------------------------------------------
# TemplateSet
# ---------------------------------------------------------------------------
def test_template_set_from_a_plain_path(minimal_template, tmp_path):
    path = minimal_template(_build_minimal_model())
    template = tc.load_template_set(path, tmp_path / "cache")
    assert template.resolved_path == path
    assert template.normalised is False
    assert len(template.sha256) == 64


def test_template_set_from_a_json_spec_with_aliases(minimal_template, tmp_path):
    path = minimal_template(_build_minimal_model(
        rename={"ground_slab": "Slab With Insulation"}))
    spec = tmp_path / "template.json"
    spec.write_text(json.dumps({
        "name": "foreign",
        "template": path.name,
        "aliases": {"ground_slab": "Slab With Insulation"},
    }), encoding="utf-8")

    template = tc.load_template_set(spec, tmp_path / "cache")
    assert template.name == "foreign"
    assert template.normalised is True
    assert template.resolved_path != path
    tc.validate_template(template.resolved_path)


def test_template_fingerprint_is_stable_and_covers_the_alias_map(minimal_template,
                                                                 tmp_path):
    path = minimal_template(_build_minimal_model(
        rename={"ground_slab": "Slab With Insulation",
                "party_wall": "Party Ref B"}))
    cache = tmp_path / "cache"
    one_alias = {"ground_slab": "Slab With Insulation"}
    two_aliases = {**one_alias, "party_wall": "Party Ref B"}

    first = tc.load_template_set({"template": str(path), "aliases": two_aliases}, cache)
    again = tc.load_template_set({"template": str(path), "aliases": two_aliases}, cache)
    assert first.fingerprint == again.fingerprint

    # the same file bound differently is a different template as far as
    # provenance is concerned
    with pytest.raises(tc.TemplateError, match="party_wall"):
        tc.load_template_set({"template": str(path), "aliases": one_alias}, cache)


def test_record_survives_being_written_to_a_ledger(minimal_template, tmp_path):
    path = minimal_template(_build_minimal_model())
    record = tc.load_template_set(path, tmp_path / "cache").record()
    for key in ("fingerprint", "sha256", "normalised", "aliases", "source"):
        assert key in record
    assert json.dumps(record)


def test_a_missing_template_file_is_refused(tmp_path):
    with pytest.raises(tc.TemplateError, match="template not found"):
        tc.validate_template(tmp_path / "nope.osm")


# ---------------------------------------------------------------------------
# Library-only gate (review finding, 2026-08-03)
#
# The builder re-processes EVERY Space in the loaded model as one of its own
# storeys (it sorts osm.getSpaces() by z and types them), so a template that
# arrives with geometry or systems would have them silently absorbed into the
# building.  The contract now refuses such a template outright.
# ---------------------------------------------------------------------------
def test_a_template_smuggling_a_space_is_refused(minimal_template):
    model = _build_minimal_model()
    space = openstudio.model.Space(model)
    space.setName("pre-existing space")
    with pytest.raises(tc.TemplateError, match="library-only"):
        tc.validate_template(minimal_template(model))


def test_a_template_smuggling_systems_is_refused_with_counts(minimal_template):
    model = _build_minimal_model()
    openstudio.model.ThermalZone(model)
    openstudio.model.PlantLoop(model)
    openstudio.model.DesignDay(model)
    with pytest.raises(tc.TemplateError) as excinfo:
        tc.validate_template(minimal_template(model))
    message = str(excinfo.value)
    # every kind is named with its count so the author knows what to strip
    assert "ThermalZone x1" in message
    assert "PlantLoop x1" in message
    assert "DesignDay x1" in message


def test_a_clean_library_template_still_passes(minimal_template):
    report = tc.validate_template(minimal_template(_build_minimal_model()))
    assert not [item for item in report["missing"] if item["required"]]
