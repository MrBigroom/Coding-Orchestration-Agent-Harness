# Contract-change request CCR-T9-01 — notification records, `GET /api/notifications`, `run_cancelled`

- **Status:** REQUESTED by T-9, 2026-11-04. T-9's Definition of Done explicitly authorizes both
  changes: DoD 3 requires "a persistent list that the GUI and an endpoint can read", and DoD 8
  requires the missing fourth terminal event `run_cancelled`. This file is the request of record;
  the operator confirms it at the merge gate.
- **Why a request:** `docs/frozen-contracts.md` freezes the SQLite/SQLModel schema, REST API shapes
  under `/api` (a new route needs its own request), and the build-log event vocabulary, which
  recorded that `run_cancelled` did not exist and was deferred to T-9.

## What landed

1. **Additive table via a new Alembic revision.** `notifications` is created by
   `migrations/versions/0010_notifications.py` (`down_revision = 0009_chat_continue`) and mirrored
   by `core/models.py::Notification`. No existing table or column changed;
   `tests/integration/test_migrations.py` pins model/migration parity and round-trips the revision.
2. **Additive read-only route.** `GET /api/notifications` (`api/routes/notifications.py`) returns
   those rows newest first, with optional `limit`, `status` and `task_id` filters. Errors keep the
   frozen `{"error", "detail"}` shape; no existing route or response key changed.
3. **Additive terminal event name.** `run_service._execute`'s cancel branch emits `run_cancelled`
   once, after `RunStatus.cancelled` commits and before handoff salvage, with
   `details="<run short id> cancelled (exit <code>)"` and `role="worker"`. The line shape is
   unchanged; no event was renamed or removed.

## Proposed contract text (already applied on this branch)

> **Notification records.** `notifications` is an additive SQLModel table created by revision
> `0010_notifications`; `GET /api/notifications` reads it newest first. `status` is one of
> `pending | delivered | failed | skipped | coalesced`, with `skipped` ("not attempted because the
> channel was off") distinct from `failed` ("attempted and failed"). No existing table, route,
> response key or error shape changed.
>
> **Build-log events.** All four terminal statuses now have a named event:
> `run_completed`, `run_failed`, `run_timeout`, `run_cancelled` (added by T-9). Event names remain
> additive; the line shape `{timestamp, phase, task_id, run_id, role, event, details, commit_sha}`
> is unchanged.

## Safest options

1. **Approve as written** (the branch's state). Zero code change; the table, route and event are
   what the code already ships and what the GUI reads.
2. **Approve the event but drop the endpoint** — not possible: the browser GUI has no database
   access, so the route is how the persistent list is read.
3. **Leave both unfrozen** — the code works, but a later phase could change the record shape
   silently, and the persistent list is what makes a missed desktop notification recoverable.

## Blocking?

No. DoD 3 and DoD 8 authorize the work; this artifact records the frozen-contract surface so it is
a decision rather than a silent edit.