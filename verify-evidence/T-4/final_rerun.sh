#!/usr/bin/env bash
# T-4: re-run the project's own test and lint commands at the CURRENT tip and record the
# output, with the HEAD stamp, in final_rerun.txt beside this script.
#
# Why this exists: `capture.sh` stamps the tree it ran at, and the evidence it writes is
# committed AFTER that run, so the branch tip is never the tree the counts were taken on.
# Re-running here closes the gap: the stamp in the file names the tip this run saw, and the
# only later commits add this file and the handoff copies, all under verify-evidence/T-4/,
# which `ruff check src tests` and the pytest collection never read.
#
# Re-run: bash verify-evidence/T-4/final_rerun.sh   (writes only its own output file)
set -uo pipefail
cd "$(dirname "$0")/../.."

HEAD_SHA=$(git rev-parse HEAD)
DIRTY=$(git status --porcelain | grep -cv '^.. verify-evidence/T-4/' || true)
STAMP="# HEAD=$HEAD_SHA ($(git rev-parse --short HEAD)) branch=$(git branch --show-current) captured=$(date -Is) tree=$DIRTY dirty-entries (excluding verify-evidence/T-4)"
TMP=$(mktemp -d /tmp/t4-final.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

{
  echo "$STAMP"
  echo "# RE-RUN of both project commands at the branch tip, after the evidence commits."
  echo "# Nothing under verify-evidence/T-4/ is read by either command:"
  echo "#   ruff target  = 'ruff check src tests'  (see the orch-verify: lint target line)"
  echo "#   pytest target= the tests/ tree         (see the orch-verify: pytest target line)"
  echo "# command: ./scripts/orch_verify.sh test"
  ./scripts/orch_verify.sh test
  echo "FINAL_SUITE_EXIT=$?"
  echo "# command: ./scripts/orch_verify.sh lint"
  ./scripts/orch_verify.sh lint
  echo "FINAL_LINT_EXIT=$?"
  echo "$STAMP"
} >"$TMP/final_rerun.txt"

mv "$TMP/final_rerun.txt" verify-evidence/T-4/final_rerun.txt
tail -n 3 verify-evidence/T-4/final_rerun.txt | head -n 1
grep -E "^[0-9]+ (passed|failed)|All checks passed|FINAL_(SUITE|LINT)_EXIT|^# HEAD=" \
  verify-evidence/T-4/final_rerun.txt
