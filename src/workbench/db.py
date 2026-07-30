"""Small SQLite repository for local, immutable research runs."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any


PROJECT = Path(__file__).resolve().parents[2]
VAR_DIR = Path(os.environ.get("WORKBENCH_VAR_DIR", PROJECT / "var"))
DB_PATH = Path(os.environ.get("WORKBENCH_DB_PATH", VAR_DIR / "workbench.sqlite3"))
LATEST_SCHEMA_VERSION = 7


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back a context-managed transaction, then close its file handles."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, factory=_ClosingConnection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, locale TEXT NOT NULL DEFAULT 'tr', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_settings (
    project_id TEXT PRIMARY KEY,
    building_dataset_id TEXT,
    neighbor_dataset_id TEXT,
    template_dataset_id TEXT,
    weather_dataset_id TEXT,
    initialized INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id),
    FOREIGN KEY(building_dataset_id) REFERENCES datasets(id),
    FOREIGN KEY(neighbor_dataset_id) REFERENCES datasets(id),
    FOREIGN KEY(template_dataset_id) REFERENCES datasets(id),
    FOREIGN KEY(weather_dataset_id) REFERENCES datasets(id)
);
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
    path TEXT NOT NULL, sha256 TEXT NOT NULL, read_only INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}', snapshot_hash TEXT,
    verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED', created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS dataset_field_notes (
    dataset_id TEXT NOT NULL, field_name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '', semantic_label TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(dataset_id,field_name),
    FOREIGN KEY(dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS workflow_input_policies (
    project_id TEXT NOT NULL, workflow TEXT NOT NULL,
    schema_version INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    policy_json TEXT NOT NULL, fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,workflow,revision),
    FOREIGN KEY(project_id) REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, source TEXT NOT NULL,
    config_json TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS scenarios (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, profile_id TEXT NOT NULL,
    name TEXT NOT NULL, config_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'DRAFT',
    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id), FOREIGN KEY(profile_id) REFERENCES profiles(id)
);
CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL, field TEXT NOT NULL,
    reason TEXT NOT NULL, source_type TEXT NOT NULL, source_ref TEXT,
    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT,
    FOREIGN KEY(scenario_id) REFERENCES scenarios(id)
);
CREATE TABLE IF NOT EXISTS geometry_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL, action TEXT NOT NULL,
    before_json TEXT NOT NULL, after_json TEXT NOT NULL, approved_at TEXT NOT NULL,
    FOREIGN KEY(scenario_id) REFERENCES scenarios(id)
);
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
    total INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, refparcela TEXT NOT NULL,
    payload_json TEXT NOT NULL, result_path TEXT, error TEXT, batch_id TEXT,
    auto_commit INTEGER NOT NULL DEFAULT 0, attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2, heartbeat_at TEXT, lease_owner TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT 'queued',
    timeout_seconds INTEGER NOT NULL DEFAULT 180,
    attempt_started_at TEXT, terminal_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES batches(id)
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, created_at TEXT NOT NULL,
    level TEXT NOT NULL, message TEXT NOT NULL, progress REAL NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, refparcela TEXT NOT NULL,
    scenario_name TEXT NOT NULL, config_json TEXT NOT NULL, stats_json TEXT NOT NULL,
    qa_json TEXT NOT NULL, artifact_dir TEXT NOT NULL,
    run_type TEXT NOT NULL DEFAULT 'model', parent_run_id TEXT, scenario_id TEXT,
    verification_status TEXT NOT NULL DEFAULT 'LEGACY', raw_model_sha256 TEXT,
    canonical_fingerprint TEXT, manifest_sha256 TEXT, committed_at TEXT,
    provenance TEXT NOT NULL DEFAULT 'pipeline', authored_from TEXT,
    patch_journal_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, name TEXT NOT NULL,
    path TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, verified_at TEXT,
    FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS snapshot_refs (
    owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, role TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(owner_type,owner_id,role)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_events_job_id ON job_events(job_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshot_refs_hash ON snapshot_refs(snapshot_hash);
"""


