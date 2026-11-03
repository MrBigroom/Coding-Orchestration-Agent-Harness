# coding-orchestrator

A local-first control plane for an **Architect → Supervisor → Worker** coding workflow.
It drives headless coding CLIs (OpenCode, DeepSeek Harness, Qwen Code, ZCode, Aider,
Claude Code) and model APIs (Anthropic, Qwen/Model Studio, DeepSeek, GLM, local
OpenAI-compatible servers). It coordinates them through **artifacts and a state machine,
not chat**: phase branches in isolated git worktrees, `handoff.json`, `review.json`,
`verification.json`, a JSONL build log, and gated merges.

- FastAPI + SQLite + Alembic backend, Typer CLI, React/Vite/Tailwind GUI (plus a
  dependency-free fallback UI)
- Mock provider and mock harness, so the whole loop runs with **no API keys and no network**
- Prompt construction ordered for prompt caching, plus a cache preview (CLI and GUI)
- Persistent, role-aware shared memory with an approval workflow
- Binds to `127.0.0.1`, no telemetry, no Docker, Redis, or Postgres

The full product spec is in [`docs/product-spec.md`](docs/product-spec.md).

## Quick start (CachyOS / any Linux)

Requirements: Python ≥ 3.12, git. Optional: Node ≥ 20 + npm (for the React GUI) and
[`uv`](https://github.com/astral-sh/uv) (faster installs).

```bash
make setup     # venv + deps (+ npm), creates .env, ORCHESTRATOR_HOME, alembic upgrade head
make demo      # full offline role loop on examples/sample-project
make build     # build the React GUI (optional; the fallback UI works without it)
make start     # http://127.0.0.1:8321  (API docs at /api/docs)
```

Without `make`: `./scripts/setup.sh`, `.venv/bin/orchestrator demo`,
`.venv/bin/orchestrator serve`. `just` users get the same targets via the `justfile`.

### Desktop app

```bash
make desktop-install   # pywebview (GTK) + GUI build + "Coding Orchestrator" app-menu launcher
make desktop           # or open it from your app launcher
```

The app opens the GUI in a native window. It attaches to an orchestrator that is already
running, or starts one in the background. When you close it with tasks still running, it
asks whether to keep the server running. See [`docs/desktop.md`](docs/desktop.md).

### What the demo does

`orchestrator demo` initialises `ORCHESTRATOR_HOME`. It copies the sample project there as
a fresh git repo and registers it. It then creates a mock phase task and runs the
**mock harness** in an isolated worktree on branch `phase-N-demo-N`. Next it runs the
project's tests as the **Supervisor's independent verification**, gets an **Architect
review** from the mock provider, and accepts the task. The merge gate is off, so it
prints the exact `git merge --no-ff` command instead of merging. Finally it approves
one memory suggestion from the handoff.

Other mock scenarios use tags: `--tag mock-reject` (changes requested → requeued),
`mock-blocked` (BLOCKED handoff), `mock-fail` (non-zero exit), `mock-timeout`,
`mock-ambiguous` (unparseable review → fails closed).

## Everyday use

```bash
orchestrator project add myapp ~/code/myapp --test-command "pytest -q" --lint-command "ruff check ."
orchestrator project doctor myapp
orchestrator task create "Add health endpoint" --project myapp --phase 1 --slug health \
    --dod "GET /health returns 200" --dod "test added and green" --tag qwen
orchestrator task dry-run T-1         # which harness / model / command would run
orchestrator task run T-1             # worker → verification → review → gate
orchestrator task show T-1
orchestrator prompt-preview --role architect --task T-1
orchestrator memory list --status pending
orchestrator agent add deps --project myapp --model worker-deepseek-flash --instructions "…"
orchestrator agent run deps "Update the patch-level dependencies"
```

**Agents** are saved instructions + model (+ project/harness). Each run becomes a task
that goes through the harness routed for the agent's model. With a project it gets the
full worktree → tests → Architect review gate. **Scheduled tasks** can run an agent on a
cron or at a set time, through that pipeline or as a standalone session that saves its own
report. See [`docs/agents.md`](docs/agents.md).

The GUI covers the same workflows. Its pages are Dashboard, Usage (tokens per model per
day, as a heatmap and stacked bars), Chat, Tasks, Task Detail (live logs, diff, handoff,
review, verification, accept/reject/escalate, retry with a different harness/model),
Agents, Scheduled Tasks, Projects, Models & Providers, Adapters, Memory, Prompt Cache,
Build Log, and Settings. It has light and dark themes (switch in the sidebar or on
Settings), with fonts bundled locally.

## Chat with a harness directly

The **Chat** page is an interactive session with a coding harness: an input box, model
picker, effort slider, permission modes, approval prompts, and a Stop button. It works with
Pi, Qwen Code, OpenCode, DeepSeek Harness and Claude Code. It's off until you turn it on,
because sessions run agents with shell access and skip the review gate. See
[docs/chat.md](docs/chat.md).

## Adding real providers

Put keys in `.env` (copied from `.env.example`). The DB stores only the **name** of each
key's env var, and the GUI shows whether a key is set, never its value.

| Provider | Env vars | Default profiles |
|---|---|---|
| **Claude subscription via `claude -p`** (default Architect) | none: install Claude Code and run `claude auth login` with your Pro/Max account | `architect-claude-cli` → `claude-opus-5-5` (tools disabled, uses your plan limits) |
| Anthropic API (optional) | `ANTHROPIC_API_KEY` (`ANTHROPIC_BASE_URL` optional) | `architect-opus` (fallback if the CLI profile is disabled), `supervisor-sonnet` → `claude-sonnet-5-5` |
| Alibaba Model Studio (Token Plan default) | `QWEN_API_KEY`, `QWEN_OPENAI_BASE_URL` | `supervisor-qwen-max` → `qwen3.8-max` (primary supervisor), `worker-qwen-flash`, `worker-deepseek-flash` → `deepseek-v4.1-flash`, `worker-glm` → `glm-5.3`, `agent-flash` |
| DeepSeek direct (disabled) | `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` | point a profile at model `deepseek-flash` |
| Z.ai GLM direct (disabled) | `GLM_API_KEY`, `GLM_OPENAI_BASE_URL` | point a profile at your GLM model id |
| Hermes / local (disabled) | `HERMES_OPENAI_BASE_URL`, `HERMES_API_KEY` | `hermes-local`; only used when explicitly selected |

> **Claude subscription.** Anthropic allows `claude -p` on Pro/Max subscription limits, but
> reserves subscription sign-in for its own apps. So the orchestrator runs the unmodified
> `claude` binary and never touches your OAuth token or uses it for API calls.
> `ANTHROPIC_API_KEY` is never passed to `claude`, so you can't be billed through the API
> by accident. Reviews draw on your Pro usage limits. When a limit is hit, the review
> comes back `NEEDS_HUMAN` and the task blocks instead of failing silently.

> **Token Plan terms (confirmed by Alibaba Cloud support, 2026-10-19).** Tasks you trigger
> manually, which then run Qwen Code headlessly (`qwen -p`), are allowed. So are direct
> calls through the standard API, and the plan's DeepSeek/GLM models. Still not allowed:
> high-frequency automated scripts, sharing the account among several people, and using
> outputs to train third-party models. Keep your key out of public repos (`.env` is
> gitignored). In practice, use the Token Plan for tasks you start yourself, and avoid
> frequent cron schedules on it. Details: [`docs/providers.md`](docs/providers.md).

Model IDs are editable placeholders (Models & Providers page, or `/api/model-profiles`).
See [`docs/providers.md`](docs/providers.md).

## Configuring external harness adapters

Each adapter is a **command + argv template stored in the DB** and editable in the GUI
(Adapters page) or with `orchestrator adapters set NAME --args '[...]'`. Defaults:

| Adapter | Command | Default args | Status |
|---|---|---|---|
| Pi | `pi` | `--mode json --no-session --provider orchestrator --model {model} "…"` (prompt on stdin) | experimental, mock-API-checked |
| OpenCode | `opencode` | `run --format json ?--model={model} {prompt}` | experimental, mock-API-checked |
| DeepSeek Harness | `dsh` | `--profile headless --json -` (prompt on stdin) | experimental, mock-API-checked |
| ZCode | `zcode` | `--cwd {cwd} --mode yolo --json --prompt {prompt}` | experimental, mock-API-checked; bundled CLI fallback |
| Qwen Code | `qwen` | `--auth-type openai --approval-mode auto-edit --allowed-tools run_shell_command … -o json -p {prompt}` | experimental, mock-API-checked |
| Aider | `aider` | `--yes-always ?--model={model} --message-file {prompt_file}` | experimental |
| Claude Code (Worker) | `claude` | `-p {prompt} --output-format json --permission-mode acceptEdits ?--model={model} --allowedTools Bash` | experimental, disabled by default (subscription auth) |
| Mock | python | built-in | tested |

`orchestrator adapters detect` shows which binaries are on `PATH`. Manual verification
steps for each adapter: [`docs/adapters.md`](docs/adapters.md).

Worker runs are routed to the harness that suits the worker model, and the most
token-efficient one comes first (Pi for Qwen, DeepSeek, GLM and Kimi, then each model's
own harness or OpenCode). Each run's real token usage is recorded, and routing switches once
measured data shows a cheaper harness per accepted task. See
[`docs/harness-routing.md`](docs/harness-routing.md). To measure harness overhead
yourself, run `scripts/harness-bench/bench.sh`.

## How memory works

Memory lives in SQLite (`memory_entries`) and is rendered to markdown under
`$ORCHESTRATOR_HOME/memory/{global,projects/<slug>,roles}/`. Workers and Architects
*suggest* entries via `memory_suggestions` in their JSON. Suggestions start **pending**,
and only **approved** entries are injected into prompts. Each role gets its own slice:
the Architect gets decisions + conventions, the Worker conventions + gotchas, the
Supervisor decisions + gotchas, and every role gets global memory. See
[`docs/memory.md`](docs/memory.md).

## How prompt caching works

Every prompt is built in a fixed order: role prompt → global memory → role memory →
project memory → frozen contracts → phase/DoD (all **static**, the cached prefix) →
history → current diff/test summary/handoff/feedback (**dynamic**, always last).
Anthropic requests place `cache_control` breakpoints on the static `system` blocks.
OpenAI-compatible requests keep one stable `system` message so the provider's prefix
cache can hit. `orchestrator prompt-preview` warns when dynamic content comes before
static content, or when the static part contains volatile values such as timestamps.
See [`docs/prompt-caching.md`](docs/prompt-caching.md).

## Safety defaults

Merging, auto-merging, Supervisor micro-fixes, and running commands from the GUI are all
**off**. The orchestrator never pushes, force-pushes, or deletes branches, and it only
cleans up worktrees it created (a marker file is checked). Child processes get a minimal
environment and never run through a shell. Secrets are redacted from logs, artifacts,
and prompts, and `.env` and key files are never staged. See
[`docs/security.md`](docs/security.md).

## Development

```bash
make test      # pytest: 120+ offline tests (external CLI checks are opt-in)
make lint      # ruff check
make dev       # backend --reload on :8321 + Vite on :5173
```

Layout: `src/orchestrator/{core,services,adapters,providers,prompts,api,migrations}`,
`web/` (React), `tests/{unit,integration}`, `docs/`.

## Known limitations

- **Real harness adapters have had only small real-model checks.** Pi, DeepSeek Harness,
  Qwen Code and ZCode ran two fixture tasks on the Token Plan. OpenCode has only run
  against a local mock API. Aider and Claude Code templates come from public docs. Verify
  each one with the steps in `docs/adapters.md` before relying on it.
- **Real providers are untested here** (no keys in CI). Request shapes are unit-tested
  without network calls.
- The LLM Supervisor is **recommendation-only**. All gate decisions come from the
  deterministic Supervisor (`supervisor_service`), which runs only the project's
  configured test/lint/build commands.
- "Endpoints against the live stack" and migration checks are recorded as N/A unless your
  project's test command covers them. There is no generic live-endpoint prober yet.
- The Supervisor micro-fix setting is stored but there is no micro-fix executor. Code
  changes always go back to the Worker.
- Token counts are estimates (~4 characters per token), not tokenizer-exact.
- Single-user and local only, by design.
