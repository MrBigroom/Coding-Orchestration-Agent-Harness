# Providers

Providers handle planning, supervision recommendations, review, simple agent tasks, and
the direct-API fallback worker. Coding work normally goes through a harness adapter.

Each provider row stores `kind`, `base_url`, and `api_key_env`, the **name** of the env
var that holds the key. Keys come from the environment or `.env` at call time and are
never stored or shown. `base_url` accepts `env:VAR` or `env:VAR|default`.

All requests ask for structured JSON. Output is parsed from raw JSON, fenced blocks, or
embedded objects and validated with Pydantic. Invalid output is **retried once** with the
validation error attached, and a second failure is a clear `ProviderError`.

## Claude subscription via Claude Code CLI (`kind=claude_cli`, provider `claude-subscription`) — default Architect

- **Setup:** install Claude Code, then `claude auth login` with your Claude Pro/Max account.
  No API key. For headless machines, `claude setup-token` prints a long-lived token you
  can export as `CLAUDE_CODE_OAUTH_TOKEN`; it's an official Claude Code mechanism.
- **Profile:** `architect-claude-cli` → `claude-opus-5-5`, effort `high`. It's used for
  planning, gate reviews, and escalations ahead of `architect-opus`.
- **How a call runs:**
  `claude -p --output-format json --model <model> --system-prompt-file <static prefix>
  --tools "" --disallowedTools "mcp__*" --strict-mcp-config --no-session-persistence
  [--effort <e>]`. Dynamic state goes on stdin, and the call runs in an empty temporary
  directory. With every tool removed, the Architect has no execution access, as its role
  requires. Calls run one at a time.
- **Policy** (checked 2026-10-19): the Claude Help Center says subscribers "can still use
  ... `claude -p` ... with your subscription limits". Anthropic's legal page reserves
  OAuth sign-in for "ordinary use of Claude Code and other native Anthropic applications"
  and forbids routing requests through Free/Pro/Max credentials from other software. So
  the orchestrator only ever launches the unmodified `claude` binary. It never reads,
  stores, or forwards the token, and it **never passes `ANTHROPIC_API_KEY`**, so usage
  can't silently switch to API billing.
- **Limits:** reviews count against your plan's usage, which assumes "ordinary,
  individual usage". When a limit is hit, `claude -p` returns an error, the review becomes
  `NEEDS_HUMAN`, and the task blocks. `orchestrator doctor` runs `claude auth status` and
  warns when Claude Code is using an API key instead of your subscription.
- **Optional flags:** `CLAUDE_CLI_EXTRA_ARGS` adds flags, e.g. `--safe-mode` to skip your
  personal `~/.claude` CLAUDE.md, plugins, and hooks during reviews. `CLAUDE_CLI_COMMAND`
  overrides the binary path.
- **Verified:** flags were checked against the Claude Code CLI reference, and one real
  call was made with Claude Code 2.1.303 (it reported no tools available and returned
  parsed JSON).

## Anthropic API (`kind=anthropic`, optional)

- **Env:** `ANTHROPIC_API_KEY` (required), `ANTHROPIC_BASE_URL` (optional).
- **Profiles:** `architect-opus` → `claude-opus-5-5` (effort `high`); `supervisor-sonnet` →
  `claude-sonnet-5-5` (effort `medium`, alternate supervisor).
