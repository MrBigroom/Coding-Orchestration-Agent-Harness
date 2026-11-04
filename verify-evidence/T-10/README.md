# T-10 evidence — a killed run must not discard the handoff it already wrote

Branch `task-10-a-run-that-times-out-discards-the-valid-handoff`, cut from `main`
(merge-base `359a991`). Every log was produced from THIS worktree with the project wrapper
(`./scripts/orch_verify.sh …`), which asserts `orchestrator` imported from the worktree — the
`ORCH_VERIFY|…|worktree_root=<this worktree>` marker is in each file.

| file | command | result |
| --- | --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` (the configured gate command) | `460 passed, 6 skipped` (0 failed) at code HEAD `c0a4954`; `All checks passed!` is in `lint.txt` from the same round |
| `handoff_salvage_tests.txt` | `./scripts/orch_verify.sh test -k handoff_salvage -vv` | `34 passed, 432 deselected` at code HEAD `c0a4954` |
| `lint.txt` | `./scripts/orch_verify.sh lint` | `All checks passed!` at code HEAD `c0a4954` |
| `zz_code_map.txt` | `python3 verify-evidence/T-10/collect_code_map.py` | commit list, recovery-commit stat, diffstat, cited function bodies/anchors with `file:line` from this checkout |
| `changed_existing_tests.txt` | `git diff <merge-base>..HEAD -- tests/integration/test_mock_run.py` | the one pre-existing test whose assertions changed |

Why `zz_`: the review payload truncates the branch diff from the middle and keeps the tail.
`zz_code_map.txt` sorts last on purpose, so the file that compensates for the truncated diff is
the one that survives. It repeats the commit list at its own tail for the same reason.

## DoD item -> implementation -> proof

1. **Ingestion before the non-clean-exit return; run status unchanged.**
   `run_service.try_salvage_handoff` is called in the cancel branch and the timeout/failed branch
   *before* the `task → planned` fallback; a salvaged submission continues through
   `_gate_salvaged` into the normal verification + review path. The run was already set to
   `cancelled`/`timeout`/`failed` and is never re-labelled `completed`. Proof: integration tests
   `test_timeout_with_a_valid_handoff_is_salvaged_and_reaches_the_gate` (timeout),
   `test_cancel_with_a_valid_handoff_is_salvaged`, and
   `test_non_zero_exit_with_a_valid_handoff_is_salvaged` assert `run.status` stays terminal while
   `Verification` and `Review` rows exist.

2. **Additive `handoff_salvaged` event.** Emitted on the non-clean-exit salvage and the pre-round
   archive; payload builder `_salvage_log_payload` carries `run_status`, `exit_code`, `timed_out`,
   `cancelled`, `error`, `mode`, `handoff_status` and claim/commit counts. No existing event name
   or line-shape key changed (`log_service.log_event` untouched). Documented in
   `docs/build-loop.md` (§ Events and the event list). Proof: the DoD-1 tests parse the payload
   with `structured_log.parse`; the cancel test also asserts `run_cancelled` (T-9's name) is still
   absent.

3. **Never discard a pre-existing submission at round start; never ingest a stale one.**
   `archive_before_round_start` copies an existing `.orchestrator/handoff.json` to
   `ORCHESTRATOR_HOME/evidence/pre-round-handoff/<key>/` before the caller unlinks it; on any
   read/write failure it returns False and the unlink is skipped, so nothing is destroyed. Because
   a stale file can then survive, `handoff_snapshot` fingerprints it at the round start and
   `try_salvage_handoff` refuses a file that is byte-for-byte unchanged at the end of the run
   (`reason=unchanged since round start`, `stale=true`); the clean-exit path applies the same rule
   and synthesizes instead. Proof:
   `test_a_pre_existing_submission_is_archived_before_the_round_start_unlink`,
   `test_an_unusable_leftover_is_archived_raw_and_the_round_still_starts`,
   `test_a_retry_after_a_salvaged_round_does_not_lose_the_archived_copy`,
   `test_a_stale_leftover_is_not_salvaged_as_the_new_rounds_submission`, and the clean-exit branch
   `test_a_clean_exit_does_not_ingest_a_stale_leftover_and_synthesizes`.

4. **Lifecycle representation.** `handoff_status = SALVAGED`
   (`artifact_service.HANDOFF_STATUS_SALVAGED`), set in `_gate_salvaged`, distinct from
   `SYNTHESIZED` and from the Worker's own status kept inside the archived handoff. A green gate
   routes a salvaged round to `review_status=NEEDS_HUMAN` and `task blocked` (never `accepted`,
   never auto-merged); a failing gate requeues it like any other round. Documented at
   `docs/build-loop.md` "Lifecycle: SALVAGED is not SYNTHESIZED and not READY_FOR_REVIEW". Proof:
   the DoD-1 tests assert `handoff_status == "SALVAGED"`, `review_status == "NEEDS_HUMAN"`,
   `task.status == blocked`, `merge_status != "merged"`.

5. **Offline mock-harness tests for the five cases.**
   (a) `test_timeout_with_a_valid_handoff_is_salvaged_and_reaches_the_gate`;
   (b) `test_timeout_with_no_handoff_is_exactly_todays_behaviour`;
   (c) `test_timeout_with_a_truncated_handoff_falls_back_and_logs_the_parse_failure`;
   (d) `test_cancel_with_a_valid_handoff_is_salvaged`;
   (e) `test_a_retry_after_a_salvaged_round_does_not_lose_the_archived_copy`.
   All offline: mock harness + mock provider + a real local git repo in `tmp_path`; no network, no
   live service. The cancel test cancels when the handoff file actually exists instead of sleeping,
   and the mock's cancel clamp is 15s so it cannot race the cancel marker into a timeout.

6. **Existing safety properties preserved.** The gate is still deterministic
   (`supervisor_service.verify`): the salvaged tests assert the project's own configured test
   command passes (the T-6 wrapper path is untouched) and that the run is not accepted. The
   no-handoff case is today's behaviour (`RUN_TIMEOUT`, `task planned`, no verification/review
   rows). The stale-leftover case falls back to the same incomplete-round reporting. The full
   suite is green, including all pre-existing run/gate tests.

7. **A salvaged handoff cannot launder a promise.** `delivery_evidence` computes at ingestion time
   the **baseline sha this run started from** (`_run_baseline_sha`: previous run `commit_sha`, else
   merge-base), the commits created by this run (`run_commits`, with the recovery flag), the
   earlier commits on the branch (`prior_run_commits`), the diffstat versus the merge base,
   `git status --porcelain -uall`, the dirt present before the harness recovery sweep, and a
   per-claim status (`worker_commit` / `recovery_commit_only` / `uncommitted_only` /
   `prior_run_commit` / `absent`). It is archived in `runs/<id>/run.json` as
   `handoff_delivery_evidence` on both the salvage path and the clean-exit path. The reviewer
   payload renders the same evidence (`review_service.delivery_evidence_section` ->
   `prompt_builder` dynamic state).
   **Reviewer finding 1 (round 5) is the scoping bug this fixes:** classifying against the
   merge-base made a zero-commit retry inherit earlier rounds' commits, so it could set
   `claims_all_backed_by_this_run_commits=true` for work it did not do. The flag is now named for
   what it means, and a claim on an earlier round's file is `prior_run_commit`. Proof:
   `test_a_zero_commit_retry_reports_prior_round_files_as_not_this_runs_delivery` (unit) and
   `test_a_zero_commit_retry_cannot_claim_an_earlier_rounds_files_as_this_runs_delivery`
   (end-to-end, zero-commit retry), plus
   `test_a_salvaged_promise_is_flagged_in_the_evidence_not_turned_into_a_new_gate` and
   `test_the_architect_prompt_for_a_salvaged_run_carries_the_delivery_evidence` (captures the real
   Architect prompt).
   **This is attached provenance + a structured flag, not a new gate**: no verification check was
   added, because a harness recovery commit is still content on the branch and T-8 owns that
   machinery. The rendered wording says exactly that instead of calling recovery-commit content
   undelivered work.

## Authority for behaviour beyond the four round-2 directive items

The round-2 directive named four items; three behaviours in the round-2 diff were not named
verbatim. Each is a consequence of a DoD item or of the phase's out-of-scope clause, not a new
product decision:

- **Stale-fingerprint refusal** — DoD 3's own fail-safe: when the archive cannot copy the leftover,
  the unlink is skipped so a stale file survives. DoD 7 then forbids attributing it to this run:
  that would be a provenance failure (a previous round's work read as this round's). DoD 6 forbids
  reporting a genuinely incomplete round as complete. Refusing the file and falling back to
  today's `RUN_TIMEOUT`/`planned` keeps all three true.
- **Clean-exit synthesis instead of reading the leftover** — the same guard must exist on the clean
  path. On a clean exit the alternative is worse than on the salvage path: the previous round's
  handoff would be ingested as a self-certified `READY_FOR_REVIEW` and could be auto-accepted.
  DoD 6 ("a genuinely incomplete round is still reported as incomplete") requires the fallback.
  Covered by `test_a_clean_exit_does_not_ingest_a_stale_leftover_and_synthesizes`.
- **No new claims-vs-commits gate** — the phase's out-of-scope clause: "No change to recovery_commit
  semantics or the DoD 1-10 machinery owned by T-8". Turning `recovery_commit_only` into a
  verification failure would re-gate T-8's recovery machinery through T-10. DoD 7 asks for
  attached provenance and a structured flag, which is what shipped.

## Round-5 reviewer findings — disposition

1. **Claims scoped to the base branch, not this run (finding 1).** Fixed structurally: per-run
   baseline, `prior_run_commit`, renamed flag, offline zero-commit-retry tests. See DoD 7 above.
2. **`dod_self_check[6]` self-certified DoD 7 while finding 1 was open (findings 2/5).** It is now
   true on the evidence above; `tests.command` is the configured
   `./scripts/orch_verify.sh test`, and this README records the measured HEAD.
3. **`mock_harness.py` `KNOWN_TAGS` comment described a routing mechanism that does not exist
   (finding 6).** Corrected: `build_command` forwards only known tags, and `main()`'s exit 2 is a
   direct-CLI guard.
4. **Findings 3 and 4 are NOT worked on.** The truncated supplied diff and artifacts that do not
   appear in a supplied artifact are the harness review-payload gap tracked as T-11; no review
   plumbing, frozen `runs/<run_id>/` file name, or new evidence machinery was added. The existing
   `zz_code_map.txt` was regenerated, with the added commit list kept at its tail.

## Round-2 reviewer findings — disposition (the five supplied this round)

1. **Diff/evidence truncated; cited `file:line` unverifiable.** `zz_code_map.txt` now carries the
   commit list, the recovery-commit stat, the diffstat, and only the cited functions/anchors with
   `file:line` from this checkout; it sorts last so it survives the tail of a truncated diff.
   `handoff.md` also quotes the decisive lines inline. `changed_existing_tests.txt` is the full
   diff of the single changed pre-existing test.
2. **Recovery-commit content not visible; Worker commit list missing.** The commit list and the
   `3e6a1a7` recovery-commit file stat are in `zz_code_map.txt` (head and tail) and inline in
   `handoff.md`. The recovery commit was not amended, rebased or re-split; later commits repaired
   its defects.
3. **`render_delivery_evidence_md` called recovery commits "PROMISES … wreckage", contradicting
   docs and effectively re-gating via the LLM reviewer.** Fixed: the closing line now states that
   `recovery_commit_only` IS on the branch but lacks a self-check, that `uncommitted_only` is not
   on the branch, and that this is provenance only, not a gate. Pinned by the unit renderer test.
4. **Handoff claimed unsupplied reviewer findings / untraceable behaviour / no clean-exit stale
   test.** This README and `handoff.md` now answer the actual five findings supplied this round,
   give the authority above for each extra behaviour, and add the clean-exit stale test named
   there.
5. **DoD 4 doc anchor `SALVAGED is not` was unfindable because the heading used backticks.** The
   heading is now plain text (`### Lifecycle: SALVAGED is not SYNTHESIZED and not
   READY_FOR_REVIEW`), and `zz_code_map.txt` greps and prints it.

## Round-1 recovery commit (undeclared content, not self-checked by a Worker)

The salvage core arrived in `3e6a1a7` (`chore(orchestrator-recovery): uncommitted worker output
from R-16`), not in declared feature commits. This round read and executed it rather than
re-implementing it, and repaired the defects found: the mock harness never emitted the failure
shapes (`01d8c75`), the clean-exit `exit_state` recorded `status=running` (`3582afc`), the
missing doc section (`47057d4`), test plumbing (`967aea9`), one lint error and the round-2
findings (`7a8215b`, and this round).

No frozen-contract file was touched. `git diff <merge-base> HEAD --name-only` contains no
`docs/frozen-contracts.md`, no `src/orchestrator/core/models.py`, no migration, no
`src/orchestrator/api/**`, and no new file under `runs/<run_id>/` (the evidence root is
`ORCHESTRATOR_HOME/evidence/`, outside the frozen layout).