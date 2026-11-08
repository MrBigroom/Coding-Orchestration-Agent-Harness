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
role *files*. `POST /api/memory` gets the same words from `MemoryCreate` itself: a
`model_validator(mode="before")` runs `check_role_name` before the enum coerces, so the 422
detail says `allowed role memory files: architect|worker|supervisor` plus the two aliases
instead of `Input should be 'architect', 'supervisor', 'worker', 'reviewer' or 'agent'`. The
accepted set is unchanged — `role` is still typed as `Role`, so the request shape and its
OpenAPI enum are untouched and this is an error-text fix, not a contract change.

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
`ROLE_FILES` after applying `ROLE_MEMORY_KEY`), `ROLE_SCOPE_ALIASES` and `SCOPE_VOCABULARY`
are importable constants; before T-4 there was no `ROLE_SCOPES`, so no vocabulary check could
be written for the role category. They live in `core/memory_vocab.py` and `memory_service`
re-exports every one of them: `core/schemas.py` has to refuse the same shapes with the same
words, and it cannot import the service (`memory_service` imports `schemas`, so the reverse
import would be circular). The three rejection messages are produced there too —
`vocab_error(category, scope)` for a scope and `check_role_name(role)` for a role — so a
global rejection names `environment|preferences|patterns`, a project rejection
`conventions|decisions|gotchas` and a role rejection `architect|worker|supervisor`, at the
CLI, the service, the request model and the HTTP route alike.

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

## Committed artifacts are only trustworthy while they match the commit under review

This is the companion convention to the evidence-freshness check (`docs/build-loop.md`,
"Evidence freshness"). A `verify-evidence/<task>/` file is a measurement, not a permanent truth:
its HEAD stamp names the commit it was taken at. The deterministic Supervisor check compares that
stamp with the run's `commit_sha` and reports `stale` when code moved after the capture; a deleted
file is reported with the commit that deleted it, not treated as absent evidence. Refresh the whole
bundle with one command (`orchestrator task evidence <id>`), which runs the committed
`capture.sh`, canonicalises every stamp and writes `manifest.json`. Keep memory entries about
verification results phrased relative to that stamp — "at `<sha>` the suite was green", not "the
suite is green" — because a remembered green outlives the code it described. The check reports
staleness; it does not force a re-run.

## Relationship to prompt caching

Memory sits inside the static, cached prefix (sections 2–4). Any approval or edit changes
that prefix, which is fine at phase boundaries. Avoid changing memory while a phase is in
its review loop. Caps and demotions change the prefix too, so trim memory in one batch at a phase boundary rather than entry by entry — every edit costs a cache miss on the next run.
