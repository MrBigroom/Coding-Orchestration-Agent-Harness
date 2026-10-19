# Providers

Providers handle planning, supervision recommendations, review, simple agent tasks, and
the direct-API fallback worker. Coding work normally goes through a harness adapter.

Each provider row stores `kind`, `base_url`, and `api_key_env`, the **name** of the env
var that holds the key. Keys come from the environment or `.env` at call time and are
never stored or shown. `base_url` accepts `env:VAR` or `env:VAR|default`.

All requests ask for structured JSON. Output is parsed from raw JSON, fenced blocks, or
embedded objects and validated with Pydantic. Invalid output is **retried once** with the
validation error attached, and a second failure is a clear `ProviderError`.

## Anthropic (`kind=anthropic`)

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
  - Use an Anthropic **API key**. A Claude.ai subscription is not an API credential.

## Qwen Cloud / Alibaba Model Studio (`kind=openai_compatible`, provider `qwen-token-plan`)

- **Env:** `QWEN_API_KEY`, `QWEN_OPENAI_BASE_URL`. The default is the Token Plan endpoint
  `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`. Alibaba's docs
  disagree on whether Personal Edition is Beijing- or Singapore-region, so copy the URL
  from your console.
- **Profiles (all on this provider by default):** `supervisor-qwen-max` → `qwen3.8-max`
  (**primary supervisor**), `worker-qwen-flash` → `qwen3.8-flash`, `worker-deepseek-flash`
  → `deepseek-v4.1-flash`, `worker-glm` → `glm-5.3`, `agent-flash` → `qwen3.8-flash`.
  These IDs match Model Studio's documented model IDs as of 2026-10-19.
- ⚠️ **Terms of use.** Alibaba's Token Plan documentation says the plan is *for
  interactive use only within compatible AI coding and agent tools, not for automated
  scripts or application backends*, and violations may suspend the subscription or revoke
  the key. The background queue worker, schedules, and direct API calls from this
  orchestrator are automated use. The default follows your explicit choice. To stay
  within the terms, point `QWEN_OPENAI_BASE_URL` at the pay-as-you-go compatible-mode
  endpoint for your region, or limit Token Plan usage to interactive harness sessions.
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
| Planning, gate review, high-risk final review | `architect-opus` |
| Supervisor recommendation | `supervisor-qwen-max`, then `supervisor-sonnet` |
| Coding worker / direct fallback | tag `deepseek` → `worker-deepseek-flash`, `glm` → `worker-glm`, `qwen`/default → `worker-qwen-flash` |
| Background / cron agent tasks | `agent-flash`; Opus only if escalated |
| Offline | tag `mock` → `mock-*` profiles |

Model IDs are editable placeholders. Change them on the Models & Providers page or via
`PATCH /api/model-profiles/{id}`.
