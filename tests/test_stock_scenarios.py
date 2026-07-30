from __future__ import annotations

from pathlib import Path

import pytest

from workbench import stock_scenarios


def test_baseline_rejects_scientific_overrides(monkeypatch):
    monkeypatch.setattr(stock_scenarios, "_weather", lambda dataset_id: (
        {"id": dataset_id, "snapshot_hash": "weather"} if dataset_id else None
    ))
    baseline = stock_scenarios.validate_request({"mode": "baseline"}, scope="Benicalap")
    assert baseline["run_mode"] == "full_baseline"
    assert baseline["scenario"] is None

    with pytest.raises(ValueError, match="cannot contain scenario overrides"):
        stock_scenarios.validate_request(
            {"mode": "baseline", "heat_delta_c": 0.1}, scope="Benicalap",
        )
    with pytest.raises(ValueError, match="cannot contain scenario overrides"):
        stock_scenarios.validate_request(
            {"mode": "baseline", "weather_dataset_id": "epw"}, scope="Benicalap",
        )


def test_stock_scenario_validation_and_domain_kwargs(tmp_path, monkeypatch):
    weather = {
        "id": "future-epw", "name": "Future weather", "snapshot_hash": "abc123",
    }
    monkeypatch.setattr(
        stock_scenarios, "_weather",
        lambda dataset_id: weather if dataset_id == "future-epw" else None,
    )
    request = {
        "mode": "scenario",
        "name": "Comfort and climate",
        "heat_delta_c": 1.0,
        "cool_delta_c": -1.0,
        "weather_dataset_id": "future-epw",
        "reason": "Supervisor scenario",
        "source_type": "supervisor",
        "source_ref": "meeting-2026-07-16",
    }
    validated = stock_scenarios.validate_request(request, scope="VALENCIA")
    assert validated["run_mode"] == "full_scenario"
    assert validated["scenario"]["weather_snapshot_hash"] == "abc123"
    assert validated["scenario"]["fingerprint"]

    epw = tmp_path / "selected_weather" / "weather.epw"
    monkeypatch.setattr(
        stock_scenarios, "_materialize_snapshot",
        lambda _snapshot_hash, destination: destination / "weather.epw",
    )
    kwargs = stock_scenarios.domain_kwargs(validated, tmp_path)
    assert kwargs == {"heat_delta": 1.0, "cool_delta": -1.0, "epw": epw}


@pytest.mark.parametrize("value", [3.1, -3.1, float("nan"), float("inf")])
def test_stock_scenario_rejects_invalid_offsets(value):
    with pytest.raises(ValueError, match=r"between -3.0 and \+3.0 K"):
        stock_scenarios.validate_request(
            {
                "mode": "scenario", "name": "Invalid offset",
                "heat_delta_c": value, "reason": "Out of range",
                "source_type": "human_judgement",
            },
            scope="Benicalap",
        )


def test_stock_scenario_rejects_noop(monkeypatch):
    monkeypatch.setattr(stock_scenarios, "_weather", lambda _dataset_id: None)
    with pytest.raises(ValueError, match="must change an offset or the EPW"):
        stock_scenarios.validate_request(
            {
                "mode": "scenario", "name": "No change",
                "reason": "Control case", "source_type": "human_judgement",
            },
            scope="Benicalap",
        )
