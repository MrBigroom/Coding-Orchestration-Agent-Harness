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

These are the **only** scopes any injector can read, so membership — not just shape — is
checked at the boundaries that store a new or edited row (task T-4):
`POST /api/memory`, `PATCH /api/memory/{id}`, `orchestrator memory add` and
`memory_service.create/update`. An out-of-vocabulary scope is a `400`/CLI error that names
the allowed values — `MemoryError_` subclasses `ValueError`, so the API answer keeps the
frozen `{"error", "detail"}` shape. `PATCH` validates a throwaway preview **before**
assigning anything, so a refused edit leaves the stored row and its markdown byte-for-byte
unchanged rather than relying on a rollback.

For `category=role` the check covers **both** columns, not just `scope`. Injectability keys
off `role` first (`for_prompt` selects on it, `_file_for` names the file from it), so
`memory_service.check_role_name()` validates the role name against `ROLE_SCOPES` after
applying `ROLE_MEMORY_KEY`, and `create`, `update` and `import_all` all refuse
`role='logbook-writer' scope='worker'` — a row that looks fine to a scope-only check and is
read by nothing. `orchestrator memory add --role` takes a plain string, so it runs that same
check before Pydantic's enum can answer with a list of role *values* that never names the
role *files*; over HTTP the enum is an extra layer in front (a 422 in the frozen shape, so
an unknown role name is refused before the service is even entered).

Two paths *repair* instead of refusing, because refusing would lose facts that already
exist and cannot be re-typed by the caller:

| Path | Behaviour on an out-of-vocabulary scope | Why |
|---|---|---|
| create / PATCH / `memory add` | **rejects**, naming the allowed values | the caller is present and can fix it |
| a Worker suggestion (`memory_suggestions`) | **coerces** to the fallback, or drops a role suggestion | see below |
| `memory import` (JSON) | **repairs** to the fallback, then de-duplicates on the repaired scope | a pre-validation export must not lose its facts |
| `memory import --markdown` | **repairs** to the fallback | a dead scope-file must not be re-indexed as a new orphan |
| `memory fix-scopes` | **moves** the row | the repair path for rows already stored |

`memory_service.GLOBAL_SCOPES`, `PROJECT_SCOPES`, `ROLE_SCOPES` (the role *files*,
`ROLE_FILES` after applying `ROLE_MEMORY_KEY`), `ROLE_SCOPE_ALIASES` and
`SCOPE_VOCABULARY` are importable constants; before T-4 there was no `ROLE_SCOPES`, so no
check could be written for the role category.

### What happens to a Worker suggestion with a made-up scope

Suggestions are never trusted for their scope, and nothing is stored where no prompt can
reach it:

- `project` → coerced to `gotchas`, always.
- `global` → coerced to `environment`, always.
- `role` → **dropped with a warning** if it resolves to no role memory file; guessing a
  role would file someone's note under the wrong agent.

The fallback is the whole rule. Nothing is ever *inferred* from the words inside a
proposed scope: the scope decides **which roles receive the note** (`decisions` goes to
Architect+Supervisor, `gotchas` to Worker+Supervisor), so inferring `decisions` from
`decisions-and-gating` would silently re-route a Worker's gotcha, and a substring match
fires inside unrelated words. `orchestrator memory list` shows where it actually landed.

A `category=role` row is rendered by the pre-T-4 rule and T-4 does not change it: the
file is `role`, falling back to `scope`, through `ROLE_MEMORY_KEY` (`reviewer` →
architect, `agent` → worker). So a legacy row with `role='logbook-writer'` keeps rendering
to `roles/logbook-writer.md` — it is *reported* as uninjectable, never re-homed into the
scope's file, and `fix-scopes` refuses to invent a role for it.

Every substitution and every drop is returned in `SuggestionOutcome.notes` and written to
the build log as `memory_scope_substituted` / `memory_suggestion_dropped` (and said out
loud in the chat stream), so the operator can see what was rewritten.

## Auditing and repairing scopes

```bash
orchestrator memory list                  # `inj` column: ok or NO, reason printed below
orchestrator memory list --uninjectable   # only the rows no prompt can read
orchestrator memory fix-scopes --dry-run  # preview the moves and the dead files
orchestrator memory fix-scopes            # move them, re-render, collect the orphans
orchestrator memory fix-scopes --no-gc    # move and re-render, delete nothing
orchestrator memory fix-scopes --gc-only  # only collect dead files
```

A dry run only ever says "would": it prints `would move` / `would re-render` / `would
collect dead scope-file` / `would remove dead scope-file` and writes nothing, including
the file list. A real run reports **only the deletions it performed** — `--gc-only` lists
what `gc_orphan_files` unlinked rather than falling back to the pre-GC listing, so the
report cannot claim a file was removed while it is still on disk.

`fix-scopes` migrates rows stored before validation existed (entries #5/#22/#24 shipped
with free-form scopes that no injector could read). It **moves rows and never deletes
them**, leaves rejected rows alone, re-renders **both** the old and the new markdown file
so the old one cannot keep a stale `<!-- mem:ID -->` line, then removes rendered files
that no longer own an entry — a hand re-scope used to leave a title-only ghost such as
`memory/projects/<slug>/<slug>.md` behind forever.

The file collection is deliberately narrow, because hand-edited markdown in this tree is
operator input (`memory import --markdown`). A file is deleted only if **all four** hold:
it sits in a layout directory (`global/`, `roles/`, `projects/<slug>/`), its name is not a
canonical scope that `ensure_layout` owns, no approved entry maps to it, and its text is
something `render_file` could have written — first line exactly `# <stem>`, later lines
only blank or `- …  <!-- mem:N -->` marker lines. Prose, an un-marked bullet, a different
heading, or an `ensure_layout` header such as `# Project X — decisions` disqualifies the
whole file. Every interpolated value in this output is Rich-escaped, so a scope or reason
containing `[brackets]` is printed instead of being parsed as a style and vanishing.

`memory import` repairs the scopes of a legacy export on the way in, and both paths are
idempotent. The import duplicate check takes **both** spellings of a fact into account —
the scope a stored row still carries and the scope it would be repaired to — so importing
an export never manufactures a second copy of a fact that is already in the database,
even while the original is waiting for `fix-scopes`.

## Role-aware injection

Only **approved** entries are injected:

| Role | Global | Role file | Project scopes |
|---|---|---|---|
| Architect / Reviewer | all | architect | decisions, conventions |
| Worker / Agent | all | worker | conventions, gotchas |
| Supervisor | all | supervisor | decisions, gotchas |

`for_prompt` selects role memory on the **`role` column**, while rendering falls back to
`scope`. A legacy row with an empty `role` and a valid `scope` therefore renders into the
right file but is not injected; `create` can no longer produce that shape (`_validate`
fills `role` from `scope`), and `memory fix-scopes` reports role rows instead of guessing
a role for them.

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
