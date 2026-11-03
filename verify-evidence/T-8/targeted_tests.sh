#!/usr/bin/env bash
# T-8 DoD evidence: the recovery/redaction test files run on their own, LABELED per command.
#
# Round 3 added this script because the committed targeted_tests.txt carried three unlabeled
# counts (16 / 14 / 22); 16 and 14 reconcile against single-file runs but 22 does not reconcile
# against any file in this set, so the file is regenerated from explicit commands instead.
# Everything here is offline: temp git repos, mock harness, mock provider.
#
# Re-run:  bash verify-evidence/T-8/targeted_tests.sh > verify-evidence/T-8/targeted_tests.txt
set -uo pipefail
cd "$(dirname "$0")/../.."

echo "# HEAD=$(git rev-parse HEAD) ($(git rev-parse --short HEAD)) captured=$(date -Is)"
echo "# each block below is one labeled command run at that HEAD; counts are not merged across runs"
echo

run() {
  echo "### \$ ./scripts/orch_verify.sh test $*"
  ./scripts/orch_verify.sh test "$@" 2>&1 |
    grep -E "^(ORCH_VERIFY\||tests/|[0-9]+ (passed|failed|deselected)|FAILED|ERROR)"
  echo
}

run tests/unit/test_recovery_markers.py
run tests/integration/test_recovery_after_timeout.py
run tests/unit/test_recovery_markers.py tests/integration/test_recovery_after_timeout.py
run tests/unit/test_redaction.py
run tests/integration/test_git_worktree.py
run tests/unit/test_recovery_markers.py tests/integration/test_recovery_after_timeout.py \
    tests/unit/test_redaction.py tests/integration/test_git_worktree.py
run tests/unit/test_recovery_markers.py -vv \
    -k "contract_authorization or reviewer_prompt_sections or unauthorized_contract_change"
