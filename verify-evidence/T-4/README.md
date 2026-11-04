# T-4 evidence index — memory scope vocabularies

Task: **T-4 — Memory suggestions accept unvalidated scopes, creating entries no role can ever
inject.** Branch `task-4-memory-suggestions-accept-unvalidated-scopes-cre`, base `main`.

## Round 5 (attempt 5) — current tree

Round 5 changed **tests only** (`tests/unit/test_memory_scopes.py`,
`tests/integration/test_memory_scope_boundary.py`) and re-measured on the resulting code HEAD
`abb7615`: **534 passed / 6 skipped / 0 failed**, lint `All checks passed!`, both stamped in
`final_rerun.txt` (`FINAL_SUITE_EXIT=0`, `FINAL_LINT_EXIT=0`). The committed handoff for this
round is `handoff_round5.md` / `handoff_round5.json`. `git diff --name-only fcff05d abb7615`
shows only those two test files outside this directory, so no `src/` or `docs/` file changed
between the round-4 measurement and this one. The round-4 sections below are kept as history.

Everything here is produced by `bash verify-evidence/T-4/capture.sh` — one invocation, one
stamped HEAD, offline (throwaway `ORCHESTRATOR_HOME` per demo, in-process `TestClient`, Typer
`CliRunner`, no adapter, no provider call, no network, no API key). Each file's first line is
`# HEAD=<sha> (...) captured=<iso8601> tree=<n> dirty-entries (excluding verify-evidence/T-4)`,
so every claim below is tied to the tree it was measured on. The dirty-entry count excludes
this directory because the capture rewrites its own outputs; `git status` outside it was clean.

**Captured at HEAD `fcff05d`** — the last commit that carries source, tests and docs. Every
stamped log in this directory names that tree in its first line, and **534 passed / 6 skipped /
0 failed** (`full_suite.txt`, `SUITE_EXIT=0`) with `All checks passed!` (`checks.txt`,
`LINT_EXIT=0`) is the number this index and the round-4 handoff quote — all four of
`full_suite.txt`, `checks.txt`, `memory_scope_tests.txt` and `review_round4.txt` carry
`# HEAD=fcff05d…` and `tree=0` outside this directory.

The commits after `fcff05d` can only touch `verify-evidence/T-4/` — `ruff check src tests` and
the pytest collection never read this directory — and that is checked, not asserted:
`git diff --name-only fcff05d HEAD | grep -v '^verify-evidence/T-4/'` prints nothing.
`final_rerun.txt` then repeats `test` + `lint` at the branch tip, stamped with that tip's own
sha, and reports the identical **534 / 6 / 0** and `All checks passed!`, so the number the
handoff quotes is the number the tip produces. (Round 3 lost a review point here by quoting one
HEAD's counts while its assumptions said they had not been re-run; this round states both trees
and keeps one number.)

Round 3's stamps (`d5aa57b`, `56b2990`, 529 passed) are the history of the earlier rounds and are
superseded for the current tree by `fcff05d` and `final_rerun.txt`.

## What the R-27 review and the operator's round-4 directive asked for

