# Contract-change request CCR-T8-02 — the added `recovery_commits` field on the task detail response

- **Status:** PROPOSED — awaiting the Architect/human decision. **Not applied.**
  `docs/frozen-contracts.md` is untouched on this branch, and this request does not edit it.
- **Raised by:** T-8 (`task-8-recovery-commits-after-a-timeout-commit-half-fin`), 2026-11-04,
  after the round-5 review (finding 4). The reviewer is correct against the frozen text:
  `docs/frozen-contracts.md` l.32 freezes **"REST API shapes under `/api`: routes in
  `src/orchestrator/api/routes/`"**, and T-8 added a field to one of those shapes.
- **Scope of this request:** ONE additive field on an existing response. The new route
  `GET /api/tasks/{id}/recovery` that also landed in round 2 was **dropped** in round 5 (reviewer
  finding 3: dead code, nothing read it, no DoD item asked for an endpoint), so there is no new
  route to declare. That removal is why this CCR covers one field and not two.

## What landed

`GET /api/tasks/{task_id}` (`src/orchestrator/api/routes/tasks.py`, `_detail`) now returns one
additional key:

```json
"recovery_commits": [
  {
    "sha": "<full sha>", "short": "<7 chars>", "subject": "chore(orchestrator-recovery): …",
    "recovery": true, "recovery_run": "R-10",
    "author": {"name": "coding-orchestrator", "email": "orchestrator@localhost"},
    "files": [{"path": "app.py", "status": "M"}, {"path": "RESERIAL_DONE.txt", "status": "A"}]
  }
]
```

- Additive only: no existing key changed name, type or meaning; no other endpoint changed; no
  request body changed; no status code changed.
- Read-only derivation: the value is computed from git history at request time
  (`project_service.recovery_commits_for` → `git_service.recovery_commits`), never stored, so it
  cannot drift from the branch and there is nothing to migrate.
- Consumers today: `web/src/api/types.ts` (`TaskDetail.recovery_commits?`, optional) and
  `web/src/pages/TaskDetail.tsx` (`RecoveryNote` above the diff, header badge). Because the field
  is optional in the client type, an older API response still renders.
- Pinned by `tests/integration/test_recovery_after_timeout.py::test_recovery_reaches_the_reviewer_and_the_gui_payload`.

## Proposed contract text (for the Architect to place in `frozen-contracts.md` if approved)

> **Task detail response**: `GET /api/tasks/{task_id}` returns the task row plus read-only derived
> collections. `recovery_commits` is an additive array of harness-created commits on the task's
> branch, each entry `{sha, short, subject, recovery, recovery_run, author, files[]}`. The field
> is always present (empty when there are none) and is derived from git history, never stored.
> Adding a derived field to an existing `/api` response is additive and allowed; changing or
> removing an existing key, or adding a new route, requires its own contract-change request.

## Safest options

1. **Approve as written.** Zero code change — the field already has this shape and this is what
   the GUI reads. Freezing it means a later phase cannot silently repurpose `recovery_commits`.
2. **Leave it unfrozen** (this branch's state, the T-6 precedent for the `ORCH_VERIFY` marker).
   The code and GUI work; the shape lives in `docs/build-loop.md` only. Cost: the REST-shape
   contract says any change to `/api` responses needs a request, so an unfrozen field is a
   contract the harness is already violating in letter while honoring in spirit.
3. **Approve option 1 and also freeze the `files[].status` vocabulary** (`M`/`A`/`D`/`R`, straight
   from `git diff-index`). Cheapest addition to the same paragraph; it is the only part a consumer
   might branch on.

## Blocking?

No. Nothing in T-8's DoD depends on this being frozen before merge; DoD 4 is satisfied by the
field plus the reviewer-payload section. This artifact exists so that the contract-bearing surface
is declared to a human instead of accepted because a Worker put it in `contracts_touched`.
