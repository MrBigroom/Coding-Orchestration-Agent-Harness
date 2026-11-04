# T-12 evidence — attempts vs verified review rounds

Branch `task-12-the-review-round-cap-counts-only-completed-runs`. Every file here was produced by
one invocation of `capture.sh`; each log starts with the captured `HEAD=<sha>` and
`tree=0 dirty-entries` stamp. Generated logs are marked `-diff` in `.gitattributes` so they stay
committed without consuming the reviewer's 12k-token diff budget.

| Evidence | Command | Expected result |
|---|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` | `471 passed, 6 skipped` |
| `attempt_cap_tests.txt` | `./scripts/orch_verify.sh test tests/integration/test_attempt_cap.py -vv` | every test named, `11 passed` |
| `checks.txt` | lint, cold `settings_service.defaults()`, `tsc -b --force` on `web/src` | lint clean, `max_run_attempts_per_task=6`, `TSC_EXIT=0` |

`full_suite.txt` also carries the `ORCH_VERIFY|…|worktree_root=<this worktree>` marker, so the
tests ran against this branch, not the main checkout.

## Definition of Done → code and proof

1. **Attempts have their own additive limit.** `settings_service.py` registers
   `max_run_attempts_per_task` with default **6**; `supervisor_service.run_attempt_stats` counts
   completed/`testing` as verified rounds and timeout/failed/cancelled as attempts.
   Proof: `test_three_consecutive_timeouts_reach_blocked_on_the_attempt_limit` and
   `test_default_attempt_limit_preserves_the_completed_only_flow`.
2. **Either limit blocks, names the fired limit and breakdown, emits an event, GUI shows the
   summary.** `supervisor_service._fired_limits` / `_block_for_limits` /
   `_requeue_or_block`; API `attempts` field in `api/routes/tasks.py`; `Tasks.tsx` and
   `TaskDetail.tsx` render `attempts.summary`. Event `task_limits_exhausted` documented in
   `docs/build-loop.md`. Blocked tests assert the fired limit, the
   `completed/timeout/failed/cancelled` breakdown and exactly one submission clause.
3. **Requeue reason and next prompt name the previous submission status.**
   `supervisor_service.submission_reason_suffix` (added once, by the callee) reaches the `planned`
   transition, the `queued` requeue and both blocked paths. `run_service._submission_note` feeds
   `prompt_builder` section “Previous round submission status”. Since T-10 is on this base, the
   round-start action is archive-then-unlink, and the prompt says “archived at round start”.
4. **Offline tests (a)–(e).** (a) three timeouts → blocked; (b) completed-only flow blocks at
   `max_review_rounds`; (c) mixed timeout/cancelled/completed/timeout counts both limits;
   (d) blocked reason names the fired limit and breakdown (and only one submission clause);
   (e) default 6 preserves completed-only behaviour. Mock harness, local git repo, no network.
5. **Docs with an explicit default.** `docs/configuration.md`; `README.md` runtime-settings table;
   `docs/build-loop.md` “Verified rounds versus attempts”. Default **6**.

## Contract notes

- No state-machine, schema or frozen-contract change. `blocked` is reached through the existing
  legal `in_progress -> blocked` transition, asserted by every new cap test.
- `GET /api/tasks` and `/api/tasks/{id}` gain one additive derived key, `attempts`; no existing
  key changes. `task_limits_exhausted` is an additive build-log event.
- No new dependency: `pyproject.toml` and `uv.lock` are unchanged against the merge-base.