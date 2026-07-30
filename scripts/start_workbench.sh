#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
cd "$PROJECT_DIR"

export PYTHONPATH="$PROJECT_DIR/src"
export WORKBENCH_ENV="production"
export WORKBENCH_PORT="${WORKBENCH_PORT:-8765}"
export WORKBENCH_VAR_DIR="$PROJECT_DIR/var"
export WORKBENCH_DB_PATH="$PROJECT_DIR/var/workbench.sqlite3"
export WORKBENCH_PREVIEW_ROOT="$PROJECT_DIR/var/previews"
export WORKBENCH_IMPORT_ROOT="$PROJECT_DIR/var/imports"
export WORKBENCH_RUN_ROOT="$PROJECT_DIR/out/ui_runs"
export WORKBENCH_EXPORT_ROOT="$PROJECT_DIR/var/exports"
export MPLCONFIGDIR="$PROJECT_DIR/var/matplotlib"
unset WORKBENCH_TEST_ROOT WORKBENCH_TEST_RUN_ID WORKBENCH_TEST_REQUIRE_HEADER
exec "$PROJECT_DIR/.venv/bin/python" -m workbench
