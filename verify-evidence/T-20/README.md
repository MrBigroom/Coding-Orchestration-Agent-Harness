# T-20 evidence — parity path is synthetic and residue is swept

Code HEAD this evidence was produced at: `31bc1820cc94270ee64430216caca8be21e29083`
(the evidence commit changes only this directory; each log file stamps the HEAD it ran at).

## Commands and counts

| file | command | result |
|---|---|---|
| `full-test.txt` | `./scripts/orch_verify.sh test` | `660 passed, 9 skipped`, exit 0 |
| `parity-tests.txt` | `./scripts/orch_verify.sh test tests/integration/test_env_hermeticity.py tests/integration/test_cli.py::test_cleanup_sweeps_orphaned_parity_residue -vv` | `14 passed, 3 skipped`, exit 0 |
| `lint.txt` | `./scripts/orch_verify.sh lint` | `All checks passed!`, exit 0 |

The wrapper printed its `ORCH_VERIFY|…|worktree_root=<this worktree>` marker in
`full-test.txt` and `lint.txt` before the tool ran, so the suite verified this checkout.

## DoD mapping

1. **Never copy the real `.env`.**
   - `src/orchestrator/services/project_service.py:462` `_env_variable_names` returns names
     only; `:498` `_synthetic_env_text` emits placeholders; `:508` `_synthetic_env_file`
     writes them 0600/create-only. The doctor path calls it at
     `src/orchestrator/services/project_service.py:692`.
   - `scripts/orch_env_parity.sh:64` `parity_value`, `:76-85` synthesise from the names emitted
     by the sed expression; there is no `cp` of the source file.
   - Offline proof: `tests/integration/test_env_hermeticity.py:592`
     `test_parity_synthesis_never_writes_a_real_credential_value` intercepts every `os.write`
     and asserts the real credential value never appears in the bytes or the file.
     It passes in `parity-tests.txt`.

2. **Crash-safe / residue reclaim.**
   - `src/orchestrator/services/git_service.py:353` `sweep_parity_residue` reclaims managed
     `env-parity` snapshots and `<tmpdir>/orch-env-parity.*` shell trees, with per-destination
     locks (`:183`/`:197`) so a live run is skipped, not deleted.
   - Called from the doctor probe at `src/orchestrator/services/project_service.py:645`
     and from `orchestrator cleanup` at `src/orchestrator/cli.py:329`.
   - `scripts/orch_env_parity.sh:44` creates/holds the lock before the worktree exists;
     `:45-52` is the EXIT/INT/TERM trap, and a SIGKILL is covered by the sweep.
   - Proof: `tests/integration/test_env_hermeticity.py:479`
     `test_a_killed_shell_parity_run_leaves_no_registered_worktree` starts the real script,
     proves the live lock blocks the sweep, SIGKILLs the process group, then proves the sweep
     removes the tree and its `git worktree list` registration. Passes in `parity-tests.txt`.
   - `test_parity_sweep_reclaims_an_orphaned_shell_script_tree` (`:460`) and
     `test_env_parity_sweeps_a_leftover_worktree_from_a_killed_run` (`:328`) cover the managed
     and shell-script orphan paths; `test_parity_sweep_never_touches_a_tree_a_live_run_is_using`
     (`:439`) is the in-use assertion.

3. **Cleanup knows parity files are not worker dirt.**
   - `src/orchestrator/services/git_service.py:422` `status_split` filters untracked parity
     `.env`/`.env.*.parity` only inside a parity worktree; `:739` `remove_worktree` removes them
     explicitly before the dirty check (never on a dry-run).
   - Proof: `tests/integration/test_env_hermeticity.py:556`
     `test_recovery_commit_never_stages_parity_residue` (a `.env` produces no commit and is
     absent from a mixed commit) and `:577` `test_remove_worktree_succeeds_with_parity_residue`.
   - CLI wiring: `tests/integration/test_cli.py:84` proves `cleanup --dry-run` only reports and
     `cleanup --no-dry-run` removes the residue and the tree.

4. **Offline tests.**
   - (a) kill/residue: `test_a_killed_shell_parity_run_leaves_no_registered_worktree` (`:479`);
     (b) recovery commit: `test_recovery_commit_never_stages_parity_residue` (`:556`);
     (c) remove worktree: `test_remove_worktree_succeeds_with_parity_residue` (`:577`);
     (d) no real credential written: `test_parity_synthesis_never_writes_a_real_credential_value`
     (`:592`). All four are in `parity-tests.txt`; no test uses the network or a real key, and
     the spawned-suite tests remain opt-in and skipped here.

5. **Docs.**
   - `docs/build-loop.md` parity section says the environment is synthetic, names the sweep,
     the lock, and that residue is swept rather than assumed absent.
   - `README.md` Known limitations records the remaining SIGKILL residue risk and its sweep.
   - `tests/integration/test_env_hermeticity.py:419` pins the documentation and the absence of a
     real `cp` in the shell script.
