# T-4 handoff — round 5 (attempt 5)

- **Branch**: `task-4-memory-suggestions-accept-unvalidated-scopes-cre`
- **Status**: CHANGES_APPLIED — the round-4 (R-28) review findings applied on the same branch. No earlier commit was amended, rebased or re-split; every round-5 change is a new commit on top.
- **Base / diff root**: merge-base `db00112` (never `main`), per the standing fence.
- **Phase**: T-4 — Memory suggestions accept unvalidated scopes, creating entries no role can ever inject.

## Summary

Round 5 (attempt 5) applies the three operator items. No behaviour changed this round: the
only non-evidence files touched are the two T-4 test files, and the full suite plus lint were
re-measured on the resulting code HEAD.

PROVENANCE, stated so it cannot go stale. The counts below are stamped to code HEAD `abb7615`,
the last commit carrying source or tests in this round; this handoff was written after that
measurement, and the commit that carries it adds only files under `verify-evidence/T-4/`. If
the harness swept uncommitted output the branch tip may be a later recovery commit, and
`handoff_arrival` / the recovery-commit count in the gate record are the authoritative
statement of that.

ITEM 1 (finding 3 and finding 6: assertion shape, and the missing HTTP project/global text).
Every rejection assertion this round added is now ONE assertion per outcome, on an exact string
or a structured error field, never `a in text and b in text`:
* `tests/unit/test_memory_scopes.py::test_check_role_name_names_the_role_vocabulary`,
  `::test_create_refuses_an_unreadable_role_column_even_when_the_scope_is_valid` and
  `::test_update_refuses_a_legacy_role_column_that_no_prompt_reads` compare the service error
  to `role_rejection(name)`, a whole sentence built from `ROLE_SCOPES` and
  `ROLE_SCOPE_ALIASES` (equality, so a duplicated or garbled message fails).
* `::test_the_request_model_refuses_an_unknown_role_by_naming_the_role_files` no longer joins
  `str(ValidationError)` and checks four substrings. It reads `exc.value.errors()`: exactly one
  error, `loc == ()`, `input["role"] == "logbook-writer"`, `str(ctx["error"]) == expected`, and
  `msg == "Value error, " + expected`. Pydantic's own enum message fails all of these.
* The suggestion-drop note and the build-log event are compared with `==` to
  `"dropped suggestion from handoff:R-3: " + vocab_error(...)`, not substring-checked.
* HTTP: `tests/integration/test_memory_scope_boundary.py` now asserts the EXACT project and
  global rejection sentence at POST (`vocab_rejection("project", ...)` /
  `("global", ...)`, derived from `SCOPE_VOCABULARY`), and the role 422 is asserted from the
  structured error list (`detail[0]["loc"] == ["body"]`, `input["role"]`, `ctx["error"]`,
  `msg`). PATCH for a bad project scope, and PATCH of a dead role column, are exact sentences
  too; the CLI asserts the full `error: <sentence>` line.
* The `gc=False` sibling already exists and was not riding on a disjunction:
  `test_fix_scopes_moves_legacy_rows_and_leaves_no_stale_line_in_the_old_file` keeps the old
  file and compares its text exactly to `"# <slug>\n"`;
  `test_fix_scopes_with_gc_on_deletes_the_dead_old_file` asserts `removed_files == [old file]`
  and that it is gone. No `or` assertion remains in the repair tests. No code change was needed
  for this item.

ITEM 2 (findings 1, 2, 4, and the round-scoped fence claim). Provenance is above in the form
the operator prescribed, with no "tip" named as final. On finding 2: I do NOT claim the absence
of evidence machinery -- `round4_demo.py` and `capture.sh` were added in round 4 and
`final_rerun.sh` in round 4's last commit, and all three are still here. Round 5 added no new
script; it only re-ran `final_rerun.sh`, which the operator adopted. On finding 5: this round
(`abb7615`) does not modify `src/orchestrator/services/run_service.py`; the path is in
`files_changed` because earlier rounds touched it, and `git log fcff05d..abb7615 --
src/orchestrator/services/run_service.py` is empty. The `memory list --uninjectable` flag is
retained because DoD 4 requires the marker (operator ruling: not scope creep).

ITEM 3 (process). Handoff written and committed inside the round; suite re-run once at
`abb7615` via the project's own commands and stamped in `verify-evidence/T-4/final_rerun.txt`
(534 passed / 6 skipped / 0 failed, lint exit 0). No scratch files are left in the worktree; the
only uncommitted path at submission is the gitignored `.orchestrator/handoff.json`.

