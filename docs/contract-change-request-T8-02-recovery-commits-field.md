# Contract-change request CCR-T8-02 — the added `recovery_commits` field on the task detail response

- **Status:** PROPOSED — awaiting the Architect/human decision. **Not applied.**
  `docs/frozen-contracts.md` is untouched on this branch, and this request does not edit it.
- **Raised by:** T-8 (`task-8-recovery-commits-after-a-timeout-commit-half-fin`), 2026-11-04,
  (round 6 corrected `author` to the string the server actually emits — see "Shape, verified
  against the code" below; the first draft of this file described it as an object, which was
  wrong and would have frozen a shape the server does not produce.)
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
    "author": "coding-orchestrator <orchestrator@localhost>",
    "files": [{"status": "M", "path": "app.py"}, {"status": "A", "path": "RESERIAL_DONE.txt"}]
  }
]
```

### Shape, verified against the code (round 6)

Every key below is read off the shipped types, not off the prose:

| key | type on the wire | source |
|---|---|---|
| `sha`, `short`, `subject` | `str` | `git_service.CommitInfo` (`src/orchestrator/services/git_service.py:111-120`), `as_dict` `:122-131` |
| `author` | **`str`**, formatted `"<name> <email>"` | `CommitInfo.author: str` l.117; produced by the `--format` spec `_LOG_FIELDS = "%x1e%H%x1f%h%x1f%s%x1f%an <%ae>"` l.380 |
| `recovery` | `bool` | `CommitInfo.recovery` l.118, set from the `Orchestrator-Recovery:` trailer match |
| `recovery_run` | `str \| None` | `CommitInfo.recovery_run` l.119 (`None` for non-recovery commits; the literal `"?"` when the trailer is present but empty) |
| `files` | `[{"status": str, "path": str}]` | `CommitInfo.files` l.120, built from `git log --name-status` (status first, because that is the tuple order the code writes) |
| client mirror | `author: string`, `recovery: boolean`, `recovery_run: string \| null`, `files: { status: string; path: string }[]` | `web/src/api/types.ts:85-93` (`RecoveryCommit`) |

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
> branch, each entry `{sha: str, short: str, subject: str, recovery: bool, recovery_run: str|null,
> author: str ("name <email>"), files: [{status: str, path: str}]}`. The field
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
   from `git log --name-status`). Cheapest addition to the same paragraph; it is the only part a
   consumer might branch on.
4. **Split `author` into `{name, email}`** — the shape this file wrongly described in its first
   draft. Not recommended for this phase: it is a breaking change to an emitted field (the client
   type at `web/src/api/types.ts:89` reads a string), and nothing consumes the email separately.

## Blocking?

No. Nothing in T-8's DoD depends on this being frozen before merge; DoD 4 is satisfied by the
field plus the reviewer-payload section. This artifact exists so that the contract-bearing surface
is declared to a human instead of accepted because a Worker put it in `contracts_touched`.
