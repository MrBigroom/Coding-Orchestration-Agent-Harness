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

## Files

| Evidence | Command | What it proves |
|---|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` | `520 passed, 6 skipped`, `SUITE_EXIT=0` over the whole `tests/` tree of this branch |
| `memory_scope_tests.txt` | `./scripts/orch_verify.sh test tests/unit/test_memory_scopes.py tests/integration/test_memory_scope_boundary.py -vv` | all **49** T-4 tests named individually, `49 passed`, `TARGETED_EXIT=0` (`-vv` because `addopts = "-q"`) |
| `checks.txt` | `./scripts/orch_verify.sh lint` + a cold offline demo (throwaway home) of coercion, rejection, repair, the marker and the CLI | `All checks passed!` `LINT_EXIT=0`, `DEMO_EXIT=0`, behaviour shown end-to-end on real files |
| `review_round2.txt` | cold offline demo of the **review round** fixes, section per finding | `ROUND2_EXIT=0`; every R-25 finding demonstrated fixed on this worktree's code (what Rich printed before vs after, dry-run wording, the four-part GC predicate, the refused PATCH, the untouched role rendering, reject-vs-repair at the CLI) |
| `manifest.txt` | `git diff --stat/-name-only db00112..HEAD` + `sha256sum` | the complete change inventory with the base and head sha of each file, excluding `verify-evidence/` |
| `diff_*.txt` (10) | `git diff db00112 HEAD -- <one file>` | **each changed file as its own complete diff**, with a `DIFF_COMPLETE=<path>` trailer, because the combined diff supplied last round was truncated inside `memory_service.update()` |
| `live_home_readonly.txt` | `sqlite3 …?mode=ro` plus the pure `injectability()`/`canonical_file()` helpers against the operator's live home | the shape of the problem on real data; **strictly read-only, no CLI call and no repair run** (a CLI command would `_ensure_ready()` → `db.upgrade()` the live DB with branch code) |

## Definition of Done → code and proof

Line numbers are at HEAD `5c678f9` (see `manifest.txt`).

1. **Project suggestions with a bad scope are coerced to the deterministic fallback `gotchas`,
   the substitution is recorded; role suggestions outside `ROLE_FILES` are dropped with a
   warning.**
   `memory_service.coerce_scope` (`src/orchestrator/services/memory_service.py:193`) is
   fallback-only: no token match, no substring match, nothing inferred from the words
   (`FALLBACK_SCOPE` `:53` — project → `gotchas`, global → `environment`; `role` has no
   fallback and re-raises). `suggest_from_handoff_report` (`:376`, dataclass
   `SuggestionOutcome` `:77`) returns `created` + `notes`, and the role drop is inside it.
   The notes reach the operator through `run_service._store_memory_suggestions`
   (`src/orchestrator/services/run_service.py:448`), which writes the build-log events
   `memory_scope_substituted` / `memory_suggestion_dropped` from all three handoff paths
   (salvage `:565`, clean exit `:1212`, agent run `:1327`), and through `chat_service`
   (`:554`, `:567`), which says
   each note out loud in the chat stream.
   Proof: `test_project_suggestion_with_an_invented_scope_is_coerced_and_recorded`,
   `test_coercion_falls_back_and_never_infers_from_the_words_in_the_scope`,
   `test_a_scope_mentioning_decisions_does_not_route_the_note_to_the_architect`,
   `test_global_suggestion_with_a_scope_that_hints_a_vocabulary_word_falls_back`,
   `test_suggestions_with_no_hint_fall_back_deterministically`,
   `test_role_suggestion_outside_the_vocabulary_is_dropped_not_stored`,
   `test_in_vocabulary_suggestions_are_stored_untouched_and_silent`,
   `test_coerced_suggestion_reaches_a_prompt_once_approved`,
   `test_duplicate_suggestions_are_compared_on_the_repaired_scope`,
   `test_coercion_is_written_to_the_build_log_the_operator_reads`; `checks.txt` section (1),
   `review_round2.txt` FINDING 1.
2. **API and CLI create/update reject an out-of-vocabulary scope for EVERY category, naming
   the allowed values; the three vocabularies are importable constants including the new
   `ROLE_SCOPES`.**
   `GLOBAL_SCOPES` `:35`, `PROJECT_SCOPES` `:36`, `ROLE_FILES` `:37`, `ROLE_SCOPES` `:43`
   (the role *files* — the constant that did not exist), `SCOPE_VOCABULARY` `:45`,
   `FALLBACK_SCOPE` `:53`, `ROLE_MEMORY_KEY` `:67`, `ROLE_SCOPE_ALIASES` `:69`,
   `role_memory_file` `:147`, `normalize_scope` `:173`. One boundary check `_validate`
   (`:302`) is used by `create` (`:343`), `update` (`:449`), `import_all` (`:840`) and the
   suggestion path, so the API and the CLI refuse the same rows for the same reason.
   `POST` maps `MemoryError_` → 400 at `src/orchestrator/api/routes/memory.py:45`, `PATCH` at
   `:81`; the CLI turns the same error into `error: …` + exit 1 through `handled`
   (`src/orchestrator/cli.py:60`, `memory add` at `:1051`). `MemoryError_` subclasses
   `ValueError`, so no new error contract was invented.
   `update()` (`:449`) builds a throwaway preview and validates it **before** assigning
   anything to the ORM row, so a refused PATCH leaves the row and its markdown unchanged
   without relying on a rollback (proved without `session.rollback()` in
   `test_update_rejects_without_touching_the_row_or_the_rendered_file`). The check applies to
   the scope the caller **supplies** (`check_scope=data.scope is not None`), so a content-only
   edit of a legacy row stays possible.
   Proof: `test_the_three_vocabularies_are_importable_named_constants`,
   `test_normalize_scope_applies_the_role_aliases`,
   `test_create_rejects_out_of_vocabulary_scope_for_every_category`,
   `test_create_accepts_the_vocabulary`, `test_update_rejects_an_out_of_vocabulary_scope`,
   `test_update_rejects_without_touching_the_row_or_the_rendered_file`,
   `test_update_of_a_legacy_row_edits_content_without_freezing_on_its_bad_scope` (unit);
   `test_api_create_rejects_an_uninjectable_scope_for_every_category`,
   `test_api_create_still_accepts_the_vocabulary_including_role_aliases`,
   `test_api_patch_rejects_an_uninjectable_scope`,
   `test_api_patch_still_edits_the_content_of_a_legacy_row`,
   `test_cli_add_rejects_and_names_the_allowed_values` (boundary); `checks.txt` section (2),
   `review_round2.txt` FINDING 6.
3. **A repair path migrates rows stored with invalid scopes, re-renders BOTH files, prints the
   ids it moved, leaves rejected rows alone; rendered ghost files are collected.**
   `fix_scopes` (`:657`, result dataclass `ScopeRepair` `:95`) moves rows and never deletes
   them, reporting `moved` / `skipped` / `removed_files` / `ghost_files`; it re-renders the
   old file then the new one. The collector is a four-part, visible predicate:
   `in_layout_dir` (`:574`) AND NOT `canonical_file` (`:553`) AND unowned AND
   `_renderer_output` (`:592` — first line exactly `# <stem>`, later lines only blank or
   `- …  <!-- mem:N -->` markers), so hand-authored markdown that `memory import --markdown`
   indexes is never deleted; `orphan_files` (`:615`) / `gc_orphan_files` (`:635`).
   `orchestrator memory fix-scopes [--dry-run] [--no-gc] [--gc-only]`
   (`src/orchestrator/cli.py:1117`) prints `moved N entries (ids: …)`, escapes every
   interpolated value (`from rich.markup import escape`, `:18`), and in a dry run uses
   `would move` / `would re-render` / `would collect` / `would remove` only.
   `import_all` repairs a legacy export's scope on the way in and de-dupes on **both**
   spellings (`_import_keys` `:821`), so it never adds a second copy of a fact that is already
   stored under a dead scope.
   Proof: `test_fix_scopes_moves_legacy_rows_and_re_renders_both_files`,
   `test_fix_scopes_leaves_rejected_rows_alone`,
   `test_fix_scopes_refuses_to_guess_a_role_and_to_delete_a_row`,
   `test_fix_scopes_reports_a_canonical_scope_that_is_still_unreachable`,
   `test_fix_scopes_dry_run_writes_nothing`,
   `test_gc_removes_dead_files_and_keeps_the_canonical_layout`,
   `test_gc_only_deletes_files_render_file_could_have_written`,
   `test_fix_scopes_can_move_rows_without_deleting_a_single_file`,
   `test_import_does_not_duplicate_a_row_that_is_still_awaiting_fix_scopes`,
   `test_import_repairs_a_legacy_export_instead_of_dropping_it` (unit);
   `test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files`,
   `test_cli_fix_scopes_reports_lines_the_operator_can_actually_read`,
   `test_cli_fix_scopes_no_gc_moves_rows_and_deletes_nothing`,
   `test_cli_import_repairs_a_legacy_export_scope` (boundary); `checks.txt` section (3),
   `review_round2.txt` FINDINGS 2, 3, 5, 8.
