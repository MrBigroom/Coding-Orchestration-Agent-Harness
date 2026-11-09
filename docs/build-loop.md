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
   - an absence finding ("file/test/line X is missing") is **resolved to a path before it is
     judged**, then checked mechanically, and the two kinds of claim are checked differently
     (T-11, resolution added by T-22): an **existence** claim is refuted when the file is tracked
     at the reviewed commit; a **visibility** claim ("not supplied / truncated / not in the diff")
     is never refuted by existence — it is checked against the supplied patch and the archived
     truncation manifest, and is `unchecked` when neither can decide it. A **refuted** claim is
     recorded (`review_absence_refuted`) and cannot on its own requeue the Worker; `confirmed` and
     `unchecked` claims keep their normal weight. `confirmed` requires a located path the
     repository does not contain; a file the repository contains but the budget withheld is
     `unchecked` with the `omitted-from-review-input` state, never a confirmed absence. A bare
     filename that matches exactly one path is judged at that path; a name that matches several
     (or none) is `unchecked` with every candidate listed, because a lookup that found nothing is
     not evidence of absence.
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

## Gated runs are environment-free — and the opt-in parity check

`git worktree add` copies **tracked files only**, so a gated run is deliberately a different
world from the operator checkout: no `.env` (`.gitignore` excludes it), no `.venv`, no
`web/node_modules`, no built assets, and none of the checkout's untracked local configuration.
That is the honest way to verify a branch, but it means a gate green and a local red can both
be true — exactly what happened when the T-9 merge was green in three gated runs and red in the
operator checkout because a module-level app import loaded the developer's `.env` during
collection.

Three things now keep that difference visible and harmless:

1. **The test suite is hermetic by construction.** `tests/conftest.py` derives the clear-list
   from `core.config.settings_env_names()`, which **asks pydantic-settings' own
   `EnvSettingsSource`** for the names it would read — not a parallel prefix+UPPER copy that could
   ignore `validation_alias`, `case_sensitive` or `env_prefix_target`. The tests are behavioural:
   a newly added, aliased and case-sensitive field is read into a real `Settings` instance and
   must be named by the derivation and reported as a gap when the clear-list is stale; a mutation
   check shows the old hand-kept provider list fails. A session-scoped autouse fixture then
   **restores `os.environ` to its import-time snapshot and resets the settings cache after
   collection imports have run**: every key a `.env` exported (provider and proxy variables too),
   not just the hand-kept names. It also closes pydantic-settings' own `env_file` reader, so a
   cwd-relative `.env` is never ambient for the rest of the session; an explicit path passed to
   `load_env_file()` still works. A test that spawns pytest is opt-in
   (`ORCH_ENV_SPAWN_TESTS=1`), always passes explicit node ids, and never runs a nested full
   suite, so a gated run pays nothing for it. Since T-16 only the **expensive** environment
   scenarios are opt-in: the `@pytest.mark.parity` items in
   `tests/integration/test_env_hermeticity.py` are skipped unless `ORCH_ENV_PARITY_TESTS=1`
   (the nested-suite cases still need `ORCH_ENV_SPAWN_TESTS=1`), while the cheap hermeticity
   checks stay in the gated suite. The gate also fingerprints the operator repository's
   worktree registry (the listing plus the registry and git-dir mtimes) before and after the
   session, so a **transient** `git worktree add` + `prune` is caught, not only a registration
   that survives; an unreadable registry fails the guard instead of silently skipping it. A
   test designed to protect gate trust therefore cannot perturb the gate.
2. **The gate record says which world it was.** `verification.json`'s existing `tests` check
   evidence ends with `environment: cwd=…; local .env absent (tracked-files-only worktree);
   node_modules absent; .venv absent` (or `present` for whichever exists). The
   `(tracked-files-only worktree)` label is only added when the cwd really is a linked worktree
   (`.git` is a file); an ordinary checkout without a `.env` says just `absent`. The check-record
   shape and the `runs/<run_id>/` layout are frozen, so this rides in the existing `evidence`
   string — no new field and no new artifact.
