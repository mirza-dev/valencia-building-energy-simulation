"""Tests for the validated climate bundle.

The point of `src/climate.py` is that a wrong climate value cannot reach a
simulation silently, so most of these tests are refusals: each one takes a
consistent bundle and breaks exactly one thing.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import climate as cl  # noqa: E402

REAL_EPW = PROJECT_ROOT / "data" / "weather" / "ESP_Valencia.082840_IWEC.epw"
REAL_DDY = PROJECT_ROOT / "data" / "weather" / "ESP_Valencia.082840_IWEC.ddy"

HEATING_DAY = "VALENCIA Ann Htg 99.6% Condns DB"
COOLING_DAY = "VALENCIA Ann Clg .4% Condns DB=>MWB"


# ---------------------------------------------------------------------------
# Fixtures - a working bundle in a scratch directory, ready to be broken
# ---------------------------------------------------------------------------
def _epw_header_only(destination: Path, source: Path = REAL_EPW,
                     lines: int = 8) -> Path:
    """Copy just an EPW's header.

    `read_epw_header` never looks past the first few lines, so the 8 760 data
    rows are 1.5 MB of nothing for these tests.
    """
    with open(source, "r", encoding="latin-1") as handle:
        head = [handle.readline() for _ in range(lines)]
    destination.write_text("".join(head), encoding="latin-1")
    return destination


@pytest.fixture
def bundle(tmp_path: Path):
    """A valid bundle whose files can be edited without touching the project."""
    epw = _epw_header_only(tmp_path / "test.epw")
    ddy = tmp_path / "test.ddy"
    shutil.copy2(REAL_DDY, ddy)

    spec = {
        "name": "test_climate",
        "epw": epw.name,
        "ddy": ddy.name,
        "heating_design_day": HEATING_DAY,
        "cooling_design_day": COOLING_DAY,
        "ground_temperature_c": 18.0,
        "water_mains_temperature_c": 10.0,
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    def rewrite(**changes):
        spec.update(changes)
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    rewrite.path = path          # type: ignore[attr-defined]
    rewrite.epw = epw            # type: ignore[attr-defined]
    rewrite.ddy = ddy            # type: ignore[attr-defined]
    return rewrite


# ---------------------------------------------------------------------------
# The neutrality lock
# ---------------------------------------------------------------------------
def test_valencia_bundle_reproduces_the_verified_constants():
    """The shipped bundle must reproduce, exactly, the values every verified
    number in this project was produced with.

    These literals are written out rather than imported from `deep_building` on
    purpose: this test pins the bundle to the *verified result*, so it still
    fails if both the bundle and the code drift together.
    """
    climate = cl.valencia_iwec()

    assert climate.ground_temperature_c == 18.0
    assert climate.water_mains_temperature_c == 10.0
    assert climate.barometric_pressure_pa == 100582.0

    heating = climate.design_days["heating"]
    assert heating["month"] == 1 and heating["day"] == 21
    assert heating["db"] == 1.0
    assert heating["range"] == 0.0
    assert heating["wb"] == 1.0
    assert heating["day_type"] == "WinterDesignDay"
    assert heating["wind_speed"] == 2.0
    assert heating["wind_direction"] == 280.0
    assert heating["solar_model"] == "ASHRAEClearSky"
    assert heating["clearness"] == 0.0

    cooling = climate.design_days["cooling"]
    assert cooling["month"] == 8 and cooling["day"] == 21
    assert cooling["db"] == 33.1
    assert cooling["range"] == 9.4
    assert cooling["wb"] == 21.4
    assert cooling["day_type"] == "SummerDesignDay"
    assert cooling["wind_speed"] == 5.2
    assert cooling["wind_direction"] == 120.0
    assert cooling["solar_model"] == "ASHRAETau"
    assert cooling["taub"] == 0.505
    assert cooling["taud"] == 1.864


def test_valencia_site_comes_from_the_epw_header():
    # EnergyPlus takes the site from the weather file - no Site:Location is
    # written - so these are the coordinates the simulation actually runs at
    site = cl.valencia_iwec().site
    assert site["city"] == "VALENCIA"
    assert site["latitude"] == 39.5
    assert site["longitude"] == -0.47
    assert site["elevation_m"] == 62.0
    assert site["time_zone"] == 1.0


def test_valencia_bundle_cross_checks_against_the_epw_header():
    assert cl.valencia_iwec().cross_check == "epw_design_conditions"


def test_valencia_is_cached_per_process():
    # the stock runner loads this once per worker, not once per building
    assert cl.valencia_iwec() is cl.valencia_iwec()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_mismatched_epw_and_ddy_are_refused(bundle, tmp_path):
    """The most damaging mistake: files from two different stations.

    It produces a building simulated in one city with equipment sized for
    another, and nothing downstream looks broken.
    """
    text = bundle.epw.read_text(encoding="latin-1").splitlines(keepends=True)
    fields = text[0].split(",")
    fields[6] = "40.42"          # Madrid's latitude on Valencia's weather file
    text[0] = ",".join(fields)
    bundle.epw.write_text("".join(text), encoding="latin-1")

    with pytest.raises(cl.ClimateError, match="different sites"):
        cl.load_climate(bundle.path)


def test_unknown_design_day_name_lists_what_is_available(bundle):
    path = bundle(heating_design_day="VALENCIA Ann Htg 42% Condns DB")
    with pytest.raises(cl.ClimateError) as excinfo:
        cl.load_climate(path)
    message = str(excinfo.value)
    assert "no design day named" in message
    # the error has to be actionable, not just a refusal
    assert COOLING_DAY in message


def test_edited_design_day_contradicting_the_epw_is_refused(bundle):
    """One file edited, the other not - exactly the case a hash alone misses."""
    text = bundle.ddy.read_text(encoding="latin-1")
    assert "33.1" in text
    bundle.ddy.write_text(text.replace("33.1", "38.0"), encoding="latin-1")

    with pytest.raises(cl.ClimateError) as excinfo:
        cl.load_climate(bundle.path)
    message = str(excinfo.value)
    assert "DESIGN CONDITIONS" in message
    assert "dry bulb 0.4 %" in message


@pytest.mark.parametrize("missing", ["ground_temperature_c",
                                     "water_mains_temperature_c"])
def test_missing_site_temperatures_are_refused(bundle, missing):
    """These are climate-dependent and cannot be read from an EPW, so a bundle
    that omits them must fail rather than inherit Valencia's value."""
    spec = json.loads(bundle.path.read_text(encoding="utf-8"))
    del spec[missing]
    bundle.path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(cl.ClimateError) as excinfo:
        cl.load_climate(bundle.path)
    assert missing in str(excinfo.value)
    assert "never" in str(excinfo.value)


