#!/usr/bin/env bash
# T-12 evidence capture: one invocation, one stamped HEAD. Outputs are staged in /tmp and moved
# into verify-evidence/T-12 only after every command ran, so the dirty-entries stamp is real.
# These are plain committed UTF-8 text files. They are deliberately NOT hidden from `git diff`
# (an earlier round added `verify-evidence/T-12/* -diff` to .gitattributes; it was removed in
# round 4 because hiding evidence from the reviewer - and making text logs classify as binary -
# is not this task's call to make).
# Re-run: bash verify-evidence/T-12/capture.sh
set -uo pipefail
cd "$(dirname "$0")/../.."

WT=$(pwd)
MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
HEAD=$(git rev-parse HEAD)
STAMP="# HEAD=$HEAD ($(git rev-parse --short HEAD)) branch=$(git branch --show-current) captured=$(date -Is) tree=$(git status --porcelain | wc -l) dirty-entries"
TMP=$(mktemp -d /tmp/t12-evidence.XXXXXX)
TMPW=/tmp/t12-web-typecheck
trap 'rm -rf "$TMP" "$TMPW"' EXIT

echo "$STAMP"

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test   (whole tests/ tree of THIS worktree)"
  ./scripts/orch_verify.sh test
  echo "SUITE_EXIT=$?"
} >"$TMP/full_suite.txt"

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test tests/integration/test_attempt_cap.py -vv"
  ./scripts/orch_verify.sh test tests/integration/test_attempt_cap.py -vv
  echo "TARGETED_EXIT=$?"
} >"$TMP/attempt_cap_tests.txt"

{
  echo "$STAMP"

  echo "# command: ./scripts/orch_verify.sh lint   (ruff check src tests)"
  ./scripts/orch_verify.sh lint
  echo "LINT_EXIT=$?"
  echo

  echo "# command: cold python, imported from THIS worktree, clean env defaults"
  env -u VIRTUAL_ENV -u ORCHESTRATOR_ENABLE_WORKER -u ORCHESTRATOR_ENABLE_SCHEDULER \
    ORCHESTRATOR_HOME="$TMP/home" PYTHONPATH="$WT/src" \
    "$MAIN/.venv/bin/python" - <<'PY'
from orchestrator.services import settings_service

for key, value in sorted(settings_service.defaults().items()):
    print(f"{key}={value!r}")
PY
  echo "DEFAULTS_EXIT=$?"
  echo

  echo "# GUI half of DoD 2 (Tasks.tsx, TaskDetail.tsx, Settings.tsx, api/types.ts) is proved"
  echo "# separately by capture_gui_typecheck.sh -> gui_typecheck.txt: a real tsc -b --force"
  echo "# over this worktree's web/src, plus --listFiles and a deliberate-error control."
} >"$TMP/checks.txt"

for f in full_suite.txt attempt_cap_tests.txt checks.txt; do
  mv "$TMP/$f" "verify-evidence/T-12/$f"
done

grep -E "^(#|[0-9]+ (passed|failed|skipped)|[0-9]+ error|All checks|SUITE_EXIT|TARGETED_EXIT|LINT_EXIT|TSC_EXIT|DEFAULTS_EXIT)" \
  verify-evidence/T-12/full_suite.txt \
  verify-evidence/T-12/attempt_cap_tests.txt \
  verify-evidence/T-12/checks.txt