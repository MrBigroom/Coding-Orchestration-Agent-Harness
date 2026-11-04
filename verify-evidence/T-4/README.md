# T-4 evidence index — memory scope vocabularies

Task: **T-4 — Memory suggestions accept unvalidated scopes, creating entries no role can ever
inject.** Branch `task-4-memory-suggestions-accept-unvalidated-scopes-cre`, base `main`.

Everything here is produced by `bash verify-evidence/T-4/capture.sh` — one invocation, one
stamped HEAD, offline (throwaway `ORCHESTRATOR_HOME` per demo, in-process `TestClient`, Typer
`CliRunner`, no adapter, no provider call, no network, no API key). Each file's first line is
`# HEAD=<sha> (...) captured=<iso8601> tree=<n> dirty-entries (excluding verify-evidence/T-4)`,
so every claim below is tied to the tree it was measured on. The dirty-entry count excludes
this directory because the capture rewrites its own outputs; `git status` outside it was clean.

**Captured at HEAD `d5aa57b`** — the last commit that carries source, tests and docs. The
commits after it touch only `verify-evidence/T-4/` — check with
`git diff --name-only d5aa57b HEAD | grep -v '^verify-evidence/T-4/'` (measured: 0 lines).
`final_rerun.txt` repeats `test` + `lint` at the branch tip `56b2990`, stamped and clean, and
reports the identical 529 passed / 6 skipped / 0 failed and `All checks passed!` — so no
number in this index comes from a tree other than the one it names.

## What the R-26 review asked for, and where the proof is

| R-26 finding | Fix | Test | Log |
|---|---|---|---|
| DoD 2: role entries were only guarded on `scope`; nothing showed create / PATCH / `memory add` refusing `role='logbook-writer' scope='worker'` | `memory_service.check_role_name` (`:159`) called from `_validate` (`:340-341`) **before** the scope check, and by `cli.memory_add` (`:1070`) before `MemoryCreate` is built; `create` (`:376`) reads the role without `.value` so a raw string is refused instead of raising `AttributeError` | unit `test_check_role_name_names_the_role_vocabulary`, `test_create_refuses_an_unreadable_role_column_even_when_the_scope_is_valid`, `test_update_refuses_a_legacy_role_column_that_no_prompt_reads`, `test_import_refuses_an_unreadable_role_column_without_storing_it`, `test_role_and_scope_disagreements_land_on_the_file_the_row_is_read_from`; integration `test_api_create_refuses_an_unknown_role_name_with_the_frozen_error_shape`, `test_api_patch_refuses_a_legacy_role_column_even_with_an_in_vocabulary_scope`, `test_cli_add_refuses_a_role_name_no_prompt_can_read` | `review_round3.txt` R3-FINDING 1 (prints the `_validate`/`create` line numbers it checked, then each boundary refusing, then the accepted alias), `slice_tests_unit__role_column_boundary.txt`, `slice_memory_service__validate.txt`, `slice_memory_service__check_role_name.txt`, `slice_memory_service__create.txt`, `slice_cli__memory_add.txt` |
| DoD 3: `--gc-only` printed `removed dead scope-file` for files it had not deleted (`removed_files=removed or listed`) | `cli.memory_fix_scopes` (`:1147-1156`) builds the report from what `gc_orphan_files` actually unlinked; a `--dry-run` still previews with `orphan_files` and the verb "would" | integration `test_cli_fix_scopes_gc_only_reports_only_the_files_it_actually_deleted` (monkeypatches the collector to delete nothing, asserts the claim is absent **and** the file is still on disk) | `review_round3.txt` R3-FINDING 2 (three cases: GC-deletes-nothing, dry run, real run), `slice_cli__gc_only_branch.txt`, `slice_cli__memory_fix_scopes.txt`, `slice_tests_integration__gc_only_honesty.txt` |
| handoff summary claimed a final state that the stamps contradict | the stamps are `d5aa57b`; the handoff says which tree each count was taken on and does not call an unstamped re-run evidence | — | `full_suite.txt`, `checks.txt`, `memory_scope_tests.txt` (all stamped at `d5aa57b`) |
| `files_changed` omitted the committed handoff copies | this round's copies are named in the handoff | — | `handoff_round3.json`, `handoff_round3.md` |
| the supplied diff was truncated, so the build-log writes / repair re-render+GC / update preview / test assertions could not be inspected | `capture.sh` now emits **final source** (numbered, at this HEAD) for exactly those areas, and splits every per-file diff into 160-line parts so a reader with a 2000-line / 50KB view limit never loses a function mid-hunk | — | `slices_index.txt` (what was written and how many lines), `slice_*.txt`, `diff_*.partN.txt` |

