#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || { printf 'Run scripts/install.sh first.\n' >&2; exit 1; }
[[ -f "$PROJECT_DIR/frontend/dist/index.html" ]] || { printf 'Frontend is not built; run scripts/install.sh first.\n' >&2; exit 1; }

export PYTHONPATH="$PROJECT_DIR/src"
export WORKBENCH_ENV="production"
export WORKBENCH_PORT="${WORKBENCH_PORT:-8765}"
export WORKBENCH_VAR_DIR="${VALENCIA_STATE_DIR:-$PROJECT_DIR/var}"
export WORKBENCH_DB_PATH="$WORKBENCH_VAR_DIR/workbench.sqlite3"
export WORKBENCH_PREVIEW_ROOT="$WORKBENCH_VAR_DIR/previews"
export WORKBENCH_IMPORT_ROOT="$WORKBENCH_VAR_DIR/imports"
export WORKBENCH_RUN_ROOT="${VALENCIA_RUN_DIR:-$PROJECT_DIR/out/ui_runs}"
export WORKBENCH_EXPORT_ROOT="$WORKBENCH_VAR_DIR/exports"
export MPLCONFIGDIR="$WORKBENCH_VAR_DIR/matplotlib"
export TMPDIR="$WORKBENCH_VAR_DIR/tmp"
export TMP="$TMPDIR"
export TEMP="$TMPDIR"
unset WORKBENCH_TEST_ROOT WORKBENCH_TEST_RUN_ID WORKBENCH_TEST_REQUIRE_HEADER
mkdir -p "$WORKBENCH_VAR_DIR" "$WORKBENCH_RUN_ROOT" "$TMPDIR"

URL="http://127.0.0.1:$WORKBENCH_PORT/#/files"
if [[ "${VALENCIA_NO_BROWSER:-0}" != "1" ]]; then
  (
    sleep 2
    if [[ "$(uname -s)" == "Darwin" ]]; then open "$URL"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1
    fi
  ) &
fi
printf 'Building Stock Energy Workbench: %s\nPress Ctrl+C to stop.\n' "$URL"
cd "$PROJECT_DIR"
exec "$PYTHON" -m workbench
