BEGIN PROMPT

You are acting as a senior full-stack engineer, agent orchestration architect, and technical writer. Build a local-first coding orchestration harness for me. The project name is `coding-orchestrator`.

Save this entire prompt as `docs/product-spec.md` in the repository. If the repository already exists, continue carefully without destroying existing work. If the folder does not exist, create it.

If session length prevents completing everything at once, prioritize in this order:

1. Create the repository scaffold and all documentation files.
2. Implement backend core models, state machine, database, and Alembic migrations.
3. Implement mock adapters/providers and the demo loop.
4. Implement CLI.
5. Implement API.
6. Implement memory and prompt caching services.
7. Implement frontend GUI or fallback static GUI.
8. Add tests and final polish.

Do not ask many clarifying questions unless something is truly blocking. Make sensible local-first assumptions. If an external tool’s exact CLI is uncertain, make the adapter configurable and document it. Do not hallucinate features of external harnesses. If a feature is stubbed, clearly mark it as stubbed or experimental.

PROJECT CONTEXT

I use CachyOS Linux with Hyprland. I prefer GUIs for coding harnesses, but I accept using CLI/headless modes under the hood for automation. The goal is not to build a normal AI chat assistant. The goal is to build a custom orchestration layer with a GUI on top of CLI/headless coding agents and model APIs.

My development workflow has three roles:

1. Architect / Reviewer
   - Primary model: Claude Opus 5.5, configurable.
   - Responsibilities:
     - write implementation plans
     - define acceptance criteria
     - identify risks
     - review phase diffs against Definition of Done
     - review frozen contracts
     - decide when to escalate
     - act as final reviewer for important work
   - This role has no execution access. It must never claim it ran commands, tests, or greps.

2. Supervisor
   - Preferred models or processes: Claude Sonnet 5.5, Qwen3.8-Max, GLM-5.3, or a deterministic local process.
   - Responsibilities:
     - run the process
     - launch and monitor worker builds
     - independently verify evidence
     - enforce gate order
     - coordinate fixes
     - perform live verification
     - merge only when merge gate is enabled and all checks pass
   - The Supervisor does not write feature code by default.
   - If the Supervisor is an LLM, it should operate in recommendation-only mode unless wrapped by a controlled executor with allowed commands.

3. Worker
   - Preferred cheap models:
     - Qwen3.8-Flash
     - DeepSeek-v4.1-Flash
     - GLM flash-class or GLM-5.3 where appropriate
   - Responsibilities:
     - implement one phase/task at a time
     - work in isolated branch/worktree
     - write tests
     - run tests
     - produce handoff artifacts
     - never merge
     - never self-certify
     - never modify main directly

I also plan to use a Qwen Cloud token plan for cheaper models such as Qwen3.8-Max, Qwen3.8-Flash, DeepSeek-v4.1-Flash, and GLM-5.3. Claude Opus 5.5 should be reserved for architect/reviewer/high-risk work and should not be burned on trivial agent tasks unless explicitly escalated.

External coding harnesses I care about:

- OpenCode: preferred general/model-agnostic coding harness.
- DeepSeek Harness: official DeepSeek agent harness, CLI command likely `dsh`.
- ZCode: Z.ai harness for GLM models, may have an unofficial CLI such as `zcode`.
- Qwen Code: Alibaba/Qwen harness optimized for Qwen models.
- Aider: optional alternative coding harness.
- Claude Code CLI: optional. Do not assume my Claude subscription can be used as an API. Use official Anthropic API keys or legitimate CLI auth only where supported.
- Nous Research Hermes: optional future model/provider only. Do not make Hermes central. Treat it as an optional OpenAI-compatible or local model provider if configured.

CRITICAL DESIGN DECISIONS

Do not try to automate the GUIs of OpenCode, ZCode, Qwen Code, DeepSeek Harness, or any other external harness.

Instead:

- Build a custom local GUI dashboard/control plane.
- Use CLI/headless adapters to run external coding harnesses.
- Use model API adapters for planning/review/simple agent tasks.
- Store all coordination state in a local database.
- Use files, patches, logs, git worktrees, and structured task artifacts as the source of truth.
- Do not implement free-form agent-to-agent chat as the primary coordination mechanism.
- Use structured artifacts and state transitions for all role communication.
- Optimize all LLM prompt construction for prompt caching.
- Implement a persistent shared memory system.

PRIMARY OBJECTIVE

Build a working MVP of a local orchestration harness with:

1. Python FastAPI backend.
2. SQLite database.
3. Alembic migrations.
4. Local web GUI.
5. CLI.
6. Task queue/state machine.
7. Role-based build loop.
8. Model/provider router.
9. Harness adapter system.
10. Git worktree isolation.
11. Test/lint/build verification.
12. Review/accept/reject gates.
13. Supervisor live verification.
14. Merge gate, disabled by default.
15. Scheduler/cron support.
16. Log/artifact collection.
17. Prompt caching service.
18. Shared memory system.
19. Mock adapters so the system works without external tools or API keys.

TARGET ENVIRONMENT

- OS: CachyOS Linux.
- Desktop: Hyprland/Wayland.
- Local-first, single-user.
- No cloud deployment.
- No multi-user authentication system.
- Bind web server to 127.0.0.1 by default.
- No telemetry.
- No external analytics.
- No Docker required.
- No Kubernetes.
- No Redis.
- No Postgres.
- No message broker.
- No microservices.
- SQLite only for persistence.

TECH STACK

Backend:

- Python 3.12+
- FastAPI
- Uvicorn
- SQLModel or SQLAlchemy with SQLite
- Alembic for migrations
- Pydantic v2
- pydantic-settings
- Typer for CLI
- Rich for CLI output
- APScheduler for cron/scheduled tasks
- asyncio subprocess for external processes
- httpx where needed
- Official Anthropic SDK for Anthropic models
- OpenAI-compatible client support for Qwen/DeepSeek/GLM/local providers
- pytest for tests
- ruff for lint/format

