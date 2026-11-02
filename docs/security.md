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

## Other websites are refused

Any page in your browser can send requests and open WebSockets to `127.0.0.1`, and DNS
rebinding can point a hostile domain at this server. So every HTTP request and WebSocket,
including the GUI's own pages, must have:

- a local `Host`, or the configured bind host. With a token set, any `Host` is accepted,
  because a hostile page can't know the token and every `/api/*` route still needs it.
- an `Origin` matching that `Host`, if it sends one. Browsers send it on these requests, and
  CLI tools and scripts don't.

Refused requests get a 403, and WebSockets close with code 4403. A reverse proxy must pass
the browser's `Host` through unchanged.

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
- The only GUI endpoint that runs a command itself runs a project's *configured*
  test/lint/build command, is off by default (`allow_gui_commands`), and checks
  `allowed_commands`.
- Chat is the one way to reach arbitrary execution from the GUI, because it drives a coding
  agent that has a shell. See below.

## Interactive sessions (Chat)

A chat session gives a browser page control of an agent running as your user, so it has
its own gates:

- **Off by default.** `allow_interactive_sessions` must be turned on, and it's checked on
  every message as well as when a session starts.
- **Other websites are refused**, as for the rest of the server. See
  [above](#other-websites-are-refused).
- **Isolated by default.** A session gets its own worktree and branch unless you choose
  the project checkout, which the GUI flags, or no project, which gives it a new empty folder.
  Merging goes through the `merge_enabled` gate.
- **Asks first by default.** Yolo modes need a confirmation. What "ask" means depends on the
  harness; see [chat.md](chat.md#permission-modes). Only DeepSeek Harness enforces its mode
  with an OS sandbox. For the others, modes are prompts inside the agent, not a boundary, and
  a worktree or folder limits where an agent starts, not what it can reach.
- **Minimal environment and redaction.** Children get the same allowlisted environment as
  batch runs, and transcripts are redacted before they reach disk. Text shown live is
  redacted per chunk, so a secret split across chunks can appear on screen, though not in
  the stored transcript.
- **No review.** Sessions skip the supervisor, verification and review gate.

## Standalone runs

A [standalone run](agents.md#standalone-runs) is a Chat session with nobody watching, so it
has no human approval and no review:

- **Off by default.** `allow_unattended_sessions` must be on, and it's checked on every run.
  This setting and `allow_interactive_sessions` never sync between devices.
- **Denied unless it can be identified.** In **Read and report**, the run denies anything that
  isn't a read or a write inside its output folder. Paths are resolved first, so `..` and
  symlinks can't leave the folder.
- **Not a sandbox.** Only DeepSeek Harness enforces its mode with an OS sandbox. For the others,
  these limits depend on the harness asking first. Pi's are the orchestrator's prompt gate, and
  a command a harness treats as read-only runs without being checked.
- **Can make changes approves shell commands.** Changes land on a separate branch, but a
  worktree limits where an agent starts, not what it can reach. Use it only for agents you'd
  trust with your user account, because prompt injection from anything the agent reads could
  run commands.
- **Contained blast radius for reads.** Read and report works in a throwaway copy, so a write
  that slips through doesn't reach your checkout. If that copy was changed, it's kept and the
  run says so.

The same environment allowlist and redaction as Chat apply, and each run has a time limit.

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