4. **`orchestrator memory list` marks any entry whose own category cannot reach its scope, and
   never misflags a valid role entry.** `reachable_roles` (`:242`), `injectability` (`:260`),
   `is_injectable`, `role_file_key` (`:227`): global → `GLOBAL_SCOPES` (read by every role),
   project → `ROLE_PROJECT_SCOPES`, role → resolved through `_file_for`/`ROLE_MEMORY_KEY`
   exactly as on main (`role`, else `scope`, then `ROLE_MEMORY_KEY`) — T-4 changed neither the
   rendering (`_file_for` `:288` is main's body) nor `for_prompt` (`:762`). The `inj` column is
   `_memory_table` (`src/orchestrator/cli.py:980`), the reason lines under it are
   `_print_injectability` (`:1007`), used by `memory list` (plus `--uninjectable`, `:1032`) and
   `memory search` (`:1105`).
   Proof: `test_injectability_marks_only_the_unreachable`,
   `test_role_rows_still_render_by_the_rule_main_used`,
   `test_legacy_rows_that_cannot_be_injected_do_not_reach_any_prompt` (unit),
   `test_cli_list_marks_uninjectable_entries_and_leaves_valid_role_rows_alone`,
   `test_cli_list_prints_an_uninjectable_scope_literally` (boundary); `checks.txt` section (4)
   shows `scope='worker'` with and without a `role`, plus `reviewer`/`agent`/`supervisor`, all
   `ok`, and only the invented project scope `NO`; `live_home_readonly.txt` shows all 9 live
   `category=role scope=worker` rows are **not** flagged; `review_round2.txt` FINDING 7 shows a
   legacy `role='logbook-writer'` row still rendering to `roles/logbook-writer.md` and being
   reported instead of re-homed.
5. **New offline tests for coercion, rejection, repair (both files) and the marker; test and
   lint pass with no network and no API keys.**
   `tests/unit/test_memory_scopes.py` (31 test functions) and
   `tests/integration/test_memory_scope_boundary.py` (11 test functions) — 49 collected cases
   counting parametrization. DoD 5 asks for offline unit coverage of the four behaviours; the
   second file drives the same behaviours through the real HTTP and CLI boundaries, which is
   where DoD 2 is written. Per-test names and `49 passed` are in `memory_scope_tests.txt`; the
   whole-tree `520 passed, 6 skipped` is `full_suite.txt`; `All checks passed!` is in
   `checks.txt`.

## Review round (R-25 → this round), finding → change → proof

| # | Finding | Change | Proof on disk |
|---|---|---|---|
| 1 | inference by token then substring before the fallback | `coerce_scope` is fallback-only (`memory_service.py:193`); the note says "deterministic fallback, not inferred from the words" | `test_coercion_falls_back_and_never_infers_from_the_words_in_the_scope`, `test_a_scope_mentioning_decisions_does_not_route_the_note_to_the_architect`; `review_round2.txt` FINDING 1 |
| 2 | `[{why}]` parsed as Rich markup and eaten | `from rich.markup import escape` (`cli.py:18`); every interpolated scope, note, reason, category and path in `_memory_table`, `_print_injectability` and `memory fix-scopes` is escaped, and the reason is printed after `: ` instead of in brackets | `test_cli_fix_scopes_reports_lines_the_operator_can_actually_read`, `test_cli_list_prints_an_uninjectable_scope_literally`; `review_round2.txt` FINDING 2 shows the old line printing `scope '' alone` and the new one printing `scope '[stale]' alone: rejected rows are left alone` |
| 3 | `--dry-run` claimed writes; each ghost named twice | verbs switch to `would re-render` / `would collect` / `would remove`; a collected ghost is printed once (the `would collect` line exists only in a dry run, the `removed dead scope-file` line only in a real run) | `test_cli_fix_scopes_reports_lines_the_operator_can_actually_read` (asserts no `moved #`, no `re-rendered`, no `removed dead scope-file`, no `collected ` in a dry run, and `count("removed dead scope-file") == 2` for two distinct ghosts); `review_round2.txt` FINDING 3 |
| 4 | supplied diff truncated, most of the branch unreviewable | per-file complete diffs with a `DIFF_COMPLETE=` trailer plus a `sha256` manifest | `verify-evidence/T-4/diff_*.txt` (10 files, one per changed file), `manifest.txt` |
| 5 | GC could delete hand-authored markdown; predicate invisible and untested | four-part predicate `in_layout_dir AND not canonical_file AND not owned AND _renderer_output`, documented in `docs/memory.md` and in the docstrings; `--no-gc` added | `test_gc_only_deletes_files_render_file_could_have_written`, `test_fix_scopes_can_move_rows_without_deleting_a_single_file`, `test_cli_fix_scopes_no_gc_moves_rows_and_deletes_nothing`; `review_round2.txt` FINDING 5 |
| 6 | `update()` assigned before validating | preview-then-assign; the test asserts the row, the file and `session.dirty`/`session.new` after the refusal, with no rollback | `test_update_rejects_without_touching_the_row_or_the_rendered_file`; `review_round2.txt` FINDING 6 |
| 7 | role rendering/injection semantics changed, not declared | `_file_for` reverted to main's body (`:288`) and `reachable_roles` resolves through the same `role`→`scope`→`ROLE_MEMORY_KEY` rule (`role_file_key` `:227`); an unreachable role row is *reported*, never re-homed; `docs/memory.md` documents the legacy `role`-column gap and `fix-scopes` still refuses to invent a role | `test_role_rows_still_render_by_the_rule_main_used`; `review_round2.txt` FINDING 7 |
| 8 | `docs/memory.md` listed `import` as a rejecting boundary | a table now separates **rejecting** (create, PATCH, `memory add`) from **repairing** (`memory import`, `--markdown`, `fix-scopes`) and **coercing/dropping** (a Worker suggestion) | `docs/memory.md` "Categories and scopes" + "Auditing and repairing scopes"; `test_cli_import_repairs_a_legacy_export_scope` |
| 9 | R-25 provenance line `error=none -> harness reported an error while exiting 0` | not this branch's code: `run_service.exit_state_text` (`:390`, unchanged since the merge-base `db00112`) has a catch-all final `else` that fires for **any** `exit_code == 0`, so a clean run is mislabelled. `runs/25/run.json` records `exit_code=0`, `error=none`, `status=completed`, `handoff_arrival=clean_exit` | quoted in the handoff; proposed follow-up, deliberately not fixed here (out of T-4 scope, and the string is part of an archived artifact shape) |

## What this phase did NOT do

- No model or migration change: `memory_entries` keeps the frozen baseline shape; the
  injectability marker is derived at read time, never stored.
- No new fields on `GET /api/memory` and no new route: the marker is CLI-only, matching the
  task's "No GUI redesign; a Memory-page indicator can be a follow-up task".
- No change to `for_prompt`, `ROLE_PROJECT_SCOPES`, `_file_for` or `render_file`: which scopes
  each role receives, and where a row is rendered, is exactly as on main.
- The live `ORCHESTRATOR_HOME` was audited read-only. Running `orchestrator memory fix-scopes`
  against it is the operator's call, not a Worker's: this branch is not merged, and the
  command in a linked worktree would run the branch's code against the live DB.
- `run_service.exit_state_text`'s catch-all wording (finding 9) was left alone: it is pre-existing
  on the merge-base and outside this task's scope.
- `docs/product-spec.md`'s CLI list was not edited: it is already a subset of the shipped
  surface (no `task escalate|merge|plan|archive`, no `agent`/`sync`/`desktop`), so it is not a
  live inventory; DoD 3 authorises the `memory fix-scopes` command name itself.
