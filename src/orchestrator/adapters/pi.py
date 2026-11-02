from __future__ import annotations

import json
from pathlib import Path

from orchestrator.adapters.base import AdapterResult, BaseHarnessAdapter, RunContext
from orchestrator.adapters.common import (
    JsonOutputMixin,
    ManagedConfigMixin,
    pi_stop_reason,
    provider_settings,
    takes_images,
    wants_explicit_cache,
)

FAILED_STOP_REASONS = {"length", "error"}


class PiAdapter(JsonOutputMixin, ManagedConfigMixin, BaseHarnessAdapter):
    """Pi (npm `@earendil-works/pi-coding-agent`), a minimal coding agent: 4 tools
    (read, bash, edit, write) and a ~1.5k-token request prefix, the smallest measured
    among full coding agents (see scripts/harness-bench).

    The prompt is piped on stdin (Pi prepends stdin to the first message), `--mode json`
    prints events with per-call token usage, and `--no-session` keeps nothing on disk.
    Unless `capabilities.provider` is null, each run gets a temporary PI_CODING_AGENT_DIR
    with a `models.json` that defines the `orchestrator` provider (default: the Token Plan
    via QWEN_API_KEY / QWEN_OPENAI_BASE_URL; the key is written as `$QWEN_API_KEY`, never
    its value). For models that support explicit caching, Pi sends `cache_control` on the
    system prompt, last tool and newest message (cacheControlFormat=anthropic).
    With `provider` null, edit the template to drop `--provider orchestrator` and use
    your own ~/.pi config.

    Pi exits 0 even when its last response hit `max_tokens` or errored. A run that ends
    that way without a handoff file is reported as failed; with a handoff, the worker had
    already finished and only its closing message was lost.

    Experimental: checked against Pi 1.4.0 on 2026-10-26 with a local mock API.
    """

    kind = "pi"
    experimental = True
    default_command = "pi"
    default_args = [
        "--mode",
        "json",
        "--no-session",
        "--provider",
        "orchestrator",
        "--model",  # Pi rejects the `--model=<id>` form
        "{model}",
        "Carry out the task described above.",
    ]
    default_env_vars = ["QWEN_API_KEY", "QWEN_OPENAI_BASE_URL"]
    default_capabilities = {"prompt_via_stdin": True}
    description = "Minimal, token-lean coding agent (4 tools); default for flash-class models."
    verify_hint = "pi --version && pi --help"

    def prepare(self, ctx: RunContext, tmp: Path) -> dict[str, str]:
        env = {"PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"}
        prov = provider_settings(self.capabilities)
        if prov is None:
            return env
        model: dict = {"id": ctx.model or "default", "name": ctx.model or "default"}
        if wants_explicit_cache(ctx.model, self.capabilities):
            model["compat"] = {"cacheControlFormat": "anthropic"}
        if takes_images(ctx.model, self.capabilities):
            model["input"] = ["text", "image"]
        config = {
            "providers": {
                "orchestrator": {
                    "baseUrl": prov["base_url"],
                    "api": "openai-completions",
                    "apiKey": f"${prov['api_key_env']}",
                    "models": [model],
                }
            }
        }
        (tmp / "models.json").write_text(json.dumps(config, indent=2))
        env["PI_CODING_AGENT_DIR"] = str(tmp)
        return env

    async def run(self, ctx: RunContext, runner) -> AdapterResult:
        result = await super().run(ctx, runner)
        if (
            result.exit_code == 0
            and result.stdout_path
            and result.stdout_path.is_file()
            and not ctx.handoff_path.is_file()
        ):
            reason = pi_stop_reason(result.stdout_path.read_text(errors="replace"))
            if reason in FAILED_STOP_REASONS:
                result.error = f"Pi stopped early (stopReason: {reason}) without a handoff"
        return result
