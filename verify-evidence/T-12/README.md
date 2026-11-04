# T-12 evidence — attempts vs verified review rounds

Branch `task-12-the-review-round-cap-counts-only-completed-runs`. Everything below was captured by
one invocation of `capture.sh` at:

`HEAD=bb43acc1d105953004093d315805308f9b58b533` (`bb43acc`) — tree clean, `dirty-entries=0`.

The branch's code HEAD under review is `486def2`; `bb43acc` adds only this evidence directory, and
the final handoff/evidence commit adds only these logs. `capture.sh` stages every output in `/tmp`
and moves them in only after all commands finish, so the `dirty-entries=0` stamp is real rather than
counting the evidence files themselves.

## Commands and real counts

| Evidence | Command | Result at `bb43acc` |
|---|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` | **471 passed, 6 skipped, 0 failed** in 101.56s |
| `attempt_cap_tests.txt` | `./scripts/orch_verify.sh test tests/integration/test_attempt_cap.py -vv` | **11 passed** in 13.95s |
| `lint.txt` | `./scripts/orch_verify.sh lint` | `All checks passed!` |
| `gui_typecheck.txt` | `tsc -b --force` in a scratch tree whose `web/src` symlinks into this worktree | `TSC_EXIT=0`; the four T-12 GUI files are listed as read |
| `settings_defaults.txt` | cold interpreter, `PYTHONPATH=<worktree>/src`, clean env | registry defaults, including `max_run_attempts_per_task=6` |
| `attempt_cap_source.txt` | `nl -ba tests/integration/test_attempt_cap.py` | full test source (371 lines) |
| `head_files.txt` | `git show --stat`, `git ls-tree HEAD`, `Tasks.tsx` at HEAD | every claimed file, including `Tasks.tsx` |

Each log starts with the same `# HEAD=… tree=0 dirty-entries` stamp. `full_suite.txt` also carries
the `ORCH_VERIFY|…|worktree_root=<this worktree>` marker, so the tests ran against this branch,
not the main checkout.

## Round-2 reviewer findings → what landed

1. **Submission status now appears in every non-gate requeue and in the attempt-limit block.**
   `supervisor_service.py:476` adds `submission_reason_suffix(run)`;
   `supervisor_service.py:490` appends it inside `enforce_attempt_limit`, and
   `supervisor_service.py:513` appends it to the `after_run → queued` reason.
   `run_service.py:819` (unexpected exception), `:1058` (cancelled), `:1095` (timeout/failed) and
   `:1258` (provider error) pass the same clause into both the `in_progress → planned` transition
   and `enforce_attempt_limit`. Tests assert it on both destinations.
2. **The other cap paths are exercised at the limit, not just timeout.**
   `test_cancelled_run_at_the_attempt_limit_blocks_with_its_submission_status` (l.212),
   `test_failed_run_at_the_attempt_limit_blocks_with_its_submission_status` (l.237),
   `test_unexpected_exception_at_the_attempt_limit_blocks` (l.257) and
   `test_provider_error_at_the_attempt_limit_blocks` (l.281) each drive a real run, spend the
   configured budget, and assert the exact `in_progress -> blocked` transition. Each asserts the
   breakdown and the submission-status clause.
3. **The test diff is no longer the only way to read the assertions.**
   `attempt_cap_source.txt` is the full numbered source; the `-vv` log names all 11 tests. The
   evidence is committed, so it cannot be lost to payload truncation.
4. **`Tasks.tsx` provenance is explicit.**
   `head_files.txt` records `git ls-tree HEAD` for `web/src/pages/Tasks.tsx` (blob
   `e974fe4c…`) and `git show HEAD:web/src/pages/Tasks.tsx` lines 105, 116–117 — the attempts
   column. The same file records `git show --stat a3eb2fd`, where it appears.
5. **The README no longer asserts defaults for keys this phase did not touch.**
   `README.md:102-117` now scopes the table to the two loop limits and points at
   `docs/configuration.md` for the rest. `settings_defaults.txt` still proves the code defaults
   for every registry key in a clean environment.

