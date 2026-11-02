# Configuration

Settings come from environment variables, or from `.env` in the directory where you run
`orchestrator`. Real environment variables always win over `.env`. `ORCHESTRATOR_*`
variables configure the orchestrator itself. Provider and harness variables are read by
the providers/adapters when they run.

## Orchestrator settings

| Variable | Default | Meaning |
|---|---|---|
| `ORCHESTRATOR_HOME` | `${XDG_DATA_HOME:-$HOME/.local/share}/coding-orchestrator` | Root for DB, runs, logs, memory, worktrees |
| `ORCHESTRATOR_DB_URL` | `sqlite:///$HOME_DIR/orchestrator.db` | SQLite URL (other databases are out of scope) |
| `ORCHESTRATOR_BIND_HOST` | `127.0.0.1` | Server bind host. Non-local hosts require a token, and `serve` refuses to start without one |
| `ORCHESTRATOR_BIND_PORT` | `8321` | Server port |
| `ORCHESTRATOR_TOKEN` | unset | Enables `X-Orchestrator-Token` auth for `/api/*` (except `/api/health`) and `?token=` for WebSockets |
| `ORCHESTRATOR_LOG_LEVEL` | `INFO` | Python log level (logs are redacted) |
| `ORCHESTRATOR_MAX_PARALLEL_RUNS` | `2` | Global concurrent runs (queue worker) |
| `ORCHESTRATOR_MAX_PARALLEL_RUNS_PER_PROJECT` | `1` | Concurrent runs per project |
| `ORCHESTRATOR_ENABLE_SCHEDULER` | `true` | Start the APScheduler cron runner in `serve` |
| `ORCHESTRATOR_ENABLE_WORKER` | `true` | Start the background queue worker in `serve` |
| `ORCHESTRATOR_WORKTREE_ROOT` | `$HOME/worktrees` | Managed worktrees |
| `ORCHESTRATOR_ARTIFACT_ROOT` | `$HOME/runs` | Run artifacts (`runs/<run_id>/…`) |
| `ORCHESTRATOR_LOG_ROOT` | `$HOME/logs` | Reserved for server logs |
| `ORCHESTRATOR_MEMORY_ROOT` | `$HOME/memory` | Memory markdown tree |
| `ORCHESTRATOR_RUN_RETENTION_DAYS` | `30` | Retention target for run artifacts (informational; artifacts are never deleted automatically) |
| `ORCHESTRATOR_GIT_USER_NAME` / `_EMAIL` | `coding-orchestrator` / `orchestrator@localhost` | Author identity for orchestrator and child-process commits |
| `ORCHESTRATOR_DEFAULT_BASE_BRANCH` | `main` | Used when a project's base branch can't be detected |
| `ORCHESTRATOR_FRONTEND_DIST` | auto (`web/dist`) | Path to a built GUI. Falls back to the static UI |
| `ORCHESTRATOR_AUTOSTART_BROWSER` | `false` | Open the GUI when `serve` starts |
| `ORCHESTRATOR_MERGE_ENABLED` | `false` | Merge gate |
| `ORCHESTRATOR_AUTO_MERGE_ENABLED` | `false` | Auto-merge after APPROVED + verification (needs merge gate) |
| `ORCHESTRATOR_SUPERVISOR_MICROFIXES_ENABLED` | `false` | Policy flag; no micro-fix executor exists |
| `ORCHESTRATOR_ALLOW_GUI_COMMANDS` | `false` | Allow running a project's configured test/lint/build from the GUI |
| `ORCHESTRATOR_MAX_PROMPT_CONTEXT_TOKENS` | `60000` | Prompt budget (~4 chars/token) |
| `ORCHESTRATOR_MAX_LOG_TOKENS` | `4000` | Per-log budget in prompts |
| `ORCHESTRATOR_MAX_DIFF_TOKENS` | `12000` | Diff budget in prompts |
| `ORCHESTRATOR_ANTHROPIC_REFUSAL_FALLBACK` | `true` | Anthropic server-side refusal fallback on Claude 5.x |

## Runtime settings (GUI / `PATCH /api/settings`)

Stored in the `settings` table; they override the env defaults: `merge_enabled`,
`auto_merge_enabled`, `supervisor_microfixes_enabled`, `max_parallel_runs`,
`max_parallel_runs_per_project`, `scheduler_enabled`, `worker_enabled`,
`allow_gui_commands`, `allow_interactive_sessions`, `allow_unattended_sessions`,
`max_review_rounds` (default 3), and `theme` (dark).

The React GUI doesn't read the stored `theme` setting. Theme is a per-browser choice
(Light / Dark / System, default System) made in the sidebar or on Settings → Appearance,
and saved in the browser's `localStorage` under `orchestrator.theme`. The fallback UI is
dark only.

## Provider variables

`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `QWEN_OPENAI_BASE_URL`, `QWEN_API_KEY`,
`DEEPSEEK_BASE_URL`, `DEEPSEEK_API_KEY`, `GLM_OPENAI_BASE_URL`, `GLM_API_KEY`,
`HERMES_OPENAI_BASE_URL`, `HERMES_API_KEY`. See `providers.md`.

## Claude CLI variables

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_CLI_COMMAND` | `claude` | Path to the Claude Code binary (Architect provider and Worker adapter seed) |
| `CLAUDE_CLI_EXTRA_ARGS` | empty | Extra flags appended to Architect `claude -p` calls, e.g. `--safe-mode` |
| `CLAUDE_CODE_OAUTH_TOKEN` | unset | Optional long-lived subscription token from `claude setup-token` (headless machines) |
| `CLAUDE_CONFIG_DIR` | unset | Alternate Claude Code config directory |

## Adapter variables

`OPENCODE_COMMAND`, `DEEPSEEK_HARNESS_COMMAND`, `ZCODE_COMMAND`, `QWEN_CODE_COMMAND`,
`AIDER_COMMAND`, `CLAUDE_CLI_COMMAND` override the binary name when adapters are first
seeded. After that, edit the adapter row directly. See `adapters.md`.

## Projects

`orchestrator project add NAME PATH [--base-branch] [--test-command] [--lint-command]
[--build-command] [--allow CMD]...`. Commands are split with `shlex` and run **without a
shell**, so pipes, `&&`, redirects and `$(…)` are rejected. Put complex steps in a script
and reference that.
