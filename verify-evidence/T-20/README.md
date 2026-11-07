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

## Budget note

The branch diff is larger than `max_diff_tokens=12000`, so this directory may not be supplied to
the reviewer. The decisive VERBATIM source and Doc excerpts are therefore inside
`.orchestrator/handoff.json` `dod_self_check[].evidence` strings (rendered into `handoff.md`), not
only here. This file is the per-test PASSED index required by the task brief.
