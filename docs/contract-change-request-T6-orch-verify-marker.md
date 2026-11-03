# Contract-change request CCR-T6-01 — freeze the `ORCH_VERIFY|` resolution marker

- **Status:** APPROVED by the operator, 2026-11-04. Applied verbatim to `docs/frozen-contracts.md`
  ("Path-independent verification commands"). This file is kept as the request of record.
- **Raised by:** T-6 (`task-6-verification-commands-cannot-run-in-a-worktree-n`), 2026-11-03.
- **Reason it is a request and not a change:** T-6's Definition of Done does **not** authorize
  a change to `docs/frozen-contracts.md`, so the branch does not edit that file. An earlier
  revision of the branch added a bullet there; the reviewer correctly flagged it as an
  unauthorized contract edit, and it was reverted in this revision. The convention below is
  therefore documented only as a repo-internal pattern (`build-loop.md`, `configuration.md`).

## What the code already depends on

`project_service.run_in_worktree_checks()` (reached by `orchestrator project doctor
<name> --run-commands`) decides "did this command verify the right checkout?" by reading one
machine-readable line that a path-independent wrapper prints:

```
ORCH_VERIFY|mode=<mode>|module=<name>|module_file=<abs>|worktree_root=<abs>
```

- emitted on stdout **or** stderr (the gate captures both into `test.log`),
- keys are order-independent, `|`-delimited so paths with spaces survive,
- absent marker ⇒ the doctor check **fails** ("an unproven resolution is not a proven local
  one"), so the current convention is fail-closed without being frozen anywhere.

`scripts/orch_verify.sh` is the only producer today; `project_service.parse_orch_verify`
is the only consumer.

## Proposed contract text (for the Architect to place in `frozen-contracts.md` if approved)

> **Path-independent verification commands**: the gate runs a project's test/lint/build
> command with cwd = the task worktree (`worktrees/<project_slug>/<task_id>`), never in the
> main checkout. A wrapper that proves where its imports resolved emits
> `ORCH_VERIFY|mode=<mode>|module=<name>|module_file=<abs>|worktree_root=<abs>` on stdout or
> stderr; `scripts/orch_verify.sh` is the reference implementation and
> `project_service.parse_orch_verify` (+ `project doctor --run-commands`) consumes it. Keys
> are order-independent; the field names and the `ORCH_VERIFY|` prefix are the contract.

## Safest options

1. **Approve as written** (add the bullet with an authorizing DoD item on a planning task).
   Zero code change; the field names are already what the code parses.
2. **Leave it unfrozen** as a repo-internal convention documented in `build-loop.md`
   (this is the state of this branch). Cost: a later phase may rename the fields without
   tripping any contract check.
3. **Drop the marker entirely** and have doctor exec a separate
   `python -c "import <pkg>; print(<pkg>.__file__)"` probe. Cost: it proves a *different*
   resolution than the one the tool run used (that exact gap is the reviewer's nit on this
   task), so this option is not recommended.

## Blocking?

No. T-6 completes under option 2. This artifact exists so the decision is recorded rather
than silently made by the Worker.
