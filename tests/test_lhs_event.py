"""The event uncertainty study: its contract, and what it must refuse to do."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import lhs_event_study as study  # noqa: E402
from workbench import lhs_event_adapter as adapter  # noqa: E402


# --------------------------------------------------------------------------
# The register
# --------------------------------------------------------------------------

class _Ctx:
    """The bits of an EventContext the register functions read."""

    envelope = {"wall_u": 1.2, "roof_u": 1.0, "window_u": 5.0}
    days = 8
    cluster = "AB_YBET:1946-1969"


def test_envelope_ranges_bracket_the_row_s_own_pinned_values():
    """A range built around the wrong centre would sample another archetype.

    The annual study's absolute bounds come from the Spanish IVE 1960-80 stock;
    an Italian building is sampled around its own pinned value instead.
    """
    variables = study.simulation_variables(_Ctx())
    for name, pinned in _Ctx.envelope.items():
        low, high = variables[name]
        assert low < pinned < high
        assert low == pytest.approx(pinned * 0.75)
        assert high == pytest.approx(pinned * 1.25)


def test_cop_and_seer_are_excluded_with_a_stated_reason():
    """Event runs report metered consumption, so a second COP would double-count."""
    variables = study.all_variables(_Ctx())
    assert "cop" not in variables and "seer" not in variables
    assert set(study.EXCLUDED_VARIABLES) == {"cop", "seer"}
    assert all(reason.strip() for reason in study.EXCLUDED_VARIABLES.values())


def test_every_variable_declares_whether_its_range_was_sourced_or_assumed():
    """An assumed range presented as a sourced one is a retraction waiting to happen."""
    catalog = study.variable_catalog(_Ctx())
    assert {entry["name"] for entry in catalog} == set(study.all_variables(_Ctx()))
    assert all(entry["source"] in {"sourced", "assumed"} for entry in catalog)
    assumed = {entry["name"] for entry in catalog if entry["source"] == "assumed"}
    assert assumed == {"wall_u", "roof_u", "window_u", "microclimate_delta_scale"}


def test_the_sample_matrix_is_stratified_and_reproducible():
    variables = study.all_variables(_Ctx())
    first = study.sample_matrix(20, 42, variables)
    second = study.sample_matrix(20, 42, variables)
    pd.testing.assert_frame_equal(first, second)
    assert list(first["sample"]) == list(range(1, 21))
    for name, (low, high) in variables.items():
        assert first[name].between(low, high).all()
        strata = ((first[name] - low) / (high - low) * 20).clip(upper=19).astype(int)
        assert set(strata) == set(range(20))


def test_outputs_are_event_outputs_not_the_annual_study_s():
    """Heating is structurally zero over an August event; total site takes its place."""
    assert study.OUTPUT_COLUMNS == ("total_site_kwh_m2", "cooling_kwh_m2", "co2_kg_m2")
    assert adapter.OUTPUT_COLUMNS == study.OUTPUT_COLUMNS
    assert "heating_kwh_m2" not in study.OUTPUT_COLUMNS


def test_a_constant_output_is_reported_with_its_own_reason():
    """Two constants, two different causes; explaining one with the other's is wrong."""
    frame = pd.DataFrame({
        "space_heating_kwh_m2": [0.0] * 5,
        "site_gas_kwh_m2": [0.09] * 5,
        "cooling_kwh_m2": [0.4, 0.41, 0.42, 0.43, 0.44],
    })
    constants = study.constant_outputs(frame)
    assert constants == {"space_heating_kwh_m2": 0.0, "site_gas_kwh_m2": 0.09}
    assert "cooling_kwh_m2" not in constants
    assert "August" in study.CONSTANT_REASONS["space_heating_kwh_m2"]
    assert "hot water" in study.CONSTANT_REASONS["site_gas_kwh_m2"]


# --------------------------------------------------------------------------
# What the study refuses
# --------------------------------------------------------------------------

