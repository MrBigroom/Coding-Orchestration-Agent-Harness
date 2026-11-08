# T-36 verification evidence

Each evidence file (not this README) starts with
`# HEAD=<sha> entries-outside-verify-evidence/T-36=N`. The stamp excludes
`verify-evidence/T-36/` itself, so `N=0` means the suite ran against a tree with no uncommitted work
outside this directory. `HEAD` is the commit the suite ran at; the commit that adds these bytes
touches `verify-evidence/T-36/` only, so the code it certifies is that `HEAD`, not the later sha.

| file | proves |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` -> `757 passed, 10 skipped`, with the `ORCH_VERIFY|...` line showing `orchestrator.__file__` resolved inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` -> `All checks passed!` |
| `targeted_tests.txt` | all 30 tests in `tests/unit/test_review_budget.py`, `-vv`, each named `PASSED` (30 passed) |
| `own_branch_diff_supply.txt` | `render_diff_budget` run over **this branch's own** `git diff <merge-base>...HEAD` at the default budget: 12 files, `BUDGET_files_with_zero_supplied_bytes=0`, `BUDGET_files_without_a_hunk=0`, `BUDGET_files_missing_from_supplied=0`; the source group delivered 6409 of its 7800-token share (`source_leftover=1391`) and README/docs are supplied whole rather than floor-only |

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
   case the old allocator stranded), `:756` `test_the_floor_pass_leaves_the_t16_evidence_reserve_working`,
   plus the T-16 tests at `:45`, `:66`, `:89`. `own_branch_diff_supply.txt` shows the reserve in force on
   the real branch (5 compensation artifacts, 5291 reserved tokens), source delivery at 6409/7800 with
   `source_leftover=1391`, and README/docs supplied whole. This is the round-6 finding 1/2 fix: the old
   allocator delivered ~4932 of the same 7800 share and left README/docs at floor.
3. **Manifest truthful and additive.** Columns unchanged (`prompt_builder.py:908` `_manifest_table`); only
   `reason` text differs, and `:626` computes `leftover` from **delivered** bytes after every pass, so a
   floor-only row cannot claim the remainder was exhausted while the group still had tokens. `:701`
   `_floor_only_reason` derives its sentence from a comparison that is actually performed:
   *floor only, remainder exhausted* when `leftover <= 0`; *floor only, share under the floor* only when
   the file's **effective ceiling** is at or below `DIFF_FILE_FLOOR_TOKENS` -- `max(per_file, cap)` for
   the source group, because the top-up pass may spend up to `cap` even when the fair share is smaller;
   the evidence group is uncapped after its fair-share pass (`cap == 0`) and never receives the share
   claim -- and *floor only, next hunk did not fit* when that ceiling was larger than the floor, naming
   hunk granularity: the next hunk did not fit inside what remained of the file's allowance, so the
   leftover tokens stayed unallocated because no hunk could absorb them. No branch claims unallocated
   tokens were spent on other files. Tests:
   `tests/unit/test_review_budget.py:432`
   (`test_floor_only_reason_says_whether_the_budget_ran_out_or_the_cap_held`, one assertion per outcome),
   `:555` `test_own_branch_shape_reason_names_the_hunk_cause_and_keeps_every_floor` (the measured
   `per_file=1114`, `leftover=1769` shape), `:582`
   `test_r68_shape_reason_names_the_hunk_cause_and_keeps_every_floor` (30 files, one >43KB, `per_file=260`),
   `:603` `test_default_budget_49_file_shape_emits_granularity_not_the_share_claim` (the default budget
   with 49 files: `per_file=159`, `cap=3900`, `leftover=1`; the allocator-emitted row is the granularity
   wording and never the share wording), `:640`
   `test_the_allocator_emits_the_granularity_row_with_its_own_leftover` (the row comes from
   `render_diff_budget`, not a direct helper call, with its own measured leftover), `:671`
   `test_floor_only_reason_at_or_below_the_floor_keeps_the_share_claim` (the other conditional branch),
   `:716` (`test_a_floor_only_file_is_reported_distinctly_from_full_and_omitted`).
   `review_diff_incomplete` still fires on omission: `tests/integration/test_review_payload.py:139`
   `test_an_over_budget_diff_still_fires_review_diff_incomplete` (in `full_suite.txt`).
4. **No default raised.** `src/orchestrator/core/config.py:73` and `:79` keep `60_000`/`12_000`;
   `docs/configuration.md:37` states what the number controls and that floor capacity
   `(max_diff_tokens - header_reserve) // 160` grows with it (two-thirds of that with the evidence
   reserve). The fix is the floor plus delivered-cost allocation, not a wider budget.
