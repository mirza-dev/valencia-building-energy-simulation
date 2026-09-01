"""Verify the exact runtime required by Building Stock Energy Workbench.

Run with::

    .venv/bin/python src/verify_toolchain.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REQUIRED_PYTHON = (3, 13)
REQUIRED_OPENSTUDIO = "3.11.0"
REQUIRED_ENERGYPLUS = "25.2.0"


def fail(message: str) -> None:
    raise SystemExit(f"TOOLCHAIN ERROR: {message}")


if sys.version_info[:2] != REQUIRED_PYTHON:
    fail(f"Python 3.13.x is required; found {sys.version.split()[0]}")

import openstudio  # noqa: E402

openstudio_version = openstudio.openStudioVersion()
print("OpenStudio SDK :", openstudio_version)
if openstudio_version != REQUIRED_OPENSTUDIO:
    fail(f"OpenStudio {REQUIRED_OPENSTUDIO} is required; found {openstudio_version}")

# A small SDK smoke test proves that the installed binding can create and
# translate geometry, not merely that its package metadata has the right label.
model = openstudio.model.Model()
points = [
    openstudio.Point3d(0, 0, 0),
    openstudio.Point3d(0, 8, 0),
    openstudio.Point3d(10, 8, 0),
    openstudio.Point3d(10, 0, 0),
]
space = openstudio.model.Space.fromFloorPrint(points, 3.0, model)
if not space.is_initialized():
    fail("OpenStudio Space.fromFloorPrint smoke test failed")
print("Space.fromFloorPrint: OK")

workspace = openstudio.energyplus.ForwardTranslator().translateModel(model)
if not workspace.objects():
    fail("OpenStudio ForwardTranslator produced an empty IDF")
print("ForwardTranslator :", len(workspace.objects()), "IDF objects")

import run_simulation  # noqa: E402

energyplus_dir = Path(run_simulation.resolve_eplus_dir()).expanduser().resolve()
binary_name = "energyplus.exe" if sys.platform == "win32" else "energyplus"
energyplus_binary = energyplus_dir / binary_name
if not energyplus_binary.is_file():
    fail(f"EnergyPlus executable was not found at {energyplus_binary}")
try:
    version_output = subprocess.run(
        [str(energyplus_binary), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
except (OSError, subprocess.SubprocessError) as exc:
    fail(f"EnergyPlus could not be executed: {exc}")
if REQUIRED_ENERGYPLUS not in version_output:
    fail(f"EnergyPlus {REQUIRED_ENERGYPLUS} is required; found {version_output or 'unknown'}")
print("EnergyPlus      :", version_output)

sys.path.insert(0, str(energyplus_dir))
try:
    from pyenergyplus.api import EnergyPlusAPI  # noqa: E402
except ImportError as exc:
    fail(f"pyenergyplus could not be imported from {energyplus_dir}: {exc}")
print("pyenergyplus API:", EnergyPlusAPI().api_version())

import geopandas  # noqa: E402
import matplotlib  # noqa: E402
import numpy  # noqa: E402
import pandas  # noqa: E402
import scipy  # noqa: E402

print(
    "Analysis/GIS    : pandas", pandas.__version__,
    "| geopandas", geopandas.__version__,
    "| numpy", numpy.__version__,
    "| scipy", scipy.__version__,
    "| matplotlib", matplotlib.__version__,
)
print(">>> TOOLCHAIN OK <<<")
