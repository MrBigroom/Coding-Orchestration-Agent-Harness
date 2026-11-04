# T-12 evidence — attempts vs verified review rounds

Implementation HEAD for every log below: `a3eb2fd` (the evidence commit adds only this directory).

## Commands and real counts

| Evidence | Command | Result |
|---|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` | **467 passed, 6 skipped** in 99.41s |
| `attempt_cap_tests.txt` | `./scripts/orch_verify.sh test tests/integration/test_attempt_cap.py -vv` | **7 passed** in 10.91s |
| `lint.txt` | `./scripts/orch_verify.sh lint` | `All checks passed!` (ruff) |
| `gui_typecheck.txt` | `tsc -b --force` in a scratch dir with `web/node_modules` symlinked outside the worktree | exit 0 |

Each log begins with `# HEAD=<sha>`; the full-suite log also records the tree and the number of
dirty entries at the moment it ran.

## Definition of Done → code and proof

1. **Attempts have their own additive limit, registered in `settings_service`.**
   - `src/orchestrator/services/settings_service.py:29` adds `max_run_attempts_per_task` to
     `EDITABLE`; `:53` sets the code default to **6**.
   - `src/orchestrator/services/supervisor_service.py:373` (`run_attempt_stats`) counts
     completed/`testing` as verified rounds and timeout/failed/cancelled as attempts.
   - Proof: `test_three_consecutive_timeouts_reach_blocked_on_the_attempt_limit` (line 101) and
     `test_default_attempt_limit_preserves_the_completed_only_flow` (line 138).

2. **Either limit blocks, names the fired limit and the breakdown, emits an event, and the GUI
   shows attempts.**
   - `supervisor_service.py:414` (`_fired_limits`), `:425` (`_block_for_limits`), `:497`
     (`_requeue_or_block`); `run_service.py:1097` applies the attempt cap to timeout/failed runs,
     `:1059` to cancelled runs, `:819`/`:1261` to unexpected/provider failures.
   - Additive event `task_limits_exhausted` at `supervisor_service.py:438`; documented in
     `docs/build-loop.md:463`.
   - Derived API field: `src/orchestrator/api/routes/tasks.py:43` (detail) and `:102` (list).
   - GUI rows: `web/src/pages/Tasks.tsx:105` and `web/src/pages/TaskDetail.tsx:141`.
   - Proof: blocked-reason assertions inside the first, second and third tests, plus
     `test_task_api_exposes_attempts_in_the_list_and_detail` (line 253).

3. **Requeue reason and next Worker prompt state the previous submission status.**
   - `supervisor_service.py:458` (`submission_status`) is appended to the requeue reason at `:497`.
   - `run_service.py:566` (`_submission_note`) inspects the previous run and the round-start
     archive/unlink; the note is passed into the prompt at `:956` and rendered by
     `prompt_builder.py:152`.
   - T-10 is already fixed on this base, so the old round-start *delete* is now
     archive-then-unlink; the prompt states that explicitly instead of claiming a deletion.
   - Proof: `test_changes_requested_requeue_names_the_previous_submission` (line 203),
     `test_next_prompt_states_an_absent_previous_submission` (line 214), and
     `test_salvaged_round_is_named_in_the_requeue_and_the_next_prompt` (line 232).

4. **Offline tests (a)–(e).**
   - (a) three timeouts → blocked: test at line 101.
   - (b) completed-only path still blocks at `max_review_rounds`: test at line 138; the pre-existing
     `tests/integration/test_mock_run.py::test_changes_requested_requeues_then_blocks_after_round_limit`
     also remains green in `full_suite.txt`.
   - (c) mixed timeout/cancelled/completed/timeout counts both limits: test at line 158.
   - (d) blocked reason names the fired limit + breakdown: asserted in all three blocked tests.
   - (e) default preserves the completed-only flow: `defaults()["max_run_attempts_per_task"] == 6`
     plus the two-round block in the test at line 138.

5. **Documentation.**
   - `docs/configuration.md:40-56` — key, default 6, verified-round vs attempt semantics.
   - `README.md:96-125` — runtime settings table and the explicit default.
   - `docs/build-loop.md:327-339` — “Verified rounds versus attempts”; also the failure-path note
     at `:62-66` and the event list at `:463`.

## Contract notes

- No schema or state-machine change. `blocked` is reached through the existing legal
  `in_progress/defer review → blocked` transitions.
- `GET /api/tasks` and `GET /api/tasks/{id}` gain one additive derived key, `attempts`, and no
  existing key changes; the derived-field allowance is the one recorded in
  `docs/frozen-contracts.md` (task detail response).
- The new build-log event name is additive and is documented in `docs/build-loop.md`.
