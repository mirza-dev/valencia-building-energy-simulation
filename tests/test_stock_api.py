"""Files / Run / Outputs: the surface Rai and Javier actually touch.

These tests use the real Valencia dataset and the real finished runs on disk,
because the point of this surface is that the numbers it shows are the numbers
the engine produced - a mocked adapter would prove nothing about that.
"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from workbench import stock_adapter
from workbench.api import app

BENICALAP = "BENICALAP"
FINISHED_RUN = "benicalap_v6"


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def has_finished_run():
    return any(item["run"] == FINISHED_RUN for item in stock_adapter.list_runs())


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def test_profile_names_the_model_this_installation_runs(client):
    """The header badge: which verified model, and is anything missing."""
    body = client.get("/api/stock/profile").json()
    assert body["profile"]["fingerprint"]
    assert body["missing_inputs"] == [], "default dataset is not resolvable"
    assert all(body["entrypoints"].values()), body["entrypoints"]


def test_districts_come_from_the_dataset(client):
    districts = client.get("/api/stock/districts").json()["districts"]
    assert BENICALAP in districts
    assert len(districts) > 10


# ---------------------------------------------------------------------------
# Run - preflight tells the truth before anything is spent
# ---------------------------------------------------------------------------
def test_preflight_reports_what_would_run_and_what_would_not(client):
    body = client.post("/api/stock/preflight",
                       json={"scope": "district", "district": BENICALAP}).json()
    assert body["ok"] is True
    # the engine's own screen: this is what benicalap_v6 actually ran
    assert body["runnable"] == 968
    assert body["excluded"] == 44
    assert sum(body["exclusion_reasons"].values()) == body["excluded"]
    assert body["estimated_minutes"] > 0
    assert body["estimated_bytes"] > 0


def test_preflight_estimate_follows_the_keep_mode(client):
    full = client.post("/api/stock/preflight",
                       json={"scope": "district", "district": BENICALAP,
                             "keep": "full"}).json()
    summary = client.post("/api/stock/preflight",
                          json={"scope": "district", "district": BENICALAP,
                                "keep": "summary"}).json()
    assert summary["estimated_bytes"] < full["estimated_bytes"]


@pytest.mark.parametrize("payload,reason", [
    ({"scope": "nonsense"}, "unknown scope"),
    ({"scope": "district"}, "needs a district"),
    ({"scope": "references"}, "needs at least one"),
])
def test_preflight_refuses_an_incoherent_request(client, payload, reason):
    response = client.post("/api/stock/preflight", json=payload)
    assert response.status_code == 422
    assert reason in response.json()["detail"]


def test_starting_a_run_without_a_name_is_refused(client):
    response = client.post("/api/stock/runs", json={"scope": "all"})
    assert response.status_code == 422
    assert "name" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
def test_runs_are_listed_newest_first(client):
    runs = client.get("/api/stock/runs").json()["runs"]
    assert runs, "no finished runs on disk"
    assert all("running" in item for item in runs)
    modified = [item["modified"] for item in runs]
    assert modified == sorted(modified, reverse=True)


def test_run_detail_carries_the_engines_own_totals(client, has_finished_run):
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    body = client.get(f"/api/stock/runs/{FINISHED_RUN}").json()
    assert body["progress"]["counts"]["ok"] > 0
    totals = body["summary"]["totals"]
    # both bases reported, so a reader never has to guess the denominator
    assert totals["area_weighted_total_site_kwh_m2"] > 0
    assert totals["cadastral_total_site_kwh_m2"] > 0
    assert totals["cadastral_total_site_kwh_m2"] > totals["area_weighted_total_site_kwh_m2"]


def test_unknown_run_is_a_404_not_an_empty_page(client):
    assert client.get("/api/stock/runs/no_such_run").status_code == 404


def test_ledger_csv_has_one_row_per_building(client, has_finished_run):
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    response = client.get(f"/api/stock/runs/{FINISHED_RUN}/ledger.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    # Parsed as CSV, not split on newlines: failure details are multi-line text
    # and are quoted, so a raw line count over-reports.
    rows = list(csv.DictReader(io.StringIO(response.text)))
    counts = client.get(f"/api/stock/runs/{FINISHED_RUN}").json()["progress"]["counts"]
    assert len(rows) == sum(counts.values())
    assert len({row["refparcela"] for row in rows}) == len(rows)


def test_energyplus_table_is_served_sandboxed(client, has_finished_run):
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    rows = stock_adapter.ledger_rows(FINISHED_RUN)
    reference = next(r["refparcela"] for r in rows if r.get("status") == "ok")
    response = client.get(
        f"/api/stock/runs/{FINISHED_RUN}/buildings/{reference}/eplustbl.htm")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    # third-party HTML: render it, run nothing
    assert "sandbox" in response.headers["content-security-policy"]


def test_only_allowlisted_artifacts_are_served(client, has_finished_run):
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    rows = stock_adapter.ledger_rows(FINISHED_RUN)
    reference = next(r["refparcela"] for r in rows if r.get("status") == "ok")
    response = client.get(
        f"/api/stock/runs/{FINISHED_RUN}/buildings/{reference}/eplusout.sql")
    assert response.status_code == 422


def test_a_run_name_cannot_escape_the_stock_root():
    with pytest.raises(ValueError):
        stock_adapter.run_directory("../../etc")


def test_an_artifact_path_cannot_escape_the_run(has_finished_run):
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    with pytest.raises((ValueError, FileNotFoundError)):
        stock_adapter.artifact_path(FINISHED_RUN, "../../../etc", "eplustbl.htm")
