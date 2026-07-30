from pathlib import Path

import pytest

from model_config import (
    BuildConfig,
    config_for_profile,
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
    assert profile.envelope.wall_u == 1.33
    assert profile.envelope.roof_u == 1.92
    assert profile.envelope.window_u == 5.70
    assert profile.geometry.ground_unconditioned is True
    assert base.envelope.roof_u is None


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