R-28 RECOVERY COMMIT. `ba5dfb8` (harness recovery, never Worker-checked) modified
`verify-evidence/T-4/final_rerun.txt` and added `handoff_round4.{json,md}`. It touched no
contract file and no source file. It is inside the tree that this round's suite+lint re-verified
at `abb7615`, and its stale final_rerun content is superseded by the round-5 re-run in the same
file. Nothing else from it needed repair.

STANDING FENCES: no edit to `docs/frozen-contracts.md`, `adapters/chat/**`,
`test_chat_drivers.py`, `run_service.py`, `review_service.py`, memory caps/budgets (T-13),
review-payload archiving (T-11), timing/duration reporting (T-14); no file added to the frozen
`runs/<run_id>/` layout; no earlier commit amended, rebased or re-split; the diff base is the
merge-base `db00112`, never `main`.


## Files changed

90 paths, generated from `git diff --name-only db00112..HEAD` plus the two `handoff_round5` files this submission adds; `.orchestrator/**` is filtered out because it is gitignored and therefore not on the branch.

```
README.md
docs/decisions.md
docs/memory.md
src/orchestrator/api/routes/memory.py
src/orchestrator/cli.py
src/orchestrator/core/memory_vocab.py
src/orchestrator/core/schemas.py
src/orchestrator/services/chat_service.py
src/orchestrator/services/memory_service.py
src/orchestrator/services/run_service.py
tests/integration/test_memory_scope_boundary.py
tests/unit/test_memory_scopes.py
verify-evidence/T-4/README.md
verify-evidence/T-4/capture.sh
verify-evidence/T-4/checks.txt
verify-evidence/T-4/diff_README.md.part1.txt
verify-evidence/T-4/diff_docs_decisions.md.part1.txt
verify-evidence/T-4/diff_docs_memory.md.part1.txt
verify-evidence/T-4/diff_src_orchestrator_api_routes_memory.py.part1.txt
verify-evidence/T-4/diff_src_orchestrator_cli.py.part1.txt
verify-evidence/T-4/diff_src_orchestrator_cli.py.part2.txt
verify-evidence/T-4/diff_src_orchestrator_core_memory_vocab.py.part1.txt
verify-evidence/T-4/diff_src_orchestrator_core_memory_vocab.py.part2.txt
verify-evidence/T-4/diff_src_orchestrator_core_schemas.py.part1.txt
verify-evidence/T-4/diff_src_orchestrator_services_chat_service.py.part1.txt
verify-evidence/T-4/diff_src_orchestrator_services_memory_service.py.part1.txt
verify-evidence/T-4/diff_src_orchestrator_services_memory_service.py.part2.txt
verify-evidence/T-4/diff_src_orchestrator_services_memory_service.py.part3.txt
verify-evidence/T-4/diff_src_orchestrator_services_memory_service.py.part4.txt
verify-evidence/T-4/diff_src_orchestrator_services_memory_service.py.part5.txt
verify-evidence/T-4/diff_src_orchestrator_services_run_service.py.part1.txt
verify-evidence/T-4/diff_tests_integration_test_memory_scope_boundary.py.part1.txt
verify-evidence/T-4/diff_tests_integration_test_memory_scope_boundary.py.part2.txt
verify-evidence/T-4/diff_tests_integration_test_memory_scope_boundary.py.part3.txt
verify-evidence/T-4/diff_tests_integration_test_memory_scope_boundary.py.part4.txt
verify-evidence/T-4/diff_tests_unit_test_memory_scopes.py.part1.txt
verify-evidence/T-4/diff_tests_unit_test_memory_scopes.py.part2.txt
verify-evidence/T-4/diff_tests_unit_test_memory_scopes.py.part3.txt
verify-evidence/T-4/diff_tests_unit_test_memory_scopes.py.part4.txt
verify-evidence/T-4/diff_tests_unit_test_memory_scopes.py.part5.txt
verify-evidence/T-4/diff_tests_unit_test_memory_scopes.py.part6.txt
verify-evidence/T-4/final_rerun.sh
verify-evidence/T-4/final_rerun.txt
verify-evidence/T-4/full_suite.txt
verify-evidence/T-4/handoff_round2.json
verify-evidence/T-4/handoff_round2.md
verify-evidence/T-4/handoff_round3.json
verify-evidence/T-4/handoff_round3.md
verify-evidence/T-4/handoff_round4.json
verify-evidence/T-4/handoff_round4.md
verify-evidence/T-4/handoff_round5.json
verify-evidence/T-4/handoff_round5.md
verify-evidence/T-4/live_home_readonly.txt
verify-evidence/T-4/manifest.txt
verify-evidence/T-4/memory_scope_tests.txt
verify-evidence/T-4/review_round2.txt
verify-evidence/T-4/review_round3.txt
verify-evidence/T-4/review_round4.txt
verify-evidence/T-4/round4_demo.py
verify-evidence/T-4/slice_cli__gc_only_branch.txt
verify-evidence/T-4/slice_cli__memory_add.txt
verify-evidence/T-4/slice_cli__memory_fix_scopes.txt
verify-evidence/T-4/slice_cli__memory_list.txt
verify-evidence/T-4/slice_cli__print_injectability.txt
verify-evidence/T-4/slice_memory_service___renderer_output.txt
verify-evidence/T-4/slice_memory_service__check_role_name.txt
verify-evidence/T-4/slice_memory_service__coerce_scope.txt
verify-evidence/T-4/slice_memory_service__create.txt
verify-evidence/T-4/slice_memory_service__fix_scopes.txt
verify-evidence/T-4/slice_memory_service__gc_orphan_files.txt
verify-evidence/T-4/slice_memory_service__import_all.txt
verify-evidence/T-4/slice_memory_service__injectability.txt
verify-evidence/T-4/slice_memory_service__normalize_scope.txt
verify-evidence/T-4/slice_memory_service__orphan_files.txt
verify-evidence/T-4/slice_memory_service__re_exports.txt
verify-evidence/T-4/slice_memory_service__reachable_roles.txt
verify-evidence/T-4/slice_memory_service__role_memory_file.txt
verify-evidence/T-4/slice_memory_service__update.txt
verify-evidence/T-4/slice_memory_service__validate.txt
verify-evidence/T-4/slice_memory_vocab__vocabularies.txt
verify-evidence/T-4/slice_run_service__store_memory_suggestions.txt
verify-evidence/T-4/slice_schemas__memory_create.txt
verify-evidence/T-4/slice_tests_integration__gc_only_honesty.txt
verify-evidence/T-4/slice_tests_integration__round4_api_text.txt
verify-evidence/T-4/slice_tests_unit__coercion_and_log.txt
verify-evidence/T-4/slice_tests_unit__list_marker.txt
verify-evidence/T-4/slice_tests_unit__repair_and_gc.txt
verify-evidence/T-4/slice_tests_unit__role_column_boundary.txt
verify-evidence/T-4/slice_tests_unit__round4_text_and_split.txt
verify-evidence/T-4/slices_index.txt
```

