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
> tests don't run it against a real model. "Mock-API-checked" means the real CLI was run
> through the adapter against a local mock API on 2026-10-26 (`scripts/harness-bench`):
> argv, stdin, per-run config, cache markers and usage parsing all worked.
> **Verify manually before relying on an adapter.**

**Managed config (Pi, DeepSeek Harness, Qwen Code, ZCode):**
- **Endpoint:** each run is pointed at the endpoint in `capabilities.provider`. The
  default is `{"base_url_env": "QWEN_OPENAI_BASE_URL", "base_url_default": <Token Plan>,
  "api_key_env": "QWEN_API_KEY"}`.
- **Per-run config:** the files these harnesses need go into a temporary directory that
  is deleted after the run, so they never land in `runs/<id>`. Keys are referenced by
  env-var **name** only.
- **Using your own config:** set `capabilities.provider` to `null` to keep the harness's
  own `~/.<harness>` config. For Pi, also drop `--provider orchestrator` from the template.

**Long lines:** JSON modes print whole messages on one line (Pi reaches about 500 KB), so
the log pumps accept lines up to 32 MiB.

**Token usage:** usage the harness prints is parsed into `runs.usage`. It covers Pi,
DeepSeek Harness, Qwen Code, ZCode and OpenCode via their JSON modes, Claude Code via
`--output-format json`, and Aider via its `Tokens:` lines. Redacted numeric fields are
tolerated in saved ZCode JSON. See [`harness-routing.md`](harness-routing.md).

---

## Mock (`mock`)

- **Purpose:** offline demo and tests. Appends a line to `MOCK_CHANGE.md`, commits it, and
  writes a handoff. Tags: `mock-fail`, `mock-timeout` (timeout clamped to 2 s),
  `mock-blocked`.
- **Command:** current Python, `-m orchestrator.adapters.mock_harness --cwd {cwd} …`
- **Env:** none · **Detect:** always available · **Test:** `orchestrator demo`
- **Experimental:** no (covered by integration tests).

## Pi (`pi`)

- **Purpose:** minimal coding agent: 4 tools and a ~1.4k-token request prefix, the
  smallest among full agents. It is the first choice for Qwen, DeepSeek and GLM worker
  models.
- **Install:** `npm i -g @earendil-works/pi-coding-agent` (the old `@mariozechner/…`
  package is deprecated).
- **Template:** `pi --mode json --no-session --provider orchestrator --model {model}
  "Carry out the task described above."`. The prompt goes on stdin, and Pi prepends it to
  the message, so prompt size isn't limited by argv. Pi rejects `--model=<id>`, so the
  model is a separate argument.
- **Managed config:** a temporary `PI_CODING_AGENT_DIR/models.json` defines the
  `orchestrator` provider (`apiKey: "$QWEN_API_KEY"`). For `qwen3.8-max`/`qwen3.8-flash`
  it adds `compat.cacheControlFormat: "anthropic"`, so Pi marks the system prompt, last
  tool and newest message for Qwen Cloud's explicit cache. Pi treats a model as text-only
  unless told otherwise, so models whose names start with an entry in the
  `image_models` capability (default `qwen3.8-` and `deepseek-v4`; `glm-5.3` refuses
  images) get `input: ["text", "image"]`. The model also gets its `contextWindow` (profile,
  then `core/context_windows.py`); without one Pi assumes 128K and compacts at about 112K.
  `PI_OFFLINE=1`, `PI_SKIP_VERSION_CHECK=1` and
  `PI_TELEMETRY=0` are also set.
- **Failure detection:** Pi exits 0 even when its last response hit `max_tokens` or
  errored. A run that ends that way without a handoff file is reported as failed.
- **Env:** `QWEN_API_KEY`, `QWEN_OPENAI_BASE_URL`; binary override `PI_COMMAND`.
- **Detect:** `which pi` · **Manual test:** `pi --version && pi --help`.
- **Experimental:** yes (mock-API-checked, Pi 1.4.0).

## OpenCode (`opencode`)

- **Purpose:** general, model-agnostic harness and the fallback after each model's
  preferred harnesses.
- **Template:** `opencode run --format json ?--model={model} {prompt}`. `--format json`
  prints events with per-step token usage. Use `-m provider/model` to pick the model. The
  worktree is the process cwd, so no `--cwd` is passed.
- **Env:** `OPENCODE_CONFIG`, plus whichever provider keys your OpenCode config uses
  (`ANTHROPIC_API_KEY`, `QWEN_API_KEY`, …). Override the binary with `OPENCODE_COMMAND`.
- **Detect:** `which opencode` · **Manual test:** `opencode --version && opencode run --help`,
  then `orchestrator task create "…" --project X --harness opencode` + `task dry-run`.
- **Experimental:** yes (mock-API-checked, OpenCode 1.19.4). The model value must be in
  OpenCode's `provider/model` form; set it on the task's profile or edit the template.
  OpenCode sends no explicit cache markers to OpenAI-compatible providers, so it relies
  on the implicit cache. It also makes one extra ~520-token title call per run.

## DeepSeek Harness (`deepseek_harness`)

- **Purpose:** official DeepSeek agent harness, and the second choice after Pi for
  `deepseek*` worker models. In published tests it used about 7× fewer tokens per task than
  Claude Code or OpenCode on DeepSeek V4.
- **Template:** `dsh --profile headless --json -`. `-` reads the task from stdin, and
  `--json` prints run events with per-step usage.
- **Managed config:** a temporary `DSH_HOME` with a home-level `cordis.patch.yml` adds an
  OpenAI-compatible `orchestrator` route (`apiKeyEnv: QWEN_API_KEY`) and makes the task's
  model the default. That means the Token Plan's `deepseek-v4.1-flash` works without a
  DeepSeek account. With `capabilities.provider: null`, dsh uses its own DeepSeek login
  (`DEEPSEEK_API_KEY`).
