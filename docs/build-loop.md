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
   recovery is logged.
3. **Build the worker prompt** in cache order (see `prompt-caching.md`), including the
   latest reviewer feedback on retries, and save it as `runs/<id>/prompt.md`.
4. **running.** The adapter command runs inside the worktree with a minimal environment.
   stdout/stderr are redacted and streamed to `runs/<id>/stdout.log|stderr.log` and the
   WebSocket.
5. After exit: leftover changes get a recovery commit, and `patch.diff` + `diffstat.txt`
   are collected against the base branch.
6. **Failure paths.** A non-zero exit → run `failed`. Exceeding the adapter timeout →
   `timeout`. A `CANCEL` marker → `cancelled`. In all three cases the task returns to
   `planned` for a deliberate retry.
7. **Handoff.** `.orchestrator/handoff.json` in the worktree (git-excluded) is
   validated and copied to `runs/<id>/handoff.json`, with `handoff.md` rendered next to
   it. If the worker produced none, the orchestrator writes a **synthesized** handoff
   marked as such, and that counts as a missing artifact at review.
8. `memory_suggestions` are stored as **pending** memory entries.
9. **BLOCKED handoff** → task `blocked` (waiting for a decision, not "still working").
   The run completes and nothing is retried automatically.
10. Otherwise **testing**: the task moves to `review` and the Supervisor gate runs.

## Supervisor gate (`supervisor_service.after_run`)

1. **Live verification**, recorded in `verification.json` and `verification.md`:
   - handoff present
   - claimed files exist on disk and match the diff
   - project test command (re-run independently and written to `test.log`; a worker claim
     of green tests that the run contradicts sets `contradicts_worker`)
   - lint and build commands when configured
   - migrations, live endpoints, and LLM coherence recorded as N/A with a reason unless
     in scope
2. **Tests not green or no handoff** → no Architect review. The task goes back to the
   Worker on the same branch.
3. **Architect review** (coding/maintenance/review tasks, or any high-risk task). It sees
   only artifacts: the diff, handoff, test summary, verification narrative, and a list of
   missing artifacts.
   - `NEEDS_HUMAN` → `blocked`
   - `CHANGES_REQUESTED` → back to the Worker on the same branch
   - after `max_review_rounds` rounds (default 3) → `blocked`
4. **APPROVED + verification pass** → `accepted`. Nits in an APPROVED review are logged as
   *deferred in writing* (`nits_deferred`). Agent tasks without high risk skip the
   Architect, so cron jobs don't spend Opus tokens.
5. **Merge gate** (below).

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
`run_timeout`, `supervisor_verification`, `architect_review`, `nits_deferred`,
`merge_candidate`, `merged`, `merge_refused`, `schedule_fired`, `standalone_run`, `escalated`,
`human_accept`. The GUI Build Log page and `GET /api/build-log` read it.

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
