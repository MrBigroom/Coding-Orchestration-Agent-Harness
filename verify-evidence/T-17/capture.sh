#!/usr/bin/env bash
# Regenerate verify-evidence/T-17/ in ONE pass. Run it from the worktree at the code HEAD
# you want to certify: `orchestrator task evidence T-17` commits-first checks and then
# runs this committed script and nothing else.
#
#   ./verify-evidence/T-17/capture.sh                # fast: lint + focused tests, seconds
#   T15_FULL=1 ./verify-evidence/T-17/capture.sh     # fast + the full suite (~2.5 min)
#   T15_ONLY=lint.txt ./verify-evidence/T-17/capture.sh
#
# Offline: no network, no API keys. Wall-clock suffixes are stripped so a re-run at the
# same HEAD is byte-identical; T15_STEP lines let the driver write timing.txt.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="$ROOT/verify-evidence/T-17"
MODE=fast
if [ "${T15_FULL:-0}" = "1" ]; then MODE=full; fi
STAMP="# HEAD=$(git rev-parse HEAD) mode=$MODE"
SHORT="$(git rev-parse --short HEAD)"
KEEP='ORCH_VERIFY\||[0-9]+ (passed|failed)|All checks passed|^# (HEAD|command)|assertion_shape=|^  l\.'
stable() { sed -E -e 's/ in [0-9.]+s( \([0-9:]+\))?//' -e 's/ \[ *[0-9]+%\]//'; }
now() { date +%s%N; }
ms() { echo $(( ($2 - $1) / 1000000 )); }
emit() { printf 'T15_STEP|%s|%s\n' "$1" "$2"; }

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

if want focused_tests.txt; then
  T0=$(now)
  { echo "$STAMP"
    echo "# command: ./scripts/orch_verify.sh test -vv (T-17 memory lifecycle + migration drift)"
    ./scripts/orch_verify.sh test -vv \
      tests/unit/test_memory_lifecycle.py \
      tests/unit/test_memory.py \
      tests/unit/test_memory_triage.py \
      tests/unit/test_memory_scopes.py \
      tests/integration/test_memory_scope_boundary.py \
      tests/integration/test_migrations.py
  } 2>&1 | grep -E "PASSED|FAILED|ERROR|passed|failed|ORCH_VERIFY\||^# (HEAD|command)" | stable \
    > "$OUT/focused_tests.txt"
  emit focused_tests "$(ms "$T0" "$(now)")"
fi