- **Notes** (checked against the installed `anthropic` 1.x SDK and Anthropic's docs):
  - Claude Opus 5.5 / Sonnet 5.5 don't accept sampling parameters, so `temperature` is
    never sent. Control depth with the profile's `effort` (`low`…`max`). Opus 5.5 defaults
    to `medium` when unset.
  - Static prompt sections get `cache_control` breakpoints (see `prompt-caching.md`).
  - Server-side refusal fallback (`fallbacks="default"`, beta
    `server-side-fallback-2026-07-01`) is enabled for Claude 5.x models. Turn it off with
    `ORCHESTRATOR_ANTHROPIC_REFUSAL_FALLBACK=false`. A final `stop_reason=refusal` becomes
    a `ProviderRefusal`, and the review is recorded as `NEEDS_HUMAN`.
  - Use an Anthropic **API key**. A Claude.ai subscription is not an API credential (use
    the `claude-subscription` provider above for that). Max 5x/20x plans include
    $100/$200 per month of API credits that work with this provider; Pro doesn't.

## Qwen Cloud / Alibaba Model Studio (`kind=openai_compatible`, provider `qwen-token-plan`)

- **Env:** `QWEN_API_KEY`, `QWEN_OPENAI_BASE_URL`. The default is the Token Plan endpoint
  `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`. Alibaba's docs
  disagree on whether Personal Edition is Beijing- or Singapore-region, so copy the URL
  from your console.
- **Profiles (all on this provider by default):** `supervisor-qwen-max` → `qwen3.8-max`
  (**primary supervisor**), `worker-qwen-flash` → `qwen3.8-flash`, `worker-deepseek-flash`
  → `deepseek-v4.1-flash`, `worker-glm` → `glm-5.3`, `agent-flash` → `qwen3.8-flash`.
  These IDs match Model Studio's documented model IDs as of 2026-10-19.
- **Qwen Cloud = Alibaba Model Studio.** Qwen Cloud's Token Plan (docs.qwencloud.com) and
  Alibaba Model Studio's Token Plan carry the same scope rule: interactive use inside
  coding/agent tools only, with no "automation scripts, custom application backends, or
  any non-interactive batch call scenarios". The rule attaches to the **plan key**, so it
  covers every model on the plan, including `deepseek-v4.1-flash` and `glm-5.3`. It also
  covers harness runs the orchestrator launches unattended (e.g. `qwen -p`), not only
  direct API calls.
- ✅ **Support confirmation (Alibaba Cloud International Support, 2026-10-19).** We
  described this harness to them: a single user on a local machine clicks Run per task,
  and the harness launches `qwen -p` with the Token Plan key, which edits code and runs
  tests. Their answer:
  - Manually triggering individual tasks "is fully permitted", as long as you avoid
    **high-frequency automated scripts**, **sharing the account among multiple users**,
    and **using the service to train third-party models**.
  - Model Studio "monitors request characteristics at the API level rather than
    identifying specific client tools". Any tool making legitimate calls through the
    standard APIs, without bypassing security measures or abusing quotas, is treated the
    same, so direct API calls from the orchestrator are in the same category as `qwen -p`.
  - `deepseek-v4.1-flash` and `glm-5.3` are included in the Personal Edition and can be
    used normally.
  - Use the API key and base URL that belong to your Token Plan, and never put the key in
    a public repository.
- **Staying within it in this harness:** start tasks yourself; keep `max_parallel_runs`
  low; don't put frequent cron schedules on Token Plan profiles; keep
  `max_review_rounds` modest (each CHANGES_REQUESTED round triggers another Worker run
  without a click). The written docs still describe the plan as "interactive use only",
  so if your usage pattern changes significantly, check again with support.
- Prefix caching is automatic where supported. `cached_tokens` is recorded when returned.

## DeepSeek direct (`provider deepseek`, disabled by default)

- **Env:** `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` (default `https://api.deepseek.com`).
- **Model ID:** DeepSeek's API name is **`deepseek-flash`**, which currently serves
  DeepSeek-V4.1-Flash-1017; the legacy `deepseek-v4-flash` still routes there. Note that
  Model Studio uses `deepseek-v4.1-flash` instead. Enable the provider and point a worker
  profile at it to use the direct API.

## GLM / Z.ai direct (`provider glm`, disabled by default)

- **Env:** `GLM_API_KEY`, `GLM_OPENAI_BASE_URL`. The default is the general API
  `https://api.z.ai/api/paas/v4`. The GLM Coding Plan uses
  `https://api.z.ai/api/coding/paas/v4`; check Z.ai's terms for automated use before
  pointing the orchestrator at it.
- **Model ID:** set the exact GLM model ID from Z.ai's docs on your profile. `glm-5.3` was
  not confirmed in Z.ai's own API docs at the time of writing.

## Hermes / local OpenAI-compatible (`provider hermes`, disabled)

- **Env:** `HERMES_OPENAI_BASE_URL` (e.g. a local llama.cpp/vLLM server), `HERMES_API_KEY`
  (optional).
- **Profile:** `hermes-local`. Used **only** when explicitly assigned or the task is
  tagged `hermes`. It is never a default.

## Routing summary

| Need | Profile |
|---|---|
| Planning, gate review, high-risk final review | `architect-claude-cli` (`claude -p` on your subscription), then `architect-opus` (API key) |
| Supervisor recommendation | `supervisor-qwen-max`, then `supervisor-sonnet` |
| Coding worker / direct fallback | tag `deepseek` → `worker-deepseek-flash`, `glm` → `worker-glm`, `qwen`/default → `worker-qwen-flash` |
| Background / cron agent tasks | `agent-flash`; Opus only if escalated |
| Offline | tag `mock` → `mock-*` profiles |

Model IDs are editable placeholders. Change them on the Models & Providers page or via
`PATCH /api/model-profiles/{id}`.
