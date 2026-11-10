# T-17 evidence index

Bundle measured at code HEAD `1c878a1` by `orchestrator task evidence T-17 --full` (the
committed `verify-evidence/T-17/capture.sh` is the only thing that ran). Quote only
`timing.txt`: **866 passed, 10 skipped** in the full suite (147.9s), focused memory/API/CLI/
migration run **208 passed** (20.9s), lint **All checks passed!**. Every `.txt` here carries
its own `# HEAD=…` stamp; `freshness_selfcheck.txt` reports `status=pass`, `stamped_files=9`,
`stale_count=0` against `1c878a1` before this index was written.

Round-3 review findings (R-79) fixed on this branch, with their tests:

- **F1 every approval-shaped write refuses a garbled row** — `test_creating_an_approved_key_value_entry_is_refused_as_garbled`,
  `test_importing_an_approved_key_value_entry_is_refused_as_garbled` (plus the existing
  `test_a_marked_entry_is_garbled_not_rendered_and_not_approvable` for `approve()`).
- **F2 self-demotion is never silent** — `test_approving_a_row_that_would_itself_be_demoted_is_refused`,
  `test_override_records_the_demoted_ids_in_the_build_log`; `_eviction_impact` includes the
  row itself (`"self": true`) and an override emits the `memory_eviction_override` build-log
  event with the demoted ids.
- **F3 only an APPROVED row is a link target** — `test_a_pending_row_from_the_same_batch_is_not_a_link_target`;
  the `by_domain.append(outcome.created[-1])` line is gone.
- **F4 the override flow is keyed on structure, not prose** — both UIs read `detail.evicted`
  from the frozen `{"error", "detail"}` 400 body (`Memory.tsx:casualtyLines`, fallback
  `e.detail.evicted`); integration tests assert `detail.evicted` ids.

| DoD | Where it is proven |
|---|---|
| 1. ingest dedup: a reworded restatement is a REAL linked pending row; byte-identical creates no row; same-domain unrelated facts are not linked; a same-batch pending row is not a link target | `focused_tests.txt`: `test_a_reworded_restatement_is_recorded_as_a_linked_row` (new row, `duplicate_of()` = approved id, content persisted, reason = `duplicate_reason(ratio, threshold)`), `test_a_byte_identical_suggestion_still_creates_no_row`, `test_same_domain_but_unrelated_facts_are_not_linked`, `test_a_reworded_restatement_shared_only_by_subject_is_linked` (rare-word containment False, rare-identifier subject True), `test_a_repeated_restatement_does_not_add_a_second_linked_row`, `test_a_pending_row_from_the_same_batch_is_not_a_link_target`, `test_triage_flags_a_linked_restatement_as_a_merge_candidate`. `source_anchors.txt`: `memory_service.def _linked_duplicate`, `def duplicate_of`, `memory_text.SUBJECT_MAX_DF`, `def is_subject_pair`, `def subject_clusters`. |
| 2. value-ordered demotion from `tags_json` only; untagged keeps the old order; calls are byte-identical | `test_a_pinned_entry_is_kept_while_an_older_unpinned_entry_is_demoted`, `test_untagged_entries_keep_the_pre_t17_demotion_order`, `test_untagged_durable_entries_are_still_demoted_oldest_first` (oldest-first pinned among equal-class untagged rows), `test_two_selections_are_byte_identical_and_exclude_the_same_ids`; `source_anchors.txt`: `def _keep_priority` / `def _tag_rank`; `migrations_untouched.txt`. |
| 3. condense lists candidates (ids, token delta, proposed content) and writes nothing without `--apply` naming ids; apply updates + rejects through `memory_service` | `test_condense_dry_run_changes_no_row`, `test_condense_apply_updates_the_survivor_and_rejects_the_absorbed`, `test_condense_partial_apply_is_refused_without_writing`, `test_condense_rendered_markdown_matches_the_surviving_approved_rows`, `test_a_subject_cluster_proposal_keeps_every_distinct_fact`, `test_condense_requires_a_project_and_never_groups_global_or_role_rows`; integration `test_memory_condense_is_a_dry_run_until_apply_names_the_ids`, `test_memory_condense_requires_a_project`; `source_anchors.txt`: `memory_triage.def condense_proposals`, `memory_service.def condense`. |
| 4. lifecycle behind a contract change; no Alembic revision touches `memory_entries` | `docs/contract-change-request-T13-memory-lifecycle.md`; `test_lifecycle_fields_stay_behind_a_contract_change_request`, `test_the_frozen_memory_table_has_no_lifecycle_columns_yet`; `migrations_untouched.txt` (empty migration diff, model fields `False False`); `test_models_match_migration` and `test_upgrade_downgrade_populated_db` PASSED in `focused_tests.txt`. |
| 5. `docs/memory.md` documents ingest dedup, tag-driven demotion replacing oldest-first, and non-destructive condense | `docs/memory.md` sections "Injection budget" (value order, id only a tiebreak, refusals incl. self-demotion, structured `detail.evicted`, override build-log event), "Triage" (`linked-duplicate`), "Condensing memory" (required `--project`, same-scope groups, `distinct_facts` union, dry-run default), "Approval workflow" (same-batch row is not a link target), "Storage is not log redaction" (no path stores an approved garbled row), "Lifecycle fields are not here yet". |
| 6. redaction is recorded, garbled rows are never rendered/approvable, log redaction unchanged | `test_adding_a_key_value_phrase_records_the_substitution`, `test_updating_a_key_value_phrase_records_the_substitution`, `test_a_marked_entry_is_garbled_not_rendered_and_not_approvable`, `test_a_marked_approved_entry_is_not_injected_or_rendered`, `test_creating_an_approved_key_value_entry_is_refused_as_garbled`, `test_importing_an_approved_key_value_entry_is_refused_as_garbled`, `test_log_redaction_is_still_a_security_control`; `tests/unit/test_redaction.py` IS in the focused invocation and its 24 tests PASSED inside the 208. `source_anchors.txt`: `def is_garbled`, `def _redact_memory_content`, `def _refuse_garbled`; triage `redaction-marker`. |
| 7. eviction is value-ordered, visible and refused on every approval-shaped write, including a row that would demote itself | `test_approval_that_would_evict_names_the_casualty_and_needs_override`, `test_approving_a_row_that_would_itself_be_demoted_is_refused`, `test_override_records_the_demoted_ids_in_the_build_log`, `test_budget_and_triage_name_the_current_demotions`, `test_creating_an_already_approved_row_is_refused_when_it_would_evict`, `test_importing_an_approved_row_is_refused_when_it_would_evict`; integration `test_approval_eviction_is_refused_and_override_is_explicit` (asserts `detail.evicted` structure), `test_creating_an_approved_row_over_the_cap_is_refused_and_override_is_explicit`, `test_memory_add_approve_over_the_cap_is_refused_unless_override`. `source_anchors.txt`: `class EvictionRefused`, `def _eviction_impact`, `def _refuse_eviction`, `def _log_eviction_override`, both UIs' `detail.evicted`. |
| Acceptance: live condense at the operator's caps, no unexpected demotion, triage reports no duplicate group for the merged rows | `live_condense.txt`: a read-only `sqlite3 .backup` of the live DB run on a throwaway copy (source sha256 and byte count recorded), caps `global 2000 / role 1100 / project 8000`, condense dry run with `rows_unchanged=True`, one explicit `--apply` with the highest token saving (`[67,49]`, 175 tokens), then budget/triage: merged ids not co-grouped, no section demoted (`excluded=[]`), project usage `7356 -> 7181`. |

