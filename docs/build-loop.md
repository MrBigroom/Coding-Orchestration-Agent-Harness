# Build loop

The role loop is a **state machine over artifacts**. Roles never chat with each other.
Each step reads the artifacts it needs and writes new ones, and every transition is
validated (`src/orchestrator/core/state.py`) and logged.

## Task lifecycle

```
draft ─► planned ─► queued ─► in_progress ─► review ─► accepted ─► archived
            ▲          │           │            │
            │          │           ├─► blocked ◄┤ (BLOCKED handoff / NEEDS_HUMAN / round limit)
            │          │           │     │      │
            └──────────┴── failed/timeout/cancel│
                                         ▼      └─► queued (CHANGES_REQUESTED or failed
                                    queued / review          verification → same branch)
                                                       rejected (human) ─► queued (retry)
```

| From | Allowed to |
|---|---|
| draft | planned, queued, archived |
| planned | draft, queued, blocked, archived |
| queued | in_progress, planned (cancel), blocked, archived |
| in_progress | review, blocked, planned (failed/timeout/cancelled run), rejected |
| review | accepted, rejected, queued (changes requested), blocked |
| blocked | queued (unblock), planned, review (operator decides), rejected, archived |
| rejected | queued (retry), planned, archived |
| accepted | archived |

`accepted` is reachable **only from `review`** (covered by a test).

Run states: `pending → preparing → running → testing → completed`, with terminal
`failed | cancelled | timeout`.

## One Worker run (`run_service.execute`)

1. **pending → preparing.** The router picks the harness and model profile
   (`task dry-run` shows the choice without executing). The run creates or reuses the
   task's worktree `ORCHESTRATOR_HOME/worktrees/<project>/<task_id>` on branch
   `phase-N-<slug>`.
2. **Check git status before (re)trying.** A dirty worktree from an aborted turn gets a
   clearly labelled `chore(orchestrator-recovery): …` commit inside the worktree, and the
   recovery is logged as `recovery_commit`. The commit is machine-identifiable (see “Recovery
   commits” below); untracked worker output is swept too, so nothing the Worker wrote is
   discarded, and sensitive paths are never staged.
3. **Build the worker prompt** in cache order (see `prompt-caching.md`), including the
   latest reviewer feedback on retries and, when the branch carries recovery commits, the
   list of them plus the instruction to re-verify before handing off. Saved as
   `runs/<id>/prompt.md`.
4. **running.** The adapter command runs inside the worktree with a minimal environment.
   stdout/stderr are redacted and streamed to `runs/<id>/stdout.log|stderr.log` and the
   WebSocket.
5. After exit: leftover changes get a recovery commit too (a killed agent leaves its edits
   uncommitted), and `patch.diff` + `diffstat.txt` are collected against the base branch.
6. **Failure paths.** A non-zero exit → run `failed`. Exceeding the adapter timeout →
   `timeout`. A `CANCEL` marker → `cancelled`. The failure paths emit `run_failed` / `run_timeout`
   (a cancel is recorded on the `task_status` line); a run that finishes normally emits
   `run_completed`. **Before the task goes back to `planned`,** the harness tries to salvage a
   `handoff.json` the killed Worker already wrote — see “Salvaging a handoff from a killed run”.
   With no usable submission the task returns to `planned` for a deliberate retry, exactly as
   before — **unless the attempt cap has fired**: once `max_run_attempts_per_task` attempts have
   been spent, the task goes to `blocked` instead of `planned`, so a timeout/cancel loop cannot
   run forever. Every new round clears the previous round's lifecycle markers (see
   “What a new round resets”) and its prompt states what became of the previous round's
   submission (salvaged, absent, or archived at round start). The `in_progress → planned`
   requeue reason and the attempt-limit `blocked` reason carry the same submission-status
   clause, so the decision to retry is visible even before the next prompt is built.
7. **Handoff.** `.orchestrator/handoff.json` in the worktree (git-excluded) is
   validated and copied to `runs/<id>/handoff.json`, with `handoff.md` rendered next to
   it. If the worker produced none, the orchestrator writes a **synthesized** handoff
   marked as such, and that counts as a missing artifact at review.
8. `memory_suggestions` are stored as **pending** memory entries.
9. **BLOCKED handoff** → task `blocked` (waiting for a decision, not "still working").
   The run completes and nothing is retried automatically.
10. Otherwise **testing**: the task moves to `review` and the Supervisor gate runs.

## Recovery commits

A turn that is killed (timeout, cancel, crash) leaves its edits sitting uncommitted in the
worktree. The harness never discards them: it commits them. A subject line alone is not
enough, because a half-finished edit then looks exactly like authored work to the next run,
to the reviewer and in the diff — so a recovery commit is **machine-identifiable**:

- **Subject** `chore(orchestrator-recovery): <label>` — unchanged format, because tests, docs
  and GUI displays match on it (`git_service.RECOVERY_SUBJECT_PREFIX`).
- **Trailer** `Orchestrator-Recovery: <run_short_id>` (`RECOVERY_TRAILER_KEY`) naming the
  aborted run whose work is swept — not the run that finds it. Detection is by trailer
  (`git_service.recovery_run_of` / `CommitInfo.recovery`), never by subject matching: a
  hand-authored commit that copies the subject is *not* a recovery commit.