def test_default_barometric_pressure_is_refused(bundle):
    """OpenStudio's 31 000 Pa default is about 9 000 m of altitude.

    This is the exact defect found on 2026-07-28; it must now be impossible to
    load a climate carrying it.
    """
    text = bundle.ddy.read_text(encoding="latin-1")
    bundle.ddy.write_text(text.replace("100582", "31000"), encoding="latin-1")

    with pytest.raises(cl.ClimateError) as excinfo:
        cl.load_climate(bundle.path)
    assert "31 000 Pa" in str(excinfo.value)


def test_missing_files_are_refused(bundle):
    path = bundle(epw="does_not_exist.epw")
    with pytest.raises(cl.ClimateError, match="was not found"):
        cl.load_climate(path)


def test_invalid_json_is_refused(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(cl.ClimateError, match="invalid JSON"):
        cl.load_climate(path)


def test_a_file_that_is_not_an_epw_is_refused(bundle):
    bundle.epw.write_text("hello\n", encoding="latin-1")
    with pytest.raises(cl.ClimateError, match="not a valid EPW header"):
        cl.load_climate(bundle.path)


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------
def test_fingerprint_is_stable_for_the_same_inputs(bundle):
    first = cl.load_climate(bundle.path).fingerprint
    second = cl.load_climate(bundle.path).fingerprint
    assert first == second


def test_fingerprint_changes_when_a_site_temperature_changes(bundle):
    before = cl.load_climate(bundle.path).fingerprint
    after = cl.load_climate(bundle(ground_temperature_c=16.0)).fingerprint
    assert before != after


def test_fingerprint_changes_when_the_weather_file_changes(bundle):
    before = cl.load_climate(bundle.path).fingerprint
    with open(bundle.epw, "a", encoding="latin-1") as handle:
        handle.write("COMMENT 3,edited\n")
    assert cl.load_climate(bundle.path).fingerprint != before


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def test_standard_pressure_matches_the_international_standard_atmosphere():
    assert cl._standard_pressure_pa(0.0) == pytest.approx(101325.0, abs=1.0)
    # Valencia station elevation - the .ddy's own 100 582 Pa sits within
    # the tolerance of this, which is what makes the pressure gate meaningful
    assert cl._standard_pressure_pa(62.0) == pytest.approx(100582.0, rel=0.02)


def test_epw_without_design_conditions_needs_an_explicit_declaration(bundle):
    """A morphed future-climate EPW may carry no DESIGN CONDITIONS line.

    It used to sail through with a note nothing acted on, so an EPW with the
    header stripped ran as if fully verified (review finding, 2026-08-03).
    Now the bundle author must declare it, and the declaration is recorded.
    """
    lines = bundle.epw.read_text(encoding="latin-1").splitlines(keepends=True)
    kept = [line for line in lines
            if not line.upper().startswith("DESIGN CONDITIONS")]
    bundle.epw.write_text("".join(kept), encoding="latin-1")

    with pytest.raises(cl.ClimateError, match="allow_unverified_design_days"):
        cl.load_climate(bundle.path)

    climate = cl.load_climate(bundle(allow_unverified_design_days=True))
    assert climate.cross_check == "unverified_by_declaration"


def test_the_declaration_does_not_weaken_a_verifiable_epw(bundle):
    """With DESIGN CONDITIONS present the cross-check still runs and still
    fails on contradiction - the flag only covers the header's absence."""
    climate = cl.load_climate(bundle(allow_unverified_design_days=True))
    assert climate.cross_check == "epw_design_conditions"


def test_record_carries_everything_needed_to_reproduce_a_run(bundle):
    record = cl.load_climate(bundle.path).record()
    for key in ("fingerprint", "epw_sha256", "ddy_sha256", "design_days",
                "ground_temperature_c", "water_mains_temperature_c",
                "barometric_pressure_pa", "site", "cross_check"):
        assert key in record, key
    assert json.dumps(record)          # has to survive being written to a ledger
