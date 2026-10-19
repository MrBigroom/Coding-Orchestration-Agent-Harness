# Harness adapters

Adapters run external coding CLIs **headlessly** inside the task's isolated worktree.
They never automate a GUI. Every adapter is data: a command plus an argv template, stored
in the `harness_adapters` table and editable in the GUI (Adapters page),
`PATCH /api/adapters/{name}`, or `orchestrator adapters set`.

**Template tokens:** `{cwd} {prompt_file} {prompt} {model} {branch} {artifacts_dir}
{run_id} {handoff_path}`. Each argv token is formatted on its own, with no shell. A token
prefixed with `?` is dropped when any placeholder in it is empty (e.g. `?--model={model}`).
`{prompt}` inlines the prompt file's text and is refused above 100 KB (Linux limits single
arguments to 128 KiB). Use `{prompt_file}` or stdin mode (`"prompt_via_stdin": true` in
capabilities) for larger prompts.

**Environment:** a child process gets only a small allowlist (PATH, HOME, locale, proxy,
CA bundle…), the adapter's declared variable names, and git author identity overrides.
The orchestrator's own environment is **not** passed through.

**Interface** (`adapters/base.py`): `detect()`, `health()`, `build_command()`, `run()`,
`cancel()`, `parse_result()`, `capabilities`.

**Worker contract:** the prompt asks the harness to commit on the phase branch and write
`.orchestrator/handoff.json` (git-excluded). When it doesn't, the orchestrator commits the
leftovers with a labelled recovery commit and synthesizes a handoff. A synthesized handoff
counts as a missing artifact at review.

> "Experimental" means the adapter is implemented and configurable, but this project's
> tests do not exercise it against the real CLI. Flags were checked against public docs
> on 2026-10-19 where noted. **Verify manually before relying on an adapter.**

---

## Mock (`mock`)

- **Purpose:** offline demo and tests. Appends a line to `MOCK_CHANGE.md`, commits it, and
  writes a handoff. Tags: `mock-fail`, `mock-timeout` (timeout clamped to 2 s),
  `mock-blocked`.
- **Command:** current Python, `-m orchestrator.adapters.mock_harness --cwd {cwd} …`
- **Env:** none · **Detect:** always available · **Test:** `orchestrator demo`
- **Experimental:** no (covered by integration tests).

## OpenCode (`opencode`)

- **Purpose:** preferred general, model-agnostic harness (default when no tag routes elsewhere).
- **Template:** `opencode run ?--model={model} {prompt}`. Non-interactive mode is
  `opencode run "<message>"` with `-m provider/model`. The worktree is the process cwd, so
  no `--cwd` is passed.
- **Env:** `OPENCODE_CONFIG`, plus whichever provider keys your OpenCode config uses
  (`ANTHROPIC_API_KEY`, `QWEN_API_KEY`, …). Override the binary with `OPENCODE_COMMAND`.
- **Detect:** `which opencode` · **Manual test:** `opencode --version && opencode run --help`,
  then `orchestrator task create "…" --project X --harness opencode` + `task dry-run`.
- **Experimental:** yes. The model value must be in OpenCode's `provider/model` form;
  set it on the task's profile or edit the template.

## DeepSeek Harness (`deepseek_harness`)

- **Purpose:** official DeepSeek agent harness; preferred for `deepseek`-tagged tasks when installed.
- **Template:** `dsh run --cwd {cwd} --prompt-file {prompt_file}`. **These flags are the
  product spec's placeholders and are UNVERIFIED.** DeepSeek documents a headless mode,
  but the exact flags weren't confirmed.
- **Env:** `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`; binary override `DEEPSEEK_HARNESS_COMMAND`.
- **Detect:** `which dsh`. Make sure it resolves to the official npm package
  `@deepseek-ai/dsh` (developer preview, needs Node ≥ 22.19). An unrelated community
  Python package also installs a `dsh` binary.
- **Manual test:** `dsh --version && dsh --help`, then edit the template to match.
- **Experimental:** yes.

## ZCode (`zcode`)

- **Purpose:** Z.ai harness for GLM models; preferred for `glm`-tagged tasks when installed.
- **Template:** `zcode-cli run --cwd {cwd} --prompt-file {prompt_file}`. **Binary name and
  flags are UNVERIFIED.** ZCode is primarily a desktop app. Reports mention an Agent CLI in
  its source, but no stable headless CLI was confirmed.
- **Env:** `GLM_API_KEY`, `GLM_OPENAI_BASE_URL`; binary override `ZCODE_COMMAND`.
- **Detect:** `which zcode-cli` (or your `ZCODE_COMMAND`) · **Manual test:** `<binary> --help`.
- **Experimental:** yes.

## Qwen Code (`qwen_code`)

- **Purpose:** Qwen-optimised harness; preferred for `qwen`-tagged tasks when installed.
- **Template:** `qwen --approval-mode auto-edit ?--model={model} -p {prompt}`. The binary is
  `qwen` (not `qwen-code`). `-p` runs one prompt headlessly. `auto-edit` allows file edits
  but doesn't auto-approve shell commands. Use `--yolo` only if you accept unattended
  shell execution inside the worktree.
- **Env:** `QWEN_API_KEY`, `QWEN_OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
  `OPENAI_MODEL`; binary override `QWEN_CODE_COMMAND`.
- **Detect:** `which qwen` · **Manual test:** `qwen --version && qwen --help`.
- **Experimental:** yes (docs-checked).

## Aider (`aider`)

- **Purpose:** optional fallback after OpenCode.
- **Template:** `aider --yes-always ?--model={model} --message-file {prompt_file}`. Aider
  auto-commits by default, and those commits land on the phase branch.
- **Env:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_API_BASE`, `DEEPSEEK_API_KEY`;
  binary override `AIDER_COMMAND`.
- **Detect:** `which aider` · **Manual test:** `aider --version`.
- **Experimental:** yes.

## Claude Code CLI (`claude_cli`)

- **Purpose:** optional headless Claude Code runs, e.g. for escalations. **Disabled by default.**
- **Template:** `claude -p {prompt} --permission-mode acceptEdits ?--model={model}`.
- **Env:** `CLAUDE_CODE_OAUTH_TOKEN` (optional, from `claude setup-token`), `CLAUDE_CONFIG_DIR`;
  binary override `CLAUDE_CLI_COMMAND`. `ANTHROPIC_API_KEY` is deliberately **not** passed.
- **Auth:** your own Claude subscription through the unmodified `claude` binary
  (`claude auth login`). Anthropic permits `claude -p` on subscription limits. Worker runs
  use the same plan quota as the Architect, so keep parallel runs low.
- **Detect:** `which claude` · **Manual test:** `claude --version && claude -p "say hi"`.
- **Experimental:** yes.

## Routing

1. An explicit `assigned_harness_adapter` on the task always wins.
2. `mock`/`mock-*` tags → mock.
3. `deepseek` → DeepSeek Harness, `glm` → ZCode, `qwen` → Qwen Code, when installed and enabled.
4. Otherwise OpenCode, then Aider.
5. No harness: low-risk tasks fall back to a **direct API worker** (output stored as an
   artifact, no repo changes). Anything else is **blocked**.
