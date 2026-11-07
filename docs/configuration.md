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
| `ORCHESTRATOR_MAX_LOG_TOKENS` | `8000` | Per-log/handoff budget in prompts (4000 was measured to truncate a normal handoff every round; still a hard cap, and any cut is named in the review payload manifest) |
| `ORCHESTRATOR_MAX_DIFF_TOKENS` | `12000` | Diff budget in prompts |
| `ORCHESTRATOR_MAX_MEMORY_TOKENS_GLOBAL` | `2000` | Cap on the injected global memory section |
| `ORCHESTRATOR_MAX_MEMORY_TOKENS_ROLE` | `800` | Cap on the injected role memory section |
| `ORCHESTRATOR_MAX_MEMORY_TOKENS_PROJECT` | `4000` | Cap on the injected project memory section |
| `ORCHESTRATOR_MEMORY_BUDGET_ENABLED` | `true` | When false, every approved entry is injected |
| `ORCHESTRATOR_MEMORY_BUDGET_WARN_FRACTION` | `0.8` | Warn once a memory section reaches this share of its cap |
| `ORCHESTRATOR_ANTHROPIC_REFUSAL_FALLBACK` | `true` | Anthropic server-side refusal fallback on Claude 5.x |

## Runtime settings (GUI / `PATCH /api/settings`)

Stored in the `settings` table; they override the env defaults: `merge_enabled`,
`auto_merge_enabled`, `supervisor_microfixes_enabled`, `max_parallel_runs`,
`max_parallel_runs_per_project`, `scheduler_enabled`, `worker_enabled`,
`allow_gui_commands`, `allow_interactive_sessions`, `allow_unattended_sessions`,
`max_review_rounds` (default 3), `max_run_attempts_per_task` (default 6), `theme` (dark), and
the T-9 notification keys: `notifications_enabled` (master, default **false**) plus one
`notification_<trigger>` boolean per trigger. Human-must-act triggers (`run_completed`,
`run_failed`, `run_timeout`, `run_cancelled`, `standalone_run`, `task_review`, `task_blocked`,
`escalated`, `merge_candidate`, `merged`, `merge_refused`) default subscribed; the noisy
machinery triggers
(`worktree_created`, `recovery_commit`, `supervisor_verification`, `architect_review`,
`nits_deferred`, `schedule_fired`) default silent. A channel that is off is recorded as `skipped`
in the persistent list instead of attempting delivery.

`max_review_rounds` counts **verified rounds**: runs that reached the gate (including the
`testing` run being gated). `max_run_attempts_per_task` counts **every adapter attempt**,
including `timeout`, `failed` and `cancelled` runs. The default 6 is above the default 3, so a
completed-only flow blocks on the review-round limit as before while a timeout loop cannot consume
unbounded adapter windows. When either fires, the task goes to `blocked`, `task_limits_exhausted`
records which limit and the breakdown, and the blocked reason names both.

The React GUI doesn't read the stored `theme` setting. Theme is a per-browser choice
(Light / Dark / System, default System) made in the sidebar or on Settings → Appearance,
and saved in the browser's `localStorage` under `orchestrator.theme`. The fallback UI is
dark only.

## Notification delivery and status (T-18)

`notifications_enabled` is the master switch and is **off on a new install**; each trigger also has
its own `notification_<trigger>` switch (human-must-act triggers default on, noisy machinery
triggers default off). With the master off nothing is attempted: the row is written `skipped` with
`reason=master_disabled`, and a per-trigger switch records `reason=trigger_disabled`. A channel that
is off is therefore never confused with one that was tried.

Every row carries one of the frozen CCR-T9-01 statuses. The service classifies each transport
result into one explicit **outcome class** first (`notification_service.OUTCOME_CLASSES`) and then
maps that class to a status; `details_json.outcome_class` always names the class, and `reason`
names the finer distinction the class carries.

| Outcome class | Status | When | `reason` |
|---|---|---|---|
| `delivered` | `delivered` | the `gdbus` `Notify` call returned a notification id, so the daemon acknowledged ownership of the message | *(empty)* |
| `shown_unacknowledged` | `delivered` | `notify-send` exited 0: the message was handed over and shown, but nothing acknowledged a dismissal or an action | `shown_unacknowledged` |
| `failed` | `failed` | the client exited non-zero, timed out, was missing, or raised: delivery was attempted and did not succeed | `delivery_failed` |
| `skipped` | `skipped` | the master switch or the trigger is off, so nothing was attempted | `master_disabled` / `trigger_disabled` |
| `coalesced` | `coalesced` | folded into one earlier popup of the same batch; `coalesced_into` points at it | `coalesced_into:<id>` |

