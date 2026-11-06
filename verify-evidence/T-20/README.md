# T-20 evidence — synthetic parity env, reclaimable residue, verified sweep claims

Code HEAD these logs ran at: `135b0b9b4870998e0355bb3e2c2b794de10d0e76` (round 3 code commit).
The evidence commit after it changes only `verify-evidence/T-20/`. Every log stamps the HEAD it
ran at, so the later evidence commit does not move the claimed run.

## Commands and counts (all at the stamped HEAD)

| file | command | result |
|---|---|---|
| `full-test.txt` | `./scripts/orch_verify.sh test` | **670 passed, 9 skipped, 0 failed**, exit 0 |
| `zz-per-test.txt` | `./scripts/orch_verify.sh test -vv -k "<T-20 selection>"` | **23 passed, 656 deselected**, exit 0 |
| `lint.txt` | `./scripts/orch_verify.sh lint` | `All checks passed!`, exit 0 |
| `zz-final-source.txt` | generated from the committed files | FINAL SOURCE slices, numbered at that HEAD |

The 9 skips are the pre-existing opt-in spawned-suite tests (`ORCH_ENV_SPAWN_TESTS`), not T-20.

**Review-budget note.** The branch diff is ~96 KB (≈24k tokens) against `max_diff_tokens=12000`, so
the harness cannot supply every file whole. `zz-final-source.txt` is the budget-sized extract the
round-3 review asked for: it is written last alphabetically so it sorts last, and it carries the
final source of exactly the areas the review named — the lock path, the sweep result type, both
sweep bodies, the residue decision in `run_env_parity_checks`, the DoD 4a–4d test assertions, the
README Known-limitations block and the shell name pipeline — each prefixed with `file:start-end` at
the stamped HEAD. The same decisive excerpts are repeated in `handoff.md`, which is supplied whole.

## DoD mapping

1. **Never copy the real `.env`.** `project_service._env_variable_names`
   (`zz-final-source.txt`: `project_service.py:462-484`) parses only the name before `=`;
   `_synthetic_env_file` writes placeholders 0600/create-only; `scripts/orch_env_parity.sh:91`
   derives names with `sed` and creates the file inside the `umask 077` + `noclobber` subshell.
   Proof: `test_parity_synthesis_never_writes_a_real_credential_value` (assertions in
   `zz-final-source.txt`, `test_env_hermeticity.py:900-940`) intercepts every `os.write` and asserts
   the credential value never reaches the written bytes or the file; the shell path is checked by
   the SIGKILL test's `secret not in observed` assertions.
2. **Crash-safe residue reclaim.** `git_service.sweep_parity_residue` reclaims managed `env-parity`
   snapshots (`_sweep_one_managed_parity`, `:395-436`) and `<tmpdir>/orch-env-parity.*` roots
   (`_sweep_temp_parity`, `:479-554`). Refusals: `_untrusted_parity_root` lstat-checks the root
   (symlink/foreign-uid/group-writable), a symlinked `root/tree` is unlinked not followed, and
   `git worktree remove` runs only for a worktree git lists. A killed shell run is reclaimed by
   `test_a_killed_shell_parity_run_leaves_no_registered_worktree`
   (`test_env_hermeticity.py:728-805`).
3. **Cleanup knows parity files are residue.** `status_split` filters parity names for parity
   destinations only; `recovery_commit_info` removes residue before staging and resets sensitive
   names; `remove_worktree` removes residue before the dirty check. Proof:
   `test_recovery_commit_never_stages_parity_residue` (`:808-842`),
   `test_recovery_commit_never_stages_a_leaked_dotenv_on_a_phase_branch` (`:845-882`, the
   phase-branch half, with a git-visible `?? .env` control),
   `test_remove_worktree_succeeds_with_parity_residue` (`:885-897`).
4. **Offline tests.** (a) SIGKILL: `:728-805`; (b) recovery x2: `:808-842`, `:845-882`;
   (c) worktree removal: `:885-897`; (d) write interception: `:900-940`. No network, no real
   credentials; fixtures are built from name/value pairs so no assignment-shaped literal is stored.
5. **Docs.** `docs/build-loop.md` states the parity environment is synthetic, that a SIGKILL can
   leave residue, and that the sweep (not assumption) reclaims it; `README.md:273-276`
   (`zz-final-source.txt`) states the synthetic `.env` and the remaining SIGKILL residue risk.
   Pinned by `test_make_target_is_documented_as_the_developer_parity_path`.

## Round-3 review dispositions

1. **Lock path inside `worktrees/`** — removed. `parity_lock_path` (`git_service.py:207-217`) is now
   the frozen managed marker itself (`.managed/env-parity`); no `.locks/` directory is created and no
   path scheme is added. `parity_lock` (`:221-241`) holds `flock` on that marker, so the lock
   disappears with the marker. Test: `test_parity_lock_is_the_managed_marker_not_a_new_directory`.
2. **`ok` derived from prose / unverified removal claims** — replaced by
   `ParitySweepAction(detail, ok, busy)` (`:70-86`). `ok` is set by re-checking the tree, the git
   worktree listing (`_listed_worktree`) and the marker afterwards; a refused `remove_worktree`, an
   rmtree that left the root, or a surviving registration reports `ok=False` with a `could not
   remove …` detail. Tests: `test_parity_sweep_reports_a_surviving_tree_as_not_reclaimed` (the old
   `removed residue …; skip …: has uncommitted changes` shape), and
   `test_parity_sweep_does_not_claim_a_removal_rmtree_did_not_do`.
3. **Test/evidence outside the diff budget** — `zz-final-source.txt` (this README's note above).
4. **README Known-limitations cut** — `README.md:273-276` quoted above and in the section extract.
5. **Top-level `fcntl`** — imported lazily inside `parity_lock` and `_parity_lock_at`; pinned by
   `test_git_service_keeps_the_posix_lock_import_lazy`.
6. **Name order (`sort -u` vs first-seen)** — Python now returns `sorted(names)`
   (`project_service.py:462-484`) and the script sorts with `LC_ALL=C`, so both write the same
   sequence. Pinned by `test_parity_env_name_order_matches_the_shell_path`, which extracts the sed
   pipeline from the shipped script instead of copying it.