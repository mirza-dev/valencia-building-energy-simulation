"""Files / Run / Outputs: the surface Rai and Javier actually touch.

These tests use the real Valencia dataset and the real finished runs on disk,
because the point of this surface is that the numbers it shows are the numbers
the engine produced - a mocked adapter would prove nothing about that.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
import types
import zipfile
from pathlib import Path
from unittest import mock

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
    # The engine's own screen.  968 / 44 was what benicalap_v6 actually ran; the
    # 2026-08-22 gate widening moved it to 1008 / 4; and stating the shape
    # fidelity the simplification must achieve - instead of fixing its tolerance
    # and accepting whatever signed area change came out - moved it to 1010 / 2.
    # None of the recovered buildings was ever unmodellable.  They were turned
    # away by `simplify_tolerance_m`, by `footprint_min_m2`, and by an area test
    # that cancels: a finer tolerance could fail it while being geometrically
    # closer to the cadastral polygon.
    assert body["runnable"] == 1010
    assert body["excluded"] == 2
    # The two survivors are genuine and deliberately out of scope: one cadastral
    # reference drawn as two rows, and one polygon with interior rings.
    assert set(body["exclusion_reasons"]) == {"duplicate_refparcela_2_rows",
                                              "interior_rings_2"}
    # Deliberately redundant against the line above, because it is the claim that
    # GENERALISES and must not be lost if that set is ever edited for this one
    # district: simplification can no longer reject a building in any city.  The
    # ladder refines to 1 cm and falls back to the raw polygon, so this reason is
    # unreachable now rather than merely rare.
    assert "simplification_area_change_over_limit" not in body["exclusion_reasons"]
    # what matters more than any count above: the screen still accounts for
    # everyone it was handed, so nobody can be dropped silently
    assert body["runnable"] + body["excluded"] == 1012
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


def _minimal_ok_row(reference: str, **extra) -> dict:
    row = {"refparcela": reference, "status": "ok", "seconds": 1.5,
           "res_area_m2": 1000.0, "space_heating_kwh_m2": 5.0,
           "cooling_kwh_m2": 4.0, "dhw_kwh_m2": 10.0,
           "total_site_kwh_m2": 50.0, "total_site_co2_kg_m2": 15.0}
    row.update(extra)
    return row


def test_a_stock_without_a_cadastre_omits_the_cadastral_totals():
    """Absent, not zero.

    Both cadastral figures come off the Spanish Tipo15 record.  A city that has
    no such record has no cadastral dwelling area at all, and writing 0.0 there
    would read as "we measured it and it was nothing" - the same NULL-is-not-
    zero rule the ledger and the coverage blocks already follow.  The interface
    types these fields as optional on the strength of this contract, and gates a
    KPI card on their presence, so a change that started emitting 0.0 would put
    an empty Valencia-shaped card back on every other city.
    """
    import stock_runner as sr

    spanish = sr.aggregate([_minimal_ok_row("A1", tipo15_res_area_m2=900.0),
                            _minimal_ok_row("A2", tipo15_res_area_m2=800.0)])
    assert spanish["totals"]["tipo15_residential_area_m2"] == 1700.0
    assert spanish["totals"]["cadastral_total_site_kwh_m2"] > 0

    foreign = sr.aggregate([_minimal_ok_row("B1"), _minimal_ok_row("B2")])
    assert "cadastral_total_site_kwh_m2" not in foreign["totals"]
    assert "tipo15_residential_area_m2" not in foreign["totals"]
    # the geometric basis is unaffected: what is missing is a second denominator
    assert foreign["totals"]["area_weighted_total_site_kwh_m2"] == 50.0


def test_a_cadastral_column_that_is_entirely_blank_is_also_absent():
    """A column of nulls is a column that measured nothing, not a zero area."""
    import stock_runner as sr

    blank = sr.aggregate([_minimal_ok_row("C1", tipo15_res_area_m2=None),
                          _minimal_ok_row("C2", tipo15_res_area_m2=None)])
    assert "cadastral_total_site_kwh_m2" not in blank["totals"]
    assert "tipo15_residential_area_m2" not in blank["totals"]


def test_finished_run_reports_its_totals_as_final(client, has_finished_run):
    """A written aggregate is a final total, and says so."""
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    body = client.get(f"/api/stock/runs/{FINISHED_RUN}").json()
    assert body["summary_is_partial"] is False
    assert stock_adapter.summary_is_partial(FINISHED_RUN) is False


def test_a_run_totalled_from_a_partial_ledger_is_flagged(tmp_path, monkeypatch,
                                                         has_finished_run):
    """The defect this guards: `summary()` recomputes from a partial ledger and
    returns the same field names a finished run uses, so a caller that only
    checks "is there a summary" prints a fraction of the stock under a headline
    claiming all of it."""
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    source = stock_adapter.run_directory(FINISHED_RUN)
    partial = tmp_path / "partial_run"
    partial.mkdir()
    rows = (source / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    (partial / "ledger.jsonl").write_text("\n".join(rows[:20]) + "\n",
                                          encoding="utf-8")
    # no aggregate.json: exactly the mid-run state
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path)
    assert stock_adapter.summary_is_partial("partial_run") is True
    # and it still totals, so the flag is the only thing telling them apart
    assert stock_adapter.summary("partial_run")["buildings_ok"] >= 0


def _publication_heatmap_fixture(tmp_path, monkeypatch, name="PUBLISHED_RUN"):
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path / "stock")
    monkeypatch.setattr(stock_adapter, "PUBLICATION_RESULTS_ROOT",
                        tmp_path / "published")
    run = stock_adapter.STOCK_ROOT / name
    run.mkdir(parents=True)
    (run / "ledger.jsonl").write_text(
        json.dumps(_minimal_ok_row("ONE")) + "\n", encoding="utf-8")
    (run / "aggregate.json").write_text(json.dumps({
        "buildings_ok": 1,
        "results_layer": {"written": True, "heatmap": {
            "written": True, "image": "results_heatmap.png",
            "panels": ["total_site_kwh_m2"],
        }},
    }), encoding="utf-8")
    published = stock_adapter.PUBLICATION_RESULTS_ROOT / name
    published.mkdir(parents=True)
    image = published / "results_heatmap.png"
    image.write_bytes(b"publication-png")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (published / "results_heatmap.json").write_text(json.dumps({
        "schema": "bsew-heatmap-evidence-v1", "run": name,
        "unit": "kWh/m²·yr", "denominator": "named denominator",
        "panels": {"total_site_kwh_m2": {"valid": 1, "missing": 0}},
        "status_classes": {"successful": 1, "excluded": 0,
                           "failed": 0, "metric_missing": 0},
        "geography": {"outside_frame": 0, "views": [
            {"id": "main", "buildings": 1}]},
        "crs": "EPSG:25830", "profile_fingerprint": "abc",
        "png": {"file": image.name, "sha256": digest},
    }), encoding="utf-8")
    return name, image


def test_settled_run_prefers_verified_publication_heatmap(tmp_path, monkeypatch):
    name, image = _publication_heatmap_fixture(tmp_path, monkeypatch)

    assert stock_adapter.results_heatmap(name) == image
    heatmap = stock_adapter.summary(name)["results_layer"]["heatmap"]
    assert heatmap["publication_derivative"] is True
    assert heatmap["outside_frame"] == 0
    assert heatmap["sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()


def test_heatmap_endpoint_refuses_a_running_or_partial_run(client, tmp_path,
                                                           monkeypatch):
    name, _ = _publication_heatmap_fixture(tmp_path, monkeypatch, "ACTIVE_MAP")
    monkeypatch.setattr(stock_adapter, "active_process",
                        lambda candidate: object() if candidate == name else None)

    response = client.get(f"/api/stock/runs/{name}/results.png")

    assert response.status_code == 409
    assert "has not settled" in response.json()["detail"]


def test_scope_size_comes_from_the_runs_own_config(tmp_path, monkeypatch):
    """The honest denominator while rows are still arriving."""
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path)
    run = tmp_path / "scoped"
    run.mkdir()
    (run / "run_config.json").write_text(
        json.dumps({"scope": "all", "runnable": 873, "excluded": 162}),
        encoding="utf-8")
    assert stock_adapter.scope_size("scoped") == {
        "runnable": 873, "excluded": 162, "total": 1035}


@pytest.mark.parametrize("payload", [
    None,                                   # no run_config.json at all
    "{ not json",                           # unreadable
    json.dumps({"scope": "all"}),           # older run, fields absent
    json.dumps({"runnable": -1, "excluded": 3}),
])
def test_scope_size_reports_unknown_rather_than_guessing(tmp_path, monkeypatch,
                                                         payload):
    """An unrecorded scope must read as unknown, never as a count: a guessed
    denominator is what made the progress bar claim 100% from the first row."""
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path)
    run = tmp_path / "unscoped"
    run.mkdir()
    if payload is not None:
        (run / "run_config.json").write_text(payload, encoding="utf-8")
    assert stock_adapter.scope_size("unscoped") is None


def _finished_run(root, name, *, scope, mean, stamp):
    """The two files `_seconds_per_building` reads, and nothing else."""
    run = root / name
    run.mkdir(parents=True)
    (run / "run_config.json").write_text(json.dumps({"scope": scope}),
                                         encoding="utf-8")
    summary = run / "aggregate.json"
    summary.write_text(json.dumps({
        "seconds_per_building": {"mean": mean, "median": mean, "max": mean},
        "energy_period": {"period": "annual", "unit": "kWh/m2-yr"},
    }), encoding="utf-8")
    os.utime(summary, (stamp, stamp))
    return run


def test_the_duration_rate_ignores_a_hand_picked_repair_run(tmp_path, monkeypatch):
    """A retry of the buildings the last run could not finish is a repair of it,
    not a sample of the next one.  Measured on ALL-VALENC-A_unfinished, which
    retried 1 406 buildings: 210.2 s against the city's own 96.8 s, which would have quoted 10.7 days for a run
    the city's ledger says takes 4.9.  Newest still wins - among comparable runs.
    """
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path)
    _finished_run(tmp_path, "city", scope="all", mean=96.8, stamp=1_000)
    _finished_run(tmp_path, "city_unfinished", scope="references", mean=210.2,
                  stamp=9_000)                      # newer, and the wrong shape
    rate, basis = stock_adapter._seconds_per_building()
    assert rate == pytest.approx(96.8)
    assert "city" in basis and "unfinished" not in basis


def test_the_duration_rate_still_takes_the_newest_comparable_run(tmp_path,
                                                                 monkeypatch):
    """The scope guard must narrow the field, not freeze it: engine changes move
    the time per building, so a newer whole-scope run must still win."""
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path)
    _finished_run(tmp_path, "older_district", scope="district", mean=143.3,
                  stamp=1_000)
    _finished_run(tmp_path, "newer_district", scope="district", mean=56.3,
                  stamp=9_000)
    rate, basis = stock_adapter._seconds_per_building()
    assert rate == pytest.approx(56.3)
    assert basis == "measured on newer_district"


def test_a_run_that_never_recorded_its_scope_still_counts(tmp_path, monkeypatch):
    """An absent `run_config.json` predates the field, and every run that old
    covered a whole scope.  Dropping those would leave a fresh installation with
    no measured rate at all, which is the constant this function exists to
    replace."""
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path)
    run = _finished_run(tmp_path, "ancient", scope="all", mean=101.9, stamp=1_000)
    (run / "run_config.json").unlink()
    rate, basis = stock_adapter._seconds_per_building()
    assert rate == pytest.approx(101.9)
    assert basis == "measured on ancient"


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


def test_historical_ledger_rows_are_enriched_from_the_exact_prepared_stock(tmp_path, monkeypatch):
    run = tmp_path / "historical"
    run.mkdir()
    (run / "ledger.jsonl").write_text(
        '{"refparcela":"A","status":"ok"}\n'
        '{"refparcela":"B","status":"failed","reason":"RuntimeError"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path)
    monkeypatch.setattr(
        stock_adapter, "_cluster_lookup_for_run",
        lambda _out_dir: {"A": "VivUniP02", "B": "BlocPluriP04"},
    )
    rows = stock_adapter.ledger_rows("historical")
    assert [row["cluster"] for row in rows] == ["VivUniP02", "BlocPluriP04"]


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


def test_building_scene_carries_the_models_own_origin(client, has_finished_run):
    """The scene must be the building's, not a stub that renders identically.

    `extract_scene` only needs three keys, and it is cheap to hand it zeros;
    doing so would draw the same shape while lying about where it stands.  So
    this asserts the values come from the building's own `deep_layers.json`.
    """
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    rows = stock_adapter.ledger_rows(FINISHED_RUN)
    reference = next(r["refparcela"] for r in rows if r.get("status") == "ok")
    layers = json.loads(stock_adapter.artifact_path(
        FINISHED_RUN, reference, "deep_layers.json").read_text(encoding="utf-8"))

    response = client.get(f"/api/stock/runs/{FINISHED_RUN}/buildings/{reference}/scene")
    assert response.status_code == 200
    scene = response.json()
    assert scene["refparcela"] == str(reference)
    assert scene["origin_epsg25830"] == [
        layers["summary"]["origin_x"], layers["summary"]["origin_y"]]
    assert scene["origin_epsg25830"] != [0.0, 0.0]
    # The viewer indexes all four unconditionally, and collapses to NaN with no
    # surfaces, so an empty geometry must never reach it as a 200.
    for key in ("surfaces", "subsurfaces", "shading", "facade_qa"):
        assert isinstance(scene[key], list)
    assert scene["surfaces"]


def test_building_scene_route_is_not_shadowed(client, has_finished_run):
    """`scene` must not bind to the artifact route's `{filename}`.

    Starlette matches in registration order.  Declared after the artifact
    route, this path would be read as a request for a file called `scene`,
    refused by the allowlist, and 422 - a failure that looks like a bad
    request rather than a routing mistake.
    """
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    rows = stock_adapter.ledger_rows(FINISHED_RUN)
    reference = next(r["refparcela"] for r in rows if r.get("status") == "ok")
    response = client.get(f"/api/stock/runs/{FINISHED_RUN}/buildings/{reference}/scene")
    assert response.status_code != 422


def test_building_scene_refuses_what_it_cannot_show(client, has_finished_run):
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    # No model on disk is a 404, not an empty scene: a building that failed or
    # was excluded before EnergyPlus has no geometry to check.
    assert client.get(
        f"/api/stock/runs/{FINISHED_RUN}/buildings/no-such-building/scene",
    ).status_code == 404
    assert client.get(
        "/api/stock/runs/no-such-run/buildings/whatever/scene").status_code == 404


def test_building_scene_reports_unreadable_evidence_as_such(client, has_finished_run, tmp_path):
    """A damaged model is a 500 with a reason, never a 422 and never a blank."""
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    rows = stock_adapter.ledger_rows(FINISHED_RUN)
    reference = next(r["refparcela"] for r in rows if r.get("status") == "ok")
    damaged = tmp_path / "model_python.osm"
    damaged.write_text("not an OpenStudio model", encoding="utf-8")
    real = stock_adapter.artifact_path

    def fake(name, ref, filename):
        return damaged if filename == "model_python.osm" else real(name, ref, filename)

    with mock.patch.object(stock_adapter, "artifact_path", fake):
        response = client.get(
            f"/api/stock/runs/{FINISHED_RUN}/buildings/{reference}/scene")
    assert response.status_code == 500
    assert "could not be opened" in response.json()["detail"]


def test_a_missing_origin_record_is_not_reported_as_a_missing_model(
        client, has_finished_run):
    """Two files are resolved here, and the 404 must name the one that is gone.

    The model is 1 MB of geometry sitting on disk; saying it is missing sends
    someone to look for a file that is right there, while the record that
    actually went absent goes unmentioned.
    """
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    rows = stock_adapter.ledger_rows(FINISHED_RUN)
    reference = next(r["refparcela"] for r in rows if r.get("status") == "ok")
    real = stock_adapter.artifact_path

    def fake(name, ref, filename):
        if filename == "deep_layers.json":
            raise FileNotFoundError(f"deep_layers.json not found for {ref}")
        return real(name, ref, filename)

    with mock.patch.object(stock_adapter, "artifact_path", fake):
        response = client.get(
            f"/api/stock/runs/{FINISHED_RUN}/buildings/{reference}/scene")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "deep_layers.json" in detail
    assert "model_python.osm" not in detail


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


def test_delete_run_removes_only_the_inactive_run_and_exact_exports(monkeypatch, tmp_path):
    stock_root = tmp_path / "stock"
    run = stock_root / "sample"
    run.mkdir(parents=True)
    (run / "ledger.jsonl").write_text('{"status":"ok"}\n', encoding="utf-8")
    exports = tmp_path / "exports"
    exports.mkdir()
    exact = exports / "stock_sample_full.zip"
    selected = exports / "stock_sample_selected-0123456789ab.zip"
    sibling = exports / "stock_sample_more_full.zip"
    for path in (exact, selected, sibling):
        path.write_bytes(b"zip")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)
    monkeypatch.setenv("WORKBENCH_EXPORT_ROOT", str(exports))

    result = stock_adapter.delete_run("sample")

    assert result == {"deleted": True, "run": "sample", "removed_exports": 2}
    assert not run.exists()
    assert not exact.exists()
    assert not selected.exists()
    assert sibling.exists()


def test_delete_run_answers_before_the_bytes_are_reclaimed(monkeypatch, tmp_path):
    """The rename is the deletion; reclaiming the space happens behind it.

    A full-city run is ~133 GB over ~26 000 directories and unlinking that
    inside the request took minutes.  The contract is that the run is gone the
    moment the call returns, whether or not its bytes have been given back yet,
    so the reclaim is stubbed out here rather than waited on.
    """
    stock_root = tmp_path / "stock"
    run = stock_root / "sample"
    (run / "runs" / "one_deep").mkdir(parents=True)
    (run / "ledger.jsonl").write_text('{"status":"ok"}\n', encoding="utf-8")
    (run / "runs" / "one_deep" / "eplusout.err").write_text("evidence", encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)
    monkeypatch.setattr(stock_adapter.threading, "Thread",
                        lambda *a, **k: types.SimpleNamespace(start=lambda: None))

    result = stock_adapter.delete_run("sample")

    assert result["deleted"] is True
    assert not run.exists()
    assert [item["run"] for item in stock_adapter.list_runs()] == []
    held = list((stock_root / ".discarded").iterdir())
    assert len(held) == 1
    assert (held[0] / "runs" / "one_deep" / "eplusout.err").read_text() == "evidence"


def test_discarded_runs_are_neither_listed_nor_addressable(monkeypatch, tmp_path):
    """What is waiting to be reclaimed must not look like a run to anyone."""
    stock_root = tmp_path / "stock"
    waiting = stock_root / ".discarded" / "sample.deadbeef"
    waiting.mkdir(parents=True)
    (waiting / "ledger.jsonl").write_text('{"status":"ok"}\n', encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    assert [item["run"] for item in stock_adapter.list_runs()] == []
    with pytest.raises(ValueError):
        stock_adapter.run_directory(".discarded")


def test_reclaim_finishes_a_deletion_that_an_exit_interrupted(monkeypatch, tmp_path):
    stock_root = tmp_path / "stock"
    waiting = stock_root / ".discarded" / "sample.deadbeef"
    (waiting / "runs" / "one_deep").mkdir(parents=True)
    (waiting / "ledger.jsonl").write_text('{"status":"ok"}\n', encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    assert stock_adapter.reclaim_discarded_runs() == 1

    assert not waiting.exists()
    assert list((stock_root / ".discarded").iterdir()) == []


def test_delete_run_refuses_a_live_process(monkeypatch, tmp_path):
    run = tmp_path / "stock" / "live"
    run.mkdir(parents=True)
    (run / "ledger.jsonl").write_text('{"status":"ok"}\n', encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path / "stock")
    monkeypatch.setattr(stock_adapter, "active_process", lambda _name: {"pid": 123})

    with pytest.raises(stock_adapter.RunActiveError):
        stock_adapter.delete_run("live")
    assert run.exists()


def test_delete_run_refuses_a_symlink_outside_the_stock_root(monkeypatch, tmp_path):
    stock_root = tmp_path / "stock"
    outside = tmp_path / "outside"
    stock_root.mkdir()
    outside.mkdir()
    (outside / "ledger.jsonl").write_text('{"status":"ok"}\n', encoding="utf-8")
    (stock_root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    with pytest.raises(ValueError):
        stock_adapter.delete_run("linked")
    assert outside.exists()


def test_delete_run_route_reports_conflict_without_removing_live_run(monkeypatch, tmp_path):
    run = tmp_path / "stock" / "live"
    run.mkdir(parents=True)
    (run / "ledger.jsonl").write_text('{"status":"ok"}\n', encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path / "stock")
    monkeypatch.setattr(stock_adapter, "active_process", lambda _name: {"pid": 123})

    with TestClient(app) as local_client:
        response = local_client.delete("/api/stock/runs/live")

    assert response.status_code == 409
    assert "still running" in response.json()["detail"]
    assert run.exists()


def test_delete_run_route_removes_an_inactive_run(monkeypatch, tmp_path):
    run = tmp_path / "stock" / "old"
    run.mkdir(parents=True)
    (run / "ledger.jsonl").write_text('{"status":"ok"}\n', encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path / "stock")
    monkeypatch.setattr(stock_adapter, "active_process", lambda _name: None)
    monkeypatch.setenv("WORKBENCH_EXPORT_ROOT", str(tmp_path / "exports"))

    with TestClient(app) as local_client:
        response = local_client.delete("/api/stock/runs/old")

    assert response.status_code == 200
    assert response.json() == {
        "deleted": True, "run": "old", "removed_exports": 0,
    }
    assert not run.exists()


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


# ---------------------------------------------------------------------------
# Counting a live ledger
#
# The full city writes ~25,000 rows and the Run screen polls every two seconds,
# so re-parsing the whole file per poll would spend more than a core on bytes
# that have not changed - while six EnergyPlus workers want the same machine.
# These lock the incremental tally: it must agree with a full parse, notice new
# rows, and never carry a previous run's count into a directory that was reused.
# ---------------------------------------------------------------------------
def _ledger(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8")


def _rows(count: int, status: str = "ok") -> list[dict]:
    return [{"refparcela": f"R{i}", "status": status, "seconds": 1.5}
            for i in range(count)]


def test_counting_a_ledger_agrees_with_reading_all_of_it(tmp_path):
    from stock_runner import read_ledger

    ledger = tmp_path / "ledger.jsonl"
    _ledger(ledger, _rows(40) + _rows(3, "failed") + _rows(7, "excluded"))

    expected: dict[str, int] = {}
    seconds = 0.0
    for row in read_ledger(ledger):
        expected[str(row["status"])] = expected.get(str(row["status"]), 0) + 1
        seconds += float(row["seconds"])

    counts, total = stock_adapter._tally(ledger)
    assert counts == expected
    assert total == pytest.approx(seconds)


def test_rows_appended_after_the_first_count_are_counted(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _ledger(ledger, _rows(10))
    assert stock_adapter._tally(ledger)[0] == {"ok": 10}

    with ledger.open("a", encoding="utf-8") as handle:
        for row in _rows(5, "failed"):
            handle.write(json.dumps(row) + "\n")
    assert stock_adapter._tally(ledger)[0] == {"ok": 10, "failed": 5}


def test_a_row_that_is_still_being_written_is_counted_once(tmp_path):
    """A read can land between the row and its newline; the half must wait."""
    ledger = tmp_path / "ledger.jsonl"
    complete = json.dumps({"refparcela": "R9", "status": "ok", "seconds": 2.0})
    _ledger(ledger, _rows(3))
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(complete[:20])
    assert stock_adapter._tally(ledger)[0] == {"ok": 3}

    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(complete[20:] + "\n")
    assert stock_adapter._tally(ledger)[0] == {"ok": 4}


def test_a_directory_reused_by_a_new_run_does_not_inherit_the_old_count(tmp_path):
    """Otherwise the screen would report buildings this run never simulated."""
    ledger = tmp_path / "ledger.jsonl"
    _ledger(ledger, _rows(30))
    assert stock_adapter._tally(ledger)[0] == {"ok": 30}

    _ledger(ledger, _rows(4))              # started again, same name
    assert stock_adapter._tally(ledger)[0] == {"ok": 4}


# ---------------------------------------------------------------------------
# The duration a person plans a multi-day run around
# ---------------------------------------------------------------------------
def test_the_time_estimate_is_measured_from_finished_runs(tmp_path, monkeypatch):
    """A rate carried over from an older engine would be wrong by hours."""
    stock_root = tmp_path / "stock"
    old, new = stock_root / "older", stock_root / "newer"
    for path, mean in ((old, 30.0), (new, 61.5)):
        path.mkdir(parents=True)
        (path / "aggregate.json").write_text(
            json.dumps({"seconds_per_building": {"median": 40.0, "mean": mean}}),
            encoding="utf-8")
    os.utime(old / "aggregate.json", (1_000_000, 1_000_000))
    os.utime(new / "aggregate.json", (2_000_000, 2_000_000))
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    rate, basis = stock_adapter._seconds_per_building()
    assert rate == 61.5                      # the newest run, not the first found
    assert "newer" in basis


def test_an_installation_with_no_finished_run_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path / "empty")
    rate, basis = stock_adapter._seconds_per_building()
    assert rate == stock_adapter.SECONDS_PER_BUILDING
    assert basis == "default"


def test_the_rate_is_the_mean_because_the_workers_share_one_queue(tmp_path, monkeypatch):
    """Benicalap v8: 968 buildings, 3 workers, mean 60.1 s -> it took 323.47 min.

    The median was 40.2 s and the slowest building 1130 s; estimating from the
    median would have promised 217 minutes for a run that took 323.
    """
    stock_root = tmp_path / "stock"
    (stock_root / "v8").mkdir(parents=True)
    (stock_root / "v8" / "aggregate.json").write_text(
        json.dumps({"seconds_per_building": {"median": 40.2, "mean": 60.1}}),
        encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    rate, _ = stock_adapter._seconds_per_building()
    predicted = 968 * rate / 3 / 60
    assert predicted == pytest.approx(323.47, rel=0.01)


def test_an_event_run_is_not_a_basis_for_an_annual_estimate(tmp_path, monkeypatch):
    """The real pair, on the day the full city was about to be launched.

    `LECCO_1` was the newest finished run and simulated an 8-day window at
    9.6 s per building; `benicalap_v9` was annual at 56.3 s.  Taking the newest
    by date alone quoted 11 hours for Valencia's 25,094 buildings where the
    annual basis says 65 - the difference between waiting through an afternoon
    and leaving a machine running for three days.
    """
    stock_root = tmp_path / "stock"
    event, annual = stock_root / "LECCO_1", stock_root / "benicalap_v9"
    for path, mean, period in (
            (annual, 56.3, {"period": "annual", "unit": "kWh/m²·yr"}),
            (event, 9.6, {"period": "microclimate_event", "event_days": 8})):
        path.mkdir(parents=True)
        (path / "aggregate.json").write_text(json.dumps({
            "seconds_per_building": {"median": 40.3, "mean": mean},
            "energy_period": period,
        }), encoding="utf-8")
    # The event run really is the newer one; skipping it is a judgement about
    # comparability, not a date comparison that happens to work out.
    os.utime(annual / "aggregate.json", (1_000_000, 1_000_000))
    os.utime(event / "aggregate.json", (2_000_000, 2_000_000))
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    rate, basis = stock_adapter._seconds_per_building()
    assert rate == 56.3
    assert "benicalap_v9" in basis
    assert 25_094 * rate / 6 / 3600 == pytest.approx(65.4, rel=0.02)


def test_a_run_from_before_energy_period_still_counts_as_annual(tmp_path, monkeypatch):
    """Absent is not unknown here: every run predating the block was annual.

    Dropping them would push a fresh installation back onto the hard-coded
    default, which is the figure measuring was introduced to replace.
    """
    stock_root = tmp_path / "stock"
    (stock_root / "v7").mkdir(parents=True)
    (stock_root / "v7" / "aggregate.json").write_text(
        json.dumps({"seconds_per_building": {"median": 56.6, "mean": 93.2}}),
        encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    rate, basis = stock_adapter._seconds_per_building()
    assert rate == 93.2
    assert "v7" in basis


# ---------------------------------------------------------------------------
# Building report
# ---------------------------------------------------------------------------
def test_building_report_renders_the_runs_own_numbers(client, has_finished_run):
    """The report must repeat the preserved record, not recompute it.

    Every figure on the page is supposed to be lifted from `deep_layers.json`,
    so the test picks values out of that file and demands they appear.  A
    report that recalculated anything would drift from the evidence it claims
    to be showing.
    """
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    rows = stock_adapter.ledger_rows(FINISHED_RUN)
    reference = next(r["refparcela"] for r in rows if r.get("status") == "ok")
    layers = json.loads(stock_adapter.artifact_path(
        FINISHED_RUN, reference, "deep_layers.json").read_text(encoding="utf-8"))

    response = client.get(f"/api/stock/runs/{FINISHED_RUN}/buildings/{reference}/report")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]

    page = response.text
    assert str(reference) in page
    # provenance, geometry, energy and QA all present and all the run's own
    assert layers["summary"]["verified_profile"]["profile_id"] in page
    assert f"{layers['results']['total_site_kwh_m2']:,.2f}" in page
    assert f"{layers['summary']['footprint_m2']:,.1f}" in page
    for check in layers["qa"]:
        assert check["check"] in page
    # the stage records are explained, not dumped
    assert "Storey-use and partial-storey policy" in page
    assert "Thermal-zoning scheme" in page
    # The primary reading path is plain-language and addressable from the UI;
    # raw JSON/OSM/log files stay available only as a clearly technical layer.
    assert 'id="interpretation"' in page
    assert 'id="limitations"' in page
    assert 'id="energy"' in page
    assert 'id="quality"' in page
    assert 'id="methods"' in page
    assert 'id="provenance"' in page
    assert 'id="references"' in page
    assert "Interpretation summary" in page
    assert "Original technical files" in page
    assert "specialist software and forensic audit" in page


def test_building_report_route_is_not_shadowed(client, has_finished_run):
    """`report` must not bind to the artifact route's `{filename}`.

    Starlette matches in registration order.  Declared after the artifact
    route, `report` is read as a filename and refused by the allowlist with a
    422 - the same trap `scene` documents.  This pins the order.
    """
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    rows = stock_adapter.ledger_rows(FINISHED_RUN)
    reference = next(r["refparcela"] for r in rows if r.get("status") == "ok")
    response = client.get(f"/api/stock/runs/{FINISHED_RUN}/buildings/{reference}/report")
    assert response.status_code != 422, "the artifact route swallowed 'report'"
    assert response.status_code == 200


def test_building_report_refuses_a_building_with_no_evidence(client, has_finished_run):
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    assert client.get(
        f"/api/stock/runs/{FINISHED_RUN}/buildings/NO_SUCH_BUILDING/report"
    ).status_code == 404
    assert client.get(
        f"/api/stock/runs/{FINISHED_RUN}/buildings/..%2F..%2Fetc/report"
    ).status_code in (404, 422)


def test_building_report_says_unreadable_rather_than_bad_request(tmp_path, monkeypatch):
    """A corrupt layer record is broken evidence, not a bad request.

    422 would blame the caller for asking about a building that exists.  The
    distinction is the same one `SceneUnavailable` draws.
    """
    stock_root = tmp_path / "stock"
    building = stock_root / "run1" / "runs" / "REF_deep"
    building.mkdir(parents=True)
    (building / "deep_layers.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    with pytest.raises(stock_adapter.ReportUnavailable):
        stock_adapter.building_report("run1", "REF")
    assert not issubclass(stock_adapter.ReportUnavailable, ValueError)


def test_building_report_renders_without_a_ledger_row(tmp_path, monkeypatch):
    """Preserved evidence outlives its row; the page must not need one."""
    stock_root = tmp_path / "stock"
    building = stock_root / "run1" / "runs" / "REF_deep"
    building.mkdir(parents=True)
    (building / "deep_layers.json").write_text(json.dumps({
        "summary": {"refparcela": "REF", "footprint_m2": 100.0},
        "results": {"total_site_kwh_m2": 42.0},
        "carbon": {}, "qa": [], "layers": {},
    }), encoding="utf-8")
    (stock_root / "run1" / "ledger.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    page = stock_adapter.building_report("run1", "REF")
    assert "REF" in page and "42.00" in page


# ---------------------------------------------------------------------------
# Unfinished buildings
# ---------------------------------------------------------------------------
def test_unfinished_splits_failures_from_exclusions(client, has_finished_run):
    """The two need different actions, so they must not arrive as one list."""
    if not has_finished_run:
        pytest.skip(f"{FINISHED_RUN} not on disk")
    body = client.get(f"/api/stock/runs/{FINISHED_RUN}/unfinished").json()
    rows = stock_adapter.ledger_rows(FINISHED_RUN)
    assert body["failed"] == sorted(
        r["refparcela"] for r in rows if r.get("status") in ("failed", "failed_qa"))
    assert body["excluded"] == sorted(
        r["refparcela"] for r in rows if r.get("status") == "excluded")
    assert set(body["failed"]).isdisjoint(body["excluded"])
    assert sum(body["exclusion_reasons"].values()) == len(body["excluded"])


def test_unfinished_counts_a_retried_building_once(tmp_path, monkeypatch):
    """A failure that later succeeded is done, not outstanding.

    `--retry-failed` appends, so the same reference can hold a `failed` row and
    an `ok` one.  Reading raw rows would keep offering to retry a building that
    already has a result.
    """
    stock_root = tmp_path / "stock"
    (stock_root / "run1").mkdir(parents=True)
    (stock_root / "run1" / "ledger.jsonl").write_text("\n".join(json.dumps(row) for row in [
        {"refparcela": "A", "status": "failed", "error": "boom"},
        {"refparcela": "A", "status": "ok", "total_site_kwh_m2": 40.0},
        {"refparcela": "B", "status": "failed", "error": "boom"},
        {"refparcela": "C", "status": "excluded", "reason": "footprint_outside_range"},
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    left = stock_adapter.unfinished_references("run1")
    assert left["failed"] == ["B"]
    assert left["excluded"] == ["C"]
    assert left["exclusion_reasons"] == {"footprint_outside_range": 1}


def test_retry_failed_is_passed_to_the_runner_as_its_own_flag(tmp_path, monkeypatch):
    """`--retry-failed` is not a flavour of `--resume`.

    `--resume` skips every terminal row, including failures; this one exists to
    pick exactly those back up.  Folding them together would make the retry
    button a no-op.
    """
    captured: dict = {}

    class _FakePopen:
        pid = 4321

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv

    monkeypatch.setattr(stock_adapter.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", tmp_path / "stock")
    # Its own inputs, not the machine's active project.  This test is about how
    # one flag reaches the runner; leaving `start_run` to resolve the live
    # settings made it pass or fail according to which city happened to be
    # active, which is a property of the machine and not of the flag.
    for name in ("stock.gpkg", "climate.json", "template.osm"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    inputs = stock_adapter.InputSet(
        stock=tmp_path / "stock.gpkg",
        climate=tmp_path / "climate.json",
        template=tmp_path / "template.osm",
    )

    stock_adapter.start_run("run1", "references", references=["A"],
                            inputs=inputs, retry_failed=True,
                            log_dir=tmp_path / "logs")
    assert "--retry-failed" in captured["argv"]
    assert "--resume" not in captured["argv"]


def test_scope_size_prefers_the_whole_scope_over_a_resumes_remainder(tmp_path, monkeypatch):
    """A resume rewrites `run_config.json` with only the work it has left.

    Measured on ALL-VALENC-A: `runnable 6251 + excluded 1351` against 26 558
    cumulative ledger rows, which is a 268 % progress bar.  The clamp hid it as
    a permanent 100 %, which is worse than showing nothing.
    """
    stock_root = tmp_path / "stock"
    (stock_root / "run1").mkdir(parents=True)
    (stock_root / "run1" / "run_config.json").write_text(json.dumps({
        "runnable": 6251, "excluded": 1351, "scope_total": 26445,
    }), encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    assert stock_adapter.scope_size("run1")["total"] == 26445


def test_scope_size_still_reads_a_run_written_before_scope_total(tmp_path, monkeypatch):
    """Absent is not zero: the old pair is the best record those runs have."""
    stock_root = tmp_path / "stock"
    (stock_root / "run1").mkdir(parents=True)
    (stock_root / "run1" / "run_config.json").write_text(
        json.dumps({"runnable": 900, "excluded": 100}), encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    assert stock_adapter.scope_size("run1")["total"] == 1000


def test_scope_size_never_reports_a_scope_smaller_than_its_own_parts(tmp_path, monkeypatch):
    """A corrupt or stale `scope_total` must not shrink the denominator."""
    stock_root = tmp_path / "stock"
    (stock_root / "run1").mkdir(parents=True)
    (stock_root / "run1" / "run_config.json").write_text(json.dumps({
        "runnable": 900, "excluded": 100, "scope_total": 5,
    }), encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)

    assert stock_adapter.scope_size("run1")["total"] == 1000


def _curated_run(tmp_path, monkeypatch, rows, *, aggregate=True):
    stock_root = tmp_path / "stock"
    run = stock_root / "city"
    run.mkdir(parents=True)
    (run / "ledger.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    if aggregate:
        (run / "aggregate.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)
    monkeypatch.setattr(stock_adapter, "active_process", lambda _name: None)
    return run


def test_curated_csv_publishes_exactly_the_documented_schema(tmp_path, monkeypatch):
    """The header is the contract the User Guide's Appendix A declares.

    `/ledger.csv` builds its columns from whichever keys the first rows happen
    to carry, so a run whose first record is an exclusion puts `reason` before
    `status`.  A published table cannot move its columns between runs.
    """
    _curated_run(tmp_path, monkeypatch, [
        {"refparcela": "B", "status": "ok", "cluster": "Z", "total_site_kwh_m2": 41.2},
        {"refparcela": "A", "status": "ok", "cluster": "Z", "total_site_kwh_m2": 39.9},
    ])

    rows = stock_adapter.curated_building_rows("city")

    assert list(rows[0]) == list(stock_adapter.CURATED_BUILDING_CSV_FIELDS)
    assert len(stock_adapter.CURATED_BUILDING_CSV_FIELDS) == 88
    assert [row["refparcela"] for row in rows] == ["A", "B"]   # deterministic


def test_curated_csv_never_carries_a_local_path_or_a_traceback(tmp_path, monkeypatch):
    """These are the two fields that made the raw export unpublishable.

    The engine's own failure message quotes the absolute path of the `.err`
    file it wants read.  The sentence is worth keeping; the path is this
    machine's directory layout, and the project directory contains spaces -
    which is what defeated the first attempt at scrubbing it.
    """
    stock_root = tmp_path / "stock"
    run = stock_root / "city"
    run.mkdir(parents=True)
    (run / "ledger.jsonl").write_text(json.dumps({
        "refparcela": "A", "status": "failed", "reason": "RuntimeError",
        "message": f"1 unexplained Severe. See {run}/runs/A_deep/eplusout.err",
        "model_osm": "/Volumes/disk/Mirza works/out/model.osm",
        "traceback": 'File "/Users/someone/src/x.py", line 3\nRuntimeError',
    }) + "\n", encoding="utf-8")
    (run / "aggregate.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(stock_adapter, "STOCK_ROOT", stock_root)
    monkeypatch.setattr(stock_adapter, "active_process", lambda _name: None)

    rows = stock_adapter.curated_building_rows("city")

    published = "\n".join("\x1f".join(row.values()) for row in rows)
    assert "/Volumes" not in published
    assert "/Users" not in published
    assert str(run) not in published
    assert "runs/A_deep/eplusout.err" in rows[0]["message"]   # still points at it
    assert "traceback" not in stock_adapter.CURATED_BUILDING_CSV_FIELDS
    assert "model_osm" not in stock_adapter.CURATED_BUILDING_CSV_FIELDS


def test_an_exclusion_is_explained_in_words_not_only_in_a_code(tmp_path, monkeypatch):
    """Twelve exclusions arrived with a machine code and no sentence."""
    _curated_run(tmp_path, monkeypatch, [
        {"refparcela": "A", "status": "excluded", "reason": "interior_rings_2"},
        {"refparcela": "B", "status": "excluded", "reason": "duplicate_refparcela_3_rows"},
    ])

    rows = stock_adapter.curated_building_rows("city")

    assert "2 interior ring" in rows[0]["message"]
    assert "3 rows" in rows[1]["message"]
    assert rows[0]["reason"] == "interior_rings_2"   # the code stays beside it


def test_curated_csv_refuses_a_run_whose_totals_are_still_moving(tmp_path, monkeypatch):
    """A partial ledger downloaded under a final name is the whole hazard."""
    _curated_run(tmp_path, monkeypatch, [
        {"refparcela": "A", "status": "ok", "total_site_kwh_m2": 40.0},
    ], aggregate=False)

    with pytest.raises(stock_adapter.RunActiveError):
        stock_adapter.curated_building_rows("city")


def test_a_measured_zero_and_an_absent_value_do_not_look_alike(tmp_path, monkeypatch):
    _curated_run(tmp_path, monkeypatch, [{
        "refparcela": "A", "status": "ok",
        "space_heating_kwh_m2": 0.0,          # measured: the building never heated
        "cooling_kwh_m2": None,               # absent from the record
        "qa_all_passed": True,
        "total_site_kwh_m2": float("nan"),
    }])

    row = stock_adapter.curated_building_rows("city")[0]

    assert row["space_heating_kwh_m2"] == "0"
    assert row["cooling_kwh_m2"] == ""
    assert row["qa_all_passed"] == "true"      # not True, not 1
    assert row["total_site_kwh_m2"] == ""      # NaN is not a measurement


def test_a_small_number_is_written_in_full_not_as_an_exponent(tmp_path, monkeypatch):
    """`repr` switches to an exponent below 1e-4.

    Measured on the finished city run: 583 cells of `footprint_fidelity` came
    out as `6.1e-05`, which left one column holding two visibly different kinds
    of number in a table meant to be read.
    """
    _curated_run(tmp_path, monkeypatch, [{
        "refparcela": "A", "status": "ok",
        "footprint_fidelity": 6.1e-05,
        "storey_rule_margin": 2.7e-08,
        "total_site_kwh_m2": 45.04,
    }])

    row = stock_adapter.curated_building_rows("city")[0]

    assert row["footprint_fidelity"] == "0.000061"
    assert row["storey_rule_margin"] == "0.000000027"
    assert row["total_site_kwh_m2"] == "45.04"   # unchanged where it was fine


def test_text_that_a_spreadsheet_would_execute_is_neutralised(tmp_path, monkeypatch):
    _curated_run(tmp_path, monkeypatch, [{
        "refparcela": "A", "status": "failed", "reason": "=cmd|'/c calc'!A1",
        "space_heating_kwh_m2": -1.5,
    }])

    row = stock_adapter.curated_building_rows("city")[0]

    assert row["reason"].startswith("'=")
    assert row["space_heating_kwh_m2"] == "-1.5"   # a negative number stays one


def test_every_published_column_is_defined_in_the_dictionary():
    entries = stock_adapter.curated_building_dictionary()

    assert [item["field"] for item in entries] == list(
        stock_adapter.CURATED_BUILDING_CSV_FIELDS)
    assert all(item["meaning"] for item in entries)
    carbon = {item["field"]: item["ledger_source"] for item in entries}
    assert carbon["total_site_co2_t"] == "total_site_co2_t_yr"
    assert not any(field.endswith("_yr")
                   for field in stock_adapter.CURATED_BUILDING_CSV_FIELDS)
