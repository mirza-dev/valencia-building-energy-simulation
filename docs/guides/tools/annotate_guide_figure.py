#!/usr/bin/env python3
"""Add deterministic numbered callouts to a real Workbench PNG.

The tool never resamples or invents interface pixels.  It draws only the
numbered markers described by a reviewed JSON sidecar and embeds hashes of the
source image and annotation record in the output PNG metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin


SCHEMA = "bsew-figure-callouts-v1"
PETROL = (13, 73, 79, 255)
WHITE = (255, 255, 255, 255)
SHADOW = (0, 0, 0, 90)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def validate(record: Any) -> list[dict[str, Any]]:
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ValueError(f"annotation JSON must use schema {SCHEMA!r}")
    markers = record.get("markers")
    if not isinstance(markers, list) or not markers:
        raise ValueError("annotation JSON must contain at least one marker")
    expected_numbers = list(range(1, len(markers) + 1))
    observed_numbers: list[int] = []
    for marker in markers:
        if not isinstance(marker, dict):
            raise ValueError("every marker must be an object")
        number = marker.get("number")
        x = marker.get("x")
        y = marker.get("y")
        label = marker.get("label")
        if not isinstance(number, int):
            raise ValueError("marker number must be an integer")
        if not isinstance(x, (int, float)) or not 0.0 <= float(x) <= 1.0:
            raise ValueError(f"marker {number} x must be a normalised value from 0 to 1")
        if not isinstance(y, (int, float)) or not 0.0 <= float(y) <= 1.0:
            raise ValueError(f"marker {number} y must be a normalised value from 0 to 1")
        if not isinstance(label, str) or not label.strip() or not label.strip().endswith("."):
            raise ValueError(f"marker {number} label must be a complete sentence")
        observed_numbers.append(number)
    if observed_numbers != expected_numbers:
        raise ValueError(
            f"marker numbers must be consecutive and ordered: expected "
            f"{expected_numbers}, observed {observed_numbers}"
        )
    return markers


def annotate(source: Path, annotations: Path, output: Path) -> None:
    if output.resolve() == source.resolve():
        raise ValueError("output must not overwrite the unannotated source capture")
    record = json.loads(annotations.read_text(encoding="utf-8"))
    markers = validate(record)
    with Image.open(source) as opened:
        if opened.format != "PNG":
            raise ValueError("source capture must be a true PNG")
        image = opened.convert("RGBA")
    width, height = image.size
    if width < 1200 or height < 675:
        raise ValueError(f"source capture is below publication size: {width}x{height}")

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # Smaller markers than the first draft: at 1440x900 the earlier 23 px radius
    # could not be placed beside a control without covering its own label, so
    # several markers landed on top of interface text or measured values.
    radius = max(14, round(min(width, height) * 0.018))
    font = load_font(max(18, round(radius * 1.08)))
    for marker in markers:
        cx = round(float(marker["x"]) * (width - 1))
        cy = round(float(marker["y"]) * (height - 1))
        if not radius <= cx < width - radius or not radius <= cy < height - radius:
            raise ValueError(
                f"marker {marker['number']} is too close to the image edge for a complete callout"
            )
        draw.ellipse(
            (cx - radius + 3, cy - radius + 4, cx + radius + 3, cy + radius + 4),
            fill=SHADOW,
        )
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=PETROL,
            outline=WHITE,
            width=max(2, radius // 8),
        )
        text = str(marker["number"])
        box = draw.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        draw.text(
            (cx - tw / 2, cy - th / 2 - box[1]),
            text,
            fill=WHITE,
            font=font,
        )

    result = Image.alpha_composite(image, overlay).convert("RGB")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("bsew:source_sha256", sha256(source))
    metadata.add_text("bsew:callouts_sha256", canonical_hash(record))
    metadata.add_text("bsew:callout_schema", SCHEMA)
    output.parent.mkdir(parents=True, exist_ok=True)
    result.save(output, format="PNG", optimize=False, pnginfo=metadata)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    annotate(args.input, args.annotations, args.output)
    print(f"ANNOTATED_FIGURE_OK {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
