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
