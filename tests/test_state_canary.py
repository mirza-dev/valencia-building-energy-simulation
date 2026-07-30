from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/workbench_state_canary.py"
SPEC = importlib.util.spec_from_file_location("workbench_state_canary", SCRIPT)
assert SPEC and SPEC.loader
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)


def sample_project(root: Path) -> Path:
    database = root / "var/workbench.sqlite3"
    database.parent.mkdir(parents=True)
    (root / "out/ui_runs/run-a").mkdir(parents=True)
    (root / "out/ui_runs/run-a/manifest.json").write_text("baseline", encoding="utf-8")
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
        con.execute("INSERT INTO runs(name) VALUES('baseline')")
    return root


def test_canary_is_stable_when_live_state_is_unchanged(tmp_path):
    project = sample_project(tmp_path)
    before = canary.state_snapshot(project)
    after = canary.state_snapshot(project)
    assert before["fingerprint"] == after["fingerprint"]
    assert canary.changes(before, after) == {"tables": {}, "roots": {}}


def test_canary_reports_database_and_artifact_changes(tmp_path):
    project = sample_project(tmp_path)
    before = canary.state_snapshot(project)
    with sqlite3.connect(project / "var/workbench.sqlite3") as con:
        con.execute("INSERT INTO runs(name) VALUES('test contamination')")
    (project / "out/ui_runs/run-a/manifest.json").write_text("changed", encoding="utf-8")
    after = canary.state_snapshot(project)
    report = canary.changes(before, after)
    assert before["fingerprint"] != after["fingerprint"]
    assert report["tables"]["runs"]["before_rows"] == 1
    assert report["tables"]["runs"]["after_rows"] == 2
    assert "run-a/manifest.json" in report["roots"]["runs"]["changed_paths"]
