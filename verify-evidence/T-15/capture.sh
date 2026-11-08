#!/usr/bin/env bash
# Regenerate verify-evidence/T-15/ in ONE pass. Run it from the worktree at the code HEAD you want
# to certify: `orchestrator task evidence T-15` does exactly that (it refuses a dirty worktree, so
# the HEAD the stamps name is the tree that was measured).
#
#   ./verify-evidence/T-15/capture.sh                 # fast pass: lint + focused tests, a few seconds
#   T15_FULL=1 ./verify-evidence/T-15/capture.sh      # fast pass plus the full suite (~2 minutes)
#
# Wall-clock suffixes are stripped so a re-run at the same HEAD is byte-identical, which is what
# makes the second regeneration a no-op (DoD 3/5d). Offline: no network, no API keys.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="$ROOT/verify-evidence/T-15"
STAMP="# HEAD=$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"
KEEP='ORCH_VERIFY\||[0-9]+ (passed|failed)|All checks passed|^# (HEAD|command)'
# Drop " in 12.34s (0:00:12)" and pytest's "[ 42%]" so evidence bytes depend only on the code.
stable() { sed -E -e 's/ in [0-9.]+s( \([0-9:]+\))?//' -e 's/ \[ *[0-9]+%\]//'; }
now() { date +%s%N; }
ms() { echo $(( ($2 - $1) / 1000000 )); }
mkdir -p "$OUT"
START=$(now)
echo "capturing at $SHORT -> $OUT"

LINT_START=$(now)
{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh lint"
  ./scripts/orch_verify.sh lint; } 2>&1 | grep -E "$KEEP" | stable > "$OUT/lint.txt"
LINT_MS=$(ms "$LINT_START" "$(now)")

TESTS_START=$(now)
{ echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test -vv tests/unit/test_evidence_freshness.py" \
       "tests/integration/test_evidence_freshness.py plus the two DoD-4 memory nodes"
  ./scripts/orch_verify.sh test -vv tests/unit/test_evidence_freshness.py \
    tests/integration/test_evidence_freshness.py \
    tests/unit/test_memory_scopes.py::test_fix_scopes_leaves_rejected_rows_alone \
    tests/unit/test_memory_scopes.py::test_the_rejected_reason_assertion_fails_when_the_reason_changes
} 2>&1 | grep -E "PASSED|FAILED|ERROR|passed|failed|ORCH_VERIFY\||^# (HEAD|command)" | stable \
  > "$OUT/freshness_tests.txt"
TESTS_MS=$(ms "$TESTS_START" "$(now)")

if [ "${T15_FULL:-0}" = "1" ]; then
  FULL_START=$(now)
  { echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
    ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP" | stable > "$OUT/full_suite.txt"
  # Written only by the full pass, because a timing changes every run and a file that changes
  # every run would make the second regeneration dirty the bundle it just certified.
  { echo "$STAMP"
    echo "# command: date +%s%N around each step inside capture.sh (milliseconds)"
    echo "lint_ms=$LINT_MS"
    echo "focused_tests_ms=$TESTS_MS"
    echo "full_suite_ms=$(ms "$FULL_START" "$(now)")"
    echo "pass_total_ms=$(ms "$START" "$(now)")"
    echo "# 'orchestrator task evidence T-15' reports its own wall time and rewrite count."
  } > "$OUT/timing.txt"
else
  # The slow half was not re-measured in this pass: leave full_suite.txt and timing.txt as they
  # are on disk. Their own stamps then decide whether they are fresh; re-stamping old output is
  # exactly what this task exists to stop.
  echo "full suite not run (set T15_FULL=1); full_suite.txt and timing.txt left untouched"
fi

{ echo "$STAMP"
  echo "# command: grep -nF for the T-15 anchors (source line numbers at this HEAD)"
  for pair in \
    "src/orchestrator/services/evidence_freshness.py:CHECK_NAME =" \
    "src/orchestrator/services/evidence_freshness.py:def parse_stamps" \
    "src/orchestrator/services/evidence_freshness.py:def check_freshness" \
    "src/orchestrator/services/evidence_freshness.py:def check_status" \
    "src/orchestrator/services/evidence_freshness.py:def regenerate_bundle" \
    "src/orchestrator/services/supervisor_service.py:evidence_freshness.check_freshness" \
    "src/orchestrator/services/review_service.py:evidence_freshness.check_freshness" \
    "src/orchestrator/cli.py:def task_evidence"; do
    file="${pair%%:*}"; needle="${pair#*:}"
    grep -nF "$needle" "$file"
  done
} > "$OUT/source_anchors.txt"

{ echo "$STAMP"
  echo "# command: static DoD map; the -v run above supplies each node's PASSED line"
  cat <<'MAP'
5a older HEAD -> stale with the right paths/count:
   unit::test_an_older_stamp_is_stale_with_the_paths_and_the_count
   integration::test_stale_evidence_is_reported_without_flipping_the_verdict
5b a freshly regenerated bundle -> pass:
   unit::test_the_generated_bundle_is_fresh_once_committed
   integration::test_the_existing_checks_and_frozen_artifact_names_are_unchanged
5c unstamped or absent evidence -> not_applicable (never a false failure):
   unit::test_absent_evidence_is_not_applicable, unit::test_unstamped_evidence_is_not_applicable
   integration::test_an_absent_bundle_is_not_applicable
5d regeneration is idempotent; the second run rewrites nothing:
   unit::test_regeneration_is_one_command_and_a_second_run_rewrites_nothing
5e existing verification checks and frozen artifact names unchanged:
   integration::test_the_existing_checks_and_frozen_artifact_names_are_unchanged
5f split assertions pass; the reason-shaped one fails when the reason changes:
   unit_memory::test_fix_scopes_leaves_rejected_rows_alone
   unit_memory::test_the_rejected_reason_assertion_fails_when_the_reason_changes
   unit::test_the_memory_scope_tests_keep_their_assertion_shape (AST inventory)
also: integration::test_strict_mode_is_the_only_way_staleness_fails_the_gate
MAP
} > "$OUT/dod_map.txt"

printf 'wrote: %s\n' "$(cd "$OUT" && ls *.txt | tr '\n' ' ')"
printf 'regenerated at %s\n' "$SHORT"