- **Body** states plainly that the content was never self-checked, and lists
  `Tracked modifications (n): …` separately from `Newly added untracked paths (n): …`.
- **Build-log event** `recovery_commit`, line shape unchanged; the structured payload rides
  inside `details` as `… | findings_json={"recovery": true, "run_short_id": …,
  "sha": …, "tracked_modified": […], "untracked_added": […]}`
  (`utils/structured_log.py`, `git_service.recovery_event_details`). Old lines without the
  marker parse to `None`.
- **Next run's prompt**: if the branch carries recovery commits, `run_service` lists them
  (short sha, subject, tracked vs newly-added paths) and requires the Worker to re-verify with
  the project's lint/test commands, repair or report every defect they introduced, and emit a
  BLOCKED handoff with a contract-change request instead of continuing if one touched a
  contract file the phase does not authorize. Logged as `recovery_notice_included`.
- **Reviewer payload**: `review_service.recovery_section` + `harness_findings_section`, rendered
  into the Architect's prompt by `prompt_builder._dynamic` ("Recovery commits on this branch",
  "Harness-detected findings"), and the GUI marks the diff with a warning block
  (`TaskDetail.tsx` `RecoveryNote`) built from the
  `recovery_commits` field of `GET /api/tasks/{id}` — that field is the only place the data is
  served; there is no `GET /api/tasks/{id}/recovery` route (reviewer ruling: the route was dead
  code, nothing read it, and no DoD item asked for a new endpoint).
  Since T-11 the exact payload that reached the reviewer is archived as
  `runs/<id>/review_payload.md`, together with its truncation manifest — see
  "Review input auditability" below and the PROPOSED layout request
  `docs/contract-change-request-T11-review-payload.md`.
- Sensitive paths (`.env`, keys) are **never** staged into a recovery commit; they stay on
  disk untracked and are reported in `skipped_sensitive`.

A recovery commit is not itself a rejection reason and not itself an approval: it is
information that the content has no self-check behind it. What *does* fail the gate is what
re-verification finds in it (a lint error, a failing test) or a harness-detected contract-file
hit — see “Unauthorized contract edits”.

**Boundary:** detection is the trailer and nothing else. Recovery commits created *before* the
trailer existed — any `chore(orchestrator-recovery): …` commit without `Orchestrator-Recovery:`,
including this repo's own history — are **not** flagged. There is deliberately no fallback to
subject matching: a subject match cannot distinguish wreckage from a hand-authored commit that
copies the wording (a case covered by a test), so the harness reports what it can prove and
stays silent about the rest. A reviewer who sees the historical subject prefix without the
trailer should treat it as a human question, not as verified work.

## Salvaging a handoff from a killed run

A Worker writes `.orchestrator/handoff.json` before it finishes. A run that is killed — adapter
timeout, `CANCEL` marker, non-zero exit, harness-reported error — can therefore leave a complete,
valid submission on disk that nobody reads. Measured on run 11 (T-8 round 2, 2026-11-04): a
36184-byte handoff validated cleanly and was never ingested; the round was treated as if the
Worker had produced nothing. The bug had **two halves**, and fixing one without the other does
not recover the round:

1. **The early return.** `_execute` took the non-clean-exit branch, set the terminal run status,
   set `handoff_status = RUN_TIMEOUT`/`RUN_FAILED` (or transitioned `run cancelled`), and
   **returned** — before `adapter.parse_result`, before the synthesized-handoff fallback, and
   before `runs/<id>/handoff.json` was written. Ingestion simply lived after the `return`.
2. **The round-start unlink.** The next attempt began with
   `handoff_path.unlink(missing_ok=True)` and no archive, so a retry after a timeout actively
   *destroyed* the previous round's submission. The operator could only recover it by copying the
   file out in the gap between runs, which is what happened for run 11.

Now the non-clean-exit path attempts ingestion **before** returning (`run_service.try_salvage_handoff`),
and the round-start unlink archives first (`run_service.archive_before_round_start`).

### Salvage modes

Recorded as `mode` in the `handoff_salvaged` details payload:

| mode | when | what lands |
| --- | --- | --- |
| `non_clean_exit` | the run was killed/errored and a `handoff.json` was on disk when the result was read | `runs/<id>/handoff.json` + `handoff.md`, `run.json` gains `handoff_arrival`/`handoff_delivery_evidence`, the run continues to verification + review |
| `pre_round_archive` | a submission was still in the worktree at the start of the next round | copied to `ORCHESTRATOR_HOME/evidence/pre-round-handoff/<project>-<task>-<run>-<ts>/` (`handoff.json`, plus `handoff.md` when it validated) and only then unlinked |
| `already_archived` | the leftover is byte-identical to the previous round's `runs/<id>/handoff.json` | nothing copied, nothing logged, unlink allowed — firing an event on every ordinary retry would say nothing |

