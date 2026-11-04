# T-4 handoff — round 2 (reviewer feedback applied)

**Branch:** `task-4-memory-suggestions-accept-unvalidated-scopes-cre`
**Status:** CHANGES_APPLIED (READY_FOR_REVIEW again)
**Base:** `main` @ `db00112` (merge-base). Commits this round: `1fc412d` (the eight review
fixes) → `5c678f9` (the import-duplicate guard) → `95f3c78` (stamped evidence, branch tip when
`make test`/`make lint` were last run) → this final commit, whose only content is the committed
copies of this handoff.

**Summary.** All eight R-25 findings are fixed at the source, each with a test and a line in
`verify-evidence/T-4/review_round2.txt` that demonstrates the behaviour on this worktree's code:

1. **No more inference** — `memory_service.coerce_scope` (`src/orchestrator/services/memory_service.py:193`)
   is fallback-only. Out of vocabulary ⇒ `FALLBACK_SCOPE[category]`: project → `gotchas`,
   global → `environment`. The token pass and the substring pass are gone: inferring
   `decisions` from a scope that merely mentions it changed *which roles* receive the note
   (decisions → Architect+Supervisor vs gotchas → Worker+Supervisor), and a substring match
   fires inside unrelated words. Proved by
   `test_coercion_falls_back_and_never_infers_from_the_words_in_the_scope` (which feeds
   `decisions-and-gating`, `decisionss`, `conventions-notes`, `patterns-of-the-team`) and
   `test_a_scope_mentioning_decisions_does_not_route_the_note_to_the_architect`.
   `review_round2.txt` FINDING 1 shows all four landing on the fallback.
2. **Rich markup no longer eats the operator's text** — `cli.py:18` imports
   `rich.markup.escape` and every interpolated scope, category, note, reason and path in
   `_memory_table`, `_print_injectability` and `memory fix-scopes` goes through it; the
   skipped-row reason is printed after `: ` instead of inside `[...]`.
   `review_round2.txt` FINDING 2 prints both: the old bracketed f-string renders
   `left #1 project scope '' alone ` (the reason vanished, exactly what the reviewer measured),
   the new CLI renders `left #4 project scope '[stale]' alone: rejected rows are left alone`.
   Tests: `test_cli_fix_scopes_reports_lines_the_operator_can_actually_read`,
   `test_cli_list_prints_an_uninjectable_scope_literally`.
3. **`--dry-run` promises, it does not report** — verbs are `would move` / `would re-render` /
   `would collect dead scope-file` / `would remove dead scope-file`, and the tests assert that
   none of `moved #`, `re-rendered`, `removed dead scope-file`, `collected ` appear in a dry
   run. Each ghost file is named **once**: `would collect` exists only in a dry run (the file is
   still owned at that point), `removed dead scope-file` only in a real run —
   `review_round2.txt` FINDING 3 counts 1/1/1 dry and 2 removals for two distinct files.
   `--no-gc` moves rows, re-renders both files, deletes nothing and still *shows* the dead files
   (`ScopeRepair.ghost_files`).
4. **The GC predicate is visible, narrow and tested** — a file is collectable only if
   `in_layout_dir` (`:574`) AND NOT `canonical_file` (`:553`) AND unowned AND `_renderer_output`
   (`:592`: first line exactly `# <stem>`, every other non-blank line a
   `- …  <!-- mem:N -->` marker). Hand-authored markdown — which `docs/memory.md` tells
   operators to write and index with `memory import --markdown` — fails that predicate and
   survives. Proved by `test_gc_only_deletes_files_render_file_could_have_written` (notes.md with
   prose, `# scratch` + prose, an `ensure_layout` header, a title-only ghost, a stale marker
   file, a file outside the root) and `review_round2.txt` FINDING 5, which prints the four
   predicate terms per file.
5. **A refused PATCH changes nothing, without a rollback** — `update()` (`:449`) builds a
   throwaway preview and validates it **before** touching the ORM row.
   `test_update_rejects_without_touching_the_row_or_the_rendered_file` asserts the row's scope and
   content, the file bytes, and `session.dirty` / `session.new` are empty, and it deliberately
   does **not** call `session.rollback()`; `review_round2.txt` FINDING 6 shows the same.
6. **Role rendering and injection are back to main's rule** — `_file_for` (`:288`) is main's body
   verbatim (`role`, else `scope`, through `ROLE_MEMORY_KEY`), and `reachable_roles` resolves the
   same way via `role_file_key` (`:227`) and only then checks membership in `ROLE_FILES`. A legacy
   `role='logbook-writer' scope='worker'` row therefore keeps rendering to
   `roles/logbook-writer.md` and is *reported* as uninjectable — it is never re-homed into
   `worker.md`, and `fix-scopes` still refuses to invent a role. Tests:
   `test_role_rows_still_render_by_the_rule_main_used`; `review_round2.txt` FINDING 7 prints the
   file, the flag, the reason and the untouched `worker.md`. `for_prompt`, `ROLE_PROJECT_SCOPES`
   and `render_file` are untouched, so no role-injection semantics moved.
7. **Docs now say which boundary rejects and which repairs** — `docs/memory.md` gained a table:
   POST/PATCH/`memory add` **reject** (a caller is present and can fix it), a Worker suggestion
   **coerces or drops**, `memory import` (JSON), `import --markdown` and `fix-scopes` **repair**.
   `test_cli_import_repairs_a_legacy_export_scope` pins both halves in one test.