The status vocabulary is frozen, so `shown-unacknowledged` is not a sixth status: a shown message
is recorded `delivered` with the `shown_unacknowledged` reason and outcome class, never `failed`.
Delivery is best-effort and nothing waits for an acknowledgement: the code passes **no action** to
any client. `notify-send --action` implies `--wait`, which blocks until the popup is dismissed
(under mako's defaults: never) and made a message the operator saw time out and be recorded
`failed`. Nothing listens for the freedesktop `ActionInvoked` signal either, so a default action
was advertised without being handled and is deliberately gone until such a listener exists.

**Capability requirements.** Before sending anything the service probes the daemon with
`org.freedesktop.Notifications.GetCapabilities` (`probe_capabilities()`):

- A body is rendered as markup with a clickable `<a href>` only on the `gdbus` transport and only
  when the daemon advertises **both** `body-markup` and `body-hyperlinks`; the call then carries
  `body-markup: true`. Everything is HTML-escaped before it is sent.
- `notify-send` always receives the plain-text body, because that client attaches no
  `body-markup` hint today: an anchor sent through it would be displayed as a literal tag.
- A daemon that does not advertise `body-hyperlinks` (mako, dunst, most tiling-WM notifiers), a
  daemon that cannot be probed (no `gdbus`, a failed or timed-out call), and every `notify-send`
  delivery all receive a **plain-text body**: the task URL stays as a readable `Open: <url>` line
  that can be copied, and nothing pretends it is clickable.
- Nothing assumes a capability: `probed=False` always means plain text.

On the development machine (CachyOS/Hyprland) the daemon is quickshell's notification server
(`qs -c caelestia`); its `GetCapabilities` returns
`persistence, body, body-markup, body-hyperlinks, body-images, actions, icon-static`, which is a
freedesktop-compliant set. It is still reached through `notify-send` first, so the link arrives as
text there; the `gdbus` path is the one that upgrades to an anchor.

**Pending rows are flushed, never stranded.** A row is written `pending` only when delivery is
enabled, and every one of these points delivers or terminally resolves it:

- the coalescing timer: one global window (`COALESCE_SECONDS`, 2s) that is **debounced** by each
  new row, so a batch of parallel finishes is one popup even when a later run finishes after the
  first timer was armed, and capped (`COALESCE_MAX_SECONDS`, 10s) so a steady stream cannot
  postpone delivery forever;
- API startup (`serve` lifespan) for rows a dead CLI process left behind;
- the read path: `GET /api/notifications` flushes pending rows before answering, so a long-running
  server cannot leave rows pending indefinitely;
- CLI exit: `orchestrator.cli.main()` flushes on the way out and writes
  `failed` / `reason=process_exit_undelivered` for anything it still could not deliver. It is
  inert when the database has no `notifications` table yet, so `orchestrator --help` in a fresh
  home still creates nothing.

Coalescing is global, not per run: one message can contain rows from several runs and several
triggers (the title says how many updates it carries) while `dedupe_key` still allows at most one
row per run per trigger.

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

**The commands must be path-independent.** The Supervisor runs them with cwd = the task
worktree (`ORCHESTRATOR_HOME/worktrees/<project>/<task_id>`), and a worktree contains
tracked files only: no `.venv`, no `node_modules`. An editable install in the main
checkout's venv makes `import <package>` resolve to the **main checkout**, so a venv-relative
command either exits 127 or — worse — goes green against code that is not the branch under
review. Use the wrapper pattern in this repo, `scripts/orch_verify.sh`: resolve the worktree
(`git rev-parse --show-toplevel`) and the main repo (`git rev-parse
--path-format=absolute --git-common-dir`), take the interpreter from the main repo venv by
absolute path, **ignore the inherited `VIRTUAL_ENV`**, put the *worktree* `src` on
`PYTHONPATH` (it wins over the editable `.pth`), print the resolved module path and the test
root, and exit non-zero if that path is not inside the worktree. Full explanation in
`build-loop.md`.

| Field | When null |
|---|---|
| `test_command` | `tests` is recorded as `not_applicable` and the **green-claim contradiction check is skipped** (`supervisor_service.py`: `if handoff and project.test_command:`). A Worker that claims `failed: 0, passed: N` is never contradicted, so a false pass can reach the Architect. Prefer a path-independent command over a null one. |
| `lint_command` | `lint` is `not_applicable`; no `lint.log` artifact is written. |
| `build_command` | `build` is `not_applicable`. |

```bash
# opt-in probe: run the configured commands in a throwaway worktree of the base branch and
# check they exit 0 AND resolve their imports inside that worktree. Off by default (it
# executes project code), and never reachable from the API or the GUI.
orchestrator project doctor myapp --run-commands
```

Two things the probe can tell you that are not wrapper bugs: a command added by a task branch
is missing from the base-branch snapshot until that branch merges (exit 127 on the first run),
and a command that prints no `ORCH_VERIFY` marker is reported as *not path-independent* even
when it exits 0 — that is the false-pass case, and the probe fails closed. Keep the configured
command's output count-parseable too: the gate reads the runner's summary line, and stacking a
second `-q` on a project whose `addopts` already has one suppresses it, so the comparison sees
`passed: 0` and can contradict nothing.
