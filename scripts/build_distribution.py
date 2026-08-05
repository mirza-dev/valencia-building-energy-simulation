#!/usr/bin/env python3
"""Build the self-contained research distribution as a ZIP64 archive.

The source repository intentionally excludes licensed/third-party inputs and the
multi-gigabyte finished example. This maintainer command combines a clean Git
checkout with the payload described by ``distribution-manifest.json``.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
ROOT_NAME = "valencia-stock-energy-workbench"


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=PROJECT, check=True, capture_output=True, text=True,
    ).stdout


def payload_files() -> list[Path]:
    manifest = json.loads((PROJECT / "distribution-manifest.json").read_text(encoding="utf-8"))
    paths: set[Path] = set()
    for item in manifest["payload"]:
        source = PROJECT / item["path"]
        if not source.exists():
            raise FileNotFoundError(f"required release payload is missing: {item['path']}")
        if source.is_dir():
            paths.update(path for path in source.rglob("*") if path.is_file() and not path.is_symlink())
        elif not source.is_symlink():
            paths.add(source)

    # Preserve the projection and vendor metadata that travel with the
    # shapefile, even though the three core members are enough for hash gating.
    paths.update(
        path for path in (PROJECT / "data/gis").glob("DatosRai_ciudadValencia.*")
        if path.is_file() and not path.is_symlink()
    )
    return sorted(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="destination .zip path")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.suffix.lower() != ".zip":
        parser.error("output must end in .zip")
    if git("status", "--porcelain").strip():
        raise SystemExit("Release builds require a clean Git worktree.")

    tracked = [PROJECT / item for item in git("ls-files").splitlines() if item]
    files = sorted({path for path in tracked + payload_files() if path.is_file() and not path.is_symlink()})
    if output in files:
        raise SystemExit("The output archive cannot be one of its own source files.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    if temporary.exists():
        raise SystemExit(f"Temporary output already exists: {temporary}")

    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True,
        ) as archive:
            for index, source in enumerate(files, start=1):
                relative = source.relative_to(PROJECT)
                archive.write(source, f"{ROOT_NAME}/{relative.as_posix()}")
                if index % 500 == 0:
                    print(f"Packed {index:,}/{len(files):,} files", flush=True)
        temporary.replace(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    print(f"Distribution written: {output}")
    print(f"Files: {len(files):,} | ZIP bytes: {output.stat().st_size:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
