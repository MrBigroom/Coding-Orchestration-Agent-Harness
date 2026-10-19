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
  `{"type":"end","run_id","status"}`.
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
  review.md, review.json, review.raw.txt?, verification.md, verification.json, run.json}`.
  Worktrees live at `worktrees/<project_slug>/<task_id>` and the build log at
  `build-log.jsonl`.
- **Provider adapter interface**: `providers/base.py` (`detect`, `health`, `complete`,
  optional `stream`, `validate_config`).
- **Harness adapter interface**: `adapters/base.py` (`detect`, `health`, `build_command`,
  `run`, `cancel`, `parse_result`, `capabilities`) and the template tokens.
- **CLI command surface**, unless a phase explicitly changes it.

## Out of scope

Postgres · Redis · Docker · Kubernetes · cloud deployment · multi-user auth ·
telemetry/analytics · Electron/Tauri desktop wrapper · mobile app · agent-to-agent
free-form chat · external GUI automation · scraping harness GUIs · automatic push to
remotes · automatic merge unless the merge gate is enabled · arbitrary shell execution
from the GUI unless explicitly enabled.
