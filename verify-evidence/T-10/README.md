# T-10 evidence — a timed-out run must not discard the handoff it already wrote

Branch `task-10-a-run-that-times-out-discards-the-valid-handoff`, cut from `main`
(`merge-base 359a991`). Every log below was produced from THIS worktree with the project's
configured wrapper (`./scripts/orch_verify.sh …`), which asserts that `orchestrator` imported from
the worktree and not the main checkout — the `ORCH_VERIFY|…|worktree_root=<this worktree>` marker
is in each file.

| file | command | result |
| --- | --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh all` | `456 passed, 6 skipped` (0 failed) + `All checks passed!` (ruff) |
| `handoff_salvage_tests.txt` | `./scripts/orch_verify.sh test -k handoff_salvage -vv` | `30 passed, 432 deselected` |
| `code_map.txt` | `python3 verify-evidence/T-10/collect_code_map.py` | full function bodies + `file:line` for every claim below, plus the diffstat |
| `changed_existing_tests.txt` | `git diff <merge-base>..HEAD -- tests/integration/test_mock_run.py` | the only pre-existing test whose assertions changed, itemised |

Each log starts with the HEAD it ran at (`# HEAD=<sha> …`). `code_map.txt` and
`changed_existing_tests.txt` were generated at `7a8215b`; this README/evidence commit is the only
change after that, so the `file:line` references below are the ones the logs cover. Result counts
were measured, not copied from a previous round.

## DoD item -> implementation -> proof

1. **Ingestion before the non-clean-exit return, run status unchanged.**
   `run_service.try_salvage_handoff` (`run_service.py:423`) is called in the cancel branch
   (`run_service.py:943` passes the stale guard) and the timeout/failed branch
   (`run_service.py:976`) *before* the `task → planned` fallback; a salvaged submission continues
   through `_gate_salvaged` (`run_service.py:636`) into the normal verification + review path.
   The run was already set to `cancelled`/`timeout`/`failed` and is never re-labelled
   `completed`. Proof: integration tests at `tests/integration/test_handoff_salvage.py:173`
   (timeout), `:233` (cancel), `:255` (non-zero exit) assert `run.status` stays terminal while
   `Verification` and `Review` rows exist.

