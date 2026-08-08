#!/usr/bin/env python3
"""Verify that an unpacked release contains its data and finished example run."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify() -> dict:
    manifest = json.loads((PROJECT / "distribution-manifest.json").read_text(encoding="utf-8"))
    present = []
    missing = []
    mismatched = []
    for item in manifest["payload"]:
        path = PROJECT / item["path"]
        if not path.exists():
            missing.append(item)
            continue
        if path.is_dir():
            file_count = sum(1 for candidate in path.rglob("*") if candidate.is_file())
            problems = []
            if file_count < item.get("minimum_files", 1):
                problems.append(f"only {file_count} files; expected at least {item['minimum_files']}")
            for requirement in item.get("required_globs", []):
                matches = sum(1 for candidate in path.glob(requirement["pattern"]) if candidate.is_file())
                if matches < requirement["minimum"]:
                    problems.append(
                        f"{requirement['pattern']} matched {matches}; expected at least {requirement['minimum']}"
                    )
            if problems:
                mismatched.append(item | {"actual": "; ".join(problems)})
                continue
        if path.is_file() and item.get("sha256"):
            actual = sha256(path)
            if actual != item["sha256"]:
                mismatched.append(item | {"actual_sha256": actual})
                continue
        present.append(item)

    example = None
    aggregate = PROJECT / "out/stock/benicalap_v8/aggregate.json"
    if aggregate.is_file() and not any(item["path"] == str(aggregate.relative_to(PROJECT)) for item in mismatched):
        body = json.loads(aggregate.read_text(encoding="utf-8"))
        example = {
            "run": "benicalap_v8",
            "buildings_ok": body.get("buildings_ok"),
            "total_site_gwh": body.get("totals", {}).get("total_site_gwh"),
        }
    return {"ok": not missing and not mismatched, "present": present,
            "missing": missing, "mismatched": mismatched, "example": example}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require", action="store_true", help="return non-zero if payload is missing")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = verify()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for item in result["present"]:
            print(f"OK      {item['role']}: {item['path']}")
        for item in result["missing"]:
            print(f"MISSING {item['role']}: {item['path']}")
        for item in result["mismatched"]:
            detail = item.get("actual") or item.get("actual_sha256") or "content mismatch"
            print(f"CHANGED {item['role']}: {item['path']} ({detail})")
        if result["example"]:
            sample = result["example"]
            print(f"EXAMPLE {sample['run']}: {sample['buildings_ok']} buildings, {sample['total_site_gwh']} GWh")
        print("DISTRIBUTION READY" if result["ok"] else "DISTRIBUTION PAYLOAD INCOMPLETE")
    return 0 if result["ok"] or not args.require else 2


if __name__ == "__main__":
    raise SystemExit(main())
