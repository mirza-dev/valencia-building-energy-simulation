"""Keep the Python test suite away from persistent Workbench state."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


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


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(TEST_STATE_ROOT, ignore_errors=True)
