"""The stock runner: ledger durability, resume, isolation, pruning, aggregation.

The runner carries no physics, so these tests are about the promises that let a
multi-day run be trusted: nothing is silently skipped, an interruption does not
lose or repeat work, one bad building cannot kill the run, and the shading
context is not quietly changed by floor imputation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from shapely.geometry import Point, Polygon

import deep_building as db
import model_builder as mb
import stock_runner as sr
import verified_model as vm

PROJECT_ROOT = Path(__file__).resolve().parents[1]


PILOT = "4252702YJ2745A"


# ---------------------------------------------------------------------------
# The one engine change: target source and shading context are separable
# ---------------------------------------------------------------------------
def test_unset_neighbour_path_reproduces_historical_behaviour():
    # this is the regression that protects every result produced before the
    # stock runner existed
    assert db.resolve_neighbour_source(None, None) == Path(mb.NEIGHBORS_SHP)


def test_neighbour_path_falls_back_to_the_target_source():
    assert db.resolve_neighbour_source(Path("/a/gis.gpkg"), None) == Path("/a/gis.gpkg")


def test_neighbour_path_wins_when_both_are_given():
    # the stock case: target reads the prepared file, context reads the raw one
    assert db.resolve_neighbour_source(
        Path("/a/prepared.gpkg"), Path("/b/raw.shp")) == Path("/b/raw.shp")


def test_simulate_verified_building_forwards_the_context(monkeypatch):
    seen = {}

    def fake(refparcela, out_dir, **kwargs):
        seen.update(kwargs)
        return {"refparcela": refparcela}, True

    monkeypatch.setattr(db, "simulate_deep_building", fake)
    vm.simulate_verified_building(PILOT, Path("/tmp/x"),
                                  gis_path=Path("/a/prepared.gpkg"),
                                  neighbors_path=Path("/b/raw.shp"))
    assert seen["gis_path"] == Path("/a/prepared.gpkg")
    assert seen["neighbors_path"] == Path("/b/raw.shp")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------
def test_ledger_round_trip(tmp_path):
    path = tmp_path / "ledger.jsonl"
    with sr.Ledger(path) as ledger:
        ledger.append({"refparcela": "A", "status": "ok"})
        ledger.append({"refparcela": "B", "status": "failed"})
    rows = sr.read_ledger(path)
    assert [r["refparcela"] for r in rows] == ["A", "B"]


def test_ledger_survives_a_truncated_final_line(tmp_path):
    # what a hard kill in the middle of a write actually leaves behind
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"refparcela": "A", "status": "ok"}\n{"refparcela": "B", "st',
                    encoding="utf-8")
    rows = sr.read_ledger(path)
    assert [r["refparcela"] for r in rows] == ["A"]


def test_resume_skips_done_but_not_failures(tmp_path):
    rows = [{"refparcela": "A", "status": "ok"},
            {"refparcela": "B", "status": "failed"},
            {"refparcela": "C", "status": "excluded"}]
    done = sr.completed_references(rows)
    assert done == {"A", "C"}
    assert "B" not in done          # --retry-failed can pick it up again


def test_missing_ledger_is_an_empty_run(tmp_path):
    assert sr.read_ledger(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# Geometry screening: every exclusion is recorded with a reason
# ---------------------------------------------------------------------------
def _stock(rows):
    import geopandas as gpd
    return gpd.GeoDataFrame(rows, crs="EPSG:25830")


def _square(x0, side):
    return Polygon([(x0, 0), (x0 + side, 0), (x0 + side, side), (x0, side)])


def test_screen_uses_the_engines_own_footprint_window():
    # a hardcoded 30 m2 guess let 30-50 m2 buildings through the screen and into
    # a stack trace, and ignored the upper bound entirely (Stage 2, 2026-07-28)
    low, high = sr.footprint_limits()
    assert (low, high) == (mb.DEFAULT_BUILD_CONFIG.geometry.footprint_min_m2,
                           mb.DEFAULT_BUILD_CONFIG.geometry.footprint_max_m2)


def test_screen_geometry_reasons():
    low, high = sr.footprint_limits()
    courtyard = Polygon([(0, 0), (40, 0), (40, 40), (0, 40)],
                        [[(10, 10), (20, 10), (20, 20), (10, 20)]])
    stock = _stock([
        {"refparcela": "OK", "geometry": _square(0, 20)},                 # 400 m2
        {"refparcela": "TINY", "geometry": _square(100, (low ** 0.5) / 2)},
        {"refparcela": "HUGE", "geometry": _square(200, (high ** 0.5) * 2)},
        {"refparcela": "COURT", "geometry": courtyard},
        {"refparcela": "POINT", "geometry": Point(0, 0)},
    ])
    runnable, excluded = sr.screen_geometry(stock)
    assert runnable == ["OK"]
    reasons = {e["refparcela"]: e["reason"] for e in excluded}
    assert reasons["TINY"] == "footprint_outside_range"
    assert reasons["HUGE"] == "footprint_outside_range"
    assert reasons["COURT"] == "interior_rings_1"
    assert reasons["POINT"].startswith("unsupported_geometry")


def test_simplification_never_excludes_a_building():
    """The gate that refused 853 buildings at 0.3 m now refuses none.

    This test used to assert the opposite - that a shape simplification would
    distort is dropped from the city with a reason string.  That was the wrong
    thing to promise.  Simplification exists to bound the vertex count, so the
    only thing it may decide is how many vertices the model carries, never
    whether the building is in the city at all.  When the configured tolerance
    cannot hold the shape, a finer one is used; at worst the polygon is modelled
    as drawn.
    """
    import math
    # A near-circle, 4 m radius: at the configured tolerance it moves well past
    # the fidelity bound, and it clears the size floor (50.2 m2 against 20 m2)
    # so it exercises simplification rather than the size gate - a smaller one
    # would be excluded for being small and the test would pass for the wrong
    # reason.
    circle = Polygon([(600 + 4 * math.cos(t * math.pi / 30),
                       4 * math.sin(t * math.pi / 30)) for t in range(60)])
    low, _ = sr.footprint_limits()
    assert circle.area > low, "the fixture must exercise simplification, not size"
    g = mb.DEFAULT_BUILD_CONFIG.geometry
    at_configured = circle.simplify(g.simplify_tolerance_m, preserve_topology=True)
    assert mb.footprint_fidelity(circle, at_configured) > g.max_area_delta_fraction, \
        "the fixture must be past the bound at the configured tolerance"

    stock = _stock([{"refparcela": "ROUND", "geometry": circle},
                    {"refparcela": "OK", "geometry": _square(0, 20)}])
    runnable, excluded = sr.screen_geometry(stock)
    assert sorted(runnable) == ["OK", "ROUND"]
    assert excluded == []

    detail = mb.prepare_footprint_detail(circle)
    assert detail["footprint_fidelity"] <= g.max_area_delta_fraction
    assert detail["simplify_tolerance_used_m"] < g.simplify_tolerance_m


# ---------------------------------------------------------------------------
# The footprint gate: a fidelity guarantee, not a fixed tolerance
# ---------------------------------------------------------------------------
def test_fidelity_sees_the_distortion_that_area_cancels():
    """Why the metric changed: area is signed and its errors cancel."""
    raw = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    shifted = Polygon([(1, 0), (11, 0), (11, 10), (1, 10)])
    # the same building by area, a different building by geometry
    assert shifted.area == raw.area
    assert abs(shifted.area - raw.area) / raw.area == 0.0      # the old check
    assert mb.footprint_fidelity(raw, shifted) == pytest.approx(0.2)


def test_a_coarser_simplification_is_taken_only_when_it_costs_nothing():
    """`1349405YJ3514G`, translated to the origin and otherwise untouched.

    Shapely's topology-preserving simplifier is not strictly hierarchical, so a
    finer tolerance can leave a worse shape: this footprint scores 0.85 % at
    0.1 m and 0.69 % at 0.3 m, on four vertices either way.  35 Valencia
    buildings sat like that.  The coarser candidate is taken because it is
    better on fidelity AND no more expensive - never as a trade-off.
    """
    footprint = Polygon([
        (22.26, 4.21), (22.26, 4.13), (22.51, 4.13), (22.449, 0.13),
        (22.449, 0.0), (9.97, 0.92), (0.0, 1.64), (0.25, 5.581), (3.92, 5.35)])
    g = mb.DEFAULT_BUILD_CONFIG.geometry
    fine = footprint.simplify(g.simplify_tolerance_m, preserve_topology=True)
    coarse = footprint.simplify(g.simplify_tolerance_m * 3.0, preserve_topology=True)
    assert mb.footprint_fidelity(footprint, coarse) < mb.footprint_fidelity(footprint, fine)
    assert len(coarse.exterior.coords) <= len(fine.exterior.coords)

    detail = mb.prepare_footprint_detail(footprint)
    assert detail["simplify_tolerance_used_m"] == pytest.approx(g.simplify_tolerance_m * 3.0)
    assert detail["footprint_fidelity"] == pytest.approx(
        mb.footprint_fidelity(footprint, coarse))


def test_an_unreachable_fidelity_bound_models_the_polygon_as_drawn():
    """The fallback is the raw polygon, never a refusal."""
    import math
    fine = Polygon([(3 * math.cos(2 * math.pi * k / 2000),
                     3 * math.sin(2 * math.pi * k / 2000)) for k in range(2000)])
    geometry = mb.DEFAULT_BUILD_CONFIG.geometry.model_copy(
        update={"max_area_delta_fraction": 1e-12})
    config = mb.DEFAULT_BUILD_CONFIG.model_copy(update={"geometry": geometry})
    detail = mb.prepare_footprint_detail(fine, config=config)   # must not raise
    assert detail["simplify_tolerance_used_m"] == 0.0
    assert len(detail["coords"]) == len(fine.exterior.coords) - 1
    assert detail["footprint_fidelity"] == 0.0


def test_prepare_footprint_keeps_its_two_value_contract():
    """Six call sites still unpack two values; the detail form is additive."""
    square = _square(0, 20)
    coords, area = mb.prepare_footprint(square)
    detail = mb.prepare_footprint_detail(square)
    assert coords == detail["coords"]
    assert area == detail["area_m2"]


def test_geometry_quality_reports_absence_as_absence():
    """A ledger written before these fields must not report zeroes.

    `0 % over the bound` and `nobody measured` are different statements, and
    printing the first when the second is true is how this project has twice
    published a default as if it were a measurement.
    """
    old = pd.DataFrame([{"res_area_m2": 100.0, "total_site_kwh_m2": 50.0}])
    block = sr.geometry_quality_block(old)
    assert block["measured"] is False
    assert "buildings_measured" not in block
    assert "fidelity_median" not in block
    assert "predates" in block["note"]
    assert sr.geometry_quality_block(pd.DataFrame())["measured"] is False


def test_geometry_quality_categories_partition_the_buildings():
    """The four tolerance outcomes must add back to the building count."""
    frame = pd.DataFrame([
        {"footprint_fidelity": 0.0004, "simplify_tolerance_used_m": 0.1,
         "storey_rule_margin": 0.30, "storey_rule_snapped": False, "res_area_m2": 500.0},
        {"footprint_fidelity": 0.0090, "simplify_tolerance_used_m": 0.05,
         "storey_rule_margin": 0.0089, "storey_rule_snapped": True, "res_area_m2": 437.0},
        {"footprint_fidelity": 0.0069, "simplify_tolerance_used_m": 0.3,
         "storey_rule_margin": None, "storey_rule_snapped": False, "res_area_m2": 200.0},
        {"footprint_fidelity": 0.0, "simplify_tolerance_used_m": 0.0,
         "storey_rule_margin": 0.015, "storey_rule_snapped": False, "res_area_m2": 300.0},
    ])
    block = sr.geometry_quality_block(frame)
    assert block["measured"] is True
    assert block["buildings_measured"] == 4
    assert (block["at_configured_tolerance"] + block["refined_finer"]
            + block["taken_coarser"] + block["modelled_as_drawn"]) == 4
    assert block["over_bound"] == 0
    assert block["storey_snapped"] == 1
    # the margin is absent for one building, and absence is not counted as
    # sitting outside the band
    assert block["storey_margin_measured"] == 3
    assert block["within_band"] == 1


def test_duplicate_reference_is_excluded_once_not_run(tmp_path):
    # the engine refuses a duplicated refparcela outright; screening per row used
    # to file it twice, as excluded AND failed (Stage 2, 2026-07-28)
    stock = _stock([
        {"refparcela": "DUP", "geometry": _square(0, 20)},
        {"refparcela": "DUP", "geometry": _square(100, 5)},
        {"refparcela": "OK", "geometry": _square(200, 20)},
    ])
    runnable, excluded = sr.screen_geometry(stock)
    assert runnable == ["OK"]
    assert [e["refparcela"] for e in excluded] == ["DUP"]
    assert excluded[0]["reason"] == "duplicate_refparcela_2_rows"


def test_screening_never_drops_a_building_silently():
    low, _ = sr.footprint_limits()
    stock = _stock([{"refparcela": "A", "geometry": _square(0, 20)},
                    {"refparcela": "B", "geometry": _square(100, (low ** 0.5) / 3)}])
    runnable, excluded = sr.screen_geometry(stock)
    references = set(stock["refparcela"])
    assert set(runnable) | {e["refparcela"] for e in excluded} == references


# ---------------------------------------------------------------------------
# Failure isolation and pruning
# ---------------------------------------------------------------------------
def test_run_one_records_a_failure_instead_of_raising(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("Building row 'altura_max' field must be an integer")

    monkeypatch.setattr(vm, "simulate_verified_building", boom)
    monkeypatch.setitem(sr._WORKER, "out_dir", str(tmp_path))
    worker = {"prepared_gis": "p.gpkg", "context_gis": "c.shp",
              "zero_policy": "literal_zero", "keep": "summary"}
    worker.update({key: f"id-{key}" for key in sr.IDENTITY_FIELDS})
    for key, value in worker.items():
        monkeypatch.setitem(sr._WORKER, key, value)

    row = sr.run_one(("BROKEN", "BlocPluriP04"))
    assert row["status"] == "failed"
    assert row["cluster"] == "BlocPluriP04"
    assert row["reason"] == "ValueError"
    assert "altura_max" in row["message"]
    assert "traceback" in row
    # a failure row still has to say which inputs produced it
    for key in sr.IDENTITY_FIELDS:
        assert row[key] == f"id-{key}", key


# ---------------------------------------------------------------------------
# Swappable inputs
# ---------------------------------------------------------------------------
def test_no_flags_still_means_the_verified_valencia_setup():
    """No flags must resolve to Valencia - but through the real validators.

    The default path used to stamp the literal `valencia_iwec:builtin`, so a
    changed EPW or template on disk would have gone unnoticed.
    """
    import climate as cl
    import stock_input_policy as sip
    import template_contract as tpl

    assert sr.load_policy(None) == sip.StockInputPolicy()

    climate = cl.valencia_iwec()
    template = tpl.load_template_set(tpl.DEFAULT_TEMPLATE,
                                     Path(mb._project_root()) / "var" / "templates")
    stamps = sr.input_fingerprints(
        climate, template, "pol", profile_fingerprint="prof",
        zero_policy="literal_zero", stock_source_fingerprint="src")

    assert stamps["climate_name"] == "valencia_iwec"
    # a real content hash, not a label
    assert len(stamps["climate_fingerprint"]) == 64
    assert len(stamps["template_fingerprint"]) == 64
    assert ":builtin" not in stamps["climate_fingerprint"]
    assert len(stamps["run_identity"]) == 64


def test_run_identity_covers_everything_that_changes_a_result():
    import climate as cl
    import template_contract as tpl
    climate = cl.valencia_iwec()
    template = tpl.load_template_set(tpl.DEFAULT_TEMPLATE,
                                     Path(mb._project_root()) / "var" / "templates")

    def identity(**overrides):
        kwargs = dict(profile_fingerprint="prof", zero_policy="literal_zero",
                      stock_source_fingerprint="src")
        kwargs.update(overrides)
        return sr.input_fingerprints(climate, template, "pol", **kwargs)["run_identity"]

    base = identity()
    # each of these describes a different physical or data situation
    assert identity(profile_fingerprint="other") != base
    assert identity(zero_policy="cluster_median_impute") != base
    assert identity(stock_source_fingerprint="other") != base
    assert identity() == base


def test_a_policy_file_is_read_and_notes_are_ignored(tmp_path):
    import stock_input_policy as sip
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "floors_field": "n_plantas",
        "_notes": {"why": "human-readable notes must not reach the dataclass"},
    }), encoding="utf-8")

    policy = sr.load_policy(path)
    assert policy.floors_field == "n_plantas"
    assert sip.policy_fingerprint(policy) != sip.policy_fingerprint(
        sip.StockInputPolicy())


def test_the_shipped_policy_file_reproduces_the_default():
    import stock_input_policy as sip
    shipped = sr.load_policy(PROJECT_ROOT / "policies" / "valencia_default.json")
    assert sip.policy_fingerprint(shipped) == sip.policy_fingerprint(
        sip.StockInputPolicy())


def test_a_climate_points_the_build_config_at_its_own_epw():
    import climate as cl
    valencia = cl.valencia_iwec()
    config = sr.build_config_for(valencia, None)
    assert config is not None
    assert config.data.epw_path == valencia.epw_path
    # the frozen default must not have been mutated in the process
    import model_builder as mb
    assert mb.DEFAULT_BUILD_CONFIG.data.epw_path != Path("changed")


# ---------------------------------------------------------------------------
# The resume guard - two climates must never land in one ledger
# ---------------------------------------------------------------------------
def _stamps(**overrides):
    stamps = {key: f"{key}-1" for key in sr.IDENTITY_FIELDS}
    stamps.update(overrides)
    return stamps


def _row(**overrides):
    return {"refparcela": "A", "status": "ok", **_stamps(**overrides)}


def test_resume_is_allowed_when_the_inputs_match():
    sr.assert_ledger_matches_inputs([_row()], _stamps())    # must not raise


@pytest.mark.parametrize("key", sr.IDENTITY_FIELDS)
def test_resuming_with_any_changed_identity_field_is_refused(key):
    """Every field in the identity describes a different physical or data
    situation; continuing would average two of them into one total."""
    with pytest.raises(sr.InputMismatch) as excinfo:
        sr.assert_ledger_matches_inputs([_row()], _stamps(**{key: "changed"}))
    message = str(excinfo.value)
    assert "REFUSED to resume" in message
    assert key in message


def test_an_unstamped_ledger_is_refused():
    """Deliberately inverted on 2026-07-30.

    Tolerating unstamped rows was how a ledger written under one profile could
    be continued under another - the Benicalap ledger really was produced by a
    different profile than the one running today, and nothing said so.
    """
    old = {"refparcela": "A", "status": "ok", "policy_fingerprint": "pol-1"}
    with pytest.raises(sr.InputMismatch, match="predate run identity"):
        sr.assert_ledger_matches_inputs([old], _stamps())


# ---------------------------------------------------------------------------
# The weather file has to reach EnergyPlus, not only the model
# ---------------------------------------------------------------------------
def test_run_energyplus_takes_the_weather_file_as_an_argument():
    """Measured 2026-07-30: `--weather` overrides whatever the model carries, so
    a scenario EPW written into the OSM alone changed nothing - an EPW with every
    hour +5 K gave a byte-identical result while eplusout.eio named VALENCIA."""
    import inspect
    import run_simulation as sim
    parameters = inspect.signature(sim.run_energyplus).parameters
    assert "epw_path" in parameters
    assert parameters["epw_path"].default is None      # default = project weather


class _FakeClimate:
    name = "test_climate"
    epw_path = Path("/tmp/other.epw")
    fingerprint = "climate-fp"
    design_days: dict = {}
    barometric_pressure_pa = 100582.0
    ground_temperature_c = 18.0
    water_mains_temperature_c = 10.0


def _stub_deep_chain(monkeypatch, seen: dict):
    """Mock the chain up to the EnergyPlus call and capture its weather argument."""
    import pandas as pd
    import run_simulation as sim

    row = pd.Series({"pob_total": 10.0, "num_vivend": 4.0, "cluster": "X",
                     "geometry": _square(0, 20)})
    monkeypatch.setattr(db, "load_building_row", lambda *a, **k: row)
    monkeypatch.setattr(mb, "clean_polygon", lambda g: g)
    monkeypatch.setattr(mb, "load_neighbors", lambda *a, **k: [])
    monkeypatch.setattr(mb, "find_party_walls", lambda *a, **k: None)

    stats = {
        "deep_layers": {"occupancy": {"m2_per_person": 40.0},
                        "dhw": {"dhw_litres_per_day": 280.0},
                        "hvac": {"heating_cop": 4.07, "cooling_cop": 5.0}},
        "occupancy_plausibility": "plausible",
        "footprint_m2": 400.0, "n_floors_total": 5,
        "res_area_m2": 1000.0, "total_conditioned_area_m2": 1200.0,
    }
    monkeypatch.setattr(db, "build_deep_model", lambda *a, **k: (object(), stats))

    def fake_run(osm, run_dir, epw_path=None):
        seen["epw_path"] = epw_path
        raise RuntimeError("stop after the weather argument")

    monkeypatch.setattr(sim, "run_energyplus", fake_run)


def test_deep_chain_passes_the_climate_epw_to_energyplus(monkeypatch, tmp_path):
    seen: dict = {}
    _stub_deep_chain(monkeypatch, seen)
    # `match` is deliberate: it fails if anything ELSE raised first, which is how
    # the first version of this test passed while proving nothing
    with pytest.raises(RuntimeError, match="stop after the weather argument"):
        db.simulate_deep_building("X", tmp_path, climate=_FakeClimate())
    assert seen["epw_path"] == Path("/tmp/other.epw")


def test_deep_chain_without_a_climate_leaves_the_weather_alone(monkeypatch, tmp_path):
    seen: dict = {}
    _stub_deep_chain(monkeypatch, seen)
    with pytest.raises(RuntimeError, match="stop after the weather argument"):
        db.simulate_deep_building("X", tmp_path)
    assert seen["epw_path"] is None      # -> the project default, as before


# ---------------------------------------------------------------------------
# Prepared-stock cache identity
# ---------------------------------------------------------------------------
def test_prepared_stock_key_covers_the_source_datasets(tmp_path):
    """Keying on the policy alone reused a previous dataset's prepared file while
    the scope came from the new one - correct in memory, stale on disk, silent."""
    gis_a, gis_b = tmp_path / "a.shp", tmp_path / "b.shp"
    tipo = tmp_path / "t.csv"
    gis_a.write_bytes(b"dataset A")
    gis_b.write_bytes(b"dataset B")
    tipo.write_bytes(b"dwellings")

    same = sr.stock_source_fingerprint(gis_a, tipo, "policy-1")
    assert sr.stock_source_fingerprint(gis_a, tipo, "policy-1") == same
    assert sr.stock_source_fingerprint(gis_b, tipo, "policy-1") != same
    assert sr.stock_source_fingerprint(gis_a, tipo, "policy-2") != same

    tipo.write_bytes(b"dwellings edited")
    assert sr.stock_source_fingerprint(gis_a, tipo, "policy-1") != same


def test_prepared_stock_paths_differ_per_source(tmp_path):
    a = sr.prepared_stock_path("a" * 64, tmp_path)
    b = sr.prepared_stock_path("b" * 64, tmp_path)
    assert a != b and a.suffix == ".gpkg"


# ---------------------------------------------------------------------------
# Coverage: a total is not the district's energy
# ---------------------------------------------------------------------------
def test_coverage_counts_buildings_and_says_what_totals_mean():
    rows = [{"refparcela": "A", "status": "ok"},
            {"refparcela": "B", "status": "ok"},
            {"refparcela": "C", "status": "excluded"},
            {"refparcela": "D", "status": "failed"}]
    ok = [r for r in rows if r["status"] == "ok"]
    block = sr.coverage_block(rows, ok, None)
    assert block["buildings_in_scope"] == 4
    assert block["buildings_with_result"] == 2
    assert block["buildings_without_result"] == 2
    assert block["building_coverage_pct"] == 50.0
    assert "under-reports" in block["note"]


def test_coverage_reports_the_footprint_share_not_just_the_count():
    """The geometry gates fall hardest on large buildings, so a 95 % building
    coverage can still be an 86 % footprint coverage (measured on Benicalap)."""
    stock = _stock([
        {"refparcela": "SMALL", "footprint_area_m2": 100.0, "geometry": _square(0, 10)},
        {"refparcela": "BIG", "footprint_area_m2": 900.0, "geometry": _square(50, 30)},
    ])
    rows = [{"refparcela": "SMALL", "status": "ok"},
            {"refparcela": "BIG", "status": "excluded"}]
    block = sr.coverage_block(rows, [rows[0]], stock)
    assert block["building_coverage_pct"] == 50.0
    assert block["footprint_coverage_pct"] == 10.0


def test_aggregate_carries_coverage_even_when_nothing_succeeded():
    report = sr.aggregate([{"refparcela": "A", "status": "failed"}], None)
    assert report["buildings_ok"] == 0
    assert report["coverage"]["buildings_with_result"] == 0
    assert report["buildings_failed"] == 1


# ---------------------------------------------------------------------------
# Stall visibility
# ---------------------------------------------------------------------------
def test_watchdog_reports_only_what_is_actually_slow():
    import time as _time
    watchdog = sr._StallWatchdog({}, slow_seconds=10.0)
    watchdog._started = {"SLOW": _time.time() - 60, "FRESH": _time.time()}
    slow = dict(watchdog.slow_now())
    assert "SLOW" in slow and "FRESH" not in slow


def test_watchdog_forgets_a_building_once_it_finishes():
    import time as _time
    watchdog = sr._StallWatchdog({}, slow_seconds=10.0)
    watchdog._started = {"SLOW": _time.time() - 60}
    watchdog.finished("SLOW")
    assert watchdog.slow_now() == []


def test_watchdog_does_not_start_a_thread_when_disabled():
    watchdog = sr._StallWatchdog({}, slow_seconds=0.0)
    watchdog.start()
    assert not watchdog._thread.is_alive()
    watchdog.stop()


# ---------------------------------------------------------------------------
# The TABULA cluster envelope actually reaches the physics
# ---------------------------------------------------------------------------
def test_every_cluster_resolves_its_own_period_envelope():
    """Until 2026-07-30 the deep chain handed DEFAULT_BUILD_CONFIG to every
    building: 21 representatives came out with 1 distinct wall and 1 distinct
    roof, so the clusters differed by label only."""
    resolved = {}
    for family in ("VivUni", "EdiPluri", "BlocPluri"):
        for period in ("P01", "P02", "P03", "P04", "P05", "P06"):
            cluster = f"{family}{period}"
            config = db.config_for_building({"cluster": cluster})
            resolved[cluster] = (config.envelope.wall_u, config.envelope.roof_u,
                                 config.envelope.window_u)

    assert all(u is not None for triple in resolved.values() for u in triple)
    # the periods are an insulation timeline: the oldest must be the leakiest
    for family in ("VivUni", "EdiPluri", "BlocPluri"):
        assert resolved[f"{family}P01"][0] > resolved[f"{family}P06"][0], family
    # and the families are not interchangeable
    assert resolved["EdiPluriP04"][0] != resolved["BlocPluriP04"][0]
    # Near-distinct envelopes over 18 clusters; the floor allows for the
    # duplicates each source carries of its own accord (the IVE table has
    # VivUniP02 == P03, Rai's has none) without pinning either one's count.
    assert len(set(resolved.values())) >= 15


def test_p07_maps_onto_p06():
    assert (db.config_for_building({"cluster": "BlocPluriP07"}).envelope.wall_u
            == db.config_for_building({"cluster": "BlocPluriP06"}).envelope.wall_u)


def test_ground_stays_unconditioned_at_build_time_for_every_cluster():
    """Rai's ground regime is applied post-build and needs that space to exist.

    `config_for_profile` would set ground_unconditioned=False for VivUni; the
    override is deliberate (user decision 2026-07-30, Rai's regime for every
    typology) and must hold for all of them.
    """
    for cluster in ("VivUniP01", "VivUniP06", "EdiPluriP03", "BlocPluriP04"):
        assert db.config_for_building({"cluster": cluster}).geometry.ground_unconditioned


def test_a_caller_that_pinned_the_envelope_keeps_it():
    """The Rai replica builds a row with no cluster and states his U-values."""
    pinned = mb.DEFAULT_BUILD_CONFIG.model_copy(deep=True)
    pinned.envelope.wall_u, pinned.envelope.roof_u = 1.37, 2.48
    assert db.config_for_building({}, base=pinned) is pinned


def test_a_building_without_a_cluster_is_refused():
    with pytest.raises(KeyError, match="refusing to fall back"):
        db.config_for_building({"cluster": ""})


# ---------------------------------------------------------------------------
# A failed QA gate is not a result
# ---------------------------------------------------------------------------
def test_qa_failure_is_not_counted_as_ok(tmp_path, monkeypatch):
    """The chain advertises 'a failed gate fails the run'; it used to write the
    building as ok and let its energy into the totals anyway."""
    def fake(refparcela, out_dir, **kwargs):
        return {"total_site_kwh_m2": 55.0, "res_area_m2": 100.0,
                "total_site_co2_kg_m2": 10.0, "qa_all_passed": False}, False

    monkeypatch.setattr(vm, "simulate_verified_building", fake)
    monkeypatch.setitem(sr._WORKER, "out_dir", str(tmp_path))
    worker = {"prepared_gis": "p.gpkg", "context_gis": "c.shp",
              "zero_policy": "literal_zero", "keep": "full", "models_root": None}
    worker.update({key: f"id-{key}" for key in sr.IDENTITY_FIELDS})
    for key, value in worker.items():
        monkeypatch.setitem(sr._WORKER, key, value)

    row = sr.run_one("QAFAIL")
    assert row["status"] == "failed_qa"

    report = sr.aggregate([row], None)
    assert report["buildings_ok"] == 0
    assert report["buildings_failed_qa"] == 1
    assert report["totals"] == {}          # its energy never entered a total


# ---------------------------------------------------------------------------
# Cache identity covers the whole shapefile, not just the .shp
# ---------------------------------------------------------------------------
def test_cache_key_covers_the_attribute_sidecar(tmp_path):
    """altura_max, cluster, pob_total and num_vivend live in the .dbf; hashing
    only the .shp would miss an attribute edit entirely."""
    shp = tmp_path / "stock.shp"
    for suffix, payload in ((".shp", b"geometry"), (".dbf", b"attributes"),
                            (".prj", b"EPSG:25830")):
        shp.with_suffix(suffix).write_bytes(payload)
    tipo = tmp_path / "t.csv"
    tipo.write_bytes(b"dwellings")

    before = sr.stock_source_fingerprint(shp, tipo, "pol")
    shp.with_suffix(".dbf").write_bytes(b"attributes edited")
    assert sr.stock_source_fingerprint(shp, tipo, "pol") != before


def test_cache_key_changes_when_the_preparation_schema_changes(tmp_path, monkeypatch):
    """A GeoPackage written by older code must not be served to newer code -
    exactly how the Tipo15 column silently went missing on 2026-07-30."""
    shp = tmp_path / "stock.shp"
    shp.write_bytes(b"geometry")
    tipo = tmp_path / "t.csv"
    tipo.write_bytes(b"dwellings")

    before = sr.stock_source_fingerprint(shp, tipo, "pol")
    monkeypatch.setattr(sr, "PREPARED_STOCK_SCHEMA", sr.PREPARED_STOCK_SCHEMA + 1)
    assert sr.stock_source_fingerprint(shp, tipo, "pol") != before


def test_pruning_keeps_the_evidence_and_drops_the_bulk(tmp_path):
    run_dir = tmp_path / "X_deep"
    run_dir.mkdir()
    for name in sr.PRUNABLE_ARTIFACTS + sr.KEPT_ARTIFACTS:
        (run_dir / name).write_text("x" * 100, encoding="utf-8")
    freed = sr._prune_run_dir(run_dir)
    assert freed == 100 * len(sr.PRUNABLE_ARTIFACTS)
    for name in sr.KEPT_ARTIFACTS:
        assert (run_dir / name).exists(), name
    for name in sr.PRUNABLE_ARTIFACTS:
        assert not (run_dir / name).exists(), name


def test_eplustbl_is_never_pruned():
    # the human-readable evidence is what makes a run auditable against Rai
    assert "eplustbl.htm" in sr.KEPT_ARTIFACTS
    assert "eplustbl.htm" not in sr.PRUNABLE_ARTIFACTS


def test_openstudio_model_is_never_pruned():
    # it is the artifact a person opens by hand; rebuilding it costs a full run
    assert sr.MODEL_ARTIFACT in sr.KEPT_ARTIFACTS
    assert sr.MODEL_ARTIFACT not in sr.PRUNABLE_ARTIFACTS


def test_full_is_the_default_keep_policy():
    # the CLI and the function must not disagree about what a plain run keeps
    import inspect
    assert inspect.signature(sr.run_stock).parameters["keep"].default == "full"


def test_model_index_hardlinks_by_cluster(tmp_path):
    run_dir = tmp_path / "REF1_deep"
    run_dir.mkdir()
    source = run_dir / sr.MODEL_ARTIFACT
    source.write_text("osm-bytes", encoding="utf-8")

    target = sr.link_model(run_dir, "REF1", "BlocPluriP04", tmp_path / "models")

    assert target is not None
    linked = Path(target)
    assert linked == tmp_path / "models" / "BlocPluriP04" / "REF1.osm"
    assert linked.read_text(encoding="utf-8") == "osm-bytes"
    # a hardlink is the same bytes under a second name, not a second copy
    assert linked.stat().st_ino == source.stat().st_ino


def test_model_index_survives_pruning(tmp_path):
    # summary mode must never orphan the model: the index has to keep it alive
    run_dir = tmp_path / "REF2_deep"
    run_dir.mkdir()
    (run_dir / sr.MODEL_ARTIFACT).write_text("osm", encoding="utf-8")
    for name in sr.PRUNABLE_ARTIFACTS:
        (run_dir / name).write_text("x", encoding="utf-8")

    target = Path(sr.link_model(run_dir, "REF2", "VivUniP03", tmp_path / "models"))
    sr._prune_run_dir(run_dir)

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "osm"


def test_model_index_without_cluster_is_still_filed(tmp_path):
    run_dir = tmp_path / "REF3_deep"
    run_dir.mkdir()
    (run_dir / sr.MODEL_ARTIFACT).write_text("osm", encoding="utf-8")
    target = Path(sr.link_model(run_dir, "REF3", None, tmp_path / "models"))
    assert target.parent.name == "_uncategorised"


def test_model_index_returns_none_when_there_is_no_model(tmp_path):
    run_dir = tmp_path / "REF4_deep"
    run_dir.mkdir()
    assert sr.link_model(run_dir, "REF4", "X", tmp_path / "models") is None


# ---------------------------------------------------------------------------
# Scope selection
# ---------------------------------------------------------------------------
def _scope_stock():
    def square(x, size):
        return Polygon([(x, 0), (x + size, 0), (x + size, size), (x, size)])
    return _stock([
        {"refparcela": "A", "cluster": "BlocPluriP04", "nombre": "BENICALAP",
         "geometry": square(0, 10)},
        {"refparcela": "B", "cluster": "BlocPluriP04", "nombre": "BENICALAP",
         "geometry": square(100, 20)},
        {"refparcela": "C", "cluster": "BlocPluriP04", "nombre": "CAMPANAR",
         "geometry": square(200, 30)},
        {"refparcela": "D", "cluster": "VivUniP02", "nombre": "CAMPANAR",
         "geometry": square(300, 12)},
    ])


def test_scope_all():
    assert len(sr.select_scope(_scope_stock(), "all")) == 4


def test_scope_clusters_picks_one_median_building_per_cluster():
    picked = sr.select_scope(_scope_stock(), "clusters")
    assert set(picked["cluster"]) == {"BlocPluriP04", "VivUniP02"}
    assert len(picked) == 2
    # 400 / 900 m2 -> median 900 is B's own area, the closest match
    assert picked[picked["cluster"] == "BlocPluriP04"]["refparcela"].iloc[0] == "B"


def test_scope_district_is_case_insensitive():
    picked = sr.select_scope(_scope_stock(), "district", district="benicalap")
    assert set(picked["refparcela"]) == {"A", "B"}


def test_scope_district_rejects_an_unknown_name():
    with pytest.raises(ValueError, match="no buildings in district"):
        sr.select_scope(_scope_stock(), "district", district="ATLANTIS")


def test_scope_references():
    picked = sr.select_scope(_scope_stock(), "references", references=["A", "D"])
    assert set(picked["refparcela"]) == {"A", "D"}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _ok_row(ref, cluster, area, intensity, **extra):
    return {"refparcela": ref, "status": "ok", "cluster": cluster,
            "res_area_m2": area, "total_site_kwh_m2": intensity,
            "space_heating_kwh_m2": 2.0, "cooling_kwh_m2": 3.0, "dhw_kwh_m2": 10.0,
            "total_site_co2_kg_m2": 15.0, "qa_all_passed": True,
            "severes_unexplained": 0, "occupancy_plausibility": "plausible",
            "seconds": 50.0, **extra}


def test_aggregate_totals_are_area_weighted():
    rows = [_ok_row("A", "BlocPluriP04", 1000.0, 40.0),
            _ok_row("B", "BlocPluriP04", 3000.0, 60.0)]
    report = sr.aggregate(rows)
    # 1000*40 + 3000*60 = 220 000 kWh
    assert report["totals"]["total_site_gwh"] == pytest.approx(0.22, abs=1e-6)
    assert report["totals"]["residential_area_m2"] == 4000.0
    # area weighted, not the plain mean of 50
    assert report["totals"]["area_weighted_total_site_kwh_m2"] == pytest.approx(55.0)


def test_aggregate_does_not_duplicate_a_ledger_row_for_multi_footprint_stock():
    rows = [_ok_row("A", "BlocPluriP04", 1000.0, 40.0)]
    stock = pd.DataFrame([
        {"refparcela": "A", "cluster": "BlocPluriP04", "nombre": "Benicalap", "footprint_area_m2": 60.0},
        {"refparcela": "A", "cluster": "BlocPluriP04", "nombre": "Benicalap", "footprint_area_m2": 40.0},
    ])
    report = sr.aggregate(rows, stock)
    assert report["buildings_ok"] == 1
    assert report["totals"]["total_site_gwh"] == pytest.approx(0.04)
    assert report["by_cluster"][0]["cluster"] == "BlocPluriP04"


def test_aggregate_uses_the_thesis_reference_not_the_shapefile_rounding():
    """Ilustración 31 (thesis p. 95) carries two decimals; the shapefile rounds.

    Two shapefile values are also wrong - EdiPluriP02 is 0 there while the
    thesis's own demand table gives that cluster 15.0 GWh/yr over 1,404
    buildings - so the thesis is the source of record (2026-08-04).
    """
    assert sr.RAI_CLUSTER_CONSUME["BlocPluriP04"] == pytest.approx(46.55)
    assert sr.RAI_CLUSTER_CONSUME["EdiPluriP02"] == pytest.approx(52.02)
    assert sr.RAI_CLUSTER_CONSUME_SHAPEFILE["EdiPluriP02"] == 0
    # the 7 VivUni clusters - 5,262 buildings - used to be missing entirely
    assert len(sr.RAI_CLUSTER_CONSUME) == 21
    assert sr.RAI_CLUSTER_CONSUME["VivUniP01"] == pytest.approx(62.57)


def test_aggregate_compares_against_rai_on_the_cadastral_basis():
    """Rai's constants are per m2 of cadastral dwelling area, not per m2 of the
    geometric storey area we condition, so the comparison runs on his basis."""
    # 1000 m2 geometric at 46.55 kWh/m2 = 46 550 kWh, over 1000 m2 cadastral
    rows = [_ok_row("A", "BlocPluriP04", 1000.0, 46.55,
                    tipo15_res_area_m2=1000.0)]
    block = sr.aggregate(rows)["by_cluster"][0]
    assert block["rai_consume_kwh_m2"] == pytest.approx(46.55)
    assert block["cadastral_kwh_m2"] == pytest.approx(46.55)
    assert block["vs_rai_pct"] == pytest.approx(0.0)
    assert block["vs_rai_energy_ratio"] == pytest.approx(1.0)


def test_aggregate_deviation_follows_the_cadastral_area_not_the_geometric_one():
    # same energy, cadastral area half the geometric one -> the intensity Rai's
    # constant must be read against doubles
    rows = [_ok_row("A", "BlocPluriP04", 1000.0, 46.55,
                    tipo15_res_area_m2=500.0)]
    block = sr.aggregate(rows)["by_cluster"][0]
    assert block["area_weighted_kwh_m2"] == pytest.approx(46.55)   # geometric
    assert block["cadastral_kwh_m2"] == pytest.approx(93.10)       # Rai's basis
    assert block["vs_rai_pct"] == pytest.approx(100.0)
    assert block["vs_rai_energy_ratio"] == pytest.approx(2.0)


def test_aggregate_reports_no_deviation_without_a_cadastral_area():
    """Silence beats a number on the wrong basis: no Tipo15 area, no vs-Rai."""
    rows = [_ok_row("A", "BlocPluriP04", 1000.0, 46.55)]
    block = sr.aggregate(rows)["by_cluster"][0]
    assert block["vs_rai_pct"] is None
    assert block["vs_rai_energy_ratio"] is None


def test_aggregate_counts_every_status():
    rows = [_ok_row("A", "BlocPluriP04", 100.0, 40.0),
            {"refparcela": "B", "status": "failed", "reason": "ValueError"},
            {"refparcela": "C", "status": "excluded", "reason": "interior_rings_1"}]
    report = sr.aggregate(rows)
    assert (report["buildings_ok"], report["buildings_failed"],
            report["buildings_excluded"]) == (1, 1, 1)


def test_aggregate_surfaces_quality_flags():
    rows = [_ok_row("A", "BlocPluriP04", 100.0, 40.0, qa_all_passed=False),
            _ok_row("B", "BlocPluriP04", 100.0, 40.0, severes_unexplained=3),
            _ok_row("C", "BlocPluriP04", 100.0, 40.0,
                    occupancy_plausibility="implausible_dense_capped")]
    report = sr.aggregate(rows)
    assert report["qa_failed"] == 1
    assert report["unexplained_severes"] == 1
    assert report["implausible_occupancy"] == 1


def test_aggregate_on_an_empty_ledger():
    assert sr.aggregate([])["buildings_ok"] == 0


def test_aggregate_counts_buildings_not_ledger_rows():
    """A retried building holds several rows; only the last one is the truth.

    Counting rows inflated the Benicalap failure count from 2 to 4 (2026-07-28),
    and would double-count energy if a retry succeeded after a failure.
    """
    rows = [
        {"refparcela": "A", "status": "failed", "reason": "ValueError"},
        {"refparcela": "A", "status": "failed", "reason": "ValueError"},
        {"refparcela": "B", "status": "failed", "reason": "RuntimeError"},
        _ok_row("B", "BlocPluriP04", 1000.0, 40.0),      # succeeded on retry
        _ok_row("C", "BlocPluriP04", 1000.0, 40.0),
    ]
    report = sr.aggregate(rows)
    assert report["buildings_failed"] == 1               # only A
    assert report["buildings_ok"] == 2                   # B and C, B counted once
    assert report["totals"]["residential_area_m2"] == 2000.0


def test_latest_per_reference_keeps_the_last_row():
    rows = [{"refparcela": "A", "status": "failed"},
            {"refparcela": "A", "status": "ok"}]
    assert sr.latest_per_reference(rows) == [{"refparcela": "A", "status": "ok"}]


# ---------------------------------------------------------------------------
# The run refuses to start on a drifted profile
# ---------------------------------------------------------------------------
def test_drifted_profile_stops_the_run_before_any_building(tmp_path, monkeypatch):
    def drift(*args, **kwargs):
        raise vm.ProfileDrift("PTHP_HEATING_COP moved")

    monkeypatch.setattr(vm, "assert_profile_intact", drift)
    called = {"prepare": False}
    monkeypatch.setattr(sr, "prepare_stock_file",
                        lambda *a, **k: called.__setitem__("prepare", True))
    with pytest.raises(vm.ProfileDrift):
        sr.run_stock(scope="all", out_dir=tmp_path, workers=1,
                     gis_path=Path("x.shp"), tipo15_path=Path("y.csv"),
                     var_dir=tmp_path)
    assert called["prepare"] is False


# ---------------------------------------------------------------------------
# Real buildings
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_runner_reproduces_the_single_building_path(tmp_path):
    """The runner must not become a second, subtly different engine."""
    report = sr.run_stock(
        scope="references", references=[PILOT], out_dir=tmp_path / "run",
        workers=1, gis_path=Path(mb.NEIGHBORS_SHP),
        tipo15_path=Path(mb._project_root()) / "data/reference/Tipo15_soloV(in).csv",
        var_dir=tmp_path / "var", keep="full")
    assert report["buildings_ok"] == 1
    assert report["buildings_failed"] == 0

    row = [r for r in sr.read_ledger(tmp_path / "run" / "ledger.jsonl")
           if r["refparcela"] == PILOT][0]
    assert row["qa_all_passed"] is True
    # Measured 2026-08-06, on Rai's own envelope (ENVELOPE_SOURCE = "rai").
    # The pilot is BlocPluriP04: his wall is 1.369 against the IVE 1.33 and his
    # roof 2.479 against 1.92, so both directions of the envelope got leakier
    # and heating and cooling both rise.  The roof carries it - 562 m2 at
    # +0.559 W/m2K is about 314 W/K, against roughly 39 W/K from the walls.
    # DHW does not move, because it never depended on the envelope.
    # Earlier baselines: 2.22 / 2.71 / 50.12 on the IVE table (2026-07-30), and
    # 2.26 / 2.73 / 50.19 before any cluster envelope was connected at all.
    assert row["space_heating_kwh_m2"] == pytest.approx(2.74, abs=0.01)
    assert row["cooling_kwh_m2"] == pytest.approx(3.00, abs=0.01)
    assert row["dhw_kwh_m2"] == pytest.approx(10.32, abs=0.01)
    assert row["total_site_kwh_m2"] == pytest.approx(50.99, abs=0.01)
    assert row["profile_fingerprint"] == vm.profile_fingerprint()
    # the two area bases travel side by side and are never conflated
    assert row["res_area_m2"] == pytest.approx(2809.9, abs=0.1)
    assert row["tipo15_res_area_m2"] == pytest.approx(2910.0, abs=0.1)


# ---------------------------------------------------------------------------
# Footprint ceiling and the single-zone caveat
#
# The ceiling was raised from 5 000 to 20 000 m2 on 2026-08-03.  At 5 000 the
# gate excluded 123 Valencia buildings carrying 11.55 % of the city's floor
# area, so a "city total" quietly stood for 87.5 % of the stock.  Raising it
# admits them; these tests keep the admission honest.
# ---------------------------------------------------------------------------
def test_footprint_range_constant_cannot_go_stale():
    """`mb.FOOTPRINT_RANGE` is derived, never restated.

    It sat at a literal (50.0, 5000.0) while the real gate in prepare_footprint
    read the config, so raising the ceiling would have left a wrong pair in the
    frozen builder for the next reader to believe.
    """
    geometry = mb.DEFAULT_BUILD_CONFIG.geometry
    assert mb.FOOTPRINT_RANGE == (geometry.footprint_min_m2,
                                  geometry.footprint_max_m2)
    assert sr.footprint_limits() == mb.FOOTPRINT_RANGE


def test_footprint_ceiling_admits_large_blocks():
    """The size gate no longer turns anyone away for being large or small.

    Measured 2026-08-22 over the whole stock: the 20 000 m2 ceiling was costing
    4 buildings and 0.885 % of the city's footprint area, and the 50 m2 floor was
    costing 538 buildings that every one of them carries a cadastral dwelling
    record for.  Both were widened.  Being large is now recorded by the
    single-zone flag rather than punished by exclusion - which is why that flag
    is pinned in test_deep_building.py and must not be widened with the gate.
    """
    low, high = sr.footprint_limits()
    assert low == 20.0 and high == 1000000.0
    assert low < 17272.0 < high            # 3748901YJ2734H, 15 storeys
    assert low < 32172.0 < high            # 2405201YJ2820E - was excluded, now runs
    # the honesty now rests entirely on the flag, not on the gate
    assert db.LARGE_FOOTPRINT_SINGLE_ZONE_M2 < high


def test_zoning_block_reports_the_share_resting_on_one_zone():
    """The caveat is a share of area and energy, not just a building count."""
    rows = [_ok_row("small", "BlocPluriP04", 100.0, 50.0,
                    large_footprint_single_zone=False),
            _ok_row("large", "BlocPluriP06", 900.0, 50.0,
                    large_footprint_single_zone=True)]
    zoning = sr.aggregate(rows)["zoning"]

    assert zoning["threshold_m2"] == db.LARGE_FOOTPRINT_SINGLE_ZONE_M2
    assert zoning["buildings"] == 1
    assert zoning["buildings_pct"] == pytest.approx(50.0)
    # one building in two, but nine tenths of the area and of the energy: the
    # count alone would badly understate what rests on the weaker assumption
    assert zoning["residential_area_pct"] == pytest.approx(90.0)
    assert zoning["total_site_pct"] == pytest.approx(90.0)


def test_zoning_block_survives_an_empty_and_a_legacy_ledger():
    """Older ledgers have no flag; the block degrades instead of raising."""
    assert sr.aggregate([])["zoning"]["threshold_m2"] == \
        db.LARGE_FOOTPRINT_SINGLE_ZONE_M2
    legacy = sr.aggregate([_ok_row("A", "BlocPluriP04", 100.0, 50.0)])["zoning"]
    assert "buildings" not in legacy          # nothing invented from absence


def test_large_footprint_flag_is_carried_into_the_ledger():
    assert "large_footprint_single_zone" in sr.LEDGER_METRICS


# ---------------------------------------------------------------------------
# Review findings, 2026-08-03
# ---------------------------------------------------------------------------
def test_aggregate_reports_instead_of_crashing_when_nothing_succeeded():
    """aggregate([]) and an all-failed ledger used to KeyError in _print_report."""
    for rows in ([], [{"refparcela": "A", "status": "failed", "reason": "X"}]):
        report = sr.aggregate(rows)
        sr._print_report(report)          # must not raise
        assert report["buildings_ok"] == 0
        assert report["qa_failed"] == 0
        assert report["unexplained_severes"] == 0
        assert report["totals"] == {}
        assert report["by_cluster"] == []


def test_worker_crash_row_is_stamped_and_the_ledger_stays_resumable():
    """A dead worker's row must carry the same identity as every other row.

    Unstamped, assert_ledger_matches_inputs rightly refused the whole ledger -
    one crash cost the remaining days of a stock run.
    """
    fingerprints = {key: f"value-{key}" for key in sr.IDENTITY_FIELDS}
    ok_row = {"refparcela": "A", "status": "ok", **fingerprints}
    crash_row = {"refparcela": "B", "status": "failed",
                 "reason": "worker_BrokenProcessPool", **fingerprints}
    # both rows present: resume must accept the ledger
    sr.assert_ledger_matches_inputs([ok_row, crash_row], fingerprints)
    # and an unstamped crash row must still be refused - the guard is the point
    with pytest.raises(sr.InputMismatch, match="predate run identity"):
        sr.assert_ledger_matches_inputs(
            [ok_row, {"refparcela": "C", "status": "failed"}], fingerprints)


def test_provenance_block_names_its_ledgers_and_the_merge_rule(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    fingerprints = {key: f"value-{key}" for key in sr.IDENTITY_FIELDS}
    rows = [{"refparcela": "A", "status": "ok", **fingerprints}]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    block = sr.provenance_block(rows, [ledger])
    assert block["identity"]["run_identity"] == "value-run_identity"
    assert block["source_ledgers"][0]["sha256"] == sr.file_sha256(ledger)
    assert block["source_ledgers"][0]["rows"] == 1
    assert "latest_per_reference" in block["merge_rule"]


def test_the_prepared_file_itself_carries_the_ground_columns(tmp_path, monkeypatch):
    """The FILE the workers read, not the frame the policy returned.

    prepare_stock resolved ground_use correctly while the written GeoPackage
    silently dropped it through the column allowlist, so the first v5 launch
    ran with terciario everywhere (2026-08-03).  The codebase's own rule -
    verify from the engine's output, not from what you wrote into it - applies
    to data artifacts too.
    """
    import geopandas as gpd_mod
    import pandas as pd
    from shapely.geometry import box
    import stock_input_policy as sip_mod

    gis = tmp_path / "stock.gpkg"
    gpd_mod.GeoDataFrame({
        "refparcela": ["AAA"], "nombre": ["ONE"], "altura_max": [2],
        "cluster": ["BlocPluriP04"], "Shape_Area": [100.0],
    }, geometry=[box(0, 0, 10, 10)], crs="EPSG:25830").to_file(gis, driver="GPKG")
    tipo15 = tmp_path / "tipo15.csv"
    pd.DataFrame({"31_pc": ["AAA"], "442_sup_Residencial": [150.0],
                  "252_planta": ["B0"]}).to_csv(
        tipo15, sep=";", encoding="latin-1", index=False)

    out, _, _ = sr.prepare_stock_file(
        gis, tipo15, sip_mod.StockInputPolicy(), tmp_path / "var")
    written = gpd_mod.read_file(out)
    assert "ground_use" in written.columns
    assert "ground_use_source" in written.columns
    assert written.loc[0, "ground_use"] == "residential"      # B0 is a ground code


def test_the_prepared_file_and_the_returned_frame_carry_the_same_names(tmp_path):
    """One name for one quantity, whichever way a reader reaches the stock.

    A live run passes the RETURNED FRAME to `aggregate()`; re-aggregating an
    existing ledger reads the WRITTEN FILE.  While the cadastral-area rename
    was applied only to the copy being written, those two paths disagreed:
    the coverage-bias block, which asks for `tipo15_res_area_m2`, measured on
    the re-aggregation and reported "no cadastral area column" on the live run
    (Benicalap v9).  Checking the file alone could not see it - the file was
    right - so this holds the two to each other.
    """
    import geopandas as gpd_mod
    import pandas as pd
    from shapely.geometry import box
    import stock_input_policy as sip_mod

    gis = tmp_path / "stock.gpkg"
    gpd_mod.GeoDataFrame({
        "refparcela": ["AAA"], "nombre": ["ONE"], "altura_max": [2],
        "cluster": ["BlocPluriP04"], "Shape_Area": [100.0],
    }, geometry=[box(0, 0, 10, 10)], crs="EPSG:25830").to_file(gis, driver="GPKG")
    tipo15 = tmp_path / "tipo15.csv"
    pd.DataFrame({"31_pc": ["AAA"], "442_sup_Residencial": [150.0],
                  "252_planta": ["B0"]}).to_csv(
        tipo15, sep=";", encoding="latin-1", index=False)

    out, frame, _ = sr.prepare_stock_file(
        gis, tipo15, sip_mod.StockInputPolicy(), tmp_path / "var")
    written = gpd_mod.read_file(out)

    assert "tipo15_res_area_m2" in written.columns
    assert "tipo15_res_area_m2" in frame.columns
    # The geometric name must not survive on either side: it means a different
    # quantity downstream, and the whole point of the rename is that the two
    # never share a column name.
    assert "res_area_m2" not in written.columns
    assert "res_area_m2" not in frame.columns
    assert float(frame.loc[0, "tipo15_res_area_m2"]) == pytest.approx(
        float(written.loc[0, "tipo15_res_area_m2"]))


def test_the_returned_frame_lets_the_bias_be_measured(tmp_path):
    """The frame a live run hands to `aggregate()` can answer the bias question.

    The bound on whether the excluded buildings skew the published intensity is
    produced from the stock the run holds in memory.  A full-city run that
    silently could not produce it would leave the very question it was written
    to answer (Javier's, 2026-08-12) unanswered.
    """
    import geopandas as gpd_mod
    import pandas as pd
    from shapely.geometry import box
    import stock_input_policy as sip_mod
    import coverage_bias as cb_mod

    gis = tmp_path / "stock.gpkg"
    gpd_mod.GeoDataFrame({
        "refparcela": ["AAA", "BBB"], "nombre": ["ONE", "ONE"],
        "altura_max": [2, 2], "cluster": ["BlocPluriP04", "BlocPluriP04"],
        "Shape_Area": [100.0, 100.0],
    }, geometry=[box(0, 0, 10, 10), box(20, 0, 30, 10)],
        crs="EPSG:25830").to_file(gis, driver="GPKG")
    tipo15 = tmp_path / "tipo15.csv"
    pd.DataFrame({"31_pc": ["AAA", "BBB"],
                  "442_sup_Residencial": [150.0, 150.0],
                  "252_planta": ["B0", "B0"]}).to_csv(
        tipo15, sep=";", encoding="latin-1", index=False)

    _, frame, _ = sr.prepare_stock_file(
        gis, tipo15, sip_mod.StockInputPolicy(), tmp_path / "var")

    rows = [{"refparcela": "AAA", "status": "ok", "total_site_kwh_m2": 50.0,
             "res_area_m2": 200.0},
            {"refparcela": "BBB", "status": "excluded", "reason": "footprint"}]
    ok = [r for r in rows if r["status"] == "ok"]
    block = cb_mod.bias_block(rows, ok, frame)
    assert block["measured"] is True, block.get("reason")
