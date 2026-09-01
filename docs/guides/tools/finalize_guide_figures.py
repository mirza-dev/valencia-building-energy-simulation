#!/usr/bin/env python3
"""Build reviewed numbered-callout assets from the real figure captures."""

from __future__ import annotations

import json
from pathlib import Path

from annotate_guide_figure import annotate


POSITIONS: dict[str, list[tuple[float, float]]] = {
    "files-overview": [(0.775, 0.128), (0.42, 0.656), (0.38, 0.826)],
    "files-prepared-stock": [(0.315, 0.586), (0.38, 0.656), (0.44, 0.723)],
    "files-building-database": [(0.44, 0.586), (0.3, 0.729), (0.2, 0.765)],
    "files-climate-event-chain": [(0.245, 0.381), (0.76, 0.316), (0.4, 0.875)],
    "run-preflight": [(0.29, 0.246), (0.535, 0.508), (0.35, 0.632)],
    "run-live-ledger": [(0.815, 0.311), (0.585, 0.372), (0.665, 0.46)],
    "run-stop-resume": [(0.815, 0.311), (0.535, 0.508), (0.365, 0.872)],
    "outputs-final-overview": [(0.36, 0.168), (0.79, 0.303), (0.27, 0.331)],
    "outputs-cluster-district": [(0.59, 0.366), (0.755, 0.487), (0.34, 0.828)],
    "outputs-building-ledger": [(0.69, 0.792), (0.9, 0.262), (0.955, 0.567)],
    "outputs-readable-report": [(0.31, 0.037), (0.3, 0.535), (0.3, 0.615)],
    "outputs-energyplus-tables": [(0.155, 0.105), (0.14, 0.437), (0.2, 0.355)],
    "outputs-geometry-viewer": [(0.71, 0.269), (0.755, 0.435), (0.945, 0.893)],
    "outputs-exports": [(0.556, 0.205), (0.754, 0.205), (0.943, 0.205)],
    "outputs-valencia-heatmap": [(0.31, 0.131), (0.825, 0.931), (0.725, 0.973)],
    "outputs-lecco-event": [(0.85, 0.283), (0.6, 0.42), (0.72, 0.397)],
}


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    source = root / "docs" / "guides" / "source"
    plan_path = source / "user-guide-figure-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for figure in plan["figures"]:
        figure_id = figure["id"]
        positions = POSITIONS[figure_id]
        labels = figure["callout_labels"]
        if len(positions) != len(labels):
            raise ValueError(f"{figure_id}: positions and labels differ")
        callout_path = source / figure["callouts"]
        callout_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema": "bsew-figure-callouts-v1",
            "figure_id": figure_id,
            "markers": [
                {"number": index, "x": x, "y": y, "label": label}
                for index, ((x, y), label) in enumerate(zip(positions, labels), start=1)
            ],
        }
        callout_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        annotate(
            source / figure["source_asset"],
            callout_path,
            source / figure["asset"],
        )
    print(f"GUIDE_FIGURES_ANNOTATED_OK {len(plan['figures'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
