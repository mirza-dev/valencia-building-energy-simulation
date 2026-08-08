"""Files / Run / Outputs: the surface Rai and Javier actually touch.

These tests use the real Valencia dataset and the real finished runs on disk,
because the point of this surface is that the numbers it shows are the numbers
the engine produced - a mocked adapter would prove nothing about that.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench import db, integrity, stock_adapter
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


def test_ledger_page_is_bounded_searchable_and_filterable(client, has_finished_run):
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    page = client.get(
        f"/api/stock/runs/{FINISHED_RUN}/ledger",
        params={"status": "ok", "limit": 7},
    ).json()
    assert page["run"] == FINISHED_RUN
    assert page["total"] > 7
    assert len(page["items"]) == 7
    assert all(row["status"] == "ok" for row in page["items"])

    reference = page["items"][0]["refparcela"]
    found = client.get(
        f"/api/stock/runs/{FINISHED_RUN}/ledger", params={"q": reference}
    ).json()
    assert found["total"] == 1
    assert found["items"][0]["refparcela"] == reference


def test_finished_run_log_endpoint_is_safe_and_uncached(client, has_finished_run):
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    response = client.get(f"/api/stock/runs/{FINISHED_RUN}/log")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


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


def test_a_stopped_run_does_not_look_alive_and_block_its_own_resume():
    """A stopped run leaves a zombie, and `os.kill(pid, 0)` succeeds for those.

    Until this was fixed, every stopped stock run reported "run is already
    going" and could not be resumed until the service restarted - defeating the
    durable ledger it was stopped against.
    """
    pid = os.fork()
    if pid == 0:                      # child: exit at once, unreaped for now
        os._exit(0)
    deadline = time.time() + 5
    while time.time() < deadline and stock_adapter._process_state(pid) != "Z":
        time.sleep(0.05)

    assert stock_adapter.is_running(pid) is False


def test_a_live_process_is_still_reported_as_running():
    assert stock_adapter.is_running(os.getpid()) is True


def test_a_run_name_cannot_escape_the_stock_root():
    with pytest.raises(ValueError):
        stock_adapter.run_directory("../../etc")


def test_an_artifact_path_cannot_escape_the_run(has_finished_run):
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    with pytest.raises((ValueError, FileNotFoundError)):
        stock_adapter.artifact_path(FINISHED_RUN, "../../../etc", "eplustbl.htm")


def test_selected_package_is_complete_and_ed25519_signed(monkeypatch, tmp_path: Path):
    stock_root = tmp_path / "stock"
    run = stock_root / "sample"
    deep = run / "runs" / "REF1_deep"
    models = run / "models" / "Cluster"
    deep.mkdir(parents=True)
    models.mkdir(parents=True)
    (deep / "qa_report.txt").write_text("QA PASS", encoding="utf-8")
    model = models / "REF1.osm"
    model.write_text("OSM", encoding="utf-8")
    row = {"refparcela": "REF1", "status": "ok", "cluster": "Cluster",
           "model_osm": str(model)}
    (run / "ledger.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (run / "aggregate.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)
    monkeypatch.setattr(db, "VAR_DIR", tmp_path / "var")
    monkeypatch.setenv("WORKBENCH_EXPORT_ROOT", str(tmp_path / "exports"))

    plan = stock_adapter.package_plan("sample", ["REF1"])
    assert plan["scope"] == "selection"
    assert plan["signed"] is True
    package = stock_adapter.export_package("sample", ["REF1"])
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert {"selected_ledger.json", "export_manifest.json",
                "export_manifest.sig.json"} <= names
        assert any(name.endswith("qa_report.txt") for name in names)
        assert any(name.endswith("REF1.osm") for name in names)
        manifest = json.loads(archive.read("export_manifest.json"))
        signature = json.loads(archive.read("export_manifest.sig.json"))
    assert integrity.verify_signed_manifest(manifest, signature)