5. **Tests (a)-(f), offline/mock-only.** (a) `test_review_budget.py:288`, `:349`; (b) `:716`; (c) `:756`;
   (d) `:775` `test_budget_rendered_diff_has_no_blocking_completeness_issue` (fixture is >12000 tokens, and
   the test asserts the allocator actually ran by `rendered != patch` and a truncated manifest row);
   (e) `:817` `test_over_budget_still_omits_a_file_and_the_auditor_still_blocks` +
   `tests/integration/test_review_payload.py:139`; (f) `:836`
   `test_rendering_the_same_diff_twice_is_byte_identical`. The multi-hunk redistribution case is `:316`;
   the round-7 reason-shape cases are `:555`, `:582` and `:671`; the round-8 allocator-emitted
   reason-shape cases are `:603` and `:640`.
6. **Docs.** `docs/build-loop.md:568` (the three floor-only reasons and the effective ceiling),
   `:586` (floor guarantee), `:606` (arithmetic incl. floor capacity), `:616` (delivered-cost remainder);
   `docs/configuration.md:37` (what `ORCHESTRATOR_MAX_DIFF_TOKENS` controls, including floor capacity
   growth); `README.md:284-295` (what a floor-sized hunk can and cannot prove); `docs/decisions.md:405`
   (T-36 bullet, rounds 7-8 wording). The reason helper, `docs/build-loop.md`, `docs/decisions.md` and
   this map were re-read against each other after the final change.
7. **Full suite + lint via `./scripts/orch_verify.sh`**, evidence committed here with this map:
   `full_suite.txt` (`757 passed, 10 skipped`), `lint.txt` (`All checks passed!`).

## Round-6 review findings

- Finding 1 (DoD 2) and finding 2 (DoD 3): allocation is accounted by delivered bytes
  (`_render_target`, `_raise`, `leftover = remaining`), and the multi-hunk test plus
  `own_branch_diff_supply.txt` show the freed tokens reach later files (README/docs supplied whole).
- Finding 3 (DoD 6): `docs/configuration.md:37` no longer says raising the budget cannot fit more
  files; it states the floor-capacity formula and that the remainder is allocated by delivered bytes.
- Finding 4 (minor): the broken wrap in the `docs/build-loop.md` manifest bullet is fixed, and the
  handoff carries the decisive excerpts inline instead of pointing at an evidence file.

## Round-7 review finding

- **DoD 3 (blocking): the floor-only reason asserted a share comparison it never made.** The old
  `leftover > 0` branch unconditionally said the file's share (`per_file`, `cap`) "did not exceed"
  the 160-token floor and that the leftover "went to other files". That is false for the measured
  shapes (this branch: `per_file=1114`, `leftover=1769`; R-68: `per_file=260`) and self-contradictory
  for an unspent remainder. `_floor_only_reason` now branches on the share comparison: the under-floor
  sentence keeps the share claim only there, the over-floor case names hunk granularity with the real
  `per_file`/`cap`/`leftover` numbers, and no sentence says the leftover was allocated elsewhere.
- Redistribution of the unspent remainder beyond the round-6 delivered-cost change was **not**
  attempted: the allocator is unchanged and the truthful wording explains the unspent tokens.
  `own_branch_diff_supply.txt` is regenerated at this HEAD and reports the floor guarantee plus
  `source_leftover=1391`.

## Round-8 review findings

- **Finding 1 (blocking): the share branch compared `per_file`, not the effective ceiling.** At the
  default budget 49 source files give `per_file=159` but `cap=3900`, so the old condition printed
  "share (159 tokens, cap 3900) did not exceed that floor", contradicting its own numbers. The branch
  now conditions on `max(per_file, cap) <= DIFF_FILE_FLOOR_TOKENS` for the source group and never makes
  the share claim for the uncapped evidence group (`cap == 0`). The emitted granularity row names
  `per_file`, `cap`, effective ceiling, floor and the real leftover. Pinned by
  `tests/unit/test_review_budget.py:603` (49-file default-budget fixture, `leftover > 0`) and
  `:640` (allocator-emitted row).
- **Finding 2 (blocking, minor): the shape tests only called the helper directly.** `:640` drives
  `render_diff_budget` end to end over seven multi-hunk files whose next hunk exceeds the remaining
  allowance; it asserts the emitted manifest row is the granularity wording with the allocator's own
  measured `per_file=120`, `cap=210`, `leftover=5` and the 160-token floor.
- **Finding 3 (blocking, trivial): this README claimed a trailing newline the bytes did not have.**
  The file now ends with a newline, and every self-describing claim above was re-read against the
  committed bytes: the stamp sentence names the stamp-bearing evidence files rather than this README,
  and the counts, line references and supply numbers are the ones in this bundle.

## Scope

`docs/frozen-contracts.md` untouched; no contract file edited. `review.json` shape, the run artifact
layout, the manifest columns and the `review_diff_incomplete` / `review_evidence_undelivered` event
names are unchanged. The allocator itself is unchanged from round 7; this round changes only the
reason helper's condition, its tests and the documentation of the condition. No new dependency.
