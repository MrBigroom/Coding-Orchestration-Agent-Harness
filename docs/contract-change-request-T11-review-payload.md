# Contract-change request CCR-T11-01 — archive the reviewer input as `review_payload.md`

- **Status:** PROPOSED — awaiting an operator decision. `docs/frozen-contracts.md` is **not**
  edited by T-11; this request is the declaration that the artifact layout addition needs a
  human decision.
- **Raised by:** T-11
  (`task-11-the-reviewer-prompt-and-payload-are-never-archiv`), 2026-11-04.
- **Reason it is a request and not a change:** the frozen artifact layout lists
  `runs/<run_id>/{prompt.md, stdout.log, stderr.log, patch.diff, diffstat.txt, test.log,
  test_summary.txt, handoff.md, handoff.json, review.md, review.json, review.raw.txt?,
  verification.md, verification.json, run.json}` (`docs/frozen-contracts.md`, "Artifact directory
  layout"). `review_payload.md` is a new file name in that directory. T-11 DoD 1 explicitly asks
  for the archive and for this PROPOSED request, so the code lands now and the layout stays
  unfrozen until a human approves it.

## What lands

At review time `review_service.architect_review()` writes one additional file:

```
runs/<run_id>/review_payload.md
```

It contains, in order:

1. the **exact** reviewer input, `BuiltPrompt.static_text() + "\n\n" + BuiltPrompt.dynamic_text()`,
   written verbatim (the prompt is already redacted before it reaches a provider, so no second
   redaction pass rewrites the archived bytes);
2. a **truncation manifest** — one row per dropped item with `section`, `file / item`,
   `bytes available`, `bytes included`, `budget`, and `reason`. The rows are produced by the same
   code that performed the cut (`prompt_builder.TruncationRecorder`), including the per-file diff
   budget, `max_log_tokens`, the contracts-document cut, history, and the aggregate
   `max_prompt_context_tokens` shave in `enforce_budget`;
3. **harness-detected missing artifacts** (computed from the run directory) in their own section;
4. **reviewer-asserted missing artifacts** (supplied by the model) in a separate section;
5. **absence checks** against the task worktree: every concrete file/test/line claim marked
   `confirmed`, `refuted` or `unchecked`;
6. **diff completeness** findings from the git diffstat, `git check-attr -a`, and the supplied
   patch, including whether a hiding attribute predates the task.

## Why

Measured on reviews.7 (T-8 round 5): the reviewer asserted that three required proofs were
missing. They all existed on the branch. The reviewer also saw a handoff block cut off at DoD 5.
Nothing in `runs/<run_id>/` could show what the model had been given, so the void finding cost a
45-minute paid round (run 15). Archiving the input plus its truncation manifest makes "the
reviewer was shown X" checkable after the fact. Checking absence claims against the worktree
prevents a refuted finding from spending another round.

## Shape

- Additive only: one new file name. No existing run artifact changed name, shape or meaning.
- `review.json` keeps its frozen shape. Provenance is recorded in its existing `notes` and
  `missing_artifacts` arrays, with `harness-detected:` / `reviewer-asserted:` labels; no new
  top-level key is introduced.
- No REST API route or response changes.
- Two **additive** build-log events: `review_absence_refuted` (the model claimed missing evidence
  the repository disproves) and `review_diff_incomplete` (a changed file has no hunk in the
  supplied patch, or an attribute hides it). The frozen line shape `{timestamp, phase, task_id,
  run_id, role, event, details, commit_sha}` is unchanged.

## Proposed contract text (for `docs/frozen-contracts.md` if approved)

> **Artifact directory layout**: extend the `runs/<run_id>/` list with
> `review_payload.md` — the exact reviewer input (`static_text()` + `dynamic_text()`), the
> truncation manifest (item, bytes available, bytes included, budget, reason), separately labelled
> harness-detected and reviewer-asserted missing artifacts, the mechanical absence checks, and the
> diff-completeness findings. The file is additive; no existing artifact name or shape changes.

## Safest options

1. **Approve as written.** Zero code change; the file already has this shape and is what the
   reviewer-audit tests read. Freezing it stops a later phase repurposing the name.
2. **Leave it unfrozen** (this branch's state, the T-6 precedent). The audit trail works today,
   but a later phase could delete or rename the file without tripping a contract check.
3. **Write the archive outside `runs/<run_id>/`** (for example `ORCHESTRATOR_HOME/evidence/` like
   T-10's pre-round handoff). Cost: the evidence is then not next to the `review.json` it explains
   and is not archived with the run, which is exactly the invisibility this task is fixing.

## Blocking?

No. T-11's implementation and tests are complete under option 2; only the layout remains
PROPOSED until the operator decides.