Completeness of the split diffs is mechanically checkable — the body of the parts equals the
raw `git diff` for the file:

```bash
BASE=$(git merge-base main HEAD)
for f in $(git diff --name-only "$BASE" HEAD -- . ':(exclude)verify-evidence'); do
  n="diff_$(echo "$f" | tr '/' '_')"
  raw=$(git diff "$BASE" HEAD -- "$f" | wc -l)
  got=$(cat verify-evidence/T-4/${n}*.txt | grep -v '^# HEAD=' | grep -v '^# git diff' \
        | grep -v '^# The COMPLETE' | grep -v '^# 2000-line' | grep -v '^# line appears' \
        | grep -v '^# Raw diff' | grep -v '^DIFF_PART_COMPLETE=' | wc -l)
  printf '%-60s raw=%s body=%s %s\n' "$f" "$raw" "$got" "$([ "$raw" = "$got" ] && echo OK || echo MISMATCH)"
done
```

Measured (round 3): `README.md 22/22 OK`, `docs/decisions.md 59/59 OK`, `docs/memory.md 141/141
OK`, `api/routes/memory.py 28/28 OK`, `cli.py 205/205 OK` (2 parts), `chat_service.py 26/26 OK`,
`memory_service.py 808/808 OK` (6 parts), `run_service.py 105/105 OK`,
`tests/integration/test_memory_scope_boundary.py 471/471 OK` (3 parts),
`tests/unit/test_memory_scopes.py 743/743 OK` (5 parts).

## DoD self-check — the literal reading, per item

### DoD 1 — coerce an out-of-vocabulary project suggestion to `gotchas`, record the
substitution where the operator can see it; drop a role suggestion outside `ROLE_FILES`.

* `src/orchestrator/services/memory_service.py:214` `coerce_scope` — the whole rule is the
  fallback (`:53` `FALLBACK_SCOPE` = project→`gotchas`, global→`environment`); nothing is
  inferred from the words in the scope, because the scope decides **which roles** read the
  note. `:240` `_substitution_note` builds the operator-visible sentence.
* `:404` `suggest_from_handoff_report` returns `SuggestionOutcome(created, notes)`; a role
  suggestion that resolves to no role memory file is **not stored** and is recorded as
  `dropped suggestion …`.
* Where the operator sees it: `src/orchestrator/services/run_service.py:448`
  `_store_memory_suggestions` writes each note to the build log as
  `memory_scope_substituted` / `memory_suggestion_dropped` (additive event names; three call
  sites — `:485` salvage, `:871` `_execute`, `:1268` `_execute_direct`);
  `src/orchestrator/services/chat_service.py:554-567` emits the same notes into the chat stream.
* Tests: `tests/unit/test_memory_scopes.py`
  `test_project_suggestion_with_an_invented_scope_is_coerced_and_recorded`,
  `test_coercion_falls_back_and_never_infers_from_the_words_in_the_scope`,
  `test_a_scope_mentioning_decisions_does_not_route_the_note_to_the_architect`,
  `test_role_suggestion_outside_the_vocabulary_is_dropped_not_stored`,
  `test_coerced_suggestion_reaches_a_prompt_once_approved`,
  `test_coercion_is_written_to_the_build_log_the_operator_reads` (asserts the two build-log
  event rows themselves).
* Logs: `checks.txt` sections (1) and the `memory_scope_substituted` line of
  `test_coercion_is_written_to_the_build_log_the_operator_reads` in `memory_scope_tests.txt`;
  `review_round2.txt` FINDING 1.

