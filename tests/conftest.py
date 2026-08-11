"""Keep the Python test suite away from persistent Workbench state."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_BASE = Path(
    os.environ.get(
        "WORKBENCH_PYTEST_BASE",
        PROJECT_ROOT.parent / "valencia-workbench-test-state" / "pytest",
    )
).expanduser().resolve()
TEST_BASE.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(TEST_BASE)
os.environ["TMP"] = str(TEST_BASE)
os.environ["TEMP"] = str(TEST_BASE)
os.environ["PYTEST_DEBUG_TEMPROOT"] = str(TEST_BASE)
tempfile.tempdir = str(TEST_BASE)

TEST_STATE_ROOT = Path(
    tempfile.mkdtemp(prefix="valencia-workbench-pytest-", dir=TEST_BASE)
)

os.environ["WORKBENCH_ENV"] = "test"
os.environ["WORKBENCH_PORT"] = "18767"
os.environ["WORKBENCH_TEST_ROOT"] = str(TEST_STATE_ROOT)
os.environ["WORKBENCH_TEST_RUN_ID"] = "pytest"
# TestClient coverage exercises existing mutation endpoints without Playwright's request tag.
# Dedicated middleware tests opt into the strict header contract explicitly.
os.environ["WORKBENCH_TEST_REQUIRE_HEADER"] = "0"
os.environ["WORKBENCH_VAR_DIR"] = str(TEST_STATE_ROOT / "var")
os.environ["WORKBENCH_DB_PATH"] = str(TEST_STATE_ROOT / "var/workbench.sqlite3")
os.environ["WORKBENCH_PREVIEW_ROOT"] = str(TEST_STATE_ROOT / "previews")
os.environ["WORKBENCH_IMPORT_ROOT"] = str(TEST_STATE_ROOT / "imports")
os.environ["WORKBENCH_RUN_ROOT"] = str(TEST_STATE_ROOT / "runs")
os.environ["WORKBENCH_EXPORT_ROOT"] = str(TEST_STATE_ROOT / "exports")
os.environ["MPLCONFIGDIR"] = str(TEST_STATE_ROOT / "matplotlib")


def provision_reference_city() -> list[str]:
    """Register and activate Valencia the way an operator would.

    The product ships no city: an empty install reports its inputs as missing
    until they are uploaded.  Tests that need a city therefore provision one,
    which is also the honest arrangement - what a run reads is what somebody
    put in, never a shipped default.  The files still live in `data/`, because
    the frozen regression and `verified_model --verify` read them directly.

    Callable directly as well as through the fixture, because several tests
    install their own isolation inside the test body and must provision after
    that, not before it.
    """
    from workbench import db, integrity, service

    # The app normally does this on startup; provisioning happens first here,
    # so the schema has to exist before the rows do.
    service.bootstrap()
    project = PROJECT_ROOT
    inputs = [
        ("valencia-city", "gis", "Valencia city buildings",
         project / "data/gis/DatosRai_ciudadValencia.shp"),
        ("tipo15-ledger", "companion", "Tipo15 dwelling ledger",
         project / "data/reference/Tipo15_soloV(in).csv"),
        ("plantilla-v2", "template", "PlantillaOS_v2",
         project / "data/templates/PlantillaOS_v2.osm"),
        ("valencia-iwec", "weather", "Valencia IWEC",
         project / "data/weather/ESP_Valencia.082840_IWEC.epw"),
        ("valencia-iwec-ddy", "ddy", "Valencia IWEC design days",
         project / "data/weather/ESP_Valencia.082840_IWEC.ddy"),
    ]
    registered = []
    for dataset_id, kind, name, path in inputs:
        if not path.exists():
            continue
        snapshot = integrity.ensure_snapshot(path, kind=kind)
        metadata = {"managed": False, "suffix": path.suffix.lower()}
        if kind == "gis":
            gdf = service.read_gdf(path)
            metadata.update({
                "rows": len(gdf), "crs": str(gdf.crs),
                "columns": list(gdf.columns),
                "geometry_types": sorted(gdf.geometry.geom_type.dropna().unique().tolist()),
            })
        elif kind == "companion":
            from workbench.data_dictionary import companion_bootstrap_metadata
            metadata.update(companion_bootstrap_metadata())
        else:
            metadata.update(service._inspect_uploaded_dataset(kind, path))
        db.upsert_dataset({
            "id": dataset_id, "kind": kind, "name": name, "path": str(path),
            "sha256": snapshot["snapshot_hash"], "snapshot_hash": snapshot["snapshot_hash"],
            "verification_status": "VERIFIED", "read_only": True,
            "metadata": metadata | {"snapshot_components": snapshot["components"]},
        })
        registered.append(dataset_id)
    db.update_project_settings({
        field: dataset_id for field, dataset_id in (
            ("building_dataset_id", "valencia-city"),
            ("neighbor_dataset_id", "valencia-city"),
            ("tipo15_dataset_id", "tipo15-ledger"),
            ("template_dataset_id", "plantilla-v2"),
            ("weather_dataset_id", "valencia-iwec"),
            ("ddy_dataset_id", "valencia-iwec-ddy"),
        ) if dataset_id in registered
    })
    return registered


@pytest.fixture
def reference_city():
    return provision_reference_city()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(TEST_STATE_ROOT, ignore_errors=True)
