"""Anthropic Messages API provider (official `anthropic` SDK).

Prompt caching: static sections become `system` text blocks; sections flagged with
`cache_breakpoint` get `cache_control: {"type": "ephemeral"}` (max 4 per request). Dynamic
state goes into the single user message, after every breakpoint.

Claude 5.x notes (verified against the SDK installed for this project):
  - sampling params (temperature) are not accepted, so `temperature` is never sent;
  - depth is controlled with `output_config.effort` (profile.effort);
  - server-side refusal fallback (`fallbacks="default"`) is enabled by default and can be
    turned off with ORCHESTRATOR_ANTHROPIC_REFUSAL_FALLBACK=false.
"""

from __future__ import annotations

import os
from typing import Any

from orchestrator.core.config import get_settings
from orchestrator.providers.base import (
    BaseProvider,
    CompletionRequest,
    CompletionResult,
    ProviderError,
    ProviderRefusal,
)

MAX_BREAKPOINTS = 4
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1")


class AnthropicProvider(BaseProvider):
    kind = "anthropic"

    def _api_key(self) -> str | None:
        return os.environ.get(self.config.api_key_env or "ANTHROPIC_API_KEY")

    def validate_config(self) -> list[str]:
        problems = []
        try:
            import anthropic  # noqa: F401
        except ImportError:
            problems.append("anthropic SDK not installed")
        if not self._api_key():
            problems.append(f"env var {self.config.api_key_env or 'ANTHROPIC_API_KEY'} is not set")
        return problems

    def build_request(self, req: CompletionRequest) -> dict[str, Any]:
        """Pure function: build the Messages API kwargs (unit-testable, no network)."""
        system: list[dict[str, Any]] = []
        breakpoints = 0
        statics = req.prompt.static_sections
        for i, s in enumerate(statics):
            block: dict[str, Any] = {"type": "text", "text": f"## {s.name}\n{s.content}"}
            last_static = i == len(statics) - 1
            if (s.cache_breakpoint or last_static) and breakpoints < MAX_BREAKPOINTS:
                block["cache_control"] = {"type": "ephemeral"}
                breakpoints += 1
            system.append(block)
        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": req.prompt.dynamic_text()}],
        }
        if req.effort:
            kwargs["output_config"] = {"effort": req.effort}
        return kwargs

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise ProviderError("anthropic SDK not installed") from e
        problems = self.validate_config()
        if problems:
            raise ProviderError("; ".join(problems))

        client = anthropic.AsyncAnthropic(
            api_key=self._api_key(),
            base_url=self.config.resolved_base_url(),
            timeout=float(req.timeout_seconds),
        )
        kwargs = self.build_request(req)
        use_fallback = get_settings().anthropic_refusal_fallback and req.model in FALLBACK_MODELS
        try:
            if use_fallback:
                msg = await client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                )
            else:
                msg = await client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise ProviderError(f"rate limited: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"Anthropic connection error: {e}") from e

        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            raise ProviderRefusal(f"model refused (category={getattr(details, 'category', None)})")

        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        u = msg.usage
        usage = {
            "input_tokens": getattr(u, "input_tokens", 0),
            "output_tokens": getattr(u, "output_tokens", 0),
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        }
        return CompletionResult(
            text=text, model=msg.model, usage=usage, stop_reason=msg.stop_reason
        )