All three are best-effort by design: a file that cannot be read or archived leaves the unlink
skipped and logs `handoff_salvage_failed`. Keeping a stale file in the scratch dir is
recoverable; deleting an unread submission is not. Because that means a stale file can survive
into the next round, the round notes a fingerprint of it (`run_service.handoff_snapshot`: stat +
sha256) when the archive fails, and the salvage path refuses a file that is byte-for-byte
unchanged at the end of the run — it is the previous round's submission, not this round's, and
attributing it to this run would be a provenance failure. The clean-exit path applies the same
rule and synthesizes from observed evidence instead. A Worker that writes a new handoff over the
leftover changes the fingerprint and is salvaged normally.

### Lifecycle: SALVAGED is not SYNTHESIZED and not READY_FOR_REVIEW

A salvaged submission is a third thing, and the task row says which:

- `handoff_status = SALVAGED` (`artifact_service.HANDOFF_STATUS_SALVAGED`) — the Worker's own
  status (`READY_FOR_REVIEW`, `CHANGES_APPLIED`, `BLOCKED`) is preserved **inside** the archived
  handoff and in the event payload (`handoff_status`); the task-level field records how the
  submission arrived.
- `SYNTHESIZED` means the orchestrator wrote a handoff from observed evidence because none
  existed. `SALVAGED` means the Worker did write one; a reviewer must not read the first as the
  second, or a killed round's real submission is thrown away as orchestrator filler.
- `RUN_TIMEOUT` / `RUN_FAILED` are what the task carries when **no** usable handoff was salvaged.
  That remains exactly today's behaviour: task back to `planned`, no verification row, no review.
- The **run** keeps its terminal status (`timeout`, `failed`, `cancelled`). The frozen run
  vocabulary has no edge out of a terminal state, and widening it to `completed` would hide the
  fact that the process was killed from every consumer that watches run outcomes — including the
  T-9 notification fan-out. A salvaged run is a killed run that was not wasted.
- **Routing: `NEEDS_HUMAN`.** When everything else is green, a salvaged submission goes to the
  Architect reviewer and then to `task blocked` with `review_status = NEEDS_HUMAN`, never to
  `accepted` and never to an automatic merge. The Worker process never saw the gate result and
  cannot apply reviewer feedback on the same branch; the operator accepts or retries
  (`blocked -> queued`) with the handoff, review and verification artifacts attached. If the gate
  fails, the round is requeued exactly like any other failing round.

### Events

`handoff_salvaged` is a new, **additive** build-log event (no existing name or line-shape key
changed). `details` carries `mode`, `run_status`, `exit_code`, `timed_out`, `cancelled`, `error`,
`handoff_status`, claim/commit counts, and for the pre-round archive the `source_run`,
`source_run_status`, `valid`, `parse_error`, `bytes` and `archived_to`. Its summary states the
exit state and that the handoff arrived out of band. A present-but-unusable file emits
`handoff_salvage_failed` instead — a truncated write from a killed process is the normal shape,
so parse failures are logged, never raised. `handoff_salvage_failed` also covers a file left over
from a previous round when the round-start archive could not copy it: the payload carries
`reason=unchanged since round start` and `stale=true`, and the file is refused rather than
attributed to this run.

### Salvaged handoffs are provenance-limited evidence

A salvaged handoff is **not** worker self-certification and it does not skip the gate: the
Supervisor verification runs the project's real commands (the T-6 wrapper included), the delivery
evidence attached to the run is compared against git, and the Architect review still runs. It is
also
weaker evidence than a clean-exit handoff, because an early handoff is partly a statement of
*intent* — measured on run 13 (cancelled at 01:33:05): a 1164-byte handoff claimed four code/doc
fixes while the branch had no new commits and the worktree had no tracked modifications.

So every ingestion path — clean exit **and** salvage — archives delivery evidence in
`runs/<id>/run.json` (`handoff_delivery_evidence`): the **baseline sha this run started from**, the
commits created by this run (`git log baseline..HEAD`, with the recovery flag), the earlier
commits on the branch that predate this run, the diffstat vs the merge-base,
`git status --porcelain -uall` at ingestion time, and the dirt that existed *before* the harness
recovery sweep. The baseline is the previous run's `commit_sha` for the same task, falling back to
the merge-base on a first run — so a retry that creates **zero** commits cannot inherit the
previous round's commits as its own delivery. Each claimed path is classified `worker_commit`
(this run, Worker-authored), `recovery_commit_only` (this run, only inside a harness recovery
commit), `uncommitted_only` (worktree state, not on the branch), `prior_run_commit` (on the
branch, but created by an earlier round) or `absent`, and
`claims_all_backed_by_this_run_commits` is the one-line answer. The reviewer prompt renders the
same evidence, so a reviewer sees a promise labelled as a promise rather than reading it as
delivered work.

This is **attached provenance, not a new gate**. T-10 does not add a verification check for
claims, and deliberately does not make `recovery_commit_only`, `uncommitted_only` or
`prior_run_commit` fail automatically: a harness recovery commit is still content on the branch,
and how recovery commits are treated is T-8's machinery (`recovery commits (harness-created)`),
which T-10 must not widen. The reviewer and the operator read the flag — a salvaged round already
routes to `NEEDS_HUMAN` — and the older "claimed files exist on disk" check is unchanged. Turning
the flag into a gate failure would have made every round that needed a recovery sweep reject its
own committed work, which is the opposite of recoverable.

