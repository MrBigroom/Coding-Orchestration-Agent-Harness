# T-4 handoff — round 4 (attempt 4)

- **Branch**: `task-4-memory-suggestions-accept-unvalidated-scopes-cre`
- **Status**: CHANGES_APPLIED — the round-3 (R-27) findings applied on the same branch. No earlier commit was amended, rebased or re-split; every round-4 change is a new commit on top.
- **Base / diff root**: merge-base `db00112` (never `main`), per the standing fence.
- **Phase**: T-4 — Memory suggestions accept unvalidated scopes, creating entries no role can ever inject.

## Summary

Round 4 applies the operator's four items; no new behaviour was invented.
ITEM 1 (the handoff, not the code). `files_changed` is GENERATED from the branch diff
(`git diff --name-only --cached db00112`, i.e. exactly the paths this commit lands with), sorted,
so it cannot contain the gitignored `.orchestrator/handoff.json` that R-27 finding 2 named. At
the stamped code HEAD `fcff05d` the list was 75 paths and `round4_demo.py` ITEM 1 verified all 75
against `git ls-tree HEAD` (`75/75`, none gitignored) and printed
`git check-ignore -v .orchestrator/handoff.json` beside them. This handoff carries 88: the same
75 plus the 13 evidence files that the capture and the two rerun commits added afterwards, all
under `verify-evidence/T-4/` (including the committed copy of this handoff). Nothing outside that
directory changed after `fcff05d`, which is also why the counts are unchanged.
ITEM 2 (DoD 2 at the API boundary, on the MESSAGE). An unknown `role` at `POST /api/memory` was
answered by Pydantic's `Role` enum with `Input should be 'architect', 'supervisor', 'worker',
'reviewer' or 'agent'` — role VALUES, with no vocabulary joined as `architect|worker|supervisor`,
no `reviewer -> architect` / `agent -> worker` aliases and no mention of the role memory FILES.
`MemoryCreate` now runs `check_role_name` in a `model_validator(mode="before")`
(`core/schemas.py:358-386`), which is the only place that can beat an enum coercion, so the 422
detail carries the same sentence the CLI, `_validate` and `import_all` use. The field is still
typed `Role | None`: all five members (aliases included) still validate, nothing new is accepted,
so the frozen request shape is intact and round 3's objection ("widening `MemoryCreate.role` to
str for a message string") does not apply. To make the vocabularies reachable from `schemas` at
all they moved to `core/memory_vocab.py` (`memory_service` imports `schemas`, so `schemas` cannot
import the service) and `memory_service` re-exports every name; a test asserts the two modules
hold the SAME objects. Assertions are on the error TEXT at every boundary that rejects a scope or
a role — project, global, role scope and the role column, at the service, the request model, the
HTTP route and the CLI. Deliberate, because a per-value loop is NOT proof: Pydantic's enum message
contains the words `architect`, `worker` and `supervisor` too. The tests pin the joined
vocabulary, the phrase `allowed role memory files` and both alias arrows, and `round4_demo.py`
ITEM 2 prints the enum-only message beside them to show the assertions are load-bearing.
ITEM 3 (assertion shape). `tests/unit/test_memory_scopes.py:473`'s
`assert not orphan.exists() or f"mem:{id}" not in orphan.read_text()` is gone. Two named cases,
one outcome each: `test_fix_scopes_moves_legacy_rows_and_leaves_no_stale_line_in_the_old_file`
runs with `gc=False` so the dead file SURVIVES and compares its text exactly to `"# <slug>\n"`
(plus the new file's exact two lines and `ghost_files` still naming it), and
`test_fix_scopes_with_gc_on_deletes_the_dead_old_file` runs with the collector on and asserts
`removed_files == [old_file]` and that the file is gone. Both were mutation-checked in-process:
with the old file's `render_file` call skipped, the OLD disjunction passes while CASE A's exact
text fails; with `gc_orphan_files` patched to delete nothing, only CASE B fails. Runtime patches
of the demo process only — no committed file is edited, and the mutations are printed in
`review_round4.txt` rather than described.
ITEM 4 (provenance). One number, stated the same way everywhere: 534 passed / 6 skipped / 0
failed with lint clean. Measured at `fcff05d` (the last commit carrying source, tests and docs;
stamped in `full_suite.txt`, `checks.txt`, `memory_scope_tests.txt`, `review_round4.txt`, tree=0
outside `verify-evidence/T-4`) and re-measured at the branch tip `f136a3b` in `final_rerun.txt`
(`FINAL_SUITE_EXIT=0`, `FINAL_LINT_EXIT=0`, stamped with that sha). The commits after `fcff05d`
add only files under `verify-evidence/T-4/`, which `ruff check src tests` and the pytest
collection never read — printed in both logs as the command that proves it. 534 = the round-3
reference of 529 plus the 5 tests this round added (63 T-4 tests now: 44 unit + 19 integration).
NOT WORKED ON, per the directive: reviews.19 finding 4 (truncated diff / DoD self-check in the
review payload) is T-11. No new evidence machinery beyond the two capture scripts already in this
directory, no new file under the frozen `runs/<run_id>/` layout, no repository configuration to
shrink the diff. Finding 1 (verification must pass before approval) is the gate working, and is
met here by `final_rerun.txt` being produced by the project's own commands on this branch.
Standing fences respected: no edit to `docs/frozen-contracts.md`, `adapters/chat/**`,
`test_chat_drivers.py`, `run_service.py`, `review_service.py`, no memory budgeting, no review
payload archiving, no timing report; the branch's earlier commits are not amended, rebased or
re-split; the diff base is the merge-base `db00112`, not `main`.


## Files changed

88 paths, generated with `git diff --name-only --cached db00112` and sorted; `.orchestrator/**` is filtered out because `git check-ignore -v` proves it is gitignored and therefore not on the branch.

Source, tests and docs (the only paths any test or lint command reads):

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
```

Evidence for the review gate (76 paths, all under `verify-evidence/T-4/`; the repo's `test` and `lint` commands never read this directory):

```
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

The two `handoff_round4.*` entries are added by the commit that lands this handoff; every other entry is already in `HEAD`'s tree, which `review_round4.txt` ITEM 1 checks path by path against `git ls-tree HEAD`.

## Tests

| command | result | exit | where |
|---|---|---|---|
| `./scripts/orch_verify.sh test` | **534 passed / 6 skipped / 0 failed** | `SUITE_EXIT=0` | `verify-evidence/T-4/full_suite.txt`, stamped `# HEAD=fcff05d` |
| `./scripts/orch_verify.sh test` (re-run at the branch tip) | **534 passed / 6 skipped / 0 failed** | `FINAL_SUITE_EXIT=0` | `verify-evidence/T-4/final_rerun.txt`, stamped `# HEAD=f136a3b` |
| `./scripts/orch_verify.sh lint` | `All checks passed!` | `LINT_EXIT=0` | `checks.txt`, stamped `fcff05d` |
| `./scripts/orch_verify.sh lint` (at the tip) | `All checks passed!` | `FINAL_LINT_EXIT=0` | `final_rerun.txt`, stamped `f136a3b` |
| `./scripts/orch_verify.sh test tests/unit/test_memory_scopes.py tests/integration/test_memory_scope_boundary.py -vv` | **63 named T-4 tests** (44 unit + 19 integration), 0 failed | `TARGETED_EXIT=0` | `memory_scope_tests.txt` |

Every one of those logs begins with the `ORCH_VERIFY|mode=...|module=<this worktree>/src/orchestrator/__init__.py|worktree_root=<this worktree>` marker line, so the code under test was this branch and not main's editable install. Offline throughout: no network, no API keys, isolated `tmp_path` `ORCHESTRATOR_HOME`, in-process `TestClient`, Typer `CliRunner`, and `round4_demo.py` runs in a throwaway home that never opens the live one.

## DoD self-check (an item is ticked only if it is literally true)

### DoD 1 — met: **YES**

> 1. Project suggestions whose scope is not conventions|decisions|gotchas are coerced to a deterministic fallback (gotchas) and the substitution is recorded where the operator can see it (run log and/or handoff result); role suggestions outside ROLE_FILES are dropped with a warning rather than stored.

**Evidence**: core/memory_vocab.py:156 coerce_scope + :43 FALLBACK_SCOPE (project->gotchas, global->environment) + :148 substitution_note; memory_service.py:278 suggest_from_handoff_report returns SuggestionOutcome(created, notes) and drops a role suggestion that resolves to no role memory file; run_service.py:448 _store_memory_suggestions writes each note to the build log as memory_scope_substituted / memory_suggestion_dropped (call sites :565, :1212, :1327); chat_service.py:554-566 emits the same notes. Landed in rounds 1-2, UNCHANGED this round except the move to core/memory_vocab.py. Tests: tests/unit/test_memory_scopes.py test_project_suggestion_with_an_invented_scope_is_coerced_and_recorded, test_coercion_falls_back_and_never_infers_from_the_words_in_the_scope, test_a_scope_mentioning_decisions_does_not_route_the_note_to_the_architect, test_role_suggestion_outside_the_vocabulary_is_dropped_not_stored, test_coerced_suggestion_reaches_a_prompt_once_approved, test_coercion_is_written_to_the_build_log_the_operator_reads (asserts the two build-log rows). Logs: verify-evidence/T-4/checks.txt section (1), memory_scope_tests.txt (44 named unit tests), full_suite.txt at fcff05d.

### DoD 2 — met: **YES**

> 2. The API and CLI create/update boundaries reject an out-of-vocabulary scope for EVERY category, with an error naming the allowed values: global -> environment|preferences|patterns, project -> conventions|decisions|gotchas, role -> the ROLE_FILES vocabulary after applying ROLE_MEMORY_KEY (reviewer -> architect, agent -> worker), so category=role stops being only half-guarded by memory_service._validate l.88-95 and the three vocabularies are exposed as named constants the tests can import (add the missing ROLE_SCOPES).

**Evidence**: Vocabularies as importable constants: core/memory_vocab.py:25 GLOBAL_SCOPES, :26 PROJECT_SCOPES, :27 ROLE_FILES, :33 ROLE_SCOPES (the missing constant), :35 SCOPE_VOCABULARY, :49 ROLE_PROJECT_SCOPES, :57 ROLE_MEMORY_KEY, :59 ROLE_SCOPE_ALIASES; re-exported by memory_service.py:35-53 and asserted to be the SAME objects by test_the_vocabularies_are_one_source_importable_from_core_and_re_exported. One check per category: memory_vocab.py:128 normalize_scope -> :113 vocab_error; the role COLUMN via :91 check_role_name, called by memory_service.py:197 _validate at :215 BEFORE the scope, by cli.py:1070 memory_add, and (this round) by core/schemas.py:358-386 MemoryCreate. Boundary TEXT assertions (not status codes): unit test_create_rejects_out_of_vocabulary_scope_for_every_category (3 categories), test_update_rejects_an_out_of_vocabulary_scope, test_check_role_name_names_the_role_vocabulary, test_the_request_model_refuses_an_unknown_role_by_naming_the_role_files; integration test_api_rejects_every_category_with_its_own_named_vocabulary, test_api_create_refuses_an_unknown_role_with_a_message_naming_the_role_files (asserts 'architect|worker|supervisor', 'allowed role memory files', both alias arrows and the refused value in the 422 body), test_api_create_still_accepts_every_role_name_the_enum_allows (the accepted set is still exactly the five Role members), test_api_patch_rejects_an_uninjectable_scope, test_api_patch_refuses_a_legacy_role_column_even_with_an_in_vocabulary_scope, test_cli_add_rejects_and_names_the_allowed_values, test_cli_add_refuses_a_role_name_no_prompt_can_read. Logs: verify-evidence/T-4/review_round4.txt ITEM 2 (a)-(d), which prints the text each boundary answers with and Pydantic's enum-only message for contrast; slice_schemas__memory_create.txt, slice_memory_vocab__vocabularies.txt, slice_memory_service__re_exports.txt, slice_tests_integration__round4_api_text.txt. Mutation check: with the MemoryCreate validator neutered only the three vocabulary-text assertions fail (see the handoff summary and README.md).

### DoD 3 — met: **YES**

> 3. A repair path migrates entries already stored with invalid scopes (orchestrator memory fix-scopes, or repair during import): it must re-render BOTH the old and the new markdown file so no orphan file keeps a stale line, print the ids it moved, and leave rejected rows alone.

**Evidence**: memory_service.py:559 fix_scopes moves rows and never deletes one; re-renders the OLD and the NEW file at :646-647 (and :651 for a row that was never approved); returns ScopeRepair(moved with old_file/new_file ids, skipped with reasons, removed_files, ghost_files); rejected rows go to skipped with 'rejected rows are left alone'; a role row is reported, never re-homed. :537 gc_orphan_files / :517 orphan_files / :494 _renderer_output / :476 in_layout_dir / :455 canonical_file collect the dead scope-file only when it is unowned, outside the canonical layout and text `render_file` could have written, so hand-edited markdown survives. cli.py:1124 memory_fix_scopes prints the ids it moved and, at :1147-1156, only the deletions it performed. import_all (:742) + _import_keys (:723) repair a legacy export on the way in and de-duplicate on both spellings. New this round (item 3): the re-render and the deletion are two named tests with one outcome each, exact-string assertions plus the gc=False case -- test_fix_scopes_moves_legacy_rows_and_leaves_no_stale_line_in_the_old_file, test_fix_scopes_with_gc_on_deletes_the_dead_old_file, test_fix_scopes_can_move_rows_without_deleting_a_single_file. Both mutations are demonstrated in verify-evidence/T-4/review_round4.txt ITEM 3 (CASE A A1-A5, CASE B B1-B4, MUTATION 1, MUTATION 2). Also covered: test_fix_scopes_leaves_rejected_rows_alone, test_fix_scopes_refuses_to_guess_a_role_and_to_delete_a_row, test_fix_scopes_dry_run_writes_nothing, test_gc_only_deletes_files_render_file_could_have_written, test_import_does_not_duplicate_a_row_that_is_still_awaiting_fix_scopes, and at the CLI test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files, test_cli_fix_scopes_no_gc_moves_rows_and_deletes_nothing, test_cli_fix_scopes_gc_only_reports_only_the_files_it_actually_deleted. Contract held: no memory_entries row is deleted and the repair is idempotent (a second run moves nothing, asserted in both new tests).

### DoD 4 — met: **YES**

> 4. orchestrator memory list marks any entry whose scope cannot be reached by its OWN category as uninjectable -- global scopes read by every role, project scopes filtered by ROLE_PROJECT_SCOPES, role entries resolved through _file_for/ROLE_MEMORY_KEY -- so this class of defect is visible immediately instead of discovered months later, and valid role entries such as category=role scope=worker are never misflagged.

**Evidence**: memory_service.py:155 injectability / :137 reachable_roles / :122 role_file_key / :183 _file_for resolve exactly what for_prompt (:664) reads: global by every role, project filtered by ROLE_PROJECT_SCOPES, role by `role`, else `scope`, through ROLE_MEMORY_KEY -- so category=role scope=worker (#39/#47 shape) stays ok while role='logbook-writer' is flagged with the dead file name. cli.py:1027 memory_list prints the `inj` column (:980 _memory_table), _print_injectability (:1007) prints the full reason under the table and --uninjectable filters. Tests: test_injectability_marks_only_the_unreachable, test_role_rows_still_render_by_the_rule_main_used, test_legacy_rows_that_cannot_be_injected_do_not_reach_any_prompt, test_cli_list_marks_uninjectable_entries_and_leaves_valid_role_rows_alone, test_cli_list_prints_an_uninjectable_scope_literally. Logs: checks.txt section (4)/(5) and live_home_readonly.txt -- a READ-ONLY (sqlite3 mode=ro, no CLI, no writes) audit of the operator's live home: 76 rows, 38 approved / 12 rejected / 26 pending, 1 uninjectable left (#6, the row the operator rejected on 2026-11-03) and the measured ghost memory/projects/ai-livestreaming-avatar/ai-livestreaming-avatar.md (content '# ai-livestreaming-avatar\n') still unowned, which is what fix-scopes' GC collects.

### DoD 5 — met: **YES**

> 5. New offline unit tests cover coercion, boundary rejection, the repair path re-rendering both files, and the list marker; make test and make lint both pass with no network and no API keys.

**Evidence**: make test -> ./scripts/orch_verify.sh test -> 534 passed / 6 skipped / 0 failed, SUITE_EXIT=0, stamped at fcff05d in verify-evidence/T-4/full_suite.txt and re-measured at the branch tip f136a3b in final_rerun.txt (FINAL_SUITE_EXIT=0). make lint -> ./scripts/orch_verify.sh lint -> 'All checks passed!', LINT_EXIT=0 (checks.txt) and FINAL_LINT_EXIT=0 (final_rerun.txt). T-4 alone: 63 tests named individually (-vv) in memory_scope_tests.txt, 44 unit + 19 integration, TARGETED_EXIT=0. Both markers in every log read `module_file=<this worktree>/src/orchestrator/__init__.py`, so the branch's code was imported and not main's. Offline: the `home`/`session` fixtures give an isolated tmp ORCHESTRATOR_HOME, worker/scheduler env off, the six SECRET_ENV vars deleted (tests/conftest.py); API tests use the in-process TestClient (no socket), CLI tests Typer's CliRunner; round4_demo.py runs in a throwaway home. No adapter, provider, network or API-key call anywhere in the added tests. New test files/functions this round are listed in items 2 and 3.

## New dependencies

None. The frozen stack is unchanged (no framework or library substitution, nothing added to `pyproject.toml` or `uv.lock`).

## Assumptions

- The only counts claimed anywhere in this handoff are 534 passed / 6 skipped / 0 failed and 'All checks passed!', stamped at fcff05d (full_suite.txt, checks.txt, memory_scope_tests.txt, review_round4.txt) and re-measured at the branch tip f136a3b (final_rerun.txt, stamped with that sha). 534 = the round-3 reference 529 plus the 5 tests round 4 adds; nothing else changed the count.
- Every commit after fcff05d touches only verify-evidence/T-4/. `ruff check src tests` and the pytest collection never read that directory, so a re-run at the tip must agree with the stamped run -- and it does: FINAL_SUITE_EXIT=0 / FINAL_LINT_EXIT=0 with the identical 534 / 6 / 0.
- files_changed is generated by `git diff --name-only --cached db00112` (sorted, minus any .orchestrator/ path) rather than typed; every entry exists in HEAD's tree, verified in review_round4.txt ITEM 1.
- The reviewer-reachable copy of this handoff is verify-evidence/T-4/handoff_round4.json / .md, committed on the branch, because .orchestrator/ is gitignored and not archived into runs/<run_id>/.
- The three `# noqa: F401` markers on memory_service.py's re-export lines are the documented re-export idiom, not a rule switched off to pass a finding: no code in that module calls scope_vocabulary / substitution_note / ROLE_SCOPE_ALIASES, but `memory_service.<name>` is the surface callers and tests already read. No lint rule, validator or allowlist was weakened anywhere in this round.
- reviews.19 finding 4 (truncated diff and DoD self-check in the review payload) is T-11 and was not worked on. Finding 1 (verification must pass before approval) is the gate working correctly and is met by final_rerun.txt, not argued with.

## Questions

- POST /api/memory still answers an out-of-vocabulary ROLE with 422 (Pydantic) while an out-of-vocabulary SCOPE answers 400 (the service). DoD 2 asks for the allowed VALUES in the message, which both now carry. Should the two be unified to one status code, or is the split acceptable? Unifying would mean changing `MemoryCreate.role`'s type, which I treated as a frozen-shape change and did not make.
- `orchestrator memory approve` still accepts an uninjectable row (it renders to the dead file and `memory list` flags it). No DoD item asks for a refusal, so I left it; is a refusal wanted as a follow-up?
- DoD 3 allows 'fix-scopes, or repair during import'; both exist. Confirm if exactly one repair surface is wanted.
- Does the operator want verify-evidence/T-4/ kept per task, or should the evidence path move to one agreed location? It is the only path that is reviewer-reachable under the current .gitignore + runs/<run_id>/ archive rules.

## Blockers

None. No frozen contract had to change, no DoD item proved impossible as written, and no test needed a contract broken to pass. The two contract surfaces this task touches at all are named under Contracts touched, and both are additive (the error TEXT inside an already-frozen body, and a re-exported module).

## Contracts touched

- SQLite/SQLModel schema: unchanged -- no model and no migration edit, and no memory_entries row is deleted by any code path in this task (idempotent repair, asserted per DoD 3).
- REST API shapes under /api: no new route, no renamed or removed key, no changed status code. `MemoryCreate` gained a `model_validator(mode="before")` (core/schemas.py:358-386) that runs the existing `check_role_name`; `role` stays typed `Role | None` so the enum, the request shape and the OpenAPI document are unchanged, and only the TEXT inside the already-frozen 422 {"error","detail"} body changed. DoD 2 authorizes a boundary that 'rejects ... with an error naming the allowed values'.
- Build-log events: line shape unchanged; the two event names memory_scope_substituted / memory_suggestion_dropped are ADDITIVE (rounds 1-2) and nothing was renamed or removed.
- CLI command surface: `memory fix-scopes` (rounds 1-2, DoD 3-authorised) and its options are unchanged; this round added no command and changed no argument or exit code.
- Artifact directory layout: unchanged. Nothing was added under the frozen runs/<run_id>/ layout; evidence stays under the repository path verify-evidence/T-4/ because .orchestrator/ is gitignored and is not archived into runs/<run_id>/.
- Memory scope vocabularies (the task's own frozen entries: approved entries render to $ORCHESTRATOR_HOME/memory/**.md with one '- <content>  <!-- mem:ID -->' line each; existing memory_entries rows are never deleted; scope repair is idempotent): held -- render_file is byte-identical to the merge-base and the two new repair tests assert both the exact rendered text and the second-run no-op.
- Role-injection semantics: unchanged, and explicitly out of scope. for_prompt, ROLE_PROJECT_SCOPES and _file_for's resolution are byte-identical to db00112 in behaviour; the move to core/memory_vocab.py preserved them by reference (asserted by test_the_vocabularies_are_one_source_importable_from_core_and_re_exported).
- Frozen stack: no new dependency, no framework or library substitution.

## Memory suggestions

- **project / gotchas** `pytest,evidence,orch_verify` — pyproject.toml sets addopts = "-q", so passing an extra -q on the command line makes -qq and SUPPRESSES pytest's final 'N passed, M skipped' summary line (measured: a 477-item run printed only progress dots). Capture evidence with no extra -q, or with -vv when individual test names are needed, or the counts cannot be read off the log.
- **project / conventions** `memory,architecture,imports` — The memory scope vocabularies and the pure scope/role resolution live in src/orchestrator/core/memory_vocab.py, BELOW core/schemas.py, because memory_service imports schemas and so schemas cannot import the service; memory_service re-exports every name and test_the_vocabularies_are_one_source_importable_from_core_and_re_exported asserts the two modules hold the SAME objects. Put any new boundary-check vocabulary there, not in the service, or the request model cannot use it.
- **project / gotchas** `tests,assertions,review` — Asserting a rejection message by looping over the allowed VALUES is not proof: Pydantic's enum error for an unknown role lists 'architect', 'supervisor', 'worker', 'reviewer' or 'agent' and so contains every word of the ROLE_SCOPES vocabulary while naming no vocabulary at all. Pin the joined string ('architect|worker|supervisor') plus a phrase only the intended validator emits ('allowed role memory files', the 'reviewer -> architect' arrows).
- **role / worker** `tests,assertions,mutation` — One assertion per outcome, and mutation-check each new assertion by breaking the code it guards inside the same process (runtime-patch the function, do not edit a committed file). Three review rounds in a row on this project were findings about assertion SHAPE rather than code: a duplicated clause passing an `in` test, an unquoted substring claim, and `not exists or text-gone` passing whether the file was re-rendered or deleted.

## Out of scope, and what was deliberately NOT done

- reviews.19 finding 4 (the truncated diff / DoD self-check inside the review payload) is T-11. No new evidence machinery, no file added to the frozen `runs/<run_id>/` layout, no `.gitattributes`-style diff suppression (ruled out by the operator on T-12).
- reviews.19 finding 1 (verification must pass before approval) is the gate working correctly; it is satisfied by item 1, not argued with.
- Not touched: `src/orchestrator/adapters/chat/**`, `tests/unit/test_chat_drivers.py`, `run_service.py`, `review_service.py`, memory budgeting/caps (T-13), review payload archiving (T-11), timing/duration reporting (T-14), `docs/frozen-contracts.md`, the GUI.
- Role-injection semantics and which scopes each role receives: unchanged (out of scope for this task).

## Where a reviewer should look first

1. `verify-evidence/T-4/review_round4.txt` — items 1-4 measured on this tree, including the two runtime mutations that show the old disjunctive assertion proved nothing while the two split cases each fail on exactly one mutation.
2. `verify-evidence/T-4/final_rerun.txt` — the project's own `test` and `lint`, at the branch tip, with that tip's sha in line 1.
3. `verify-evidence/T-4/README.md` — the DoD-by-DoD index, every file:line re-cited to the round-4 tree, plus the R-27 finding → fix → test → log table.
4. The round-4 code change is three files: `src/orchestrator/core/memory_vocab.py` (new), `src/orchestrator/core/schemas.py` (the `MemoryCreate` before-validator), and `src/orchestrator/services/memory_service.py` (the re-export, −158 moved lines). `git diff db00112..HEAD -- src/orchestrator/core/schemas.py` is 68 lines.