| Item | What landed | Test / demo | Log |
|---|---|---|---|
| **Item 1 / finding 2** — `files_changed` listed the gitignored `.orchestrator/handoff.json`, which is not on the branch | the field is now GENERATED from `git diff --name-only db00112..HEAD`, and every entry is checked to exist in `HEAD`'s tree; the committed reviewer-reachable copy lives at `verify-evidence/T-4/handoff_round4.json` | `round4_demo.py` ITEM 1 (`git ls-tree HEAD -- <path>` per entry, `git check-ignore -v .orchestrator/handoff.json`) | `review_round4.txt` ITEM 1: `75/75` paths in the tree, `no gitignored path ... :: []` |
| **Item 2 / finding 5** — no shown assertion covered the API's role-rejection MESSAGE | `MemoryCreate` runs `check_role_name` in a `model_validator(mode="before")` (`schemas.py:358-386`), so the 422 detail names the role **files** and the aliases instead of the bare enum list. `role` stays typed `Role`: the accepted set and the response shape are untouched | integration `test_api_create_refuses_an_unknown_role_with_a_message_naming_the_role_files`, `test_api_create_still_accepts_every_role_name_the_enum_allows`, `test_api_rejects_every_category_with_its_own_named_vocabulary`; unit `test_the_request_model_refuses_an_unknown_role_by_naming_the_role_files` | `review_round4.txt` ITEM 2 (a)–(d): the printed 422 body, each category's 400 text, the CLI text, and the enum-only message for contrast |
| **Item 3 / finding 6** — the disjunctive assertion at `tests/unit/test_memory_scopes.py:473` proved neither outcome, and there was no `gc=False` case | split into `test_fix_scopes_moves_legacy_rows_and_leaves_no_stale_line_in_the_old_file` (gc=False: the old file still exists, its text equals exactly `"# <slug>\n"`, the new file holds both lines in id order) and `test_fix_scopes_with_gc_on_deletes_the_dead_old_file` (gc on: `removed_files == [old file]` and it is gone). `test_fix_scopes_can_move_rows_without_deleting_a_single_file` is the `--no-gc` reporting case | both named tests above, mutation-checked | `review_round4.txt` ITEM 3: CASE A A1–A5, CASE B B1–B4, MUTATION 1 (the old disjunction PASSES while CASE A's shape FAILS), MUTATION 2 (disabling the collector fails CASE B only) |
| **Item 4 / finding 3** — the handoff contradicted itself about which tree the counts came from | one number, two stamped trees, stated in the handoff and here | — | `full_suite.txt` (stamped `fcff05d`), `final_rerun.txt` (stamped at the tip) |
| **Finding 4 — NOT ACTIONABLE**: the review payload truncated the diff and the DoD self-check | tracked as T-11 (review payload archiving). No new evidence machinery, no new file under the frozen `runs/<run_id>/` layout, no repo configuration to shrink the diff. What exists already: `slice_*.txt` final source and `diff_*.partN.txt` | — | `slices_index.txt`, `manifest.txt`, and the completeness loop below |

## What the R-26 review asked for, and where the proof is

| R-26 finding | Fix | Test | Log |
|---|---|---|---|
| DoD 2: role entries were only guarded on `scope`; nothing showed create / PATCH / `memory add` refusing `role='logbook-writer' scope='worker'` | `check_role_name` (round 4: `core/memory_vocab.py:91`, re-exported as `memory_service.check_role_name`) called from `_validate` (`memory_service.py:197`, role column at `:215`) **before** the scope check, and by `cli.memory_add` (`:1070`) before `MemoryCreate` is built, and — round 4 — by `MemoryCreate` itself (`core/schemas.py:358-386`) so the HTTP 422 says the same thing; `create` (`:241`) reads the role without `.value` so a raw string is refused instead of raising `AttributeError` | unit `test_check_role_name_names_the_role_vocabulary`, `test_create_refuses_an_unreadable_role_column_even_when_the_scope_is_valid`, `test_update_refuses_a_legacy_role_column_that_no_prompt_reads`, `test_import_refuses_an_unreadable_role_column_without_storing_it`, `test_role_and_scope_disagreements_land_on_the_file_the_row_is_read_from`; integration `test_api_create_refuses_an_unknown_role_with_a_message_naming_the_role_files (renamed in round 4)`, `test_api_patch_refuses_a_legacy_role_column_even_with_an_in_vocabulary_scope`, `test_cli_add_refuses_a_role_name_no_prompt_can_read` | `review_round3.txt` R3-FINDING 1 (prints the `_validate`/`create` line numbers it checked, then each boundary refusing, then the accepted alias), `slice_tests_unit__role_column_boundary.txt`, `slice_memory_service__validate.txt`, `slice_memory_service__check_role_name.txt`, `slice_memory_service__create.txt`, `slice_cli__memory_add.txt` |
| DoD 3: `--gc-only` printed `removed dead scope-file` for files it had not deleted (`removed_files=removed or listed`) | `cli.memory_fix_scopes` (`:1147-1156`) builds the report from what `gc_orphan_files` actually unlinked; a `--dry-run` still previews with `orphan_files` and the verb "would" | integration `test_cli_fix_scopes_gc_only_reports_only_the_files_it_actually_deleted` (monkeypatches the collector to delete nothing, asserts the claim is absent **and** the file is still on disk) | `review_round3.txt` R3-FINDING 2 (three cases: GC-deletes-nothing, dry run, real run), `slice_cli__gc_only_branch.txt`, `slice_cli__memory_fix_scopes.txt`, `slice_tests_integration__gc_only_honesty.txt` |
| handoff summary claimed a final state that the stamps contradict | every number names the tree it was measured on; round 4 stamps `fcff05d` in `full_suite.txt`, `checks.txt`, `memory_scope_tests.txt`, `review_round4.txt` and repeats `test`+`lint` at the tip in `final_rerun.txt` | — | the same five logs, plus `manifest.txt` |
| `files_changed` omitted the committed handoff copies | the committed copy is `verify-evidence/T-4/handoff_round4.json`/.md, and `files_changed` is the generated branch-diff list (round-4 item 1) | `round4_demo.py` ITEM 1 | `review_round4.txt` ITEM 1, `handoff_round3.json`, `handoff_round3.md` |
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

Measured at `fcff05d` (every one `raw == body`, 12 files, no MISMATCH): `README.md 24/24 OK`,
`docs/decisions.md 86/86 OK`, `docs/memory.md 152/152 OK`,
`src/orchestrator/api/routes/memory.py 28/28 OK`, `src/orchestrator/cli.py 205/205 OK`,
`src/orchestrator/core/memory_vocab.py 185/185 OK`, `src/orchestrator/core/schemas.py 68/68 OK`,
`src/orchestrator/services/chat_service.py 26/26 OK`,
`src/orchestrator/services/memory_service.py 701/701 OK`,
`src/orchestrator/services/run_service.py 105/105 OK`,
`tests/integration/test_memory_scope_boundary.py 545/545 OK`,
`tests/unit/test_memory_scopes.py 849/849 OK`.

## DoD self-check — the literal reading, per item

### DoD 1 — coerce an out-of-vocabulary project suggestion to `gotchas`, record the
substitution where the operator can see it; drop a role suggestion outside `ROLE_FILES`.

* `src/orchestrator/core/memory_vocab.py:156` `coerce_scope` — the whole rule is the
  fallback (`:43` `FALLBACK_SCOPE` = project→`gotchas`, global→`environment`); nothing is
  inferred from the words in the scope, because the scope decides **which roles** read the
  note. `:148` `substitution_note` builds the operator-visible sentence. (Round 4 moved these
  out of `memory_service` so `core/schemas.py` can read the same functions — see DoD 2.)
* `memory_service.py:278` `suggest_from_handoff_report` returns `SuggestionOutcome(created, notes)` (`:62`); a role
  suggestion that resolves to no role memory file is **not stored** and is recorded as
  `dropped suggestion …`.
* Where the operator sees it: `src/orchestrator/services/run_service.py:448`
  `_store_memory_suggestions` writes each note to the build log as
  `memory_scope_substituted` / `memory_suggestion_dropped` (additive event names; three call
  sites — `:565` salvage, `:1212` `_execute`, `:1327` `_execute_direct`);
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

* Vocabularies as importable named constants — `core/memory_vocab.py:25` `GLOBAL_SCOPES`,
  `:26` `PROJECT_SCOPES`, `:27` `ROLE_FILES`, **`:33` `ROLE_SCOPES`** (the constant that did not
  exist before this task), `:35` `SCOPE_VOCABULARY`, `:43` `FALLBACK_SCOPE`, `:49`
  `ROLE_PROJECT_SCOPES`, `:57` `ROLE_MEMORY_KEY`, `:59` `ROLE_SCOPE_ALIASES`, `:62`
  `MemoryError_`. They live in `core`, below `core/schemas.py`, because the API request model has
  to refuse the same shapes with the same words and cannot import the service
  (`memory_service.py:35-53` imports `schemas`). `memory_service` re-exports all of them, and
  `test_the_vocabularies_are_one_source_importable_from_core_and_re_exported` asserts the two
  modules hold the **same objects**, so a vocabulary cannot fork.
* One check for every category: `memory_vocab.py:128` `normalize_scope` → `:113` `vocab_error`
  (names the allowed values), `:79` `role_memory_file` (alias resolution), **`:91`
  `check_role_name`** (the role *column*, which is what `for_prompt` selects on),
  `memory_service.py:197` `_validate` — which checks the role column at `:215` **before** the
  scope (`:218` refuses, `:221` keeps the historic `role ← scope` copy, `:224` normalizes the
  scope) — and `:241` `create` / `:351` `update` / `:742` `import_all` all route through it.
* Boundaries, each asserted on the TEXT and not on a status code:
  `api/routes/memory.py:45` POST and `:81` PATCH return the frozen `{"error","detail"}` 400 with
  `vocab_error`'s words; `cli.py:1051` `memory_add` validates `--role` through `check_role_name`
  at `:1070` before `MemoryCreate` is built, so the CLI message names the role **files** and the
  aliases; and `core/schemas.py:358-386` — a `model_validator(mode="before")` on `MemoryCreate`
  — runs `check_role_name` **before** the `Role` enum coerces, so `POST /api/memory` with
  `role="logbook-writer"` answers 422 with `allowed role memory files:
  architect|worker|supervisor (role names agent -> worker, reviewer -> architect are accepted
  and resolve to their file)` instead of the enum list `Input should be 'architect',
  'supervisor', 'worker', 'reviewer' or 'agent'` (`api/main.py:149` keeps the body in the frozen
  shape). This is the R-27 finding 5 fix: the accepted set is UNCHANGED (five `Role` members, all
  still 201), only the rejection text changed, so the frozen request shape is untouched.
* `update()` (`:351`) validates a throwaway preview **before** assigning, so a refused PATCH
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
  `test_api_patch_rejects_an_uninjectable_scope`,
  `test_api_patch_refuses_a_legacy_role_column_even_with_an_in_vocabulary_scope`,
  `test_cli_add_rejects_and_names_the_allowed_values`, `test_cli_add_refuses_a_role_name_no_prompt_can_read`,
  and — round 4, on the message and not the code —
  `test_api_create_refuses_an_unknown_role_with_a_message_naming_the_role_files`,
  `test_api_create_still_accepts_every_role_name_the_enum_allows`,
  `test_api_rejects_every_category_with_its_own_named_vocabulary`.
* Logs: `review_round4.txt` ITEM 2 (a)–(d) prints the rejection text of all four surfaces plus
  the enum-only message for contrast, and checks the assertions themselves hold.
* Logs: `review_round3.txt` R3-FINDING 1 (six numbered boundaries + the counter-examples),
  `checks.txt` section (2), `review_round2.txt` FINDING 6.

### DoD 3 — a repair path that migrates rows already stored with invalid scopes, re-renders
BOTH the old and the new markdown file, prints the ids it moved, and leaves rejected rows alone.

* `memory_service.py:559` `fix_scopes` — moves rows and **never deletes one**; rejected rows go
  to `ScopeRepair.skipped` with a reason (`"rejected rows are left alone"`); a role row is
  reported, not re-homed, because inventing a role is not a repair. It re-renders `old_file`
  **and** `_file_for(entry)` (`:646-647`, and `:651` re-renders the old file for a row that was
  never approved), so the old file cannot keep a stale `<!-- mem:ID -->` line, then collects dead
  rendered files (`:537` `gc_orphan_files`, `:517` `orphan_files`, `:494` `_renderer_output`,
  `:476` `in_layout_dir`, `:455` `canonical_file`) — the measured orphan was
  `memory/projects/ai-livestreaming-avatar/ai-livestreaming-avatar.md`, title line only.
  Idempotent, and `dry_run=True` writes nothing at all (including the file list).
* `cli.py:1124` `memory_fix_scopes` (`:1147-1156` the gc-only honesty) prints `moved #<id> <category>: '<from>' -> '<to>'`,
  the two re-rendered paths, the ids in one line (`(ids: 4, 5)`), the rows left alone **with
  their reasons**, and — this round — **only the deletions that happened**
  ; every interpolated value is Rich-`escape`d so a scope containing brackets is
  printed instead of parsed as a style tag.
* The other repair surface the DoD's "or" allows: `import_all` (`:868`) + `_import_keys`
  (`:849`) repair a legacy export's scope and de-duplicate on **both** spellings, so importing
  never manufactures a second copy of a fact that is still stored under its dead scope.
* Tests: unit — round 4 split the one disjunctive case into two, one outcome each —
  `test_fix_scopes_moves_legacy_rows_and_leaves_no_stale_line_in_the_old_file` (`gc=False`: the
  old file is still on disk and its text is EXACTLY `"# <slug>\n"`, while the new file holds both
  lines in id order) and `test_fix_scopes_with_gc_on_deletes_the_dead_old_file` (default `gc`:
  `removed_files == [old_file]` and the file is gone), plus
  `test_fix_scopes_can_move_rows_without_deleting_a_single_file` (the `--no-gc` report still
  NAMES the dead file in `ghost_files`),
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
* Logs: `review_round4.txt` ITEM 3 — CASE A A1–A5 and CASE B B1–B4, then MUTATION 1 (keep the
  deletion, drop the re-render: the OLD `not exists or marker gone` assertion PASSES while CASE
  A's exact-text shape fails) and MUTATION 2 (disable the collector: only CASE B's assertion
  fails, and the file is proven still on disk). Mutations are runtime patches of the demo process
  (`m.render_file`, `m.gc_orphan_files`); no committed file is edited and nothing outside the
  throwaway home changes.
* Logs: `checks.txt` section (3) (files before/after, dry run, real run, second idempotent run),
  `review_round2.txt` FINDING 3 and 5, `review_round3.txt` R3-FINDING 2,
  `slice_memory_service__fix_scopes.txt`, `slice_memory_service__gc_orphan_files.txt`,
  `slice_cli__memory_fix_scopes.txt`.

### DoD 4 — `orchestrator memory list` marks any entry its own category can never reach, and
never misflags a valid role entry.

* `memory_service.py:155` `injectability` / `:137` `reachable_roles` / `:122` `role_file_key`
  / `:183` `_file_for` — global read by every role, project filtered by `ROLE_PROJECT_SCOPES`
  (`memory_vocab.py:49`), role resolved through `ROLE_MEMORY_KEY` exactly as `for_prompt`
  (`:664`) does it. Counter-examples
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

* `make test` → `./scripts/orch_verify.sh test` → **534 passed, 6 skipped, 0 failed** —
  `full_suite.txt`, `SUITE_EXIT=0`, marker
  `ORCH_VERIFY|mode=test|module=orchestrator|module_file=<this worktree>/src/orchestrator/__init__.py|worktree_root=<this worktree>`
  (the branch's own code was imported, not main's).
* `make lint` → `./scripts/orch_verify.sh lint` → **All checks passed!** — `checks.txt`,
  `LINT_EXIT=0`.
* T-4 only → `./scripts/orch_verify.sh test tests/unit/test_memory_scopes.py
  tests/integration/test_memory_scope_boundary.py -vv` → **63 passed** (44 unit + 19
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
| `round4_demo.py` | the round-4 ITEM 1-4 demo `capture.sh` runs; exits non-zero if any claim it prints stops being true |
| `review_round4.txt` | R-27 items 1-4 measured on this tree: the branch-diff file list, the rejection TEXT at every boundary, CASE A / CASE B and the two mutations |
| `full_suite.txt` | stamped `./scripts/orch_verify.sh test` — **534 passed / 6 skipped / 0 failed** (at `fcff05d`) |
| `final_rerun.txt` | the suite and lint RE-RUN at the branch tip, stamped with that tip: identical 534 / 6 / 0 and `All checks passed!`, `FINAL_SUITE_EXIT=0`, `FINAL_LINT_EXIT=0` |
| `checks.txt` | stamped lint + the five-part cold demo + the CLI verbatim |
| `memory_scope_tests.txt` | stamped `-vv` run of the two T-4 test files, 63 named tests (44 + 19) |
| `live_home_readonly.txt` | read-only audit of the operator's live home (no writes, no CLI) |
| `review_round2.txt` | the R-25 findings, each demonstrated on this worktree's code |
| `review_round3.txt` | the R-26 findings, each demonstrated on this worktree's code |
| `manifest.txt` | base sha, head sha, `--stat`, sha256 + line count per changed file |
| `slices_index.txt` | which final-source slices were written, and how many lines each holds |
| `slice_<module>__<function>.txt` | the file **as it now stands**, numbered at this HEAD, for the code the review payload truncated. Round 4 adds `slice_memory_vocab__vocabularies.txt`, `slice_memory_service__re_exports.txt`, `slice_schemas__memory_create.txt`, `slice_tests_unit__round4_text_and_split.txt`, `slice_tests_integration__round4_api_text.txt` |
| `diff_<path>.partN.txt` | the complete per-file diff vs the merge-base, cut at 160 lines so nothing is lost mid-function |
| `handoff_round2.md/.json`, `handoff_round3.md/.json`, `handoff_round4.md/.json` | the committed, reviewer-reachable copies of those rounds' handoffs (`.orchestrator/` is gitignored and is not archived into `runs/<run_id>/`) |

## Contracts

No frozen contract changed. `docs/frozen-contracts.md`, `core/models.py`,
`core/state.py`, the migrations, `for_prompt`, `render_file` and `ROLE_PROJECT_SCOPES` are
byte-identical to the merge-base `db00112`, and `core/schemas.py` differs from it ONLY by the
`MemoryCreate` before-validator of R-27 item 2 (`:358-386`) — the field keeps its `Role | None`
type, the accepted set is the same five members, and the response/error shapes are untouched, so
the frozen REST request shape is preserved and DoD 2 authorizes the boundary check itself.
Verified with `diff <(git show db00112:src/orchestrator/services/memory_service.py | awk '/^def for_prompt/,/^# ----/') <(awk '/^def for_prompt/,/^# ----/' src/orchestrator/services/memory_service.py)` — no output), and `_file_for` differs only by an explanatory comment plus a local rename (`role` -> `name`) with the same resolution. Additive only: two build-log event names
(`memory_scope_substituted`, `memory_suggestion_dropped`), one CLI command
(`memory fix-scopes`, DoD 3-authorised) with additive options, the already-frozen
`{"error","detail"}` 400/422 bodies on existing routes. The frozen constraint for this task —
"approved entries continue to render to `$ORCHESTRATOR_HOME/memory/**.md` with one
`- <content>  <!-- mem:ID -->` line each" and "existing `memory_entries` rows are never deleted;
scope repair is idempotent" — is asserted by
`test_fix_scopes_moves_legacy_rows_and_leaves_no_stale_line_in_the_old_file`,
`test_fix_scopes_with_gc_on_deletes_the_dead_old_file`,
`test_fix_scopes_refuses_to_guess_a_role_and_to_delete_a_row`,
`test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files` and the idempotence half
of `test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files` ("moved 0 entries"
on the second run).
