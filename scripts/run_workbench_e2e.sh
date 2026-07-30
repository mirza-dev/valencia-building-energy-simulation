#!/bin/zsh
set -uo pipefail

PROJECT_DIR="${0:A:h:h}"
if [[ -n "${WORKBENCH_URL:-}" ]]; then
  print -u2 "WORKBENCH_URL is forbidden: E2E must use its disposable Workbench server"
  exit 2
fi

export WORKBENCH_E2E_RUN_ID="${WORKBENCH_E2E_RUN_ID:-$(date +%s)-$$-$RANDOM}"
export WORKBENCH_E2E_BASE="${WORKBENCH_E2E_BASE:-${PROJECT_DIR:A:h}/valencia-workbench-test-state/e2e}"
export WORKBENCH_E2E_ROOT="$WORKBENCH_E2E_BASE/valencia-workbench-e2e-${WORKBENCH_E2E_RUN_ID}"
mkdir -p "$WORKBENCH_E2E_ROOT/tmp"
export TMPDIR="$WORKBENCH_E2E_ROOT/tmp"
export TMP="$TMPDIR"
export TEMP="$TMPDIR"

"$PROJECT_DIR/frontend/node_modules/.bin/playwright" test "$@"
exit_code=$?

if [[ $exit_code -eq 0 && "${WORKBENCH_E2E_KEEP_STATE:-0}" != "1" ]]; then
  e2e_base_abs="${WORKBENCH_E2E_BASE:A}"
  case "${WORKBENCH_E2E_ROOT:A}" in
    "$e2e_base_abs"/valencia-workbench-e2e-*)
      chmod -R u+w -- "$WORKBENCH_E2E_ROOT" 2>/dev/null
      rm -rf -- "$WORKBENCH_E2E_ROOT"
      if [[ -e "$WORKBENCH_E2E_ROOT" ]]; then
        print -u2 "E2E cleanup failed: $WORKBENCH_E2E_ROOT"
        exit 4
      fi
      ;;
    *)
      print -u2 "Refusing to clean unsafe E2E root: $WORKBENCH_E2E_ROOT"
      exit 3
      ;;
  esac
elif [[ $exit_code -ne 0 ]]; then
  print -u2 "E2E evidence retained at $WORKBENCH_E2E_ROOT"
fi

exit $exit_code
