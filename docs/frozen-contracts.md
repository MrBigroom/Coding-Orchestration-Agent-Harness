# Frozen contracts

These apply after the **baseline contract phase** is approved. The baseline is the
current state: Alembic revision `0001_baseline`. Changing anything listed below requires
a contract-change request (see `build-loop.md`). A phase must explicitly authorize the
change in its Definition of Done.

## Frozen stack

- Python 3.12+
- FastAPI
- Uvicorn
- SQLModel or SQLAlchemy with SQLite (implemented with SQLModel on SQLAlchemy 2.x)
- Alembic
- Pydantic v2
- Typer
- Rich
- APScheduler (3.x line)
- React
- Vite
- TypeScript
- Tailwind CSS (v4)
- Official Anthropic SDK for Anthropic models
- OpenAI-compatible client support for Qwen/DeepSeek/GLM/local providers (official `openai` SDK)

## Frozen contracts

- **SQLite/SQLModel schema** after baseline approval: `src/orchestrator/core/models.py` +
  `src/orchestrator/migrations/versions/0001_baseline.py`. Changes ship only as new
  revisions. `tests/integration/test_migrations.py` fails on any drift between the models
  and the migrations.
- **REST API shapes under `/api`**: routes in `src/orchestrator/api/routes/`. Errors are
  always `{"error": str, "detail": any}`.
- **WebSocket event shapes under `/ws`**: `/ws/runs/{run_id}/logs` emits
  `{"type":"log","run_id","stream","line"}`, `{"type":"status","run_id","status"}`,
  `{"type":"end","run_id","status"}`. `/ws/chat/{id}` is documented in `chat.md`.
- **Task/run state machine transitions**: `src/orchestrator/core/state.py`.
- **handoff.json schema**:
  ```json
  {"phase": "string", "branch": "string",
   "status": "READY_FOR_REVIEW | CHANGES_APPLIED | BLOCKED", "summary": "string",
   "files_changed": ["string"],
   "tests": {"command": "string", "passed": 0, "failed": 0, "skipped": 0, "log_path": "string"},
   "dod_self_check": [{"item": "string", "met": true, "evidence": "string"}],
   "new_dependencies": [{"name": "string", "version": "string", "reason": "string"}],
   "assumptions": ["string"], "questions": ["string"], "blockers": ["string"],
   "contracts_touched": ["string"],
   "memory_suggestions": [{"category": "global | project | role", "scope": "string",
                           "content": "string", "tags": ["string"]}]}
  ```
- **review.json schema**:
  `{"verdict": "APPROVED | CHANGES_REQUESTED | NEEDS_HUMAN", "findings": [{"file_or_area", "issue", "violates"}], "missing_artifacts": [], "notes": []}`
- **verification.json schema**:
  `{"phase", "checks": [{"name", "status": "pass | fail | skipped | not_applicable", "command", "evidence", "notes"}], "contradicts_worker": bool, "final_result": "pass | fail | needs_human"}`
- **Memory entry schema**: `memory_entries` table + `MemoryCreate`/`MemorySuggestion`.
- **Artifact directory layout**: `runs/<run_id>/{prompt.md, stdout.log, stderr.log,
  patch.diff, diffstat.txt, test.log, test_summary.txt, handoff.md, handoff.json,
  review.md, review.json, review.raw.txt?, review_payload.md, verification.md,
  verification.json, run.json}`. `review_payload.md` (approved from CCR-T11-01, 2026-11-04)
  archives the exact reviewer input — `static_text()` + `dynamic_text()` as sent — plus the
  truncation manifest (item, bytes available, bytes included, budget, reason), separately
  labelled harness-detected and reviewer-asserted missing artifacts, the mechanical absence
  checks with `confirmed | refuted | unchecked` provenance, and the diff-completeness findings.
  Additive: no existing artifact name or shape changed. A run written before the review-payload
  merge simply has no such file; its absence is not a finding against that run.
  Worktrees live at `worktrees/<project_slug>/<task_id>` and the build log at
  `build-log.jsonl`.
- **Provider adapter interface**: `providers/base.py` (`detect`, `health`, `complete`,
  optional `stream`, `validate_config`).
- **Harness adapter interface**: `adapters/base.py` (`detect`, `health`, `build_command`,
  `run`, `cancel`, `parse_result`, `capabilities`) and the template tokens.
- **CLI command surface**, unless a phase explicitly changes it.
- **Path-independent verification commands**: the gate runs a project's test/lint/build command
  with cwd = the task worktree (`worktrees/<project_slug>/<task_id>`), never in the main checkout.
  A wrapper that proves where its imports resolved emits
  `ORCH_VERIFY|mode=<mode>|module=<name>|module_file=<abs>|worktree_root=<abs>` on stdout or
  stderr; `scripts/orch_verify.sh` is the reference implementation and
  `project_service.parse_orch_verify` (+ `project doctor --run-commands`) consumes it. Keys are
  order-independent; the field names and the `ORCH_VERIFY|` prefix are the contract. Approved from
  CCR-T6-01 (T-6) by the operator, 2026-11-04.
- **Task detail response**: `GET /api/tasks/{task_id}` returns the task row plus read-only derived
  collections. `recovery_commits` is an additive array of harness-created commits on the task's
  branch, each entry `{sha: str, short: str, subject: str, recovery: bool, recovery_run: str|null,
  author: str ("name <email>"), files: [{status: str, path: str}]}`. The field is always present
  (empty when there are none) and is derived from git history, never stored. Adding a derived field
  to an existing `/api` response is additive and allowed; changing or removing an existing key, or
  adding a new route, requires its own contract-change request. Approved from CCR-T8-02 (T-8) by the
  operator, 2026-11-04.
- **Build-log events**: the line shape `{timestamp, phase, task_id, run_id, role, event, details,
  commit_sha}` is frozen. A run's terminal status is one of `completed | failed | timeout |
  cancelled`, and **three of those four** have a named terminal event: `run_completed` (normal
  completion), `run_failed`, `run_timeout`. A `cancelled` run has **no** named event today — it is
  observable only as a `task_status` line reading `run cancelled` — and `run_cancelled` is not an
  event name yet; adding it is a T-9 requirement, deliberately not frozen here as if it existed.
  `run_error` is not a terminal status: it labels an unexpected exception inside the harness, after
  which the run still ends as `failed`. Event names are additive — a phase may add one, never rename
  or remove one. Where a machine consumer needs structure, `details` may carry a final segment
  ` | findings_json=<json object>`; the segment is always last, `json.loads` of the remainder is the
  payload, its absence means no payload, and no payload may contradict the human summary in the same
  string. Approved from CCR-T8-build-log-events (T-8) by the operator, 2026-11-04, after correcting
  its original overclaim that every terminal status already had an event.

## Out of scope

Postgres · Redis · Docker · Kubernetes · cloud deployment · multi-user auth ·
telemetry/analytics · Electron/Tauri desktop wrapper · mobile app · agent-to-agent
free-form chat · external GUI automation · scraping harness GUIs · automatic push to
remotes · automatic merge unless the merge gate is enabled · arbitrary shell execution
from the GUI unless explicitly enabled.
