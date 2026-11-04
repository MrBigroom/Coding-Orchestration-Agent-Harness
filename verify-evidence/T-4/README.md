# T-4 evidence — memory suggestions accept unvalidated scopes

Branch `task-4-memory-suggestions-accept-unvalidated-scopes-cre`.

Every `.txt` here was produced by one invocation of `capture.sh` (re-run:
`bash verify-evidence/T-4/capture.sh`); each log starts with the captured `HEAD=<sha>` and a
`tree=<n> dirty-entries` stamp taken **before** any output was written, so the number is the
state of the branch the run tested. Outputs are staged in `/tmp` and moved in only after every
command ran. They are plain committed UTF-8 `.txt` (`*.log` is gitignored in this repo, and
`.orchestrator/` is invisible to the review gate, so neither can carry evidence).

The suite and lint ran through `./scripts/orch_verify.sh`, which executes **this worktree's**
code and asserts it: each log carries the
`ORCH_VERIFY|mode=…|module=orchestrator|module_file=<this worktree>/src/…|worktree_root=<this worktree>`
marker line. No network, no API keys: every test uses an isolated `ORCHESTRATOR_HOME` under
`/tmp`, the in-process `TestClient`, Typer's `CliRunner`, and the mock adapter.

| Evidence | Command | What it proves |
|---|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` | `509 passed, 6 skipped`, `SUITE_EXIT=0` over the whole `tests/` tree of this branch |
| `memory_scope_tests.txt` | `./scripts/orch_verify.sh test tests/unit/test_memory_scopes.py tests/integration/test_memory_scope_boundary.py -vv` | every new T-4 test named individually, `38 passed`, `TARGETED_EXIT=0` (`-vv` because `addopts = "-q"`) |
| `checks.txt` | `./scripts/orch_verify.sh lint` + a cold offline demo (throwaway home) of coercion, rejection, repair, the marker and the CLI | `All checks passed!` `LINT_EXIT=0`, `DEMO_EXIT=0`, and the behaviour shown end-to-end on real files |
| `live_home_readonly.txt` | `sqlite3 …?mode=ro` plus the pure `injectability()`/`canonical_file()` helpers against the operator's live home | the shape of the problem on real data; **strictly read-only, no CLI call and no repair run** (a CLI command would `_ensure_ready()` → `db.upgrade()` the live DB with branch code) |

## Definition of Done → code and proof

1. **Project suggestions with a bad scope are coerced to `gotchas`, the substitution is
   recorded; role suggestions outside `ROLE_FILES` are dropped with a warning.**
   `memory_service.coerce_scope` (vocabulary token → substring → `FALLBACK_SCOPE`,
   `src/orchestrator/services/memory_service.py:190`); `suggest_from_handoff_report` returns
   `SuggestionOutcome(created, notes)` (`:345`, dataclass at `:77`) and the role drop is
   `:379` — nothing is stored where no prompt can reach it. The notes reach the operator
   through `run_service._store_memory_suggestions`
   (`src/orchestrator/services/run_service.py:448`), which writes the build-log events
   `memory_scope_substituted` / `memory_suggestion_dropped` from all three handoff paths:
   salvage `:565`, clean exit `:1212`, agent run `:1327`; and through
   `src/orchestrator/services/chat_service.py:554` + `:566`, which says each note out loud in
   the chat stream.
   Proof: `test_project_suggestion_with_an_invented_scope_is_coerced_and_recorded`,
   `test_global_suggestion_infers_the_scope_it_names`,
   `test_suggestions_with_no_hint_fall_back_deterministically`,
   `test_role_suggestion_outside_the_vocabulary_is_dropped_not_stored`,
   `test_in_vocabulary_suggestions_are_stored_untouched_and_silent`,
   `test_coerced_suggestion_reaches_a_prompt_once_approved`,
   `test_duplicate_suggestions_are_compared_on_the_repaired_scope`,
   `test_coercion_is_written_to_the_build_log_the_operator_reads`; `checks.txt` section (1).
2. **API and CLI create/update reject an out-of-vocabulary scope for EVERY category, naming
   the allowed values; the vocabularies are importable constants including the new
   `ROLE_SCOPES`.**
   `GLOBAL_SCOPES` `:35`, `PROJECT_SCOPES` `:36`, `ROLE_FILES` `:37`, `ROLE_SCOPES` `:43`
   (the role *files* — the missing constant), `SCOPE_VOCABULARY` `:45`, `FALLBACK_SCOPE` `:53`,
   `ROLE_MEMORY_KEY` `:67`, `ROLE_SCOPE_ALIASES` `:69`. One boundary check
   `_validate` (`:271`) is used by `create` (`:312`), `update` (`:418`), `import_all` (`:712`)
   and the suggestion path, so the API and the CLI refuse the same rows for the same reason.
   The API keeps the frozen error body `{"error", "detail"}`: `POST` maps `MemoryError_` →
   400 at `src/orchestrator/api/routes/memory.py:44`, `PATCH` at `:80`; the CLI turns the same
   error into `error: …` + exit 1 through `handled` (`src/orchestrator/cli.py:60`, command at
   `:1046`). `MemoryError_` subclasses `ValueError`, so no new error contract was invented.
   The check applies to the scope the caller **supplies**: `_validate(entry,
   check_scope=data.scope is not None)` (`:271`, `:429`), so a content-only edit of a legacy
   row stays possible — refusing that would freeze out exactly the rows `fix-scopes` repairs.
   Proof: `test_the_three_vocabularies_are_importable_named_constants`,
   `test_normalize_scope_applies_the_role_aliases`,
   `test_create_rejects_out_of_vocabulary_scope_for_every_category`,
   `test_create_accepts_the_vocabulary`, `test_update_rejects_an_out_of_vocabulary_scope`,
   `test_update_of_a_legacy_row_edits_content_without_freezing_on_its_bad_scope`
   (unit) and `test_api_create_rejects_an_uninjectable_scope_for_every_category`,
   `test_api_create_still_accepts_the_vocabulary_including_role_aliases`,
   `test_api_patch_rejects_an_uninjectable_scope`,
   `test_api_patch_still_edits_the_content_of_a_legacy_row`,
   `test_cli_add_rejects_and_names_the_allowed_values` (boundary); `checks.txt` section (2).
3. **A repair path migrates rows stored with invalid scopes, re-renders BOTH files, prints the
   ids it moved, leaves rejected rows alone; orphan rendered files are collected.**
   `fix_scopes` (`:560`) moves rows and never deletes them and reports
   `moved`/`skipped`/`removed_files`; it re-renders `old_file` and then the new file, and
   `canonical_file` / `orphan_files` / `gc_orphan_files` (`:508`, `:529`, `:540`) delete a
   rendered file only when no approved entry owns it **and** its name is outside the
   `ensure_layout` vocabulary — which is exactly the title-only
   `memory/projects/<slug>/<slug>.md` ghost named in the task report (measured live:
   `'# ai-livestreaming-avatar\n'`, unowned, the only non-canonical file in the tree).
   `orchestrator memory fix-scopes [--dry-run] [--gc-only]`
   (`src/orchestrator/cli.py:1110`) prints `moved N entries (ids: …)`. `import_all` repairs a
   legacy export on the way in and dedupes on the repaired values (`:712`).
   Proof: `test_fix_scopes_moves_legacy_rows_and_re_renders_both_files`,
   `test_fix_scopes_leaves_rejected_rows_alone`,
   `test_fix_scopes_refuses_to_guess_a_role_and_to_delete_a_row`,
   `test_fix_scopes_reports_a_canonical_scope_that_is_still_unreachable`,
   `test_fix_scopes_dry_run_writes_nothing`,
   `test_gc_removes_dead_files_and_keeps_the_canonical_layout`,
   `test_import_repairs_a_legacy_export_instead_of_dropping_it`,
   `test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files`; `checks.txt`
   section (3) — moved ids `[6, 7]`, both files re-rendered, `collected files` names the ghost,
   rejected `#8` left alone, second run `moved [] … collected 0`.