3. **There is an opt-in developer path, never a gate command.** To ask "does my local `.env`
   change the result?", run `make test-parity` (or `just test-parity`), or
   `orchestrator project doctor <name> --env-parity`. **The two paths are not the same tree:**
   `test-parity` creates a throwaway detached worktree of the **current HEAD** and runs it via
   `scripts/orch_verify.sh`; `doctor --env-parity` snapshots the project's configured **base
   branch** and runs the configured **test** command there, so a branch's own wrapper is not in
   that snapshot (the same caveat as `--run-commands`). Neither path copies the real `.env`:
   both read only the variable **names** and write a **synthetic** `.env` with placeholder
   values (mode `0600`, never world-readable), so no credential value is ever written into a
   harness-managed directory; contents are never printed or committed. The synthetic file is
   removed on every normal exit path. A SIGKILL can still leave the synthetic tree behind, which
   is residue but not a secret: `orchestrator cleanup`, and the start of the next
   `doctor --env-parity`, reclaim orphaned `env-parity` worktrees and
   `<tmpdir>/orch-env-parity.*` roots (each root records its checkout in `.repo`, so another
   repository's run is not this project's failure). The sweep takes each destination's lock first,
   so a tree a live parity run is still using is reported and skipped, never deleted. The doctor
   path locks `<ORCHESTRATOR_HOME>/locks/<project_slug>-env-parity.lock`, outside the frozen
   `<project_slug>/<task_id>` layout and outside `.managed/` (whose markers keep exactly their old
   `<path>\n(detached)\n` meaning and are never used as locks). That lock records the holder's pid;
   a stale lock is broken only when `os.kill(pid, 0)` says the holder is gone, and by atomic
   rename-then-unlink rather than unlinking a path someone may still hold open. The shell path
   locks `<tmpdir>/orch-env-parity.*/.lock`. A shell root without its `.repo` marker is judged a
   starting run and skipped while it is younger than a 60-second grace window; an older unmarked
   root is a pre-T-20 orphan and is reclaimed. A root that
   is a symlink, owned by another user, or group/world-writable is refused rather than followed,
   and `git worktree remove` runs only for a linked worktree git itself lists. Every removal is
   re-checked afterwards, so a tree or registration that survived is reported as not reclaimed.
   Recovery commits and worktree removal also treat untracked parity files as harness residue
   (removed before staging) rather than worker dirt.
   `--env-parity` is off by default and CLI-only; the API/GUI doctor route keeps its static
   shape. Neither is wired into `make test`, `make verify` or any project `test_command` — a gate
   must stay env-free or its green stops meaning what it says. A parity green also requires the
   command to emit the `ORCH_VERIFY|` marker, so it cannot come from importing the main checkout
   unseen. The offline parity proof runs the
   affected env/config/settings/sync subset twice (once with a synthetic `.env`, once without) in
   a throwaway clone, behind `ORCH_ENV_SPAWN_TESTS=1`; it is not part of a gated run.

The same tracked-only rule is why the TypeScript check needs the scratch-directory recipe
(symlink the main checkout's `node_modules` and the worktree's web sources, run the local `tsc`):
a worktree has no `web/node_modules`, and it also has no `.git`-ignored local config of any kind.

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

