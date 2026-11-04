# T-9 evidence — run-completion notifications (round after CHANGES_REQUESTED)

Code HEAD: `51855f9db95467c14a8c1f2a1d64113c0caf4c0a`. Source/tests/docs were committed first; the
suite and lint below ran at that HEAD; this directory is the only later commit. The branch diff is
large, so `handoff.md` repeats the decisive excerpts for the review budget.

| Command | Result | Log |
|---|---|---|
| `./scripts/orch_verify.sh test` | **596 passed, 6 skipped**, exit 0 | `full_suite.txt` |
| `./scripts/orch_verify.sh lint` | `All checks passed!`, exit 0 | `lint.txt` |
| `tsc -b --force` (main checkout `web/node_modules` symlinked outside the worktree) | `tsc_exit=0` | `gui_typecheck.txt` |

## DoD -> code -> test

1. **Single chokepoint, human-must-act triggers.** `log_service.py:70` calls the fan-out after the
   build-log row commits. Trigger map `core/notification_vocab.py:29`; exact `task_status` parse
   `notification_vocab.py:69-83`. Tests: `test_trigger_selection_against_the_real_event_vocabulary`
   (including the negative `review_notes` case).
2. **Desktop channel and payload.** `notification_service.py:169` prefers `notify-send` (with
   `--action=default=Open task`), else a typed `gdbus` Notifications call; `_markup` (`:159`) escapes
   the body and emits a real `<a href>`; `_payload` (`:100`) carries task/run ids, outcome, next action
   and `http://<host>:<port>/tasks/<id>`; everything goes through `redact`. SPA `/tasks/<id>` is
   served by `api/main.py` and already pinned by `test_api.py::test_fallback_ui_served`.
3. **Persistent list, off vs failed.** `models.py:409` + `migrations/versions/0010_notifications.py`;
   `GET /api/notifications` (`api/routes/notifications.py:23`); `skipped` has
   `master_disabled`/`trigger_disabled`, `failed` carries the channel error. Tests:
   `test_off_by_default_records_skip_and_delivers_nothing`, `test_no_client...`,
   `test_notifications_endpoint_serves_the_persistent_list`.
4. **Settings default off + per-event toggles.** `settings_service.py:41,78`; Settings card
   `web/src/pages/Settings.tsx`; README `README.md:47`; `docs/configuration.md:52`.
5. **Failure-proof.** The fan-out opens its own session (`notification_service.py:262`), records a
   `fanout_error` row (`:244`), and `log_service.py:77-81` rolls back only if the caller's session is
   already inactive, then logs a warning. Tests: `test_failures_never_lose_the_build_log_or_poison_the_session`,
   `test_a_bug_inside_the_fanout_leaves_one_durable_failed_row`.
6. **Spam control.** Unique `dedupe_key` per run+trigger (`models.py:418`); one global pending flush
   (`notification_service.py:189`) coalesces parallel runs; noisy triggers default false. Tests:
   `test_parallel_runs_coalesce_and_a_trigger_is_deduped`, `test_noisy_events_are_silent_until_enabled`.
7. **Offline e2e + build.** Mock-harness completion: `test_completed_mock_run_yields_one_coalesced_notification`;
   cancellation: `test_cancelled_run_emits_run_cancelled_once_and_fans_out`; logs above; tsc above.
8. **`run_cancelled`.** Emitted once in the cancel branch (`run_service.py:1091`) before handoff
   salvage, with run short id and exit code; vocabulary updated in `docs/build-loop.md:501,509`;
   contract updated in `docs/frozen-contracts.md:92` via
   `docs/contract-change-request-T9-01-notifications.md`. Tested by the cancellation test and the
   updated `test_handoff_salvage.py` assertion.
9. **Honest duration.** `duration_details` (`notification_service.py:45`) reads
   `run.json.process_ms` + DB `duration_ms`, adds `clock suspended` only above 25% of wall. Tests:
   `test_duration_reports_compute_and_wall_and_only_names_a_large_freeze` and the e2e completed-run
   assertion that the live terminal body contains `compute / ... wall`.

## Reviewer findings addressed

- Session poisoning / unrecorded failure: own session + `fanout_error` row + conditional rollback.
- Timer slot/mid-flush events: `_timer` cleared before delivery; `flush_pending` wired at API
  startup (`api/main.py:117`) and tested by `test_startup_recovery_delivers_rows_a_dead_cli_process_left_pending`.
- Duration evidence: mock-harness e2e asserts the live terminal body carries both windows.
- Clickable link/markup: action + escaped anchor + typed gdbus arguments, asserted in
  `test_channel_actions_markup_and_no_client`; `/tasks/<id>` SPA target already tested.
- Parallel-run stampede: one global pending flush, asserted by the parallel coalescing test.
- Frozen contracts: CCR-T9-01 + `docs/frozen-contracts.md` updated for the table, route and
  `run_cancelled`.
- Trigger substring: exact transition-format parse, negative `review_notes` assertion.
- GUI build: `gui_typecheck.txt` (tsc exit 0); the Supervisor build command is not configured, so
  its check is `not_applicable` by design, not a Worker claim.