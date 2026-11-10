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
COMMON="$(git rev-parse --path-format=absolute --git-common-dir)"
PY="${COMMON%/.git}/.venv/bin/python"
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
    echo "# command: ./scripts/orch_verify.sh test -vv (T-17 memory lifecycle + API/CLI + migration drift)"
    ./scripts/orch_verify.sh test -vv \
      tests/unit/test_memory_lifecycle.py \
      tests/unit/test_memory.py \
      tests/unit/test_memory_triage.py \
      tests/unit/test_memory_scopes.py \
      tests/unit/test_redaction.py \
      tests/integration/test_memory_scope_boundary.py \
      tests/integration/test_migrations.py \
      tests/integration/test_cli.py \
      tests/integration/test_api.py
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
      "src/orchestrator/services/memory_service.py:def duplicate_of" \
      "src/orchestrator/services/memory_service.py:def duplicate_record" \
      "src/orchestrator/services/memory_service.py:def _duplicate_tags" \
      "src/orchestrator/services/memory_service.py:def is_garbled" \
      "src/orchestrator/services/memory_service.py:def _redact_memory_content" \
      "src/orchestrator/services/memory_service.py:class EvictionRefused" \
      "src/orchestrator/services/memory_service.py:def _eviction_impact" \
      "src/orchestrator/services/memory_service.py:def _refuse_eviction" \
      "src/orchestrator/services/memory_service.py:def _refuse_garbled" \
      "src/orchestrator/services/memory_service.py:def _log_eviction_override" \
      "src/orchestrator/services/memory_service.py:def approval_impact" \
      "src/orchestrator/services/memory_service.py:def create" \
      "src/orchestrator/services/memory_service.py:def update" \
      "src/orchestrator/services/memory_service.py:replace_approved" \
      "src/orchestrator/services/memory_service.py:def approve" \
      "src/orchestrator/services/memory_service.py:def condense" \
      "src/orchestrator/services/memory_service.py:def import_all" \
      "src/orchestrator/services/memory_triage.py:def duplicate_groups" \
      "src/orchestrator/services/memory_triage.py:def condense_proposals" \
      "src/orchestrator/services/memory_triage.py:redaction-marker" \
      "src/orchestrator/services/memory_triage.py:linked-duplicate" \
      "src/orchestrator/utils/memory_text.py:SUBJECT_MAX_DF" \
      "src/orchestrator/utils/memory_text.py:def is_subject_pair" \
      "src/orchestrator/utils/memory_text.py:def subject_clusters" \
      "src/orchestrator/services/run_service.py:_SUGGESTION_EVENT_BY_KIND" \
      "src/orchestrator/services/memory_service.py:memory_eviction_override" \
      "src/orchestrator/cli.py:def memory_condense" \
      "src/orchestrator/cli.py:def memory_approve" \
      "src/orchestrator/api/routes/memory.py:def approve_memory" \
      "src/orchestrator/api/routes/memory.py:def update_memory" \
      "web/src/pages/Memory.tsx:Injection caps" \
      "web/src/pages/Memory.tsx:detail.evicted" \
      "web/src/pages/Memory.tsx:casualtyLines" \
      "web/src/api/client.ts:this.detail = detail;" \
      "web/src/pages/Memory.tsx:e.detail" \
      "src/orchestrator/api/static_fallback/index.html:Injection caps" \
      "src/orchestrator/api/static_fallback/index.html:detail.evicted"; do
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

if want live_condense.txt; then
  LIVE_DB="${T17_LIVE_DB:-$HOME/.local/share/coding-orchestrator/orchestrator.db}"
  if [ -f "$LIVE_DB" ]; then
    T0=$(now)
    TMPHOME="$(mktemp -d)"
    mkdir -p "$TMPHOME/home"
    sqlite3 "$LIVE_DB" ".backup '$TMPHOME/home/orchestrator.db'"
    if [ -d "$(dirname "$LIVE_DB")/memory" ]; then
      cp -r "$(dirname "$LIVE_DB")/memory" "$TMPHOME/home/memory"
    fi
    {
      echo "$STAMP"
      echo "# command: sqlite3 .backup of the live memory DB -> a throwaway ORCHESTRATOR_HOME;"
      echo "#          condense/budget/triage then run on the COPY at the operator's own caps."
      echo "#          The live home is opened read-only and never written. Copy is removed after."
      echo "# source_db=$LIVE_DB"
      echo "# source_sha256=$(sha256sum "$LIVE_DB" | cut -d' ' -f1)"
      echo "# source_bytes=$(stat -c%s "$LIVE_DB")"
      ORCHESTRATOR_HOME="$TMPHOME/home" PYTHONPATH="$ROOT/src" "$PY" - <<'PYCODE'
import hashlib
import json
from orchestrator.core.db import session_scope
from orchestrator.services import memory_service as m
from orchestrator.services import memory_triage as mt
from orchestrator.utils.memory_text import build_df, is_duplicate_pair

def digest(session):
    rows = [
        (e.id, e.category, e.scope, e.project_slug, e.role, e.approved, e.rejected,
         e.content, tuple(e.tags_json or []))
        for e in m.list_entries(session)
    ]
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda r: r[0] or 0):
        h.update(json.dumps(row, default=str, sort_keys=True).encode())
    return h.hexdigest()[:16]