8. **Nothing is unreviewable any more** — `verify-evidence/T-4/` now carries a **complete diff per
   changed file** (`diff_*.txt`, 10 files, each ending in its own `DIFF_COMPLETE=<path>` trailer,
   with the diff line count in the header) plus `manifest.txt` (base sha, head sha, `--stat`,
   sha256 and line count per file). The R-25 truncation happened inside `memory_service.update()`;
   `diff_src_orchestrator_services_memory_service.py.txt` is
   the whole thing. This round's committed copy of the handoff is
   `verify-evidence/T-4/handoff_round2.json` / `handoff_round2.md` because `.orchestrator/` is
   gitignored and is not archived into `runs/<run_id>/` by the branch itself.

**One extra fix, found while proving finding 7.** Importing an export whose legacy row is *still*
stored under its dead scope added a second copy of the same fact under the fallback scope — and
`render_file` then wrote the line twice. `_import_keys` (`:821`) makes the duplicate check take
both spellings (stored and repaired), so `import` never duplicates and stays idempotent on the
second pass. Test: `test_import_does_not_duplicate_a_row_that_is_still_awaiting_fix_scopes`;
demo: `review_round2.txt` FINDING 8 (`imported 0` for a fact already stored, `imported 1 entry`
+ `stored as: [('gotchas', 'other', …)]` for a fact that is not).

**Tests.**
- Command: `make test` → `./scripts/orch_verify.sh test` → **520 passed, 6 skipped, 0 failed**
  (`verify-evidence/T-4/full_suite.txt`, `SUITE_EXIT=0`, marker line
  `ORCH_VERIFY|mode=test|module=orchestrator|module_file=<this worktree>/src/orchestrator/__init__.py|worktree_root=<this worktree>`).
- Command: `make lint` → `./scripts/orch_verify.sh lint` → **All checks passed!**
  (`verify-evidence/T-4/checks.txt`, `LINT_EXIT=0`).
- T-4 only: `./scripts/orch_verify.sh test tests/unit/test_memory_scopes.py
  tests/integration/test_memory_scope_boundary.py -vv` → **49 passed**
  (`verify-evidence/T-4/memory_scope_tests.txt`, `TARGETED_EXIT=0`).
- Offline: isolated `/tmp` `ORCHESTRATOR_HOME` per test, in-process `TestClient`, Typer
  `CliRunner`, `tests/conftest.py` deletes all six secret env vars; no adapter, no provider call,
  no network, no API key.

  Provenance of those numbers: `git diff --name-only 5c678f9 HEAD` lists only files under
  `verify-evidence/T-4/`. Every `src/`, `tests/` and `docs/` byte on this branch is at `5c678f9`,
  which is the tree the stamped logs tested, so the 520/6/0 and the clean lint describe the whole
  source content of the tip.

**DoD self-check.** All five items ticked, each with file:line + test + log in
`handoff.json`. Nothing in the DoD was left partially true; the two items the reviewer found broken
(fallback literalism in DoD 1, repair reporting in DoD 3) are now literally true and asserted.

**New dependencies.** None.

**Assumptions.** (a) DoD 1 names only the project fallback; global's fallback `environment` is
the symmetric constant, documented and tested. (b) DoD 4 judges a role row by the *file* it
renders into, so a legacy row with an empty `role` column reads `ok` even though `for_prompt`
selects on `role` — that asymmetry is main's, is documented in `docs/memory.md`, can no longer be
created (`_validate` fills `role` from `scope`), and is deliberately not auto-repaired because
DoD 3 covers invalid *scopes* and `MemoryUpdate` has no role field (adding one would change the
frozen PATCH/MemoryCreate shape). (c) The logs in `verify-evidence/T-4/` are stamped at HEAD `5c678f9` with
`tree=0` dirty entries outside that directory; `make test` and `make lint` were then re-run at the
branch tip `95f3c78`, on a clean tree, with the identical **520 passed / 6 skipped / 0 failed** and
`All checks passed!`.
(d) R-25's `error=none -> harness reported an error while exiting 0` is explained, not smoothed
over: `runs/25/run.json` says `exit_code=0, timed_out=False, cancelled=False, status=completed,
handoff_arrival=clean_exit` with no error; the phrase comes from `run_service.exit_state_text`
(`:390-405`, unchanged since the merge-base) whose final `else` fires for **any** zero exit, so a
clean run is mislabelled by main's wording.

**Questions.** (1) Should the `exit_state_text` catch-all become its own follow-up task? One
line, but the string is already archived inside historical `run.json` artifacts. (2) DoD 3 says
"fix-scopes, **or** repair during import"; both are implemented — name one to drop if the
reviewer wants exactly one repair surface. (3) Confirm the GUI uninjectable indicator stays a
follow-up task.

**Blockers.** None. No frozen contract needed to change: no schema/migration edit, no new route,
no renamed field, no renamed event, no changed CLI argument or exit code. Additive only — two new
build-log event names (`memory_scope_substituted`, `memory_suggestion_dropped`), one new command
(`memory fix-scopes`, DoD 3-authorised) with additive options, and the already-frozen
`{"error","detail"}` 400 body on two existing routes.

**Out of scope, untouched.** `docs/frozen-contracts.md`, `core/models.py`, `core/schemas.py`,
`core/state.py`, migrations, `for_prompt`, `render_file`, `_file_for` (reverted to main), the Web
GUI, adapters/providers, worktrees, merges, pushes.