def _ensure_column(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migration_2(con: sqlite3.Connection) -> None:
    additions = {
        "project_settings": {"initialized": "INTEGER NOT NULL DEFAULT 0"},
        "datasets": {
            "snapshot_hash": "TEXT",
            "verification_status": "TEXT NOT NULL DEFAULT 'UNVERIFIED'",
        },
        "scenarios": {
            "status": "TEXT NOT NULL DEFAULT 'DRAFT'",
            "revision": "INTEGER NOT NULL DEFAULT 1",
        },
        "overrides": {
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "created_at": "TEXT",
        },
        "jobs": {
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 2",
            "heartbeat_at": "TEXT",
            "lease_owner": "TEXT",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            "stage": "TEXT NOT NULL DEFAULT 'queued'",
            "timeout_seconds": "INTEGER NOT NULL DEFAULT 180",
        },
        "runs": {
            "run_type": "TEXT NOT NULL DEFAULT 'model'",
            "parent_run_id": "TEXT",
            "scenario_id": "TEXT",
            "verification_status": "TEXT NOT NULL DEFAULT 'LEGACY'",
            "raw_model_sha256": "TEXT",
            "canonical_fingerprint": "TEXT",
            "manifest_sha256": "TEXT",
            "committed_at": "TEXT",
        },
        "artifacts": {"verified_at": "TEXT"},
    }
    for table, columns in additions.items():
        for column, ddl in columns.items():
            _ensure_column(con, table, column, ddl)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS snapshot_refs (
            owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, role TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(owner_type,owner_id,role)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshot_refs_hash ON snapshot_refs(snapshot_hash);
    """)
    con.execute("UPDATE runs SET verification_status='LEGACY' WHERE verification_status IS NULL")


def _migration_3(con: sqlite3.Connection) -> None:
    _ensure_column(con, "jobs", "attempt_started_at", "TEXT")
    _ensure_column(con, "jobs", "terminal_at", "TEXT")
    con.execute(
        """UPDATE jobs SET terminal_at=updated_at
           WHERE terminal_at IS NULL AND status IN ('completed','failed','canceled')"""
    )
    con.execute(
        """UPDATE jobs SET attempt_started_at=COALESCE(heartbeat_at,updated_at,created_at)
           WHERE attempt_started_at IS NULL AND status='running'"""
    )


def _migration_4(con: sqlite3.Connection) -> None:
    _ensure_column(con, "runs", "provenance", "TEXT NOT NULL DEFAULT 'pipeline'")
    _ensure_column(con, "runs", "authored_from", "TEXT")
    _ensure_column(con, "runs", "patch_journal_json", "TEXT NOT NULL DEFAULT '[]'")
    con.execute("UPDATE runs SET provenance='pipeline' WHERE provenance IS NULL OR provenance='' ")
    con.execute("UPDATE runs SET patch_journal_json='[]' WHERE patch_journal_json IS NULL")


def _migration_5(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS dataset_field_notes (
               dataset_id TEXT NOT NULL, field_name TEXT NOT NULL,
               note TEXT NOT NULL DEFAULT '', semantic_label TEXT NOT NULL DEFAULT '',
               updated_at TEXT NOT NULL,
               PRIMARY KEY(dataset_id,field_name),
               FOREIGN KEY(dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
           )"""
    )


def _migration_6(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS workflow_input_policies (
               project_id TEXT NOT NULL, workflow TEXT NOT NULL,
               schema_version INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
               policy_json TEXT NOT NULL, fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL,
               PRIMARY KEY(project_id,workflow,revision),
               FOREIGN KEY(project_id) REFERENCES projects(id)
           )"""
    )


def _migration_7(con: sqlite3.Connection) -> None:
    """Make policy revisions append-only for project-default audit history."""
    columns = con.execute("PRAGMA table_info(workflow_input_policies)").fetchall()
    primary_key = [row[1] for row in sorted((row for row in columns if row[5]), key=lambda row: row[5])]
    if primary_key == ["project_id", "workflow", "revision"]:
        return
    con.execute("ALTER TABLE workflow_input_policies RENAME TO workflow_input_policies_v6")
    con.execute(
        """CREATE TABLE workflow_input_policies (
               project_id TEXT NOT NULL, workflow TEXT NOT NULL,
               schema_version INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
               policy_json TEXT NOT NULL, fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL,
               PRIMARY KEY(project_id,workflow,revision),
               FOREIGN KEY(project_id) REFERENCES projects(id)
           )"""
    )
    con.execute(
        """INSERT INTO workflow_input_policies(
               project_id,workflow,schema_version,revision,policy_json,fingerprint,updated_at
           ) SELECT project_id,workflow,schema_version,revision,policy_json,fingerprint,updated_at
             FROM workflow_input_policies_v6"""
    )
    con.execute("DROP TABLE workflow_input_policies_v6")


def init_db() -> None:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as probe:
        current = int(probe.execute("PRAGMA user_version").fetchone()[0])
    if DB_PATH.exists() and 0 < current < LATEST_SCHEMA_VERSION:
        backup_dir = VAR_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"workbench-v{current}-{datetime.now():%Y%m%dT%H%M%S}.sqlite3"
        source = sqlite3.connect(DB_PATH)
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    with connect() as con:
        con.executescript(SCHEMA)
        if current < 2:
            _migration_2(con)
        if current < 3:
            _migration_3(con)
        if current < 4:
            _migration_4(con)
        if current < 5:
            _migration_5(con)
        if current < 6:
            _migration_6(con)
        if current < 7:
            _migration_7(con)
        con.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")
        con.execute(
            "INSERT OR IGNORE INTO projects(id,name,locale,created_at) VALUES(?,?,?,?)",
            ("valencia", "Valencia Energy Simulation", "tr", utcnow()),
        )
        con.execute(
            "INSERT OR IGNORE INTO project_settings(project_id,updated_at) VALUES(?,?)",
            ("valencia", utcnow()),
        )
        con.commit()


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def upsert_dataset(dataset: dict[str, Any]) -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO datasets(id,project_id,kind,name,path,sha256,read_only,metadata_json,
               snapshot_hash,verification_status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,path=excluded.path,
               sha256=excluded.sha256,metadata_json=excluded.metadata_json,
               snapshot_hash=excluded.snapshot_hash,verification_status=excluded.verification_status""",
            (
                dataset["id"], "valencia", dataset["kind"], dataset["name"], dataset["path"],
                dataset["sha256"], int(dataset.get("read_only", True)),
                json_dump(dataset.get("metadata", {})), dataset.get("snapshot_hash"),
                dataset.get("verification_status", "UNVERIFIED"), dataset.get("created_at", utcnow()),
            ),
        )
    if dataset.get("snapshot_hash"):
        set_snapshot_ref("dataset", dataset["id"], "source", dataset["snapshot_hash"])


def list_datasets() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM datasets ORDER BY kind,name").fetchall()
    return [dict(row) | {"metadata": json.loads(row["metadata_json"])} for row in rows]


def get_dataset(dataset_id: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["metadata"] = json.loads(item.pop("metadata_json"))
    return item


def dataset_field_notes(dataset_id: str) -> dict[str, dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT field_name,note,semantic_label,updated_at FROM dataset_field_notes WHERE dataset_id=?",
            (dataset_id,),
        ).fetchall()
    return {str(row["field_name"]): dict(row) for row in rows}


def upsert_dataset_field_note(
    dataset_id: str, field_name: str, note: str, semantic_label: str,
) -> dict[str, Any]:
    note = note.strip()
    semantic_label = semantic_label.strip()
    now = utcnow()
    with connect() as con:
        if not note and not semantic_label:
            con.execute(
                "DELETE FROM dataset_field_notes WHERE dataset_id=? AND field_name=?",
                (dataset_id, field_name),
            )
            return {
                "dataset_id": dataset_id, "field_name": field_name,
                "note": "", "semantic_label": "", "updated_at": now,
            }
        con.execute(
            """INSERT INTO dataset_field_notes(dataset_id,field_name,note,semantic_label,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(dataset_id,field_name) DO UPDATE SET
                 note=excluded.note,semantic_label=excluded.semantic_label,updated_at=excluded.updated_at""",
            (dataset_id, field_name, note, semantic_label, now),
        )
    return {
        "dataset_id": dataset_id, "field_name": field_name,
        "note": note, "semantic_label": semantic_label, "updated_at": now,
    }


def get_workflow_input_policy(workflow: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            """SELECT * FROM workflow_input_policies
               WHERE project_id='valencia' AND workflow=?
               ORDER BY revision DESC LIMIT 1""",
            (workflow,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["policy"] = json.loads(item.pop("policy_json"))
    return item


def upsert_workflow_input_policy(
    workflow: str, schema_version: int, policy: dict[str, Any], fingerprint: str,
) -> dict[str, Any]:
    now = utcnow()
    with connect() as con:
        current = con.execute(
            """SELECT MAX(revision) AS revision FROM workflow_input_policies
               WHERE project_id='valencia' AND workflow=?""",
            (workflow,),
        ).fetchone()
        revision = int(current["revision"]) + 1 if current and current["revision"] is not None else 1
        con.execute(
            """INSERT INTO workflow_input_policies(
                   project_id,workflow,schema_version,revision,policy_json,fingerprint,updated_at
               ) VALUES('valencia',?,?,?,?,?,?)""",
            (workflow, schema_version, revision, json_dump(policy), fingerprint, now),
        )
    return get_workflow_input_policy(workflow) or {}


def project_settings() -> dict[str, Any]:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM project_settings WHERE project_id='valencia'"
        ).fetchone()
    if row is None:
        raise RuntimeError("Project settings are not initialized")
    return dict(row)


def update_project_settings(values: dict[str, str | None]) -> dict[str, Any]:
    allowed = {
        "building_dataset_id", "neighbor_dataset_id",
        "template_dataset_id", "weather_dataset_id",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Unknown project settings: {', '.join(sorted(unknown))}")
    assignments = ",".join(f"{field}=?" for field in values)
    with connect() as con:
        if assignments:
            con.execute(
                f"UPDATE project_settings SET {assignments},initialized=1,updated_at=? WHERE project_id='valencia'",
                (*values.values(), utcnow()),
            )
    return project_settings()


def set_snapshot_ref(owner_type: str, owner_id: str, role: str, snapshot_hash: str) -> None:
    with connect() as con:
        con.execute(
            """INSERT OR REPLACE INTO snapshot_refs(owner_type,owner_id,role,snapshot_hash,created_at)
               VALUES(?,?,?,?,?)""",
            (owner_type, owner_id, role, snapshot_hash, utcnow()),
        )


def snapshot_hashes_in_use() -> set[str]:
    with connect() as con:
        rows = con.execute("SELECT DISTINCT snapshot_hash FROM snapshot_refs").fetchall()
    return {str(row[0]) for row in rows}


def create_scenario(profile_id: str, name: str, config: dict[str, Any],
                    overrides: list[dict[str, Any]], geometry_actions: list[dict[str, Any]]) -> str:
    scenario_id = uuid.uuid4().hex
    now = utcnow()
    with connect() as con:
        con.execute(
            """INSERT INTO scenarios(id,project_id,profile_id,name,config_json,status,revision,created_at)
               VALUES(?,?,?,?,?,'DRAFT',1,?)""",
            (scenario_id, "valencia", profile_id, name, json_dump(config), now),
        )
        for item in overrides:
            con.execute(
                """INSERT INTO overrides(scenario_id,field,reason,source_type,source_ref,revision,created_at)
                   VALUES(?,?,?,?,?,1,?)""",
                (scenario_id, item["field"], item["reason"], item["source_type"],
                 item.get("source_ref"), now),
            )
        for action in geometry_actions:
            con.execute(
                """INSERT INTO geometry_actions(scenario_id,action,before_json,after_json,approved_at)
                   VALUES(?,?,?,?,?)""",
                (scenario_id, action.get("action", "unknown"), "{}", json_dump(action), now),
            )
    return scenario_id


def get_scenario(scenario_id: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        if row is None:
            return None
        overrides = con.execute(
            "SELECT field,reason,source_type,source_ref,revision,created_at FROM overrides WHERE scenario_id=?",
            (scenario_id,),
        ).fetchall()
    return dict(row) | {
        "config": json.loads(row["config_json"]),
        "overrides": [dict(item) for item in overrides],
    }


def update_scenario_status(scenario_id: str, status: str) -> None:
    with connect() as con:
        con.execute("UPDATE scenarios SET status=? WHERE id=?", (status, scenario_id))


def replace_profiles(profiles: list[dict[str, Any]]) -> None:
    with connect() as con:
        for item in profiles:
            con.execute(
                "INSERT OR REPLACE INTO profiles(id,label,source,config_json,locked) VALUES(?,?,?,?,1)",
                (item["id"], item["label"], item["source"], json_dump(item["config"])),
            )


def list_profiles() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    return [dict(row) | {"config": json.loads(row["config_json"])} for row in rows]


def create_job(kind: str, refparcela: str, payload: dict[str, Any], *,
               batch_id: str | None = None, auto_commit: bool = False,
               timeout_seconds: int = 180) -> str:
    job_id = uuid.uuid4().hex
    now = utcnow()
    with connect() as con:
        con.execute(
            """INSERT INTO jobs(id,kind,status,refparcela,payload_json,batch_id,auto_commit,
               max_attempts,timeout_seconds,stage,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,2,?,'queued',?,?)""",
            (job_id, kind, "queued", refparcela, json_dump(payload), batch_id,
             int(auto_commit), timeout_seconds, now, now),
        )
        con.execute(
            "INSERT INTO job_events(job_id,created_at,level,message,progress) VALUES(?,?,?,?,?)",
            (job_id, now, "info", "Queued", 0.0),
        )
    return job_id


def create_immediate_job(kind: str, refparcela: str, payload: dict[str, Any]) -> str:
    """Create an API-owned running job that the queue worker must never claim."""
    job_id = uuid.uuid4().hex
    now = utcnow()
    with connect() as con:
        con.execute(
            """INSERT INTO jobs(id,kind,status,refparcela,payload_json,auto_commit,
               attempt_count,max_attempts,timeout_seconds,stage,attempt_started_at,
               heartbeat_at,created_at,updated_at)
               VALUES(?,?,?,?,?,0,1,1,180,'committing',?,?,?,?)""",
            (job_id, kind, "running", refparcela, json_dump(payload), now, now, now, now),
        )
        con.execute(
            "INSERT INTO job_events(job_id,created_at,level,message,progress) VALUES(?,?,?,?,?)",
            (job_id, now, "info", "Authoring commit started", 0.05),
        )
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            """SELECT j.*,r.id AS run_id FROM jobs j
               LEFT JOIN runs r ON r.job_id=j.id WHERE j.id=?""",
            (job_id,),
        ).fetchone()
        queue_position = None
        if row is not None and row["status"] == "queued":
            queue_position = int(con.execute(
                """SELECT COUNT(*) FROM jobs WHERE status='queued'
                   AND (created_at < ? OR (created_at = ? AND id <= ?))""",
                (row["created_at"], row["created_at"], row["id"]),
            ).fetchone()[0])
    if row is None:
        return None
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    item["queue_position"] = queue_position
    return item


def list_jobs_with_statuses(statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    """Return typed job records for storage accounting and retention decisions."""
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    with connect() as con:
        rows = con.execute(
            f"""SELECT id FROM jobs WHERE status IN ({placeholders})
                ORDER BY created_at,id""",
            statuses,
        ).fetchall()
    return [job for row in rows if (job := get_job(row["id"])) is not None]


def list_active_jobs() -> list[dict[str, Any]]:
    return list_jobs_with_statuses(("queued", "running"))


def update_job_payload(job_id: str, values: dict[str, Any]) -> dict[str, Any]:
    """Merge orchestration evidence into a job payload without changing its identity."""
    job = get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    payload = dict(job["payload"])
    payload.update(values)
    with connect() as con:
        con.execute(
            "UPDATE jobs SET payload_json=?,updated_at=? WHERE id=?",
            (json_dump(payload), utcnow(), job_id),
        )
    return get_job(job_id) or job | {"payload": payload}


def find_active_simulation_job(parent_run_id: str) -> dict[str, Any] | None:
    """Return the queued/running simulation for a parent, if one exists."""
    return find_current_simulation_job(parent_run_id)


def find_current_simulation_job(parent_run_id: str | None = None) -> dict[str, Any] | None:
    """Return the running simulation first, then the oldest queued simulation."""
    with connect() as con:
        rows = con.execute(
            """SELECT id,payload_json FROM jobs WHERE kind='simulation'
               AND status IN ('queued','running')
               ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at, id"""
        ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        if parent_run_id is None or payload.get("parent_run_id") == parent_run_id:
            return get_job(row["id"])
    return None


def find_current_job(kind: str) -> dict[str, Any] | None:
    """Return the running job first, then the oldest queued job for a kind."""
    with connect() as con:
        row = con.execute(
            """SELECT id FROM jobs WHERE kind=? AND status IN ('queued','running')
               ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at, id
               LIMIT 1""",
            (kind,),
        ).fetchone()
    return get_job(row["id"]) if row else None


def list_current_jobs(kind: str, *, include_ready: bool = False,
                      limit: int = 20) -> list[dict[str, Any]]:
    """Return recoverable jobs without mixing records from other worker kinds."""
    statuses = ("queued", "running", "ready") if include_ready else ("queued", "running")
    placeholders = ",".join("?" for _ in statuses)
    with connect() as con:
        rows = con.execute(
            f"""SELECT id FROM jobs WHERE kind=? AND status IN ({placeholders})
                ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                         CASE WHEN status='ready' THEN updated_at END DESC,
                         created_at, id
                LIMIT ?""",
            (kind, *statuses, max(1, min(int(limit), 100))),
        ).fetchall()
    return [job for row in rows if (job := get_job(row["id"])) is not None]


def worker_queue_context(job_id: str) -> dict[str, Any]:
    """Describe why a queued job is waiting in the shared single-worker queue."""
    job = get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    with connect() as con:
        queued_total = int(con.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='queued'"
        ).fetchone()[0])
        active_row = con.execute(
            """SELECT id FROM jobs WHERE status='running'
               ORDER BY attempt_started_at, created_at, id LIMIT 1"""
        ).fetchone()
    active = get_job(active_row["id"]) if active_row else None
    active_summary = None if active is None else {
        "id": active["id"],
        "kind": active["kind"],
        "refparcela": active["refparcela"],
        "stage": active["stage"],
        "attempt_count": active["attempt_count"],
        "heartbeat_at": active.get("heartbeat_at"),
    }
    return {
        "position": job.get("queue_position"),
        "queued_total": queued_total,
        "active_job": active_summary,
        "worker_busy": active is not None and active["id"] != job_id,
    }


def find_current_scenario_job(fingerprint: str | None = None) -> dict[str, Any] | None:
    """Return an active Part G job, optionally matching its immutable request fingerprint."""
    with connect() as con:
        rows = con.execute(
            """SELECT id,payload_json FROM jobs WHERE kind='scenario'
               AND status IN ('queued','running')
               ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at, id"""
        ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        if fingerprint is None or payload.get("scenario_fingerprint") == fingerprint:
            return get_job(row["id"])
    return None


def claim_next_job() -> dict[str, Any] | None:
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            con.commit()
            return None
        now = utcnow()
        con.execute(
            """UPDATE jobs SET status='running',stage='starting',attempt_count=attempt_count+1,
               heartbeat_at=?,lease_owner=?,attempt_started_at=?,terminal_at=NULL,updated_at=?
               WHERE id=? AND status='queued'""",
            (now, f"pid:{__import__('os').getpid()}", now, now, row["id"]),
        )
        con.commit()
    return get_job(row["id"])


def update_job(job_id: str, status: str, *, result_path: str | None = None,
               error: str | None = None) -> None:
    now = utcnow()
    terminal_at = now if status in {"completed", "failed", "canceled"} else None
    with connect() as con:
        con.execute(
            """UPDATE jobs SET status=?,stage=?,result_path=COALESCE(?,result_path),error=?,
               heartbeat_at=?,terminal_at=COALESCE(?,terminal_at),updated_at=? WHERE id=?""",
            (status, status, result_path, error, now, terminal_at, now, job_id),
        )


def heartbeat_job(job_id: str, stage: str | None = None) -> None:
    with connect() as con:
        con.execute(
            "UPDATE jobs SET heartbeat_at=?,stage=COALESCE(?,stage),updated_at=? WHERE id=?",
            (utcnow(), stage, utcnow(), job_id),
        )


def request_cancel(job_id: str) -> dict[str, Any] | None:
    batch_id: str | None = None
    with connect() as con:
        row = con.execute(
            "SELECT status,batch_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        batch_id = row["batch_id"]
        now = utcnow()
        progress_row = con.execute(
            "SELECT COALESCE(MAX(progress),0.0) FROM job_events WHERE job_id=?", (job_id,)
        ).fetchone()
        progress = float(progress_row[0])
        if row["status"] == "queued":
            con.execute(
                """UPDATE jobs SET status='canceled',stage='canceled',cancel_requested=1,
                   terminal_at=?,updated_at=? WHERE id=?""",
                (now, now, job_id),
            )
        elif row["status"] == "running":
            con.execute(
                "UPDATE jobs SET cancel_requested=1,stage='canceling',updated_at=? WHERE id=?",
                (now, job_id),
            )
        else:
            return get_job(job_id)
        con.execute(
            "INSERT INTO job_events(job_id,created_at,level,message,progress) VALUES(?,?,?,?,?)",
            (job_id, now, "warning", "Cancel requested", progress),
        )
    if batch_id:
        reconcile_batch(batch_id)
    return get_job(job_id)


def fail_or_retry(job_id: str, error: str) -> str:
    """Retry a crashed lease once; otherwise leave a terminal failure."""
    with connect() as con:
        row = con.execute(
            "SELECT attempt_count,max_attempts,cancel_requested,batch_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row["cancel_requested"]:
            status = "canceled"
        elif row["attempt_count"] < row["max_attempts"]:
            status = "queued"
        else:
            status = "failed"
        now = utcnow()
        terminal_at = now if status in {"canceled", "failed"} else None
        con.execute(
            """UPDATE jobs SET status=?,stage=?,error=?,lease_owner=NULL,
               heartbeat_at=?,attempt_started_at=CASE WHEN ?='queued' THEN NULL ELSE attempt_started_at END,
               terminal_at=?,updated_at=? WHERE id=?""",
            (status, status, error, now, status, terminal_at, now, job_id),
        )
        con.execute(
            "INSERT INTO job_events(job_id,created_at,level,message,progress) VALUES(?,?,?,?,?)",
            (job_id, now, "warning" if status in {"queued", "canceled"} else "error",
             f"Worker retry queued: {error[-900:]}" if status == "queued" else error[-1000:],
             0.0 if status == "queued" else 1.0),
        )
    if row["batch_id"]:
        reconcile_batch(row["batch_id"])
    return status


def retry_failed_job(job_id: str) -> dict[str, Any]:
    original = get_job(job_id)
    if original is None:
        raise KeyError(job_id)
    if original["status"] != "failed":
        raise ValueError("Only failed jobs can be retried manually")
    retry_id = create_job(
        original["kind"], original["refparcela"], original["payload"],
        batch_id=original.get("batch_id"), auto_commit=bool(original.get("auto_commit")),
        timeout_seconds=int(original.get("timeout_seconds", 180)),
    )
    if original.get("batch_id"):
        with connect() as con:
            con.execute(
                "UPDATE batches SET total=total+1,status='running',updated_at=? WHERE id=?",
                (utcnow(), original["batch_id"]),
            )
    return get_job(retry_id)


def recover_orphan_jobs() -> dict[str, int]:
    recovered = failed = 0
    with connect() as con:
        rows = con.execute("SELECT id,attempt_count,max_attempts FROM jobs WHERE status='running'").fetchall()
        for row in rows:
            if row["attempt_count"] < row["max_attempts"]:
                con.execute(
                    """UPDATE jobs SET status='queued',stage='recovered',lease_owner=NULL,
                       cancel_requested=0,attempt_started_at=NULL,terminal_at=NULL,updated_at=?
                       WHERE id=?""",
                    (utcnow(), row["id"]),
                )
                recovered += 1
            else:
                con.execute(
                    """UPDATE jobs SET status='failed',stage='failed',error=?,terminal_at=?,
                       updated_at=? WHERE id=?""",
                    ("Worker stopped and retry limit was exhausted", utcnow(), utcnow(), row["id"]),
                )
                failed += 1
    return {"requeued": recovered, "failed": failed}


def recover_incomplete_auto_commits() -> int:
    """Make interrupted READY -> run commits terminal and manually retryable."""
    recovered = 0
    affected_batches: set[str] = set()
    message = "Automatic commit was interrupted; retry this job manually"
    with connect() as con:
        rows = con.execute(
            """SELECT j.id,j.batch_id FROM jobs j
               WHERE j.status='ready' AND j.auto_commit=1
                 AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.job_id=j.id)"""
        ).fetchall()
        for row in rows:
            now = utcnow()
            con.execute(
                """UPDATE jobs SET status='failed',stage='commit_interrupted',error=?,
                   lease_owner=NULL,heartbeat_at=?,terminal_at=?,updated_at=?
                   WHERE id=? AND status='ready'""",
                (message, now, now, now, row["id"]),
            )
            con.execute(
                """INSERT INTO job_events(job_id,created_at,level,message,progress)
                   VALUES(?,?,?,?,?)""",
                (row["id"], now, "error", message, 1.0),
            )
            if row["batch_id"]:
                con.execute(
                    "UPDATE batches SET failed=failed+1,updated_at=? WHERE id=?",
                    (now, row["batch_id"]),
                )
                affected_batches.add(row["batch_id"])
            recovered += 1
        for batch_id in affected_batches:
            batch = con.execute(
                "SELECT total,completed,failed FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                continue
            status = "completed" if batch["completed"] + batch["failed"] >= batch["total"] else "running"
            con.execute(
                "UPDATE batches SET status=?,updated_at=? WHERE id=?",
                (status, utcnow(), batch_id),
            )
    return recovered


def add_event(job_id: str, message: str, progress: float, level: str = "info") -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO job_events(job_id,created_at,level,message,progress) VALUES(?,?,?,?,?)",
            (job_id, utcnow(), level, message, max(0.0, min(1.0, progress))),
        )
        con.execute(
            "UPDATE jobs SET heartbeat_at=?,stage=?,updated_at=? WHERE id=?",
            (utcnow(), message[:80], utcnow(), job_id),
        )


def list_events(job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id", (job_id, after_id)
        ).fetchall()
    return [dict(row) for row in rows]


def create_batch(name: str, refs: list[str], payload: dict[str, Any]) -> tuple[str, list[str]]:
    return create_batch_jobs(name, [(ref, payload | {"building_ref": ref}) for ref in refs])


def create_batch_jobs(name: str, items: list[tuple[str, dict[str, Any]]]) -> tuple[str, list[str]]:
    batch_id = uuid.uuid4().hex
    now = utcnow()
    with connect() as con:
        con.execute(
            "INSERT INTO batches(id,name,status,total,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (batch_id, name, "queued", len(items), now, now),
        )
    jobs = [create_job("batch", ref, payload, batch_id=batch_id, auto_commit=True)
            for ref, payload in items]
    return batch_id, jobs


def update_batch_for_job(job: dict[str, Any], succeeded: bool) -> None:
    del succeeded
    if not job.get("batch_id"):
        return
    reconcile_batch(job["batch_id"])


def reconcile_batch(batch_id: str) -> None:
    """Derive aggregate counters from child jobs so recovery is idempotent."""
    with connect() as con:
        batch = con.execute(
            "SELECT total,completed,failed,status FROM batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            return
        counts = con.execute(
            """SELECT
                 SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                 SUM(CASE WHEN status IN ('failed','canceled') THEN 1 ELSE 0 END) AS failed,
                 SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                 SUM(CASE WHEN status IN ('running','ready') THEN 1 ELSE 0 END) AS active
               FROM jobs WHERE batch_id=?""",
            (batch_id,),
        ).fetchone()
        completed = int(counts["completed"] or 0)
        failed = int(counts["failed"] or 0)
        if completed + failed >= batch["total"]:
            status = "completed"
        elif counts["active"] or (counts["queued"] and (completed or failed)):
            status = "running"
        else:
            status = "queued"
        if (
            completed != int(batch["completed"])
            or failed != int(batch["failed"])
            or status != batch["status"]
        ):
            con.execute(
                """UPDATE batches SET completed=?,failed=?,status=?,updated_at=? WHERE id=?""",
                (completed, failed, status, utcnow(), batch_id),
            )


def reconcile_all_batches() -> int:
    with connect() as con:
        batch_ids = [row["id"] for row in con.execute("SELECT id FROM batches").fetchall()]
    for batch_id in batch_ids:
        reconcile_batch(batch_id)
    return len(batch_ids)


def list_batches() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM batches ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def get_batch(batch_id: str) -> dict[str, Any] | None:
    with connect() as con:
        batch = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        jobs = con.execute(
            """SELECT j.*,r.id AS run_id FROM jobs j LEFT JOIN runs r ON r.job_id=j.id
               WHERE j.batch_id=? ORDER BY j.created_at""", (batch_id,),
        ).fetchall()
    if batch is None:
        return None
    output = dict(batch)
    output["jobs"] = []
    for row in jobs:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        output["jobs"].append(item)
    return output


def insert_run(run: dict[str, Any], artifacts: list[dict[str, Any]],
               snapshot_refs: dict[str, str] | None = None) -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO runs(id,job_id,refparcela,scenario_name,config_json,stats_json,
               qa_json,artifact_dir,run_type,parent_run_id,scenario_id,verification_status,
               raw_model_sha256,canonical_fingerprint,manifest_sha256,committed_at,
               provenance,authored_from,patch_journal_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["id"], run["job_id"], run["refparcela"], run["scenario_name"],
                json_dump(run["config"]), json_dump(run["stats"]), json_dump(run["qa"]),
                run["artifact_dir"], run.get("run_type", "model"), run.get("parent_run_id"),
                run.get("scenario_id"), run.get("verification_status", "COMMITTING"),
                run.get("raw_model_sha256"), run.get("canonical_fingerprint"),
                run.get("manifest_sha256"), run.get("committed_at"),
                run.get("provenance", "pipeline"), run.get("authored_from"),
                json_dump(run.get("patch_journal", [])),
                run.get("created_at", utcnow()),
            ),
        )
        for artifact in artifacts:
            con.execute(
                "INSERT INTO artifacts(run_id,name,path,sha256,size_bytes,verified_at) VALUES(?,?,?,?,?,?)",
                (run["id"], artifact["name"], artifact["path"],
                 artifact["sha256"], artifact["size_bytes"], utcnow()),
            )
        for role, snapshot_hash in (snapshot_refs or {}).items():
            con.execute(
                """INSERT INTO snapshot_refs(owner_type,owner_id,role,snapshot_hash,created_at)
                   VALUES('run',?,?,?,?)""",
                (run["id"], role, snapshot_hash, utcnow()),
            )


def finalize_run(run_id: str, artifact_dir: str, manifest_sha256: str) -> None:
    with connect() as con:
        con.execute(
            """UPDATE runs SET artifact_dir=?,verification_status='VERIFIED',manifest_sha256=?,
               committed_at=? WHERE id=?""",
            (artifact_dir, manifest_sha256, utcnow(), run_id),
        )


def update_run_verification(run_id: str, status: str) -> None:
    with connect() as con:
        con.execute("UPDATE runs SET verification_status=? WHERE id=?", (status, run_id))


def committing_runs() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM runs WHERE verification_status='COMMITTING'").fetchall()
    return [dict(row) for row in rows]


def list_runs() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        output = []
        for row in rows:
            artifacts = con.execute(
                "SELECT name,sha256,size_bytes FROM artifacts WHERE run_id=? ORDER BY name",
                (row["id"],),
            ).fetchall()
            output.append(dict(row) | {
                "config": json.loads(row["config_json"]),
                "stats": json.loads(row["stats_json"]),
                "qa": json.loads(row["qa_json"]),
                "patch_journal": json.loads(row["patch_journal_json"] or "[]"),
                "artifacts": [dict(item) for item in artifacts],
            })
    return output


def list_runs_by_type(run_type: str) -> list[dict[str, Any]]:
    return [run for run in list_runs() if run["run_type"] == run_type]


def list_run_summaries_by_type(run_type: str) -> list[dict[str, Any]]:
    """Return picker metadata without loading or hashing immutable artifacts."""
    with connect() as con:
        rows = con.execute(
            """SELECT id, scenario_name, verification_status, qa_json, created_at
               FROM runs WHERE run_type=? ORDER BY created_at DESC""",
            (run_type,),
        ).fetchall()
    output = []
    for row in rows:
        qa = json.loads(row["qa_json"] or "{}")
        output.append({
            "id": row["id"],
            "scenario_name": row["scenario_name"],
            "verification_status": row["verification_status"],
            "scientific_status": qa.get("scientific_status"),
            "created_at": row["created_at"],
        })
    return output


def get_run(run_id: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        artifacts = con.execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY name", (run_id,)).fetchall()
    if row is None:
        return None
    return dict(row) | {
        "config": json.loads(row["config_json"]),
        "stats": json.loads(row["stats_json"]),
        "qa": json.loads(row["qa_json"]),
        "patch_journal": json.loads(row["patch_journal_json"] or "[]"),
        "artifacts": [dict(item) for item in artifacts],
    }


def referenced_snapshot_hashes() -> set[str]:
    """Return every snapshot protected by a dataset or immutable run reference."""
    with connect() as con:
        rows = con.execute(
            """SELECT snapshot_hash FROM snapshot_refs
               UNION SELECT snapshot_hash FROM datasets WHERE snapshot_hash IS NOT NULL"""
        ).fetchall()
    return {str(row["snapshot_hash"]) for row in rows if row["snapshot_hash"]}
