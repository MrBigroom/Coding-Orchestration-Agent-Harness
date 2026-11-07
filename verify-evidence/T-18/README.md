# T-18 evidence — a notification record can say failed or delivered without either being true

Stamped at HEAD `236978dfc66d92b4595241631750228e853a2862` (the code commit before this evidence
commit; the evidence commit changes only files under `verify-evidence/T-18/`). Every count below
belongs to that SHA: `full_suite.txt` and `lint.txt` print the `ORCH_VERIFY` marker line with the
resolved module path inside the worktree, and their first line names the SHA and the dirty-entry
count (0) at run time.

| File | What it proves |
|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` at the stamped HEAD: **726 passed, 10 skipped, 0 failed**; `ORCH_VERIFY\|mode=test\|…` shows `orchestrator.__file__` inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` at the same HEAD: `All checks passed!`, `exit=0` |
| `per_test.txt` | Every test in the two notification modules by name (`-vv`; a single `-v` cancels pyproject's `-q`): 38 passed |
| `live_delivery.txt` | Real daemon on the dev machine, throwaway `ORCHESTRATOR_HOME` under `/tmp`: three real deliveries, **no `failed` row**; the live installation read `mode=ro` shows the before-state (49 failed, all `timed out after 10s`, 0 delivered) |
| `zz_dod_map.txt` | DoD item → file:line + test name, small and sorts last so it survives a truncated review diff |

## DoD self-check, one line each

1. **Truthful record.** `OUTCOME_CLASSES` / `STATUS_FOR_OUTCOME`
   (`src/orchestrator/services/notification_service.py:56`, `:63`) name all five classes and map
   each to a frozen CCR-T9-01 status; `TransportResult.reason` (`:232`) carries the finer
   distinction; `_deliver_pending_locked` (`:424`) writes `status`/`reason`/`details_json.outcome_class`
   from the transport result. No `--action`/action array is passed anywhere (`deliver`, `:371`).
   Tests: `test_every_transport_result_maps_to_one_frozen_status`,
   `test_a_displayed_message_is_recorded_delivered_not_failed`,
   `test_the_standalone_path_reaches_the_same_outcome_classes`,
   `test_channel_actions_markup_and_no_client`. Live: `live_delivery.txt` rows 1–3 are
   `delivered` with `shown_unacknowledged` / `delivered` classes and no error.
2. **No unadvertised capability.** `probe_capabilities` (`:291`) calls `GetCapabilities`; `_body_for`
   (`:353`) emits markup+`<a href>` only on `gdbus` *and* only when the daemon advertises both
   `body-markup` and `body-hyperlinks`, otherwise the plain body with the copyable `Open: <url>`
   line; `probed=False` ⇒ plain. No action is passed. Tests:
   `test_probe_capabilities_parses_what_the_daemon_advertises`,
   `test_a_daemon_that_advertises_no_hyperlink_gets_a_plain_body`,
   `test_channel_actions_markup_and_no_client`. Live: `live_delivery.txt` call 3 sends
   `{'body-markup': <true>}` with the anchor to a daemon whose probe advertised both; call 2 sends
   `{}` because the event had no link.
3. **Pending rows are not lost.** `flush_pending_if_any` (`:516`) is called by the read path
   (`src/orchestrator/api/routes/notifications.py:32`); `flush_pending_at_exit` (`:561`) +
   `_terminate_pending_locked` (`:524`) run from `cli.main` (`src/orchestrator/cli.py:1442`,
   `:1449`) and write `failed`/`process_exit_undelivered` for what could not be delivered; the
   exit flush is inert without a `notifications` table (`_database_ready`, `:542`). Tests:
   `test_the_exit_flush_delivers_pending_rows`,
   `test_the_exit_flush_terminates_rows_it_cannot_deliver`,
   `test_the_exit_flush_is_inert_before_the_database_exists`,
   `test_reading_the_list_flushes_rows_the_server_left_pending`,
   `test_cli_exit_delivers_rows_the_process_left_pending`,
   `test_cli_exit_terminates_rows_it_cannot_deliver`.
4. **Coalesce across runs.** `_deliver_pending_locked` folds every pending row of a flush into one
   message and marks the rest `coalesced` with `coalesced_into`; `_schedule` (`:474`) debounces the
   window per new row (2s) with a 10s cap (`COALESCE_SECONDS`/`COALESCE_MAX_SECONDS`, `:36`, `:39`)
   so a batch straddling the boundary is still one message; `dedupe_key` still allows at most one
   row per run per trigger. Tests: `test_parallel_finishes_across_runs_are_one_message`,
   `test_a_batch_that_straddles_the_window_still_produces_one_message`,
   `test_the_debounce_never_delays_a_batch_past_the_cap`,
   `test_parallel_runs_coalesce_and_a_trigger_is_deduped`.
5. **Deep link by call, offline transport fakes.** `test_the_task_link_resolves_in_the_fallback_ui_and_the_react_router`
   (`tests/integration/test_notifications.py:327`) requests `/tasks/7`, parses the route the server
   resolved into the page (`data-route`, `src/orchestrator/api/fallback.py:19`, `:35`), asserts the
   page has the `task` view, and matches the same URL against the `<Route>` table in
   `web/src/App.tsx`. Every outcome class has a fake-transport test
   (`test_every_transport_result_maps_to_one_frozen_status`, plus the probe and markup tests); the
   suite cannot reach a daemon at all because `tests/conftest.py::_no_real_notification_daemon`
   blocks `_run_client` and `probe_capabilities` for every test.
6. **Docs.** `docs/configuration.md` → "Notification delivery and status (T-18)" states the master
   default, the outcome-class→status table, the capability requirements, what a daemon without
   `body-hyperlinks` receives, the probe result observed on this machine, and every pending-flush
   point. `README.md` → Known limitations, first bullet, names the single channel tested and that
   the link is text on the `notify-send` path.

## Deliberate, documented scope calls

- `shown-unacknowledged` is **not** a new status value: it maps to `delivered` with
  `reason=shown_unacknowledged` + `details_json.outcome_class`. CCR-T9-01 froze the vocabulary, T-18
  did not grant a change, so no CCR file was written and the vocabulary is untouched.
- `notify-send` always gets the plain body: it attaches no `body-markup` hint, so an anchor through
  it would render as a literal tag. The clickable anchor is therefore a `gdbus`-only shape, and the
  `notify-send` path is plain by design (README says so).
- The suite proves the transports with fakes; the only real daemon calls in this round are the
  three in `live_delivery.txt`, run outside pytest on a throwaway `ORCHESTRATOR_HOME`.