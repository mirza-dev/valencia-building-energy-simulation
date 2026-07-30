from __future__ import annotations

import hashlib
from pathlib import Path

from workbench import capability_sync


def _part_c_source(path: Path) -> None:
    path.write_text(
        """
def load_stock(district=None):
    pass

def run_representative(stock, representative, run_dir, scenario=None):
    cons_hc_kwh_m2 = 0
    total_site_kwh_m2 = 0

def run_single_building():
    pass

def cli():
    parser.add_argument('--district')
    parser.add_argument('--building')
    parser.add_argument('--heat-delta')
    parser.add_argument('--cool-delta')
    parser.add_argument('--epw')
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_neighborhood_diagnostics_expose_source_drift_and_new_features(tmp_path, monkeypatch):
    runner = tmp_path / "neighborhood_pipeline.py"
    _part_c_source(runner)
    current_hash = hashlib.sha256(runner.read_bytes()).hexdigest()
    monkeypatch.setattr(capability_sync, "_latest_verified_run", lambda _name: {
        "run_id": "verified-run", "source_sha256": "old-hash",
    })

    contract = {
        "runner": str(runner),
        "expected_runner_sha256": "old-hash",
        "checks": {
            "fixture_schema": True,
            "adapter_signature": True,
            "adapter_hash": False,
            "runner_production": True,
            "runner_hash": False,
            "runner_functions": True,
            "simulation_dependency": True,
            "golden_smoke": True,
        },
    }
    diagnostic = capability_sync.diagnostics(
        "neighborhood", {"ready": True}, {"ok": True, "sha256": "adapter"},
        contract, False,
    )

    assert diagnostic["state"] == "source_changed"
    assert diagnostic["source"]["current_sha256"] == current_hash
    assert diagnostic["latest_verified_evidence"]["run_id"] == "verified-run"
    features = {item["key"]: item["available"] for item in diagnostic["features"]}
    assert features == {
        "district_scope": True,
        "single_building": True,
        "comfort_scenario": True,
        "weather_scenario": True,
        "real_hvac_consumption": True,
    }

    capability = {
        "declared_ready": True,
        "inspection": {"ok": True},
        "contract": contract,
        "diagnostic": diagnostic,
    }
    plan = capability_sync.revalidation_plan("neighborhood", capability)
    assert plan["eligible"] is True
    assert plan["plan_token"]
    assert plan["steps"][-1] == "hash_promotion"


def test_accepted_fixture_wins_over_an_older_production_ledger(tmp_path, monkeypatch):
    runner = tmp_path / "neighborhood_pipeline.py"
    _part_c_source(runner)
    current_hash = hashlib.sha256(runner.read_bytes()).hexdigest()
    fixture = tmp_path / "neighborhood_smoke.json"
    fixture.write_text(
        '{"runner_sha256":"%s","verified_at":"2026-07-22T10:00:00+00:00",'
        '"verification_run_id":"isolated-revalidation"}' % current_hash,
        encoding="utf-8",
    )
    monkeypatch.setattr(capability_sync, "_latest_verified_run", lambda _name: {
        "run_id": "older-production-run",
        "verified_at": "2026-07-17T08:00:00+00:00",
        "source_sha256": "old-hash",
    })
    contract = {
        "fixture": str(fixture),
        "runner": str(runner),
        "expected_runner_sha256": current_hash,
        "checks": {"runner_hash": True, "golden_smoke": True},
    }
    diagnostic = capability_sync.diagnostics(
        "neighborhood", {"ready": True}, {"ok": True}, contract, True,
    )

    assert diagnostic["state"] == "verified"
    assert diagnostic["reason"] is None
    assert diagnostic["latest_verified_evidence"]["run_id"] == "isolated-revalidation"
    assert diagnostic["latest_verified_evidence"]["evidence_source"] == "accepted_fixture"


def test_golden_comparison_requires_scientific_qa():
    expected = {
        "buildings": 959,
        "clusters": 18,
        "qa_passed_clusters": 18,
        "heating_gwh_yr": 21.04,
        "cooling_gwh_yr": 25.58,
        "absolute_tolerance_gwh": 0.05,
    }
    result = {
        "summary": {
            "buildings": 959,
            "clusters_completed": 18,
            "qa_passed_clusters": 18,
            "totals": {"heating_gwh_yr": 21.06, "cooling_gwh_yr": 25.56},
        },
        "qa": {"all_pass": True},
    }
    assert capability_sync.compare_golden("neighborhood", result, expected)["passed"] is True

    result["qa"]["all_pass"] = False
    report = capability_sync.compare_golden("neighborhood", result, expected)
    assert report["passed"] is False
    assert report["status"] == "REVIEW_REQUIRED"


def test_dependency_drift_points_to_the_changed_builder(tmp_path, monkeypatch):
    runner = tmp_path / "lhs_study.py"
    runner.write_text("def sample_matrix():\n    pass\n", encoding="utf-8")
    builder = tmp_path / "model_builder.py"
    builder.write_text("def build_model():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(capability_sync, "_latest_verified_run", lambda _name: None)
    contract = {
        "runner": str(runner),
        "expected_runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
        "builder": str(builder),
        "expected_builder_sha256": "old-builder-hash",
        "checks": {"runner_hash": True, "builder_hash": False},
    }
    diagnostic = capability_sync.diagnostics(
        "lhs", {"ready": True}, {"ok": True}, contract, False,
    )
    assert diagnostic["state"] == "source_changed"
    assert diagnostic["source"]["path"] == str(builder)
    assert diagnostic["source"]["expected_sha256"] == "old-builder-hash"
