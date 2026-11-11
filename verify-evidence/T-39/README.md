# T-39 verification evidence

Generated with `orchestrator task evidence T-39 --full` from a clean worktree. That driver runs the
committed `capture.sh`, canonicalises the stamp on every file the pass rewrote to
`# HEAD=<sha> mode=<fast|full>`, refuses any uncommitted input, and writes `manifest.json` plus
`freshness_selfcheck.txt` itself. The stamped HEAD is the code commit measured; the evidence-only
commit that ships this bundle does not change code, so it does not stale the bundle.
`freshness_selfcheck.txt` is the driver's own record (no stamp by construction) and reports the
stamped-file count.

| file | proves |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` -> full-suite counts; `ORCH_VERIFY` shows the imports resolved inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` -> `All checks passed!` |
| `t39_tests.txt` | the 10 T-39 tests, each named `PASSED` (`-vv`, offline: fixture repo, fixture worktrees, temporary ORCHESTRATOR_HOME) |
| `source_anchors.txt` | committed-HEAD file:line anchors for every claim below |
| `live_measurement.txt` | DRY RUN ONLY against the operator's live home, database opened `mode=ro`, `db.upgrade()` never called; worktree counts before/after |

## DoD -> evidence

1. **Ownership, not a numeric marker.** `cleanup_service.plan` classifies a marker as a task or a
   chat worktree (`cleanup_service.py:166`), then resolves the owning session through
   `chat_sessions.cwd` (`_chat_sessions_by_cwd`, `:99`) and skips any status that is not `closed`
   (`:138`: `[c for c in chats if c.status != ChatStatus.closed]`). The status vocabulary is read
   from `ChatStatus` (`core/models.py:101-108`, `busy` at `:104`), not hard-coded. `live_measurement.txt`
   shows `skip .../chat-5: chat session 5 is closed (pass --include-chats to remove)` and
   `skip .../chat-13: chat session 13 is closed (...)`, i.e. the chat marker is now owned.
2. **Live-process backstop.** `cwd_owner_pids` (`cleanup_service.py:68`) reads every
   `/proc/<pid>/cwd`, matches the worktree or a directory inside it, and confirms each pid with
   `git_service.process_is_alive` (`git_service.py:1221`, T-20's `os.kill(pid, 0)` rule); the
   refusal reason names the check and the session (`cleanup_service.py:128-136`). Live proof:
   `live_measurement.txt` shows
   `refused | .../chat-10 | live process 282742, 282767, 282846 has it as its cwd (chat session 10 is idle)`
   -- the tree the old code listed for deletion is now refused, and the refusal does not depend on
   the row (the committed test `test_live_cwd_refuses_even_when_session_row_says_closed` uses a
   `closed` row with a real sleeping child holding the cwd).
3. **Opt-in flags.** `cli.py:310` declares `--include-chats` (`:319`, closed sessions only) and
   `--include-live-chats` (`:324`, help text: "CAN DELETE THE DIRECTORY OF A RUNNING SESSION"),
   and `--include-active` never reaches a chat worktree (`cleanup_service.py:128/137/146`).
   `test_busy_chat_worktree_is_skipped_and_named` asserts `--include-active` and `--include-chats`
   both leave a busy session alone; `test_without_the_live_process_a_closed_session_is_removable`
   and `test_closed_chat_worktree_is_removable` show the closed-session opt-in.
4. **Task behaviour unchanged.** `cleanup_service.py:199-208` keeps the exact old rule
   (`DONE_TASK_STATUSES` = accepted/archived/rejected, else skipped unless `--include-active`), and
   the parity sweep is still `git_service.sweep_parity_residue` before the plan (`cli.py:344`,
   unchanged code). Evidence: `test_task_worktrees_keep_their_existing_behaviour` and the live
   `skip .../39: task T-39 is in_progress`.
5. **Three dry-run outcomes.** Records are `would remove worktree ...`, `skip <path>: <reason>`
   (task status, chat session status, chat-closed opt-in, dirty) and `refuse <path>: <reason>`
   (live process); `live_measurement.txt` contains all three in one dry run.
6. **Tests, offline and mock-only.** `t39_tests.txt` names all 10:
   `test_busy_chat_worktree_is_skipped_and_named` (a), `test_closed_chat_worktree_is_removable` (b),
   `test_live_cwd_refuses_even_when_session_row_says_closed` (c),
   `test_task_worktrees_keep_their_existing_behaviour` (d),
   `test_busy_chat_worktree_is_skipped_and_named` (e),
   `test_dry_run_mutates_nothing_including_git_metadata` (f), plus
   `test_failed_removal_is_counted_as_a_failure_not_a_removal`,
   `test_printed_counts_and_json_match_the_actual_outcomes`,
   `test_the_live_process_check_is_what_refuses_a_closed_session`,
   `test_counts_and_summary_read_the_kind_not_the_wording`.
   (f) compares git's own metadata: `git worktree list --porcelain`, `git status`, and the
   `.git/worktrees/*` directory mtimes, because a plain `git status` rewrites the per-worktree
   index. The preview therefore asks git for a lock-free status (`utils/git.py:71/88/92/99`,
   `--no-optional-locks`, wired at `git_service.py:1243`); `live_measurement.txt` records
   `managed_markers_before=N after=N` and `git_worktree_list_before=N after=N`.
7. **Docs.** `docs/chat.md:237` (`## \`orchestrator cleanup\` and chat worktrees`) states which
   worktrees cleanup will and will not touch, the session-status rule, the live-process refusal and
   the two opt-in flags; `docs/build-loop.md:902-916` states the ownership rule (task by status,
   chat by session + live process, parity sweep unchanged) and the one-record-per-line/`--json`
   output.
8. **Suite and lint.** `full_suite.txt` (926 passed, 10 skipped at the stamped HEAD) and
   `lint.txt` (`All checks passed!`), both through `./scripts/orch_verify.sh`, whose
   `ORCH_VERIFY|...|worktree_root=...` marker proves the imports resolved inside this worktree.

## Reporting honesty (DoD 4)

`remove_worktree_outcome` (`git_service.py:1226`) verifies the outcome after acting: a tree that
still exists is `REMOVAL_FAILED` (`:1265`), never "removed"; a failing `shutil.rmtree` becomes a
failure with its exception instead of propagating out of the loop; git metadata is pruned so the
checkout is not left mid-operation; and the marker is only unlinked once the tree is gone.
`cleanup_service.counts`/`summary` (`:209`/`:216`) count `kind`, never the message text. A failed
removal exits non-zero (`cli.py:369`). `test_failed_removal_is_counted_as_a_failure_not_a_removal`
forces `shutil.rmtree` to fail (read-only parent) and asserts exit 1, `failed to remove worktree`,
no `removed worktree` record, and `cleanup: 0 removed, 0 skipped, 0 refused, 1 failed` with the
tree and marker still present. `test_counts_and_summary_read_the_kind_not_the_wording` gives an
action the message "removed ..." but kind `failed` and asserts the summary counts it as failed.

## Assumption recorded in the handoff

`chat_sessions` has no pid column (verified in `core/models.py:325-357` and the baseline migration),
so DoD 2's "harness pid is alive" half is implemented as the PID-based cwd scan: the harness is
spawned with `cwd=Path(chat.cwd)` (`chat_service.py:826`, `:882`), so a live harness process *is* a
process whose cwd is the worktree, and that is what the scan proves alive with
`os.kill(pid, 0)`.