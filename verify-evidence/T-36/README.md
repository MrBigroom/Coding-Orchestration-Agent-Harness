# T-36 verification evidence

Produced from this worktree at the code HEAD stamped in each file (`0afb70b`). The evidence
commit that adds these files changes only `verify-evidence/T-36/`; the suite below ran at the
code HEAD (the commit before that evidence commit).

| file | what it proves |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` -> `748 passed, 10 skipped`, `suite_exit=0`, with the `ORCH_VERIFY|...` line proving `orchestrator.__file__` resolved inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` -> `All checks passed!`, `lint_exit=0` |
| `targeted_tests.txt` | the two touched test files with `-k` and `-vv` -> `9 passed, 20 deselected`; all nine T-36 node ids are named in the log |

## DoD -> evidence

1. Floor before remainder; a floor that cannot be funded is named
   `src/orchestrator/services/prompt_builder.py:282-297` states the floor and its arithmetic;
   `:358-541` `_allocate_diff_group` renders each file's floor first, then the remainder;
   `:515-520` names a file whose floor did not fit as `omitted entirely`, `:528-534` names a
   file that received only its floor as `floor only, remainder exhausted`.
   Pinned by `tests/unit/test_review_budget.py:274`
   `test_r68_fixture_gets_a_hunk_for_every_file_at_the_default_budget` (the named R-68 shape:
   one >43KB source file plus 29 smaller docs/test files, every one with an `@@` hunk at 12000
   tokens and no zero-byte manifest row) and `:302`
   `test_forty_files_keep_their_floor_at_the_default_budget_even_with_long_paths` (40 files,
   120-character paths, no omission). Log: both named `PASSED` in `targeted_tests.txt`.
2. Remainder still prioritised, T-16 evidence reserve intact
   `prompt_builder.py:466-497` keeps the fair-share evidence remainder and tops source up by
   priority/cap after floors; `COMPENSATION_BUDGET_FRACTION` is unchanged.
   Pinned by `tests/unit/test_review_budget.py:366`
   `test_the_floor_pass_leaves_the_t16_evidence_reserve_working` (source-heavy diff, every
   `verify-evidence/` file present and the big source still larger) and the existing T-16 tests
   at `:42`, `:63`, `:86` all pass in `full_suite.txt`.
3. Manifest truthful and additive
   The existing columns are unchanged (`prompt_builder.py:723-744` `_manifest_table`); only the
   `reason` text is more specific. Floor-only and omitted rows are asserted distinct in
   `tests/unit/test_review_budget.py:326`
   `test_a_floor_only_file_is_reported_distinctly_from_full_and_omitted`. `review_diff_incomplete`
   still fires on omission: `tests/integration/test_review_payload.py:139`
   `test_an_over_budget_diff_still_fires_review_diff_incomplete` asserts exactly one event and a
   blocking harness finding (both in `targeted_tests.txt`).
4. No default raised
   `docs/configuration.md:37` keeps `ORCHESTRATOR_MAX_DIFF_TOKENS=12000`; `core/config.py` is
   unchanged. The floor is `160` tokens, not a wider budget (`prompt_builder.py:297`).
5. Tests (a)-(f), offline/mock-only
   (a) `test_r68_...` at `tests/unit/test_review_budget.py:274` and the 40-file long-path
   test at `:302`; (b) `test_a_floor_only_...` `:326`; (c) `test_the_floor_pass_...` `:366`; (d)
   `test_budget_rendered_diff_has_no_blocking_completeness_issue` `:385`;
   (e) `test_over_budget_still_omits_a_file_and_the_auditor_still_blocks` `:408` plus the
   integration event test `tests/integration/test_review_payload.py:139`; (f)
   `test_rendering_the_same_diff_twice_is_byte_identical` `:427`.
6. Docs
   `docs/build-loop.md:566-567` and `:579-596` state the floor, the arithmetic and the two
   manifest reasons; `docs/configuration.md:37` says what the setting now controls;
   `README.md:284-290` says a floor-sized hunk may be the first hunk only or a slice and cannot
   prove the rest of the file.
7. Full suite + lint
   `full_suite.txt` (`suite_exit=0`) and `lint.txt` (`lint_exit=0`), both stamped at the code
   HEAD `0afb70b`.

## Scope / contracts

`docs/frozen-contracts.md` is untouched; no contract file is edited. `review.json`'s shape, the
run artifact layout, the truncation manifest columns and the `review_diff_incomplete` /
`review_evidence_undelivered` event names are unchanged. No new dependency.
