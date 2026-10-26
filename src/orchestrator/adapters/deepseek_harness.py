from __future__ import annotations

import json
from pathlib import Path

from orchestrator.adapters.base import BaseHarnessAdapter, RunContext
from orchestrator.adapters.common import (
    JsonOutputMixin,
    ManagedConfigMixin,
    provider_settings,
    wants_explicit_cache,
)


class DeepSeekHarnessAdapter(JsonOutputMixin, ManagedConfigMixin, BaseHarnessAdapter):
    """DeepSeek Harness (`dsh`, npm `@deepseek-ai/dsh`, developer preview).

    Headless mode is `dsh --profile headless [--json] <task | ->`; `-` reads the task from
    stdin, so prompt size is not limited by argv. `--json` prints run events, including
    per-step token usage. Checked against dsh 0.2.1-rc.2 on 2026-10-26 with a local mock API.

    Endpoint: dsh routes models through pi-ai provider routes. Unless
    `capabilities.provider` is null, each run gets a temporary DSH_HOME whose home-level
    `cordis.patch.yml` adds an OpenAI-compatible route (default: the Token Plan via
    QWEN_API_KEY / QWEN_OPENAI_BASE_URL; the key is referenced by env-var name only) and
    makes the task's model the default. Explicit cache markers are enabled for models that
    support them (see `common.DEFAULT_EXPLICIT_CACHE_MODELS`).

    Beware: an unrelated community Python package also installs a `dsh` binary — check
    `dsh --version` resolves to the official one.
    """

    kind = "deepseek_harness"
    experimental = True
    default_command = "dsh"
    default_args = ["--profile", "headless", "--json", "-"]
    default_env_vars = ["DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "QWEN_API_KEY"]
    default_capabilities = {"prompt_via_stdin": True}
    description = "Official DeepSeek agent harness; lean and cache-friendly (deepseek models)."
    verify_hint = "dsh --version && dsh --profile headless --help"

    def prepare(self, ctx: RunContext, tmp: Path) -> dict[str, str]:
        prov = provider_settings(self.capabilities)
        if prov is None or not ctx.model:
            return {}
        route: dict = {
            "displayName": "Orchestrator",
            "apiKeyEnv": prov["api_key_env"],
            "api": "openai-completions",
            "baseURL": prov["base_url"],
            "models": [{"id": ctx.model, "name": ctx.model}],
        }
        if wants_explicit_cache(ctx.model, self.capabilities):
            route["compat"] = {"cacheControlFormat": "anthropic"}
        patch = [
            {"id": "llm-pi-ai", "config": {"providers": {"orchestrator": route}}},
            {
                "id": "agent-default-model",
                "config": {"provider": "orchestrator", "model": ctx.model},
            },
        ]
        # JSON is valid YAML, so no YAML dependency is needed.
        (tmp / "cordis.patch.yml").write_text(json.dumps(patch, indent=2))
        return {"DSH_HOME": str(tmp)}
