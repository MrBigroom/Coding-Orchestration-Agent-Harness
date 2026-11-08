# T-36 verification evidence

Each file starts with `# HEAD=<sha> entries-outside-verify-evidence/T-36=N`. The stamp excludes
`verify-evidence/T-36/` itself, so `N=0` means the suite ran against a tree with no uncommitted work
outside this directory. `HEAD` is the commit the suite ran at; the commit that adds these bytes
touches `verify-evidence/T-36/` only, so the code it certifies is that `HEAD`, not the later sha.

| file | proves |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` -> `755 passed, 10 skipped`, with the `ORCH_VERIFY|...` line showing `orchestrator.__file__` resolved inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` -> `All checks passed!` |
| `targeted_tests.txt` | all 28 tests in `tests/unit/test_review_budget.py`, `-vv`, each named `PASSED` (28 passed) |
| `own_branch_diff_supply.txt` | `render_diff_budget` run over **this branch's own** `git diff <merge-base>...HEAD` at the default budget: 12 files, `BUDGET_files_with_zero_supplied_bytes=0`, `BUDGET_files_without_a_hunk=0`, `BUDGET_files_missing_from_supplied=0`; the source group delivered 6233 of its 7800-token share (`source_leftover=1567`) and README/docs are supplied whole rather than floor-only |

To regenerate: `./scripts/orch_verify.sh test` and `./scripts/orch_verify.sh lint`, plus (for the
supply file) `env -u VIRTUAL_ENV PYTHONPATH=src <main>/.venv/bin/python -c` calling
`prompt_builder.render_diff_budget` on that `git diff`. The stamp must exclude
`verify-evidence/T-36/` so `N=0` describes the tree the suite actually ran against.

## DoD -> evidence

1. **Floor before remainder; unaffordable floor is named.** `src/orchestrator/services/prompt_builder.py:296`
   `DIFF_FILE_FLOOR_TOKENS = 160` with the arithmetic comment above it; `:335` `_fit_floor` +
   `:356` `_floor_head` render the floor and hard-cap it at 160 by `estimate_tokens`;
   `:395` `_allocate_diff_group` funds every floor before any remainder; `:701`
   `_floor_only_reason`. Tests:
   `tests/unit/test_review_budget.py:288` `test_r68_fixture_gets_a_hunk_for_every_file_at_the_default_budget`
   (the named R-68 shape), `:349` `test_forty_files_keep_their_floor_at_the_default_budget_even_with_long_paths`
   (40 files behind the 7800-token source share, asserted by the literal `40 source file(s) share 7800 tokens`),
   `:378` `test_no_floor_render_costs_more_than_the_floor_even_for_a_long_path` (every path length and a
   real mutation against the previous `max(24, ...)` renderer). All `PASSED` in `targeted_tests.txt`;
   the live branch case is `own_branch_diff_supply.txt` (`missing=0`, `without_a_hunk=0`).
2. **Remainder still prioritised, evidence reserve intact, and delivered-cost aware.**
   `prompt_builder.py:474` `_render_target` and `:493` `_raise` allocate by **delivered** tokens, not by the
   token target; a target that stops on a hunk boundary hands the difference back to the group. `:395`
   keeps the floor pass, then the T-16 fair-share evidence pass and the source cap/top-up in the existing
   priority order; `COMPENSATION_BUDGET_FRACTION` is unchanged. Tests:
   `tests/unit/test_review_budget.py:316`
   `test_remainder_moves_past_a_hunk_boundary_instead_of_being_stranded` (multi-hunk under-delivery, the
   case the old allocator stranded), `:673` `test_the_floor_pass_leaves_the_t16_evidence_reserve_working`,
   plus the T-16 tests at `:45`, `:66`, `:89`. `own_branch_diff_supply.txt` shows the reserve in force on
   the real branch (5 compensation artifacts, 5467 reserved tokens), source delivery at 6233/7800 with
   `source_leftover=1567`, and README/docs supplied whole. This is the round-6 finding 1/2 fix: the old
   allocator delivered ~4932 of the same 7800 share and left README/docs at floor.
