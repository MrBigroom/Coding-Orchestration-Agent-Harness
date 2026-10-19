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

| Category | Required | Typical scopes |
|---|---|---|
| `global` | — | environment, preferences, patterns |
| `project` | `project_slug` | conventions, decisions, gotchas |
| `role` | `role` | architect, worker, supervisor |

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