Frontend:

- React
- Vite
- TypeScript
- Tailwind CSS
- react-router-dom
- Plain fetch for API calls
- WebSocket for live logs
- Dark theme by default
- Desktop-first layout
- No SSR
- No heavy UI framework unless absolutely necessary

If Node/npm is unavailable or frontend build fails, provide a minimal fallback static HTML/JS UI served by FastAPI. The backend and CLI must remain fully usable even without the React frontend build.

PROJECT STRUCTURE

Create this structure:

coding-orchestrator/
  README.md
  Makefile
  justfile
  pyproject.toml
  alembic.ini
  .gitignore
  .env.example
  docs/
    product-spec.md
    roles.md
    build-loop.md
    prompt-caching.md
    memory.md
    frozen-contracts.md
    adapters.md
    providers.md
    configuration.md
    decisions.md
    security.md
  scripts/
    setup.sh
    dev.sh
    demo.sh
    build.sh
  src/
    orchestrator/
      __init__.py
      __main__.py
      cli.py
      api/
        __init__.py
        main.py
        routes/
        dependencies.py
        static_fallback/
      core/
        __init__.py
        config.py
        db.py
        models.py
        schemas.py
        state.py
        events.py
        logging.py
        security.py
      services/
        __init__.py
        task_service.py
        run_service.py
        queue_service.py
        scheduler_service.py
        review_service.py
        planning_service.py
        supervisor_service.py
        git_service.py
        artifact_service.py
        log_service.py
        process_service.py
        memory_service.py
        prompt_builder.py
        provider_service.py
        adapter_service.py
      adapters/
        __init__.py
        base.py
        registry.py
        mock_harness.py
        opencode.py
        deepseek_harness.py
        zcode.py
        qwen_code.py
        aider.py
        claude_cli.py
      providers/
        __init__.py
        base.py
        registry.py
        mock_provider.py
        anthropic_provider.py
        openai_compatible_provider.py
      prompts/
        architect.py
        supervisor.py
        worker.py
        memory.py
      utils/
        __init__.py
        git.py
        redaction.py
        retry.py
        json_output.py
        slug.py
        paths.py
        truncate.py
  web/
    package.json
    vite.config.ts
    tsconfig.json
    tailwind.config.js
    postcss.config.js
    index.html
    src/
      main.tsx
      App.tsx
      api/
      components/
      pages/
      hooks/
      styles/
  tests/
    unit/
    integration/
    fixtures/
  examples/
    sample-project/

DATABASE MODELS

Implement at least these entities:

Project:
- id
- name
- path
- base_branch
- test_command
- lint_command
- build_command
- allowed_commands
- notes
- created_at
- updated_at

Task:
- id
- short_id
- project_id
- parent_task_id
- phase_number
- phase_slug
- title
- description
- kind
- status
- risk
- priority
- tags
- acceptance_criteria
- definition_of_done
- frozen_contracts
- out_of_scope
- branch_name
- assigned_provider_profile
- assigned_harness_adapter
- handoff_status
- review_status
- verification_status
- merge_status
- created_by
- source
- created_at
- updated_at

Task kind:
- coding
- agent
- review
- maintenance

Task status:
- draft
- planned
- queued
- blocked
- in_progress
- review
- accepted
- rejected
- archived

Task risk:
- low
- medium
- high

Task priority:
- low
- normal
- high

Task source:
- manual
- schedule
- escalation
- retry

Run:
- id
- short_id
- task_id
- status
- provider_profile
- harness_adapter
- command_preview
- worktree_path
- branch_name
- commit_sha
- exit_code
- started_at
- finished_at
- duration_ms
- error
- usage
- artifacts_path
- created_at

Run status:
- pending
- preparing
- running
- testing
- completed
- failed
- cancelled
- timeout

Review:
- id
- task_id
- run_id
- reviewer_kind
- reviewer_model
- verdict
- findings
- reasoning
- created_at

Review verdict:
- APPROVED
- CHANGES_REQUESTED
- NEEDS_HUMAN

Verification:
- id
- task_id
- run_id
- verifier_kind
- status
- checks_json
- evidence_path
- contradicts_worker
- created_at

Schedule:
- id
- name
- cron_expression
- task_template_json
- enabled
- last_run_at
- next_run_at
- created_at
- updated_at

Provider:
- id
- name
- kind
- base_url
- api_key_env
- enabled
- notes

Provider kind:
- mock
- anthropic
- openai_compatible

ModelProfile:
- id
- name
- role
- provider_id
- model_name
- temperature
- max_tokens
- timeout_seconds
- enabled
- notes

Role:
- architect
- supervisor
- worker
- reviewer
- agent

HarnessAdapter:
- id
- name
- kind
- command
- args_template_json
- env_vars_json
- timeout_seconds
- enabled
- capabilities_json
- notes

MemoryEntry:
- id
- category
- scope
- project_slug
- role
- content
- source
- tags_json
- approved
- created_at
- updated_at

Memory category:
- global
- project
- role

Memory scope examples:
- environment
- preferences
- patterns
- conventions
- decisions
- gotchas

BuildLogEntry:
- id
- timestamp
- phase
- task_id
- run_id
- role
- event
- details
- commit_sha

Setting:
- key
- value_json
- updated_at

ALEMBIC MIGRATIONS

Use Alembic for schema changes.

Requirements:

- Create an initial baseline migration.
- Do not rely only on SQLModel/SQLAlchemy create_all after baseline.
- Migration upgrade and downgrade must work against a populated test database.
- Add tests for migration upgrade/downgrade.
- If a phase requires a schema change, Worker must stop and request a contract-change if contracts are frozen.
- During baseline contract phase, schema creation is allowed.
- After baseline approval, schema changes require explicit contract-change approval.

ROLE-BASED BUILD LOOP

Implement the role loop as a state machine and artifact flow, not as chat.

Role prompts must be stored in docs/roles.md and also embedded in the code under src/orchestrator/prompts.

