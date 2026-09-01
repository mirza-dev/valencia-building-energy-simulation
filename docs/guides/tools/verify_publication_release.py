#!/usr/bin/env python3
"""Run every BSEW publication gate and verify the document manifest.

The script is read-only. It coordinates the focused verifiers rather than
reimplementing their scientific, installation, layout or figure contracts.
Run it with the bundled document Python used for render acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_gate(command: list[str], label: str, errors: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    output = "\n".join(item for item in (result.stdout.strip(), result.stderr.strip()) if item)
    if result.returncode:
        errors.append(f"{label} failed ({result.returncode})\n{output}")
    return output


def expected_outputs(final: bool, publication_date: str) -> set[str]:
    suffix = "" if final else f" (Provisional {publication_date})"
    return {
        f"Building Stock Energy Workbench - Installation Guide{suffix}.docx",
        f"Building Stock Energy Workbench - Installation Guide{suffix}.pdf",
        f"Building Stock Energy Workbench - User Guide{suffix}.docx",
        f"Building Stock Energy Workbench - User Guide{suffix}.pdf",
        f"Building Stock Energy Workbench - Valencia Simulation Report{suffix}.docx",
        f"Building Stock Energy Workbench - Valencia Simulation Report{suffix}.pdf",
        "installation-guide.html",
        "user-guide.html",
        "valencia-simulation-report.html",
    }


def verify_manifest(
    root: Path,
    source_dir: Path,
    output_dir: Path,
    publication_date: str,
    final: bool,
    errors: list[str],
    notes: list[str],
) -> None:
    name = "document-manifest.json" if final else "document-manifest.provisional.json"
    path = output_dir / name
    if not path.is_file():
        errors.append(f"document manifest is missing: {path}")
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"document manifest cannot be read: {exc}")
        return

    expected_status = "final" if final else "provisional"
    if manifest.get("document_status") != expected_status:
        errors.append(f"manifest status is not {expected_status!r}")
    if manifest.get("publication_date") != publication_date:
        errors.append("manifest publication date differs from the requested release date")
    if manifest.get("product") != "Building Stock Energy Workbench":
        errors.append("manifest product name is not Building Stock Energy Workbench")

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False
    ).stdout.strip()
    if not commit or manifest.get("software_commit") != commit:
        errors.append(
            f"manifest software commit differs from HEAD: manifest={manifest.get('software_commit')!r}, HEAD={commit!r}"
        )

    source_files = {
        item.name: item
        for item in source_dir.iterdir()
        if item.is_file()
        and not item.name.startswith(".")
        and item.suffix.lower() in {".md", ".json"}
    }
    recorded_sources = manifest.get("source") or {}
    missing_sources = sorted(set(source_files) - set(recorded_sources))
    extra_sources = sorted(set(recorded_sources) - set(source_files))
    if missing_sources or extra_sources:
        message = f"manifest/source set mismatch: missing={missing_sources!r}, extra={extra_sources!r}"
        (errors if final else notes).append(message)
    for item_name in sorted(set(source_files) & set(recorded_sources)):
        if recorded_sources[item_name] != sha256(source_files[item_name]):
            errors.append(f"manifest source hash mismatch: {item_name}")

    expected = expected_outputs(final, publication_date)
    recorded_outputs = manifest.get("outputs") or {}
    missing_outputs = sorted(expected - set(recorded_outputs))
    extra_outputs = sorted(set(recorded_outputs) - expected)
    if missing_outputs or extra_outputs:
        errors.append(f"manifest/output set mismatch: missing={missing_outputs!r}, extra={extra_outputs!r}")
    for item_name in sorted(expected & set(recorded_outputs)):
        item = output_dir / item_name
        if not item.is_file():
            errors.append(f"manifest output is absent: {item_name}")
        elif recorded_outputs[item_name] != sha256(item):
            errors.append(f"manifest output hash mismatch: {item_name}")

    note = str(manifest.get("note") or "")
    if final and "pending" in note.lower():
        errors.append("final manifest note still says evidence is pending")
    serialized = json.dumps(manifest)
    if "/Volumes/" in serialized or "/Users/" in serialized:
        errors.append("manifest leaks an absolute local path")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--publication-date", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()

    root = args.project_root.resolve()
    source = args.source_dir.resolve()
    output = args.output_dir.resolve()
    tools = root / "docs/guides/tools"
    errors: list[str] = []
    notes: list[str] = []
    final_flag = ["--final"] if args.final else []
    commands = [
        (
            "cross-format guide verification",
            [sys.executable, str(tools / "verify_guides.py"), "--source-dir", str(source),
             "--output-dir", str(output), "--publication-date", args.publication_date, *final_flag],
        ),
        (
            "Installation Guide contract",
            [sys.executable, str(tools / "verify_installation_guide_contract.py"),
             "--project-root", str(root), "--guide", str(source / "installation-guide.md"), *final_flag],
        ),
        (
            "User Guide product contract",
            [sys.executable, str(tools / "verify_product_guide_contract.py"),
             "--project-root", str(root), "--guide", str(source / "user-guide.md"), *final_flag],
        ),
        (
            "heat-map publication contract",
            [sys.executable, str(tools / "verify_heatmap_contract.py"),
             "--project-root", str(root), "--guide", str(source / "user-guide.md"), *final_flag],
        ),
        (
            "User Guide figure contract",
            [sys.executable, str(tools / "verify_user_guide_figures.py"),
             "--project-root", str(root), "--guide", str(source / "user-guide.md"),
             "--plan", str(source / "user-guide-figure-plan.json"), *final_flag],
        ),
    ]

    outputs: list[tuple[str, str]] = []
    for label, command in commands:
        outputs.append((label, run_gate(command, label, errors)))
    verify_manifest(root, source, output, args.publication_date, args.final, errors, notes)

    if errors:
        print("PUBLICATION_RELEASE_FAILED")
        for error in errors:
            print(f"- {error}")
        return 1

    print("PUBLICATION_RELEASE_OK")
    for label, gate_output in outputs:
        headline = gate_output.splitlines()[0] if gate_output else "NO OUTPUT"
        print(f"- {label}: {headline}")
    for note in notes:
        print(f"- PROVISIONAL: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