2. **Additive `handoff_salvaged` event.** Emitted at `run_service.py:510` (non-clean-exit) and
   `run_service.py:610` (pre-round archive). Payload builder `_salvage_log_payload`
   (`run_service.py:390`) carries `run_status`, `exit_code`, `timed_out`, `cancelled`, `error`,
   `mode`, `handoff_status` and claim/commit counts. No existing event name or line-shape key
   changed (`log_service.log_event` untouched). Documented in `docs/build-loop.md:189`
   (§ Events) and listed at `docs/build-loop.md:438`. Proof: the three DoD-1 tests parse the
   event payload with `structured_log.parse` and assert `run_status`/`mode`; the cancel test also
   asserts `run_cancelled` (T-9's name) is still absent.

3. **Never discard a pre-existing submission at round start; never ingest a stale one.**
   `archive_before_round_start` (`run_service.py:539`) copies an existing `.orchestrator/handoff.json`
   to `ORCHESTRATOR_HOME/evidence/pre-round-handoff/<key>/` before the caller unlinks it; on any
   read/write failure it returns False and the unlink is skipped. Because a stale file can then
   survive, `handoff_snapshot` (`run_service.py:232`) fingerprints it at the round start
   (`run_service.py:815-820`), and `try_salvage_handoff` refuses a file that is byte-for-byte
   unchanged at the end of the run (`run_service.py:447-460`, logs
   `reason=unchanged since round start`, `stale=true`); the clean-exit path applies the same rule
   and synthesizes instead (`run_service.py:986-1005`). Proof: tests
   `test_a_pre_existing_submission_is_archived_before_the_round_start_unlink` (`:311`),
   `test_an_unusable_leftover_is_archived_raw_and_the_round_still_starts` (`:347`),
   `test_a_retry_after_a_salvaged_round_does_not_lose_the_archived_copy` (`:363`), and
   `test_a_stale_leftover_is_not_salvaged_as_the_new_rounds_submission` (`:387`).

4. **Lifecycle representation.** `handoff_status = SALVAGED`
   (`artifact_service.HANDOFF_STATUS_SALVAGED`, `artifact_service.py:51`), set in `_gate_salvaged`
   (`run_service.py:636`), distinct from `SYNTHESIZED` and from the Worker's own status kept inside
   the archived handoff. Routing in `supervisor_service.after_run` (`supervisor_service.py:496`):
   a green gate routes a salvaged round to `review_status=NEEDS_HUMAN` and `task blocked` (never
   `accepted`, never auto-merged); a failing gate requeues it like any other round. Documented at
   `docs/build-loop.md:165`. Proof: the DoD-1 tests assert `handoff_status == "SALVAGED"`,
   `review_status == "NEEDS_HUMAN"`, `task.status == blocked`, `merge_status != "merged"`, and
   that `human_decision(accept)` then works.

5. **Offline mock-harness tests for the five cases.**
   (a) `test_timeout_with_a_valid_handoff_is_salvaged_and_reaches_the_gate` (`:173`);
   (b) `test_timeout_with_no_handoff_is_exactly_todays_behaviour` (`:271`);
   (c) `test_timeout_with_a_truncated_handoff_falls_back_and_logs_the_parse_failure` (`:289`);
   (d) `test_cancel_with_a_valid_handoff_is_salvaged` (`:233`);
   (e) `test_a_retry_after_a_salvaged_round_does_not_lose_the_archived_copy` (`:363`).
   All offline: mock harness + mock provider + a real local git repo in `tmp_path`; no network,
   no live service. The cancel test now cancels when the handoff file actually exists
   (`_cancel_when_handoff_written`, `test_handoff_salvage.py:142`) instead of sleeping, and the
   mock's cancel clamp is 15s (`mock_harness.py:35`, `:62`, `:97-98`), so it cannot race the
   marker into a timeout.

6. **Existing safety properties preserved.** The gate is still deterministic
   (`supervisor_service.verify`): the salvaged tests assert `checks["tests"]["status"] == "pass"`
   from the project's own configured command (the T-6 wrapper path is untouched) and that the run
   is not accepted. The no-handoff case is today's behaviour (`RUN_TIMEOUT`, `task planned`, no
   verification/review rows, exactly one `run_timeout` event). The stale-leftover case falls back
   to that same incomplete-round reporting. The full suite (`full_suite.txt`) is green, including
   all pre-existing run/gate tests.

7. **A salvaged handoff cannot launder a promise.** `delivery_evidence` (`run_service.py:257`)
   computes at ingestion time the commit list since the merge base (with the recovery flag), the
   diffstat versus the merge base, `git status --porcelain -uall`, the dirt present before the
   harness recovery sweep, and a per-claim status (`worker_commit` / `recovery_commit_only` /
   `uncommitted_only` / `absent`). It is archived in `runs/<id>/run.json` as
   `handoff_delivery_evidence` on both the salvage path (`run_service.py:490+`) and the clean-exit
   path (`run_service.py:1029+`). The reviewer payload renders the same evidence
   (`review_service.delivery_evidence_section:140` -> `prompt_builder.py:151`).
   **This is attached provenance + a structured flag, not a new gate**: no verification check was
   added (see reviewer-feedback disposition 2), because a harness recovery commit is still content
   on the branch and T-8 owns that machinery. Proof: `test_a_salvaged_promise_is_flagged_in_the_evidence_not_turned_into_a_new_gate`
   (`:443`) shows a claim present only as pre-sweep dirt classified `recovery_commit_only` with
   `claims_all_backed_by_worker_commits=false`, and the round routed to a human rather than
   auto-accepted; `test_the_architect_prompt_for_a_salvaged_run_carries_the_delivery_evidence`
   (`:490`) captures the real Architect prompt and asserts the section, the arrival, the claim
   classification and the flag are present in it.

