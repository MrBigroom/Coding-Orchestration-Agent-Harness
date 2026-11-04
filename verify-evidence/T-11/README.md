# T-11 evidence index

Measured at `96289d7` — the HEAD stamped inside every file in this directory, with
`tree=0 dirty-entries` at capture time. `capture.sh` is the one script that produced them, so the
numbers here cannot come from a different tree than the one under review. The commit that contains
this index changes only files under `verify-evidence/T-11/`; it touches no source and no test, so
the counts above are the counts for the code the reviewer is reading.

| DoD item | Landed where | Proved by |
|---|---|---|
| 1. archive reviewer input + truncation manifest; PROPOSED CCR, frozen contracts untouched | `src/orchestrator/services/review_service.py:175` (`render_review_payload`), `:466` (writes `review_payload.md`); `src/orchestrator/services/prompt_builder.py:63` (`TruncationRecorder`), `:219` (`render_diff_budget`), `:300` (`render_truncation_manifest`), `:508` (records every withheld sensitive file), `:580` (`enforce_budget` cut); `docs/contract-change-request-T11-review-payload.md` | `t11_tests.txt` (`test_review_payload_is_archived_and_names_what_truncation_dropped`); `changed_files.txt` shows `docs/frozen-contracts.md` absent from the diff |
| 2. provenance split for harness vs reviewer absence claims, recorded in `review.json` | `review_service.py:175-241` (separate `missing_artifacts` labels + per-claim notes), `:447` ("mechanical absence checks: N confirmed, N refuted, N unchecked"); payload section at `:210-238` ("What the reviewer was actually given") | `test_harness_and_reviewer_missing_artifacts_live_in_separate_payload_sections`; `test_visibility_claim_is_refuted_by_the_supplied_patch_not_by_the_file_existing` |
| 3. pre-check absence findings before requeue; refuted finding cannot cost a round; additive build-log event | `review_audit.py:158` (`supplied_evidence`), `:194` (`_check_visibility`), `:498`/`:516` (finding + missing-artifact checks); `review_service.py:397-421` (refute, route to `NEEDS_HUMAN`, never `APPROVED`), `:424` (`review_absence_refuted`) | `test_existing_file_refutes_a_missing_file_claim`, `test_existing_test_name_refutes_a_missing_test_claim`, `test_refuted_absence_finding_does_not_requeue_and_never_becomes_an_approval`, `test_a_visibility_claim_is_refuted_by_the_supplied_patch_not_by_existence`, `test_an_unknowable_visibility_claim_is_unchecked_not_refuted` |
| 4. per-file diff budget with included/omitted file list; handoff and diff cuts named in the archive | `prompt_builder.py:158` (`diff_priority`), `:166` (`_chunk_head`, inline marker + omitted hunk headers), `:219` (`render_diff_budget`: included-in-full / partially-included / omitted, whole files preferred), `:434`/`:508` (`_dynamic` records handoff, diff and sensitive-file cuts) | `test_diff_budget_names_every_omitted_file_and_records_the_manifest`, `test_a_partially_supplied_file_says_so_in_the_text_the_reviewer_reads`, `test_an_omitted_file_is_listed_and_recorded`, `test_sensitive_files_withheld_from_the_diff_are_recorded_and_named` |
| 5. offline tests (a)-(e), mock harness only | `tests/unit/test_review_audit.py` (17 tests), `tests/integration/test_review_payload.py` (5 tests); existing suite unchanged except the T-8 assertions that now expect the T-11 artifact | `t11_tests.txt` (20 passed, every name listed); `suite_and_lint.txt` (554 passed, 6 skipped; lint clean) |
| 6. docs: artifact layout + review-auditability guidance | `docs/build-loop.md` gate step 4 (l.318-332) and "Review input auditability" (l.442-495); `docs/roles.md` review note; `docs/decisions.md` T-11 entry + round-2 entry; `docs/contract-change-request-T11-review-payload.md` | `changed_files.txt` |
| 7. hidden evidence is a harness finding: diffstat vs patch, binary/no-hunk under evidence/source, `git check-attr -a` for `-diff`/`export-ignore`, pre-existing attributes do not fail the run | `src/orchestrator/services/review_audit.py:625` (`diff_completeness`), `:731` (`render_diff_completeness`); wired in `review_service.py:367` (`review_diff_incomplete`) | `test_diff_completeness_flags_a_diff_attribute_introduced_by_this_task`, `test_diff_attribute_that_predates_the_task_is_reported_but_does_not_block`, `test_diff_completeness_flags_a_changed_file_that_has_no_hunk_body` |

## Commands

- `bash verify-evidence/T-11/capture.sh` regenerates all four files at the current HEAD.
- `./scripts/orch_verify.sh all` -> exit 0, `554 passed, 6 skipped`, `All checks passed!`
  (`suite_and_lint.txt`, stamped).
- `./scripts/orch_verify.sh test -vv -k 'review_audit or review_payload or diff_budget or visibility
  or sensitive_files_withheld or partly'` -> `20 passed`, every T-11 test named (`t11_tests.txt`).

## Round-2 review findings and where each is answered

| Reviewer finding | Answer in this tree |
|---|---|
| 1. Refuted absence claims softened `CHANGES_REQUESTED` to `APPROVED` (fail-open) | `review_service.py:405-421`: the verdict becomes `NEEDS_HUMAN` with a "review verdict" finding and routes to `blocked`; no verdict value, acceptance rule or state transition changes. Test: `test_refuted_absence_finding_does_not_requeue_and_never_becomes_an_approval` asserts `NEEDS_HUMAN`, `blocked`, `source != "retry"` and no `task_accepted` event. |
| 2. Existence was used to refute a visibility claim | `review_audit.py:194` checks visibility claims against the supplied patch and the truncation manifest only; existence can yield `unchecked`, never `refuted`. Tests: `test_a_visibility_claim_is_refuted_by_the_supplied_patch_not_by_existence`, `test_an_unknowable_visibility_claim_is_unchecked_not_refuted`, `test_a_partially_supplied_file_confirms_a_truncation_claim`. |
| 3. Sensitive-file strip dropped files with no recorder entry | `utils/redaction.py:102` collects the dropped paths; `prompt_builder.py:508` records each as a `diff:<path>` truncation. Test: `test_sensitive_files_withheld_from_the_diff_are_recorded_and_named`. |
| 4. Partial files listed as "included", no inline marker | `prompt_builder.py:166` appends `[review diff: N of M hunks shown for <path>; ... (@@ headers named)]`; `:219` reports three lists. Tests: `test_a_partially_supplied_file_says_so_in_the_text_the_reviewer_reads`, `test_diff_budget_names_every_omitted_file_and_records_the_manifest`. |
| 5. The supplied diff was truncated mid-`prompt_builder.py`, so wiring was unverifiable | `prompt_builder.py:158` orders the diff source-first and `:219` now prefers whole files: with this branch's own diff (`git diff main...HEAD`, 18 files, `max_diff_tokens=12000`) all five other source files are complete and `review_service.py` is supplied as 7 of 9 hunks with the two omitted hunk headers named inline, instead of one silent cut. `max_diff_tokens` is unchanged. |

## Notes

- All tests are offline: the mock provider/harness and a temporary local git repository; no
  network, no external CLI.
- `docs/frozen-contracts.md` is **not** modified. The new `runs/<run_id>/review_payload.md` name
  is declared PROPOSED in `docs/contract-change-request-T11-review-payload.md`.