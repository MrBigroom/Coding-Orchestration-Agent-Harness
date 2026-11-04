#!/usr/bin/env bash
# T-8: capture the two headline numbers (full suite, lint) in ONE invocation at ONE HEAD,
# stamped, so no committed text can quote a number that came from a different tree.
#
# Round 5's review found exactly that: the handoff claimed "every count comes from ONE run"
# while targeted counts were measured at 21f79c4 and suite/lint at e0904bb. Two runs, two
# HEADs, one claim. This script makes the claim checkable: the HEAD is in the file.
#
# Re-run:  bash verify-evidence/T-8/suite_and_lint.sh
set -uo pipefail
cd "$(dirname "$0")/../.."

STAMP="# HEAD=$(git rev-parse HEAD) ($(git rev-parse --short HEAD)) branch=$(git branch --show-current) captured=$(date -Is) tree=$(git status --porcelain | wc -l) dirty-entries"
echo "$STAMP"

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test   (whole tests/ tree of THIS worktree)"
  ./scripts/orch_verify.sh test
  echo "SUITE_EXIT=$?"
} >verify-evidence/T-8/full_suite.txt

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh lint  (ruff check src tests)"
  ./scripts/orch_verify.sh lint
  echo "LINT_EXIT=$?"
} >verify-evidence/T-8/lint.txt

grep -E "^(#|[0-9]+ (passed|failed)|[0-9]+ error|All checks|SUITE_EXIT|LINT_EXIT)" \
  verify-evidence/T-8/full_suite.txt verify-evidence/T-8/lint.txt