**Redaction visibility (T-16).** The artifact layout is unchanged, but the payload's truncation
manifest now separates three outcomes that used to look the same: `Withheld as a secret` (a
sensitive file's content was removed, path shown), `Cut by a token budget`, and `Rewritten in
place by the redactor`. Every in-place rewrite records the pattern class that matched and the
number of substitutions, in the section or file affected. It never records the matched text or
the substituted value. `redact_with_report()` and `redact()` share one implementation
(`utils/redaction.py`), so a rewrite cannot happen without its row. The manifest's
compensation-artifact shortfall is computed from the **final text actually sent** (after
`enforce_budget`), not only from the per-file rows taken before that step, so a file lost to the
aggregate context budget appears in the archived manifest as well as in the harness finding.
Redaction is never reported as a budget shortfall: a compensation file rewritten in place still
counts as delivered. A redaction row is recorded while its section is built, before
`enforce_budget`; if the aggregate context budget then shrinks that section, the row's `note`
column says the section was reduced afterwards and the described text may not have reached the
reviewer, rather than reading as proof the reviewer saw the rewritten text.

The handoff channel is a token budget too (`ORCHESTRATOR_MAX_LOG_TOKENS`, default 8000). It was
raised from 4000 because a normal T-20 handoff was middle-truncated in every round while the diff
budget was omitting the docs and evidence the handoff was carrying; it remains a hard cap, and any
handoff cut is named in the manifest (`handoff block`, bytes available/included).

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
  marker, then the rest are named as omitted. In the source group one file may consume at most
  half the group's budget so a single generated artifact cannot starve the rest, and the whole
  thing stays inside `max_diff_tokens`: the budget is never widened.
- **Two labelled absence sections.** Harness-detected missing artifacts (computed from the run
  directory) and reviewer-asserted missing artifacts (the model's own claims) are separate in the
  payload. The mechanical checks table marks every concrete file/test/line claim `confirmed`,
  `refuted` or `unchecked`, and says what it was checked against.
- **Resolve before judging (T-22 DoD 1/2).** Every path claim is first resolved against a
  `PathUniverse` built from the four sources that can name a path: the supplied patch, the git
  diffstat, the truncation manifest, and `git ls-tree -r HEAD` at the reviewed commit. The
  worktree filesystem is deliberately **not** a source: an untracked or gitignored file is not in
  the commit the reviewer saw, so counting it let a file missing from the commit be labelled a
  review-input omission instead of an evidence gap (round-2 finding 3). `git ls-tree` with no
  resolvable `HEAD` SHA is recorded as unavailable and contributes no paths (round-4 finding 4):
  a repository view with no commit can never support a `confirmed` verdict. A bare name is matched by
  basename against the whole universe *before* any exact root match is accepted, so `README.md`
  with both a root file and `verify-evidence/T-19/README.md` is ambiguous rather than silently
  resolved to the root one (round-2 finding 1). Exactly one match is judged at that path; several
  matches are `unchecked` with **every** candidate listed — the resolver never picks one; no match
  and the claim was only a name is `unchecked` with the reason `no path could be resolved from the
  claim`. A directory-qualified claim that misses is not automatically conclusive either
  (round-4 finding 1): a partial path such as `T-19/capture.sh` is searched by basename and path
  suffix too, and **any** candidate — `verify-evidence/T-19/capture.sh` — makes the verdict
  `unchecked` with every candidate listed, instead of a confirmed absence for a path shape one
  step away from run 38's. Only a directory-qualified miss whose name matches nothing at all is a
  located path the four sources can be asked to deny. `confirmed` is reachable only for a claim
  that was located **and whose path every one
  of the four sources denies**, with `git ls-tree HEAD` actually searched at the reviewed commit:
  if a source was unavailable, or another source lists the path (a deletion that survives only in
  the diffstat), the verdict is `unchecked` and names the blocker — a confident label is never
  manufactured from a partial search. A path the repository contains is never confirmed absent,
  even when the diff budget withheld it; a failed lookup cannot reach `confirmed` at all. That is
  the invariant the tally prints.
- **Three states, not one label (T-22 DoD 3).** `confirmed` alone conflated a review-input failure
  with a Worker evidence gap, so each verdict also carries a state in its detail, the table's
  `state` column and the review notes: `omitted-from-review-input` (present in the repository, but
  the archived manifest says the diff budget did not carry it — the budget's problem, owned by
  T-16, and not a reason to send work back to the Worker), `absent-at-reviewed-commit` (nothing to
  supply, because nothing is in the repository at that commit — a real evidence gap),
  `unresolvable-claim` (the checker cannot judge it), plus `undecided`, `present-in-repository` and
  `supplied-in-full` for the other refuted/unchecked reasons. The statuses stay inside the frozen
  `confirmed | refuted | unchecked` vocabulary: `omitted-from-review-input` is `unchecked` (the
  repository contains the file, so absence cannot be confirmed), `absent-at-reviewed-commit` is
  `confirmed`, and a failed lookup is `unchecked`. The states are extra wording inside the existing
  notes/detail fields, which is the only route a label has without a contract-change request.
- **The tally audits itself (T-22 DoD 4).** `review.json`'s summary line and the payload's
  `Tally:` line break `confirmed` down by basis — `by path-resolution`, `by explicit-named-path`,
  `by manifest-record`, `from a failed lookup` — so a human skimming `review.md` sees at a glance
  whether a confident batch rests on resolved evidence or on names the checker could not place. The
  last number is 0 by construction; if it ever is not, the resolver is broken. `review_payload.md`
  also states the universe every verdict was resolved against (sources and their counts, any
  unavailable source and why, the commit SHA) and gives each row its own `how it was resolved` and
  `where it was found` columns. A source that was consulted but contributed no entries (an empty
  patch, a diffstat with no rows) is still listed as searched, so "searched" means consulted, not
  "happened to name this path".
- **Existence is not visibility.** A file that exists on disk does not refute "its hunks were not
  supplied". Visibility claims are compared with the supplied patch and the truncation manifest;
  `refuted` means the evidence was in fact supplied in full, `omitted-from-review-input` means the
  repository contains it but the budget withheld it (`unchecked`, because DoD 2 reserves
  `confirmed` for absence), `absent-at-reviewed-commit` is `confirmed` because there was nothing to
  supply, and `unchecked` is used whenever neither can decide it (fail closed). A `refuted` claim
  removes the finding and is logged (`review_absence_refuted`), but a verdict is never upgraded on
  it: the round goes to `NEEDS_HUMAN`, not to `APPROVED`.
- **Diff completeness (T-16 DoD 3).** Before the review ships, the harness compares the git
  diffstat with the **rendered, budgeted diff text the prompt builder actually emitted** — not
  the archived `patch.diff`, which is the full record but a file the reviewer never saw. Every
  changed file must have a hunk body in that rendered text; a file the budget omitted is a
  harness finding. A sensitive file's explicit `# [sensitive file omitted]` marker is labelled
  `withheld`, never reported as a missing hunk.
- **Compensation artifact budget (T-16 DoD 7/8).** Files that make a claim checkable —
  `verify-evidence/**`, `contract-change-request-*.md`, committed handoff copies — get a
  separate reserved slice of `max_diff_tokens` (`COMPENSATION_BUDGET_FRACTION`, currently one
  third of the post-header budget). Source files may not spend it; leftovers from source are
  added to it. Evidence is allocated **fairly, not greedily**: every file first receives at most
  `budget // file_count`, and only after every file has had that share is any leftover handed
  back to files that need more, so one huge log cannot tail-drop a later `zz_review_index.txt`.
  If the bundle still cannot fit that fair share, the manifest prints a
  **Compensation-artifact shortfall** computed against the final as-sent diff, the review
  adds a harness-detected finding (`review_evidence_undelivered`), and the prompt tells the
  reviewer that the missing evidence is a delivery failure rather than a Worker evidence gap.
  That finding never changes the reviewer's verdict and is filtered out of the Worker feedback,
  so no round is spent trimming evidence to satisfy the harness's own budget. `max_diff_tokens`
  is not widened by any of this.
- **Git capability floor (T-16 DoD 4).** `git check-attr --source=<merge-base>` requires Git
  **2.40** or newer. On an older Git the audit falls back to plain `git check-attr -a`: it still
  reports every `-diff` / `export-ignore` attribute, but pre-existing status cannot be attributed
  to the merge-base, so the finding fails closed and says so instead of crashing or silently
  skipping the check. Capability is probed by actually running `check-attr --source`, not only by
  reading the version, so a build that advertises 2.40 but rejects the option is caught; if the
  probe passes and the real `--source` query fails at runtime, the audit degrades to the plain
  query with pre-existing status unknown. The fallback reason names its actual cause: an old Git
  version, an unestablished version, a probe failure, a runtime failure, or **no merge-base at
  all** (which is not a Git version limitation and is never reported as `git < 2.40`). A hiding
  attribute this task introduced is always a harness finding (and an additive
  `review_diff_incomplete` build-log event); one that is proven to predate the merge-base is
  informational. A Worker cannot opt out of review by configuring `.gitattributes`.

Measured motivation: reviews.7 (T-8 round 5) asserted three required proofs were missing; all
three existed on the branch, and the reviewer had also received a handoff block cut off at DoD 5.
The void finding requeued a 45-minute paid round (run 15).

The opposite failure is measured too, and is why a machine verdict is only as trustworthy as its
resolver: reviews.28 (run 38, T-19) reported `15 confirmed, 1 refuted, 2 unchecked`, and the
confirmed rows read

    absence check [confirmed] capture.sh: path is absent from the task worktree, the supplied patch
    and the truncation manifest

while `ls verify-evidence/T-19/` showed all seven files tracked at the reviewed commit. The claims
had arrived as bare filenames inside one brace-expanded `missing_artifacts` item, and the checker
had looked for the literal name at the repository root. T-22's regression tests replay that exact
fixture as data (`tests/fixtures/absence_run38.py`): the same input now yields `0 confirmed, 1
refuted, 17 unchecked` with `from a failed lookup: 0`. Eleven of the located files are
`omitted-from-review-input` (the budget dropped them, so absence is `unchecked`, never
`confirmed`); `README.md` is `unresolvable-claim` because both the root file and
`verify-evidence/T-19/README.md` carry that name, and the row lists both candidates; the real
absence claims (`handoff.md`, a test name) are `unchecked` because the lookup found nothing to
place. The point of T-11 was never to make harness claims unquestionable — a `confirmed` from a
check that could not find the path is worse than no check, because the operator stops auditing.
The operator-facing rule: **a machine verdict is only as trustworthy as its resolver.** When a
run's absence tally is not credible, read the `state` and `how it was resolved` columns before
believing any `confirmed`, and treat a reviewer note that says "the harness confirmed it" as
unverified until the row's resolution evidence backs it up.

## Verification artifacts

`runs/<run_id>/verification.md` + `verification.json`, plus `test.log`,
`test_summary.txt`, and `lint.log`/`build.log` when configured. When a check contradicts
the worker's story, the check wins.

### Evidence freshness

Committed evidence is stamped, verified and regenerated by one command. Every file under
`verify-evidence/<task>/` that is meant to be read as evidence carries the HEAD it was measured
at, and the deterministic Supervisor check **committed evidence freshness** compares that stamp
with the run's `commit_sha`. It is a pure equality test, reported next to "claimed files exist on
disk", so no reviewer has to discover staleness by reading an old copy of the Worker's own tests.

The row's *status* uses the check vocabulary in `docs/frozen-contracts.md`: a fresh bundle is
`pass`, absent or unstamped evidence is `not_applicable`, and a compared-and-out-of-date one is
`stale` — the fifth, additive value approved as CCR-T15-01 (operator, 2026-11-08; see
`docs/contract-change-request-T15-01-evidence-freshness-status.md`). The row's `evidence` string
names the stale count, the paths and the commit it compared, the paths and reasons are in `notes`,
and the `evidence_freshness_stale` build-log payload carries `state=stale`. Staleness never sets
`contradicts_worker` and never fails the gate unless the operator opts in with
`ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT=true`, which turns it into `fail`. The check reports; it
does not force a re-run. Scope is the two code-writing kinds (`coding`, `maintenance`) through one
predicate used by both the Supervisor verification pass and the Architect review, so a freshness
finding in `review.json` always has a matching verification row.

The parser accepts the repository's legacy stamp shapes (`# HEAD=<sha>`, `# HEAD <sha>`,
`HEAD: <sha>`, prose in a comment, the pipe-delimited T-20 form, short SHAs, `== commit under test:
... ==`), because nine of them exist. A short SHA is prefix-resolved against the commit under
verification. Only the first 20 lines are scanned: a stamp is header metadata, so a captured
transcript that mentions a stamp is neither counted nor rewritten. A short unparsable `HEAD=`
stamp is stale, never fresh.

