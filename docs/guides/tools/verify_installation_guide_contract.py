#!/usr/bin/env python3
"""Check Installation Guide claims against the shipped platform contracts.

The verifier reads scripts and manifests as text; it never imports product or
simulation modules and is safe to run while a protected stock run is active.
Provisional mode reports known post-run gaps.  Final mode fails until those
gaps are closed in the release being documented.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PRODUCT = "Building Stock Energy Workbench"


def require(text: str, tokens: tuple[str, ...], label: str, errors: list[str]) -> None:
    for token in tokens:
        if token not in text:
            errors.append(f"{label} is missing {token!r}")


def gap(message: str, final: bool, errors: list[str], notes: list[str]) -> None:
    (errors if final else notes).append(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--guide", type=Path, required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()

    root = args.project_root.resolve()
    guide = args.guide.read_text(encoding="utf-8")
    install = (root / "scripts/install.ps1").read_text(encoding="utf-8")
    start = (root / "scripts/start.ps1").read_text(encoding="utf-8")
    update = (root / "scripts/update.ps1").read_text(encoding="utf-8")
    toolchain = (root / "src/verify_toolchain.py").read_text(encoding="utf-8")
    distribution = json.loads((root / "distribution-manifest.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    notes: list[str] = []

    require(
        guide,
        (
            "64-bit Windows 10 or Windows 11",
            "`py -3.13`",
            "OpenStudio 3.11.0",
            "EnergyPlus 25.2.0",
            "20.19.0",
            "Get-FileHash -Algorithm SHA256",
            "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force",
            "Invoke-RestMethod http://127.0.0.1:8765/api/health",
            "controlled static verification — not executed live on Windows",
        ),
        "Windows installation procedure",
        errors,
    )
    require(
        install,
        (
            "Get-Command git",
            "Get-Command node",
            "Get-Command npm",
            "$PythonPrefix = @('-3.13')",
            "a===20 && b>=19",
            "src\\verify_toolchain.py",
            "scripts\\verify_distribution.py",
            "--require",
            "Installation complete. Start with: .\\scripts\\start.ps1",
        ),
        "install.ps1",
        errors,
    )
    require(
        toolchain,
        (
            'REQUIRED_PYTHON = (3, 13)',
            'REQUIRED_OPENSTUDIO = "3.11.0"',
            'REQUIRED_ENERGYPLUS = "25.2.0"',
            ">>> TOOLCHAIN OK <<<",
        ),
        "toolchain verifier",
        errors,
    )
    require(
        start,
        (
            "http://127.0.0.1:$Port/#/files",
            "$env:WORKBENCH_ENV = 'production'",
            "$env:VALENCIA_NO_BROWSER -ne '1'",
        ),
        "start.ps1",
        errors,
    )
    require(update, ("git pull --ff-only", "scripts\\install.ps1"), "update.ps1", errors)

    if distribution.get("required_python") != "3.13":
        errors.append("distribution manifest does not require Python 3.13")
    if distribution.get("openstudio_version") != "3.11.0":
        errors.append("distribution manifest does not require OpenStudio 3.11.0")
    if distribution.get("energyplus_version") != "25.2.0":
        errors.append("distribution manifest does not require EnergyPlus 25.2.0")

    # Locked post-run installer gaps.  These are structural checks, not a
    # substitute for native Windows execution evidence.
    # The gap is an *unconditional* Git requirement: a downloaded release bundle
    # is not a repository and must not be asked for one.  Requiring it where the
    # directory really is a checkout - the case `update.ps1` serves - is correct,
    # so the check looks for the guard rather than for the string.
    if "Get-Command git" in install and not re.search(
        r"Test-Path[^\n]*\.git'[\s\S]{0,200}?Get-Command git", install):
        gap("release-bundle install.ps1 still requires Git", args.final, errors, notes)
    if not re.search(
        r"&\s*\$VenvPython\s+-c\s+.+sys\.version_info\[:2\].+\(3,\s*13\)",
        install,
        re.DOTALL,
    ):
        gap("install.ps1 does not validate an existing .venv as Python 3.13", args.final, errors, notes)
    if "Python 3.13 is registered with the py launcher" not in install:
        gap("py-present/py-3.13-missing failure is not yet explicit and actionable", args.final, errors, notes)

    # Product-name gaps are separate from the three installer contracts but
    # must also be gone before a final BSEW release is accepted.
    if distribution.get("product") != PRODUCT:
        gap(
            f"distribution manifest product is {distribution.get('product')!r}, expected {PRODUCT!r}",
            args.final,
            errors,
            notes,
        )
    if "Valencia Workbench:" in start:
        gap("start.ps1 still prints the retired Valencia Workbench product name", args.final, errors, notes)

    marker = re.compile(r"\b(?:provisional|pending|capture pending|publication edition)\b", re.IGNORECASE)
    if args.final and marker.search(guide):
        errors.append("final Installation Guide still contains provisional/pending language")

    if errors:
        print("INSTALLATION_GUIDE_CONTRACT_FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("INSTALLATION_GUIDE_CONTRACT_OK")
    for note in notes:
        print(f"- KNOWN POST-RUN GAP: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
