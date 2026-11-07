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

## Budget note

The branch diff is larger than `max_diff_tokens=12000`, so this directory may not be supplied to
the reviewer. The decisive VERBATIM source and Doc excerpts are therefore inside
`.orchestrator/handoff.json` `dod_self_check[].evidence` strings (rendered into `handoff.md`), not
only here. This file is the per-test PASSED index required by the task brief.
