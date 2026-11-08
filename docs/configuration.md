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
| `ORCHESTRATOR_MAX_DIFF_TOKENS` | `12000` | Diff budget in prompts. Every changed file first gets a 160-token first-hunk floor before any file receives more (40 files need 6400 of the 7800-token source share at the default, so the auditor's "every file has a hunk" invariant holds for at least 40); raising the number widens the remainder, not the floor |
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

`notifications_enabled` is **off on a new install**; each trigger has its own
`notification_<trigger>` switch (human-must-act on, noisy machinery off). A channel that is off
writes `skipped` (`master_disabled`/`trigger_disabled`), never the `failed` of an attempt.

Each transport result maps to one frozen CCR-T9-01 status; `details_json.outcome_class` names the
finer class:

| Outcome class | Status | When (`reason`) |
|---|---|---|
| `delivered` | `delivered` | `gdbus` `Notify` returned a notification id (empty) |
| `shown_unacknowledged` | `delivered` | `notify-send` exited 0, or a client was launched but timed out: shown, no acknowledgement observed (`shown_unacknowledged`) |
| `failed` | `failed` | the client was not launched (missing/`OSError`) or exited non-zero, or the notifier raised (`delivery_failed`) |
| `skipped` | `skipped` | master or trigger off, nothing attempted (`master_disabled` / `trigger_disabled`) |
| `coalesced` | `coalesced` | folded into an earlier popup (`coalesced_into:<id>`) |

A shown message is never `failed`; `shown_unacknowledged` has no status of its own (the vocabulary
is frozen) and maps to `delivered`. Nothing sends `--action` (it implies `--wait`, blocking until a
dismissal mako never sends) or an action array: no `ActionInvoked` listener exists.

**The timeout case was the defect.** A client that was spawned and then had to be killed at
`DELIVERY_TIMEOUT_SECONDS` is recorded `delivered` / `shown_unacknowledged`, because the old
`--action`/`--wait` trap showed the popup and then blocked; the row carries
`details_json.delivery_note` saying the display is unconfirmed. Only a client that could not be
started, or that exited non-zero, is `failed`. This ambiguity is documented rather than hidden in
the `error` column.

**Capabilities decide the body shape.** `probe_capabilities()` calls `GetCapabilities` first.
`body-markup` is a capability, not a hint: `gdbus` sends `{}`, and a markup daemon parses every
body, so text is HTML-escaped on both transports when `body-markup` is advertised; an anchor is
added only on `gdbus` with `body-hyperlinks`. `details_json.body_shape` records what was sent.

Observed on the development machine (CachyOS/Hyprland, 2026-11-08): `GetCapabilities` returned
`actions, body, body-hyperlinks, body-images, body-markup, icon-static, persistence`. `notify-send`
is chosen first, so in practice this machine receives `markup-escaped` text with an `Open: <url>`
line; the `body-hyperlinks` anchor path is covered by a fake-transport test, not by a live click.
Every other row below is expected-but-**unobserved** here: a daemon that advertises a capability
is not proof that its client renders it, so no shape is claimed for a daemon this project has not
probed.

| Daemon / probe result | Body | Link |
|---|---|---|
| `gdbus` with `body-markup` + `body-hyperlinks` (**expected**; not observed live here) | escaped text + one `<a href>` (`markup+hyperlink`) | untested live |
| `gdbus` with `body-markup` but not `body-hyperlinks` (**expected**; not observed live here) | escaped text, URL kept as `Open: <url>` (`markup-escaped`) | text |
| `notify-send`, on any daemon, including this machine's Hyprland/mako stack (**observed**) | escaped text when `body-markup` is advertised, else raw text (`markup-escaped` / `plain`) | text |
| probe failed (no `gdbus`, failed/timed-out call) or no `body-markup` (**observed for the probe-failure branch**) | raw plain text (`plain`) | text |

Dunst and other freedesktop daemons have **not** been probed by this project; whether they
advertise `body-hyperlinks` is unknown here, and none of the table's hyperlink claims should be
read as covering them.

**Pending rows are never stranded.** A `pending` row is resolved by the 2s coalescing timer, API
startup, the read path (`GET /api/notifications` flushes first), or CLI exit. Delivery claims rows
with a conditional `UPDATE` (`reason=claimed_by:<process>:<uuid>:<nanoseconds>`) before sending, so
a CLI exit and a server timer cannot both deliver one row. The claim carries a fresh per-attempt
uuid, so two claims made in the same instant are distinguishable, and it is only released after
`PROBE_TIMEOUT_SECONDS + DELIVERY_TIMEOUT_SECONDS + 5` seconds (17s by default) -- the capability
probe happens before the client call, so a shorter window could revoke a claim whose owner is still
legitimately delivering. Every terminal write (`delivered`/`coalesced`/`failed`) is a
compare-and-set on that claim marker, so a flusher whose claim was revoked writes nothing and cannot
overwrite the winner's record. `cli.main()` flushes **only
rows that process created**, then writes `failed` / `process_exit_undelivered` for its own
still-pending rows (`details_json.owner`); a live server's rows stay `pending`. The exit flush is
inert without a database file. Coalescing is global: one message can carry several runs/triggers
while `dedupe_key` still allows at most one row per run per trigger.

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
