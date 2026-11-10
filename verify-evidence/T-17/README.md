# T-17 evidence index

Bundle measured at code HEAD `fc74299` by `orchestrator task evidence T-17 --full` (the
committed `verify-evidence/T-17/capture.sh` is the only thing that ran). Quote only
`timing.txt`: **886 passed, 10 skipped** in the full suite (148.9s), focused memory/API/CLI/
migration run **228 passed** (21.6s), lint **All checks passed!**, whole-tree `tsc -b --force`
**tsc_exit=0** (tsc 7.0.2, 279 files listed, 29 under `src/`, build wall time 154ms).
Every `.txt` here carries its own `# HEAD=…` stamp; `freshness_selfcheck.txt` reports
`status=pass`, `stamped_files=10`, `stale_count=0` against `fc74299` before this index was
written.

Round-6 review findings (R-82) fixed on this branch, with their tests:

- **R6-1 a scope move is not a pressure-lowering edit** — `update()`'s `lowers_pressure` now
  also requires `_file_for(priced) == old_file`, so a move with unchanged tags and length
  goes through the full eviction gate with `ignore_self=False`.
  `test_moving_a_row_into_a_full_section_where_it_ranks_lowest_is_refused` asserts the moved
  row is named as the casualty and is refused without `override`, then succeeds with it;
  the same-section rank-lowering and shrinking edits still succeed without an override, and
  a same-section pin still refuses (round-4 behaviour retained).
- **R6-2 untrusted suggestion tags are stripped at ingest** — one helper
  (`strip_reserved_tags`) drops `pinned`/`durable`, every `tier:` and `duplicate-` prefix
  (including a forged `duplicate-of:`) and `redaction-substitution:` from Worker/chat
  suggestions, at both create sites; the harness appends its own link tags after the filter.
  `test_untrusted_suggestion_tags_cannot_claim_priority_or_forge_a_link`,
  `test_a_linked_suggestion_keeps_the_harness_generated_link`,
  `test_memory_add_by_the_operator_keeps_a_pinned_tag` (CLI) and
  `test_export_import_round_trip_preserves_operator_priority_tags` (operator paths and a
  restore keep `pinned`/`tier:2`).
- **R6-3 the suggestion-event lookup is total** — `run_service.SUGGESTION_EVENT_FALLBACK`
  (`memory_suggestion_other`) instead of a `KeyError` during finalisation;
  `test_an_unknown_suggestion_kind_uses_the_named_fallback_event`.
- **R6-4 link terms survive a comma** — `_duplicate_tags` joins terms with the ASCII unit
  separator and `duplicate_record` still reads legacy comma-joined rows;
  `test_a_term_containing_a_comma_round_trips_through_the_link_record`.

Round-5 review findings (R-81) fixed on this branch, with their tests:

- **R5-1 every selection-relevant edit is gated, not only content growth** — `update()` now
  prices ANY change to content, tags, scope, role or category through `_refuse_eviction`;
  pinning a demoted row (`test_pinning_a_demoted_approved_row_is_refused_when_it_would_evict`)
  and moving a row into a tighter scope (`test_moving_an_approved_row_to_a_tighter_scope_is_refused`)
  refuse and name the casualty. A pressure-LOWERING edit still succeeds without override
  (`test_a_rank_lowering_edit_demotes_itself_without_an_override`,
  `test_a_content_shrinking_edit_still_succeeds_without_an_override`), while a growth edit
  that pushes itself out is still refused (integration
  `test_editing_an_approved_row_over_the_cap_returns_the_casualty_in_detail`). The docstring,
  the route comment and `docs/memory.md` now say this.
- **R5-2 whole-tree tsc evidence, not two files** — `tsc_web.txt` records the exact command,
  the resolved `web/tsconfig.json`, `tsc_exit` derived from the command's actual status, the
  unabridged `--listFiles` count (279, 29 under `src/`) with its first and last lines, and the
  loaded `@types/react`/`@types/react-dom` entries. It typechecks the WHOLE `web/src` tree.
  Limitation stated in the file itself: this is typechecking, not a frontend build; the gate
  has no build step (T-31), so it does not prove the GUI works.
