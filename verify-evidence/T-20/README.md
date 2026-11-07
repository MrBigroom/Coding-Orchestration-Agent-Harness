# T-20 evidence (round 5): parity lock out of `.managed/`, read-only previews, shell-root grace

Code HEAD these logs ran at: see the `HEAD ... | dirty-entries ...` stamp in every file.
The evidence commit after it changes only `verify-evidence/T-20/`. The previous round's
`zz-final-source.txt` was removed: it was stamped at the superseded HEAD `135b0b9`.

## Commands and counts (all at the stamped HEAD)

| file | command | result |
|---|---|---|
| `full-test.txt` | `./scripts/orch_verify.sh test` | **673 passed, 9 skipped, 0 failed**, exit 0 |
| `zz-per-test.txt` | `./scripts/orch_verify.sh test -vv -k "parity or cleanup or env_hermeticity"` | **30 passed, 3 skipped, 649 deselected**, exit 0 |
| `lint.txt` | `./scripts/orch_verify.sh lint` | `All checks passed!`, exit 0 |

The 9 full-suite skips are the pre-existing opt-in spawned-suite tests (`ORCH_ENV_SPAWN_TESTS`);
the 3 skips in the per-test selection are from that same file. No DoD-4 test is skipped.

## Round-5 named tests (all PASSED in `zz-per-test.txt`)

- `test_parity_lock_lives_under_orchestrator_home_not_the_managed_tree` (B.1, B.4)
- `test_parity_sweep_breaks_a_stale_lock_by_rename_then_unlink` (B.3)
- `test_parity_sweep_dry_run_creates_no_lock_and_mutates_nothing` (B.2, R-49 finding 5)
- `test_parity_sweep_never_touches_a_tree_a_live_run_is_using` (live lock, dry and real)
- `test_parity_sweep_skips_an_unmarked_root_until_the_grace_window_expires` (B.5)
- `test_a_killed_shell_parity_run_leaves_no_registered_worktree` (SIGKILL residue + pid)
- `test_recovery_commit_never_stages_a_leaked_dotenv_on_a_phase_branch` (DoD 3, phase branch)
- `test_remove_worktree_succeeds_with_parity_residue` (DoD 3)
- `test_parity_synthesis_never_writes_a_real_credential_value` (DoD 1/4d)

## Round 5.1 -- operator hand-fix (R-50 findings 1, 3, 5, 6)

Commit `d7a43eb`, authored by the operator's expert-agent (chat-10) and NOT by the Worker: the
task had reached its last funded attempt (5 of 6) with one substantive concurrency defect and
three small ones left. Each fix is the one the Architect prescribed. New named tests, all PASSED
in `zz-per-test.txt`:

- `test_breaking_a_stale_lock_never_renames_a_live_lock_that_replaced_it` (finding 1). The
  interleaving, pinned: contender A flocks a stale lock whose pid is dead, contender B replaces
  the path with a live inode before A renames. `_break_stale_lock(path, fd)` must refuse on the
  `(st_dev, st_ino)` mismatch and leave B's lock byte-identical; the positive direction (the path
  still names the inode we hold -> broken by rename-then-unlink) is asserted in the same test.
- `test_a_dry_run_probe_never_blocks_a_live_parity_run` (finding 5). Inside an open dry-run probe
  of a free lock, a real `LOCK_EX | LOCK_NB` acquire must still succeed -- it raised
  `BlockingIOError` under the old code, which is how `cleanup --dry-run` could fail a live parity
  run. A live holder must still read as busy.
- `test_the_shell_lock_names_exactly_one_holder_and_claims_no_closed_window` (findings 6 and 3).
  The pid is written by truncation after `flock -n 9` succeeds and never appended, because
  `_lock_file_pid` reads the FIRST line; and the comment no longer claims the root "is never
  unowned", which contradicted the mktemp-to-flock window documented two lines below it.

Refuted with no code change: R-50 findings 2 and 4 are review-budget artifacts (the manifest for
that round shows `git_service.py` supplied at 4190 of 30622 bytes, 2 of 8 hunks). On finding 4's
question -- can residue removal delete an untracked `.env` from an ORDINARY task worktree? -- it
cannot, and the guard is in one place rather than at each call site: `_parity_residue_paths`
(`git_service.py:455`) returns `[]` unless `is_parity_worktree(path)`, so all three callers
(`recovery_commit_info` at `:959`, `_sweep_one_managed_parity` at `:606`, `remove_worktree` at
`:1199`) are scoped to the two parity destinations. The extra name-based net in
`recovery_commit_info` is gated on `parity and _is_parity_residue_name(...)` (`:973`). An
untracked `.env` in an ordinary worktree is still only excluded from STAGING by the pre-existing
sensitive-path rule and is never deleted, so untracked worker output is not discarded.