## Round-2 reviewer feedback — disposition

1. **"Supplied diff is truncated; DoD claims rest on unverifiable file:line."** The full diff is
   `git diff 359a991..HEAD`; because the review harness truncates it, `code_map.txt` now contains
   the complete body of every function cited above, extracted from this checkout by AST with
   `file:line` headers, plus the diffstat. `changed_existing_tests.txt` contains the full diff of
   the one pre-existing test that changed.
2. **"New gate-failing claims check widens T-8 / out of scope."** Removed. There is no
   `CLAIMS_CHECK` in `supervisor_service.verify`; the comment at `supervisor_service.py:127`
   records why the evidence must stay informational. The promise test now asserts the check does
   not exist and that the evidence flag is what labels the claim
   (`tests/integration/test_handoff_salvage.py:443`).
3. **"When the round-start archive fails, the stale handoff could be salvaged as this round's."**
   Fixed with the snapshot/refusal described in DoD 3, and pinned by
   `test_a_stale_leftover_is_not_salvaged_as_the_new_rounds_submission` (`:387`), which forces
   the archive to fail and asserts the round falls back to `RUN_TIMEOUT`/`planned`, creates no
   verification row, and leaves the leftover untouched.
4. **"`test_mock_run.py` assertions changed with neither shown nor itemised."** Itemised in
   `changed_existing_tests.txt`. They were **strengthened, not loosened**: the two original
   assertions (`status == failed`, `stopReason: length` in the error) are unchanged; the test
   gains `salvaged is True`, `task.status == blocked`, `handoff_status == SALVAGED`. The fixture
   writes a real handoff and then reports an adapter error, which is precisely the out-of-band
   arrival T-10 exists to ingest, so the old "task returns to planned" expectation is the bug.
5. **"`mock-cancel-handoff` in HANG_TAGS races the 2s clamp."** Split the clamps:
   `MOCK_CANCEL_SECONDS = 15` (`mock_harness.py:35`) and `CANCEL_HANG_TAGS`
   (`mock_harness.py:62`, clamp `:97-98`); the cancellation itself is triggered by the handoff
   file existing, not by a sleep (`test_handoff_salvage.py:142`).
6. **"No integration assertion that a salvaged run's Architect prompt contains the evidence."**
   Added `test_the_architect_prompt_for_a_salvaged_run_carries_the_delivery_evidence` (`:490`),
   which wraps `provider_service.complete`, captures the `purpose="review"` prompt and asserts the
   provenance section, `handoff arrival: salvaged`, the per-claim line, the structured flag and
   the porcelain line.

## Round-1 recovery commit (undeclared content, not self-checked by a Worker)

The salvage core (constants, `read_handoff_file`, `try_salvage_handoff`,
`archive_before_round_start`, `_gate_salvaged`, `delivery_evidence`, the artifact/review/supervisor
additions, and both test files) arrived in `3e6a1a7`
(`chore(orchestrator-recovery): uncommitted worker output from R-16`), not in declared feature
commits. This round read and executed it rather than re-implementing it, and repaired the defects
found: the mock harness never emitted the failure shapes (fixed in `01d8c75`), the clean-exit
`exit_state` recorded `status=running` (fixed in `3582afc`), one F401, several test assertions that
contradicted their own fixtures, and the round-2 findings above.

No frozen-contract file was touched. `git diff <merge-base> HEAD --name-only` contains no
`docs/frozen-contracts.md`, no `src/orchestrator/core/models.py`, no migration, no
`src/orchestrator/api/**`, and no new file under `runs/<run_id>/` (the evidence root is
`ORCHESTRATOR_HOME/evidence/`, outside the frozen layout).