- **R5-3 decisive code quoted verbatim** in `handoff.json dod_self_check[].evidence` for the
  condense containment comparison and the kind-based suggestion event lookup.
- **R5-4 no rot-prone memory suggestion** — the round-5 handoff carries durable rules only,
  no snapshot ids/counts.
- **R5-5 every shared subject term is stored** — `_duplicate_tags` no longer keeps only five,
  so `duplicate_record()` rebuilds the SAME reason the outcome/build log reported;
  `test_every_shared_subject_term_is_stored_so_the_reason_matches`.

Round-4 review findings (R-80) fixed on this branch, with their tests:

- **F1 the cluster claims are now the measured answer, not the earlier overclaim** — this
  file's table below quotes `live_condense.txt` line for line; the false `memory_suggestions`
  entry was deleted rather than carried forward (a suggestion would persist the claim).
- **F2 `--apply` never absorbs a row whose text the survivor does not carry** —
  `test_condense_apply_updates_the_survivor_and_rejects_the_absorbed` (refusal without merged
  `--content`, then apply with it), plus `test_a_condense_apply_that_lengthens_the_survivor_is_refused`.
  `live_condense.txt` shows the refusal of `[67,49]` on the live corpus and
  `refusal_rows_unchanged=True`.
- **F3 the link record lives on the row** — `duplicate-record` tags
  (`duplicate-match`, `duplicate-ratio`, `duplicate-threshold`, `duplicate-terms`) in the
  existing `tags_json`; `test_triage_reads_the_link_reason_out_of_tags_json`.
- **F4 one guard on every path that can admit an approved row** —
  `test_editing_an_approved_row_that_grows_is_refused_when_it_would_evict`,
  `test_editing_an_approved_row_into_a_garbled_shape_is_refused`,
  `test_patching_a_row_cannot_flip_it_to_approved`,
  `test_markdown_import_creates_only_pending_rows_and_never_evicts`, and
  integration `test_editing_an_approved_row_over_the_cap_returns_the_casualty_in_detail`.
- **F5 GUI proof** — `tsc_web.txt`: `tsc -b --force` over the WHOLE `web/src` tree of THIS
  worktree with its own `web/tsconfig.json` (scratch project, main checkout's `node_modules`,
  disclosed workaround) exits 0 and lists 279 files, 29 of them under `src/`; `@types/react`
  and `@types/react-dom` entries are named. It does NOT prove the GUI works (no build step in
  the gate; T-31). `source_anchors.txt` pins `client.ts this.detail = detail;` and
  `Memory.tsx e.detail`, so the 400 body's `detail.evicted` reaches the override flow.
- **F6 build-log event names come from structure** — `run_service._SUGGESTION_EVENT_BY_KIND`
  maps `SuggestionOutcome.records[].kind`; `test_suggestion_event_names_come_from_the_structured_kind`
  feeds misleading message texts and asserts each lands under its own event.
- **F7 `handoff.json tests.command`** holds the runnable `./scripts/orch_verify.sh test`.

