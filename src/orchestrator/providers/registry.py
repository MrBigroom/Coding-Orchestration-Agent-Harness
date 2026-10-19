"""Provider kind -> implementation registry."""

from __future__ import annotations

from orchestrator.providers.anthropic_provider import AnthropicProvider
from orchestrator.providers.base import BaseProvider, ProviderConfig
from orchestrator.providers.claude_cli_provider import ClaudeCliProvider
from orchestrator.providers.mock_provider import MockProvider
from orchestrator.providers.openai_compatible_provider import OpenAICompatibleProvider

PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    "mock": MockProvider,
    "anthropic": AnthropicProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "claude_cli": ClaudeCliProvider,
}


def build_provider(config: ProviderConfig) -> BaseProvider:
    try:
        cls = PROVIDER_CLASSES[config.kind]
    except KeyError as e:
        raise ValueError(f"unknown provider kind: {config.kind}") from e
    return cls(config)