## What a new round resets

`handoff_status`, `review_status`, `verification_status` and `merge_status` describe the
**round that just ended**. Leaving them set while a new run is live makes the task row report
`in_progress` next to `RUN_TIMEOUT` at the same time, which mislabels the GUI header and
invites a reader to treat a live run as a failed one. So `task_service.clear_round_fields`
(`ROUND_FIELDS`) runs at the one place a round actually starts — the top of
`run_service._execute`, right after the run enters `preparing` — and logs
`retry_fields_reset` with the fields it cleared.

Deliberately **not** cleared on a requeue (`transition → queued`): a task queued after
`CHANGES_REQUESTED` must keep showing that verdict, because it is the feedback the Worker acts
on. The reset belongs at the start of a round, not at the end of one. `status` itself is not
cleared — it is the state machine's business (`core/state.py`, frozen).

## Unauthorized contract edits

Editing a contract document (`docs/plan.md`, `docs/product-spec.md`, `docs/architecture.md`,
`docs/workflow.md`, `docs/frozen-contracts.md`) is a decision only a human may authorize, and
the half-finished wreckage a recovery commit sweeps in can contain exactly that. The harness
computes it — a Worker or reviewer asserting authorization cannot make the claim true:

1. the changed-file set is the branch diff **plus** the worktree's own dirt (`tracked_modified`
   and `untracked_added`, so an uncommitted edit cannot hide),
2. intersected with `project_service.contract_files()`,
3. minus the files the task **explicitly grants** in its stored `definition_of_done`,
   `acceptance_criteria` or `frozen_contracts` (`authorized_contract_files`).

Step 3 is a **grant test, not a mention test**. A file is authorized only when one clause both
names it and grants a change to it — "authorized to amend `docs/frozen-contracts.md` to add the
ORCH_VERIFY field names". Three things are *not* authorization, each covered by a test:

- a **mere mention**: T-8's own DoD 8 lists all five contract files while describing the detector,
  so a mention rule would have authorized the exact edit it exists to catch;
- a **prohibition**: "Do NOT modify `docs/frozen-contracts.md`" names the file in order to forbid
  touching it; where negation and a grant word collide in one clause the prohibition wins, so the
  control fails closed;
- the task **description**, a commit message or a diffstat — prose about a contract file is not
  permission to change one, and a recovery commit's own subject can never launder authorization.

An unauthorized hit emits `contract_change_detected` with a `findings_json` payload, fails the
`contract files unchanged` verification check (so green tests do not save it), and is handed to
the reviewer as a harness-detected finding. **Decision: it routes straight to `blocked` with
`review_status=NEEDS_HUMAN` instead of burning a review round** (`supervisor_service.contract_change_hold`)
— a requeue would ask the Worker to fix something it is forbidden to fix. It does not reach the
reviewer for the same reason. Recovery-commit paths are named as such in the finding.

## Supervisor gate (`supervisor_service.after_run`)

1. **Live verification**, recorded in `verification.json` and `verification.md`:
   - handoff present
   - claimed files exist on disk and match the diff
   - project test command (re-run independently in the task worktree and written to
     `test.log`; a worker claim of green tests that the run contradicts sets
     `contradicts_worker`). **Skipped entirely when `test_command` is null** — see
     “Verification commands must be path-independent” below
   - lint and build commands when configured (same worktree, same null-means-N/A rule)
   - **contract files changed without authorization** (`contract files unchanged`) —
     computed by the harness from the branch diff *plus* the worktree's own dirt, and
     authorized only by an explicit grant in the task's stored `definition_of_done` /
     `acceptance_criteria` / `frozen_contracts` (a mention, or a prohibition naming the file, is
     not a grant).
     A failing check, so it fails verification even when the tests are green
   - **recovery commits on the branch** (`recovery commits (harness-created)`) —
     informational: it never rejects and never approves, it names the commits whose content
     no Worker ever self-checked
   - migrations, live endpoints, and LLM coherence recorded as N/A with a reason unless
     in scope
2. **Tests not green or no handoff** → no Architect review. The task goes back to the
   Worker on the same branch.
3. **Harness-detected contract change** (after tests/handoff, before the reviewer) →
   `blocked` with `review_status=NEEDS_HUMAN` and a `contract_change_detected` build-log
   event. It is not folded into a generic rework round: only a human may change a contract,
   so asking the Worker to "fix" it would ask it to break the same rule again.
