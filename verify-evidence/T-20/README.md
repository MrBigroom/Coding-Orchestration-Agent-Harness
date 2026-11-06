# T-20 evidence — parity env is synthetic, and parity residue is swept not left behind

Code HEAD the evidence was produced at: `49242d18b2ef7e47af0bf0871714fd92a3f0295f`
(each log file stamps the HEAD it ran at; this evidence commit changes only this directory).
Round 2 of review: findings 1-9 are addressed in `49242d1`, which is the HEAD every log below ran
at. The first-round mapping is kept where it is still the same file:line claim.

## Commands and counts

| file | command | result |
|---|---|---|
| `full-test.txt` | `./scripts/orch_verify.sh test` | `664 passed, 9 skipped`, exit 0 |
| `zz-per-test.txt` | `./scripts/orch_verify.sh test -vv -k "<the four DoD-4 tests>"` and the round-2 tests | `5 passed, 668 deselected` / `9 passed, 664 deselected`, exit 0 |
| `lint.txt` | `./scripts/orch_verify.sh lint` | `All checks passed!`, exit 0 |

`zz-per-test.txt` exists because the first round could not show the four DoD-named tests
individually inside the review's diff budget: it names each one with `PASSED` at the stamped HEAD,
so none of them is one of the 9 skips. It sorts last so it survives a mid-diff truncation.

## DoD mapping

1. **Never copy the real `.env`.**
   - `src/orchestrator/services/project_service.py:462` `_env_variable_names` returns names only;
     `:503` `_synthetic_env_text` emits placeholders; `:513` `_synthetic_env_file` writes them
     0600/create-only. The doctor path calls it at `:710`.
   - `scripts/orch_env_parity.sh:70` `parity_value`, `:85` synthesise from the names emitted by
     the sed expression; there is no `cp` of the source file.
   - Offline proof: `tests/integration/test_env_hermeticity.py:744`
     `test_parity_synthesis_never_writes_a_real_credential_value` intercepts every `os.write` and
     asserts the real credential value never appears in the bytes or the file.

2. **Crash-safe / residue reclaim.**
   - `src/orchestrator/services/git_service.py:451` `sweep_parity_residue` reclaims managed
     `env-parity` snapshots (`:322`) and `<tmpdir>/orch-env-parity.*` shell roots (`:408`).
   - Round-2 findings 1/2 (the deletion surface): `:385` `_untrusted_parity_root` `lstat`s the
     root, so a symlinked `orch-env-parity.*` is refused instead of followed, and a foreign uid or
     group/world-writable root is refused; a symlinked `root/tree` is unlinked, never `rmtree`d
     through (`:427`). `:297` `_registered_in` runs `git worktree remove` only for a LINKED
     worktree git lists, never a main checkout. `.repo` (`:368`) scopes the shared-tempdir sweep to
     the asking checkout, and every `OSError` becomes a reported `skip` line.
   - Proofs: `tests/integration/test_env_hermeticity.py:500`
     `test_parity_sweep_refuses_untrusted_temp_roots` (symlink + world-writable root survive),
     `:526` `test_parity_sweep_does_not_remove_an_unregistered_tree` (no `worktree remove` git
     call), `:551` `test_parity_sweep_is_project_scoped_and_reports_unopenable_roots` (another
     repo's live lock does not fail this project; an unopenable root is reported and the probe
     still answers).
   - Live-run protection: `:451` `test_parity_sweep_never_touches_a_tree_a_live_run_is_using`.
   - SIGKILL: `:612` `test_a_killed_shell_parity_run_leaves_no_registered_worktree` starts the real
     script, proves the live lock blocks the sweep, SIGKILLs the process group, then proves the
     sweep removes the tree and its `git worktree list` registration. It also asserts the synthetic
     `.env` is 0600 at creation (finding 6) and that `root/.repo` names the checkout.
   - Round-2 finding 7: `:447` reports `would remove orphaned parity root ...` in a dry run and
     `removed orphaned parity root ...` in a real run for the same tree-less root (asserted in
     `:472` `test_parity_sweep_reclaims_an_orphaned_shell_script_tree`).
   - `scripts/orch_env_parity.sh:42-46` creates `.lock` and `.repo` before the worktree; `:57-59`
     is the EXIT/INT/TERM trap, and a SIGKILL is covered by the sweep.

3. **Cleanup knows parity files are not worker dirt.**
   - `src/orchestrator/services/git_service.py:540` `status_split` filters untracked parity
     `.env`/`.env.*.parity` only inside a parity worktree; `:631` `recovery_commit_info` REMOVES the
     residue before staging (`:663`) and excludes it from the staged set independently of
     `is_sensitive_path` (`:672`); `remove_worktree` removes it before the dirty check.
   - Proofs: `:692` `test_recovery_commit_never_stages_parity_residue` (round-2 finding 3 adds the
     control that git really sees `?? .env` and `?? .env.999-dead.parity`, so the assertion cannot
     pass because the file is invisible, plus the `.env.*.parity` case) and `:729`
     `test_remove_worktree_succeeds_with_parity_residue`.
   - CLI wiring: `tests/integration/test_cli.py:84` proves `cleanup --dry-run` only reports and
     `cleanup --no-dry-run` removes the residue and the tree.

4. **Offline tests.**
   - (a) kill/residue: `test_a_killed_shell_parity_run_leaves_no_registered_worktree` (`:612`);
     (b) recovery commit: `test_recovery_commit_never_stages_parity_residue` (`:692`);
     (c) remove worktree: `test_remove_worktree_succeeds_with_parity_residue` (`:729`);
     (d) no real credential written: `test_parity_synthesis_never_writes_a_real_credential_value`
     (`:744`). All four are named with `PASSED` in `zz-per-test.txt`, at the stamped HEAD; no test
     uses the network or a real key, and the spawned-suite tests remain opt-in and skipped here.

5. **Docs.**
   - `docs/build-loop.md` parity section: the environment is synthetic, the sweep is lock-aware and
     scoped, a symlink/foreign root is refused, and residue is swept rather than assumed absent
     ("residue but not a secret").
   - `README.md:273-276` Known limitations records the remaining SIGKILL residue risk and its
     sweep. Round-2 finding 5: `tests/integration/test_env_hermeticity.py:419` now pins the exact
     Known-limitations sentence (not a bare `"synthetic" in README`) and the exact shell block
     `umask 077` / `noclobber` / `exec > "$TREE/.env"` plus the absence of `chmod 600`.

## Round-2 regression proofs (mutation-checked)

Each new guard was runtime-patched in-process and the guarding test observed to fail:

| mutation | test that fails |
|---|---|
| `_untrusted_parity_root` loses the symlink arm | `test_parity_sweep_refuses_untrusted_temp_roots` |
| `_untrusted_parity_root` loses the group/world-writable arm | `test_parity_sweep_refuses_untrusted_temp_roots` |
| `_registered_in` replaced by `_repo_of_worktree` | `test_parity_sweep_does_not_remove_an_unregistered_tree` |
| `_remove_parity_residue` becomes a no-op | `test_recovery_commit_never_stages_parity_residue` |
| the `owner != wanted` scope skip deleted from `sweep_parity_residue` | `test_parity_sweep_is_project_scoped_and_reports_unopenable_roots` |

The mutation harness lives outside the repository (a `-p` pytest plugin under `/tmp`), so no
scratch file is committed.