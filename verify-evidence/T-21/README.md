# T-21 evidence — standalone run notifications and task-less dedupe identity

Code HEAD these runs were taken at: `318829854c08cb0df461035970f71dec49f9f842`
(`feat(task-21)` + `test(task-21)`; the evidence commit that follows changes only this directory).

| File | Command | Proven final line |
| --- | --- | --- |
| `test.txt` | `./scripts/orch_verify.sh test` | line 23: `653 passed, 9 skipped in 134.71s` |
| `lint.txt` | `./scripts/orch_verify.sh lint` | line 13: `All checks passed!` |
| `new-tests.txt` | `./scripts/orch_verify.sh test tests/unit/test_notifications.py tests/integration/test_standalone.py -vv` | line 76: `54 passed in 12.39s` |

The `ORCH_VERIFY|...|module_file=<worktree>/src/orchestrator/__init__.py|worktree_root=<worktree>` marker is in each log, so the runs verified this worktree, not the main checkout.

## DoD map

1. **TRIGGERS gains non-noisy `standalone_run`; `trigger_for_event` returns it.**
   `src/orchestrator/core/notification_vocab.py:43-46` (key, label, action, `terminal=True`, default `noisy=False`).
   `tests/unit/test_notifications.py:336` asserts the key, the setting name and the subscribed default.
2. **`notify_event()` accepts `standalone_run_id`, resolves the StandaloneRun, keeps `Notification.run_id` NULL.**
   `src/orchestrator/services/notification_service.py:332-361` (signature, StandaloneRun lookup, `rid = None`);
   `src/orchestrator/services/log_service.py:29,68-76` forwards it from the single fan-out chokepoint.
   `tests/unit/test_notifications.py:366` asserts `run_id is None and task_id is None`;
   `tests/integration/test_standalone.py:138` asserts the same through `GET /api/notifications`.
3. **`_payload()` names the agent/status/report file; a task-less key is unique and never `task:None:<trigger>`.**
   `src/orchestrator/services/notification_service.py:122-168` (standalone body: `Agent:`, `Run: standalone #<id> (<status>)`,
   `Report file: <report_path>`, link from `_agent_link()` = `/agents`).
   Dedupe identity: `:361` standalone, `:378` run, `:380` task, `:384` per-event UUID for no owner.
   `tests/unit/test_notifications.py:366` (agent/title/status/report/link and one row per run),
   `:390` (trigger off = no delivery), `:407` (skipped), `:420` (failed + error),
   `:434` (two task-less `schedule_fired` events -> two distinct keys and two deliveries).
4. **Fan-out happens only after the report exists and the row is committed.**
   `src/orchestrator/services/standalone_service.py:188-198` (`_end` commits the row before
   `log_service.log_event(..., standalone_run_id=run.id)`; `_save_outcome` writes the report before `_end`).
   `tests/integration/test_standalone.py:153` spies on `notify_event` and reads the row from a
   separate session, asserting status `completed`, the committed `report_path`, and that the file exists.
5. **GUI + docs list the new setting.**
   `web/src/pages/Settings.tsx:48` adds `notification_standalone_run` in the human-must-act block
   (before the noisy rows); `docs/configuration.md:53-54` names `standalone_run` in the default-subscribed list.
6. **Required test coverage.**
   One row per completed standalone run: `tests/unit/test_notifications.py:366`,
   `tests/integration/test_standalone.py:138`.
   Trigger unsubscribed: `tests/unit/test_notifications.py:390` (no desktop delivery and `sent == []`;
   the durable row is `skipped`/`trigger_disabled`, the frozen T-9 shape — see handoff assumption).
   Skipped run recorded as skipped: `tests/unit/test_notifications.py:407`,
   `tests/integration/test_standalone.py:182`. Two task-less events -> two rows: `tests/unit/test_notifications.py:434`.
7. **Existing notification tests unchanged and green.**
   `tests/unit/test_notifications.py` (the original tests, including the
   `set(TRIGGERS) == set(CORE_TRIGGERS) | set(NOISY_TRIGGERS)` invariant at line 112) and
   `tests/integration/test_notifications.py` both pass in `test.txt` / `new-tests.txt`.

No frozen contract, schema, route, trigger key or response key was changed.