| DoD | Where it is proven |
|---|---|
| 1. ingest dedup: a reworded restatement is a REAL linked pending row with its reason on the row; byte-identical creates no row; same-domain unrelated facts are not linked | `focused_tests.txt`: `test_a_reworded_restatement_is_recorded_as_a_linked_row` (new row, `duplicate_of()` = approved id, `duplicate_record()` ratio/threshold/reason), `test_a_byte_identical_suggestion_still_creates_no_row`, `test_same_domain_but_unrelated_facts_are_not_linked`, `test_a_reworded_restatement_shared_only_by_subject_is_linked`, `test_a_repeated_restatement_does_not_add_a_second_linked_row`, `test_a_pending_row_from_the_same_batch_is_not_a_link_target`, `test_triage_flags_a_linked_restatement_as_a_merge_candidate`, `test_triage_reads_the_link_reason_out_of_tags_json`, `test_every_shared_subject_term_is_stored_so_the_reason_matches`, `test_a_term_containing_a_comma_round_trips_through_the_link_record`, `test_untrusted_suggestion_tags_cannot_claim_priority_or_forge_a_link`, `test_a_linked_suggestion_keeps_the_harness_generated_link`. `source_anchors.txt`: `def _linked_duplicate`, `def duplicate_of`, `def duplicate_record`, `def _duplicate_tags`, `memory_text.SUBJECT_MAX_DF`, `def is_subject_pair`, `def subject_clusters`. |
| 2. value-ordered demotion from `tags_json` only; untagged keeps the old order; calls are byte-identical | `test_a_pinned_entry_is_kept_while_an_older_unpinned_entry_is_demoted`, `test_untagged_entries_keep_the_pre_t17_demotion_order`, `test_untagged_durable_entries_are_still_demoted_oldest_first`, `test_two_selections_are_byte_identical_and_exclude_the_same_ids`; `source_anchors.txt`: `def _keep_priority` / `def _tag_rank`; `migrations_untouched.txt`. |
| 3. condense lists candidates and writes nothing without `--apply`; apply updates + rejects through `memory_service` and never drops an absorbed fact | `test_condense_dry_run_changes_no_row`, `test_condense_apply_updates_the_survivor_and_rejects_the_absorbed`, `test_condense_partial_apply_is_refused_without_writing`, `test_condense_rendered_markdown_matches_the_surviving_approved_rows`, `test_a_subject_cluster_proposal_keeps_every_distinct_fact`, `test_condense_requires_a_project_and_never_groups_global_or_role_rows`, `test_a_condense_apply_that_lengthens_the_survivor_is_refused`; integration `test_memory_condense_is_a_dry_run_until_apply_names_the_ids` (now asserts the no-`--content` refusal too), `test_memory_condense_requires_a_project`; `source_anchors.txt`: `memory_triage.def condense_proposals`, `memory_service.def condense`. |
| 4. lifecycle behind a contract change; no Alembic revision touches `memory_entries` | `docs/contract-change-request-T13-memory-lifecycle.md`; `test_lifecycle_fields_stay_behind_a_contract_change_request`, `test_the_frozen_memory_table_has_no_lifecycle_columns_yet`; `migrations_untouched.txt` (empty migration diff, model fields `False False`); `test_models_match_migration` and `test_upgrade_downgrade_populated_db` PASSED in `focused_tests.txt`. |
| 5. `docs/memory.md` documents ingest dedup, tag-driven demotion replacing oldest-first, condense usage including its non-destructive default and both refusals | `docs/memory.md` sections "Injection budget" (value order, id only a tiebreak, refusals, the edit/lengthen gate, structured `detail.evicted`, override), "Triage" (`linked-duplicate` reason read from `tags_json`), "Condensing memory" (required `--project`, literal containment before absorbing, `distinct_facts` union, dry-run default, `--override`), "Approval workflow" (link record tags), "Storage is not log redaction". |
| 6. redaction is recorded, garbled rows are never rendered/approvable, log redaction unchanged | `test_adding_a_key_value_phrase_records_the_substitution`, `test_updating_a_key_value_phrase_records_the_substitution` (pending row), `test_editing_an_approved_row_into_a_garbled_shape_is_refused`, `test_a_marked_entry_is_garbled_not_rendered_and_not_approvable`, `test_a_marked_approved_entry_is_not_injected_or_rendered`, `test_creating_an_approved_key_value_entry_is_refused_as_garbled`, `test_importing_an_approved_key_value_entry_is_refused_as_garbled`, `test_log_redaction_is_still_a_security_control`; `tests/unit/test_redaction.py` IS in the focused invocation and its 24 tests PASSED inside the 216. |
| 7. eviction is value-ordered, visible, and refused on every approval-shaped write, including edits that grow, retag or move a row | `test_approval_that_would_evict_names_the_casualty_and_needs_override`, `test_approving_a_row_that_would_itself_be_demoted_is_refused`, `test_override_records_the_demoted_ids_in_the_build_log`, `test_budget_and_triage_name_the_current_demotions`, `test_creating_an_already_approved_row_is_refused_when_it_would_evict`, `test_importing_an_approved_row_is_refused_when_it_would_evict`, `test_editing_an_approved_row_that_grows_is_refused_when_it_would_evict`, `test_pinning_a_demoted_approved_row_is_refused_when_it_would_evict`, `test_moving_an_approved_row_to_a_tighter_scope_is_refused`, `test_moving_a_row_into_a_full_section_where_it_ranks_lowest_is_refused`, `test_a_rank_lowering_edit_demotes_itself_without_an_override`, `test_a_content_shrinking_edit_still_succeeds_without_an_override`, `test_markdown_import_creates_only_pending_rows_and_never_evicts`, `test_patching_a_row_cannot_flip_it_to_approved`; integration `test_approval_eviction_is_refused_and_override_is_explicit`, `test_creating_an_approved_row_over_the_cap_is_refused_and_override_is_explicit`, `test_memory_add_approve_over_the_cap_is_refused_unless_override`, `test_editing_an_approved_row_over_the_cap_returns_the_casualty_in_detail`, `test_memory_add_by_the_operator_keeps_a_pinned_tag`. `source_anchors.txt`: `class EvictionRefused`, `_eviction_impact`, `_refuse_eviction`, `_selection_relevant_change`, `lowers_pressure`, `ignore_self`, `_log_eviction_override`, `def update`, `replace_approved`; both UIs read `detail.evicted`. |
| Acceptance: live condense at the operator's caps; triage reports no duplicate group for the merged rows | `live_condense.txt`: read-only `sqlite3 .backup` of the live DB on a throwaway copy (source sha256 and byte count recorded), caps `global 2000 / role 1100 / project 8000`, dry run `rows_unchanged=True`, the rare-token refusal of `[67,49]`, one union apply `[79,68]` with `absorbed_text_contained=True`, then triage: merged ids not co-grouped and no section demoted (`excluded=[]`). |

