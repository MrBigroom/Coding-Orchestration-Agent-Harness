# Security

Single-user, local-first. The threat model covers accidental exposure, secrets leaking
into logs/prompts/commits, untrusted model output, and destructive git operations.

## Localhost binding

The server binds to `127.0.0.1:8321` by default. Binding anywhere else prints a loud
warning, and `orchestrator serve` **refuses to start** unless `ORCHESTRATOR_TOKEN` is set.
Doctor flags non-local binds. There is no telemetry, no analytics, and no external CDN in
either UI.

## Optional token auth

With `ORCHESTRATOR_TOKEN` set, every `/api/*` route except the data-free
`/api/health` requires `X-Orchestrator-Token`, compared in constant time. WebSockets take
`?token=` because browsers can't set headers on upgrades. The GUI asks for the token on a
401 and keeps it in this browser's localStorage.

## Secret redaction

`utils/redaction.py` scrubs provider keys (`sk-ant-…`, `sk-…`), GitHub/GitLab/Slack/AWS/
Google tokens, JWTs, PEM private keys, bearer headers, `KEY=value` / `"password": "…"`
assignments, and the literal values of any secret-looking environment variables. It runs
on:
- log output (logging filter) and subprocess stdout/stderr before they're written to disk;
- every artifact the orchestrator writes;
- every prompt section before it reaches a provider, and diffs, which first have hunks for
  `.env`, key, and credential files removed;
- memory content and the build log.

The DB stores env var **names**, never key values. API and GUI only report whether a key
is set.

## Artifact path safety

Artifacts are served only from `ORCHESTRATOR_HOME/runs/<int run_id>/`. `safe_join`
rejects absolute paths, NUL bytes, `..` escapes, and symlinks that resolve outside the
root (400). A unit test and an API test cover encoded traversal attempts.

## Subprocess safety

- Always `create_subprocess_exec` / `subprocess.run` with argv lists, never `shell=True`.
- Project commands are split with `shlex`. Shell metacharacters (`| && ; > < $( )` and
  backticks) are rejected.
- Children get a minimal environment: allowlisted basics, the adapter's declared variable
  names, git identity, `GIT_TERMINAL_PROMPT=0`. The orchestrator's full environment is
  never inherited.
- Each child gets its own process group, so timeouts and cancellation kill descendants
  too (SIGTERM, then SIGKILL).
- Nothing arbitrary can run from the GUI. The only command endpoint runs a project's
  *configured* test/lint/build command, is off by default (`allow_gui_commands`), and
  checks `allowed_commands`.

## No destructive git defaults

- Every run happens in an isolated worktree on a phase branch. The main checkout is only
  touched by the gated `merge --no-ff`, which refuses unless the base branch is checked
  out and clean.
- Never push or force-push. Never delete branches (cleanup removes worktrees only).
- Cleanup only touches worktrees with an orchestrator marker under `worktree_root`, skips
  dirty ones, and supports `--dry-run` (the default).
- Recovery commits are labelled `chore(orchestrator-recovery): …`, logged, and never stage
  `.env`, keys, or credential files.
- `orchestrator reset` requires `--i-understand-data-loss` and only deletes
  `ORCHESTRATOR_HOME` data, never your repositories.

## No external GUI automation

The orchestrator never drives or scrapes the GUIs of OpenCode, ZCode, Qwen Code,
DeepSeek Harness or any other tool. It uses only their headless CLIs, through configurable
adapters, and model APIs through official SDKs.

## Untrusted model output

LLM output is data. Reviews are parsed strictly and fail closed. Reviewer claims of having
executed something are flagged and disregarded. Worker claims are checked against
independent verification, and the check wins. Handoff JSON is schema-validated before use.