### DoD 2 — reject an out-of-vocabulary scope at the API and CLI create/update boundaries,
for EVERY category, naming the allowed values; add the missing `ROLE_SCOPES`.

* Vocabularies as importable constants: `memory_service.py:35` `GLOBAL_SCOPES`,
  `:36` `PROJECT_SCOPES`, `:37` `ROLE_FILES`, `:43` **`ROLE_SCOPES`** (the constant that did
  not exist), `:45` `SCOPE_VOCABULARY`, `:67` `ROLE_MEMORY_KEY`, `:69` `ROLE_SCOPE_ALIASES`.
* One check for every category: `:194` `normalize_scope` → `:180` `_vocab_error` (names the
  allowed values), `:147` `role_memory_file` (alias resolution),
  **`:159` `check_role_name`** (the role *column*, which is what `for_prompt` selects on),
  `:323` `_validate` — which now checks the role column at `:340-341` **before** the scope, and
  `:367` `create` / `:477` `update` / `:868` `import_all` all route through it.
* Boundaries: `api/routes/memory.py:45` POST and `:81` PATCH return the frozen
  `{"error","detail"}` 400; `cli.py:1051` `memory_add` validates `--role` through
  `check_role_name` before `MemoryCreate`, so the CLI message names the role **files** and the
  aliases, not just Pydantic's enum values. Over HTTP, `MemoryCreate.role` is the `Role` enum,
  so an unknown role name is stopped in front of the service with a 422 in the same frozen
  shape (`api/main.py:149`) — asserted by
  `test_api_create_refuses_an_unknown_role_name_with_the_frozen_error_shape`.
* `update()` (`:477`) validates a throwaway preview **before** assigning, so a refused PATCH
  changes neither the row nor its markdown without depending on a rollback; a content-only edit
  of a legacy row is still possible (`check_scope=False`).
* Tests (all five boundary paths): unit `test_create_rejects_out_of_vocabulary_scope_for_every_category`
  (3 categories), `test_create_accepts_the_vocabulary` (4 valid shapes),
  `test_update_rejects_an_out_of_vocabulary_scope`,
  `test_update_rejects_without_touching_the_row_or_the_rendered_file`,
  `test_update_refuses_a_legacy_role_column_that_no_prompt_reads`,
  `test_import_refuses_an_unreadable_role_column_without_storing_it`,
  `test_check_role_name_names_the_role_vocabulary`;
  integration `test_api_create_rejects_an_uninjectable_scope_for_every_category` (3),
  `test_api_create_still_accepts_the_vocabulary_including_role_aliases`,
  `test_api_patch_rejects_an_uninjectable_scope`, `test_api_patch_refuses_a_legacy_role_column_even_with_an_in_vocabulary_scope`,
  `test_cli_add_rejects_and_names_the_allowed_values`, `test_cli_add_refuses_a_role_name_no_prompt_can_read`.
* Logs: `review_round3.txt` R3-FINDING 1 (six numbered boundaries + the counter-examples),
  `checks.txt` section (2), `review_round2.txt` FINDING 6.

### DoD 3 — a repair path that migrates rows already stored with invalid scopes, re-renders
BOTH the old and the new markdown file, prints the ids it moved, and leaves rejected rows alone.

* `memory_service.py:685` `fix_scopes` — moves rows and **never deletes one**; rejected rows go
  to `ScopeRepair.skipped` with a reason (`"rejected rows are left alone"`); a role row is
  reported, not re-homed, because inventing a role is not a repair. It re-renders `old_file`
  **and** `_file_for(entry)` (`:772-777`), so the old file cannot keep a stale
  `<!-- mem:ID -->` line, then collects dead rendered files (`:663` `gc_orphan_files`,
  `:643` `orphan_files`, `:620` `_renderer_output`, `:602` `in_layout_dir`, `:581`
  `canonical_file`) — the measured orphan was
  `memory/projects/ai-livestreaming-avatar/ai-livestreaming-avatar.md`, title line only.
  Idempotent, and `dry_run=True` writes nothing at all (including the file list).