Architect/Reviewer rules:

- Has no execution access.
- Must never claim it ran commands, tests, or greps.
- Reviews only supplied artifacts: diff, handoff block, handoff.json, test logs, verification narrative, and contract notes.
- Missing required artifacts are findings.
- Output must be parseable.
- If output is ambiguous, treat it as CHANGES_REQUESTED.
- Approves rarely and only on evidence.

Worker rules:

- Implements one phase/task at a time.
- Reads phase Steps and Definition of Done before coding.
- Works on phase branch and isolated worktree.
- Never modifies main directly.
- Never force pushes.
- Never deletes branches.
- Never commits secrets.
- Writes tests and runs tests.
- Produces human-readable handoff block and machine-readable handoff.json.
- If blocked, emits BLOCKED handoff and stops immediately.
- Does not wait for interactive chat.
- Does not start next phase.
- Does not merge.
- Applies reviewer feedback on the same phase branch.

Supervisor rules:

- Does not write feature code by default.
- Launches and monitors worker runs.
- Monitors process state, git status, artifact updates, handoff files, and log growth.
- Computes real elapsed time.
- Recognizes BLOCKED handoff as waiting for decision, not still working.
- Before retrying, checks git status and recovers dirty tree safely inside isolated worktree.
- Triggers Architect review only when tests green and handoff received.
- After APPROVED, ensures nits are fixed by Worker or explicitly deferred in writing.
- Performs live verification and records evidence.
- Merges only if merge gate enabled and all checks pass.
- If merge gate disabled, marks task accepted and outputs exact merge command.
- Logs gotchas, failures, and fixes to build log.

BRANCH AND WORKTREE STRATEGY

Use phase branches.

Branch name format:

phase-N-<slug>

Example:

phase-1-scaffold-backend

Rules:

- Each task/phase has one persistent branch.
- Multiple runs/retries for that phase may reuse the same branch and worktree.
- The accepted phase branch becomes the merge candidate.
- Run artifacts are stored per run.
- The merge candidate is the phase branch, not a temporary run branch unless recovery requires one.
- Worktrees are stored under ORCHESTRATOR_HOME/worktrees/<project_slug>/<task_id>.
- Only clean up worktrees managed by the orchestrator.
- Use marker files or managed path checks before cleanup.
- Never delete user branches.
- Never force push.
- Never modify main directly.
- Recovery commits must be clearly labeled and logged.

HANDOFF ARTIFACTS

The Worker must emit both human and machine-readable handoff artifacts.

Human handoff block fields:

- Branch
- Summary
- Files changed
- Tests: command -> counts
- DoD self-check per item
- New dependencies
- Assumptions
- Questions
- Blockers

Machine-readable handoff.json schema:

{
  "phase": "string",
  "branch": "string",
  "status": "READY_FOR_REVIEW | CHANGES_APPLIED | BLOCKED",
  "summary": "string",
  "files_changed": ["string"],
  "tests": {
    "command": "string",
    "passed": 0,
    "failed": 0,
    "skipped": 0,
    "log_path": "string"
  },
  "dod_self_check": [
    {
      "item": "string",
      "met": true,
      "evidence": "string"
    }
  ],
  "new_dependencies": [
    {
      "name": "string",
      "version": "string",
      "reason": "string"
    }
  ],
  "assumptions": ["string"],
  "questions": ["string"],
  "blockers": ["string"],
  "contracts_touched": ["string"],
  "memory_suggestions": [
    {
      "category": "global | project | role",
      "scope": "string",
      "content": "string",
      "tags": ["string"]
    }
  ]
}

Store handoff artifacts in the run artifact directory:

runs/<run_id>/handoff.md
runs/<run_id>/handoff.json

If status is BLOCKED, task status becomes blocked.

REVIEW ARTIFACTS

Architect output must be stored as:

runs/<run_id>/review.md
runs/<run_id>/review.json

review.json schema:

{
  "verdict": "APPROVED | CHANGES_REQUESTED | NEEDS_HUMAN",
  "findings": [
    {
      "file_or_area": "string",
      "issue": "string",
      "violates": "string"
    }
  ],
  "missing_artifacts": ["string"],
  "notes": ["string"]
}

If Architect output cannot be parsed confidently, store raw output and set verdict to CHANGES_REQUESTED with a finding saying review output was ambiguous.

SUPERVISOR VERIFICATION ARTIFACTS

Store:

runs/<run_id>/verification.md
runs/<run_id>/verification.json

verification.json schema:

{
  "phase": "string",
  "checks": [
    {
      "name": "string",
      "status": "pass | fail | skipped | not_applicable",
      "command": "string",
      "evidence": "string",
      "notes": "string"
    }
  ],
  "contradicts_worker": false,
  "final_result": "pass | fail | needs_human"
}

Rules:

- Verify only what the phase claims to deliver.
- Files must exist on disk.
- Endpoints must work against the real local stack when applicable.
- DB rows must exist where expected.
- Migrations must apply and downgrade only if migrations are in scope for the phase.
- LLM output coherence checks only apply if the phase involves LLM behavior.
- If a check is not applicable, record N/A with reason.
- If a check contradicts the worker’s story, believe the check.

BUILD LOG

Maintain a machine-readable build log:

ORCHESTRATOR_HOME/build-log.jsonl

Each entry:

{
  "timestamp": "ISO-8601",
  "phase": "string",
  "task_id": "string",
  "run_id": "string",
  "role": "supervisor | worker | architect | system",
  "event": "string",
  "details": "string",
  "commit_sha": "string or null"
}

The GUI should expose build log viewing.

MERGE GATE

Settings must include:

- merge_enabled default false
- auto_merge_enabled default false
- supervisor_microfixes_enabled default false

Rules:

- Merge requires APPROVED review.
- Merge requires supervisor live verification pass.
- If merge_enabled false, mark task accepted and output exact merge command.
- If merge_enabled true and all checks pass, merge with --no-ff.
- Merge commit message must include verdict, verification summary, deferred gaps, and regression fixes.
- Do not push to remotes by default.
- Do not auto-merge by default.

