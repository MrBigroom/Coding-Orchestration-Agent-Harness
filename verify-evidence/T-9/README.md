# T-9 verification evidence

Everything here was produced from this worktree at the code HEAD stamped inside each file. The
evidence commit that adds these files changes only these files; `full_suite.txt` names the sha the
suite ran at, which is the code HEAD (the commit before the evidence commit).

| file | what it proves |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` -> `600 passed, 6 skipped`, `suite_exit=0`, with the ORCH_VERIFY marker line proving `orchestrator.__file__` resolved inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` -> `All checks passed!`, `lint_exit=0` |
| `notifications_tests.txt` | the two notification test files run with `-vv` -> 18 named tests passed |
| `gui_typecheck.txt` | `tsc -b --force` exit 0, `readlink -f` proof the compiler read THIS worktree's `web/src`, and the list of 30 sources it read |
| `mutation_checks.txt` | four in-place mutations of committed source, each turning exactly the guarding test red, each restored with `git checkout --` |
| `zz_review_index.txt` | round-1 review findings answered per finding with file:line + the test that pins each |

## DoD -> evidence

1. Fan-out at the single chokepoint, trigger set, individually disable-able
   `src/orchestrator/services/log_service.py:64-89` calls the fan-out after the row and JSONL line
   are committed; `src/orchestrator/core/notification_vocab.py:29-66` is the trigger table
   (one `notification_<key>` setting each); `settings_service.py:38-42` registers them.
   Pinned by `tests/unit/test_notifications.py::test_trigger_selection_against_the_real_event_vocabulary`
   and `::test_noisy_events_are_silent_until_enabled` (log line: `notifications_tests.txt`).
2. freedesktop D-Bus channel, content, redaction, clickable link
   `notification_service.py:146-190` (notify-send, else gdbus, else a recorded error),
   `:162-170` markup escaping + anchor, `:103-133` payload, `:48-68` duration.
   Pinned by `::test_channel_actions_markup_and_no_client`, `::test_payload_shape_link_and_redaction`
   (secret `sk-ant-...` is absent from title/body/link and `[REDACTED]` is present), and
   `tests/integration/test_notifications.py::test_the_task_link_is_a_real_target_when_web_dist_is_absent`.
3. Persistent list, skipped vs failed
   `src/orchestrator/core/models.py` `Notification` (statuses incl. `skipped`/`failed`),
   `migrations/versions/0010_notifications.py`, `api/routes/notifications.py`;
   `notification_service.py:320-331` records `skipped` with `master_disabled` /
   `trigger_disabled` and `failed` only after an attempt.
   Pinned by `::test_off_by_default_records_skip_and_delivers_nothing` and
   `tests/integration/test_notifications.py::test_notifications_endpoint_serves_the_persistent_list`.
4. Settings: master + per-trigger, PATCH, Settings page switch, default off, documented
   `settings_service.py:38-42` + `:78-82`; `web/src/pages/Settings.tsx`;
   `README.md` "Desktop notifications (off by default)"; `docs/configuration.md` runtime settings.
   Pinned by `::test_off_by_default_records_skip_and_delivers_nothing`
   (`settings_service.defaults()[MASTER_SETTING] is False`).
5. Fire-and-forget, failure-proof, recorded at most once
   `log_service.py:74-89` (unconditional rollback, durable `record_failure`, line already written);
   `notification_service.py:247-263` (`flush_pending` never raises), `:266-288` (deduped failure row).
   Pinned by `::test_failures_never_lose_the_build_log_or_poison_the_session`,
   `::test_a_failing_fanout_rolls_back_a_poisoned_caller_session`,
   `::test_flush_never_raises_and_never_poisons_a_caller_session`,
   `::test_a_bug_inside_the_fanout_leaves_one_durable_failed_row`; mutations M1 and M3.
6. Spam control
   `notification_service.py:29` one global window, `:192-224` deliver all pending as one popup and
   mark the rest `coalesced`; dedupe_key is per run per trigger; noisy triggers default off.
   Pinned by `::test_parallel_runs_coalesce_and_a_trigger_is_deduped`,
   `::test_noisy_events_are_silent_until_enabled`, and mutation M2 for the timer.
7. Tests + `make test` / `make lint` through the wrapper + GUI build + fallback UI
   `full_suite.txt`, `lint.txt`, `gui_typecheck.txt`, and
   `test_the_task_link_is_a_real_target_when_web_dist_is_absent` for the fallback UI (mutation M4).
8. `run_cancelled`
   `run_service.py:1089-1097`; vocabulary `docs/build-loop.md` (13 insertions / 7 deletions, each
   deletion a T-8 sentence that is stale once the event exists, per the answer in
   `zz_review_index.txt` #7). Pinned by
   `tests/integration/test_notifications.py::test_cancelled_run_emits_run_cancelled_once_and_fans_out`
   (exactly one event, run short id in details, fan-out fires).
9. Honest duration under suspend
   `notification_service.py:48-68` reports `compute / wall` and adds `clock suspended <gap>` only
   above the 25% threshold, phrased as information; the compute figure is read from the archived
   `run.json` (`run_service.py:1047`, written before the terminal transitions at 1074/1125).
   Pinned by `::test_duration_reports_compute_and_wall_and_only_names_a_large_freeze` and the
   end-to-end `::test_completed_mock_run_yields_one_coalesced_notification`, which asserts the
   delivered body carries the archived `process_ms` of that run.

## Contract surface

`docs/frozen-contracts.md` is byte-identical to the merge-base `d966bd8`. The table, the route and
`run_cancelled` are requested in `docs/contract-change-request-T9-01-notifications.md` for the
operator to approve at the merge gate; the branch does not edit a contract file.