4. **`orchestrator memory list` marks any entry whose own category cannot reach its scope, and
   never misflags a valid role entry.** `reachable_roles` (`:224`), `injectability` (`:242`),
   `is_injectable` (`:254`): global → `GLOBAL_SCOPES` (read by every role), project →
   `ROLE_PROJECT_SCOPES`, role → resolved through `_file_for`/`ROLE_MEMORY_KEY`. The `inj`
   column is `_memory_table` (`src/orchestrator/cli.py:979`) and the reason lines under it are
   `_print_injectability` (`:1004`), used by `memory list` (plus `--uninjectable`, `:1020`) and
   `memory search` (`:1100`).
   Proof: `test_injectability_marks_only_the_unreachable` and
   `test_legacy_rows_that_cannot_be_injected_do_not_reach_any_prompt` (unit),
   `test_cli_list_marks_uninjectable_entries_and_leaves_valid_role_rows_alone` (boundary);
   `checks.txt` section (4) shows `scope='worker'` with and without a `role`, plus
   `reviewer`/`agent`/`supervisor`, all `ok`, and only the invented project scope `NO`;
   `live_home_readonly.txt` shows all 9 live `category=role scope=worker` rows are **not**
   flagged.
5. **New offline tests for coercion, rejection, repair (both files) and the marker; test and
   lint pass with no network and no API keys.**
   `tests/unit/test_memory_scopes.py` (29 tests) and
   `tests/integration/test_memory_scope_boundary.py` (9 tests) — DoD 5 asks for offline unit
   coverage of the four behaviours; the second file drives the same behaviours through the real
   HTTP and CLI boundaries, which is where DoD 2 is written. Counts and the per-test list are
   in `memory_scope_tests.txt` (`38 passed`); the whole-tree run (`509 passed, 6 skipped`) is
   `full_suite.txt`; `All checks passed!` is in `checks.txt`.

## What this phase did NOT do

- No model or migration change: `memory_entries` keeps the frozen baseline shape; the
  injectability marker is derived at read time, never stored.
- No new fields on `GET /api/memory` and no new route: the marker is CLI-only, matching the
  task's "No GUI redesign; a Memory-page indicator can be a follow-up task".
- `for_prompt` semantics are untouched — which scopes each role receives did not change.
- The live `ORCHESTRATOR_HOME` was audited read-only. Running `orchestrator memory
  fix-scopes` against it is the operator's call, not a Worker's: this branch is not merged,
  and the command in a linked worktree would run the branch's code against the live DB.
- `docs/product-spec.md`'s CLI list was not edited: it is already a subset of the shipped
  surface (no `task escalate|merge|plan|archive`, no `agent`/`sync`/`desktop`), so it is not a
  live inventory; DoD 3 authorises the command name itself.
