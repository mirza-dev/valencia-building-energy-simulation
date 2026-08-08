from pathlib import Path

import pytest

from model_config import (
    BuildConfig,
    RAI_ENVELOPE,
    TABULA_ES,
    config_for_profile,
    envelope_table,
    validate_override_provenance,
)


def test_default_config_preserves_legacy_params():
    config = BuildConfig.for_project(Path("/tmp/project"))
    assert config.to_legacy_params() == {
        "wall_u": None,
        "roof_u": None,
        "window_u": 5.7,
        "window_g": 0.82,
        "infiltration_ach": None,
        "shade_setpoint": 250.0,
        "context_shading": True,
        "thermal_bridge_du": 0.10,
        "massless": False,
        "ground_unconditioned": True,
    }


def test_unknown_legacy_param_is_rejected():
    config = BuildConfig.for_project(Path("/tmp/project"))
    with pytest.raises(ValueError, match="Unknown parameter"):
        config.with_legacy_params({"window_typo": 1.0})


def test_tabula_profile_is_suggested_without_mutating_baseline():
    base = BuildConfig.for_project(Path("/tmp/project"))
    profile = config_for_profile(base, "tabula_BlocPluriP04")
    # The numbers themselves belong to whichever envelope is in force; what this
    # test protects is that a profile is applied and the baseline is not touched.
    expected = envelope_table()[("BlocPluri", "P04")]
    assert profile.envelope.wall_u == expected["wall_u"]
    assert profile.envelope.roof_u == expected["roof_u"]
    assert profile.envelope.window_u == expected["window_u"]
    assert profile.geometry.ground_unconditioned is True
    assert base.envelope.roof_u is None


def test_the_two_envelope_sources_stay_separable():
    """`ive` is this project's IVE brochure derivation, `rai` is what Rai
    modelled.  Both stay reachable so a published number can be reproduced on
    the envelope that produced it rather than argued about."""
    assert envelope_table("ive") is TABULA_ES
    assert envelope_table("rai") is RAI_ENVELOPE
    assert TABULA_ES[("BlocPluri", "P04")] == dict(wall_u=1.33, roof_u=1.92, window_u=5.70)
    assert RAI_ENVELOPE[("BlocPluri", "P04")] == dict(wall_u=1.369, roof_u=2.479, window_u=5.7)
    with pytest.raises(KeyError, match="unknown envelope source"):
        envelope_table("whatever")


def test_rais_library_gaps_fall_back_to_the_ive_value():
    """Four fields have no counterpart in his 41 models and must not be guessed.

    Two are missing outright (no VivUniAdoP05 facade or roof, no VivUniAisP06
    roof or window) and one is a label slip we refuse to follow (the P03 file
    assigns the *P04* pitched roof).
    """
    assert RAI_ENVELOPE[("EdiPluri", "P03")]["roof_u"] == TABULA_ES[("EdiPluri", "P03")]["roof_u"]
    assert RAI_ENVELOPE[("EdiPluri", "P05")]["wall_u"] == TABULA_ES[("EdiPluri", "P05")]["wall_u"]
    assert RAI_ENVELOPE[("EdiPluri", "P05")]["roof_u"] == TABULA_ES[("EdiPluri", "P05")]["roof_u"]
    assert RAI_ENVELOPE[("VivUni", "P06")]["roof_u"] == TABULA_ES[("VivUni", "P06")]["roof_u"]
    assert RAI_ENVELOPE[("VivUni", "P06")]["window_u"] == TABULA_ES[("VivUni", "P06")]["window_u"]
    # ...but the fields he does have are his, not ours.
    assert RAI_ENVELOPE[("EdiPluri", "P05")]["window_u"] != TABULA_ES[("EdiPluri", "P05")]["window_u"]
    assert RAI_ENVELOPE[("VivUni", "P06")]["wall_u"] != TABULA_ES[("VivUni", "P06")]["wall_u"]


def test_every_cluster_the_stock_can_carry_resolves():
    base = BuildConfig.for_project(Path("/tmp/project"))
    for family in ("VivUni", "EdiPluri", "BlocPluri"):
        for period in ("P01", "P02", "P03", "P04", "P05", "P06", "P07"):
            profile = config_for_profile(base, f"tabula_{family}{period}")
            assert profile.envelope.wall_u is not None, f"{family}{period}"
            assert profile.envelope.roof_u is not None, f"{family}{period}"
            assert profile.envelope.window_u is not None, f"{family}{period}"


def test_changed_field_requires_per_field_provenance():
    base = BuildConfig.for_project(Path("/tmp/project"))
    changed = base.model_copy(deep=True)
    changed.envelope.window_g = 0.75
    assert validate_override_provenance(changed, base) == ["envelope.window_g"]
    changed.provenance.overrides = [{
        "field": "envelope.window_g",
        "reason": "TABULA clear double glazing assumption",
        "source_type": "publication",
        "source_ref": "TABULA España",
    }]
    assert validate_override_provenance(changed, base) == []
