# T-41 verification evidence

Generated with `orchestrator task evidence T-41` from a clean worktree. That driver runs the
committed `capture.sh`, canonicalises the stamp on every file the pass rewrote to
`# HEAD=<sha> mode=<fast|full>`, refuses any uncommitted input, and writes `manifest.json` plus
`freshness_selfcheck.txt` itself. The stamped HEAD is the code commit measured; the evidence-only
commit that ships this bundle does not change code, so it does not stale the bundle.

`freshness_selfcheck.txt` is the driver's own record (no stamp by construction: it certifies the
files around it) and reports the stamped-file count. `manifest.json` hashes every file this pass
produced: the stamped outputs (`full_suite.txt`, `lint.txt`, `t41_tests.txt`, `tsc_web.txt`,
`source_anchors.txt`, `live_measurement.txt`) plus the unstamped `freshness_selfcheck.txt`;
`README.md` and the manifest itself are bookkeeping and are not hashed as measurements.

| file | proves |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` -> full-suite counts; `ORCH_VERIFY` shows imports resolved inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` -> `All checks passed!` |
| `t41_tests.txt` | T-41 focused tests with `-vv`, each named `PASSED` (offline fake client / `httpx.MockTransport`) |
| `tsc_web.txt` | React `web/src` typechecks (`tsc -b --force`); disclosed scratch project, no worktree write |
| `source_anchors.txt` | committed-HEAD file:line anchors, including the SQL RPC contract quoted verbatim |
| `live_measurement.txt` | read-only snapshot of the operator's live log and DB: counts, breakdown, and the fixed 275,740-line reconciliation window |

## DoD -> evidence

1. **Protocol recorded, evidence not circular.** `docs/cloud-sync.md:42` (`## Version protocol and
   rejected transitions (T-41)`) states the counter is remote per-entity state, that `sync_state`
   has no version row by design, and what `0 -> 3` means; `:72-95` reconciles every count: the
   operator's 7,373 at 275,740 lines is the **all-transitions** pattern (6,985 of them exactly
   `0 -> 3`), the earlier 7,028 at 277,088 was the **exactly `0 -> 3`** pattern, and the remaining
   388 are the same defect at `9 -> 14` (370), `9 -> 11` (14), `9 -> 12` (2) and `0 -> 2` (2).
   `live_measurement.txt` records the raw `wc -l`/`head -n`/`grep`/SQLite commands and output,
   including the same 275,740-line window. The authority for `p_version = expected + 1` is the
   migration itself: `source_anchors.txt` section
   `## supabase/migrations/0001_orchestrator_sync.sql:39-90` quotes
   `orchestrator_sync_push` and its rejection verbatim; `src/orchestrator/services/sync_transport.py:217`
   shows the RPC-derived `next_version`, sent at `:225`.
2. **Recoverable or explicitly terminal.** `sync_transport.py:217`/`:225` send
   `p_expected_version = synced_version` and `p_version = synced_version + 1`
   (`test_push_sends_synced_version_plus_one_not_the_local_revision`); `sync_service.py:383`
   `reinitialise` plus `cli.py:1607` `sync reinit --dry-run|--apply` repair a version disagreement
   from remote truth (`test_reinit_dry_run_then_apply_realigns_a_rejected_version`). A dirty row
   whose remote version is ahead is `remote_ahead_skipped` and untouched, so reinit cannot
   overwrite unseen remote changes (`test_reinit_never_rebaselines_a_dirty_row_past_unseen_remote_changes`).
