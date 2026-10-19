"""OpenAI-compatible chat-completions provider (Qwen Model Studio, DeepSeek, Z.ai GLM,
Hermes/local servers). Uses the official `openai` SDK with a custom base_url.

Prompt caching: these providers cache by exact prefix where supported, so all static
sections are concatenated into one stable `system` message and dynamic state goes last.
"""

from __future__ import annotations

import os
from typing import Any

from orchestrator.providers.base import (
    BaseProvider,
    CompletionRequest,
    CompletionResult,
    ProviderError,
)


class OpenAICompatibleProvider(BaseProvider):
    kind = "openai_compatible"

    def _api_key(self) -> str | None:
        return os.environ.get(self.config.api_key_env) if self.config.api_key_env else None

    def _base_url(self) -> str | None:
        return self.config.resolved_base_url()

    def validate_config(self) -> list[str]:
        problems = []
        try:
            import openai  # noqa: F401
        except ImportError:
            problems.append("openai SDK not installed")
        if not self._base_url():
            problems.append("base_url is not configured")
        if self.config.api_key_env and not self._api_key():
            problems.append(f"env var {self.config.api_key_env} is not set")
        return problems

    def build_request(self, req: CompletionRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": [
                {"role": "system", "content": req.prompt.static_text()},
                {"role": "user", "content": req.prompt.dynamic_text()},
            ],
        }
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        return kwargs

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        try:
            import openai
        except ImportError as e:  # pragma: no cover
            raise ProviderError("openai SDK not installed") from e
        problems = self.validate_config()
        if problems:
            raise ProviderError("; ".join(problems))
        client = openai.AsyncOpenAI(
            api_key=self._api_key() or "not-needed",
            base_url=self._base_url(),
            timeout=float(req.timeout_seconds),
        )
        try:
            resp = await client.chat.completions.create(**self.build_request(req))
        except openai.APIStatusError as e:
            raise ProviderError(f"API error {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise ProviderError(f"connection error: {e}") from e
        choice = resp.choices[0] if resp.choices else None
        text = (choice.message.content or "") if choice else ""
        usage: dict[str, Any] = {}
        if resp.usage:
            usage = {
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
            }
            details = getattr(resp.usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", None) if details else None
            if cached is not None:
                usage["cache_read_input_tokens"] = cached
        return CompletionResult(text=text, model=resp.model or req.model, usage=usage,
                                stop_reason=choice.finish_reason if choice else None)