def test_a_same_named_file_is_refused_when_its_fingerprint_differs(tmp_path):
    """Three of the run's inputs are recorded by fingerprint and not by path.

    For all three -- slice, climate bundle, template -- a file of the same name
    that is *not* the one the run used exists on this machine, so resolving by
    name would silently substitute it.
    """
    class _Loaded:
        fingerprint = "aaaa"

    with pytest.raises(study.EventStudyError, match="refusing to substitute"):
        study._resolve_by_fingerprint([tmp_path / "x"], lambda _p: _Loaded(), "bbbb", "template")

    found, loaded = study._resolve_by_fingerprint(
        [tmp_path / "x"], lambda _p: _Loaded(), "aaaa", "template")
    assert found == tmp_path / "x" and loaded.fingerprint == "aaaa"


def test_an_annual_run_cannot_host_an_event_study(tmp_path):
    run = tmp_path / "ANNUAL"
    run.mkdir()
    (run / "run_config.json").write_text(json.dumps({"worker_config": {"prepared_gis": "x"}}))
    with pytest.raises(study.EventStudyError, match="not a microclimate event run"):
        study.load_context(run, "whatever")


def test_a_directory_without_a_run_config_is_refused(tmp_path):
    with pytest.raises(study.EventStudyError, match="not a stock run directory"):
        study.load_context(tmp_path, "whatever")


# --------------------------------------------------------------------------
# Isolation from the annual study -- the safety argument for the whole design
# --------------------------------------------------------------------------

def test_the_event_adapter_does_not_hash_the_annual_adapter():
    """`lhs_adapter.source_paths()` lists its own file.

    One added line there would change its hash, flip every committed Valencia
    study to `current_compatibility.current == false` and replace the published
    band with an OUTDATED banner.  The event study therefore lives in its own
    modules and pins its own sources.
    """
    roles = adapter.source_paths()
    names = {path.name for path, _kind in roles.values()}
    assert "lhs_adapter.py" not in names
    assert "lhs_study.py" not in names
    assert "lhs_event_adapter.py" in names and "lhs_event_study.py" in names


def test_the_annual_study_module_is_untouched_by_this_feature():
    import lhs_study

    assert set(lhs_study.POST_VARS) == {"cop", "seer", "emission_factor"}
    assert lhs_study.DEFAULT_N == 50 and lhs_study.DEFAULT_SEED == 42


def test_event_runs_commit_under_their_own_run_type():
    from workbench import lhs_event_service

    assert lhs_event_service.RUN_TYPE == "lhs_event"
    assert lhs_event_service.JOB_KIND == "lhs_event"


def test_the_worker_dispatches_the_event_kind_explicitly():
    """An unknown job kind falls through to `run_build_job`, which would be wrong."""
    source = (SRC / "workbench" / "worker.py").read_text()
    assert 'elif job["kind"] == "lhs_event":' in source
    assert "run_event_lhs_job" in source


def test_storage_reserves_space_for_the_event_kind():
    from workbench import storage

    assert storage.JOB_ESTIMATES["lhs_event"] > 0
    assert "lhs_event" in storage.HEAVY_JOB_KINDS


# --------------------------------------------------------------------------
# QA
# --------------------------------------------------------------------------

def _results(n: int = 12, *, constant_total: bool = False) -> pd.DataFrame:
    variables = study.all_variables(_Ctx())
    frame = study.sample_matrix(n, 42, variables)
    frame["total_site_kwh_m2"] = 1.0 if constant_total else [1.0 + i * 0.01 for i in range(n)]
    frame["cooling_kwh_m2"] = [0.4 + i * 0.01 for i in range(n)]
    frame["co2_kg_m2"] = [0.3 + i * 0.01 for i in range(n)]
    frame["qa_passed"] = True
    frame["event_epw"] = [f"event_{i}.epw" for i in range(n)]
    return frame


