# Shared memory

Memory is durable knowledge shared across runs and roles, such as environment facts,
conventions, decisions, and gotchas. It is **not** chat history.

## Storage

SQLite (`memory_entries`) is the index and source of truth. Approved entries are rendered
to markdown for humans:

```
$ORCHESTRATOR_HOME/memory/
  global/{environment,preferences,patterns}.md
  projects/<project-slug>/{conventions,decisions,gotchas}.md
  roles/{architect,worker,supervisor}.md
```

Each markdown line ends with `<!-- mem:ID -->`. Lines you add to these files by hand can
be indexed (as pending entries) with `orchestrator memory import --markdown`.

## Categories and scopes

| Category | Required | Scopes (the whole vocabulary) |
|---|---|---|
| `global` | — | environment, preferences, patterns |
| `project` | `project_slug` | conventions, decisions, gotchas |
| `role` | `role` | architect, worker, supervisor (role names `reviewer` and `agent` resolve to the architect and worker files) |

These are the **only** scopes any injector can read, so they are checked at every
boundary (task T-4): `POST /api/memory`, `PATCH /api/memory/{id}`, `orchestrator memory
add`, `import`, and `memory_service.create/update`. An out-of-vocabulary scope is a
`400`/CLI error that names the allowed values — `MemoryError_` subclasses `ValueError`,
so the API answer keeps the frozen `{"error", "detail"}` shape.

`memory_service.GLOBAL_SCOPES`, `PROJECT_SCOPES`, `ROLE_SCOPES` (the role *files*,
`ROLE_FILES` after applying `ROLE_MEMORY_KEY`), `ROLE_SCOPE_ALIASES` and
`SCOPE_VOCABULARY` are importable constants; before T-4 there was no `ROLE_SCOPES`, so no
check could be written for the role category.

### What happens to a Worker suggestion with a made-up scope

Suggestions are never trusted for their scope, and nothing is stored where no prompt can
reach it:

- `project` → coerced to a deterministic scope (a vocabulary word found inside the
  proposed scope, otherwise `gotchas`).
- `global` → same rule, fallback `environment`.
- `role` → **dropped with a warning** if it resolves to no role memory file; guessing a
  role would file someone's note under the wrong agent.

Every substitution and every drop is returned in `SuggestionOutcome.notes` and written to
the build log as `memory_scope_substituted` / `memory_suggestion_dropped` (and said out
loud in the chat stream), so the operator can see what was rewritten.

## Auditing and repairing scopes

```bash
orchestrator memory list                  # `inj` column: ok or NO, reason printed below
orchestrator memory list --uninjectable   # only the rows no prompt can read
orchestrator memory fix-scopes --dry-run  # preview the moves and the dead files
orchestrator memory fix-scopes            # move them, re-render, collect the orphans
```

`fix-scopes` migrates rows stored before validation existed (entries #5/#22/#24 shipped
with free-form scopes that no injector could read). It **moves rows and never deletes
them**, leaves rejected rows alone, re-renders **both** the old and the new markdown file
so the old one cannot keep a stale `<!-- mem:ID -->` line, then removes rendered files
that no longer own an entry — a hand re-scope used to leave a title-only ghost such as
`memory/projects/<slug>/<slug>.md` behind forever. `memory import` repairs the scopes of
a legacy export on the way in, and both paths are idempotent.

## Role-aware injection

Only **approved** entries are injected:

| Role | Global | Role file | Project scopes |
|---|---|---|---|
| Architect / Reviewer | all | architect | decisions, conventions |
| Worker / Agent | all | worker | conventions, gotchas |
| Supervisor | all | supervisor | decisions, gotchas |

## Approval workflow

1. Workers and Architects add `memory_suggestions` to their handoff/review JSON
   (`{category, scope, content, tags}`).
2. The orchestrator stores them as **pending** and skips exact duplicates of existing
   entries. Content is redacted before storage.
3. The Supervisor or a human approves or rejects them: GUI Memory page, Task Detail →
   memory tab, `orchestrator memory approve|reject ID`, or `POST /api/memory/{id}/approve`.
   An entry whose scope was substituted is still pending, and the build log says which
   scope it was filed under. `orchestrator memory list` marks any row no prompt can read.
4. Approving re-renders the matching markdown file. Rejecting keeps the row (marked
   `rejected`) so the suggestion doesn't come back.

## Export / import

```bash
orchestrator memory export -o backup.json     # or: make memory-export FILE=backup.json
orchestrator memory import backup.json        # skips exact duplicates
```

API: `GET /api/memory/export`, `POST /api/memory/import`. The export format is
`{"version": 1, "entries": [...]}`.

## Relationship to prompt caching

Memory sits inside the static, cached prefix (sections 2–4). Any approval or edit changes
that prefix, which is fine at phase boundaries. Avoid changing memory while a phase is in
its review loop.
