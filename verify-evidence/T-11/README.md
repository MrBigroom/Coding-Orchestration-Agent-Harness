# T-11 evidence index

Code HEAD measured here: `5860597` (`58605975bb933028233a284d7b3487b632528e4e`).
The evidence commit that contains this file changes no source or tests, so the counts below are
still the counts for the code under review.

| DoD item | Landed where | Proved by |
|---|---|---|
| 1. archive reviewer input + truncation manifest; PROPOSED CCR, frozen contracts untouched | `src/orchestrator/services/review_service.py:170` (`render_review_payload`), `:421` (writes `review_payload.md`); `src/orchestrator/services/prompt_builder.py:38` (`TruncationRecord`), `:167` (`render_diff_budget`), `:221` (`render_truncation_manifest`), `:444`/`:485` (records every `_dynamic`/`enforce_budget` cut); `docs/contract-change-request-T11-review-payload.md` | `t11_tests.txt` (`test_review_payload_is_archived_and_names_what_truncation_dropped`); `changed_files.txt` shows `docs/frozen-contracts.md` absent from the diff |
| 2. provenance split for harness vs reviewer absence claims, recorded in `review.json` | `review_service.py:307-332` (separate `missing_artifacts` labels + per-claim notes); payload sections at `:170-219`; `review_audit.py:51` (`AbsenceCheck`) | `test_harness_and_reviewer_missing_artifacts_live_in_separate_payload_sections` |
| 3. pre-check absence findings before requeue; refuted finding cannot cost a round; additive build-log event | `review_audit.py:253` (`check_findings`), `:267` (missing artifacts), `review_service.py:291-390` (refute, soften only when nothing else survives, `review_absence_refuted`) | `test_existing_file_refutes_a_missing_file_claim`, `test_existing_test_name_refutes_a_missing_test_claim`, `test_refuted_absence_finding_is_recorded_and_does_not_requeue` |
| 4. per-file diff budget with included/omitted file list; handoff and diff cuts named in the archive | `prompt_builder.py:167` (`render_diff_budget`), `:355` (`_dynamic` records handoff/diff/log cuts) | `test_diff_budget_names_every_omitted_file_and_records_the_manifest`; `test_review_payload_is_archived_and_names_what_truncation_dropped` |
| 5. offline tests (a)-(e), mock harness only | `tests/unit/test_review_audit.py`, `tests/integration/test_review_payload.py`; existing suite unchanged except the T-8 assertion that now expects the T-11 artifact | `t11_tests.txt` (11 passed); `suite_and_lint.txt` (545 passed, 6 skipped; lint clean) |
| 6. docs: artifact layout + review-auditability guidance | `docs/build-loop.md:436-465`; `docs/roles.md` review note; `docs/decisions.md` T-11 entry | `changed_files.txt` |
| 7. hidden evidence is a harness finding: diffstat vs patch, binary/no-hunk under evidence/source, `git check-attr -a` for `-diff`/`export-ignore`, pre-existing attributes do not fail the run | `src/orchestrator/services/review_audit.py:342` (`diff_completeness`), `:448` (`render_diff_completeness`); wired in `review_service.py:236` and `:334-360` (`review_diff_incomplete`) | `test_diff_completeness_flags_a_diff_attribute_introduced_by_this_task`, `test_diff_attribute_that_predates_the_task_is_reported_but_does_not_block`, `test_diff_completeness_flags_a_changed_file_that_has_no_hunk_body` |

## Commands

- `./scripts/orch_verify.sh all` -> exit 0, `545 passed, 6 skipped`, `All checks passed!`
  (`suite_and_lint.txt`, stamped with the measured HEAD).
- `./scripts/orch_verify.sh test tests/unit/test_review_audit.py tests/integration/test_review_payload.py -vv`
  -> `11 passed` and every T-11 test name listed (`t11_tests.txt`).

## Notes

- All tests are offline: the mock provider/harness and a temporary local git repository; no
  network, no external CLI.
- `docs/frozen-contracts.md` is **not** modified. The new `runs/<run_id>/review_payload.md` name
  is declared PROPOSED in `docs/contract-change-request-T11-review-payload.md`.
