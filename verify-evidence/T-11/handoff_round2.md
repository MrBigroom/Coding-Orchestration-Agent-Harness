# Handoff — T-11 (round 2)

**Status:** CHANGES_APPLIED (same branch, reviewer feedback applied)
**Branch:** task-11-the-reviewer-prompt-and-payload-are-never-archiv
**Tip:** 4eb490d (evidence index commit; code+tests unchanged since 96289d7, counts stamped there)

## Summary

All five round-2 reviewer findings are fixed on this branch, and the round-1 implementation
(archived `review_payload.md` + truncation manifest, PROPOSED CCR-T11-01, harness-vs-model absence
sections, `.gitattributes` / diff-completeness findings) is intact.

1. **No fail-open verdict.** When every remaining `CHANGES_REQUESTED` finding was a refuted
   absence claim, the gate used to set `APPROVED`. It now sets **`NEEDS_HUMAN`** with a
   `review verdict` finding, which routes to `blocked`. A refuted claim still never requeues a
   Worker round, but it never accepts a task either — no verdict value, acceptance rule or state
   transition changed.
2. **Existence is not visibility.** Absence claims are now split: an *existence* claim ("X is
   missing from the branch") is refuted by the worktree; a *visibility* claim ("X was not
   supplied / is truncated in the diff") is checked against the supplied patch and the archived
   truncation manifest, and is `unchecked` (never `refuted`) when neither can decide. The supplied
   set is derived from `prompt_builder.TruncationRecorder` — the same code that performed the cuts.
3. **Withheld files are named.** `strip_sensitive_files_from_diff` now returns the paths it drops
   and `_dynamic` records each as a `diff:<path>` truncation, so a `.env` appears in the manifest
   and in the reviewer-visible omission marker instead of vanishing between `patch.diff` and the
   prompt.
4. **Partial files cannot look complete.** `_chunk_head` appends
   `... [review diff: N of M hunks shown for <path>; K omitted by the per-file diff budget (@@ headers named)] ...`
   and `render_diff_budget` reports three lists — included in full / partially included (with hunk
   counts) / omitted — instead of one "included" bucket.
5. **The wiring is verifiable.** The diff is ordered source-first and whole files are preferred
   over many stubs. Measured on this branch's own diff (`git diff main...HEAD`, 18 files,
   `max_diff_tokens=12000`): five of six source files are supplied complete, and
   `review_service.py` is 7 of 9 hunks with the two omitted hunk headers named inline — previously
   one silent cut landed mid-file and made the wiring unverifiable. `max_diff_tokens` is
   **unchanged**.

## Files changed (22, `git diff --name-only $(git merge-base main HEAD)..HEAD`)

- docs/build-loop.md
- docs/contract-change-request-T11-review-payload.md
- docs/decisions.md
- docs/roles.md
- src/orchestrator/providers/base.py
- src/orchestrator/providers/mock_provider.py
- src/orchestrator/services/artifact_service.py
- src/orchestrator/services/prompt_builder.py
- src/orchestrator/services/review_audit.py
- src/orchestrator/services/review_service.py
- src/orchestrator/utils/redaction.py
- tests/integration/test_recovery_after_timeout.py
- tests/integration/test_review_payload.py
- tests/unit/test_recovery_markers.py
- tests/unit/test_review_audit.py
- verify-evidence/T-11/README.md
- verify-evidence/T-11/capture.sh
- verify-evidence/T-11/changed_files.txt
- verify-evidence/T-11/suite_and_lint.txt
- verify-evidence/T-11/handoff_round2.md
- verify-evidence/T-11/handoff_round2.json
- verify-evidence/T-11/t11_tests.txt

## Tests

- `./scripts/orch_verify.sh all` -> **554 passed, 0 failed, 6 skipped**, `All checks passed!`
  (`ALL_EXIT=0`). Log: `verify-evidence/T-11/suite_and_lint.txt`, stamped
  `HEAD=96289d7 … tree=0 dirty-entries`.
- `./scripts/orch_verify.sh test -vv -k 'review_audit or review_payload or diff_budget or
  visibility or sensitive_files_withheld or partly'` -> **20 passed, 0 failed**
  (`TARGETED_EXIT=0`), every T-11 test name listed in `verify-evidence/T-11/t11_tests.txt`.
- All tests are offline: mock provider + mock harness + a temporary local git repo. No network,
  no external service.