## Definition of Done → code and proof

1. **Attempts have their own additive limit, registered through `settings_service`.**
   `settings_service.py:29` adds the key to `EDITABLE`; `:53` sets the default **6**.
   `supervisor_service.py:373` (`run_attempt_stats`) counts completed/`testing` as verified rounds
   and timeout/failed/cancelled as attempts.
   Proof: `test_three_consecutive_timeouts_reach_blocked_on_the_attempt_limit` (l.102) and
   `test_default_attempt_limit_preserves_the_completed_only_flow` (l.139).

2. **Either limit blocks, names the fired limit and breakdown, emits an event, and the GUI shows
   attempts instead of “round N of M”.**
   `supervisor_service.py:414` (`_fired_limits`), `:425` (`_block_for_limits`), `:438` (event),
   `:513` (`_requeue_or_block`). Non-gate paths call `enforce_attempt_limit` at
   `run_service.py:820,1059,1096,1259`.
   Additive derived API field: `api/routes/tasks.py:43` (detail), `:102` (list).
   GUI: `web/src/pages/Tasks.tsx:115-117` and `TaskDetail.tsx:142-145`.
   Event documented at `docs/build-loop.md:465`.
   Proof: blocked-reason assertions in the tests at l.102, 139, 159, 212, 237, 257 and 281;
   `test_task_api_exposes_attempts_in_the_list_and_detail` (l.354).

3. **The requeue reason and next Worker prompt state the previous round's submission status.**
   `supervisor_service.py:476` (shared clause) and `:513` (queued reason); `run_service.py:566`
   (`_submission_note`) inspects the previous run and the round-start archive/unlink; the note is
   passed into the prompt at `:955` and rendered by `prompt_builder.py:154`.
   T-10 is already fixed on this base, so the old round-start delete is archive-then-unlink; the
   prompt says that explicitly rather than claiming a deletion.
   Proof: `test_changes_requested_requeue_names_the_previous_submission` (l.304),
   `test_next_prompt_states_an_absent_previous_submission` (l.315),
   `test_salvaged_round_is_named_in_the_requeue_and_the_next_prompt` (l.333), plus the
   planned/blocked-reason assertions in the timeout, cancelled, failed, exception and provider tests.

4. **Offline tests (a)–(e).**
   (a) three timeouts → blocked on the attempt limit: l.102.
   (b) completed-only flow still blocks at `max_review_rounds`: l.139; the pre-existing
   `tests/integration/test_mock_run.py::test_changes_requested_requeues_then_blocks_after_round_limit`
   also remains green in `full_suite.txt`.
   (c) mixed timeout/cancelled/completed/timeout counts both limits: l.159.
   (d) blocked reason names the fired limit and the completed/timeout/failed/cancelled breakdown:
   asserted in every blocked test.
   (e) default 6 preserves the completed-only flow: l.139 and `settings_defaults.txt`.
   All offline: mock harness + mock provider + a real local git repo in `tmp_path`; no network,
   no live service.

5. **Documentation with the explicit default.**
   `docs/configuration.md:42-55`; `README.md:102-117`; `docs/build-loop.md:329-342`
   (“Verified rounds versus attempts”) and the failure-path note at `:62-68`.
   `settings_defaults.txt` prints `max_run_attempts_per_task=6` from the code path.

## Contract notes

- No schema, state-machine or frozen-contract text change. `blocked` is reached through the
  existing legal `in_progress → blocked` transition, and the new tests assert that exact source
  state on the cancelled, failed, exception and provider-error branches.
- `GET /api/tasks` and `/api/tasks/{id}` gain one additive derived key, `attempts`; no existing key
  changes. This is the derived-field allowance already recorded for the task detail response.
- The new build-log event name `task_limits_exhausted` is additive and documented in
  `docs/build-loop.md`.
- No new dependency: `head_files.txt` shows an empty diff for `pyproject.toml` and `uv.lock`
  against the merge-base.