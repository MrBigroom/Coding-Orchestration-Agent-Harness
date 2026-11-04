# T-9 evidence — run-completion notifications

Captured at `HEAD 8f4cddf018287aacf3525a272f631fc16816fa63` on
`task-9-no-run-completion-notification-unattended-gated`. Source/tests were committed before the
suite ran; this directory is the only later change.

## Commands and results

| Command | Result | Log |
|---|---|---|
| `./scripts/orch_verify.sh test` | **596 passed, 6 skipped**, exit 0 | `full_suite.txt` |
| `./scripts/orch_verify.sh lint` | `All checks passed!`, exit 0 | `lint.txt` |
| `tsc -b --force` (worktree `web/`, main checkout `node_modules` symlinked outside the worktree) | `tsc_exit=0` | `gui_typecheck.txt` |

## DoD -> evidence

1. **Fan-out at the single chokepoint, all human-must-act triggers.**
   `src/orchestrator/services/log_service.py:67` calls `notification_service.notify_event` after the
   build-log row commits and the JSONL line is written. Trigger vocabulary:
   `src/orchestrator/core/notification_vocab.py:25` (`TRIGGERS`), selection for direct events and
   `task_status -> review|blocked` at `notification_vocab.py:87`; per-trigger settings at
   `src/orchestrator/services/settings_service.py:41`.
2. **Freedesktop D-Bus primary channel and payload.** `notification_service.py:203` prefers
   `notify-send`, then a direct `gdbus` `org.freedesktop.Notifications.Notify` call; payload builder
   `notification_service.py:131` carries task short id + title, run short id, outcome, next action
   and `http://<host>:<port>/tasks/<id>`, all through `redact()`. Asserted by
   `tests/unit/test_notifications.py::test_payload_shape_link_and_redaction` and
   `::test_channel_prefers_notify_send_then_gdbus`.
3. **Persistent list, skipped vs failed.** `src/orchestrator/core/models.py:409` (`Notification`,
   additive migration `0010_notifications`), written in `notification_service.py:322`; read by
   `src/orchestrator/api/routes/notifications.py:26` (`GET /api/notifications`) and
   `web/src/pages/Notifications.tsx`. `skipped` rows carry `master_disabled`/`trigger_disabled`;
   delivery errors carry `failed` plus the channel error. Asserted by
   `test_off_by_default_records_skip_and_delivers_nothing` and `test_no_client_is_a_recorded_failure`.
4. **Settings default off, per-event toggles.** `settings_service.py:41`/`:78` adds
   `notifications_enabled` (false) and every `notification_<trigger>`; Settings card at
   `web/src/pages/Settings.tsx:103`; README `Desktop notifications (off by default)` and
   `docs/configuration.md` list the keys/defaults; `docs/build-loop.md` records the trigger set.
5. **Failure-proof.** `log_service.py:67` wraps the call; `notification_service.py:322` swallows its
   own failures. `test_raising_notifier_records_failure_and_build_log_survives` makes the notifier
   raise and asserts the `BuildLogEntry` still exists; `test_raising_fanout_never_loses_the_build_log`
   raises the fan-out itself and asserts the same.
6. **Spam control.** Unique `dedupe_key` = run/task + trigger (`models.py:409`,
   `notification_service.py:322`) plus coalescing in `_flush` (`notification_service.py:248`);
   noisy triggers default false (`notification_vocab.py:81`). Asserted by
   `test_burst_for_one_run_is_coalesced_but_each_trigger_keeps_a_row` and
   `test_noisy_events_are_silent_until_enabled`.
7. **Offline coverage and buildability.** Logs above; end-to-end mock harness at
   `tests/integration/test_notifications.py:73` (completed run -> one coalesced summary containing
   `task_review`, `merge_candidate`, `run_completed`). Fallback UI still serves and has a
   Notifications view (`src/orchestrator/api/static_fallback/index.html`).
8. **`run_cancelled`.** Emitted once in the cancel branch, after the terminal status commits and
   before handoff salvage: `src/orchestrator/services/run_service.py:1091`, role `worker`, details
   `"<run short id> cancelled (exit <code>)"`. Added to `docs/build-loop.md` vocabulary; subscribable
   trigger `run_cancelled` (`notification_vocab.py:25`). Asserted by
   `tests/integration/test_notifications.py::test_cancelled_run_emits_run_cancelled_once_and_fans_out`
   and the updated `tests/integration/test_handoff_salvage.py` assertion (one `run_cancelled`).
9. **Honest duration.** `notification_service.py:55` reads `run.json.process_ms` (compute) and
   `runs.duration_ms` (wall), adds `clock suspended <gap>` only when the gap exceeds 25% of wall.
   Asserted by `test_duration_reports_compute_and_wall_and_names_a_freeze` (41.7 compute / 80.2 wall
   -> `clock suspended 38.5m`) and `test_small_gap_has_no_clock_freeze_clause`.

## Existing test changed (not silently loosened)

`tests/integration/test_handoff_salvage.py` previously asserted `"run_cancelled" not in [...]`
because T-8 deliberately deferred the event to T-9. It now asserts exactly one `run_cancelled` for
the cancelled salvaged run — the T-9 DoD 8 contract, not a weakened assertion.

## Review note

The code diff before this evidence directory is ~72.8k characters (the branch diff including these logs is ~82k), above the reviewer's ~48k diff budget. The implementation
lives in `src/orchestrator/services/notification_service.py` (381 lines) and
`src/orchestrator/core/notification_vocab.py` (100 lines); this README and the handoff name the
decisive lines so the wiring is checkable even if the supplied diff is middle-truncated.