PROMPT CACHING REQUIREMENTS

This system must be optimized for prompt caching.

Implement a prompt builder service.

Prompt construction order must always be:

1. System prompt / role definition.
2. Global memory.
3. Role-specific memory.
4. Project memory.
5. Frozen contracts / product spec / architecture context.
6. Phase instructions / Definition of Done.
7. Historical context, if needed.
8. Current dynamic state: latest diff, latest test summary, latest handoff, latest feedback.

Rules:

- Static content must come before dynamic content.
- Never inject dynamic state before static context.
- Memory is part of the cached prefix.
- Memory updates should happen at phase boundaries, not mid-conversation, unless explicitly approved.
- Use Anthropic cache breakpoints where supported.
- For OpenAI-compatible providers, rely on prefix caching where available.
- Do not send full file contents if a diff is sufficient.
- Do not send raw logs if they exceed configured limits.
- Truncate and summarize logs/test output before sending to LLMs.
- Test output should be summarized as passed/failed/skipped counts plus concise failure traces.
- If context exceeds configured max size, produce a structured summary instead of raw content.
- Do not send secrets, .env files, credentials, or private keys to LLM providers.
- Redact prompts and logs.

Add a prompt cache preview feature in CLI/GUI:

- Show which sections are static.
- Show which sections are dynamic.
- Show approximate token counts per section.
- Warn if dynamic content appears before static content.

SHARED MEMORY SYSTEM

Implement a persistent, role-aware shared memory system.

Memory storage layout:

ORCHESTRATOR_HOME/memory/
  global/
    environment.md
    preferences.md
    patterns.md
  projects/
    <project-slug>/
      conventions.md
      decisions.md
      gotchas.md
  roles/
    architect.md
    worker.md
    supervisor.md

Also index memory in SQLite table memory_entries.

Memory rules:

- Memory can be global, project-scoped, or role-scoped.
- Memory entries can be approved or unapproved.
- Only approved memory is injected into prompts.
- Workers and Architects can suggest memory updates via handoff.json memory_suggestions.
- Supervisor reviews and approves/rejects memory suggestions.
- Memory updates should be written to markdown files and indexed in SQLite.
- Memory GUI page must allow browsing, searching, editing, approving, rejecting, exporting, and importing.
- Memory CLI must support list/add/approve/reject/search/export/import.
- Memory injection must be role-aware.
- Architect gets architect role memory plus project decisions and conventions.
- Worker gets worker role memory plus project conventions and gotchas.
- Supervisor gets supervisor role memory plus project decisions and gotchas.
- Global environment/preferences memory is available to all roles.
- Memory must be loaded as part of the static prompt prefix for caching.
- If memory files change, cache prefix changes; this is acceptable at phase boundaries.

Memory API endpoints:

- GET /api/memory
- POST /api/memory
- PATCH /api/memory/{id}
- POST /api/memory/{id}/approve
- POST /api/memory/{id}/reject
- GET /api/memory/search?q=
- GET /api/memory/export
- POST /api/memory/import

MODEL/PROVIDER ROUTING

Implement provider and model profile management.

Default profiles should be editable placeholders:

- Architect: Anthropic, model placeholder claude-opus-5-5.
- Supervisor: Anthropic, model placeholder claude-sonnet-5-5.
- Supervisor alternative: OpenAI-compatible, model placeholder qwen3.8-max.
- Qwen worker: OpenAI-compatible, model placeholder qwen3.8-flash.
- DeepSeek worker: OpenAI-compatible or DeepSeek provider, model placeholder deepseek-v4.1-flash.
- GLM worker: OpenAI-compatible, model placeholder glm-5.3.
- Optional Hermes: disabled by default, OpenAI-compatible/local placeholder.

Routing intent:

- Architect/planning/final high-risk review: Opus profile.
- Supervisor/dispatch/review: Sonnet profile or Qwen3.8-Max profile.
- Coding worker:
  - if tag includes deepseek or glm, pick that worker profile and use its model route
  - if tag includes qwen and Qwen Code adapter available, prefer Qwen Code
  - otherwise prefer OpenCode if available
  - fallback to Aider if available
  - fallback to direct API worker only if no harness is available and task is simple
- Background/cron agent tasks:
  - prefer flash-class models
  - avoid Opus unless explicitly escalated
- Hermes:
  - only if explicitly selected or configured
  - never default

The router must support dry-run preview: show what provider/model/harness/command would be used without executing.

EXTERNAL HARNESS ADAPTERS

Build an adapter system for external coding harnesses.

Required adapters:

- MockHarnessAdapter
- OpenCodeAdapter
- DeepSeekHarnessAdapter
- ZCodeAdapter
- QwenCodeAdapter
- AiderAdapter
- ClaudeCliAdapter

Each adapter must implement:

- detect()
- health()
- build_command()
- run()
- cancel()
- parse_result()
- capabilities

Critical rule:

Do not invent exact CLI flags for external harnesses if uncertain.

Instead:

- Make command templates configurable.
- Provide sensible defaults.
- Detect binary existence.
- Allow user to edit command/args/env in GUI or DB.
- Document how to verify each adapter manually.
- If exact CLI usage is uncertain, mark adapter experimental.

Example editable command templates:

OpenCode:
  command: opencode
  args: run --cwd {cwd} --prompt-file {prompt_file}

DeepSeek Harness:
  command: dsh
  args: run --cwd {cwd} --prompt-file {prompt_file}

ZCode:
  command: zcode
  args: --cwd {cwd} --mode yolo --json --prompt {prompt}

Qwen Code:
  command: qwen-code
  args: run --cwd {cwd} --prompt-file {prompt_file}

Aider:
  command: aider
  args: --yes-always --message-file {prompt_file}

Claude CLI:
  command: claude
  args: -p {prompt_file}