status = "pass"
with session_scope() as s:
    allent = [e for e in m.list_entries(s) if e.approved or not e.rejected]
    by = {e.id: e for e in allent}
    ids = [e.id for e in allent]
    df = build_df([e.content for e in allent])
    rare_pairs = [
        (i, j) for i in ids for j in ids
        if j > i and is_duplicate_pair(by[i].content, by[j].content, df)[0]
    ]
    groups = mt.duplicate_groups(allent)
    print(f"corpus_rows={len(allent)} rare_token_pairs={len(rare_pairs)} "
          f"subject_plus_rare_groups={len(groups)}")
    cited = {
        "evidence_freshness 152/156/164": [152, 156, 164],
        "diff_floor 157/159/161/163": [157, 159, 161, 163],
        "bundle_ordering 154/165": [154, 165],
        "notify_send 135/140/143": [135, 140, 143],
    }
    rare_pair_set = {tuple(sorted(pair)) for pair in rare_pairs}
    for label, members in cited.items():
        hits = [g for g in groups if set(members) & set(g)]
        # Answer the operator's question explicitly: did the RARE-TOKEN matcher alone pair
        # any two members of this topical cluster, independently of the subject pass?
        inside = [list(pair) for pair in rare_pair_set if set(pair) <= set(members)]
        print(f"cited|{label}|members={members}|rare_token_pairs={inside}|groups={hits}")
    caps = m.memory_budgets(s)
    print(f"caps={caps} enabled={m.memory_budgets_enabled(s)}")
    for role in ("architect", "worker", "supervisor"):
        sel = m.select_for_prompt(s, role, "coding-orchestrator")
        print(f"budget_before|{role}|used={sel.used}|excluded={[x['id'] for x in sel.excluded]}")

    before = digest(s)
    proposals = mt.condense_proposals(s, "coding-orchestrator")
    print(f"condense_project=coding-orchestrator proposals={len(proposals)}")
    for proposal in proposals:
        print(f"  proposal|ids={proposal['ids']}|survivor={proposal['survivor']}|"
              f"distinct_facts={proposal['distinct_facts']}|"
              f"tokens={proposal['tokens_before']}->{proposal['tokens_after']}|"
              f"delta={proposal['token_delta']}")
    dry = m.condense(s, "coding-orchestrator")
    same = digest(s) == before
    print(f"dry_run={dry['dry_run']} rows_unchanged={same}")
    status = "pass" if same and dry["dry_run"] else "fail"

    if proposals:
        # (1) The rare-token branch keeps only the survivor's wording, so absorbing a reworded
        #     row would lose that fact: --apply without merged --content must REFUSE. Show the
        #     refusal on the live corpus and prove no row moved.
        rare = [p for p in proposals if not p["distinct_facts"]]
        if rare:
            target = max(rare, key=lambda p: (p["tokens_saved"], -len(p["ids"]), p["ids"]))
            try:
                m.condense(s, "coding-orchestrator", apply_ids=target["ids"])
                print(f"apply_refused|ids={target['ids']}|refused=False")
                status = "fail"
            except m.MemoryError_ as exc:
                reason = " ".join(str(exc).split())
                print(f"apply_refused|ids={target['ids']}|refused=True|reason={reason}")
            print(f"refusal_rows_unchanged={digest(s) == before}")
            if digest(s) != before:
                status = "fail"
        # (2) Apply a UNION proposal, whose surviving text literally contains every member's
        #     content -- the safe branch. It may grow the survivor, so any eviction is refused.
        safe = [p for p in proposals if p["distinct_facts"]]
        chosen = max(safe, key=lambda p: (p["tokens_saved"], -len(p["ids"]), p["ids"])) if safe else None
        if chosen is not None:
            try:
                result = m.condense(s, "coding-orchestrator", apply_ids=chosen["ids"])
            except m.EvictionRefused as exc:
                print(f"apply_refused_growth|ids={chosen['ids']}|reason={' '.join(str(exc).split())}")
                result = m.condense(
                    s, "coding-orchestrator", apply_ids=chosen["ids"], override=True
                )
            survivor_row = m._get(s, chosen["survivor"])
            contained = all(
                m._get(s, i).content.strip() in survivor_row.content
                for i in chosen["absorbed"]
            )
            print(f"applied|ids={chosen['ids']}|survivor={chosen['survivor']}|"
                  f"rejected={chosen['absorbed']}|distinct_facts={chosen['distinct_facts']}|"
                  f"absorbed_text_contained={contained}|"
                  f"tokens_saved={result['applied'][0]['tokens_saved']}")
            if not contained:
                status = "fail"
        triage = mt.triage(s, status="approved", project_slug="coding-orchestrator")
        after_groups = triage["totals"]["merge_groups"]
        co_grouped = [g for g in after_groups if set(chosen["ids"]).issubset(set(g))]
        print(f"triage_after|groups={len(after_groups)}|"
              f"merged_ids_still_grouped_together={co_grouped}")
        if co_grouped:
            status = "fail"
        again = mt.condense_proposals(s, "coding-orchestrator")
        same_proposal = [p for p in again if set(p["ids"]) == set(chosen["ids"])]
        print(f"condense_after_same_ids_proposed={len(same_proposal)}")
        report = mt.budget_report(s, "coding-orchestrator")
        for role, value in report["roles"].items():
            print(f"budget_after|{role}|used={value['sections_now']}|"
                  f"excluded={value['excluded_now']}")
