# T-20 evidence: synthetic parity env, sweepable residue, no credential on a branch

Code HEAD these logs ran at: `318bd660664c496f95950d7144759f4fd8c60a5d`.
The evidence commit that follows this file changes only `verify-evidence/T-20/`. Every log
below stamps that HEAD itself, so a later evidence commit does not move the claimed run.

## Commands and counts

| file | command | result |
|---|---|---|
| `full-test.txt` | `./scripts/orch_verify.sh test` | `665 passed, 9 skipped`, exit 0 |
| `zz-per-test.txt` | `./scripts/orch_verify.sh test -vv -k "<T-20 selection>"` | `13 passed, 661 deselected`, exit 0 |
| `lint.txt` | `./scripts/orch_verify.sh lint` | `All checks passed!`, exit 0 |

`zz-per-test.txt` names each DoD-critical test with `PASSED` at the stamped HEAD and sorts last,
so it survives the review harness's mid-diff truncation. The 9 skips are the opt-in
spawned-suite tests (`ORCH_ENV_SPAWN_TESTS`), not any T-20 test.

## DoD mapping

1. **Never copy the real `.env`.**
   - `project_service.py:462` `_env_variable_names` reads only the name before `=` (comments,
     blank lines and `export` handled); `:503` `_synthetic_env_text` emits placeholders;
     `:513` `_synthetic_env_file` writes them create-only with mode 0600. The doctor probe calls
     it at `:704`.
   - `scripts/orch_env_parity.sh:89` derives names with `sed`, `:74` `parity_value` maps a name
     to a placeholder, `:98-100` creates the file inside the `umask 077` + `noclobber` subshell;
     there is no copy of the source file.
   - Proof: `test_parity_synthesis_never_writes_a_real_credential_value`
     (`test_env_hermeticity.py:784`) intercepts every `os.write` and asserts the credential value
     is absent from the written bytes and the resulting file; the shell path is checked by the
     SIGKILL test at `:612` (`secret not in observed` and the synthetic line is present).

2. **Crash-safe residue reclaim.**
   - `git_service.py:451` `sweep_parity_residue` reclaims managed `env-parity` snapshots (`:322`)
     and `<tmpdir>/orch-env-parity.*` roots (`:408`).
   - Refusal before deletion: `_untrusted_parity_root` (`:385`) lstat-checks the root (symlink,
     owner uid, group/world-writable are refused); a symlinked `root/tree` is unlinked, not
     followed (`:426-429`); `_registered_in` (`:297`) runs `git worktree remove` only for a LINKED
     worktree git itself lists. `.repo` scopes a shared-tempdir sweep to the asking checkout;
     every `OSError` becomes a reported `skip` line.
   - `scripts/orch_env_parity.sh:50-62` now takes the lock before writing `.repo` and installs
     the cleanup trap before any failure, so a sweep never sees a half-created root.
   - Proofs: `test_parity_sweep_refuses_untrusted_temp_roots` (`:500`),
     `test_parity_sweep_does_not_remove_an_unregistered_tree` (`:526`),
     `test_parity_sweep_is_project_scoped_and_reports_unopenable_roots` (`:551`),
     `test_parity_sweep_never_touches_a_tree_a_live_run_is_using` (`:451`), and the SIGKILL test
     at `:612` (real script, live lock blocks the sweep, kill, sweep removes tree and
     `git worktree list` registration).

3. **Cleanup knows parity files are harness residue.**
   - `git_service.py:552` `status_split` filters untracked parity names in a parity worktree;
     `recovery_commit_info` removes residue before staging (`:644`) and independently resets
     anything matching `_is_parity_residue_name` or `is_sensitive_path` (`:657`);
     `remove_worktree` removes it before the dirty check (`:884`).
   - Proofs: `test_recovery_commit_never_stages_parity_residue` (`:692`) adds the git-visible
     control `?? .env` / `?? .env.999-dead.parity`; the new
     `test_recovery_commit_never_stages_a_leaked_dotenv_on_a_phase_branch` (`:729`) proves the
     same for an ordinary task worktree, so a leak cannot ride a recovery commit onto a phase
     branch; `test_remove_worktree_succeeds_with_parity_residue` (`:769`) proves cleanup still
     deletes the tree. `tests/integration/test_cli.py:84` covers the CLI wiring.

4. **Offline tests, no network or real credentials.**
   - (a) kill/residue: `:612`; (b) recovery: `:692` and the phase-branch case `:729`;
     (c) remove worktree: `:769`; (d) no real credential written: `:784` plus the shell
     assertions in `:612`. All 13 selected tests are in `zz-per-test.txt` as `PASSED`; none
     uses the network or a real key.

5. **Docs.**
   - `docs/build-loop.md:462-481`: the parity environment is synthetic, both paths read names
     only, a killed run's residue is swept rather than assumed absent, the sweep is lock-aware
     and scoped, and a symlink/foreign root is refused ("residue but not a secret").
   - `README.md:273-276` Known limitations states the remaining SIGKILL residue risk and that
     `orchestrator cleanup` reclaims it. `test_make_target_is_documented_as_the_developer_parity_path`
     (`:419`) pins the exact Known-limitations sentence and the shell `umask 077`/`noclobber`/
     `exec > "$TREE/.env"` block.

## Reviewer round-2 findings: disposition

| finding | disposition in this branch |
|---|---|
| 1 symlinked root followed / arbitrary deletion | refused with `lstat`/uid/mode; linked-tree removal only via `_registered_in`; tested at `:500` and `:526` |
| 2 cross-project lock blocks doctor / unopenable root raises | `.repo` scoping and reported `skip`; unopenable `.lock` reported, probe still answers; `:551` |
| 3 recovery test had no `?? .env` control / missed `.env.*.parity` | control added at `:692`; phase-branch case added at `:729` |
| 4 per-test evidence omitted by the review budget | `zz-per-test.txt` names all 13 tests with `PASSED`, sorts last |
| 5 README hunk truncated / bare-`synthetic` pin | exact Known-limitations sentence and shell block pinned at `:419` |
| 6 shell redirect outside `umask 077` | redirect moved inside the subshell; locked before `.repo`; pinned at `:419` |
| 7 tree-less root removed silently | real run reports `removed orphaned parity root ...`; asserted in `:472` |
| 8 placeholder classification differed | substring classification shared by both paths; asserted in `:584` |
| 9 `_run_env_parity_locked` returned an ignored list | returns `None`; callers only read `add` |