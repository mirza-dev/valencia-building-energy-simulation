#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }
command -v python3.13 >/dev/null 2>&1 || fail "Python 3.13 is required: https://www.python.org/downloads/"
command -v node >/dev/null 2>&1 || fail "Node.js 20.19+ is required: https://nodejs.org/en/download"
command -v npm >/dev/null 2>&1 || fail "npm is required and normally ships with Node.js: https://nodejs.org/en/download"
command -v git >/dev/null 2>&1 || fail "Git is required for updates: https://git-scm.com/downloads"

python3.13 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' \
  || fail "This package requires Python 3.13.x."
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>20 || (a===20 && b>=19) ? 0 : 1)' \
  || fail "Node.js 20.19 or newer is required: https://nodejs.org/en/download"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  python3.13 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$PROJECT_DIR/.venv/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
npm --prefix "$PROJECT_DIR/frontend" ci
npm --prefix "$PROJECT_DIR/frontend" run build

if ! "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/src/verify_toolchain.py"; then
  fail "OpenStudio 3.11.0 with bundled EnergyPlus 25.2.0 was not found. Install it from https://github.com/NREL/OpenStudio/releases/tag/v3.11.0 or set VALENCIA_EPLUS_DIR."
fi
if ! "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/verify_distribution.py" --require; then
  fail "This source checkout does not contain the release data payload. Use the full Valencia Workbench release bundle or add the four inputs in Files."
fi

printf '\nInstallation complete. Start with: %s/scripts/start.sh\n' "$PROJECT_DIR"
