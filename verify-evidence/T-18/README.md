# T-18 evidence — truthful outcomes, probed capabilities, durable pending rows

Code HEAD `46bb10b` (`fix(phase-18): make the pending-row claim unique, full-path-long and
compare-and-set`). The full suite, focused `-vv` run, lint and both live scripts ran at this SHA;
the later evidence commit changes only `verify-evidence/`, and `.orchestrator/` is gitignored.
At capture time `git status --porcelain -uall` outside this directory was empty
(`tracked+untracked entries outside this home=0` is also stamped inside every scripted capture).

## Raw captures (all re-run, nothing compressed)

| File | Command | Proving line |
|---|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` | `:7` ORCH_VERIFY marker; `:22` `740 passed, 10 skipped in 132.60s` (exit 0) |
| `lint.txt` | `./scripts/orch_verify.sh lint` | `:11` `All checks passed!` (exit 0) |
| `per_test.txt` | `./scripts/orch_verify.sh test tests/unit/test_notifications.py tests/integration/test_notifications.py tests/integration/test_notification_delivery.py -vv` | `:72` `52 passed in 5.15s`; each T-18 test named below |
| `live_delivery.txt` | `/tmp` capture script, real `notify-send`, throwaway `ORCHESTRATOR_HOME` | `:4` direct delivery `delivered`; `:8` one deliver call for two runs; `:10` `failed_rows: []`; `:11` `delivered_rows: [1]` |
| `mutation_check.txt` | `/tmp` capture script, runtime mutations only (no committed file edited) | M6-M9 (round 5 findings) plus M1/M2/M5 each show shipped vs mutant |
| `zz_dod_map.txt` | index (sorts last in the review diff) | DoD -> file:line -> log line |

The two `/tmp` scripts are not committed: they are throwaway harnesses, and the outputs above are
the evidence. They wrote no file inside the worktree.

## DoD map

1. **Truthful outcome classes.** `src/orchestrator/services/notification_service.py:43-49` declares
   `OUTCOME_CLASSES` and maps `shown_unacknowledged -> delivered` (CCR-T9-01 vocabulary unchanged);
   `:320-334` `_classify()` maps a launched-then-timed-out client to `shown_unacknowledged` with a
   `delivery_note`, a spawn error/non-zero exit to `failed`, and an id/stdout to `delivered`;
   `:462-506` `_write_outcome()` writes status/reason/details under the claim's compare-and-set.
   Tests: `per_test.txt:40-46`
   (`test_every_transport_result_maps_to_one_frozen_status` 5 cases,
   `test_a_displayed_message_is_recorded_delivered_not_failed`,
   `test_a_client_timeout_after_spawn_records_the_unknown_display_caveat`; the four
   standalone outcome classes at `:47-50`). Live:
   `live_delivery.txt:4,11` (`failed_rows: []` at `:10`).
2. **Probed capabilities, no advertised action.** `:274` `probe_capabilities()` calls
   `GetCapabilities` (2 s timeout, fails closed); `:316` `_body_for()` escapes on `body-markup` and
   emits `<a href>` only on `gdbus` + `body-hyperlinks`; `:343-356` `deliver()` sends no `--action`
   and `[]`/`{}` on gdbus. Tests: `per_test.txt:38-39`
   (`test_probe_parses_capabilities_and_fails_closed`,
   `test_markup_daemon_gets_escaped_text_on_every_transport_and_no_hint`). Observed capability list:
   `live_delivery.txt:3`.
3. **Pending rows never stranded, and a claim is not revocable mid-delivery.** Read path
   `src/orchestrator/api/routes/notifications.py:31`; CLI exit `src/orchestrator/cli.py:1446,1458`;
   claim `notification_service.py:427-460` (conditional `UPDATE ... WHERE status='pending' AND
   reason=''`, marker `claimed_by:<process>:<uuid>:<ns>`), stale release `:397-425`, full-path
   window `:60-66` (`PROBE 2 + CLIENT 10 + MARGIN 5 = 17s`), compare-and-set terminal write
   `:462-506`, owner-scoped exit delivery + terminal `failed/process_exit_undelivered` for this
   process's rows `:573-620`. Tests: `per_test.txt:56-62`
   (`test_two_flushers_race_and_only_the_claim_winner_delivers`,
   `test_a_flusher_whose_claim_was_revoked_mid_delivery_writes_nothing`,
   `test_the_stale_window_covers_the_whole_delivery_path`,
   `test_two_claims_in_the_same_instant_are_distinguishable`,
   `test_a_live_claim_is_not_reclaimed_mid_delivery`,
   `test_an_expired_claim_is_reclaimed_after_the_whole_delivery_path`,
   `test_a_dead_flushers_claim_is_released_instead_of_stranding_the_row`), `:63-64` for the exit
   scope, `:68` read path, and `:69` the CLI-exit row created through `notify_event` itself
   (`test_cli_exit_delivers_a_row_the_production_path_created`: no hand-stamped fixture). Mutation
   proof: `mutation_check.txt` M6 (revoked write), M7 (window), M8 (token), M9 (owner stamp).
4. **Cross-run coalescing.** `notification_service.py:423-446` folds every claimed pending row into
   one message with the `N updates` title while `dedupe_key` keeps one row per run+trigger. Test
   `per_test.txt:52` (`test_two_runs_finishing_together_produce_one_message`): one deliver call,
   two DIFFERENT `run_id`s, statuses `delivered`+`coalesced`. Live, cross-run:
   `live_delivery.txt:5-9` (two runs pending -> `deliver_calls: 1`, title `2 updates`,
   `distinct_run_ids: [1, 2]`). Standalone variant: `per_test.txt:51`
   (`test_two_standalone_runs_coalesce_into_one_message`).
5. **Deep link by call; offline transport fakes.** Fallback half is a real request:
   `tests/integration/test_notifications.py:203-227` calls `GET /tasks/7` with `web/dist` absent and
   asserts `data-route == {"view": "task", "arg": "7"}` (`per_test.txt:67`). React-router half is
   route-table inspection of `web/src/App.tsx` only — **honest partial per operator ruling 3**:
   this repo has no JS test runner and adding one is T-31 scope; the old
   `assert "test" not in web/package.json["scripts"]` pass-condition is gone. Recorded in
   `README.md:266` and in the handoff. Offline fakes: autouse
   `_no_real_notification_daemon` in `tests/conftest.py` plus `_clients()` in
   `tests/unit/test_notifications.py` (no daemon, no D-Bus, no network; mutation proof
   `mutation_check.txt` M2/M5).
6. **Docs.** `docs/configuration.md:73-134`: master default off, every status meaning incl. the
   timeout caveat, capability requirements, observed vs expected per-daemon table, claim/flush
   rules; `:121` says dunst and others were **not** probed. `README.md:257-269`: only
   `notify-send` live-tested, gdbus fake-transport only, unprobed daemons, and the route-table-only
   deep-link limitation.

## Notes

- `delivered` is written live: `live_delivery.txt:4,11` (`delivered_rows: [1]`), with reason
  `shown_unacknowledged` because `notify-send` reports no acknowledgement.
- Round 5 (operator ruling 2026-11-08) changed only the claim: the window now covers the 2s
  capability probe as well as the 10s client call, the claim identity carries a per-attempt uuid and
  a nanosecond stamp, and every terminal write is a compare-and-set on the marker. Proof: the six
  claim tests at `per_test.txt:56-62` and `mutation_check.txt` M6-M9. `docs/configuration.md:126-136`
  was updated to state the new window and the compare-and-set rule, because the old text said
  "client timeout plus five seconds" and would otherwise be false.
- The CLI-exit test no longer hand-stamps an owner: `per_test.txt:69` builds its row with
  `notify_event` and asserts both the production owner stamp and the exit delivery. Removing either
  half makes it red (`mutation_check.txt` M9).
- A client that is spawned and then killed at the timeout is `shown_unacknowledged` with
  `details_json.delivery_note` (`mutation_check.txt` M2); the alternative documented in the
  operator ruling (keep `failed`) was not chosen, and `docs/configuration.md` states the choice.
- No status value was added and no contract-change request is needed.