- **Env:** `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `QWEN_API_KEY`; binary override
  `DEEPSEEK_HARNESS_COMMAND`.
- **Detect:** `which dsh`. Make sure it resolves to the official npm package
  `@deepseek-ai/dsh` (developer preview, needs Node ≥ 22.19). An unrelated community
  Python package also installs a `dsh` binary.
- **Manual test:** `dsh --version && dsh --profile headless --help`.
- **Experimental:** yes (mock-API-checked, dsh 0.2.1-rc.2). It makes one extra ~140-token
  title call per run.

## ZCode (`zcode`)

- **Purpose:** Z.ai harness for GLM models, and the second choice after Pi on their route.
- **Template:** `zcode --cwd {cwd} --mode yolo --json --prompt {prompt}`. These flags were
  checked against the actual parser in ZCode 3.10.2's bundled CLI 0.16.5. Its help also
  advertises `--settings` and `--max-turns`, but that parser rejects them, so the adapter
  does not use them.
- **Managed config:** an isolated temporary `HOME` contains `.zcode/cli/config.json` with an
  OpenAI-compatible provider and the selected model. The provider id comes from the key env
  var (`QWEN_API_KEY` → `qwen`), allowing ZCode to resolve the secret itself; no key is
  written to the file. `ZCODE_STORAGE_DIR` is isolated too. With `capabilities.provider:
  null`, ZCode uses its normal configuration instead.
- **Env:** `QWEN_API_KEY`, `QWEN_OPENAI_BASE_URL`, `GLM_API_KEY`, `GLM_OPENAI_BASE_URL`;
  binary override `ZCODE_COMMAND`.
- **Detect:** `which zcode`, then the bundled `/opt/ZCode/resources/glm/zcode.cjs` via
  `/usr/bin/node` (or another detected Node). This also supports installations where `zcode`
  exists only as a shell function. **Manual test:** `zcode --version` or
  `/usr/bin/node /opt/ZCode/resources/glm/zcode.cjs --version`.
- **Experimental:** yes (full adapter run mock-API-checked with CLI 0.16.5, including usage
  and final-response parsing, then four real runs on the Token Plan on 2026-11-01).

## Qwen Code (`qwen_code`)

- **Purpose:** Qwen's own harness, used for `qwen`-tagged tasks and as the second choice
  for Qwen models.
- **Template:** `qwen --auth-type openai --approval-mode auto-edit --allowed-tools
  run_shell_command --exclude-tools skill notebook_edit -o json ?--model={model} -p
  {prompt}`. The binary is `qwen` (not `qwen-code`).
  - **Shell tool:** with `auto-edit` alone, `-p` mode doesn't offer the shell tool at all,
    so the worker couldn't run tests or commit. `--allowed-tools run_shell_command`
    restores it and **auto-approves shell commands** inside the isolated worktree.
  - **Excluded tools:** dropping `skill`/`notebook_edit` saves about 1.7k tokens per call.
  - **JSON output:** `-o json` prints one line at exit, holding the usage and the final
    message. Everything past 64 KiB is lost on a pipe, so stdout goes to a temporary file
    and is redacted into `stdout.log` after the process exits. That means no live log.
- **Managed config:** a temporary system-settings file
  (`QWEN_CODE_SYSTEM_SETTINGS_PATH`) turns off managed auto-memory. Otherwise every run
  makes a second, roughly 6k-token "memory extraction" call. `QWEN_API_KEY` and the base
  URL are mapped to Qwen Code's `OPENAI_*` variables. Overall the run goes from about
  13.1k + 5.8k tokens over two calls to about 9.1k in one call. An isolated temporary
  `HOME` stops `modelProviders` in `~/.qwen/settings.json` overriding that endpoint and
  key, and keeps your Qwen MCP servers out of worker runs.
  The same file turns off `general.gitCoAuthor`, so commits and PRs made in Tasks and
  Chat get no `Qwen-Coder` co-author trailer and no `refs/notes/ai-attribution` note.
- **Env:** `QWEN_API_KEY`, `QWEN_OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
  `OPENAI_MODEL`; binary override `QWEN_CODE_COMMAND`.
- **Detect:** `which qwen` · **Manual test:** `qwen --version && qwen --help`.
- **Experimental:** yes (mock-API-checked, Qwen Code 0.26.1; real Token Plan runs, 0.25.1).
  Over the OpenAI-compatible API it sends no explicit cache markers, so it relies on the
  implicit cache.

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
- **Template:** `claude -p {prompt} --output-format json --permission-mode acceptEdits
  ?--model={model} --allowedTools Bash`. The JSON result carries token usage, including
  cache reads. `--allowedTools Bash` **auto-approves shell commands** inside the isolated
  worktree; with `acceptEdits` alone, `-p` mode can't run tests or commit.
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
3. `qwen` → Qwen Code, when installed and enabled. `deepseek` and `glm` tags only pick the
   worker profile, so their harness comes from the model's route.
4. The worker model's preferred harnesses, the first installed one winning, or the one
   with the lowest measured tokens per accepted task once there's enough data
   ([`harness-routing.md`](harness-routing.md)).
5. Otherwise OpenCode, then Aider.
6. No harness: low-risk tasks fall back to a **direct API worker** (output stored as an
   artifact, no repo changes). Anything else is **blocked**.

Existing databases are upgraded at `orchestrator init` / startup. An adapter row is
moved to the new default template only if it still holds an earlier version's default,
so templates you've edited are never overwritten.