## Tests

- Command: `./scripts/orch_verify.sh test` -> **534 passed, 6 skipped, 0 failed** (`FINAL_SUITE_EXIT=0`)
- Command: `./scripts/orch_verify.sh lint` -> **All checks passed!** (`FINAL_LINT_EXIT=0`)
- Stamped HEAD: `abb7615` (`# HEAD=abb7615c0595ebadee5adaad540417e18f81c81f`), tree=0 outside `verify-evidence/T-4`
- Log: `verify-evidence/T-4/final_rerun.txt` (produced by `final_rerun.sh`, the project's own commands, offline)

## DoD self-check

1. [x] **1. Project suggestions outside conventions|decisions|gotchas are coerced to the deterministic fallback (gotchas), and the substitution is recorded where the operator can see it; role suggestions outside ROLE_FILES are dropped with a warning instead of stored.**
   - Evidence: src/orchestrator/core/memory_vocab.py coerce_scope/substitution_note (FALLBACK_SCOPE project=gotchas, global=environment; role re-raises) and memory_service.suggest_from_handoff_report; notes go to the build log via run_service.store_memory_suggestions as memory_scope_substituted / memory_suggestion_dropped. Tests: tests/unit/test_memory_scopes.py test_project_suggestion_outside_the_vocabulary_is_coerced_and_reported, test_role_suggestion_outside_the_vocabulary_is_dropped_not_stored (exact drop sentence), test_suggestion_notes_reach_the_build_log (exact event text).

2. [x] **2. API and CLI create/update boundaries reject an out-of-vocabulary scope for EVERY category with an error naming the allowed values; role rejects through the ROLE_FILES vocabulary after ROLE_MEMORY_KEY; the three vocabularies are named importable constants (ROLE_SCOPES added).**
   - Evidence: src/orchestrator/core/memory_vocab.py GLOBAL_SCOPES/PROJECT_SCOPES/ROLE_FILES/ROLE_SCOPES/ROLE_SCOPE_ALIASES; core/schemas.py MemoryCreate model_validator uses check_role_name so HTTP 422 carries the role-files sentence; memory_service._validate checks every category; api/routes/memory.py returns 400 with str(MemoryError_). Tests assert the EXACT sentence at POST for global/project/role, at PATCH for project and a dead role column, at MemoryCreate, and in the CLI (full 'error: <sentence>' line): tests/integration/test_memory_scope_boundary.py::test_api_create_rejects_an_uninjectable_scope_for_every_category, ::test_api_rejects_every_category_with_its_own_named_vocabulary, ::test_api_create_refuses_an_unknown_role_with_a_message_naming_the_role_files, ::test_api_patch_rejects_an_uninjectable_scope, ::test_cli_add_rejects_and_names_the_allowed_values; tests/unit/test_memory_scopes.py::test_the_request_model_refuses_an_unknown_role_by_naming_the_role_files, test_the_vocabularies_are_one_source_importable_from_core_and_re_exported.

3. [x] **3. A repair path migrates entries already stored with invalid scopes, re-renders BOTH the old and the new markdown file, prints the ids it moved, and leaves rejected rows alone.**
   - Evidence: memory_service.fix_scopes re-renders old_file and new home and returns moved/skipped/removed_files/ghost_files; cli memory fix-scopes prints 'moved #<id> ... (ids: ...)' and 'left #<id>'. Tests: tests/unit/test_memory_scopes.py::test_fix_scopes_moves_legacy_rows_and_leaves_no_stale_line_in_the_old_file (gc=False, old file exact '# <slug>\n', new file exact two lines), ::test_fix_scopes_with_gc_on_deletes_the_dead_old_file (gc on, removed_files == [old file], file gone), ::test_fix_scopes_leaves_rejected_rows_alone; tests/integration/test_memory_scope_boundary.py::test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files.

4. [x] **4. orchestrator memory list marks any entry whose scope cannot be reached by its OWN category as uninjectable, and valid role entries such as category=role scope=worker are never misflagged.**
   - Evidence: memory_service.injectability/reachable_roles resolve global scopes against GLOBAL_SCOPES, project scopes against PROJECT_SCOPES filtered by ROLE_PROJECT_SCOPES, and role rows through _file_for/ROLE_MEMORY_KEY; cli memory list prints 'uninjectable #<id> <category>:' with the vocabulary. Tests: tests/integration/test_memory_scope_boundary.py::test_cli_list_marks_uninjectable_entries_and_leaves_valid_role_rows_alone (exactly one marker; valid role rows absent), ::test_cli_list_prints_an_uninjectable_scope_literally; tests/unit/test_memory_scopes.py list-marker tests including category=role scope=worker.

5. [x] **5. New offline unit tests cover coercion, boundary rejection, the repair path re-rendering both files, and the list marker; make test and make lint both pass with no network access and no API keys.**
   - Evidence: Offline suite (isolated ORCHESTRATOR_HOME per test, no adapter/provider call, no network) run with the project's own commands in this worktree: ./scripts/orch_verify.sh test -> 534 passed, 6 skipped, 0 failed; ./scripts/orch_verify.sh lint -> All checks passed!, FINAL_SUITE_EXIT=0, FINAL_LINT_EXIT=0, stamped HEAD=abb7615 in verify-evidence/T-4/final_rerun.txt. T-4 tests: 63 (44 unit + 19 integration) passed.

## New dependencies

None.

## Assumptions / questions / blockers

Assumptions:
- This handoff was written at abb7615, before the round's terminal event. If the harness swept uncommitted output, the branch tip may be a later recovery commit; handoff_arrival and the recovery-commit count in the gate record are the authoritative statement of that. All counts are stamped to abb7615.
- files_changed is derived from `git diff --name-only db00112..HEAD` plus the two handoff_round5 files this submission adds; `.orchestrator/handoff.json` is gitignored and deliberately excluded.
- Harness recovery commit ba5dfb8 (never Worker-checked) modified verify-evidence/T-4/final_rerun.txt and added handoff_round4.{json,md}. It touched no contract or source file; its content is inside the tree re-verified at abb7615, and its stale stamp is superseded by the round-5 rerun.
- The counts are the number of tests, not a claim about coverage. The reference for this branch was 529 passed / 6 skipped; the current 534 includes the round-4 tests.
- No new dependencies. No contract file changed; contracts_touched is empty.

Questions: none.
Blockers: none.

## Contracts touched

None.