These are examples only. Make them configurable and document that they may need adjustment.

For each run:

- Create a task prompt file.
- Create or reuse isolated phase worktree.
- Run adapter command inside worktree.
- Capture stdout/stderr to files.
- Capture exit code.
- Collect git diff/patch.
- Store artifacts.
- Run verification commands.
- Update run state.

DIRECT MODEL PROVIDERS

Providers are used for:

- planning
- supervision
- review
- simple agent tasks
- fallback coding tasks only when no harness is suitable

Provider interface:

- detect()
- health()
- complete()
- stream optional
- validate_config()

Providers must support structured JSON output.

Implement utilities to:

- request JSON output
- extract JSON from markdown fences
- validate with Pydantic
- retry once if invalid
- fail clearly if still invalid

Do not send secrets, .env, credentials, or sensitive environment values to providers.

Redact logs and prompts.

GUI

Build a local web GUI.

Pages:

1. Dashboard
   - active runs
   - queued tasks
   - failed tasks
   - blocked tasks
   - adapter/provider health
   - memory pending approvals
   - recent activity

2. Tasks
   - task board or table
   - filter by project/status/kind/risk/tags
   - create task
   - queue task
   - dry-run preview
   - retry
   - cancel
   - archive
   - mark blocked/unblocked

3. Task Detail
   - overview
   - phase DoD
   - runs
   - logs
   - diff
   - handoff
   - review
   - verification
   - memory suggestions
   - accept/reject buttons
   - escalate button
   - retry with different model/harness

4. Projects
   - add project
   - edit project
   - validate project
   - project doctor
   - test/lint/build commands
   - base branch
   - allowed commands

5. Models/Providers
   - providers list
   - model profiles list
   - enable/disable
   - show whether API key env is set, but never show secret value
   - test connection optional

6. Adapters
   - detected external harnesses
   - command templates
   - environment variables
   - enable/disable
   - detect/refresh
   - health/status

7. Memory
   - browse by category/scope/role/project
   - search
   - approve/reject pending suggestions
   - edit entries
   - export/import

8. Prompt Cache Preview
   - show prompt sections
   - show static/dynamic sections
   - show approximate token counts
   - warn if dynamic state appears before static state

9. Schedules
   - create schedule
   - enable/disable
   - run now
   - last/next run
   - cron validation

10. Settings
   - global settings
   - max parallel runs
   - scheduler enabled
   - merge_enabled
   - auto_merge_enabled
   - supervisor_microfixes_enabled
   - token auth optional
   - theme dark by default

GUI requirements:

- Dark theme default.
- Simple, clean, desktop-first layout.
- Monospace font for logs/diff.
- Live logs via WebSocket.
- Fallback polling if WebSocket fails.
- Status badges.
- Confirmation dialogs for destructive actions.
- No external CDN dependencies.
- No analytics.
- No telemetry.
- API base URL relative by default.
- If optional token auth is enabled, support token header and minimal token entry UI.
- Do not show secrets.
- Do not allow arbitrary shell execution from GUI unless explicitly enabled and allowed by project settings.

CLI

Implement a Typer CLI named orchestrator.

Commands:

orchestrator init
orchestrator doctor
orchestrator serve
orchestrator open
orchestrator project add
orchestrator project list
orchestrator project doctor
orchestrator task create
orchestrator task list
orchestrator task show
orchestrator task run
orchestrator task dry-run
orchestrator task cancel
orchestrator task accept
orchestrator task reject
orchestrator task retry
orchestrator task block
orchestrator task unblock
orchestrator adapters detect
orchestrator adapters list
orchestrator providers list
orchestrator models list
orchestrator schedule add
orchestrator schedule list
orchestrator schedule enable
orchestrator schedule disable
orchestrator schedule run
orchestrator memory list
orchestrator memory add
orchestrator memory approve
orchestrator memory reject
orchestrator memory search
orchestrator memory export
orchestrator memory import
orchestrator prompt-preview
orchestrator demo
orchestrator cleanup --dry-run
orchestrator reset --i-understand-data-loss

The CLI should be usable without the GUI.

API

Expose REST API under /api.

Required endpoint groups:

- /api/health
- /api/projects
- /api/tasks
- /api/runs
- /api/reviews
- /api/verifications
- /api/providers
- /api/model-profiles
- /api/adapters
- /api/schedules
- /api/settings
- /api/artifacts
- /api/logs
- /api/memory
- /api/prompt-preview
- /api/build-log

WebSocket:

- /ws/runs/{run_id}/logs

API rules:

- Use proper HTTP status codes.
- Use structured errors.
- Validate input with Pydantic.
- Do not expose secrets.
- Do not allow path traversal in artifact endpoints.
- Serve artifacts only from managed ORCHESTRATOR_HOME.
- Support optional local token header X-Orchestrator-Token if token configured.
- Bind to 127.0.0.1 by default.
- If host is not localhost, require token and warn loudly.

CONFIGURATION

Use environment variables and .env.

Important settings:

ORCHESTRATOR_HOME
ORCHESTRATOR_DB_URL
ORCHESTRATOR_BIND_HOST
ORCHESTRATOR_BIND_PORT
ORCHESTRATOR_TOKEN
ORCHESTRATOR_LOG_LEVEL
ORCHESTRATOR_MAX_PARALLEL_RUNS
ORCHESTRATOR_MAX_PARALLEL_RUNS_PER_PROJECT
ORCHESTRATOR_ENABLE_SCHEDULER
ORCHESTRATOR_ENABLE_WORKER
ORCHESTRATOR_WORKTREE_ROOT
ORCHESTRATOR_ARTIFACT_ROOT
ORCHESTRATOR_LOG_ROOT
ORCHESTRATOR_MEMORY_ROOT
ORCHESTRATOR_RUN_RETENTION_DAYS
ORCHESTRATOR_GIT_USER_NAME
ORCHESTRATOR_GIT_USER_EMAIL
ORCHESTRATOR_DEFAULT_BASE_BRANCH
ORCHESTRATOR_FRONTEND_DIST
ORCHESTRATOR_AUTOSTART_BROWSER
ORCHESTRATOR_MERGE_ENABLED
ORCHESTRATOR_AUTO_MERGE_ENABLED
ORCHESTRATOR_SUPERVISOR_MICROFIXES_ENABLED
ORCHESTRATOR_MAX_PROMPT_CONTEXT_TOKENS
ORCHESTRATOR_MAX_LOG_TOKENS
ORCHESTRATOR_MAX_DIFF_TOKENS