HEAD moves for reasons the Worker does not control, so a bare SHA equality is not the rule. The
comparison target is the **nearest ancestor of the commit under verification that is not
evidence-only and not empty**. An evidence-only commit touches nothing outside `verify-evidence/`
(and no declared generator — every path `manifest.json` lists as `generator`/`inputs`, not one
hardcoded filename) and an empty commit changes nothing at all, so both are walked past: a bundle
delivered by an evidence-only commit is honestly stamped at the code HEAD it measured, which is
what lets the bundle report `pass` on the commit that ships it. Any commit that changes code after
the bundle becomes the target and makes it `stale` — including a recovery commit that swept
uncommitted source. A commit carrying an `Orchestrator-Recovery:` or `Operator-Hand-Fix:`
**trailer** is *explained, not certified*: git's `%(trailers)` parser identifies it (never a
substring search of the body), the reason names the commit, its trailer and its author, and the
bundle still reads stale because a Worker can write any trailer into its own code commit. The
reason string always names the comparison target it used. Evidence committed earlier on the branch
and gone at the reviewed commit is stale with the paths and the deleting commit, never
`not_applicable`: deleting evidence must not be rewarded with a silent pass, and it is distinct
from a task that never committed evidence.

One command refreshes the whole bundle:

```bash
orchestrator task evidence T-15            # regenerate: runs capture.sh, writes the manifest
orchestrator task evidence T-15 --check    # read-only: exit 1 when stale
```

