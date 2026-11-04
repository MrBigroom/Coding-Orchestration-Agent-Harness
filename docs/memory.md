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

## Injection budget

Memory is a **static** prompt section, so `prompt_builder.enforce_budget` can never trim it:
uncapped growth squeezes the dynamic evidence (diff, handoff, verification) instead. Each
injected section therefore has its own cap, enforced in `memory_service.select_for_prompt`:

| Section | Cap setting | Default | Env |
|---|---|---|---|
| global | `max_memory_tokens_global` | 2000 | `ORCHESTRATOR_MAX_MEMORY_TOKENS_GLOBAL` |
| role | `max_memory_tokens_role` | 800 | `ORCHESTRATOR_MAX_MEMORY_TOKENS_ROLE` |
| project | `max_memory_tokens_project` | 4000 | `ORCHESTRATOR_MAX_MEMORY_TOKENS_PROJECT` |

`memory_budget_enabled=false` (env or `PATCH /api/settings`) restores inject-everything; a
cap of `0` disables that one section. `memory_budget_warn_fraction` (default `0.8`, env-only)
warns before anything is dropped.

Demotion rule, stated so it is reviewable: if a section fits, every approved entry is
injected. If it does not, entries are demoted **rot-prone first (names a task/round/commit),
then oldest first** — the newest durable entries are never the ones silently dropped.
Demotion is reporting, not deletion: the row stays approved in SQLite and is simply left out
of the render. Every exclusion surfaces as a warning in prompt preview
(`orchestrator prompt-preview`, the GUI Prompt Cache page) and in `orchestrator memory budget`.

Selection depends on entry content and ids only — no clock, no file access — so a rendered
section stays byte-identical between calls and does not churn the prompt cache.

## Triage (read-only)

```bash
orchestrator memory triage                      # pending queue, flagged and priced
orchestrator memory triage --status approved    # audit what is already injected
orchestrator memory triage --action merge --json
orchestrator memory budget --project myapp      # caps, usage, headroom, demotions
```

API: `GET /api/memory/triage` and `GET /api/memory/budget`. Both are additive, read-only, and
approve nothing.

Flags per entry:

| Flag | Meaning |
|---|---|
| `task_shape`, `bare_sha`, `forward_looking` | true only while the referenced task is open |
| `absolute_date`, `relative_time` | date- or "currently"-anchored; unverifiable later |
| `multi_fact` | several facts in one bullet; cannot be retracted singly |
| `imperative` | instruction-shaped text (memory is injected into every future prompt) |
| `missing-path` | a named repository file is not in the project checkout |
| `stale-branch-claim`, `stale-merge-claim` | the branch it describes is gone or merged |
| `contradicts-live-config` | claims, e.g., no test command while `projects.test_command` is set |
| `unknown-project` | `project_slug` matches no registered project |

Groupings: a **duplicate group** is two pending rows whose rare content words overlap by
≥50%; **amend candidates** are approved rows sharing ≥2 code entities. Duplication is
measured with rare-token containment, never a whole-text ratio — on a single-project corpus
every pair shares the domain vocabulary (measured: 461 of 595 pending pairs above 0.72 while
the true duplicate count was zero), so ratio dedup collapses unrelated entries.

Suggested actions (advisory, one per entry): `fix-stale`, `rewrite-durable`, `merge`,
`amend`, `split`, `verify-claims`, `approve`. Triage never writes: approving and rejecting
stay explicit (`memory approve|reject`, the GUI, `POST /api/memory/{id}/approve`), so the
gate remains human by construction.

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
its review loop. Caps and demotions change the prefix too, so trim memory in one batch at a phase boundary rather than entry by entry — every edit costs a cache miss on the next run.