## The operator's cluster question, answered from `live_condense.txt`

The capture prints `rare_token_pairs` **inside each operator-cited cluster**, independently of
the subject pass. The measured result is:

| Cluster | Rare-token pairs alone | Grouped by `duplicate_groups` |
|---|---|---|
| evidence_freshness 152/156/164 | `[[152,156]]` (164 not paired) | `[[152,156]]`, `[164,166]` |
| diff_floor 157/159/161/163 | `[]` | `[[84,131,157,158]]`, `[[159,161,163]]` |
| bundle_ordering 154/165 | `[]` | `[[66,154,155,165,167]]` |
| notify_send 135/140/143 | `[]` | `[[135,140,143]]` |

So the honest answer: subject+rare-token grouping **does** catch clusters that lexical
similarity misses (notify_send `135/140/143` exactly; bundle ordering co-groups `154/165`;
diff-floor and evidence-freshness each form real groups), but it **does not reproduce the
operator's cluster boundaries exactly** — `164` grouped with `166` rather than `152/156`,
`157` split away from `159/161/163`, and `bundle_ordering` also pulled in `66/155/167`.
Rare-token containment alone catches **1 of the 10 cited pairs** (`152/156`); the
rare-identifier subject pass is what groups the rest. That is why ingest links by subject,
never by a whole-text ratio.

Supporting checks:

- `lint.txt` — `./scripts/orch_verify.sh lint` at the stamped HEAD: `All checks passed!`
- `full_suite.txt` — `./scripts/orch_verify.sh test` at the stamped HEAD: `879 passed, 10 skipped`
- `tsc_web.txt` — whole-tree `tsc -b --force` over this worktree's `web/src`: `tsc_exit=0`,
  279 files listed (29 under `src/`), scratch `src` resolves to `<worktree>/web/src`.
  Typecheck scope only; not a frontend build check (T-31).
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
