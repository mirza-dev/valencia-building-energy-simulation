from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench.api import app
from workbench.environment import TEST_REQUEST_HEADER, validate_runtime_environment


def isolated_environment(root: Path) -> dict[str, str]:
    return {
        "WORKBENCH_ENV": "test",
        "WORKBENCH_PORT": "18766",
        "WORKBENCH_TEST_ROOT": str(root),
        "WORKBENCH_TEST_RUN_ID": "e2e-123",
        "WORKBENCH_TEST_REQUIRE_HEADER": "1",
        "WORKBENCH_VAR_DIR": str(root / "var"),
        "WORKBENCH_DB_PATH": str(root / "var/workbench.sqlite3"),
        "WORKBENCH_PREVIEW_ROOT": str(root / "previews"),
        "WORKBENCH_IMPORT_ROOT": str(root / "imports"),
        "WORKBENCH_RUN_ROOT": str(root / "runs"),
        "WORKBENCH_EXPORT_ROOT": str(root / "exports"),
        "MPLCONFIGDIR": str(root / "matplotlib"),
    }


def test_isolated_test_runtime_contract_accepts_only_external_paths(tmp_path):
    payload = validate_runtime_environment(isolated_environment(tmp_path))
    assert payload["mode"] == "test"
    assert payload["port"] == 18766
    assert all(
        Path(path).is_relative_to(tmp_path)
        for path in payload["mutable_paths"].values()
    )


def test_test_runtime_rejects_production_port(tmp_path):
    environment = isolated_environment(tmp_path)
    environment["WORKBENCH_PORT"] = "8765"
    with pytest.raises(RuntimeError, match="production port 8765"):
        validate_runtime_environment(environment)


def test_test_runtime_rejects_any_mutable_path_outside_root(tmp_path):
    environment = isolated_environment(tmp_path)
    environment["WORKBENCH_RUN_ROOT"] = str(tmp_path.parent / "live-runs")
    with pytest.raises(RuntimeError, match="WORKBENCH_RUN_ROOT"):
        validate_runtime_environment(environment)


def test_production_rejects_test_tagged_requests(monkeypatch):
    monkeypatch.setenv("WORKBENCH_ENV", "production")
    with TestClient(app) as client:
        response = client.get("/api/health", headers={TEST_REQUEST_HEADER: "e2e-123"})
    assert response.status_code == 409


def test_test_runtime_requires_matching_header_for_mutations(monkeypatch):
    monkeypatch.setenv("WORKBENCH_ENV", "test")
    monkeypatch.setenv("WORKBENCH_TEST_RUN_ID", "e2e-123")
    monkeypatch.setenv("WORKBENCH_TEST_REQUIRE_HEADER", "1")
    with TestClient(app) as client:
        missing = client.post("/api/storage/cleanup/plan")
        wrong = client.post(
            "/api/storage/cleanup/plan", headers={TEST_REQUEST_HEADER: "wrong"},
        )
        accepted = client.post(
            "/api/storage/cleanup/plan", headers={TEST_REQUEST_HEADER: "e2e-123"},
        )
    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert accepted.status_code == 200


# ---------------------------------------------------------------------------
# EnergyPlus location.  The path used to be a macOS literal, which meant the
# chain could only run on the machine it was written on (fixed 2026-08-04, so
# Rai and Javier can install on Windows and Linux).
# ---------------------------------------------------------------------------
import platform as _platform
from pathlib import Path as _Path

import run_simulation as _sim


def test_eplus_dir_is_resolved_not_hardcoded():
    """The module-level constant must come from the resolver, and must point at
    a real directory on the machine running the tests."""
    assert _sim.EPLUS_DIR == _sim.resolve_eplus_dir()
    assert _Path(_sim.EPLUS_DIR).is_dir(), (
        f"no EnergyPlus at {_sim.EPLUS_DIR} - set VALENCIA_EPLUS_DIR")


def test_eplus_dir_honours_an_explicit_override(monkeypatch, tmp_path):
    """An unusual install must never need a code change."""
    monkeypatch.setenv("VALENCIA_EPLUS_DIR", str(tmp_path))
    assert _sim.resolve_eplus_dir() == str(tmp_path)


def test_eplus_dir_falls_back_to_the_developed_against_path(monkeypatch):
    """With nothing found, the macOS default is returned - a machine that has
    always worked keeps returning the identical string."""
    monkeypatch.delenv("VALENCIA_EPLUS_DIR", raising=False)
    monkeypatch.setattr(_sim, "_openstudio_install_roots", lambda: [])
    assert _sim.resolve_eplus_dir() == _sim.EPLUS_DIR_DEFAULT


def test_eplus_dir_prefers_the_install_matching_the_sdk(monkeypatch, tmp_path):
    """SDK and engine should be the same release; a mismatched pair is a silent
    way to get different physics."""
    older = tmp_path / "OpenStudio-3.10.0"
    matching = tmp_path / f"OpenStudio-{__import__('openstudio').openStudioVersion()}"
    for root in (older, matching):
        (root / "EnergyPlus").mkdir(parents=True)
    monkeypatch.delenv("VALENCIA_EPLUS_DIR", raising=False)
    # deliberately list the non-matching one first
    monkeypatch.setattr(_sim, "_openstudio_install_roots", lambda: [older, matching])
    assert _sim.resolve_eplus_dir() == str(matching / "EnergyPlus")


def test_openstudio_roots_search_is_platform_aware():
    """Each platform looks where OpenStudio actually installs itself."""
    roots = _sim._openstudio_install_roots()
    assert isinstance(roots, list)
    if _platform.system() == "Darwin":
        # this machine has one; the point is the search finds it by pattern
        assert any("OpenStudio" in p.name for p in roots)