- Regenerate with `bash verify-evidence/T-11/capture.sh`.

## DoD self-check

1. **Archive reviewer input + truncation manifest; PROPOSED CCR; no frozen-contract edit** — met.
   `review_service.py:175` `render_review_payload`, `:466` writes `review_payload.md`;
   `prompt_builder.py:63` `TruncationRecorder`, `:219` `render_diff_budget`, `:300`
   `render_truncation_manifest`, `:508` sensitive-file record, `:580` `enforce_budget` record.
   `docs/contract-change-request-T11-review-payload.md` is **PROPOSED**;
   `docs/frozen-contracts.md` is absent from `changed_files.txt`.
2. **Separate harness/model absence sections + review.json says what the harness confirmed** — met.
   Payload sections at `review_service.py:175-241` (plus "What the reviewer was actually given");
   `review.json` notes carry every `absence check [status] …` row and the summary line
   `mechanical absence checks: N confirmed, N refuted, N unchecked` (`:447`). Test asserts the
   summary line literally.
3. **Pre-check absence claims; refuted finding costs no round; additive event** — met.
   `review_audit.py:158/194/498/516`; `review_service.py:397-421` removes refuted findings and
   routes to `NEEDS_HUMAN`; `:424` emits `review_absence_refuted`. Test
   `test_refuted_absence_finding_does_not_requeue_and_never_becomes_an_approval` asserts
   `NEEDS_HUMAN`, `blocked`, `source != "retry"`, exactly one `review_absence_refuted` event and
   no `task_accepted`.
4. **Truncation impossible to hide (per-file budget, file list, targeted hunks; handoff/diff cuts
   reported)** — met. `prompt_builder.py:158` priority, `:166` inline marker + omitted `@@`
   headers, `:219` three name lists and whole-file preference, `:434/:508` records handoff, diff
   and sensitive-file cuts. Four tests named in the evidence README.
5. **Offline tests (a)–(e)** — met. 20 targeted tests listed by name in
   `verify-evidence/T-11/t11_tests.txt`: (a) artifact+manifest, (b) refuted claim does not
   requeue, (c) separate sections, (d) manifest names dropped files, (e) the rest of the suite is
   green with review/run-artifact semantics untouched (only the two T-8 assertions that *expect*
   the T-11 artifact were updated, and they now assert its content).
6. **Docs** — met. `docs/build-loop.md` gate step 4 (`NEEDS_HUMAN`, existence vs visibility,
   refuted never upgraded) and "Review input auditability"; `docs/roles.md`; `docs/decisions.md`
   round-2 entry; CCR updated.
7. **Hidden evidence is a harness finding** — met (round-1 code, still green).
   `review_audit.py:625` `diff_completeness`, `:731` renderer, wired at `review_service.py:367`
   (`review_diff_incomplete`); three tests.

## New dependencies

None.

## Assumptions

- The evidence files are stamped at `96289d7` with `tree=0 dirty-entries`; the commits after it
  (`4eb490d` and this index) change only `verify-evidence/T-11/**`, never source or tests.
- `review.json` keeps its frozen shape and the frozen verdict vocabulary. A refuted claim routes
  to the existing `NEEDS_HUMAN` → `blocked`; nothing is accepted on a refuted claim and no finding
  is dropped without being recorded in `notes`.
- Claim kind is decided from wording; a composite claim ("missing from the supplied artifacts and
  the worktree") is treated as a visibility claim, which can only be `unchecked`, never `refuted`.
- Per-file expectations come from `git diff --numstat --no-renames base...HEAD`; pre-existing
  `.gitattributes` entries are informational (`blocks=False`) and cannot fail an unrelated run.
- `max_diff_tokens` is not widened.

## Questions

- Operator decision requested: approve **CCR-T11-01** (`runs/<run_id>/review_payload.md`) or leave
  the name unfrozen as on this branch.
- On this branch (~22k tokens of diff vs `max_diff_tokens=12000`) `review_service.py` is still
  supplied as 7 of 9 hunks even with source-first whole-file ordering, because the six source
  files alone nearly fill the budget. Whole-file review at this diff size needs a higher
  `max_diff_tokens` — a harness setting change this DoD does not authorise.

## Blockers

None.

## Contracts touched

- **PROPOSED**: `runs/<run_id>/review_payload.md` (CCR-T11-01,
  `docs/contract-change-request-T11-review-payload.md`). `docs/frozen-contracts.md` is **not**
  modified.