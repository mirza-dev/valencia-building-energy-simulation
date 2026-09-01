#!/usr/bin/env python3
"""Fail closed on the publication heat-map contract.

This verifier is deliberately read-only and import-free.  It can run while a
protected stock simulation is active: it parses the guide and product source,
but it does not import the runner, render a map, or touch run evidence.

The provisional mode records known post-run gaps.  ``--final`` converts every
gap into a release-blocking error; behavioral pytest and rendered-image review
remain required in addition to this static contract check.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import Any


PANEL_STATISTICS_FIELDS = (
    "min",
    "p02",
    "p50",
    "p98",
    "max",
    "below_scale",
    "above_scale",
    "valid",
    "missing",
)
STATUS_CLASSES = (
    "successful",
    "excluded",
    "failed",
    "metric_missing",
)


def literal_assignment(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            if node.value is not None:
                return ast.literal_eval(node.value)
    raise ValueError(f"literal assignment {name} not found in {path}")


def guide_section(text: str) -> str:
    marker = "# 16. Heat map"
    if marker not in text:
        raise ValueError("User Guide section 16, Heat map, was not found")
    section = text.split(marker, 1)[1]
    return section.split("\n# ", 1)[0]


def require_tokens(text: str, tokens: tuple[str, ...], label: str, errors: list[str]) -> None:
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
    map_path = root / "src/results_maps.py"
    outputs_path = root / "frontend/src/components/OutputsPage.tsx"
    guide = args.guide.read_text(encoding="utf-8")
    map_source = map_path.read_text(encoding="utf-8")
    outputs_source = outputs_path.read_text(encoding="utf-8")
    errors: list[str] = []
    notes: list[str] = []

    try:
        section = guide_section(guide)
    except ValueError as exc:
        section = ""
        errors.append(str(exc))
    require_tokens(
        section,
        (
            "settled result geometry",
            "period",
            "denominator",
            "P2-P98",
            "numeric limits",
            "counts below/above",
            "Successful, excluded, failed and metric-missing",
            "must not disappear",
            "simulated, not measured consumption",
        ),
        "User Guide heat-map contract",
        errors,
    )

    expected_literals = {
        "HEATMAP_EVIDENCE_VERSION": "bsew-heatmap-evidence-v1",
        "HEATMAP_PANEL_STATISTICS_FIELDS": PANEL_STATISTICS_FIELDS,
        "HEATMAP_STATUS_CLASSES": STATUS_CLASSES,
    }
    for name, expected in expected_literals.items():
        try:
            observed = literal_assignment(map_path, name)
        except (ValueError, SyntaxError):
            gap(f"results_maps.py does not declare {name}", args.final, errors, notes)
            continue
        # Lists are accepted for the two ordered schemas; the version is a
        # string and is compared directly.
        normalized = tuple(observed) if isinstance(observed, list) else observed
        if normalized != expected:
            errors.append(f"{name} changed: expected {expected!r}, found {normalized!r}")

    if "drawn[panels[0][0]].notna()" in map_source:
        gap(
            "heat-map coverage is still inferred from the first visible metric panel instead of latest run status",
            args.final,
            errors,
            notes,
        )

    source_requirements = {
        "status-separated evidence legend": (
            "Successful result",
            "Excluded before simulation",
            "Simulation/QA failure",
            "Result recorded, metric missing",
        ),
        "scientific denominator and boundary": (
            "whole-site energy / geometric residential floor area",
            "simulated, not measured consumption",
        ),
        "panel clipping evidence": (
            "below_scale",
            "above_scale",
            "extend=\"both\"",
        ),
        "geographic completeness metadata": (
            "outside_frame",
            "views",
        ),
    }
    for label, tokens in source_requirements.items():
        missing = [token for token in tokens if token not in map_source]
        if missing:
            gap(
                f"results_maps.py lacks {label}: {missing!r}",
                args.final,
                errors,
                notes,
            )

    # A partial aggregate can contain a PNG written during re-aggregation.  It
    # must not be offered under the same final-download affordance while the
    # run is active.
    heatmap_guard = "detail.data?.running === false && summary?.results_layer?.heatmap?.written"
    if heatmap_guard not in outputs_source:
        gap(
            "Outputs does not yet guard the final Heat map download with running === false",
            args.final,
            errors,
            notes,
        )

    if errors:
        print("HEATMAP_CONTRACT_FAILED")
        for error in errors:
            print(f"- {error}")
        return 1

    print("HEATMAP_CONTRACT_OK")
    for note in notes:
        print(f"- KNOWN POST-RUN GAP: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
