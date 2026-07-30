from __future__ import annotations

import sqlite3

import pytest

from workbench import db


def test_connect_context_closes_sqlite_file_handles(tmp_path, monkeypatch) -> None:
    var_dir = tmp_path / "var"
    monkeypatch.setattr(db, "VAR_DIR", var_dir)
    monkeypatch.setattr(db, "DB_PATH", var_dir / "workbench.sqlite3")

    with db.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_run_summaries_do_not_load_artifact_rows(tmp_path, monkeypatch) -> None:
    var_dir = tmp_path / "var"
    monkeypatch.setattr(db, "VAR_DIR", var_dir)
    monkeypatch.setattr(db, "DB_PATH", var_dir / "workbench.sqlite3")
    db.init_db()
    job_id = db.create_job("city", "VALENCIA", {}, timeout_seconds=60)
    db.insert_run({
        "id": "city-fast-picker",
        "job_id": job_id,
        "refparcela": "VALENCIA",
        "scenario_name": "Valencia baseline",
        "config": {},
        "stats": {},
        "qa": {"scientific_status": "VALIDATED"},
        "artifact_dir": str(tmp_path / "missing-on-purpose"),
        "run_type": "city",
        "verification_status": "VERIFIED",
    }, [{
        "name": "large.bin", "path": str(tmp_path / "missing-on-purpose/large.bin"),
        "sha256": "a" * 64, "size_bytes": 10_000_000,
    }])

    assert db.list_run_summaries_by_type("city") == [{
        "id": "city-fast-picker",
        "scenario_name": "Valencia baseline",
        "verification_status": "VERIFIED",
        "scientific_status": "VALIDATED",
        "created_at": db.get_run("city-fast-picker")["created_at"],
    }]
