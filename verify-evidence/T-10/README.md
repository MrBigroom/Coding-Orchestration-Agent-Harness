# T-10 evidence — a timed-out run must not discard the handoff it already wrote

Both logs were produced from this worktree with the project's configured commands
(`./scripts/orch_verify.sh …`), which assert that `orchestrator` imported from the worktree and
not from the main checkout (`ORCH_VERIFY|…|worktree_root=<this worktree>` is quoted in each log).

| log | command | result |
| --- | --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh all` | `454 passed, 6 skipped` (0 failed) + `All checks passed!` (ruff) |
| `handoff_salvage_tests.txt` | `./scripts/orch_verify.sh test -k handoff_salvage -vv` | `28 passed, 432 deselected` |

Each log starts with the HEAD it ran at (`# HEAD=<sha> tree-entries=<n> dirty=<n>`).

## DoD item -> where it is implemented -> what proves it

1. **Ingestion before the non-clean-exit return.**
   `src/orchestrator/services/run_service.py:885` (cancelled) and `:917` (timeout / non-zero
   exit / harness error) call `try_salvage_handoff` (`:401`) *before* the
   `return`/`task → planned` fallback; a salvaged submission continues through
   `_gate_salvaged` (`:592`) into the normal verification + review path. The run keeps its
   terminal status — it is never re-labelled `completed`.
   Proof: `test_timeout_with_a_valid_handoff_is_salvaged_and_reaches_the_gate`,
   `test_cancel_with_a_valid_handoff_is_salvaged`,
   `test_non_zero_exit_with_a_valid_handoff_is_salvaged` (all assert `run.status` stays
   `timeout`/`cancelled`/`failed` while `Verification` and `Review` rows exist).

2. **Additive `handoff_salvaged` event.**
   Emitted at `run_service.py:466` (`mode=non_clean_exit`) and `:566`
   (`mode=pre_round_archive`); payload is `_salvage_log_payload` (`:368`) carrying
   `run_status`, `exit_code`, `timed_out`, `cancelled`, `error`, `mode`, `handoff_status` and
   the claims/commit counts. No event name and no line-shape key changed
   (`log_service.log_event` untouched). Documented in `docs/build-loop.md:183` (§ Events) and
   added to the event list at `docs/build-loop.md:416`.
   Proof: the same three tests parse the event payload
   (`structured_log.parse(...)`) and assert `payload["run_status"]`/`mode`; the cancel test also
   asserts `run_cancelled` (T-9's name) is still absent.

3. **Never discard a pre-existing submission at round start.**
   `archive_before_round_start` (`run_service.py:495`) copies an existing
   `.orchestrator/handoff.json` to `ORCHESTRATOR_HOME/evidence/pre-round-handoff/<key>/` before the
   caller unlinks it (`artifact_service.evidence_dir`, `artifact_service.py:68`); on any read or
   write failure it returns False and the unlink is skipped. The already-archived case
   (`mode=already_archived`) copies nothing and logs nothing.
   Proof: `test_a_pre_existing_submission_is_archived_before_the_round_start_unlink`
   (byte-identical copy), `test_an_unusable_leftover_is_archived_raw_and_the_round_still_starts`
   (raw copy, no `handoff.md`), `test_a_retry_after_a_salvaged_round_does_not_lose_the_archived_copy`.

4. **Lifecycle representation.** `handoff_status = SALVAGED`
   (`artifact_service.HANDOFF_STATUS_SALVAGED`, `artifact_service.py:51`), set in
   `_gate_salvaged` (`run_service.py:606`), distinct from `SYNTHESIZED` (orchestrator-authored)
   and from the Worker's own status kept inside the archived handoff. Routing decided in
   `supervisor_service.py:518`: a green gate routes a salvaged round to
   `review_status=NEEDS_HUMAN` and `task blocked` (never `accepted`, never auto-merged); a failing
   gate requeues it like any other round. Documented at `docs/build-loop.md:159`.
   Proof: DoD-1 tests assert `handoff_status == "SALVAGED"`, `review_status == "NEEDS_HUMAN"`,
   `task.status == blocked`, `merge_status != "merged"`, `merge_enabled is False`, and that
   `human_decision(accept)` then works (round recoverable, not burned).

5. **Offline mock-harness tests for the five cases.**
   (a) `test_timeout_with_a_valid_handoff_is_salvaged_and_reaches_the_gate`;
   (b) `test_timeout_with_no_handoff_is_exactly_todays_behaviour`;
   (c) `test_timeout_with_a_truncated_handoff_falls_back_and_logs_the_parse_failure`;
   (d) `test_cancel_with_a_valid_handoff_is_salvaged`;
   (e) `test_a_retry_after_a_salvaged_round_does_not_lose_the_archived_copy`.
   All offline: mock harness (`src/orchestrator/adapters/mock_harness.py`) + mock provider +
   a real local git repo in `tmp_path`; no network, no live service.

6. **Existing safety properties preserved.** The gate is still deterministic
   (`supervisor_service.verify`): the salvaged tests assert `checks["tests"]["status"] == "pass"`
   from the project's own configured command (the T-6 wrapper path is untouched) and that the run
   is not accepted; the no-handoff case is byte-for-byte today's behaviour
   (`RUN_TIMEOUT`, `task planned`, no verification/review rows, `run_timeout` event). The full
   suite (`full_suite.txt`) is green, including all pre-existing run/gate tests.

7. **A salvaged handoff cannot launder a promise.**
   `delivery_evidence` (`run_service.py:235`) computes, at ingestion time, the commit list since
   the merge base (with the recovery flag), the diffstat, `git status --porcelain -uall`, the dirt
   present before the harness recovery sweep, and a per-claim status
   (`worker_commit` / `recovery_commit_only` / `uncommitted_only` / `absent`). It is archived in
   `runs/<id>/run.json` as `handoff_delivery_evidence` on **both** the salvage path (`:445`+) and
   the clean-exit path (`:951`+). The `claims vs commits` check
   (`supervisor_service.py:140`) fails a claim not backed by a Worker commit, and the reviewer
   payload renders the same evidence (`review_service.delivery_evidence_section:140` ->
   `prompt_builder.py:149`).
   Proof: `test_salvaged_claims_that_were_never_committed_fail_the_claims_check` (claim present
   only as pre-sweep dirt -> check `fail`, `verification=fail`, task requeued, never accepted) and
   `test_the_clean_exit_path_carries_the_same_evidence`.

## Round-1 recovery commit (undeclared content, not self-checked by a Worker)

The salvage core (constants, `read_handoff_file`, `try_salvage_handoff`,
`archive_before_round_start`, `_gate_salvaged`, `delivery_evidence`, the artifact/review/
supervisor additions, and both test files) arrived in `3e6a1a7`
(`chore(orchestrator-recovery): uncommitted worker output from R-16`), not in declared feature
commits. It was verified by reading the code and running it, not re-implemented. This round
repaired the defects inside it: the mock harness never emitted the failure shapes (see below),
one F401, a clean-exit `exit_state` that recorded `status=running`, and several test assertions
that contradicted their own fixtures.

No frozen-contract file was touched: `git diff <merge-base> HEAD --name-only` contains no
`docs/frozen-contracts.md`, no `src/orchestrator/core/models.py`, no migration, and no
`src/orchestrator/api/**`.