#!/usr/bin/env python3
"""Validate the User Guide's real-figure plan and final captured assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
from pathlib import Path

from PIL import Image


SCHEMA = "bsew-user-guide-figure-plan-v1"
CALLOUT_SCHEMA = "bsew-figure-callouts-v1"
PRODUCT_NAME = "BUILDING STOCK ENERGY WORKBENCH"
REQUIRED_SECTIONS = {"files", "run", "outputs", "report", "geometry", "exports", "heatmap", "event"}
ACCEPTANCE_KEYS = {"final_ui", "no_local_path", "legible", "alt_caption_checked"}
REQUIRED_FORBIDDEN_TEXT = {"/Volumes/", "/Users/", "VALENCIA / STOCK", "Energy simulation workbench"}
LOCAL_PATH_FRAGMENT = re.compile(r"(?:/Users/|/Volumes/|file://|[A-Za-z]:\\Users\\)")


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("not a valid PNG header")
    return struct.unpack(">II", header[16:24])


def canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def safe_relative_path(value: object, *, prefix: str, suffix: str) -> Path | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not value.startswith(prefix)
        or path.suffix.lower() != suffix
    ):
        return None
    return path


def validate_annotated_asset(
    *,
    plan_dir: Path,
    figure_id: str,
    asset: str,
    source_asset: str,
    callouts: str,
    expected_labels: list[str],
) -> list[str]:
    errors: list[str] = []
    target = plan_dir / asset
    source = plan_dir / source_asset
    callout_path = plan_dir / callouts
    for label, path in (("asset", target), ("source capture", source), ("callout record", callout_path)):
        if not path.is_file():
            errors.append(f"figure {figure_id!r} {label} is missing: {path}")
    if errors:
        return errors

    try:
        record = json.loads(callout_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"figure {figure_id!r} callout record is unreadable: {exc}"]
    markers = record.get("markers") if isinstance(record, dict) else None
    if not isinstance(record, dict) or record.get("schema") != CALLOUT_SCHEMA:
        errors.append(f"figure {figure_id!r} callout schema must be {CALLOUT_SCHEMA!r}")
        markers = []
    if not isinstance(markers, list) or not markers:
        errors.append(f"figure {figure_id!r} callout record must contain markers")
        markers = []
    observed_labels: list[str] = []
    for index, marker in enumerate(markers, start=1):
        if not isinstance(marker, dict):
            errors.append(f"figure {figure_id!r} marker {index} is not an object")
            continue
        number, x, y, label = (
            marker.get("number"), marker.get("x"), marker.get("y"), marker.get("label")
        )
        if number != index:
            errors.append(f"figure {figure_id!r} marker numbers are not consecutive at {index}")
        if not isinstance(x, (int, float)) or not 0.0 <= float(x) <= 1.0:
            errors.append(f"figure {figure_id!r} marker {index} has invalid x")
        if not isinstance(y, (int, float)) or not 0.0 <= float(y) <= 1.0:
            errors.append(f"figure {figure_id!r} marker {index} has invalid y")
        if not isinstance(label, str) or not label.strip().endswith("."):
            errors.append(f"figure {figure_id!r} marker {index} label is not a complete sentence")
        else:
            observed_labels.append(label.strip())
    if observed_labels != expected_labels:
        errors.append(
            f"figure {figure_id!r} callout labels differ from the reviewed plan: "
            f"expected={expected_labels!r}, observed={observed_labels!r}"
        )

    try:
        with Image.open(source) as image:
            if image.format != "PNG":
                errors.append(f"figure {figure_id!r} source capture is not a true PNG")
            source_size = image.size
        with Image.open(target) as image:
            if image.format != "PNG":
                errors.append(f"figure {figure_id!r} annotated asset is not a true PNG")
            target_size = image.size
            metadata = dict(image.info or {})
    except (OSError, ValueError) as exc:
        errors.append(f"figure {figure_id!r} PNG cannot be inspected: {exc}")
        return errors
    if source_size != target_size:
        errors.append(
            f"figure {figure_id!r} was resampled: source={source_size}, annotated={target_size}"
        )
    expected_metadata = {
        "bsew:source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "bsew:callouts_sha256": canonical_hash(record),
        "bsew:callout_schema": CALLOUT_SCHEMA,
    }
    for key, expected in expected_metadata.items():
        if metadata.get(key) != expected:
            errors.append(
                f"figure {figure_id!r} provenance metadata {key!r} mismatch: "
                f"expected {expected!r}, observed {metadata.get(key)!r}"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--guide", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()

    root = args.project_root.resolve()
    guide = args.guide.read_text(encoding="utf-8")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    errors: list[str] = []
    notes: list[str] = []

    if plan.get("schema") != SCHEMA:
        errors.append(f"figure-plan schema must be {SCHEMA!r}")
    if plan.get("required_product_name") != PRODUCT_NAME:
        errors.append(f"required visible product name must be {PRODUCT_NAME!r}")
    forbidden_text = set(plan.get("forbidden_visible_text") or [])
    if not REQUIRED_FORBIDDEN_TEXT <= forbidden_text:
        errors.append(
            "figure plan does not forbid all local-path and retired-brand strings: "
            f"{sorted(REQUIRED_FORBIDDEN_TEXT - forbidden_text)!r}"
        )
    viewport = plan.get("viewport") or {}
    if viewport != {"width": 1440, "height": 900, "device_scale_factor": 1}:
        errors.append(f"capture viewport changed: {viewport!r}")
    figures = plan.get("figures") or []
    if not 12 <= len(figures) <= 16:
        errors.append(f"figure plan must contain 12-16 real visuals, found {len(figures)}")

    ids = [item.get("id") for item in figures]
    assets = [item.get("asset") for item in figures]
    source_assets = [item.get("source_asset") for item in figures]
    callout_records = [item.get("callouts") for item in figures]
    if len(set(ids)) != len(ids):
        errors.append("figure-plan ids are not unique")
    if len(set(assets)) != len(assets):
        errors.append("figure-plan assets are not unique")
    if len(set(source_assets)) != len(source_assets):
        errors.append("figure-plan source captures are not unique")
    if len(set(callout_records)) != len(callout_records):
        errors.append("figure-plan callout records are not unique")
    sections = {item.get("section") for item in figures}
    missing_sections = sorted(REQUIRED_SECTIONS - sections)
    if missing_sections:
        errors.append(f"figure plan misses required lifecycle sections: {missing_sections!r}")

    required_fields = {
        "id", "asset", "source_asset", "callouts", "callout_labels", "section",
        "route", "state", "capture", "alt", "caption", "status", "acceptance"
    }
    for item in figures:
        missing = sorted(required_fields - set(item))
        if missing:
            errors.append(f"figure {item.get('id')!r} misses fields {missing!r}")
            continue
        asset = str(item["asset"])
        if not asset.startswith("assets/") or ".." in Path(asset).parts or not asset.endswith(".png"):
            errors.append(f"figure {item['id']!r} has unsafe/non-PNG asset path {asset!r}")
        if safe_relative_path(item["source_asset"], prefix="capture-sources/", suffix=".png") is None:
            errors.append(
                f"figure {item['id']!r} has unsafe source-capture path {item['source_asset']!r}"
            )
        if safe_relative_path(item["callouts"], prefix="callouts/", suffix=".json") is None:
            errors.append(f"figure {item['id']!r} has unsafe callout path {item['callouts']!r}")
        labels = item["callout_labels"]
        if (
            not isinstance(labels, list)
            or not 1 <= len(labels) <= 6
            or any(not isinstance(label, str) or not label.strip().endswith(".") for label in labels)
        ):
            errors.append(
                f"figure {item['id']!r} must define 1-6 complete-sentence callout labels"
            )
        if not str(item["alt"]).endswith(".") or not str(item["caption"]).endswith("."):
            errors.append(f"figure {item['id']!r} alt and caption must be complete sentences")
        acceptance = item["acceptance"]
        if set(acceptance) != ACCEPTANCE_KEYS or not all(isinstance(value, bool) for value in acceptance.values()):
            errors.append(f"figure {item['id']!r} has an invalid acceptance record")
        fixture = item.get("fixture")
        if fixture is not None:
            fixture_path = Path(str(fixture))
            if (
                fixture_path.is_absolute()
                or ".." in fixture_path.parts
                or not str(fixture_path).startswith("fixtures/")
                or fixture_path.suffix.lower() != ".json"
            ):
                errors.append(f"figure {item['id']!r} has an unsafe fixture path {fixture!r}")
                continue
            target = args.plan.parent / fixture_path
            if not target.is_file():
                errors.append(f"figure {item['id']!r} fixture is missing: {target}")
                continue
            try:
                fixture_data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"figure {item['id']!r} fixture is unreadable: {exc}")
                continue
            fixture_text = target.read_text(encoding="utf-8")
            if not fixture_data.get("path_scrubbed") or LOCAL_PATH_FRAGMENT.search(fixture_text):
                errors.append(f"figure {item['id']!r} fixture is not path-scrubbed")
            if "not a final result" not in str(fixture_data.get("publication_status", "")):
                errors.append(f"figure {item['id']!r} fixture lacks an explicit non-final boundary")
            expected_hash = item.get("fixture_sha256")
            observed_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                errors.append(f"figure {item['id']!r} lacks a valid fixture SHA-256")
            elif expected_hash != observed_hash:
                errors.append(
                    f"figure {item['id']!r} fixture hash mismatch: "
                    f"expected {expected_hash}, observed {observed_hash}"
                )

    references = re.findall(r"^!\[[^]]+\]\((assets/[^ )]+\.png)(?:\s+\"[^\"]*\")?\)", guide, re.MULTILINE)
    missing_refs = [asset for asset in assets if asset not in references]
    extra_refs = [asset for asset in references if asset not in assets]
    pending = [item["id"] for item in figures if item.get("status") != "accepted"]

    if args.final:
        if missing_refs or extra_refs:
            errors.append(f"final guide/plan figure mismatch: missing={missing_refs!r}, extra={extra_refs!r}")
        if pending:
            errors.append(f"final figure plan still has non-accepted figures: {pending!r}")
        for item in figures:
            if not all(item["acceptance"].values()):
                errors.append(f"figure {item['id']!r} lacks complete visual/privacy acceptance")
            target = args.plan.parent / item["asset"]
            if not target.is_file():
                errors.append(f"figure {item['id']!r} asset is missing: {target}")
                continue
            try:
                width, height = png_dimensions(target)
            except ValueError as exc:
                errors.append(f"figure {item['id']!r}: {exc}")
                continue
            if width < 1200 or height < 675:
                errors.append(f"figure {item['id']!r} is too small for publication: {width}x{height}")
            errors.extend(
                validate_annotated_asset(
                    plan_dir=args.plan.parent,
                    figure_id=str(item["id"]),
                    asset=str(item["asset"]),
                    source_asset=str(item["source_asset"]),
                    callouts=str(item["callouts"]),
                    expected_labels=[str(label).strip() for label in item["callout_labels"]],
                )
            )
    else:
        notes.append(f"{len(references)}/{len(figures)} planned figures are referenced; {len(pending)} await final capture/acceptance")

    if errors:
        print("USER_GUIDE_FIGURES_FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("USER_GUIDE_FIGURES_OK")
    for note in notes:
        print(f"- PROVISIONAL: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
