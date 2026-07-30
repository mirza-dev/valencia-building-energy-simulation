"""The frozen verified model: profile integrity, drift guard, and the Rai gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import deep_building as db
import verified_model as vm


GOLDEN = json.loads(
    (Path(__file__).resolve().parent / "golden" / "verified_model_rai_reference.json")
    .read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The profile itself
# ---------------------------------------------------------------------------
def test_profile_is_intact_on_the_current_code():
    vm.assert_profile_intact()


def test_every_locked_parameter_actually_exists_in_deep_building():
    # guards against a typo silently locking nothing
    missing = [name for name in vm.LOCKED_PARAMETERS if not hasattr(db, name)]
    assert missing == []


def test_fingerprint_is_stable_and_matches_the_golden():
    assert vm.profile_fingerprint() == GOLDEN["profile_fingerprint"]
    assert vm.profile_fingerprint() == vm.profile_fingerprint()
    assert len(vm.profile_fingerprint()) == 64


def test_profile_record_carries_both_locked_and_live_source_hashes():
    record = vm.profile_record()
    assert record["profile_id"] == vm.VERIFIED_PROFILE_ID
    # a run record has to show what was verified AND what actually ran
    assert record["locked_source_hashes"] == vm.LOCKED_SOURCE_HASHES
    assert record["live_source_hashes"] == record["locked_source_hashes"]
    for digest in record["live_source_hashes"].values():
        assert len(digest) == 64


# ---------------------------------------------------------------------------
# The drift guard has to actually fire
# ---------------------------------------------------------------------------
def test_drift_guard_fires_when_a_locked_value_changes(monkeypatch):
    monkeypatch.setattr(db, "PTHP_HEATING_COP", 3.0)
    with pytest.raises(vm.ProfileDrift, match="PTHP_HEATING_COP"):
        vm.assert_profile_intact(check_sources=False)


def test_drift_guard_fires_on_a_locked_object_name(monkeypatch):
    monkeypatch.setattr(db, "GROUND_SLAB_CONSTRUCTION", "Solera sin aislante")
    with pytest.raises(vm.ProfileDrift, match="GROUND_SLAB_CONSTRUCTION"):
        vm.assert_profile_intact(check_sources=False)


def test_drift_guard_message_names_the_verification_source(monkeypatch):
    monkeypatch.setattr(db, "DHW_BOILER_EFFICIENCY", 0.85)
    with pytest.raises(vm.ProfileDrift) as excinfo:
        vm.assert_profile_intact(check_sources=False)
    assert vm.VERIFIED_AGAINST in str(excinfo.value)
    assert "0.85" in str(excinfo.value)


def test_simulate_verified_building_refuses_to_run_on_drift(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "PTHP_COOLING_COP", 2.5)
    with pytest.raises(vm.ProfileDrift):
        vm.simulate_verified_building("4252702YJ2745A", tmp_path)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def test_golden_records_a_passing_verification():
    assert GOLDEN["verification_passed"] is True
    assert all(gate["passed"] for gate in GOLDEN["gates"])
    assert GOLDEN["status"] == "LOCKED_VERIFIED_MODEL"


def test_score_verification_flags_a_value_outside_tolerance():
    metrics = dict(GOLDEN["expected_metrics"])
    metrics["wall_u_with_film"] = vm.RAI_REFERENCE["wall_u_with_film"] * 1.5
    gates = vm.score_verification(metrics, GOLDEN["expected_results"])
    wall = next(g for g in gates if g["gate"] == "wall_u_with_film")
    assert wall["passed"] is False


def test_score_verification_reports_a_missing_metric_instead_of_passing():
    metrics = dict(GOLDEN["expected_metrics"])
    metrics.pop("roof_u_with_film")
    gates = vm.score_verification(metrics, GOLDEN["expected_results"])
    roof = next(g for g in gates if g["gate"] == "roof_u_with_film")
    assert roof["passed"] is False
    assert roof["ours"] is None


# ---------------------------------------------------------------------------
# The real thing: rebuild Rai's building and re-score it
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_rai_reference_replica_still_reproduces_his_building(tmp_path):
    report = vm.run_rai_reference_replica(tmp_path / "replica")

    failed = [g["gate"] for g in report["gates"] if not g["passed"]]
    assert failed == [], f"verification gates failed: {failed}"
    assert report["verification_passed"] is True
    assert report["profile"]["fingerprint"] == GOLDEN["profile_fingerprint"]

    # and it must still land where the golden says, not merely inside tolerance
    for key, expected in GOLDEN["expected_results"].items():
        assert report["results"][key] == pytest.approx(expected, abs=0.05), key


# ---------------------------------------------------------------------------
# Source-hash lock (finding 3): values alone are not enough
# ---------------------------------------------------------------------------
def test_source_hashes_are_recorded_and_match_the_live_files():
    assert "PLACEHOLDER" not in vm.LOCKED_SOURCE_HASHES.values()
    for name, expected in vm.LOCKED_SOURCE_HASHES.items():
        assert vm._source_sha256(name) == expected, f"{name} drifted"


def test_source_hash_drift_is_rejected(monkeypatch):
    tampered = dict(vm.LOCKED_SOURCE_HASHES)
    tampered["deep_building.py"] = "0" * 64
    monkeypatch.setattr(vm, "LOCKED_SOURCE_HASHES", tampered)
    with pytest.raises(vm.ProfileDrift, match="deep_building.py"):
        vm.assert_profile_intact()


def test_fingerprint_covers_the_source_hashes(monkeypatch):
    # editing the layer logic must change the stamp even if every value holds
    before = vm.profile_fingerprint()
    tampered = dict(vm.LOCKED_SOURCE_HASHES)
    tampered["deep_building.py"] = "1" * 64
    monkeypatch.setattr(vm, "LOCKED_SOURCE_HASHES", tampered)
    assert vm.profile_fingerprint() != before


def test_golden_pins_the_same_source_hashes():
    assert GOLDEN["locked_source_hashes"] == vm.LOCKED_SOURCE_HASHES


# ---------------------------------------------------------------------------
# Replica vs production deltas (finding 2): the list must be true
# ---------------------------------------------------------------------------
def test_every_declared_delta_names_a_real_code_path():
    for delta in vm.REPLICA_VS_PRODUCTION_DELTAS:
        applied_by = delta["applied_by"]
        if applied_by.startswith("deep_building."):
            assert hasattr(db, applied_by.split(".", 1)[1]), applied_by
        assert isinstance(delta["in_production"], bool)
        assert delta["why"].strip()


def test_interzone_slab_is_the_only_replica_only_layer():
    replica_only = [d["item"] for d in vm.REPLICA_VS_PRODUCTION_DELTAS
                    if not d["in_production"]]
    assert replica_only == ["inter-storey slab"]


def test_shared_layers_are_applied_by_the_production_builder():
    import inspect
    source = inspect.getsource(db.build_deep_model)
    for layer in vm.SHARED_LAYERS:
        assert f"{layer}(" in source, f"{layer} is not in the production path"


def test_window_frames_are_now_shared_not_a_delta():
    # this was the gap Codex found: locked as verified but never applied
    assert "apply_window_frames" in vm.SHARED_LAYERS
    framed = [d for d in vm.REPLICA_VS_PRODUCTION_DELTAS if "frame" in d["item"]]
    assert framed == []
