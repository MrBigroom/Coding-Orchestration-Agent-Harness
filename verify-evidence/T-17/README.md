# T-17 evidence index

Bundle measured at `63137c3` (code HEAD), full pass, then committed as an evidence-only
commit. Quote only the numbers in `timing.txt`: **850 passed, 10 skipped** in 147.2s for the
full suite; focused run **116 passed** in 9.3s; lint **All checks passed!**.

| DoD | Where it is proven |
|---|---|
| 1. ingest dedup (reworded links, byte-identical no row, unrelated not linked) | `focused_tests.txt` lines for `test_a_reworded_restatement_is_linked_not_stored`, `test_a_byte_identical_suggestion_still_creates_no_row`, `test_same_domain_but_unrelated_facts_are_not_linked`. `source_anchors.txt` names `memory_service.py:def _linked_duplicate`; it calls `memory_text.is_duplicate_pair` (rare-token containment), never a text ratio. |
| 2. value-ordered demotion from `tags_json` only | `test_a_pinned_entry_is_kept_while_an_older_unpinned_entry_is_demoted`, `test_untagged_entries_keep_the_pre_t17_demotion_order`, `test_two_selections_are_byte_identical_and_exclude_the_same_ids`; `source_anchors.txt` names `_keep_priority` / `_tag_rank`. No migration file changed (`migrations_untouched.txt`). |
| 3. condense dry run and explicit apply | `test_condense_dry_run_changes_no_row`, `test_condense_apply_updates_the_survivor_and_rejects_the_absorbed`, `test_condense_partial_apply_is_refused_without_writing`, `test_condense_rendered_markdown_matches_the_surviving_approved_rows`, and CLI `test_memory_condense_is_a_dry_run_until_apply_names_the_ids` in `focused_tests.txt`. `source_anchors.txt` names `memory_service.py:def condense` and `memory_triage.py:def condense_proposals`. |
| 4. lifecycle stays behind a contract change | `docs/contract-change-request-T13-memory-lifecycle.md` (supersedes/stale_after); `test_lifecycle_fields_stay_behind_a_contract_change_request`, `test_the_frozen_memory_table_has_no_lifecycle_columns_yet`; `migrations_untouched.txt` shows an empty migration diff and `False False` for the model fields; `test_models_match_migration` / `test_upgrade_downgrade_populated_db` PASSED. |
| 5. docs/memory.md documents the new rules | `docs/memory.md` sections "Injection budget" (tag order, id only a tiebreak), "Condensing memory" (non-destructive default, `--apply` ids), "Storage is not log redaction", "Lifecycle fields are not here yet"; ingest dedup is in "Approval workflow" step 2. |
| 6. redaction is recorded, garbled rows are not rendered, logs unchanged | `test_adding_a_key_value_phrase_records_the_substitution`, `test_updating_a_key_value_phrase_records_the_substitution`, `test_a_marked_entry_is_garbled_not_rendered_and_not_approvable`, `test_a_marked_approved_entry_is_not_injected_or_rendered`, `test_log_redaction_is_still_a_security_control`; `tests/unit/test_redaction.py` is included in `focused_tests.txt` and its 24 tests PASSED. `source_anchors.txt` names `is_garbled`, `_redact_memory_content`, and the triage `redaction-marker` flag. |
| 7. eviction is value-ordered, visible and refused by default | `test_approval_that_would_evict_names_the_casualty_and_needs_override`, `test_budget_and_triage_name_the_current_demotions`, and API `test_approval_eviction_is_refused_and_override_is_explicit`; `source_anchors.txt` names `approval_impact`/`approve` and the GUI "Injection caps" panel in both `web/src/pages/Memory.tsx` and the fallback UI. |

Supporting checks:

- `lint.txt` — `./scripts/orch_verify.sh lint` at the stamped HEAD: `All checks passed!`
- `full_suite.txt` — `./scripts/orch_verify.sh test` at the stamped HEAD: `850 passed, 10 skipped`
- `assertion_shape.txt` — `python3 scripts/t15_assertion_shape.py`: `assertion_shape=PASS`
  (the two files T-15 pinned were not restructured).
- `dod_map.txt` — the static DoD-to-test map.
- `timing.txt` — both pass modes, written only by the `--full` pass.
- `freshness_selfcheck.txt` — the generator's own worktree-basis check: `status=pass`,
  `8 stamped evidence file(s) fresh at 63137c3` (self-check before the README/tip files).
- `freshness_at_tip.txt` — the committed-view check at the delivered tip, written last.
