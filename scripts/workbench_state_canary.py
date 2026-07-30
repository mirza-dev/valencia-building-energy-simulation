#!/usr/bin/env python3
"""Detect any live Workbench state change across an isolated test run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).resolve().parents[1]


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def database_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path), "fingerprint": digest(None), "tables": {}}
    uri = f"file:{path.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        user_version = int(con.execute("PRAGMA user_version").fetchone()[0])
        table_rows = con.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE type='table' AND (name NOT LIKE 'sqlite_%' OR name='sqlite_sequence') "
            "ORDER BY name"
        ).fetchall()
        tables: dict[str, Any] = {}
        for table_row in table_rows:
            name = str(table_row["name"])
            columns = con.execute(f"PRAGMA table_info({quote_identifier(name)})").fetchall()
            column_names = [str(item["name"]) for item in columns]
            primary_keys = [
                str(item["name"])
                for item in sorted(columns, key=lambda item: int(item["pk"]) or 10_000)
                if int(item["pk"]) > 0
            ]
            order_columns = primary_keys or column_names
            order_clause = ",".join(quote_identifier(item) for item in order_columns)
            rows = con.execute(
                f"SELECT * FROM {quote_identifier(name)} ORDER BY {order_clause}"
            ).fetchall()
            normalized = [
                [sqlite_value(row[column]) for column in column_names]
                for row in rows
            ]
            tables[name] = {
                "columns": column_names,
                "row_count": len(normalized),
                "fingerprint": digest(normalized),
                "schema_sha256": digest(table_row["sql"]),
            }
        identity = {
            "user_version": user_version,
            "tables": {
                name: {
                    "row_count": item["row_count"],
                    "fingerprint": item["fingerprint"],
                    "schema_sha256": item["schema_sha256"],
                }
                for name, item in tables.items()
            },
        }
        return {
            "exists": True,
            "path": str(path.resolve()),
            "user_version": user_version,
            "fingerprint": digest(identity),
            "tables": tables,
        }
    finally:
        con.close()


def root_snapshot(root: Path, *, database_path: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    ignored = {
        database_path.resolve(strict=False),
        Path(str(database_path) + "-wal").resolve(strict=False),
        Path(str(database_path) + "-shm").resolve(strict=False),
    }
    if root.exists():
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.resolve(strict=False) in ignored:
                continue
            stat = path.lstat()
            entry: dict[str, Any] = {
                "path": path.relative_to(root).as_posix(),
                "kind": "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file",
                "mode": stat.st_mode,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
            if path.is_symlink():
                entry["target"] = os.readlink(path)
            entries.append(entry)
    return {
        "path": str(root.resolve(strict=False)),
        "exists": root.exists(),
        "entry_count": len(entries),
        "file_count": sum(item["kind"] in {"file", "symlink"} for item in entries),
        "total_file_bytes": sum(item["size"] for item in entries if item["kind"] == "file"),
        "fingerprint": digest(entries),
        "entries": entries,
    }


def state_snapshot(project: Path) -> dict[str, Any]:
    project = project.resolve()
    database_path = project / "var/workbench.sqlite3"
    roots = {
        "var": root_snapshot(project / "var", database_path=database_path),
        "runs": root_snapshot(project / "out/ui_runs", database_path=database_path),
    }
    identity = {
        "database": database_snapshot(database_path),
        "roots": roots,
    }
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "project": str(project),
        "fingerprint": digest({
            "database": identity["database"]["fingerprint"],
            "roots": {name: item["fingerprint"] for name, item in roots.items()},
        }),
        **identity,
    }


def changes(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    table_changes: dict[str, Any] = {}
    old_tables = expected["database"]["tables"]
    new_tables = actual["database"]["tables"]
    for name in sorted(set(old_tables) | set(new_tables)):
        old = old_tables.get(name)
        new = new_tables.get(name)
        if old != new:
            table_changes[name] = {
                "before_rows": old.get("row_count") if old else None,
                "after_rows": new.get("row_count") if new else None,
                "before_sha256": old.get("fingerprint") if old else None,
                "after_sha256": new.get("fingerprint") if new else None,
            }
    root_changes: dict[str, Any] = {}
    for name in sorted(set(expected["roots"]) | set(actual["roots"])):
        old = expected["roots"].get(name, {})
        new = actual["roots"].get(name, {})
        if old.get("fingerprint") == new.get("fingerprint"):
            continue
        old_entries = {item["path"]: item for item in old.get("entries", [])}
        new_entries = {item["path"]: item for item in new.get("entries", [])}
        changed_paths = [
            path for path in sorted(set(old_entries) | set(new_entries))
            if old_entries.get(path) != new_entries.get(path)
        ]
        root_changes[name] = {
            "before_entries": old.get("entry_count"),
            "after_entries": new.get("entry_count"),
            "changed_paths": changed_paths[:50],
            "changed_path_count": len(changed_paths),
        }
    return {"tables": table_changes, "roots": root_changes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "verify"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--project", type=Path, default=PROJECT)
    args = parser.parse_args()

    if args.action == "snapshot":
        snapshot = state_snapshot(args.project)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(json.dumps({
            "status": "SNAPSHOT_CREATED",
            "fingerprint": snapshot["fingerprint"],
            "database_tables": len(snapshot["database"]["tables"]),
            "tracked_files": sum(root["file_count"] for root in snapshot["roots"].values()),
            "output": str(args.output),
        }, indent=2))
        return 0

    expected = json.loads(args.output.read_text(encoding="utf-8"))
    actual = state_snapshot(args.project)
    if expected["fingerprint"] != actual["fingerprint"]:
        print(json.dumps({
            "status": "LIVE_STATE_CHANGED",
            "before": expected["fingerprint"],
            "after": actual["fingerprint"],
            "changes": changes(expected, actual),
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "LIVE_STATE_UNCHANGED",
        "fingerprint": actual["fingerprint"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