def test_qa_refuses_a_band_around_an_output_that_never_moved(tmp_path):
    """A constant column has no rank correlation and no spread to publish."""
    variables = study.all_variables(_Ctx())
    samples = study.sample_matrix(12, 42, variables)
    sensitivity = {name: [{"variable": "wall_u", "rho": 0.1, "rank": i} for i in range(1, 4)]
                   for name in study.OUTPUT_COLUMNS}
    qa = adapter._qa(study, _Ctx(), samples, _results(constant_total=True),
                     sensitivity, tmp_path, 12)
    check = next(item for item in qa["checks"] if item["name"] == "outputs_vary")
    assert check["passed"] is False
    assert qa["all_pass"] is False and qa["scientific_status"] == "UNVERIFIED"


def test_qa_flags_a_study_whose_samples_all_shared_one_weather_file(tmp_path):
    """If the offsets quantise onto one EPW the microclimate term was never sampled."""
    variables = study.all_variables(_Ctx())
    samples = study.sample_matrix(12, 42, variables)
    frame = _results()
    frame["event_epw"] = "event_same.epw"
    sensitivity = {name: [{"variable": "wall_u", "rho": 0.1, "rank": i} for i in range(1, 4)]
                   for name in study.OUTPUT_COLUMNS}
    qa = adapter._qa(study, _Ctx(), samples, frame, sensitivity, tmp_path, 12)
    check = next(item for item in qa["checks"] if item["name"] == "distinct_event_weather")
    assert check["passed"] is False


def test_qa_flags_a_sample_that_failed_its_own_building_qa(tmp_path):
    variables = study.all_variables(_Ctx())
    samples = study.sample_matrix(12, 42, variables)
    frame = _results()
    frame.loc[3, "qa_passed"] = False
    sensitivity = {name: [{"variable": "wall_u", "rho": 0.1, "rank": i} for i in range(1, 4)]
                   for name in study.OUTPUT_COLUMNS}
    qa = adapter._qa(study, _Ctx(), samples, frame, sensitivity, tmp_path, 12)
    check = next(item for item in qa["checks"] if item["name"] == "per_sample_qa")
    assert check["passed"] is False


def test_a_failed_sample_stops_the_study_instead_of_shrinking_it(tmp_path, monkeypatch):
    """Dropping a failed sample would quietly narrow the published interval."""
    monkeypatch.setattr(study, "_worker_init", lambda *_args: None)
    monkeypatch.setattr(study, "_run_sample_task",
                        lambda args: {"sample": 1, "error": "RuntimeError: boom"})
    ctx = study.EventContext(
        run_dir=tmp_path, refparcela="X", gis_path=tmp_path / "g.gpkg",
        climate_path=tmp_path / "c.json", template_path=tmp_path / "t.osm",
        slice_dir=tmp_path, climate_fingerprint="a", template_fingerprint="b",
        slice_record={"fingerprint": "c"}, window=(8, 16, 8, 23, 822), days=8,
        zero_policy="literal_zero", delta_peak_k=1.0, delta_base_k=0.5,
        sample_radius_m=0.0, sample_cells=4, envelope=dict(_Ctx.envelope),
        floor_u=0.9, cluster="X",
    )
    samples = study.sample_matrix(2, 42, study.all_variables(ctx))
    with pytest.raises(study.EventStudyError, match="samples failed"):
        study.run_study(ctx, samples, tmp_path / "out", workers=1)


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

def test_the_period_label_never_claims_a_year(tmp_path):
    frame = _results()
    drivers = {name: [("wall_u", 0.1)] for name in study.OUTPUT_COLUMNS}
    ctx = study.EventContext(
        run_dir=tmp_path, refparcela="X", gis_path=tmp_path, climate_path=tmp_path,
        template_path=tmp_path, slice_dir=tmp_path, climate_fingerprint="a" * 32,
        template_fingerprint="b" * 32, slice_record={"fingerprint": "c" * 32},
        window=(8, 16, 8, 23, 822), days=8, zero_policy="literal_zero",
        delta_peak_k=1.9, delta_base_k=0.8, sample_radius_m=0.0, sample_cells=4,
        envelope=dict(_Ctx.envelope), floor_u=0.9, cluster="X",
    )
    text = study.summarize(ctx, frame, drivers, tmp_path)
    assert "/yr" not in text and "per year" not in text
    assert "8-day" in text or "8 days" in text
