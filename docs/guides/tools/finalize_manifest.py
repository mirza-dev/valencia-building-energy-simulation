#!/usr/bin/env python3
"""Add verified PDF hashes to an already-generated BSEW document manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="Recompute hashes for every top-level authoritative source file.",
    )
    parser.add_argument(
        "--manifest-name", default="document-manifest.provisional.json")
    args = parser.parse_args()

    manifest_path = args.output_dir / args.manifest_name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    status = str(manifest.get("document_status") or "").strip().lower()
    if status not in {"provisional", "final"}:
        raise SystemExit(f"unsupported document_status in manifest: {status!r}")
    existing = set(manifest.get("outputs", {}))
    expected_docx = sorted(args.output_dir.glob("*.docx"))
    expected_html = sorted(args.output_dir.glob("*.html"))
    expected_pdf = sorted(args.output_dir.glob("*.pdf"))
    if len(expected_docx) != 3 or len(expected_html) != 3 or len(expected_pdf) != 3:
        raise SystemExit(
            "expected exactly three DOCX, three HTML and three PDF outputs; "
            f"found {len(expected_docx)}, {len(expected_html)}, {len(expected_pdf)}")
    expected = expected_docx + expected_html + expected_pdf
    generated_names = {path.name for path in expected}
    unexpected = existing - generated_names
    if unexpected:
        raise SystemExit(f"manifest names are not present in output directory: {sorted(unexpected)}")

    manifest["outputs"] = {
        path.name: sha256(path) for path in sorted(expected, key=lambda item: item.name)
    }
    if args.source_dir is not None:
        source_dir = args.source_dir.resolve()
        if not source_dir.is_dir():
            raise SystemExit(f"source directory does not exist: {source_dir}")
        source_files = sorted(
            item
            for item in source_dir.iterdir()
            if item.is_file()
            and not item.name.startswith(".")
            and item.suffix.lower() in {".md", ".json"}
        )
        if not source_files:
            raise SystemExit(f"source directory contains no authoritative files: {source_dir}")
        manifest["source"] = {path.name: sha256(path) for path in source_files}
    manifest["note"] = (
        "DOCX, PDF and HTML hashes were recorded after DOCX-to-PDF render "
        "verification. "
        + ("Final ALL_VALENC-A_REAL evidence is pending."
           if status == "provisional"
           else "Settled ALL_VALENC-A_REAL evidence is included.")
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{manifest_path.name}.", suffix=".tmp", dir=args.output_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, manifest_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":
    main()