3. **Manifest truthful and additive.** Columns unchanged (`prompt_builder.py:881` `_manifest_table`); only
   `reason` text differs, and `:626` computes `leftover` from **delivered** bytes after every pass, so a
   floor-only row cannot claim the remainder was exhausted while the group still had tokens. `:701`
   `_floor_only_reason` now derives its sentence from a comparison that is actually performed:
   *floor only, remainder exhausted* when `leftover <= 0`; *floor only, share under the floor* only when
   `per_file <= DIFF_FILE_FLOOR_TOKENS` (the share, with its cap, did not exceed the 160-token floor, so
   the leftover tokens stayed unspent instead of being allocated to this file); and *floor only, next hunk
   did not fit* when the share **was** larger than the floor, naming hunk granularity: the next hunk did
   not fit inside what remained of that share, so the leftover tokens stayed unallocated because no hunk
   could absorb them. No branch claims unallocated tokens were spent on other files. Tests:
   `tests/unit/test_review_budget.py:432`
   (`test_floor_only_reason_says_whether_the_budget_ran_out_or_the_cap_held`, one assertion per outcome),
   `:540` `test_own_branch_shape_reason_names_the_hunk_cause_and_keeps_every_floor` (the measured
   `per_file=1114`, `leftover=1769` shape), `:567`
   `test_r68_shape_reason_names_the_hunk_cause_and_keeps_every_floor` (30 files, one >43KB, `per_file=260`),
   `:588` `test_floor_only_reason_at_or_below_the_floor_keeps_the_share_claim` (the other conditional
   branch), `:613` (mutation: forcing one sentence removes the distinction), `:633`
   (`test_a_floor_only_file_is_reported_distinctly_from_full_and_omitted`). `review_diff_incomplete` still
   fires on omission: `tests/integration/test_review_payload.py:139`
   `test_an_over_budget_diff_still_fires_review_diff_incomplete` (in `full_suite.txt`).
4. **No default raised.** `src/orchestrator/core/config.py:73` and `:79` keep `60_000`/`12_000`;
   `docs/configuration.md:37` states what the number controls and that floor capacity
   `(max_diff_tokens - header_reserve) // 160` grows with it (two-thirds of that with the evidence
   reserve). The fix is the floor plus delivered-cost allocation, not a wider budget.
5. **Tests (a)-(f), offline/mock-only.** (a) `test_review_budget.py:288`, `:349`; (b) `:633`; (c) `:673`;
   (d) `:692` `test_budget_rendered_diff_has_no_blocking_completeness_issue` (fixture is >12000 tokens, and
   the test asserts the allocator actually ran by `rendered != patch` and a truncated manifest row);
   (e) `:734` `test_over_budget_still_omits_a_file_and_the_auditor_still_blocks` +
   `tests/integration/test_review_payload.py:139`; (f) `:753`
   `test_rendering_the_same_diff_twice_is_byte_identical`. The multi-hunk redistribution case is `:316`;
   the round-7 reason-shape cases are `:540`, `:567` and `:588`.
6. **Docs.** `docs/build-loop.md:566` (the three floor-only reasons), `:584` (floor guarantee), `:603`
   (arithmetic incl. floor capacity), `:613` (delivered-cost remainder); `docs/configuration.md:37`
   (what `ORCHESTRATOR_MAX_DIFF_TOKENS` controls, including floor capacity growth); `README.md:284-295`
   (what a floor-sized hunk can and cannot prove); `docs/decisions.md:405` (T-36 bullet, round-7 wording).
7. **Full suite + lint via `./scripts/orch_verify.sh`**, evidence committed here with this map:
   `full_suite.txt` (`755 passed, 10 skipped`), `lint.txt` (`All checks passed!`).

## Round-6 review findings

- Finding 1 (DoD 2) and finding 2 (DoD 3): allocation is accounted by delivered bytes
  (`_render_target`, `_raise`, `leftover = remaining`), and the multi-hunk test plus
  `own_branch_diff_supply.txt` show the freed tokens reach later files (README/docs supplied whole).
- Finding 3 (DoD 6): `docs/configuration.md:37` no longer says raising the budget cannot fit more
  files; it states the floor-capacity formula and that the remainder is allocated by delivered bytes.
- Finding 4 (minor): this README ends with a newline, the broken wrap in the `docs/build-loop.md`
  manifest bullet is fixed, and the handoff carries the decisive excerpts inline instead of pointing
  at an evidence file.

## Round-7 review finding

- **DoD 3 (blocking): the floor-only reason asserted a share comparison it never made.** The old
  `leftover > 0` branch unconditionally said the file's share (`per_file`, `cap`) "did not exceed"
  the 160-token floor and that the leftover "went to other files". That is false for the measured
  shapes (this branch: `per_file=1114`, `leftover=1769`; R-68: `per_file=260`) and self-contradictory
  for an unspent remainder. `_floor_only_reason` now branches on
  `per_file <= DIFF_FILE_FLOOR_TOKENS`: the under-floor sentence keeps the share claim only there,
  the over-floor case names hunk granularity with the real `per_file`/`cap`/`leftover` numbers, and
  no sentence says the leftover was allocated elsewhere. The code, `docs/build-loop.md`,
  `docs/decisions.md` and this map were re-read against each other after the change.
- Redistribution of the unspent remainder beyond the round-6 delivered-cost change was **not**
  attempted: the allocator is unchanged and the truthful wording explains the unspent tokens.
  `own_branch_diff_supply.txt` is regenerated at this HEAD and reports the same floor guarantee plus
  `source_leftover=1567`.

## Scope

`docs/frozen-contracts.md` untouched; no contract file edited. `review.json` shape, the run artifact
layout, the manifest columns and the `review_diff_incomplete` / `review_evidence_undelivered` event
names are unchanged. No new dependency.