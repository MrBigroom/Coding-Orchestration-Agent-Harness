#!/usr/bin/env bash
# T-8 DoD 8 / reviewer ruling 1 evidence: authorization is a GRANT, not a mention.
#
# Round 5's review could not see this control at all — the supplied diff was truncated before
# project_service.py — and asked for the three required proofs to be citable per case instead of
# buried in one 90-line test. This file is the citable artifact: it prints the implementation with
# real line numbers, the test names with real line numbers, and a -v run that names each case.
#
# Everything is offline: temp git repos, no network, no external service.
#
# Re-run:  bash verify-evidence/T-8/ruling1_grant_tests.sh > verify-evidence/T-8/ruling1_grant_tests.txt
set -uo pipefail
cd "$(dirname "$0")/../.."

echo "T-8 ruling 1 — contract authorization is a grant, not a mention"
echo "HEAD=$(git rev-parse HEAD) ($(git rev-parse --short HEAD))  branch=$(git branch --show-current)"
date -Is
echo

echo "== implementation (src/orchestrator/services/project_service.py) =="
echo "-- the watched list: l.29-45"
grep -n "" src/orchestrator/services/project_service.py | sed -n '29,45p'
echo "-- grant matcher: l.48-82"
grep -n "" src/orchestrator/services/project_service.py | sed -n '48,82p'
echo "-- clause/mention helpers: l.83-105"
grep -n "" src/orchestrator/services/project_service.py | sed -n '83,105p'
echo "-- authorized_contract_files (reads ONLY the three stored fields): l.106-132"
grep -n "" src/orchestrator/services/project_service.py | sed -n '106,132p'
echo "-- unauthorized_contract_changes + contract_findings: l.133, 143-177"
grep -n "" src/orchestrator/services/project_service.py | sed -n '133,177p'
echo

echo "== gate + reviewer wiring =="
echo "-- supervisor_service.py: the check name and the fail path"
grep -n "CONTRACT_CHECK\|contract_findings\|contract_change_detected\|NEEDS_HUMAN\|contract_change_hold" \
  src/orchestrator/services/supervisor_service.py
echo "-- review_service.py: the harness-computed section in the reviewer payload"
grep -n "contract_findings\|harness_findings_section\|contract files" src/orchestrator/services/review_service.py
echo "-- log_service.py: details are redacted before they are stored"
grep -n "redact" src/orchestrator/services/log_service.py
echo

echo "== per-case tests (tests/unit/test_recovery_markers.py) =="
grep -n "^def test_contract_authorization\|^def test_reviewer_prompt_sections\|^def test_unauthorized_contract_change\|^CONTRACT_CHANGED" \
  tests/unit/test_recovery_markers.py
echo

echo "== targeted -v run (one line per proof) =="
./scripts/orch_verify.sh test tests/unit/test_recovery_markers.py -v \
  -k "contract_authorization or reviewer_prompt_sections or unauthorized_contract_change" 2>&1 |
  grep -E "^(ORCH_VERIFY\||tests/unit/test_recovery_markers\.py::|[0-9]+ (passed|failed|deselected)|FAILED|ERROR)"
echo "TARGETED_EXIT=${PIPESTATUS[0]}"
echo
echo "== integration proof: a recovery commit carrying docs/frozen-contracts.md =="
grep -n "^def test_unauthorized_contract_edit_from_recovery_waits_for_a_human\|CONTRACT_CHECK\|NEEDS_HUMAN\|blocked" \
  tests/integration/test_recovery_after_timeout.py | sed -n '1,24p'
./scripts/orch_verify.sh test tests/integration/test_recovery_after_timeout.py 2>&1 |
  grep -E "^(ORCH_VERIFY\||[0-9]+ (passed|failed|deselected)|FAILED|ERROR)"
echo "INTEGRATION_EXIT=${PIPESTATUS[0]}"
echo
echo "== the same control applied to THIS task (T-8) =="
cat <<'NOTE'
T-8's own stored authorization fields are the DoD text quoted above: DoD 8 names all five
contract files to describe the detector, and DoD 10 plus this task's brief name
 docs/frozen-contracts.md only to forbid editing it. Under the grant rule that authorizes
nothing, so the branch must — and does — leave the file untouched. A mention rule would have
made T-8 self-authorizing, which is the fail-open shape ruling 1 removed.
NOTE
git diff --name-only "$(git merge-base main HEAD)"..HEAD | grep -c "docs/frozen-contracts.md" |
  sed 's/^/frozen-contracts.md commits touched: /'
git status --porcelain -uno | grep -c "docs/frozen-contracts.md" |
  sed 's/^/frozen-contracts.md dirty: /'
exit 0
