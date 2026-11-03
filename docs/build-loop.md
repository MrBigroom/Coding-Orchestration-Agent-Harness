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
   `timeout`. A `CANCEL` marker → `cancelled`. In all three cases the task returns to
   `planned` for a deliberate retry, and the failure paths emit `run_failed` / `run_timeout`
   (a cancel is recorded on the `task_status` line); a run that finishes normally emits
   `run_completed`. Every new round clears the previous round's lifecycle markers (see
   “What a new round resets”).
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
- **Reviewer payload**: `review_service.recovery_section` + `harness_findings_section`, and
  the GUI marks the diff with a warning block (`TaskDetail.tsx` `RecoveryNote`) built from
  `GET /api/tasks/{id}/recovery`. `runs/<id>/review_payload.md` keeps what the reviewer was
  actually shown.
- Sensitive paths (`.env`, keys) are **never** staged into a recovery commit; they stay on
  disk untracked and are reported in `skipped_sensitive`.

A recovery commit is not itself a rejection reason and not itself an approval: it is
information that the content has no self-check behind it. What *does* fail the gate is what
re-verification finds in it (a lint error, a failing test) or a harness-detected contract-file
hit — see “Unauthorized contract edits”.

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
3. minus the files the task's stored `definition_of_done` / `acceptance_criteria` name
   literally (`authorized_contract_files`; the task *description* is not authorization).

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
     authorized only by the task's stored `definition_of_done` / `acceptance_criteria`.
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
   The exact payload is stored as `runs/<id>/review_payload.md`, so "the reviewer was shown
   X" is checkable afterwards instead of arguable.
   - `NEEDS_HUMAN` → `blocked`
   - `CHANGES_REQUESTED` → back to the Worker on the same branch
   - after `max_review_rounds` rounds (default 3) → `blocked`
5. **APPROVED + verification pass** → `accepted`. Nits in an APPROVED review are logged as
   *deferred in writing* (`nits_deferred`). Agent tasks without high risk skip the
   Architect, so cron jobs don't spend Opus tokens.
6. **Merge gate** (below).

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
`retry_fields_reset`, `contract_change_detected`, `run_error`. The GUI Build Log page and
`GET /api/build-log` read it.

### Terminal run events

A run's terminal status is `completed | failed | timeout | cancelled`. Three of them had a
named event (`run_failed`, `run_timeout`, and `run_error` for an unexpected exception); a run
that finished normally had none — `run_cancelled` does not exist as an event name either, a
cancel shows up as a `task_status` line reading `run cancelled`. A green run was therefore
observable only as a `task_status` transition, so any consumer had to *derive* the single most
important trigger indirectly. `run_completed` is that missing event. It is emitted from
`run_service._set_run` — the single writer of `run.status`, which the `run_failed` /
`run_timeout` events are also driven by (their `_set_run` call is immediately followed by
`log_event(f"run_{status}")` in `_execute`) — so every terminal run status is logged from the
same transition point, and it is impossible to set `completed` without emitting it. It is
**additive** — no existing event name and no line-shape key changed — and adding it to the
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

`details` is redacted (`log_service.log_event`), and the credential patterns in
`utils/redaction.py` are anchored with a leading word boundary so operational text stays
usable: a branch, task, phase or path name (`task-8-…`, `risk-…`, `mask-…`) and a
`findings_json` payload containing one must survive byte-identically, while real keys
(`sk-ant-…`, `sk-proj-…`, `gh[pousr]_`, …) still redact wherever they appear. The
`merge_candidate` instruction in particular must remain copy-pasteable —
`git -C <repo> checkout main && git -C <repo> merge --no-ff <branch>`.

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
