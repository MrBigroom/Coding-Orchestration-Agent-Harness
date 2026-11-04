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
| `full_suite.txt` | `./scripts/orch_verify.sh test` | the whole `tests/` tree of this branch passes |
| `memory_scope_tests.txt` | `./scripts/orch_verify.sh test tests/unit/test_memory_scopes.py tests/integration/test_memory_scope_boundary.py -vv` | every new T-4 test named individually (`-vv` because `addopts = "-q"`) |
| `checks.txt` | `./scripts/orch_verify.sh lint` + a cold offline demo of coercion, rejection, repair, the marker and the CLI | lint clean, and the behaviour shown end-to-end on real files in a throwaway home |
| `live_home_before_after.txt` | read-only queries + `--dry-run` against the operator's live `ORCHESTRATOR_HOME` | the shape of the repair on the real 49 rows; **no writes** |

## Definition of Done → code and proof

1. **Project suggestions with a bad scope are coerced to `gotchas`, the substitution is
   recorded; role suggestions outside `ROLE_FILES` are dropped with a warning.**
   `memory_service.coerce_scope` (vocabulary token → substring → `FALLBACK_SCOPE`,
   `memory_service.py:190`), `suggest_from_handoff_report` returning
   `SuggestionOutcome(created, notes)` (`memory_service.py:340`), the role drop at
   `memory_service.py:369`. The notes reach the operator through
   `run_service._store_memory_suggestions` (`run_service.py:448`), which writes
   `memory_scope_substituted` / `memory_suggestion_dropped` build-log events from all three
   handoff paths (clean exit `run_service.py:1226`, salvage `run_service.py:565`, agent run
   `run_service.py:1330`), and through `chat_service.py:556` as a visible notice.
   Proof: `test_project_suggestion_with_an_invented_scope_is_coerced_and_recorded`,
   `test_global_suggestion_infers_the_scope_it_names`,
   `test_suggestions_with_no_hint_fall_back_deterministically`,
   `test_role_suggestion_outside_the_vocabulary_is_dropped_not_stored`,
   `test_coerced_suggestion_reaches_a_prompt_once_approved`,
   `test_duplicate_suggestions_are_compared_on_the_repaired_scope`,
   `test_coercion_is_written_to_the_build_log_the_operator_reads`; `checks.txt` sections (1).
2. **API and CLI create/update reject an out-of-vocabulary scope for EVERY category, naming
   the allowed values; the vocabularies are importable constants incl. the new `ROLE_SCOPES`.**
   `GLOBAL_SCOPES`/`PROJECT_SCOPES`/`ROLE_SCOPES`/`ROLE_SCOPE_ALIASES`/`SCOPE_VOCABULARY`/
   `FALLBACK_SCOPE` at `memory_service.py:34-61`; one boundary check
   `_validate` (`memory_service.py:271`) used by `create`, `update`
   (`memory_service.py:413`), `import_all` and the suggestion path; the API keeps the frozen
   error body `{"error", "detail"}` — `POST` and `PATCH` map `MemoryError_` to 400 in
   `api/routes/memory.py:45` and `:78` — and the CLI turns the same error into
   `error: …` + exit 1 via `handled` (`cli.py:1044`).
   Proof: `test_the_three_vocabularies_are_importable_named_constants`,
   `test_normalize_scope_applies_the_role_aliases`,
   `test_create_rejects_out_of_vocabulary_scope_for_every_category`,
   `test_create_accepts_the_vocabulary`, `test_update_rejects_an_out_of_vocabulary_scope`
   (unit) and `test_api_create_rejects_an_uninjectable_scope_for_every_category`,
   `test_api_create_still_accepts_the_vocabulary_including_role_aliases`,
   `test_api_patch_rejects_an_uninjectable_scope`,
   `test_cli_add_rejects_and_names_the_allowed_values` (boundary); `checks.txt` section (2).
3. **A repair path migrates rows stored with invalid scopes, re-renders BOTH files, prints the
   ids it moved, leaves rejected rows alone; orphan rendered files are collected.**
   `fix_scopes` (`memory_service.py:540`) moves rows and never deletes them, records
   `moved`/`skipped`/`removed_files`, re-renders `old_file` then the new file, and is
   idempotent; `canonical_file`/`orphan_files`/`gc_orphan_files`
   (`memory_service.py:500-538`) delete a rendered file only when no approved entry owns it and
   its name is not part of the `ensure_layout` vocabulary — the title-only
   `memory/projects/<slug>/<slug>.md` ghost from the task report; the CLI command
   `orchestrator memory fix-scopes [--dry-run] [--gc-only]` prints the moved ids
   (`cli.py:1110`); `import_all` repairs a legacy export on the way in and dedupes on the
   repaired values (`memory_service.py:727`).
   Proof: `test_fix_scopes_moves_legacy_rows_and_re_renders_both_files`,
   `test_fix_scopes_leaves_rejected_rows_alone`,
   `test_fix_scopes_refuses_to_guess_a_role_and_to_delete_a_row`,
   `test_fix_scopes_reports_a_canonical_scope_that_is_still_unreachable`,
   `test_fix_scopes_dry_run_writes_nothing`,
   `test_gc_removes_dead_files_and_keeps_the_canonical_layout`,
   `test_import_repairs_a_legacy_export_instead_of_dropping_it`,
   `test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files`; `checks.txt`
   section (3).
4. **`orchestrator memory list` marks any entry whose own category cannot reach its scope, and
   never misflags a valid role entry.** `reachable_roles` / `injectability` / `is_injectable`
   (`memory_service.py:224-256`) resolve global → `GLOBAL_SCOPES`, project →
   `ROLE_PROJECT_SCOPES`, role → `_file_for`/`ROLE_MEMORY_KEY`; the `inj` column and the reason
   lines under it are `_memory_table`/`_print_injectability` (`cli.py:979`, `cli.py:1007`),
   used by `memory list` (with `--uninjectable`) and `memory search`.
   Proof: `test_injectability_marks_only_the_unreachable` (asserts the counter-examples
   `category=role scope=worker` are **ok**, and `role=reviewer`/`agent` resolve),
   `test_legacy_rows_that_cannot_be_injected_do_not_reach_any_prompt`,
   `test_cli_list_marks_uninjectable_entries_and_leaves_valid_role_rows_alone`; `checks.txt`
   sections (4) and (5).
5. **New offline tests for coercion, rejection, repair (both files) and the marker; test and
   lint pass with no network and no API keys.** `tests/unit/test_memory_scopes.py` (28 tests)
   and `tests/integration/test_memory_scope_boundary.py` (8 tests) — DoD 5 asks for unit
   coverage of the four behaviours; the second file drives the same behaviours through the real
   HTTP and CLI boundaries, which is where DoD 2 is written. Counts and the per-test list are in
   `memory_scope_tests.txt`; the whole-tree run and `All checks passed!` are in
   `full_suite.txt` and `checks.txt`.

## What this phase did NOT do

- No model/migration change: `memory_entries` is the frozen baseline shape; the marker is
  derived, never stored.
- No new response fields on `GET /api/memory`: the marker is CLI-only, matching the task's
  "No GUI redesign; a Memory-page indicator can be a follow-up task".
- `for_prompt` injection semantics are untouched — which scopes each role receives is
  unchanged.
- docs/product-spec.md's CLI list was not edited: it is already a subset of the shipped
  surface (no `task escalate|merge|plan|archive`, no `agent`/`sync`/`desktop`), so it is not a
  live inventory. DoD 3 authorises the command name itself.
