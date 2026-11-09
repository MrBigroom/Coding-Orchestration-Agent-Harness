#!/usr/bin/env bash
# Regenerate verify-evidence/T-15/ in ONE pass. Run it from the worktree at the code HEAD you want
# to certify: `orchestrator task evidence T-15` does exactly that (it refuses a worktree with any
# uncommitted input — tracked or untracked — so the HEAD the stamps name is the tree measured).
#
#   ./verify-evidence/T-15/capture.sh                 # fast pass: lint + focused tests, a few seconds
#   T15_FULL=1 ./verify-evidence/T-15/capture.sh      # fast pass plus the full suite (~2 minutes)
#   T15_ONLY=lint.txt ./verify-evidence/T-15/capture.sh   # only the step that produces lint.txt
#
# `orchestrator task evidence T-15 --full` sets T15_FULL=1 for you; `--only <name>` sets T15_ONLY to
# a comma-separated list of the files to produce. The mode is written into every stamp and into
# manifest.json, so a reader can tell a cheap refresh from a complete one.
#
# Wall-clock suffixes are stripped from the captured output so a re-run at the same HEAD is
# byte-identical, which is what makes the second regeneration a no-op (DoD 3/5d). Per-step timings
# are emitted as `T15_STEP|name|ms` lines on stdout: the driver writes timing.txt from them, because
# a file that changes on every run would dirty the bundle it just certified. Offline: no network,
# no API keys.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="$ROOT/verify-evidence/T-15"
MODE=fast
if [ "${T15_FULL:-0}" = "1" ]; then MODE=full; fi
STAMP="# HEAD=$(git rev-parse HEAD) mode=$MODE"
SHORT="$(git rev-parse --short HEAD)"
KEEP='ORCH_VERIFY\||[0-9]+ (passed|failed)|All checks passed|^# (HEAD|command)'
# Drop " in 12.34s (0:00:12)" and pytest's "[ 42%]" so evidence bytes depend only on the code.
stable() { sed -E -e 's/ in [0-9.]+s( \([0-9:]+\))?//' -e 's/ \[ *[0-9]+%\]//'; }
now() { date +%s%N; }
ms() { echo $(( ($2 - $1) / 1000000 )); }
emit() { printf 'T15_STEP|%s|%s\n' "$1" "$2"; }

