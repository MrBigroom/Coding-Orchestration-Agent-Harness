#!/usr/bin/env bash
# T-11: capture every number quoted in the handoff in ONE invocation at ONE HEAD, stamped.
#
# Round 1's handoff quoted 545 passed / 6 skipped and 11 targeted passes from a tree whose HEAD
# was committed two commits earlier than the tip that was handed off. Round 2's reviewer then
# could not tell which tree any number came from. This script puts the HEAD inside each file, so
# the claim is checkable after the fact.
#
# Re-run:  bash verify-evidence/T-11/capture.sh
set -uo pipefail
cd "$(dirname "$0")/../.."

STAMP="# HEAD=$(git rev-parse HEAD) ($(git rev-parse --short HEAD)) branch=$(git branch --show-current) captured=$(date -Is) tree=$(git status --porcelain -uall | wc -l) dirty-entries"

{
  echo "$STAMP"
  echo "# command: git diff --name-only (merge-base main..HEAD) and git diff --stat"
  git diff --name-only "$(git merge-base main HEAD)..HEAD"
  echo "--- stat ---"
  git diff --stat "$(git merge-base main HEAD)..HEAD"
} >verify-evidence/T-11/changed_files.txt

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh all   (whole tests/ tree of THIS worktree, then lint)"
  ./scripts/orch_verify.sh all
  echo "ALL_EXIT=$?"
} >verify-evidence/T-11/suite_and_lint.txt

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test -vv -k 'review_audit or review_payload or diff_budget or visibility or sensitive_files_withheld or partly'"
  ./scripts/orch_verify.sh test -vv -k "review_audit or review_payload or diff_budget or visibility or sensitive_files_withheld or partly"
  echo "TARGETED_EXIT=$?"
} >verify-evidence/T-11/t11_tests.txt

grep -E "^(#|[0-9]+ (passed|failed)|All checks|ALL_EXIT|TARGETED_EXIT)" \
  verify-evidence/T-11/suite_and_lint.txt verify-evidence/T-11/t11_tests.txt