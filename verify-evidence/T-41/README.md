# T-41 verification evidence

Each evidence file starts with `# HEAD=<sha> tracked-and-untracked-entries-outside-this-dir=N`.
`N=0` means the command ran against the commit named there with no uncommitted input outside this
directory. The evidence commit itself touches only `verify-evidence/T-41/`, so the code it
certifies is that stamped HEAD.

| file | proves |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` -> full suite counts, imports resolved inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` -> `All checks passed!` |
| `t41_tests.txt` | T-41 focused tests, `-vv`, each named `PASSED` (offline fake client / `httpx.MockTransport`) |
| `tsc_web.txt` | React `web/src` typechecks (`tsc -b --force`); scratch project, no worktree write |
| `source_anchors.txt` | committed-HEAD file:line anchors for each DoD claim |

## DoD -> evidence

1. **Protocol recorded with measured evidence.** `docs/cloud-sync.md` section
   `## Version protocol and rejected transitions (T-41)` states that the version is remote
   per-entity compare-and-swap state, that `sync_state` has no version row by design, and that
   `0 -> 3` means the local dirty revision was sent instead of `synced_version + 1`; it quotes
   the measured 7,373 log occurrences / 1,426 `sync_entities` rows / `device_id`-only
   `sync_state`. Source anchors in `sync_transport.py` show the RPC-derived `next_version`.
2. **Recoverable or explicitly terminal.** `sync_transport.py` sends
   `p_expected_version = synced_version` and `p_version = synced_version + 1` (regression test
   `test_push_sends_synced_version_plus_one_not_the_local_revision`); `sync_service.reinitialise`
   plus `orchestrator sync reinit --dry-run|--apply` repairs a version disagreement from remote
   truth (`test_reinit_dry_run_then_apply_realigns_a_rejected_version`).
3. **Classification, backoff, cap, stopped state, status.** `sync_runner.py` classifies 4xx as
   permanent and 5xx/408/425/429/network as transient; `backoff_seconds` doubles 30 -> 900 s;
   permanent errors and the consecutive-failure cap set `sync_health.stopped` and a reason.
   `runner.status` and `GET /api/sync/status` expose `stopped`, `stopped_reason`,
   `consecutive_failures`, `last_failure_kind`, `next_retry_at`; `orchestrator sync status` merges
   that record. Tests: `test_a_permanent_400_stops_the_loop_and_records_the_stopped_reason`,
   `test_transient_failures_back_off_and_stop_at_the_consecutive_cap`,
   `test_sync_status_reports_the_stopped_reason_and_failure_streak`,
   `test_sync_status_reports_a_stopped_streak`.
4. **Operator-visible notification and GUI.** `notification_vocab.py` adds the non-noisy
   `sync_failed` trigger (default subscribed), and `_notify_if_needed` emits at most one event
   when a transient streak becomes persistent and one when it stops, through
   `log_service.log_event` -> the T-9 fan-out (master switch and per-trigger subscription
   respected). React `Settings.tsx` shows a red `stopped` badge, reason, failure count and next
   retry; the fallback UI's settings view shows the same stopped reason. Tests:
   `test_a_stopped_sync_emits_one_sync_failed_notification_per_streak`,
   `test_a_stopped_sync_respects_the_notification_master_switch`.
5. **Log damage bounded.** `_record_failure`'s docstring states the rule: the log line is keyed by
   the exact `<exception type>: <message>` signature; the first occurrence logs with a traceback,
   identical repeats only increment `sync_health.log_occurrences` at DEBUG, a new signature logs
   again. Test: `test_identical_failures_log_once_with_an_occurrence_counter` asserts one ERROR
   record for two identical failures and `log_occurrences == 2`.
6. **Offline tests (a)-(f).** All are in `tests/unit/test_sync_resilience.py` except the API/CLI
   status assertions in `tests/integration/test_sync_api.py` and `tests/integration/test_cli.py`.
   (a) permanent 400 stops and records; (b) backoff sequence + transient cap; (c) dedupe counter;
   (d) CLI and API status; (e) dry-run/apply reinit then a legal push; (f) an enabled sync that
   has never run initialises and succeeds. No network: fake clients and `httpx.MockTransport`.
7. **Docs.** `docs/cloud-sync.md` covers protocol, rejection meaning, reinit, stop/notify and the
   dedupe rule; `README.md` Known limitations states what cloud sync does not guarantee and how a
   stop is surfaced/recovered; `docs/configuration.md` lists `sync_failed` as subscribed by
   default; `docs/decisions.md` records the decision.
8. **Full suite and lint via `./scripts/orch_verify.sh`**, committed here with this map and
   stamped at the code HEAD in `full_suite.txt` / `lint.txt`.