* `cli.py:1124` `memory_fix_scopes` prints `moved #<id> <category>: '<from>' -> '<to>'`,
  the two re-rendered paths, the ids in one line (`(ids: 4, 5)`), the rows left alone **with
  their reasons**, and — this round — **only the deletions that happened**
  (`:1147-1156`); every interpolated value is Rich-`escape`d so a scope containing brackets is
  printed instead of parsed as a style tag.
* The other repair surface the DoD's "or" allows: `import_all` (`:868`) + `_import_keys`
  (`:849`) repair a legacy export's scope and de-duplicate on **both** spellings, so importing
  never manufactures a second copy of a fact that is still stored under its dead scope.
* Tests: unit `test_fix_scopes_moves_legacy_rows_and_re_renders_both_files`,
  `test_fix_scopes_leaves_rejected_rows_alone`,
  `test_fix_scopes_refuses_to_guess_a_role_and_to_delete_a_row`,
  `test_fix_scopes_reports_a_canonical_scope_that_is_still_unreachable`,
  `test_fix_scopes_dry_run_writes_nothing`, `test_gc_removes_dead_files_and_keeps_the_canonical_layout`,
  `test_gc_only_deletes_files_render_file_could_have_written`,
  `test_fix_scopes_can_move_rows_without_deleting_a_single_file`,
  `test_import_does_not_duplicate_a_row_that_is_still_awaiting_fix_scopes`,
  `test_import_repairs_a_legacy_export_instead_of_dropping_it`;
  integration `test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files`,
  `test_cli_fix_scopes_reports_lines_the_operator_can_actually_read`,
  `test_cli_fix_scopes_no_gc_moves_rows_and_deletes_nothing`,
  `test_cli_fix_scopes_gc_only_reports_only_the_files_it_actually_deleted`,
  `test_cli_import_repairs_a_legacy_export_scope`.
* Logs: `checks.txt` section (3) (files before/after, dry run, real run, second idempotent run),
  `review_round2.txt` FINDING 3 and 5, `review_round3.txt` R3-FINDING 2,
  `slice_memory_service__fix_scopes.txt`, `slice_memory_service__gc_orphan_files.txt`,
  `slice_cli__memory_fix_scopes.txt`.

### DoD 4 — `orchestrator memory list` marks any entry its own category can never reach, and
never misflags a valid role entry.