Gate re-run by the operator at `d7a43eb` inside this worktree (the `ORCH_VERIFY` marker in each
file names this worktree as the import root): `full-test.txt` 676 passed / 0 failed / 9 skipped;
`lint.txt` clean; `zz-per-test.txt` 33 passed / 3 skipped, the skips being this file's
`ORCH_ENV_SPAWN_TESTS` opt-ins. No DoD-4 test is skipped.

## Round 6 -- operator hand-fix (R-52 findings 1, 4, 5)

Commit `5f1953f`, again authored by the operator's expert-agent (chat-10) and not by the Worker.
Round 6 had been ruled handoff-only, finding 1 was against the operator's own round-5 fix, and the
Architect wrote that it "needs an operator hand-fix or a funded round" -- by then both caps had
fired (attempts 6 of 6, verified rounds 6 of 3), so there was no round left to fund.

- **Finding 1 (two holders, acquiring side).** `parity_lock()` flocked whatever fd
  `_open_lock_file` returned, so a sweeper renaming a stale lock between this process's open and
  its flock left the acquirer holding an orphaned inode while the sweeper locked a fresh one, and
  the sweep then reclaimed a snapshot in use. Round 5 guarded only the sweeper's side.
  `_acquire_lock_fd(path)` now flocks, requires `_fd_names_path(fd, path)`, retries once and
  returns None otherwise; `parity_lock` maps that to `ParityBusy`, `_parity_lock_at` to "busy".
  One implementation guards both sides. Test:
  `test_parity_lock_refuses_an_inode_swapped_between_open_and_flock` -- swaps the inode on every
  attempt, asserts exactly two acquisition attempts then a refusal, and that the replacement lock
  survives untouched.
- **Finding 4 (a comment claiming a guarantee the script did not have).** The cleanup trap is now
  installed BEFORE `exec 9>>"$LOCK"`, so a failing `exec` cannot leave an unmarked root behind.
- **Finding 5 (proof coverage).** `_fingerprint` in
  `test_parity_sweep_dry_run_creates_no_lock_and_mutates_nothing` now includes
  `<repo>/.git/worktrees`, the metadata a `git status` inside `remove_worktree(dry_run=True)`
  could refresh. The byte-identical claim is proven over it rather than assumed, and it holds.

Refuted / not defects, both belonging to T-16:

- **Finding 2** said the supplied diff shows `ANTHROPIC_API_KEY=[REDACTED]` inside asserted test
  literals, "which as shown contradicts `_synthetic_env_value`". It does not contradict it: the
  credential redactor rewrote the reviewer's copy of code whose whole point is that the values are
  placeholders (`_synthetic_env_value` returns `synthetic-not-a-secret` and friends, and
  `test_parity_synthesis_never_writes_a_real_credential_value` passes). The reviewer was shown a
  masked version of an assertion about masking, and correctly labelled it a harness problem.
- **Finding 3** could not confirm the residue line sits under the Known-limitations heading because
  `README.md` reached the reviewer at **0 of 1337 bytes** (aggregate diff budget exhausted by
  higher-priority files, even at `max_diff_tokens=24000`). Inspected directly: `## Known
  limitations` is README.md:254 and the parity residue sentence is :273-278, inside that section.
  The pinning test is `test_make_target_is_documented_as_the_developer_parity_path`, which asserts
  the exact sentences (not a bare `"synthetic" in readme`); the reviewer is right that it does not
  assert the heading, which is a fair strengthening to consider but not a defect in the docs.

Gate re-run by the operator at `5f1953f` inside this worktree: `full-test.txt` 677 passed /
0 failed / 9 skipped; `lint.txt` clean; `zz-per-test.txt` 34 passed / 3 skipped, the skips being
this file's `ORCH_ENV_SPAWN_TESTS` opt-ins.

## Budget note

The branch diff is larger than `max_diff_tokens=12000`, so this directory may not be supplied to
the reviewer. The decisive VERBATIM source and Doc excerpts are therefore inside
`.orchestrator/handoff.json` `dod_self_check[].evidence` strings (rendered into `handoff.md`), not
only here. This file is the per-test PASSED index required by the task brief.