4. **Architect review** (coding/maintenance/review tasks, or any high-risk task). It sees
   only artifacts: the diff, handoff, test summary, verification narrative, the recovery
   commit list, the harness-computed contract findings, and a list of missing artifacts.
   Sections are capped by `max_log_tokens` / `max_diff_tokens`; the exact bytes sent and every
   cut that was made are archived in `runs/<id>/review_payload.md` — see
   "Review input auditability" below.
   - an absence finding ("file/test/line X is missing") is checked mechanically before the
     verdict is acted on, and the two kinds of claim are checked differently (T-11): an
     **existence** claim is refuted when the file is in the worktree; a **visibility** claim
     ("not supplied / truncated / not in the diff") is never refuted by existence — it is
     checked against the supplied patch and the archived truncation manifest, and is
     `unchecked` when neither can decide it. A **refuted** claim is recorded
     (`review_absence_refuted`) and cannot on its own requeue the Worker; `confirmed` and
     `unchecked` claims keep their normal weight.
   - `NEEDS_HUMAN` → `blocked`
   - `CHANGES_REQUESTED` → back to the Worker on the same branch, unless *every* remaining
     finding was a claim the harness refuted. That case does not requeue and is **not** turned
     into an APPROVED verdict either: the verdict becomes `NEEDS_HUMAN` with a "review verdict"
     finding, because refuting a claim about the evidence is not evidence that the code is
     correct, and the harness does not decide who may accept a task.
   - after `max_review_rounds` verified rounds (default 3) → `blocked`
5. **APPROVED + verification pass** → `accepted`. Nits in an APPROVED review are logged as
   *deferred in writing* (`nits_deferred`). Agent tasks without high risk skip the
   Architect, so cron jobs don't spend Opus tokens.
6. **Merge gate** (below).

### Verified rounds versus attempts

Two limits bound the loop, and they count different things. `max_review_rounds` (default 3)
counts a **verified round**: a run that reached the gate (`completed`, or `testing` while gated).
`max_run_attempts_per_task` (default 6) counts **every adapter attempt**, including
`timeout`/`failed`/`cancelled` runs, because each costs a full paid adapter window without buying
a verified round. A completed-only flow still blocks at three review rounds; a timeout loop blocks
after six attempts instead of requeueing forever. Either limit firing moves the task to `blocked`,
names the fired limit and the `completed / timeout / failed / cancelled` breakdown in the reason,
and emits the additive `task_limits_exhausted` event. Tasks and Task Detail render the derived
`attempts` summary (“6 attempts, 2 verified rounds”) from `GET /api/tasks[/{id}]`; it is never
stored.

## Verification commands must be path-independent

The gate runs the project's test/lint/build commands with **cwd = the task worktree**
(`supervisor_service._run_command` → `process_service.run_simple`). That has two consequences
that are easy to miss and expensive to discover mid-gate:

1. **`git worktree add` copies tracked files only.** A worktree has no `.venv`, no
   `node_modules`, no built assets. A venv-relative command (`make test`,
   `.venv/bin/pytest`) exits 127 there → verification fail → `CHANGES_REQUESTED` → requeue
   loop until `max_review_rounds` → `BLOCKED`, burning one Worker run per round with zero
   new signal.
2. **An editable install points at the main checkout — which is worse than failing.** If the
   interpreter's `site-packages` holds an editable install of the project (a
   `_editable_impl_*.pth` + `direct_url.json` pointing at the main checkout), then reusing
   that venv from a worktree makes `import <package>` resolve to the **main** checkout: the
   suite goes green while verifying code that is not the branch under review. That is a
   false pass, the exact failure mode independent verification exists to prevent, and
   `PYTHONPATH` order is the only thing standing between the two checkouts.

So a command that can be verified by the gate must be **path-independent**: it finds its
interpreter/toolchain wherever it lives, points the import path at the *worktree*, and then
**asserts** the resolution instead of assuming it. `scripts/orch_verify.sh` in this repo is
the reference pattern (use it as the template for other projects):

- `git rev-parse --show-toplevel` → the worktree root, `git rev-parse --path-format=absolute
  --git-common-dir` → the main repo (its `.venv` supplies the interpreter, by absolute path);
- the inherited `VIRTUAL_ENV` is **ignored** (it points at whatever venv the server runs in,
  and `core.security.BASE_ENV_ALLOWLIST` passes it through to children);
- `PYTHONPATH` is set to the **worktree** `src` before invoking `pytest`/`ruff`; PYTHONPATH
  takes precedence over the editable `.pth`;
- it prints the resolved module path and the test root into the artifact log, and exits 3 if
  the resolved import path is not under the worktree root. The run also emits one
  machine-readable line: `ORCH_VERIFY|mode=…|module=…|module_file=…|worktree_root=…` (stdout
  or stderr; `|`-delimited so paths with spaces survive; keys are order-independent). That
  line is a **repo-internal convention**, not a frozen contract — freezing it needs a
  contract change, requested in `contract-change-request-T6-orch-verify-marker.md`.
- it `cd`s to the worktree root **before** resolving the import, because `python -` and
  `python -m <tool>` both put the current directory first on `sys.path`: asserting a
  resolution taken from the invoking directory would not describe the run that follows.