* `memory_service.py:281` `injectability` / `:263` `reachable_roles` / `:248` `role_file_key`
  — global read by every role, project filtered by `ROLE_PROJECT_SCOPES` (`:59`), role resolved
  through `_file_for`/`ROLE_MEMORY_KEY` exactly as `for_prompt` (`:790`) does it. Counter-examples
  `category=role scope=worker` (#39/#47) stay `ok`; `role='logbook-writer'` is flagged with the
  file it renders to.
* `cli.py:1027` `memory_list` (`inj` column, `:980` `_memory_table`), `:1007`
  `_print_injectability` prints the full reason under the table (a Rich column truncates it), and
  `--uninjectable` filters.
* Tests: unit `test_injectability_marks_only_the_unreachable`,
  `test_role_rows_still_render_by_the_rule_main_used`,
  `test_legacy_rows_that_cannot_be_injected_do_not_reach_any_prompt`; integration
  `test_cli_list_marks_uninjectable_entries_and_leaves_valid_role_rows_alone`,
  `test_cli_list_prints_an_uninjectable_scope_literally`.
* Logs: `checks.txt` section (4) and (5), `live_home_readonly.txt` — a **read-only** audit of the
  operator's live `ORCHESTRATOR_HOME` (`sqlite3 mode=ro`, no CLI call, no write, no fix_scopes)
  that runs this branch's rule over the real rows and names the live ghost file's contents.

### DoD 5 — offline tests for coercion, boundary rejection, the repair and the list marker;
`make test` and `make lint` both pass with no network and no API keys.

* `make test` → `./scripts/orch_verify.sh test` → **529 passed, 6 skipped, 0 failed** —
  `full_suite.txt`, `SUITE_EXIT=0`, marker
  `ORCH_VERIFY|mode=test|module=orchestrator|module_file=<this worktree>/src/orchestrator/__init__.py|worktree_root=<this worktree>`
  (the branch's own code was imported, not main's).
* `make lint` → `./scripts/orch_verify.sh lint` → **All checks passed!** — `checks.txt`,
  `LINT_EXIT=0`.
* T-4 only → `./scripts/orch_verify.sh test tests/unit/test_memory_scopes.py
  tests/integration/test_memory_scope_boundary.py -vv` → **58 passed** (41 unit + 17
  integration), `memory_scope_tests.txt`, `TARGETED_EXIT=0`, every test named individually.
* Offline: the `home` fixture (`tests/conftest.py:26-37`) gives every test an isolated
  `tmp_path` `ORCHESTRATOR_HOME`, sets `ORCHESTRATOR_ENABLE_WORKER|SCHEDULER=false` and deletes the
  six `SECRET_ENV` vars (`tests/conftest.py:14-21`); the API tests use an in-process `TestClient` (no socket),
  the CLI tests Typer's `CliRunner`; no adapter, provider or git-remote call is made. Re-run
  yourself: `bash verify-evidence/T-4/capture.sh`.

## Files in this directory

| Name | What it is |
|---|---|
| `capture.sh` | the whole capture: one HEAD, one command, staged in `/tmp` and moved in at the end |
| `full_suite.txt` | stamped `./scripts/orch_verify.sh test` — 529 passed / 6 skipped / 0 failed (at `d5aa57b`) |
| `final_rerun.txt` | the suite and lint RE-RUN at the branch tip `56b2990`, stamped: identical 529 / 6 / 0 and `All checks passed!`, `FINAL_SUITE_EXIT=0`, `FINAL_LINT_EXIT=0` |
| `checks.txt` | stamped lint + the five-part cold demo + the CLI verbatim |
| `memory_scope_tests.txt` | stamped `-vv` run of the two T-4 test files, 58 named tests |
| `live_home_readonly.txt` | read-only audit of the operator's live home (no writes, no CLI) |
| `review_round2.txt` | the R-25 findings, each demonstrated on this worktree's code |
| `review_round3.txt` | the R-26 findings, each demonstrated on this worktree's code |
| `manifest.txt` | base sha, head sha, `--stat`, sha256 + line count per changed file |
| `slices_index.txt` | which final-source slices were written, and how many lines each holds |
| `slice_<module>__<function>.txt` | the file **as it now stands**, numbered at this HEAD, for the code the review payload truncated |
| `diff_<path>.partN.txt` | the complete per-file diff vs the merge-base, cut at 160 lines so nothing is lost mid-function |
| `handoff_round2.md/.json`, `handoff_round3.md/.json` | the committed, reviewer-reachable copies of those rounds' handoffs (`.orchestrator/` is gitignored and is not archived into `runs/<run_id>/`) |

## Contracts

No frozen contract changed. `docs/frozen-contracts.md`, `core/models.py`, `core/schemas.py`,
`core/state.py`, the migrations, `for_prompt`, `render_file` and `ROLE_PROJECT_SCOPES` are
byte-identical to the merge-base `db00112` (verified with `diff <(git show db00112:src/orchestrator/services/memory_service.py | awk '/^def for_prompt/,/^# ----/') <(awk '/^def for_prompt/,/^# ----/' src/orchestrator/services/memory_service.py)` — no output), and `_file_for` differs only by an explanatory comment plus a local rename (`role` -> `name`) with the same resolution. Additive only: two build-log event names
(`memory_scope_substituted`, `memory_suggestion_dropped`), one CLI command
(`memory fix-scopes`, DoD 3-authorised) with additive options, the already-frozen
`{"error","detail"}` 400/422 bodies on existing routes. The frozen constraint for this task —
"approved entries continue to render to `$ORCHESTRATOR_HOME/memory/**.md` with one
`- <content>  <!-- mem:ID -->` line each" and "existing `memory_entries` rows are never deleted;
scope repair is idempotent" — is asserted by
`test_fix_scopes_moves_legacy_rows_and_re_renders_both_files`,
`test_fix_scopes_refuses_to_guess_a_role_and_to_delete_a_row`,
`test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files` and the idempotence half
of `test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files` ("moved 0 entries"
on the second run).