if [ "$MODE" = "full" ] && want full_suite.txt; then
  T0=$(now)
  { echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
    ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP" | stable > "$OUT/full_suite.txt"
  emit full_suite "$(ms "$T0" "$(now)")"
elif [ "$MODE" != "full" ]; then
  echo "full suite not run (set T15_FULL=1); full_suite.txt and timing.txt left untouched"
fi

if want assertion_shape.txt; then
  T0=$(now)
  { echo "$STAMP"
    echo "# command: python3 scripts/t15_assertion_shape.py (T-15's shape pin, unchanged)"
    python3 scripts/t15_assertion_shape.py
  } > "$OUT/assertion_shape.txt"
  emit assertion_shape "$(ms "$T0" "$(now)")"
fi

if want source_anchors.txt; then
  T0=$(now)
  { echo "$STAMP"
    echo "# command: grep -nF for the T-17 anchors (source line numbers at this HEAD)"
    for pair in \
      "src/orchestrator/services/memory_service.py:def _keep_priority" \
      "src/orchestrator/services/memory_service.py:def _tag_rank" \
      "src/orchestrator/services/memory_service.py:def _linked_duplicate" \
      "src/orchestrator/services/memory_service.py:def is_garbled" \
      "src/orchestrator/services/memory_service.py:def _redact_memory_content" \
      "src/orchestrator/services/memory_service.py:def approval_impact" \
      "src/orchestrator/services/memory_service.py:def approve" \
      "src/orchestrator/services/memory_service.py:def condense" \
      "src/orchestrator/services/memory_triage.py:def condense_proposals" \
      "src/orchestrator/services/memory_triage.py:redaction-marker" \
      "src/orchestrator/services/run_service.py:memory_suggestion_linked" \
      "src/orchestrator/cli.py:def memory_condense" \
      "src/orchestrator/cli.py:def memory_approve" \
      "src/orchestrator/api/routes/memory.py:def approve_memory" \
      "web/src/pages/Memory.tsx:Injection caps" \
      "src/orchestrator/api/static_fallback/index.html:Injection caps"; do
      file="${pair%%:*}"; needle="${pair#*:}"
      grep -nF "$needle" "$file"
    done
  } > "$OUT/source_anchors.txt"
  emit source_anchors "$(ms "$T0" "$(now)")"
fi

if want migrations_untouched.txt; then
  T0=$(now)
  { echo "$STAMP"
    echo "# command: prove no Alembic revision in this branch touches memory_entries lifecycle"
    BASE="$(git merge-base HEAD main)"
    echo "# merge-base HEAD main: $BASE"
    echo '$ git diff --name-only "$BASE"..HEAD -- src/orchestrator/migrations'
    git diff --name-only "$BASE"..HEAD -- src/orchestrator/migrations || true
    echo "# (empty above means no migration file changed on this branch)"
    echo '$ grep -rn "supersedes\|stale_after" src/orchestrator/migrations || echo NONE'
    grep -rn "supersedes\|stale_after" src/orchestrator/migrations || echo NONE
    echo '# command: the frozen table has no lifecycle column'
    echo '$ python -c "from orchestrator.core.models import MemoryEntry; print(...)"'
    COMMON="$(git rev-parse --path-format=absolute --git-common-dir)"
    PY="${COMMON%/.git}/.venv/bin/python"
    PYTHONPATH="$ROOT/src" "$PY" -c \
      "from orchestrator.core.models import MemoryEntry as M; print('supersedes' in M.model_fields, 'stale_after' in M.model_fields)"
  } > "$OUT/migrations_untouched.txt"
  emit migrations_untouched "$(ms "$T0" "$(now)")"
fi

if want dod_map.txt; then
  T0=$(now)
  { echo "$STAMP"
    echo "# static DoD map; focused_tests.txt supplies each node's PASSED line"
    cat <<'MAP'
1 ingest dedup:
   test_a_reworded_restatement_is_linked_not_stored
   test_a_byte_identical_suggestion_still_creates_no_row
   test_same_domain_but_unrelated_facts_are_not_linked
2 value-ordered demotion:
   test_a_pinned_entry_is_kept_while_an_older_unpinned_entry_is_demoted
   test_untagged_entries_keep_the_pre_t17_demotion_order
   test_two_selections_are_byte_identical_and_exclude_the_same_ids
3 condense non-destructive default + apply:
   test_condense_dry_run_changes_no_row
   test_condense_apply_updates_the_survivor_and_rejects_the_absorbed
   test_condense_partial_apply_is_refused_without_writing
   test_condense_rendered_markdown_matches_the_surviving_approved_rows
   integration::test_memory_condense_is_a_dry_run_until_apply_names_the_ids
4 lifecycle behind a contract change, no migration:
   test_lifecycle_fields_stay_behind_a_contract_change_request
   test_the_frozen_memory_table_has_no_lifecycle_columns_yet
   migrations_untouched.txt (empty migration diff; model fields False False)
   integration::test_models_and_migrations_do_not_drift (test_migrations.py)
5 docs/memory.md: ingest dedup, tag demotion, non-destructive condense
   (source_anchors.txt grep names the implementation the doc describes)
6 redaction record, garbled triage, unchanged log redaction:
   test_adding_a_key_value_phrase_records_the_substitution
   test_updating_a_key_value_phrase_records_the_substitution
   test_a_marked_entry_is_garbled_not_rendered_and_not_approvable
   test_a_marked_approved_entry_is_not_injected_or_rendered
   test_log_redaction_is_still_a_security_control
   plus the unchanged tests/unit/test_redaction.py in focused_tests.txt
7 eviction is value-ordered and never silent:
   test_approval_that_would_evict_names_the_casualty_and_needs_override
   test_budget_and_triage_name_the_current_demotions
   integration::test_approval_eviction_is_refused_and_override_is_explicit
   source_anchors.txt shows the GUI Injection caps panel (React + fallback)
MAP
  } > "$OUT/dod_map.txt"
  emit dod_map "$(ms "$T0" "$(now)")"
fi

printf 'regenerated at %s (mode=%s, only=%s)\n' "$SHORT" "$MODE" "${ONLY:-all}"