# Which outputs this invocation must produce. No T15_ONLY means every output of this mode.
ONLY="${T15_ONLY:-}"
want() {
  [ -z "$ONLY" ] && return 0
  case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

mkdir -p "$OUT"
echo "capturing at $SHORT -> $OUT (mode=$MODE, only=${ONLY:-all})"

if want lint.txt; then
  T0=$(now)
  { echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh lint"
    ./scripts/orch_verify.sh lint; } 2>&1 | grep -E "$KEEP" | stable > "$OUT/lint.txt"
  emit lint "$(ms "$T0" "$(now)")"
fi

if want freshness_tests.txt; then
  T0=$(now)
  { echo "$STAMP"
    echo "# command: ./scripts/orch_verify.sh test -vv tests/unit/test_evidence_freshness.py" \
         "tests/integration/test_evidence_freshness.py plus the two DoD-4 memory nodes"
    ./scripts/orch_verify.sh test -vv tests/unit/test_evidence_freshness.py \
      tests/integration/test_evidence_freshness.py \
      tests/unit/test_memory_scopes.py::test_fix_scopes_leaves_rejected_rows_alone \
      tests/unit/test_memory_scopes.py::test_the_rejected_reason_assertion_fails_when_the_reason_changes
  } 2>&1 | grep -E "PASSED|FAILED|ERROR|passed|failed|ORCH_VERIFY\||^# (HEAD|command)" | stable \
    > "$OUT/freshness_tests.txt"
  emit focused_tests "$(ms "$T0" "$(now)")"
fi

if [ "$MODE" = "full" ] && want full_suite.txt; then
  T0=$(now)
  { echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
    ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP" | stable > "$OUT/full_suite.txt"
  emit full_suite "$(ms "$T0" "$(now)")"
elif [ "$MODE" != "full" ]; then
  # The slow half was not re-measured in this pass: leave full_suite.txt and timing.txt as they are
  # on disk. Their own stamps decide whether they are fresh; re-stamping old output is exactly what
  # this task exists to stop.
  echo "full suite not run (set T15_FULL=1); full_suite.txt and timing.txt left untouched"
fi

if want assertion_shape.txt; then
  T0=$(now)
  { echo "$STAMP"
    echo "# command: python3 scripts/t15_assertion_shape.py (AST, no pinned counts)"
    python3 scripts/t15_assertion_shape.py
  } > "$OUT/assertion_shape.txt"
  emit assertion_shape "$(ms "$T0" "$(now)")"
fi

if want source_anchors.txt; then
  T0=$(now)
  { echo "$STAMP"
    echo "# command: grep -nF for the T-15 anchors (source line numbers at this HEAD)"
    for pair in \
      "src/orchestrator/services/evidence_freshness.py:CHECK_NAME =" \
      "src/orchestrator/services/evidence_freshness.py:def parse_stamps" \
      "src/orchestrator/services/evidence_freshness.py:def _commit_class" \
      "src/orchestrator/services/evidence_freshness.py:def _comparison_target" \
      "src/orchestrator/services/evidence_freshness.py:def _manifest_declared_inputs" \
      "src/orchestrator/services/evidence_freshness.py:def check_freshness" \
      "src/orchestrator/services/evidence_freshness.py:def check_status" \
      "src/orchestrator/services/evidence_freshness.py:def applies_to_kind" \
      "src/orchestrator/services/evidence_freshness.py:def _uncommitted_inputs" \
      "src/orchestrator/services/evidence_freshness.py:def regenerate_bundle" \
      "src/orchestrator/services/evidence_freshness.py:def write_at_tip_report" \
      "src/orchestrator/services/supervisor_service.py:evidence_freshness.check_freshness" \
      "src/orchestrator/services/review_service.py:evidence_freshness.check_freshness" \
      "src/orchestrator/cli.py:def task_evidence" \
      "src/orchestrator/core/schemas.py:status: Literal" \
      "scripts/t15_assertion_shape.py:def main"; do
      file="${pair%%:*}"; needle="${pair#*:}"
      grep -nF "$needle" "$file"
    done
  } > "$OUT/source_anchors.txt"
  emit source_anchors "$(ms "$T0" "$(now)")"
fi

if want status_consumers.txt; then
  T0=$(now)
  { echo "$STAMP"
    echo "# Commands actually run, with their UNEDITED output FIRST, then per-area excerpts"
    echo "# (ruling 2026-11-10 (e) items 1-2). The repo-wide greps cover all of src/ and web/src,"
    echo "# so completeness is shown rather than asserted; the two negative results the operator"
    echo "# named are visible in that raw output: web/src has no union of the four check values,"
    echo "# and web/src/api/types.ts types verification_status as string | null."
    echo
    echo "## COMMAND 1 -- every quoted check-status value in src/ and web/src"
    echo '$ grep -rn --exclude-dir=__pycache__ --exclude-dir=node_modules -E '\''"(pass|fail|skipped|not_applicable)"'\'' src web/src'
    grep -rn --exclude-dir=__pycache__ --exclude-dir=node_modules -E '"(pass|fail|skipped|not_applicable)"' src web/src
    echo
    echo "## COMMAND 2 -- every verification_status reference in src/ and web/src"
    echo '$ grep -rn --exclude-dir=__pycache__ --exclude-dir=node_modules '\''verification_status'\'' src web/src'
    grep -rn --exclude-dir=__pycache__ --exclude-dir=node_modules 'verification_status' src web/src
    echo
    echo "## COMMAND 3 -- the frozen Literal(s)"
    echo '$ grep -rn --exclude-dir=__pycache__ '\''status: Literal'\'' src'
    grep -rn --exclude-dir=__pycache__ 'status: Literal' src
    echo
    echo "## COMMAND 4 -- the GUI status-to-tone map (including the additive stale value)"
    echo '$ grep -n -E '\''(^|[ {])(pass|fail|not_applicable|skipped|stale):'\'' web/src/components/ui.tsx'
    grep -n -E '(^|[ {])(pass|fail|not_applicable|skipped|stale):' web/src/components/ui.tsx
    echo
    echo "## COMMAND 5 -- the verification.md renderer emits c.status unchanged"
    echo '$ grep -n '\''c.status'\'' src/orchestrator/services/artifact_service.py'
    grep -n 'c.status' src/orchestrator/services/artifact_service.py
    echo
    echo "## COMMAND 6 -- the Supervisor verification row (the line a reviewer found missing)"
    echo '$ grep -n -E '\''evidence_freshness\.(CHECK_NAME|check_status|check_freshness)|status=status'\'' src/orchestrator/services/supervisor_service.py'
    grep -n -E 'evidence_freshness\.(CHECK_NAME|check_status|check_freshness)|status=status' \
      src/orchestrator/services/supervisor_service.py
    echo
    echo "## COMMAND 7 -- the Architect review call site"
    echo '$ grep -n -E '\''evidence_freshness\.(CHECK_NAME|check_status|check_freshness|STATE_STALE)'\'' src/orchestrator/services/review_service.py'
    grep -n -E 'evidence_freshness\.(CHECK_NAME|check_status|check_freshness|STATE_STALE)' \
      src/orchestrator/services/review_service.py
    echo
    echo "## RAW OUTPUT ENDS HERE; per-line excerpts with file prefixes follow"
    echo
    echo "## (1) the frozen five-value Literal"
    grep -n 'status: Literal' src/orchestrator/core/schemas.py
    echo
    echo "## (2) Supervisor verification builds the row (supervisor_service.py)"
    grep -n -E 'evidence_freshness\.(CHECK_NAME|check_status|check_freshness)|status=status' \
      src/orchestrator/services/supervisor_service.py
    echo
    echo "## (3) Architect review calls the same comparison (review_service.py)"
    grep -n -E 'evidence_freshness\.(CHECK_NAME|check_status|check_freshness|STATE_STALE)' \
      src/orchestrator/services/review_service.py
    echo
    echo "## (4) verification.md renderer emits c.status unchanged (artifact_service.py)"
    grep -n 'c.status' src/orchestrator/services/artifact_service.py
    echo
    echo "## (5) GUI status-to-tone map (web/src/components/ui.tsx)"
    grep -n -E '(^|[ {])(pass|fail|not_applicable|skipped|stale):' web/src/components/ui.tsx
  } > "$OUT/status_consumers.txt"
  emit status_consumers "$(ms "$T0" "$(now)")"
fi

if want dod_map.txt; then
  T0=$(now)
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
   integration::test_never_committed_evidence_is_not_applicable_not_a_deletion
   deleted evidence -> stale naming the deleting commit, never not_applicable:
   unit::test_deleted_evidence_names_the_paths_and_the_deleting_commit
   integration::test_deleted_evidence_is_reported_with_the_deleting_commit
5d regeneration is idempotent; the second run rewrites nothing:
   unit::test_regeneration_is_one_command_and_a_second_run_rewrites_nothing
5e existing verification checks and frozen artifact names unchanged:
   integration::test_the_existing_checks_and_frozen_artifact_names_are_unchanged
5f split assertions pass; the reason-shaped one fails when the reason changes:
   unit_memory::test_fix_scopes_leaves_rejected_rows_alone
   unit_memory::test_the_rejected_reason_assertion_fails_when_the_reason_changes
   scripts/t15_assertion_shape.py output in assertion_shape.txt (AST, comments at every site)
generator integrity and mode:
   unit::test_an_edited_generator_is_refused_even_though_it_lives_in_the_evidence_dir
   unit::test_a_generator_that_is_not_committed_is_refused
   unit::test_no_arbitrary_command_is_accepted_because_only_the_committed_generator_runs
   unit::test_an_untracked_source_file_is_refused_because_the_stamp_would_certify_it
   unit::test_only_refreshes_the_named_output_and_refuses_an_unknown_name
   unit::test_the_full_pass_is_opt_in_and_recorded_in_the_manifest
   unit::test_timing_txt_holds_both_pass_modes_with_wall_time_and_rewrites
   unit::test_the_at_tip_report_names_the_commit_it_compared
status vocabulary (CCR-T15-01) and scope:
   unit::test_the_check_status_uses_the_approved_stale_value
   unit::test_the_stale_row_renders_into_verification_md
   integration::test_an_agent_kind_task_gets_neither_the_row_nor_the_finding
assertion shape (DoD 4):
   unit::test_the_memory_scope_tests_keep_their_assertion_shape
trailer honesty / security:
   unit::test_a_recovery_trailer_is_explained_not_certified
   unit::test_an_operator_hand_fix_trailer_is_explained_not_certified
   unit::test_a_worker_can_fake_the_hand_fix_trailer_and_still_reads_stale
also: integration::test_strict_mode_is_the_only_way_staleness_fails_the_gate
MAP
  } > "$OUT/dod_map.txt"
  emit dod_map "$(ms "$T0" "$(now)")"
fi

printf 'regenerated at %s (mode=%s, only=%s)\n' "$SHORT" "$MODE" "${ONLY:-all}"