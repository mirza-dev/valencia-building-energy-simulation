#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

command -v git >/dev/null 2>&1 || { printf 'Git is required: https://git-scm.com/downloads\n' >&2; exit 1; }
if ! git diff --quiet || ! git diff --cached --quiet; then
  printf 'Local code changes are present. Commit or stash them before updating. Data and run outputs are not touched.\n' >&2
  exit 2
fi
git pull --ff-only
exec "$PROJECT_DIR/scripts/install.sh"
