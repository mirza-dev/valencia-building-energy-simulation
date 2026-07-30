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