print(f"status={status}")
PYCODE
    } > "$OUT/live_condense.txt"
    rm -rf "$TMPHOME"
    emit live_condense "$(ms "$T0" "$(now)")"
  else
    echo "live DB not found at $LIVE_DB; committed live_condense.txt left untouched"
  fi
fi

if want tsc_web.txt; then
  T0=$(now)
  {
    echo "$STAMP"
    echo "# command: tsc -b --force on the web/ sources of THIS worktree, via a /tmp scratch"
    echo "#          project whose node_modules is the main checkout's (the worktree has none)."
    COMMON="$(git rev-parse --path-format=absolute --git-common-dir)"
    MAIN="${COMMON%/.git}"
    SCRATCH="$(mktemp -d)"
    if [ ! -d "$MAIN/web/node_modules" ]; then
      echo "tsc unavailable: $MAIN/web/node_modules is missing"
      echo "tsc_exit=2"
      rm -rf "$SCRATCH"
      exit 2
    fi
    ln -s "$MAIN/web/node_modules" "$SCRATCH/node_modules"
    for p in src index.html package.json tsconfig.json vite.config.ts tailwind.config.js postcss.config.js; do
      ln -s "$ROOT/web/$p" "$SCRATCH/$p"
    done
    echo "# scratch src resolves to: $(readlink -f "$SCRATCH/src")"
    cd "$SCRATCH"
    if "$MAIN/web/node_modules/.bin/tsc" -b --force; then
      echo "tsc_exit=0"
    else
      rc=$?
      echo "tsc_exit=$rc"
      cd "$ROOT"
      rm -rf "$SCRATCH"
      exit "$rc"
    fi
    echo "# sources the compiler actually read (the two files this round names):"
    "$MAIN/web/node_modules/.bin/tsc" --noEmit --listFiles 2>&1 \
      | grep -E '/src/(api/client|pages/Memory)\.(ts|tsx)$'
    cd "$ROOT"
    rm -rf "$SCRATCH"
  } > "$OUT/tsc_web.txt" 2>&1
  emit tsc_web "$(ms "$T0" "$(now)")"
fi