Default ORCHESTRATOR_HOME:

${XDG_DATA_HOME:-$HOME/.local/share}/coding-orchestrator

Provider env examples:

ANTHROPIC_API_KEY
ANTHROPIC_BASE_URL

QWEN_OPENAI_BASE_URL
QWEN_API_KEY

DEEPSEEK_BASE_URL
DEEPSEEK_API_KEY

GLM_OPENAI_BASE_URL
GLM_API_KEY

HERMES_OPENAI_BASE_URL
HERMES_API_KEY

Adapter env examples:

OPENCODE_COMMAND
DEEPSEEK_HARNESS_COMMAND
ZCODE_COMMAND
QWEN_CODE_COMMAND
AIDER_COMMAND
CLAUDE_CLI_COMMAND

Create .env.example with placeholders only.

SAFETY AND SECURITY

Requirements:

- No secrets in logs.
- No secrets in DB.
- No secrets in GUI.
- Redact common secret patterns from logs and command previews.
- Do not pass full environment to child processes unless necessary.
- Pass only required environment variables to adapters.
- Do not use shell=True.
- Do not run arbitrary commands from GUI unless explicitly enabled.
- Do not merge automatically by default.
- Do not push automatically by default.
- Do not delete user branches.
- Do not delete non-managed worktrees.
- Do not expose server publicly by default.
- Do not send .env, credentials, or private keys to LLM providers.
- Use safe path validation for artifact access.
- Use safe git operations.
- Avoid destructive commands.
- Cleanup command must support --dry-run.

MOCK/DEMO MODE

The system must work without external tools or API keys.

Implement:

MockProvider:
- returns valid structured planning/review/worker JSON
- can simulate success/failure based on task metadata

MockHarnessAdapter:
- creates a small deterministic file change
- exits successfully by default
- can simulate failure if task tag includes mock-fail
- can simulate timeout if task tag includes mock-timeout
- can simulate blocked if task tag includes mock-blocked

Create examples/sample-project as a tiny git repository with a simple test script and no heavy dependencies.

Create demo command:

orchestrator demo

Demo should:

1. Initialize orchestrator home if needed.
2. Register sample project if not registered.
3. Create a mock phase task.
4. Run MockHarnessAdapter.
5. Produce handoff.json, patch, logs, artifacts.
6. Run mock verification.
7. Create mock Architect review.
8. Create mock Supervisor verification.
9. Show final task state.
10. Suggest memory entries and approve one mock memory entry.

The demo must not require internet, API keys, OpenCode, DeepSeek Harness, ZCode, Qwen Code, Aider, or Claude CLI.

TESTING

Write meaningful tests.

Required tests:

- task state machine transitions
- router default behavior
- redaction utility
- JSON extraction/validation utility
- git worktree manager using temporary git repo
- mock harness end-to-end run
- queue claim logic
- schedule cron validation
- API health endpoint
- task creation endpoint
- artifact path traversal protection
- memory approval flow
- prompt builder static/dynamic ordering
- prompt cache preview detects dynamic-before-static error
- Alembic migration upgrade/downgrade against populated test DB

Rules:

- No network calls in tests.
- Use mock providers/adapters.
- Use temporary directories.
- Use real git if available for integration tests.
- Mark external CLI tests as optional/skippable.
- Default test command should avoid external dependencies.

BUILD/RUN COMMANDS

Provide both Makefile and justfile.

Targets:

setup
dev
start
test
lint
format
doctor
demo
build
clean
memory-export
memory-import

Expected behavior:

make setup:
- create Python venv
- install backend dependencies
- install frontend dependencies if npm available
- initialize orchestrator home
- run alembic upgrade head

make dev:
- run FastAPI backend with reload on port 8321
- run Vite dev server on port 5173 if available

make start:
- run production backend on port 8321
- serve built frontend if available
- serve fallback UI if frontend build missing

make demo:
- run orchestrator demo

make doctor:
- run orchestrator doctor

make test:
- run pytest

make lint:
- run ruff check

make format:
- run ruff format

DOCUMENTATION REQUIREMENTS

Create concise but complete docs.

Required docs:

README.md
docs/product-spec.md
docs/roles.md
docs/build-loop.md
docs/prompt-caching.md
docs/memory.md
docs/frozen-contracts.md
docs/adapters.md
docs/providers.md
docs/configuration.md
docs/decisions.md
docs/security.md

README must include:

- what the project is
- setup instructions
- how to run demo
- how to start backend/GUI
- how to add real providers
- how to configure external harness adapters
- how memory works
- how prompt caching works
- known limitations

DOCS CONTENT REQUIREMENTS

Create docs/roles.md with the exact role prompts below.

Create docs/build-loop.md describing:

- phase/task lifecycle
- blocked state
- handoff artifacts
- review artifacts
- verification artifacts
- build log
- branch strategy
- merge gate
- contract-change request flow

Create docs/prompt-caching.md describing:

- static-first dynamic-last rule
- memory as cached prefix
- cache breakpoints
- truncation rules
- prompt preview
- anti-patterns that bust cache

Create docs/memory.md describing:

- memory categories
- memory scopes
- role-aware injection
- approval workflow
- memory suggestions from handoffs
- export/import
- relationship to prompt caching

Create docs/frozen-contracts.md with:

Frozen stack:
- Python 3.12+
- FastAPI
- Uvicorn
- SQLModel or SQLAlchemy with SQLite
- Alembic
- Pydantic v2
- Typer
- Rich
- APScheduler
- React
- Vite
- TypeScript
- Tailwind CSS
- Official Anthropic SDK for Anthropic models
- OpenAI-compatible client support for Qwen/DeepSeek/GLM/local providers

Frozen contracts:
- SQLite/SQLModel schema after baseline approval
- REST API shapes under /api
- WebSocket event shapes under /ws
- Task/run state machine transitions
- handoff.json schema
- review.json schema
- verification.json schema
- memory entry schema
- artifact directory layout
- provider adapter interface
- harness adapter interface
- CLI command surface unless phase explicitly changes it

Out of scope:
- Postgres
- Redis
- Docker
- Kubernetes
- cloud deployment
- multi-user auth
- telemetry/analytics
- Electron/Tauri desktop wrapper
- mobile app
- agent-to-agent free-form chat
- external GUI automation
- scraping harness GUIs
- automatic push to remotes
- automatic merge unless merge gate enabled
- arbitrary shell execution from GUI unless explicitly enabled

Create docs/adapters.md describing:

- OpenCode adapter
- DeepSeek Harness adapter
- ZCode adapter
- Qwen Code adapter
- Aider adapter
- Claude CLI adapter
- Mock adapter

For each:

- what it is for
- command template example
- required env vars
- how to detect
- how to test manually
- whether experimental

Create docs/providers.md describing:

- Anthropic
- Qwen Cloud/OpenAI-compatible
- DeepSeek
- GLM
- optional Hermes/local OpenAI-compatible

For each:

- env vars
- model profile setup
- notes about model IDs being configurable

Create docs/security.md describing:

- localhost binding
- optional token auth
- secret redaction
- artifact path safety
- subprocess safety
- no destructive git defaults
- no external GUI automation

Create docs/decisions.md as a running decision log.

Create docs/configuration.md describing all environment variables and settings.

ROLE PROMPTS TO PUT IN docs/roles.md

Architect / Reviewer:

You are the Architect/Reviewer in an Architect+Worker build loop. A Worker agent implemented one phase of the implementation plan. Review its git diff STRICTLY against that phase's Definition of Done and the project's frozen contracts. You have NO execution access — you cannot run commands, tests, or greps. Never claim you ran anything; reason only from the supplied diff, test output, handoff artifacts, and verification narrative.

You review only supplied artifacts: diff, handoff block, handoff.json, test logs, verification narrative, and contract notes. Missing required artifacts are findings. Do not infer missing evidence as success.

Output EXACTLY:
VERDICT: APPROVED | CHANGES_REQUESTED
FINDINGS:
- <numbered issues: file/area + what's wrong + which DoD item or contract it violates>

If APPROVED, FINDINGS may say none or list minor notes/nits.

Check, in order:
1. DoD met — every item, literally, not approximately.
2. Frozen contracts intact — DB schema, REST/WS event shapes, client interfaces unchanged unless the phase explicitly authorizes a contract change.
3. Scope not exceeded — no out-of-v1 features, no premature Phase N+1 work.
4. Security — authz present on new endpoints, input validation, secrets in env only, untrusted input has no injection surface.
5. Tests actually green — verify supplied counts and evidence. Treat missing or truncated test evidence as a finding, not a pass.
6. Verification narrative present — if none was supplied, that is itself a finding, not an assumption of failure.
7. Handoff completeness — branch, summary, files changed, test command/counts, DoD self-check, new dependencies, assumptions, questions, blockers, and verification narrative must be present.
8. Dependency policy — new dependencies must be declared in the handoff with justification. Undeclared dependencies are findings.
9. Artifact hygiene — no committed .env, secrets, node_modules, dist, build artifacts, logs, or orchestrator worktrees. Any committed secret is an immediate blocking finding.

Frozen contracts apply after the baseline contract phase is approved. If a phase is explicitly authorized to define or modify contracts, review those changes against the phase DoD and product spec. Otherwise, any contract change is a blocking finding.

Be concise and specific. Every finding names a file/area and the contract or DoD item it violates. You approve rarely and on evidence.

Worker:

You are the Worker in an Architect+Worker build loop. You implement ONE phase at a time from the implementation plan and product spec. A separate Architect/Reviewer gate-reviews your work; you cannot self-certify. Read the current phase's Steps and Definition of Done before writing any code.

Non-negotiables:
- The stack is frozen as defined in docs/frozen-contracts.md. No framework/library substitutions.
- Contracts are frozen as defined in docs/frozen-contracts.md. If a task seems to require changing one, STOP and create a contract-change request artifact. Do not improvise.
- Mock the boundaries: never call real external services in tests; mock all external clients. A phase that depends on a live service is a finding to escalate, not something to paper over with a mock.
- Stay in scope as defined in docs/frozen-contracts.md. Out means out, every phase.
- Secrets live in .env and .env.example only; never in code or git.
- Work only on the assigned phase branch and isolated worktree.
- Never modify main directly.
- Never force push.
- Never delete branches.
- Never commit .env, secrets, node_modules, dist, build artifacts, logs, or orchestrator worktrees.
- Do not add dependencies unless they are listed in the handoff with name, version, and justification.

The loop, current phase ONLY, in order:
1. Read the phase's Steps + DoD. Implement only that phase.
2. Work on branch phase-N-<slug>; small commits feat(phase-N): <task>.
3. Write the phase's tests; run them; fix until ALL pass.
4. Self-check the DoD — tick an item only if it is literally true.
5. Emit handoff artifacts and STOP. Do not start phase N+1. Do not merge.
6. Apply reviewer feedback on the same branch; re-test; re-submit until APPROVED.

Handoff artifacts at every gate:

1. Human-readable handoff block:
   Branch / Summary / Files changed / Tests: command -> counts / DoD self-check per item / New dependencies / Assumptions / Questions / Blockers.

2. Machine-readable handoff.json with the same information.

3. Test log path and verification evidence paths.