Keep the output **count-parseable**: `parse_test_counts` reads the runner's summary line, and
a second `-q` (`pytest -q` on top of this repo's `addopts = "-q"` → `-qq`) suppresses it, so
the gate's green-claim comparison silently sees `passed: 0`. Pass `-k`, `-x` or a path to the
wrapper instead of more quiet flags.

`orchestrator project doctor <name> --run-commands` (opt-in; off by default, and not exposed
on the API or in the GUI — `GET /api/projects/{id}/doctor` keeps its static shape, pinned by
`tests/integration/test_api.py::test_project_doctor_route_stays_static`) executes each configured command in a throwaway worktree of the
base branch and reports both the exit code and whether `module_file` landed inside that
worktree — so the trap shows up at project registration instead of mid-gate. A command that
exits 0 **without** an `ORCH_VERIFY` marker is reported as a failure: an unproven resolution
is not a proven local one. The throwaway worktree is removed on every exit path (green, red,
timeout, command that cannot be started); if the command leaves the worktree dirty, removal is
skipped, reported as a failed check, and `orchestrator cleanup` finishes the job.

Because that probe snapshots the **base branch**, a wrapper introduced by a task branch is not
in the snapshot yet and its check exits 127 until the branch merges — an expected red the first
time, not a broken wrapper. `scripts/orch_verify.sh test` from inside the branch worktree is the
pre-merge way to prove the same thing.

Registering a project with a null `test_command` avoids the 127 loop, but it also switches
off the green-claim contradiction check (`supervisor_service.py`, the
`if handoff and project.test_command:` branch): with no command configured the gate never
re-runs anything, so a Worker claiming green tests is never contradicted and a false pass
rides straight through. Prefer a path-independent command over a null one; if a project truly
cannot be verified, record it in `verification.json` as `not_applicable` and read the Worker's
test claims as unverified.

## Handoff artifacts

`runs/<run_id>/handoff.md` and `handoff.json` (schema in `frozen-contracts.md`). The
human block has: Branch, Summary, Files changed, Tests (command → counts), DoD
self-check, New dependencies, Assumptions, Questions, Blockers.

## Review artifacts

`runs/<run_id>/review.md` + `review.json`. Parsing fails closed:
- valid JSON is used, but if a `VERDICT:` line disagrees with it, the review counts as
  ambiguous;
- otherwise the `VERDICT:`/`FINDINGS:` text is parsed;
- otherwise the result is `CHANGES_REQUESTED` with an "ambiguous review output" finding,
  and the raw text is saved to `review.raw.txt`.

Missing required artifacts are always added as findings, and an `APPROVED` verdict with
missing artifacts is downgraded to `CHANGES_REQUESTED`. If the reviewer claims it
executed anything ("I ran…"), a note records that the claim is disregarded.

`runs/<run_id>/review_payload.md` is the archived reviewer input (T-11). Its name is a
**PROPOSED** addition to the frozen `runs/<run_id>/` layout, declared in
`docs/contract-change-request-T11-review-payload.md`; `docs/frozen-contracts.md` is untouched
until a human approves that request. `review.json` keeps its frozen shape: provenance is
recorded in the existing `notes` and `missing_artifacts` arrays, not in a new key.

### Review input auditability

A reviewer claim about *absent evidence* is now machine-checked before it can cost a round, and
the input itself is archived so "the reviewer was shown X" is answerable from disk.

- **Exact input.** `review_payload.md` starts with `static_text() + dynamic_text()` exactly as
  built, and then a **truncation manifest**: section, file/item, bytes available, bytes
  included, the budget that caused the cut, and the reason. The recorder sits in the same code
  that performs the cut (`prompt_builder`), so a cut cannot happen without a row — including the
  sensitive-file strip (`.env`, keys), which used to vanish between `patch.diff` and the prompt.
- **Per-file diff budget.** A large diff is split by file and ordered source-first (code is
  funded before the evidence that describes it). Files are included **whole** whenever they fit
  in what is left — a reviewer verifying wiring needs one complete file far more than the first
  tenth of five, which is what a flat per-file split produced (round-3 reviewer finding 5). A file
  that cannot fit whole gets as many complete hunks as the remaining budget allows, then an
  inline `... [review diff: N of M hunks shown for <path>; ... omitted ... (@@ headers named) ...]`
  marker, then the rest are named as omitted. One file may consume at most half the budget so a
  single generated artifact cannot starve the rest, and the whole thing stays inside
  `max_diff_tokens`: the budget is never widened.
- **Two labelled absence sections.** Harness-detected missing artifacts (computed from the run
  directory) and reviewer-asserted missing artifacts (the model's own claims) are separate in the
  payload. The mechanical checks table marks every concrete file/test/line claim `confirmed`,
  `refuted` or `unchecked`, and says what it was checked against.
- **Existence is not visibility.** A file that exists on disk does not refute "its hunks were not
  supplied". Visibility claims are compared with the supplied patch and the truncation manifest;
  `refuted` means the evidence was in fact supplied in full, `confirmed` means the manifest
  records the cut, and `unchecked` is used whenever neither can decide it (fail closed). A
  `refuted` claim removes the finding and is logged (`review_absence_refuted`), but a verdict is
  never upgraded on it: the round goes to `NEEDS_HUMAN`, not to `APPROVED`.
- **Diff completeness.** Before the review ships, the harness compares the git diffstat with the
  supplied patch (`git diff --numstat` vs the archived `patch.diff`): every changed file must
  have a hunk body, and `git check-attr -a` is run for `-diff` / `export-ignore` on every changed
  path. A hiding attribute this task introduced is a harness finding (and an additive
  `review_diff_incomplete` build-log event); one that predates the merge-base is reported as
  pre-existing and is not a reason to fail the run. A Worker cannot opt out of review by
  configuring `.gitattributes`.

Measured motivation: reviews.7 (T-8 round 5) asserted three required proofs were missing; all
three existed on the branch, and the reviewer had also received a handoff block cut off at DoD 5.
The void finding requeued a 45-minute paid round (run 15).

## Verification artifacts

`runs/<run_id>/verification.md` + `verification.json`, plus `test.log`,
`test_summary.txt`, and `lint.log`/`build.log` when configured. When a check contradicts
the worker's story, the check wins.

## Build log

Every event is appended to `ORCHESTRATOR_HOME/build-log.jsonl` and mirrored to the
`build_log_entries` table. Each line looks like:
`{timestamp, phase, task_id, run_id, role, event, details, commit_sha}`. Events include
`task_status`, `run_created`, `worktree_created`, `recovery_commit`, `run_failed`,
`run_timeout`, **`run_completed`**, `supervisor_verification`, `architect_review`,
`nits_deferred`, `merge_candidate`, `merged`, `merge_refused`, `schedule_fired`,
`standalone_run`, `escalated`, `human_accept`, `recovery_notice_included`,
`retry_fields_reset`, `contract_change_detected`, `handoff_salvaged`,
`handoff_salvage_failed`, **`task_limits_exhausted`**, **`review_absence_refuted`**,
**`review_diff_incomplete`**, `run_error`. The GUI Build Log page and
`GET /api/build-log` read it.

### Terminal run events

A run's terminal status is `completed | failed | timeout | cancelled`. Two of the four had a
named event (`run_failed`, `run_timeout`); `cancelled` shows up only as a `task_status` line
reading `run cancelled`, and a run that finished normally had none — there is no
`run_cancelled` event name either. (`run_error` exists but is not a terminal *status*: it labels
an unexpected exception inside the harness, after which the run still ends as `failed`.) A green
run was therefore observable only as a `task_status` transition, so any consumer had to *derive*
the single most important trigger indirectly. `run_completed` is that missing event.

**Where each terminal event is emitted (deliberate split, reviewer ruling 2).**
`run_completed` is emitted inside `run_service._set_run`, the single writer of `run.status`, so it
is impossible to set `completed` without logging it. `run_failed` / `run_timeout` stay where they
were: emitted from `_execute` immediately after the `_set_run` call that sets the terminal status,
because at that point `_execute` still holds the run-level error context (`run.error`, the exit
code, the timeout reason) that their `details` carry, and `_set_run` does not. Moving them into
`_set_run` would also fire them on the unexpected-exception path, which has its own `run_error`
line. The shared chokepoint is the **status transition** (`_set_run` is the only writer, and every
terminal status except `cancelled` has exactly one named event — `run_cancelled` does not exist
yet and is a T-9 requirement, so the honest claim is "three of four"), not the `log_event` call site. The consumer that motivated
the event — the **T-9** notification fan-out — is the reason it must be a named event at all. It
is **additive** — no existing event name and no line-shape key changed — and adding it to the
vocabulary was authorized by this task's Definition of Done, so `docs/frozen-contracts.md`
itself is untouched (it does not enumerate build-log event names; the frozen list is the
*line shape*).

Today no in-repo consumer infers run success from a task-status transition (the Build Log page
renders events as they arrive; the Dashboard counts `task.status`, which is a task verdict, not
a run outcome), so there was nothing to migrate. The notification fan-out **T-9** must express
its trigger set against these events rather than deriving success: `run_completed` (a run
finished normally — the success trigger), `run_failed`, `run_timeout`, `run_error`, `escalated`,
`merge_refused`, and a `verdict_recorded`/`task_status` line whose reason is `blocked`.
Notifications key off events, not off status inference.

`details` is redacted (`log_service.log_event`), and the two `sk-` credential patterns in
`utils/redaction.py` carry a leading anchor — the lookbehind `(?<![A-Za-z0-9])`, not `\b`:
`_` is a word character, so `\bsk-` would miss `TOKEN_sk-proj-…`, which is a shape keys really
do appear in. Only a real prefix start matches now, so operational text stays usable: a branch,
task, phase or path name (`task-8-…`, `risk-…`, `mask-…`) and a `findings_json` payload
containing one must survive byte-identically, while real keys (`sk-ant-…`, `sk-proj-…`,
`gh[pousr]_`, …) still redact wherever they appear. The other credential patterns keep their
historical `\b` anchors unchanged — widening them is not this phase's decision, and the gap that
leaves open (`OPENAI_TOKEN_ghp_…` is not caught today) is pinned by a test
(`tests/unit/test_redaction.py::test_underscore_joined_other_credentials_are_a_recorded_gap_not_a_regression`)
so it cannot be lost by accident. The `merge_candidate` instruction in particular must remain
copy-pasteable — `git -C <repo> checkout main && git -C <repo> merge --no-ff <branch>`.

## Branch strategy

- One persistent branch per task/phase: `phase-N-<slug>` (tasks without a phase number
  use `task-<id>-<slug>`). Retries reuse the same branch and worktree.
- The accepted phase branch is the merge candidate.
- The orchestrator never modifies the base branch except through the gated
  `merge --no-ff`. It never pushes or force-pushes and never deletes branches.
- Cleanup (`orchestrator cleanup --dry-run`) removes only worktrees that have the
  orchestrator's marker, skips dirty ones, and keeps branches.

## Merge gate

| Setting | Default | Effect |
|---|---|---|
| `merge_enabled` | false | allows merging accepted branches (`orchestrator task merge`) |
| `auto_merge_enabled` | false | Supervisor merges automatically after APPROVED + verification pass (requires `merge_enabled`) |
| `supervisor_microfixes_enabled` | false | stored for policy; there is no micro-fix executor (code goes back to the Worker) |

With the gate disabled the task is `accepted`, `merge_status=ready`, and the exact
command is logged and shown:
`git -C <repo> checkout <base> && git -C <repo> merge --no-ff <branch>`.
With the gate enabled, the merge refuses unless the base branch is checked out and
clean, and unless the branch HEAD still equals the verified commit (otherwise
`merge_status=stale`). The merge commit message records the verdict, the verification
summary, deferred gaps, and a pointer to regressions in the build log.

## Contract-change request flow

Frozen contracts are listed in `frozen-contracts.md` and per task (`frozen_contracts`).
When a phase seems to need a contract change:

1. The Worker **stops** and writes a `BLOCKED` handoff. It names the contract in
   `blockers`/`contracts_touched` and lists the safest options in `questions`.
2. The task becomes `blocked` (the GUI dashboard lists it under "waiting for a
   decision").
3. A human (or the Architect, via a planning task) decides. Either approve the change by
   adding an authorizing DoD item, possibly with a new Alembic revision, and
   `task unblock`, or descope and `task reject`/`archive`.
4. Schema changes after baseline always ship as a **new Alembic revision**. The baseline
   (`0001_baseline`) is never edited.

## Verification, evidence capture and test-authoring mechanics

Operational detail that belongs in a doc rather than in injected memory: it is read when
someone edits these paths, not needed by every prompt.

### `scripts/orch_verify.sh` argument handling

The wrapper replaces its pytest collection target **only when an extra argument names an
existing path** (`[ -e "$a" ]`, relative to the worktree root). A bare node id
(`tests/some.py::test_name`) therefore does **not** filter: the id is appended to the whole
`tests` target and the full suite runs. Pass file paths to get a subset.

`pyproject.toml` sets pytest `addopts = "-q"`, so:

- one extra `-v` cancels it and is needed for individual test names (`-vv` to be sure);
- an extra `-q` makes `-qq`, which **suppresses the final `N passed, M skipped` summary
  line**, leaving only progress dots — so the counts cannot be read off the log.

### Capturing evidence into a worktree

- Stage every command output in a scratch directory under `/tmp` and move the files into
  `verify-evidence/<task>/` **after** all captures finish. Writing the first log straight
  into the worktree makes the next command's `git status` stamp report dirty entries,
  which reads as evidence captured against uncommitted code.
- A `dirty-entries=0` stamp additionally requires excluding the transcript path itself from
  the count, and printing that exclusion in the line so it is auditable.
- Capture helpers are committed as evidence under `verify-evidence/<task>/` (e.g.
  `capture.sh`, `capture_gui_typecheck.sh`); they are not `scripts/` files.
- `tsc --listFiles` resolves a `node_modules` symlink to its realpath, so those entries
  appear under the main checkout while branch sources stay under the scratch dir. Split the
  count by prefix rather than reporting "entries NOT under scratch dir: N", and add a
  negative control (same input plus one deliberate type error must exit 1) so a green
  type-check is evidence rather than a no-op.

### Writing CLI output

Any `rich` `console.print` that interpolates operator data (a scope, a branch name, a reason
string) must pass it through `rich.markup.escape` first. An unescaped bracketed value is
parsed as a style tag and silently **deleted** from the printed line — the operator gets
output with the explanation missing. Escape the data, keep markup only in the literal part
of the string, and cover it with a test that puts a bracketed value through the CLI.

### Testing the loop itself (mock provider and mock harness)

Two hermeticity traps, both found by gated runs:

1. `process_service` builds the child environment from `core.security.BASE_ENV_ALLOWLIST`,
   which excludes `PYTHONPATH`, and the repo venv is an editable install pointing at the
   main checkout. So running the mock harness module inside a task worktree executes the
   **main checkout's** copy of the file; a stale copy falls through to the success path
   silently, which is how tests asking for a timeout got a `completed` run.
   `MockHarnessAdapter.run` pins `PYTHONPATH` from its own file location via `extra_env`,
   and the harness entrypoint refuses unknown `mock-*` tags with exit 2.
2. `provider_service.choose_profile` selects the offline mock provider only when the literal
   tag `mock` is present. Behaviour tags such as `mock-timeout-handoff` satisfy the harness
   router (which matches a `mock-` prefix) but **not** provider routing, so an Architect
   review would call the real Opus profile from inside a test: non-deterministic verdicts
   and a slow, paid call per test. Tag such tasks with the bare `mock` tag as well.
