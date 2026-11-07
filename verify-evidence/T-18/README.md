# T-18 evidence — truthful outcomes, probed capabilities, durable pending rows

Code HEAD `e23eb958` (suite ran there; later commits change only `verify-evidence/`);
`tracked-and-untracked-entries-outside-this-dir=0`.

`./scripts/orch_verify.sh test` -> **726 passed, 10 skipped**, exit 0 (120.53s).
`ORCH_VERIFY|mode=test|module=orchestrator|module_file=/…/worktrees/coding-orchestrator/18/src/orchestrator/__init__.py|worktree_root=/…/worktrees/coding-orchestrator/18`
`./scripts/orch_verify.sh lint` -> `All checks passed!` (exit 0).
PASSED (focused -vv, 38):
`test_probe_parses_capabilities_and_fails_closed` PASSED
`test_markup_daemon_gets_escaped_text_on_every_transport_and_no_hint` PASSED
`test_every_transport_result_maps_to_one_frozen_status` (5 parametrized cases) PASSED
`test_a_displayed_message_is_recorded_delivered_not_failed` PASSED
`test_the_standalone_path_reaches_the_same_outcome_classes` PASSED
`test_exit_flush_delivers_and_terminates_only_real_rows_it_owns` PASSED
`test_reading_the_list_flushes_rows_the_server_left_pending` PASSED
`test_cli_exit_delivers_its_pending_rows` PASSED
`test_the_task_link_is_a_real_target_when_web_dist_is_absent` PASSED

## DoD map (slices at the HEAD above)

1. **Truthful classes.** `notification_service.py:41-49` maps `shown_unacknowledged` ->
   `delivered` (vocabulary unchanged); `:342-343` writes status/reason. `:296-324` sends **no**
   `--action` and `[]`/`{}` on gdbus: only an error/timeout/missing client is `OUTCOME_FAILED`.
2. **Probed capability.** `:245-264` calls `GetCapabilities` and fails closed; `:285-294` gives
   unprobed/no-markup raw `plain`, markup escaped text on both transports, an anchor only on gdbus
   with `body-hyperlinks`; no `body-markup` hint. Per-daemon table: `docs/configuration.md:73-110`.
3. **Pending rows.** Read path `api/routes/notifications.py:31`; CLI exit `cli.py:1442-1465`;
   owner stamped at `notification_service.py:536`; scoped termination `:403-419`; inert without a
   database file `:424-427`. Tests drive `cli.main()` and the API route; a real `notify_event` row
   carries the token and only it is terminated.
4. **Coalescing across runs.** `_deliver_pending_locked:325-349` folds every pending row into the
   first; `dedupe_key` dedupes per run+trigger. Live: one flush, 2 triggers -> 1+1 coalesced.
5. **Deep link by call.** `api/main.py:224-229`; the fallback consumes `data-route`
   (`static_fallback/index.html:35,51,197-200`). The test calls `/tasks/7`, asserts
   `{"view":"task","arg":"7"}` and React's declared `/tasks/:id` route (no JS runner exists).
6. **Docs.** `docs/configuration.md:73` (default, statuses, capabilities, per-daemon shape, flush
   points); `README.md:257` (one tested channel, escaped markup, text link, no action).

## Live acceptance (real daemon, throwaway home)

`probe: GetCapabilities probed=True caps=actions,body,body-hyperlinks,body-images,body-markup,icon-static,persistence`
`1 run_timeout delivered shown_unacknowledged notify-send markup-escaped`
`2 task_review coalesced coalesced_into:1` — `failed_rows: []`, `delivered_rows: [1]`.
