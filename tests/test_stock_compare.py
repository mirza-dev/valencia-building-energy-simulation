from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from workbench import api as api_module
from workbench import stock_compare
from workbench.api import app


SNAPSHOTS = {
    "city_gis": "city", "boundary_gis": "boundary", "tipo15": "tipo15",
    "template": "template", "weather": "weather", "neighborhood_pipeline": "part-c",
    "city_pipeline": "part-d", "model_builder": "builder", "run_simulation": "simulation",
    "neighborhood_adapter": "n-adapter", "city_adapter": "c-adapter",
}


def _detail(kind: str, identifier: str, *, factor: float = 1.0) -> dict:
    city = kind == "city"
    settings = {
        "scope": "Valencia" if city else "Benicalap",
        "scope_mode": "city" if city else "boundary",
        "run_mode": "full_baseline" if factor == 1.0 else "full_scenario",
        "method": "representative_typology_period",
        "scenario": None if factor == 1.0 else {
            "name": "Comfort", "heat_delta_c": 1.0, "cool_delta_c": -1.0,
            "weather_snapshot_hash": None, "reason": "test", "source_type": "publication",
            "source_ref": "fixture", "fingerprint": "scenario",
        },
        "input_snapshot_hashes": SNAPSHOTS,
    }
    totals = {
        "heating_gwh_yr": 100.0 * factor,
        "cooling_gwh_yr": 80.0 * factor,
        "hvac_consumption_gwh_yr": 90.0 * factor,
        "total_site_gwh_yr": 200.0 * factor,
        "s1_co2_t_yr": 50.0 * factor,
        "s2_co2_t_yr": 30.0 * factor,
        "hvac_co2_t_yr": 40.0 * factor,
        "total_site_co2_t_yr": 110.0 * factor,
        "residential_area_m2": 1000.0,
    }
    cluster = {
        "cluster": "BlocPluriP04", "n_buildings": 10,
        "heating_kwh_m2": 10.0 * factor, "cooling_kwh_m2": 8.0 * factor,
        "cons_hc_kwh_m2": 9.0 * factor, "total_site_kwh_m2": 20.0 * factor,
        "s1_co2_kg_m2": 5.0 * factor, "s2_co2_kg_m2": 3.0 * factor,
        "hvac_co2_kg_m2": 4.0 * factor, "total_site_co2_kg_m2": 11.0 * factor,
    }
    district = {
        "nombre": "L'EIXAMPLE", "n_buildings": 10,
        "heating_gwh": 10.0 * factor, "cooling_gwh": 8.0 * factor,
        "cons_hc_gwh": 9.0 * factor, "total_site_gwh": 20.0 * factor,
        "s1_co2_t": 5.0 * factor, "s2_co2_t": 3.0 * factor,
        "hvac_co2_t": 4.0 * factor, "total_site_co2_t": 11.0 * factor,
    }
    summary = {
        "scope": settings["scope"], "buildings": 10, "clusters_expected": 1,
        "clusters_completed": 1, "clusters_failed": 0, "qa_passed_clusters": 1,
        "totals": totals,
    }
    if city:
        summary["districts"] = 1
    qa = {"all_pass": True, "scientific_status": "VALIDATED", "checks": [], "failures": []}
    result = {
        "settings": settings, "summary": summary, "qa": qa,
        "clusters": [cluster], "districts": [district] if city else [],
        "map_descriptor": {"fingerprint": "map", "bounds": [-0.4, 39.4, -0.3, 39.5]},
    }
    return {
        "id": identifier, "run_type": kind, "scenario_name": identifier,
        "created_at": "2026-07-16T12:00:00Z", "verification_status": "VERIFIED",
        "config": settings, "stats": summary, "qa": qa, "result": result,
        "verification": {"ok": True, "status": "VERIFIED", "issues": []},
    }


def test_neighborhood_compare_enables_percent_only_for_same_scientific_basis(monkeypatch):
    runs = {"base": _detail("neighborhood", "base"), "scenario": _detail("neighborhood", "scenario", factor=1.1)}
    monkeypatch.setattr(stock_compare, "neighborhood_detail", runs.__getitem__)
    result = stock_compare.compare_stock_runs("neighborhood", "base", "scenario")

    assert result["compatibility"]["interpretation_enabled"] is True
    assert result["compatibility"]["percent_enabled"] is True
    heating = next(item for item in result["metrics"] if item["key"] == "heating_gwh_yr")
    assert heating["delta"] == pytest.approx(10.0)
    assert heating["percent"] == pytest.approx(10.0)
    cluster = result["clusters"][0]["metrics"]["heating_kwh_m2"]
    assert cluster["delta"] == pytest.approx(1.0)
    assert result["districts"] == []


def test_stock_compare_keeps_absolute_values_but_disables_percent_for_different_weather(monkeypatch):
    base = _detail("city", "base")
    scenario = _detail("city", "scenario", factor=0.9)
    scenario["result"]["settings"]["scenario"]["weather_snapshot_hash"] = "future-weather"
    runs = {"base": base, "scenario": scenario}
    monkeypatch.setattr(stock_compare, "city_detail", runs.__getitem__)
    result = stock_compare.compare_stock_runs("city", "base", "scenario")

    assert result["compatibility"]["interpretation_enabled"] is True
    assert result["compatibility"]["percent_enabled"] is False
    assert "weather" in result["compatibility"]["reasons"]
    assert result["metrics"][0]["delta"] is not None
    assert result["metrics"][0]["percent"] is None
    assert result["districts"][0]["metrics"]["heating_gwh"]["percent"] is None


def test_stock_compare_blocks_energy_interpretation_for_tampered_evidence(monkeypatch):
    base = _detail("city", "base")
    tampered = deepcopy(_detail("city", "tampered", factor=1.1))
    tampered["verification_status"] = "TAMPERED"
    tampered["verification"] = {"ok": False, "status": "TAMPERED", "issues": ["results.json"]}
    runs = {"base": base, "tampered": tampered}
    monkeypatch.setattr(stock_compare, "city_detail", runs.__getitem__)
    result = stock_compare.compare_stock_runs("city", "base", "tampered")

    assert result["compatibility"]["interpretation_enabled"] is False
    assert result["compatibility"]["percent_enabled"] is False
    assert result["metrics"] == []
    assert result["clusters"] == []
    assert result["districts"] == []


def test_stock_compare_rejects_same_run_and_exposes_typed_api_routes(monkeypatch):
    with pytest.raises(ValueError, match="itself"):
        stock_compare.compare_stock_runs("city", "same", "same")

    calls = []
    monkeypatch.setattr(api_module, "compare_stock_runs", lambda kind, left, right: calls.append((kind, left, right)) or {"kind": kind})
    with TestClient(app) as client:
        neighborhood = client.get("/api/neighborhood/compare?left=a&right=b")
        city = client.get("/api/city/compare?left=c&right=d")
    assert neighborhood.status_code == 200
    assert city.status_code == 200
    assert calls == [("neighborhood", "a", "b"), ("city", "c", "d")]
