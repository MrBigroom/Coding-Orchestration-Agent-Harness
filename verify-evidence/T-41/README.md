# T-41 verification evidence

Generated with `orchestrator task evidence T-41` from a clean worktree. That driver runs the
committed `capture.sh`, canonicalises the stamp on every file the pass rewrote to
`# HEAD=<sha> mode=<fast|full>`, refuses any uncommitted input, and writes `manifest.json` plus
`freshness_selfcheck.txt` itself. The stamped HEAD is the code commit measured; the evidence-only
commit that ships this bundle does not change code, so it does not stale the bundle.

`freshness_selfcheck.txt` is the driver's own record (no stamp by construction: it certifies the
files around it). `manifest.json` lists the five stamped outputs (`full_suite.txt`, `lint.txt`,
`t41_tests.txt`, `tsc_web.txt`, `source_anchors.txt`); `README.md`, the manifest and the self-check
are bookkeeping and are not hashed as measurements.

| file | proves |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` -> full-suite counts; `ORCH_VERIFY` shows imports resolved inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` -> `All checks passed!` |
| `t41_tests.txt` | T-41 focused tests with `-vv`, each named `PASSED` (offline fake client / `httpx.MockTransport`) |
| `tsc_web.txt` | React `web/src` typechecks (`tsc -b --force`); disclosed scratch project, no worktree write |
| `source_anchors.txt` | committed-HEAD file:line anchors, including the SQL RPC contract quoted verbatim |

## DoD -> evidence

1. **Protocol recorded, evidence not circular.** `docs/cloud-sync.md:42` (`## Version protocol and
   rejected transitions (T-41)`) states the counter is remote per-entity state, that `sync_state`
   has no version row by design, and what `0 -> 3` means; `:68` states the rejection meaning and
   `:73` records the re-measured 7,000 `0 -> 3` lines / 1,431 `sync_entities` rows / `device_id`-only
   `sync_state`. The authority for `p_version = expected + 1` is the migration itself:
   `source_anchors.txt` section `## supabase/migrations/0001_orchestrator_sync.sql:39-90` quotes the
   created `orchestrator_sync_push` function and its lines 62-63 rejection verbatim.
   `src/orchestrator/services/sync_transport.py:217` shows the RPC-derived `next_version`, sent at
   `:225`.
2. **Recoverable or explicitly terminal.** `sync_transport.py:217`/`:225` send
   `p_expected_version = synced_version` and `p_version = synced_version + 1`
   (`test_push_sends_synced_version_plus_one_not_the_local_revision`); `sync_service.py:276`
   `reinitialise` plus `cli.py:1607` `sync reinit --dry-run|--apply` repair a version disagreement
   from remote truth (`test_reinit_dry_run_then_apply_realigns_a_rejected_version`). A dirty row
   whose remote version is ahead is `remote_ahead_skipped` and untouched, so reinit cannot
   overwrite unseen remote changes (`test_reinit_never_rebaselines_a_dirty_row_past_unseen_remote_changes`).
3. **Classification, backoff, cap, stopped state, status.** `sync_runner.py:48` `backoff_seconds`
   is a capped delay; `:151` makes only a `SyncPermanentError` terminal; `:152` floors the base at
   the configured interval and raises the cap to it. So transient failures retry indefinitely with
   60/120/240/480/900/900 s at defaults (`test_default_transient_retries_reach_the_900_second_cap`)
   and never set `stopped` (`test_transient_failures_back_off_without_ever_stopping`). A permanent
   4xx records `stopped`/`stopped_reason` (`sync_service.py:236` `SYNC_HEALTH_KEY`, `:254`
   `health`). `sync_runner.py:119` status, `GET /api/sync/status` (`api/routes/sync.py`), and
   `cli.py:1594` `sync status` expose `stopped`, `stopped_reason`, `consecutive_failures`,
   `last_failure_kind`, `next_retry_at`. Tests: `test_a_permanent_400_stops_the_loop_and_records_the_stopped_reason`,
   `test_sync_status_reports_the_stopped_reason_and_failure_streak`,
   `test_sync_status_reports_a_stopped_streak`.
4. **Operator-visible notification and GUI.** `notification_vocab.py:55` adds the non-noisy
   `sync_failed` trigger. `sync_runner.py:188` `_notify_if_needed` emits at most one event when a
   transient streak becomes persistent and one when a permanent error stops the loop; `:254` calls
   it from `run_once`, so a manual `sync now` is covered too. The T-9 fan-out therefore respects the
   master switch and per-trigger subscription. React `web/src/pages/Settings.tsx:198` shows the
   stopped reason and `:200` gives reinit only for a permanent error; `:213` shows the transient
   retry/next-attempt line. Fallback UI: `api/static_fallback/index.html:213` chooses the same hint
   from `last_failure_kind`. Tests:
   `test_a_stopped_sync_emits_one_sync_failed_notification_per_streak`,
   `test_a_manual_sync_that_stops_permanently_still_notifies`,
   `test_a_persistently_failing_transient_sync_notifies_once`,
   `test_a_stopped_sync_respects_the_notification_master_switch`.
5. **Log damage bounded.** `sync_runner.py:130` `_record_failure` states the rule in its docstring:
   a line is keyed by exact `<exception type>: <message>`; `:146` compares the signature, `:147`
   increments `sync_health.log_occurrences`, the first occurrence logs once with a traceback
   (`:173`) and identical repeats log at DEBUG (`:181`). Test
   `test_identical_failures_log_once_with_an_occurrence_counter` asserts one ERROR for two
   identical failures and `log_occurrences == 2`.
6. **Offline tests (a)-(f).** `tests/unit/test_sync_resilience.py`,
   `tests/integration/test_sync_api.py`, `tests/integration/test_cli.py`. (a) permanent 400 stops
   and records; (b) the capped-backoff sequence plus the running loop retrying a 5xx; (c) dedupe
   counter; (d) CLI and API status; (e) dry-run/apply reinit then a legal push, plus the
   remote-ahead refusal; (f) an enabled sync that has never run initialises and succeeds. No
   network: fake clients and `httpx.MockTransport`.
7. **Docs.** `docs/cloud-sync.md` covers the SQL-backed protocol, rejection meaning, reinit,
   permanent-stop vs transient-retry/notify, and the dedupe rule; `README.md` Known limitations
   states what sync does not guarantee and how a stop or transient streak is surfaced; the
   `sync_failed` notification default is in `docs/configuration.md` and `docs/decisions.md`.
8. **Full suite and lint via `./scripts/orch_verify.sh`**, stamped at the code HEAD in
   `full_suite.txt` (905 passed, 10 skipped) / `lint.txt` (`All checks passed!`), with this map in
   the same evidence-only commit.