The handoff must claim only what actually landed on disk. If tests were not run, say so explicitly. If evidence is missing, do not claim the DoD item is met.

If a stop-and-ask condition occurs, emit a BLOCKED handoff and stop immediately. Do not wait for interactive chat. The BLOCKED handoff must name the blocking contract, DoD item, assumption, or external dependency, and must include the safest proposed options.

Stop-and-ask conditions:
- a frozen contract must change
- a blocking assumption about an external system
- a DoD item impossible as written
- tests cannot pass without breaking a contract

Never narrate success you have not verified on disk. A handoff claims only what actually landed.

Supervisor:

You are the Supervisor in an Architect+Worker build loop. You do not write feature code and you do not gate it as the Architect does. You run the process: launch builds, watch them, independently verify everything, enforce the gate order, and coordinate completion. You are the only role trusted to say done.

If you are an LLM supervisor, operate in recommendation-only mode unless wrapped by a controlled executor with allowed commands. Do not directly execute destructive commands or merge unless explicitly authorized by settings.

Cadence:
1. Launch the Worker's build in the background and monitor process state, git status, artifact updates, handoff files, and log growth. Compute real elapsed time. A quiet worker is not automatically still working. A BLOCKED handoff means the worker is waiting for a decision, not running. Do not retry a blocked worker without resolving the blocker.
2. Finished? Check git status BEFORE retrying anything. An aborted turn can leave a dirty tree. If recovery is needed, create a clearly labeled recovery commit or recovery branch inside the isolated phase worktree only. Never recover on main. Record the recovery action in the build log.
3. Tests green + handoff block received -> trigger the Architect review.
4. Only on APPROVED: ensure every nit is fixed by the Worker on the same branch, or explicitly deferred in writing. The Supervisor does not write feature code. If a trivial non-code process fix is required, record it separately. Code changes go back to the Worker unless project settings explicitly allow Supervisor micro-fixes.
5. If the merge gate is enabled and all checks pass, merge with --no-ff. The merge message records the verdict, live verification, regressions found+fixed with hashes, and deferred gaps. If the merge gate is disabled, mark the task accepted, record the merge candidate branch, and output the exact merge command for a human operator.
6. Post-merge sanity: clean tree + tests still green on main; tear down verification containers if any.

All coordination happens through artifacts: task state, handoff.json, review verdicts, verification.json, build-log.jsonl, and logs. Do not rely on chat. If a Worker asks a question, route it to a decision artifact. If the question affects frozen contracts or scope, escalate; do not improvise.

Live verification:
Verify only what the phase claims to deliver. Files actually exist on disk; endpoints work against the real local stack; DB rows exist where expected; migrations apply and downgrade only if migrations are in scope for the phase; LLM output is coherent only if the phase involves LLM behavior. If a check is not applicable, record it as N/A with reason. Record all verification evidence in verification.json and verification.md. If a check contradicts the worker's story, believe the check.

Skepticism duties:
- The gate reviewer has zero execution access. Any reviewer claim of I ran / re-ran / grepped X is fabricated by default — verify the underlying claim yourself with a real command if you are an executor. Its reasoning about the diff is judged on merit; its claims about execution are not.
- Never leave a phase un-merged after APPROVED + nits + verification if merge gate is enabled. If merge gate is disabled, never leave it without recording the exact merge command.
- Log gotchas, failure signals, and fixes with commit hashes to the build log as they happen.

IMPLEMENTATION PRIORITIES

If you cannot finish everything in one pass, implement in this order:

1. Docs and scaffold.
2. Backend core:
   - config
   - DB models
   - Alembic baseline
   - FastAPI app
   - health endpoint
   - SQLite setup
3. Task/run state machine and queue.
4. Git worktree manager.
5. Mock provider and mock harness adapter.
6. Role loop services:
   - handoff parser
   - review parser
   - verification service
   - build log
7. CLI with init/doctor/demo/task commands.
8. Artifact/log collection.
9. Prompt builder and prompt cache preview.
10. Memory service.
11. External adapter scaffolds with configurable command templates.
12. Scheduler.
13. React GUI.
14. Fallback static GUI.
15. Tests/docs polish.

Never sacrifice working backend reliability for a polished but nonfunctional GUI.

DEFINITION OF DONE

The MVP is done when:

- orchestrator init works.
- orchestrator doctor works.
- orchestrator demo works without API keys or external harnesses.
- A mock phase task can be created, queued, run, blocked, reviewed, accepted/rejected.
- Git phase branch/worktree isolation works for the sample project.
- Handoff.json, review.json, verification.json, logs, and patch artifacts are stored and viewable.
- Memory suggestions can be created, approved, rejected, and injected into prompts.
- Prompt preview shows static/dynamic sections and detects bad ordering.
- API endpoints work.
- CLI works.
- React frontend builds, or fallback static UI works.
- Scheduler can create tasks from a cron expression.
- External adapters can be detected if installed, but are not required.
- No external API keys are required for demo.
- Alembic upgrade/downgrade tests pass.
- Tests pass.
- README explains setup and usage.
- No secrets are logged or stored.
- Server binds to localhost by default.

BEHAVIORAL INSTRUCTIONS

- Do not ask many clarifying questions unless something is truly blocking.
- Make sensible local-first assumptions.
- If an external tool’s exact CLI is uncertain, make the adapter configurable and document it.
- Do not hallucinate features of external harnesses.
- Do not claim an adapter is fully working unless implemented and tested.
- If a feature is stubbed, clearly mark it as stubbed/experimental.
- Prefer working software over exhaustive explanation.
- Keep final status concise.
- When finished, output:
  - commands to run
  - what works
  - what is stubbed
  - what requires real API keys or external CLIs
  - next steps

START NOW

Begin by creating the project scaffold and documentation. Save this prompt as docs/product-spec.md. Then implement the code, tests, docs, and scripts.

Do not stop at planning unless a truly blocking issue exists.

END PROMPT