## The operator's cluster question, answered

`live_condense.txt` prints `rare_token_pairs` **inside each operator-cited cluster**,
independently of the subject pass:

| Cluster | Rare-token pairs alone | Grouped by `duplicate_groups` |
|---|---|---|
| 152/156/164 (trailer explains, never certifies) | `[[152,156]]` (164 not paired) | `[[152,156]]`, `[164,166]` |
| 157/159/161/163 (diff floor/ceiling arithmetic) | `[]` | `[[84,131,157,158]]`, `[[159,161,163]]` |
| 154/165 (evidence-bundle ordering) | `[]` | `[[66,154,155,165,167]]` |

So the answer is **partly no**: rare-token containment finds only 1 of the 6 measured pairs
and misses the two 4-row/2-row clusters entirely; the rare-identifier subject pass is what
groups them. That is also why ingest links by subject, never by a whole-text ratio.

Supporting checks:

- `lint.txt` — `./scripts/orch_verify.sh lint` at the stamped HEAD: `All checks passed!`
- `full_suite.txt` — `./scripts/orch_verify.sh test` at the stamped HEAD: `866 passed, 10 skipped`
- `assertion_shape.txt` — `python3 scripts/t15_assertion_shape.py`: `assertion_shape=PASS`
  (the two files T-15 pinned were not restructured).
- `dod_map.txt` — the static DoD-to-test map.
- `timing.txt` — both pass modes, written only by the `--full` pass.
- `freshness_selfcheck.txt` — the generator's own worktree-basis check.
- `freshness_at_tip.txt` — the committed-view check at the delivered tip, written last.

`live_condense.txt` is a snapshot: it records the live DB's sha256 and byte count at capture
time. The live home is opened read-only; the condense apply happens on a throwaway copy and
the copy is removed. A re-run on a different live state produces different counts, which is
why this file, like `timing.txt`, quotes its own timestamped measurement rather than a
universal number. If the live DB is absent, the capture leaves the committed file untouched
and says so on stderr.