if want dod_map.txt; then
  T0=$(now)
  { echo "$STAMP"
    echo "# static DoD map; focused_tests.txt supplies each node's PASSED line"
    cat <<'MAP'
1 ingest dedup: a reworded restatement is a REAL pending row linked to the approved
  entry it restates (`duplicate-of:<id>` in the existing tags column), a byte-identical
  suggestion still creates no row, same-domain unrelated facts are not linked, and a row
  created earlier in the same batch is never a link target:
   test_a_reworded_restatement_is_recorded_as_a_linked_row
   test_a_byte_identical_suggestion_still_creates_no_row
   test_same_domain_but_unrelated_facts_are_not_linked
   test_a_reworded_restatement_shared_only_by_subject_is_linked
   test_a_repeated_restatement_does_not_add_a_second_linked_row
   test_a_pending_row_from_the_same_batch_is_not_a_link_target
   test_triage_flags_a_linked_restatement_as_a_merge_candidate
   test_triage_reads_the_link_reason_out_of_tags_json
   source_anchors.txt: memory_service.def _linked_duplicate / duplicate_of /
   duplicate_record / _duplicate_tags; memory_text.SUBJECT_MAX_DF / def is_subject_pair
   (rare identifiers, never a text ratio). The ratio, threshold and shared terms live in
   tags_json next to the link, so triage reads the reason FROM THE ROW.
2 value-ordered demotion from tags_json only, id only the last tiebreak; untagged rows keep
  the pre-T-17 rot-prone-then-oldest order; two calls are byte-identical:
   test_a_pinned_entry_is_kept_while_an_older_unpinned_entry_is_demoted
   test_untagged_entries_keep_the_pre_t17_demotion_order
   test_untagged_durable_entries_are_still_demoted_oldest_first
   test_two_selections_are_byte_identical_and_exclude_the_same_ids
   source_anchors.txt: memory_service.def _keep_priority / _tag_rank; migrations_untouched.txt
3 condense non-destructive default + explicit apply + project scoping + distinct-fact union:
   test_condense_dry_run_changes_no_row
   test_condense_apply_updates_the_survivor_and_rejects_the_absorbed (refuses a rare-token
     merge WITHOUT merged --content; applies with it)
   test_condense_partial_apply_is_refused_without_writing
   test_a_condense_apply_that_lengthens_the_survivor_is_refused
   test_condense_rendered_markdown_matches_the_surviving_approved_rows
   test_a_subject_cluster_proposal_keeps_every_distinct_fact
   test_condense_requires_a_project_and_never_groups_global_or_role_rows
   integration::test_memory_condense_is_a_dry_run_until_apply_names_the_ids
   integration::test_memory_condense_requires_a_project
4 lifecycle behind a contract change, no migration:
   test_lifecycle_fields_stay_behind_a_contract_change_request
   test_the_frozen_memory_table_has_no_lifecycle_columns_yet
   migrations_untouched.txt (empty migration diff; model fields False False)
   integration::test_models_match_migration / test_upgrade_downgrade_populated_db
5 docs/memory.md documents ingest dedup (linked pending row), tag-driven demotion order
  replacing oldest-first, and condense usage including its non-destructive default
6 redaction is recorded, garbled rows are not rendered, logs unchanged, and EVERY
  approval-shaped write refuses one (create(approved=True), import_all, approve):
   test_adding_a_key_value_phrase_records_the_substitution
   test_updating_a_key_value_phrase_records_the_substitution
   test_a_marked_entry_is_garbled_not_rendered_and_not_approvable
   test_a_marked_approved_entry_is_not_injected_or_rendered
   test_creating_an_approved_key_value_entry_is_refused_as_garbled
   test_importing_an_approved_key_value_entry_is_refused_as_garbled
   test_log_redaction_is_still_a_security_control
   plus tests/unit/test_redaction.py in focused_tests.txt (it IS in the focused invocation)
7 eviction is value-ordered, visible and refused on EVERY approval-shaped write, including
  an approval that would leave the row itself out of the injected section:
   test_approval_that_would_evict_names_the_casualty_and_needs_override
   test_approving_a_row_that_would_itself_be_demoted_is_refused
   test_override_records_the_demoted_ids_in_the_build_log
   test_budget_and_triage_name_the_current_demotions
   test_creating_an_already_approved_row_is_refused_when_it_would_evict
   test_importing_an_approved_row_is_refused_when_it_would_evict
   integration::test_approval_eviction_is_refused_and_override_is_explicit
   integration::test_creating_an_approved_row_over_the_cap_is_refused_and_override_is_explicit
   integration::test_memory_add_approve_over_the_cap_is_refused_unless_override
   test_editing_an_approved_row_that_grows_is_refused_when_it_would_evict
   test_editing_an_approved_row_into_a_garbled_shape_is_refused
   test_patching_a_row_cannot_flip_it_to_approved
   test_markdown_import_creates_only_pending_rows_and_never_evicts
   integration::test_editing_an_approved_row_over_the_cap_returns_the_casualty_in_detail
   source_anchors.txt: class EvictionRefused / _refuse_eviction / _eviction_impact /
   _log_eviction_override / def update / replace_approved; both UIs read detail.evicted
   (structure, never a message substring), and tsc_web.txt type-checks web/ from THIS
   worktree with client.ts's `this.detail = detail` shown in source_anchors.txt.
   Build-log event kinds come from structured outcome fields:
   test_suggestion_event_names_come_from_the_structured_kind
   source_anchors.txt: run_service._SUGGESTION_EVENT_BY_KIND
acceptance: live_condense.txt is the live DB snapshot run on a COPY at the operator's caps:
   the operator-cited topical clusters with the RARE-TOKEN answer per cluster, condense dry
   run (rows unchanged), one explicit apply, and budget/triage before and after.
MAP
  } > "$OUT/dod_map.txt"
  emit dod_map "$(ms "$T0" "$(now)")"
fi

printf 'regenerated at %s (mode=%s, only=%s)\n' "$SHORT" "$MODE" "${ONLY:-all}"
