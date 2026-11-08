#!/usr/bin/env bash
# Regenerate verify-evidence/T-15/*.txt. Run from the repo root at the code HEAD you want to
# certify. Outputs are staged in a temp dir so the worktree stays clean while the suite runs;
# the evidence is copied in afterwards and committed separately (evidence-only commit).
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="$ROOT/verify-evidence/T-15"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/t15-evidence.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
SHA="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"
STAMP="# HEAD=$SHA"
KEEP='ORCH_VERIFY\||passed|failed|skipped|PASSED|FAILED|ERROR|All checks|^# (HEAD|command)'

echo "capturing at $SHORT -> $OUT"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
  ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP" > "$STAGE/full_suite.txt"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh lint"
  ./scripts/orch_verify.sh lint; } 2>&1 | grep -E "$KEEP" > "$STAGE/lint.txt"

{ echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test -vv tests/unit/test_evidence_freshness.py" \
       "tests/integration/test_evidence_freshness.py tests/unit/test_memory_scopes.py" \
       "tests/integration/test_memory_scope_boundary.py"
  ./scripts/orch_verify.sh test -vv tests/unit/test_evidence_freshness.py \
    tests/integration/test_evidence_freshness.py tests/unit/test_memory_scopes.py \
    tests/integration/test_memory_scope_boundary.py; } 2>&1 \
  | grep -E "PASSED|FAILED|ERROR|passed|failed|^# (HEAD|command)|ORCH_VERIFY\|" \
  > "$STAGE/freshness_tests.txt"

# Source anchors: the check, the parser, the generator and the two integration points.
{
  echo "$STAMP"
  echo "# command: grep -n for the T-15 implementation anchors (source line numbers)"
  for pair in \
    "src/orchestrator/services/evidence_freshness.py:CHECK_NAME =" \
    "src/orchestrator/services/evidence_freshness.py:def parse_stamps" \
    "src/orchestrator/services/evidence_freshness.py:def check_freshness" \
    "src/orchestrator/services/evidence_freshness.py:def regenerate_bundle" \
    "src/orchestrator/services/supervisor_service.py:evidence_freshness.check_freshness" \
    "src/orchestrator/services/review_service.py:evidence_freshness.check_freshness" \
    "src/orchestrator/cli.py:def task_evidence"; do
    file="${pair%%:*}"; needle="${pair#*:}"
    grep -nF "$needle" "$file"
  done
} > "$STAGE/source_anchors.txt"

# DoD 5 map: each offline regression names its own test node id.
{
  echo "$STAMP"
  echo "# command: static DoD map (the -vv run above supplies the PASSED lines for each node id)"
  cat <<'MAP'
DoD 5(a) older HEAD -> stale check with the right paths/count:
  tests/unit/test_evidence_freshness.py::test_an_older_stamp_is_stale_with_the_paths_and_the_count
  tests/integration/test_evidence_freshness.py::test_stale_evidence_is_reported_as_stale_and_does_not_flip_the_verdict
DoD 5(b) a freshly regenerated bundle -> pass:
  tests/unit/test_evidence_freshness.py::test_a_stamp_that_differs_only_by_an_evidence_only_commit_passes
  tests/unit/test_evidence_freshness.py::test_the_generated_bundle_is_reported_fresh_by_the_check
  tests/integration/test_evidence_freshness.py::test_a_fresh_bundle_is_reported_as_pass
DoD 5(c) unstamped or absent evidence -> not_applicable:
  tests/unit/test_evidence_freshness.py::test_absent_evidence_is_not_applicable
  tests/unit/test_evidence_freshness.py::test_unstamped_evidence_is_not_applicable
  tests/integration/test_evidence_freshness.py::test_an_absent_bundle_is_not_applicable
DoD 5(d) regeneration is idempotent; second run rewrites nothing:
  tests/unit/test_evidence_freshness.py::test_regeneration_is_one_command_and_second_run_is_a_no_op
DoD 5(e) existing verification checks + frozen artifact names unchanged:
  tests/integration/test_evidence_freshness.py::test_stale_evidence_is_reported_as_stale_and_does_not_flip_the_verdict
DoD 5(f) split assertions pass; the reason-shaped one fails when the reason changes:
  tests/unit/test_memory_scopes.py::test_fix_scopes_leaves_rejected_rows_alone
  tests/unit/test_memory_scopes.py::test_the_rejected_reason_assertion_fails_when_the_reason_changes
  tests/integration/test_memory_scope_boundary.py (all split assertions)
MAP
} > "$STAGE/dod_map.txt"

cp "$STAGE"/*.txt "$OUT/"
printf 'wrote: %s\n' "$(ls "$OUT"/*.txt | xargs -n1 basename | tr '\n' ' ')"
printf 'regenerated at %s\n' "$SHORT"