It runs the committed `verify-evidence/<task>/capture.sh` and nothing else — there is no
arbitrary-command mode and no stamp-only mode, because re-stamping content that was not regenerated
is the failure the check exists to catch, and a command the branch does not contain cannot be
reproduced from it. A subset refresh is `--only <name>`: the name must be an output the *committed*
manifest declares, it re-runs that generator step, and an unknown name is refused. It refuses a
worktree with **any** uncommitted input — tracked modifications *and* untracked source or test
files, because `capture.sh` measures the tree on disk while the stamp names a commit — **including
an edited `capture.sh`**. It also refuses a generator that is not committed at `HEAD`. It
canonicalises **only the files that pass rewrote**, and writes `manifest.json` (HEAD, timestamp,
command line, mode, `--only` selection, tool versions, the generator/inputs it declares, and the
files it produced — outputs only, so `--only capture.sh` is refused by name). A file the pass did
not rewrite keeps its old stamp and is reported stale. The pass stamps the code HEAD it certifies:
when the worktree sits on an evidence-only commit, that is the nearest non-evidence-only ancestor,
so refreshing a bundle never writes a stamp the walk-back cannot certify.

The default pass is cheap by design: it regenerates everything except the expensive full-suite
capture, so it finishes in seconds and reports its elapsed time and rewrite count. `--full` sets
`T15_FULL=1` for the generator and runs the suite too; the mode is recorded in `manifest.json` and
in every canonical stamp (`# HEAD=<sha> mode=fast|full`), so a reader can tell a cheap refresh from
a complete one. `timing.txt` holds **both** modes' wall time, file count and rewrite count, and each
row carries the HEAD its own pass measured — written only by a `--full` pass so the fast pass stays
byte-identical on a re-run — and any number quoted in a bundle README or a handoff must come from
that file, never from a scratch or `/tmp` log. It needs no network or API key, and a second fast run
at the same HEAD is byte-identical so the diff stays reviewable.

