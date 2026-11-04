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
   - project test command (re-run independently in the task worktree and written to
     `test.log`; a worker claim of green tests that the run contradicts sets
     `contradicts_worker`). **Skipped entirely when `test_command` is null** — see
     “Verification commands must be path-independent” below
   - lint and build commands when configured (same worktree, same null-means-N/A rule)
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
