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
- Persistent, role-aware shared memory with an approval workflow, per-section token
  budgets (memory is static, so it otherwise squeezes the evidence), and read-only
  `orchestrator memory triage` / `memory budget` reporting for rot, staleness and merges
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

### Desktop notifications (off by default)

A finished run is only useful if someone comes back to accept it, request changes or resolve a
block, so the harness can notify you: a freedesktop D-Bus notification (`notify-send`, or a direct
`gdbus` call) that works under Hyprland/Wayland with no browser or desktop window open. It carries
the task and run ids, the outcome, the next action and a link to the task. Delivery is best-effort;
every decision is also kept in the **Notifications** page and `GET /api/notifications`, with
"not attempted because notifications are off" (`skipped`) separate from "attempted and failed"
(`failed`). The master switch `notifications_enabled` is **off on a new install** and every trigger
is individually disable-able in Settings. A record never claims more than happened: the daemon is
asked for its capabilities before anything is rendered as markup, a message that was displayed is
recorded `delivered` (with `reason=shown_unacknowledged` when nothing acknowledged it) rather than
`failed`, and no default action is advertised until an `ActionInvoked` listener exists. See
[`docs/build-loop.md`](docs/build-loop.md#terminal-run-events) and
[`docs/configuration.md`](docs/configuration.md#notification-delivery-and-status-t18).

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
# commands run with cwd = the task worktree, which has no .venv: use a path-independent
# wrapper (scripts/orch_verify.sh in this repo is the pattern), not a bare venv-relative one
orchestrator project add myapp ~/code/myapp \
    --test-command "scripts/orch_verify.sh test" --lint-command "scripts/orch_verify.sh lint"
orchestrator project doctor myapp                 # static checks only
orchestrator project doctor myapp --run-commands  # opt-in: run them in a throwaway worktree and
                                                  # check they exit 0 AND resolve inside it
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
Build Log, Notifications, and Settings. It has light and dark themes (switch in the sidebar or on
Settings) and a choice of text and code fonts (Inter, IBM Plex, Fira Code, JetBrains Mono,
Roboto Mono or your system's), all bundled locally. Code, diffs and JSON are highlighted with
VS Code themes (pick a light and a dark one on Settings), with line numbers and a copy
button.

## Runtime settings

Settings are editable in the GUI or with `PATCH /api/settings`, stored in SQLite, and read
through `settings_service`. The two loop limits are:

| Key | Default | Meaning |
|---|---|---|
| `max_review_rounds` | `3` | **Verified rounds** before `blocked` (runs that reached the gate) |
| `max_run_attempts_per_task` | `6` | **All attempts** before `blocked`, including timeout/failed/cancelled runs |

The default 6 is above the default 3 review rounds, so a completed-only flow behaves exactly as
before while a timeout loop stops after six attempts. The `task_limits_exhausted` build-log event
names the fired limit and the completed/timeout/failed/cancelled breakdown. All other runtime
keys are listed in [`docs/configuration.md`](docs/configuration.md).

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
*suggest* entries via `memory_suggestions` in their JSON. A suggestion's scope is checked
against the closed vocabulary (`environment|preferences|patterns`,
`conventions|decisions|gotchas`, `architect|worker|supervisor`): a project/global scope
outside it is coerced to the deterministic fallback (`gotchas`/`environment`, never
inferred from the words) and the substitution is written to the build log, a role
suggestion that resolves to no role file is dropped with a warning, and
`orchestrator memory list` / `memory fix-scopes` mark and repair rows stored before that
check existed. The create and edit boundaries (POST/PATCH, `memory add`) *reject* an
out-of-vocabulary value for every category, and for `category=role` they check the `role`
column too, because that is the column injection reads. Every one of those rejections names
its own allowed values (`environment|preferences|patterns`, `conventions|decisions|gotchas`,
`architect|worker|supervisor`), including the API's `422` for an unknown role name. Suggestions start **pending**,
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
make test      # scripts/orch_verify.sh test — the offline pytest suite (external CLI ones skip)
make lint      # scripts/orch_verify.sh lint — ruff check
make dev       # backend --reload on :8321 + Vite on :5173
```

`make test`/`make lint` go through `scripts/orch_verify.sh`, which works in the main
checkout **and** in any `git worktree` of it: it takes the interpreter from the main repo's
`.venv` and puts the *current checkout's* `src` first on `PYTHONPATH`, then prints the
resolved module path and refuses (exit 3) if the import resolves outside the checkout it is
running in. Plain `.venv/bin/pytest …` only works in the main checkout — a worktree has no
`.venv` (`git worktree add` copies tracked files only), and an interpreter whose editable
`.pth` points at the main checkout would import the main checkout, not the branch: a green
run that verified the wrong code. `make setup` must have run in the main checkout; nothing
is installed per worktree and no network is used.

Layout: `src/orchestrator/{core,services,adapters,providers,prompts,api,migrations}`,
`web/` (React), `tests/{unit,integration}`, `docs/`.

## Known limitations

- **Desktop notifications: one channel, and the link is text on the transport this machine uses.**
  The only channel exercised end-to-end is the freedesktop D-Bus one, on CachyOS/Hyprland with
  quickshell's notification server (`notify-send`, `gdbus` as fallback). macOS and Windows
  notification backends are out of scope and do not exist; nothing else was tested. Nothing
  listens for `ActionInvoked`, so notifications carry no clickable action. A clickable `<a href>` is
  only sent
  through the `gdbus` transport and only when the daemon advertises both `body-markup` and
  `body-hyperlinks`; `notify-send` always gets a plain-text body with the task URL as a copyable
  `Open: <url>` line, because it attaches no `body-markup` hint. A daemon that cannot be probed
  (no `gdbus`, a failed call) is treated as markup-less rather than assumed capable.
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
- **Review-input completeness is not code correctness.** Diff completeness proves only that a
  changed file has at least one hunk in the text the reviewer was actually sent (T-16); it cannot
  prove the hunk is complete or that a claim is true. Sensitive files are deliberately withheld
  (`# [sensitive file omitted]`), and the archived `patch.diff` remains the full record. Redaction
  rows in `review_payload.md` name the pattern class and count, never the secret or the
  substituted value. The compensation-artifact reserve can still be too small for an oversized
  evidence bundle; when that happens the manifest (computed from the final as-sent diff) and a
  harness finding say so instead of the reviewer being told the evidence is missing, and neither
  asks the Worker for an evidence-compression round. Attribute attribution needs Git 2.40 for
  `check-attr --source`, probed by running it rather than only reading the version; older or
  partially working Git falls back to plain `check-attr -a` and fails closed on an attribute it
  cannot date, and an empty merge-base says so rather than claiming an old Git. The handoff
  channel is capped by `ORCHESTRATOR_MAX_LOG_TOKENS` (default 8000; 4000 was measured to truncate
  a normal handoff every round), and a handoff cut is named in the manifest. The expensive
  environment-parity tests are opt-in
  (`ORCH_ENV_PARITY_TESTS=1`; nested-suite cases also need `ORCH_ENV_SPAWN_TESTS=1`).
- **A gate green is a green in a clean environment, not in your checkout.** Gated runs execute in
  a tracked-files-only worktree: no `.env`, no `node_modules`, no `.venv`. The suite itself is
  hermetic against a repo-level `.env`, and `verification.json` records which world the gate used;
  `make test-parity` runs the suite again with a **synthetic** `.env` derived from your local
  variable names (never your values), as a developer check and never as the gate command. A parity
  run killed with SIGKILL can leave that synthetic throwaway tree behind; `orchestrator cleanup`
  reclaims it, and never touches one a live parity run holds. Residue risk is swept, not assumed
  absent: an unmarked shell root is only reclaimed after a 60-second grace window, and the doctor
  parity lock lives under `<ORCHESTRATOR_HOME>/locks/`. See `docs/build-loop.md`,
  "Gated runs are environment-free".
- Single-user and local only, by design.
