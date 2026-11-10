# Contract-change request: memory lifecycle columns (`supersedes`, `stale_after`)

- **Requested by:** task T-17 (remainder of T-13), 2026-11-10
- **Status:** PROPOSED — not approved, not implemented
- **Frozen contract affected:** `memory_entries` table (`src/orchestrator/core/models.py`)
  and its Alembic baseline/migrations. The table shape is frozen; adding a column requires
  this request plus written operator approval.
- **Scope of this task:** the request and the documentation only. T-17 ships **no** Alembic
  revision touching `memory_entries`; `tests/integration/test_migrations.py` stays green.

## Why

Memory currently has no way to say that one fact replaced another, or that a fact should be
retired on a date. The only lifecycle-adjacent mechanism is content-derived and weaker:
`utils/memory_text.rot_flags` detects `absolute_date`, `forward_looking` and `task_shape`
text, and the injection order demotes those rows first — but it never retires a row, and a
carefully worded stale entry can stay injected indefinitely.

Two requested columns:

| Column | Type | Meaning |
|---|---|---|
| `supersedes` | `int NULL`, FK → `memory_entries.id` | This row replaces that approved row. The superseded row is reported, not deleted, and is not injected while the replacement is approved. |
| `stale_after` | `datetime NULL` (UTC) | After this instant the row is reported stale by triage and excluded from injection until re-confirmed (an edit clears or moves the date). |

## Behaviour requested

- Selection (`memory_service.select_for_prompt`) excludes a row whose `stale_after` is in the
  past (evaluated against a passed-in clock, not an ambient one, so a test can pin it) and a
  row named by an approved row's `supersedes`. Both exclusions carry a reason, like the
  existing cap demotions, and appear in triage/budget/GUI.
- `supersedes` is one-way and does not cascade; a cycle is refused at write time.
- Triage reports `superseded-by` and `stale-after` flags. Approving a replacement that would
  retire a row is subject to the same explicit-visibility rule as an eviction (name the row,
  offer override), never a silent disappearance.
- Markdown rendering keeps emitted rows only; a retired row stays in SQLite for audit.

## Migration shape (for the approving change)

- One new revision `0011_memory_lifecycle` adding the two nullable columns to
  `memory_entries`; no data rewrite and no backfill (NULL means the historical behaviour).
- `MemoryCreate`/`MemoryUpdate` gain optional fields; the frozen API error shape and the
  existing `/api/memory/*` routes are otherwise unchanged.
- Existing exports/imports remain valid because both fields default to NULL.

## Not requested / out of scope

- Automatic retirement, automatic approval, or an LLM deciding which row supersedes which.
- Changing the default memory caps.
- Deleting rows. Retirement is a selection fact, not a delete.

## Approval

Written operator approval is required before any of the above is implemented. Until then the
current, weaker content-derived flags remain the only lifecycle mechanism (documented in
`docs/memory.md`).