A file cannot contain the SHA of the commit that adds it. The bundle's own `freshness_selfcheck.txt`
is therefore the worktree-basis check of the pass that wrote it (generated LAST, naming the commit
it compared), and `orchestrator task evidence <id> --at-tip`, run **last**, after the commit that
delivers the bundle, writes `freshness_at_tip.txt`: the committed-view check, printing both the
delivered tip it ran at and the comparison target it resolved to, and refusing to write at all
unless the bundle is fresh. That report is unstamped — it is a statement about a commit, not a
measurement taken at one — and the commit that adds it touches only `verify-evidence/`, so it is
walked past and the gate reports `pass` naming the code HEAD the stamps were measured at.

## Build log

Every event is appended to `ORCHESTRATOR_HOME/build-log.jsonl` and mirrored to the
`build_log_entries` table. Each line looks like:
`{timestamp, phase, task_id, run_id, role, event, details, commit_sha}`. Events include
`task_status`, `run_created`, `worktree_created`, `recovery_commit`, `run_failed`,
`run_timeout`, **`run_completed`**, **`run_cancelled`**, `supervisor_verification`, `architect_review`,
`nits_deferred`, `merge_candidate`, `merged`, `merge_refused`, `schedule_fired`,
`standalone_run`, `escalated`, `human_accept`, `recovery_notice_included`,
`retry_fields_reset`, `contract_change_detected`, `handoff_salvaged`,
`handoff_salvage_failed`, **`task_limits_exhausted`**, **`review_absence_refuted`**,
**`review_diff_incomplete`**, **`evidence_freshness_stale`**, `run_error`. The GUI Build Log page and
`GET /api/build-log` read it.

### Terminal run events

A run's terminal status is `completed | failed | timeout | cancelled`. Two of the four had a
named event (`run_failed`, `run_timeout`); `cancelled` showed up only as a `task_status` line
reading `run cancelled`, and a run that finished normally had none — there was no
`run_cancelled` event name either. T-8 added `run_completed` and T-9 added `run_cancelled`
(requested in `contract-change-request-T9-01-notifications.md`), so all four have one now.
(`run_error` exists but is not a terminal *status*: it labels
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
line. The shared chokepoint is the **status transition** (`_set_run` is the only writer), not the
`log_event` call site. T-9 emits `run_cancelled` from the cancel branch of `_execute`, after that
status commits and before handoff salvage. It is **additive** — no existing event name and no
line-shape key changed. The contract wording for all four is *requested* in
`contract-change-request-T9-01-notifications.md` and applied by the operator at the merge gate;
this branch does not edit `docs/frozen-contracts.md` (a task DoD names files, it does not grant
edits).

Today no in-repo consumer infers run success from a task-status transition (the Build Log page
renders events as they arrive; the Dashboard counts `task.status`, which is a task verdict, not
a run outcome). The T-9 fan-out keys its triggers off these events rather than inferring
success: `run_completed`, `run_failed`, `run_timeout`, `run_cancelled`, `escalated`,
`merge_candidate`, `merged`, `merge_refused`, and the exact `task_status` transitions `-> review`
and `-> blocked` (see `docs/configuration.md`).

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