3. **Classification, backoff, cap, stopped state, status.** `sync_runner.py:48` `backoff_seconds`
   is a capped delay; only a `SyncPermanentError` is terminal and the base is floored at the
   configured interval (`test_default_transient_retries_reach_the_900_second_cap`,
   `test_transient_failures_back_off_without_ever_stopping`). A permanent 4xx records
   `stopped`/`stopped_reason` (`sync_service.py:238` `SYNC_HEALTH_KEY`, `:281` `health`).
   `sync_runner.py:103` status, `GET /api/sync/status` (`api/routes/sync.py`), and `cli.py:1594`
   `sync status` expose `stopped`, `stopped_reason`, `consecutive_failures`, `last_failure_kind`,
   `next_retry_at`. Tests: `test_a_permanent_400_stops_the_loop_and_records_the_stopped_reason`,
   `test_sync_status_reports_the_stopped_reason_and_failure_streak`,
   `test_sync_status_reports_a_stopped_streak`.
4. **Operator-visible notification and GUI.** `notification_vocab.py:55` adds the non-noisy
   `sync_failed` trigger. `sync_runner.py:199` `_notify_if_needed` emits at most one event when a
   transient streak becomes persistent and one when a permanent error stops the loop; `run_once`
   calls it, so a manual `sync now` is covered too. The once-per-streak flags are atomic claims:
   `sync_service.py:356` `claim_health_flag` and `:289` `update_health` do a conditional
   `UPDATE ... WHERE value_json = <bytes read>` and re-apply on a lost race. No single-writer
   assumption is made across the desktop loop and CLI processes; `docs/cloud-sync.md:146-156`
   states this. The T-9 fan-out therefore respects the master switch and per-trigger subscription.
   React `web/src/pages/Settings.tsx:198` shows the stopped reason and `:200` gives reinit only for
   a permanent error; `:213` shows the transient retry line. Fallback UI:
   `api/static_fallback/index.html:213` chooses the same hint from `last_failure_kind`. Tests:
   `test_a_stopped_sync_emits_one_sync_failed_notification_per_streak`,
   `test_a_manual_sync_that_stops_permanently_still_notifies`,
   `test_a_persistently_failing_transient_sync_notifies_once`,
   `test_a_stopped_sync_respects_the_notification_master_switch`,
   `test_concurrent_failure_records_cannot_lose_an_increment`,
   `test_a_health_flag_claim_is_won_by_exactly_one_racer`.
5. **Log damage bounded.** `sync_runner.py:130` `_record_failure` states the rule in its docstring:
   a line is keyed by exact `<exception type>: <message>`; a matching `last_logged_error` increments
   `sync_health.log_occurrences`, the first occurrence logs once with a traceback and identical
   repeats log at DEBUG. Test `test_identical_failures_log_once_with_an_occurrence_counter` asserts
   one ERROR for two identical failures and `log_occurrences == 2`.
6. **Offline tests (a)-(f).** `tests/unit/test_sync_resilience.py`,
   `tests/integration/test_sync_api.py`, `tests/integration/test_cli.py`. (a) permanent 400 stops
   and records; (b) the capped-backoff sequence, the running loop retrying a 5xx, and the transport
   classification (400 permanent vs 500/503/408/429 transient); (c) dedupe counter; (d) CLI and API
   status; (e) dry-run/apply reinit then a legal push, plus the remote-ahead refusal; (f) an enabled
   sync that has never run initialises and succeeds. Cross-process health writes are covered by the
   two racer tests above. No network: fake clients and `httpx.MockTransport`.
7. **Docs.** `docs/cloud-sync.md` covers the SQL-backed protocol, the reconciled live measurement,
   rejection meaning, reinit, permanent-stop vs transient-retry/notify, the cross-process CAS rule,
   and the dedupe rule; `README.md` Known limitations states what sync does not guarantee and how a
   stop or transient streak is surfaced; the `sync_failed` notification default is in
   `docs/configuration.md` and `docs/decisions.md`.
8. **Full suite and lint via `./scripts/orch_verify.sh`**, stamped at the code HEAD in
   `full_suite.txt` / `lint.txt` (`All checks passed!`), with the real pass counts quoted in this
   round's handoff from those files; the focused-test count in `t41_tests.txt` is quoted from the
   file itself, not restated from an earlier round.