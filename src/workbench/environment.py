"""Runtime identity and isolation contract for Workbench processes."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping


PROJECT = Path(__file__).resolve().parents[2]
TEST_REQUEST_HEADER = "X-Workbench-Test-Run"
_TEST_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MUTABLE_ENV_DEFAULTS = {
    "var_dir": ("WORKBENCH_VAR_DIR", PROJECT / "var"),
    "database": ("WORKBENCH_DB_PATH", PROJECT / "var/workbench.sqlite3"),
    "previews": ("WORKBENCH_PREVIEW_ROOT", PROJECT / "var/previews"),
    "imports": ("WORKBENCH_IMPORT_ROOT", PROJECT / "var/imports"),
    "runs": ("WORKBENCH_RUN_ROOT", PROJECT / "out/ui_runs"),
    "exports": ("WORKBENCH_EXPORT_ROOT", PROJECT / "var/exports"),
    "matplotlib": ("MPLCONFIGDIR", PROJECT / "var/matplotlib"),
}


def _resolve(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def runtime_environment(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Return the non-secret runtime identity exposed by the health endpoint."""
    env = os.environ if environ is None else environ
    mode = (env.get("WORKBENCH_ENV") or "production").strip().lower()
    port_raw = env.get("WORKBENCH_PORT", "8765")
    try:
        port: int | str = int(port_raw)
    except ValueError:
        port = port_raw
    test_root_raw = (env.get("WORKBENCH_TEST_ROOT") or "").strip()
    paths = {
        name: str(_resolve(env.get(variable, str(default))))
        for name, (variable, default) in _MUTABLE_ENV_DEFAULTS.items()
    }
    return {
        "mode": mode,
        "port": port,
        "test_run_id": (env.get("WORKBENCH_TEST_RUN_ID") or None) if mode == "test" else None,
        "test_root": str(_resolve(test_root_raw)) if test_root_raw else None,
        "test_request_header": TEST_REQUEST_HEADER,
        "test_request_header_required": (
            mode == "test" and env.get("WORKBENCH_TEST_REQUIRE_HEADER") == "1"
        ),
        "mutable_paths": paths,
    }


def validate_runtime_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Fail closed when a test process could write into production state."""
    env = os.environ if environ is None else environ
    descriptor = runtime_environment(env)
    errors: list[str] = []
    mode = descriptor["mode"]
    port = descriptor["port"]

    if mode not in {"production", "test"}:
        errors.append("WORKBENCH_ENV must be 'production' or 'test'")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        errors.append("WORKBENCH_PORT must be an integer between 1 and 65535")

    if mode == "production":
        leaked = [
            name for name in (
                "WORKBENCH_TEST_ROOT", "WORKBENCH_TEST_RUN_ID", "WORKBENCH_TEST_REQUIRE_HEADER",
            )
            if env.get(name)
        ]
        if leaked:
            errors.append(f"Production runtime contains test-only variables: {', '.join(leaked)}")

    if mode == "test":
        root_raw = (env.get("WORKBENCH_TEST_ROOT") or "").strip()
        run_id = (env.get("WORKBENCH_TEST_RUN_ID") or "").strip()
        if not root_raw:
            errors.append("WORKBENCH_TEST_ROOT is required in test mode")
            root = None
        else:
            root_input = Path(root_raw).expanduser()
            root = _resolve(root_input)
            if not root_input.is_absolute():
                errors.append("WORKBENCH_TEST_ROOT must be absolute")
            if root == Path(root.anchor):
                errors.append("WORKBENCH_TEST_ROOT cannot be a filesystem root")
            if _is_within(root, PROJECT):
                errors.append("WORKBENCH_TEST_ROOT must be outside the project tree")
        if not _TEST_RUN_ID.fullmatch(run_id):
            errors.append("WORKBENCH_TEST_RUN_ID must be 1-128 safe identifier characters")
        if port == 8765:
            errors.append("Test mode cannot bind the production port 8765")
        if env.get("WORKBENCH_TEST_REQUIRE_HEADER") != "1":
            errors.append("WORKBENCH_TEST_REQUIRE_HEADER=1 is required in test mode")

        for label, (variable, _default) in _MUTABLE_ENV_DEFAULTS.items():
            raw = (env.get(variable) or "").strip()
            if not raw:
                errors.append(f"{variable} must be set explicitly in test mode")
                continue
            path_input = Path(raw).expanduser()
            if not path_input.is_absolute():
                errors.append(f"{variable} must be absolute in test mode")
                continue
            if root is not None and not _is_within(_resolve(path_input), root):
                errors.append(f"{variable} ({label}) must stay inside WORKBENCH_TEST_ROOT")

    if errors:
        raise RuntimeError("Unsafe Workbench runtime environment: " + "; ".join(errors))
    return descriptor
