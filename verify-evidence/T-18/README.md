# T-18 evidence — truthful notification outcomes, probed capabilities, durable pending rows

Stamped at code HEAD `987295cd873ca77957a5f909f4b4bda385ea683f`. `zz_evidence.txt` carries the
`ORCH_VERIFY` marker, the suite/lint result, the per-test names and both live transport runs. This
evidence commit changes only files under `verify-evidence/T-18/`.

## DoD map

1. **Truthful outcome classes.** `OUTCOME_CLASSES`/`STATUS_FOR_OUTCOME` at
   `src/orchestrator/services/notification_service.py:49,53` map `delivered`,
   `shown_unacknowledged`, `failed`, `skipped`, `coalesced` to the frozen CCR-T9-01 statuses;
   `TransportResult.reason` (:234) carries the finer reason; `_deliver_pending_locked` (:387)
   writes status/reason/outcome_class. No `--action` and no action array is sent (:346/374).
   Tests: `test_every_transport_result_maps_to_one_frozen_status`,
   `test_a_displayed_message_is_recorded_delivered_not_failed`,
   `test_the_standalone_path_reaches_the_same_outcome_classes`,
   `test_channel_actions_markup_and_no_client`. Live: rows 1–2 are `delivered`, 0 failed, and the
   gdbus call returns a notification id.
2. **No unadvertised capability.** `probe_capabilities` (:277) calls `GetCapabilities`;
   `_body_for` (:325) escapes the body on **both** transports whenever `body-markup` is advertised
   (a markup daemon parses every body), drops the non-spec `body-markup` hint (the gdbus call sends
   `{}`), and adds an anchor only on gdbus with `body-hyperlinks`. Tests:
   `test_probe_parses_capabilities_and_fails_closed`,
   `test_markup_daemon_gets_escaped_text_on_every_transport_and_no_hint`. Live: notify-send rows are
   `markup-escaped`; the forced-gdbus call is `markup+hyperlink`; both have no failed row.
   Docs/configuration.md:96-111 maps freedesktop-compliant (quickshell/caelestia), mako/dunst
   defaults, `notify-send`, and unprobed daemons to the body shape each receives.
3. **Pending rows are never lost.** Read path: `flush_pending_if_any` (:470) called from
   `src/orchestrator/api/routes/notifications.py:32`. CLI exit: `cli.main` (:1442) always calls
   `_flush_notifications_at_exit` (:1449); `flush_pending_at_exit` (:515) delivers then
   `_terminate_pending_locked` (:478) writes `failed`/`process_exit_undelivered`, scoped by
   `details_json.owner` to rows **this process** created (:486), so a live server's rows stay
   pending. Inert without a database (`_database_ready`, :500). Tests:
   `test_reading_the_list_flushes_rows_the_server_left_pending`,
   `test_cli_exit_delivers_and_terminates_only_its_own_rows`,
   `test_exit_flush_delivers_own_rows_and_terminates_only_what_it_owns`,
   `test_exit_flush_delivers_a_pending_row`, `test_exit_flush_is_inert_before_the_database_exists`,
   `test_python_dash_m_and_the_console_script_route_through_cli_main` (runs `python -m
   orchestrator` in a child with `cli.main` replaced by a sentinel, and asserts pyproject's console
   script is `orchestrator.cli:main`).
4. **Coalescing across runs.** One global flush folds every pending row into one message
   (`_deliver_pending_locked`, :387) and marks the rest `coalesced` with `coalesced_into`;
   `dedupe_key` still allows at most one row per run per trigger. Tests:
   `test_parallel_finishes_across_runs_are_one_message` (5 runs → one "5 updates" message, 4
   coalesced, 5 distinct keys) and the pre-existing `test_parallel_runs_coalesce_and_a_trigger_is_deduplicated`.
5. **Deep link by call.** `test_the_task_link_is_a_real_target_when_web_dist_is_absent`
   (`tests/integration/test_notifications.py:204`) requests `/tasks/7`, reads the route the server
   resolved into the page (`data-route` from `src/orchestrator/api/fallback.py:11,21` served at
   `api/main.py:225`), asserts the page has the `task` view, and matches the same URL against the
   route table `web/src/App.tsx` declares. No JS runner exists in `web/package.json`, and the test
   asserts that so a future runner forces a real render call. Transport fakes are offline: every
   test module patches `_run_client`/`probe_capabilities`; `tests/conftest.py` blocks the real
   client for the whole suite.
6. **Docs.** `docs/configuration.md` (master default, status table, capability requirements, the
   per-daemon body-shape table, flush points) and `README.md` Known limitations (one tested channel,
   escaped markup text, text link, no ActionInvoked).

## Reviewer findings this round

1. Markup daemon got unescaped text and a non-spec hint → escaped on both transports, gdbus sends
   `{}`; `details_json.body_shape` is `markup-escaped`/`markup+hyperlink`/`plain`.
2. No per-daemon mapping → table at `docs/configuration.md:103`.
3. Exit flush only proven through `cli.main` → child-process `python -m orchestrator` sentinel plus
   pyproject console-script assertion.
4. Exit termination was unscoped → owner token, scoped termination; a live server's row stays
   pending (test above).
5. React route asserted by regex only → stated and asserted that no JS runner exists.
6. Evidence header printed the directory name → both live captures now print `git rev-parse HEAD`.
7–9. Harness-detected diff completeness (README, docs, tests/unit): see the overflow note below.

## Diff-budget overflow (T-16, not T-18 scope)

The branch diff is larger than `ORCHESTRATOR_MAX_DIFF_TOKENS=12000`. The source group can spend only
`available - COMPENSATION_BUDGET_FRACTION` = ~7800 tokens because `verify-evidence/` is present, and
the evidence group gets the other third. The truncation manifest in the run's `review_payload.md`
therefore records the files whose sections the rendered diff could not carry; the decisive
file:line excerpts for every DoD item are inlined above and in `zz_evidence.txt`, and the omitted
source is committed on this branch, not missing. T-18 does not change the review budget (T-16 owns
it); no `.gitattributes`/`-diff` was added.