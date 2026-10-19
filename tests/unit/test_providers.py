import asyncio

from orchestrator.providers.anthropic_provider import MAX_BREAKPOINTS, AnthropicProvider
from orchestrator.providers.base import (
    BuiltPrompt,
    CompletionRequest,
    PromptSection,
    ProviderConfig,
)
from orchestrator.providers.mock_provider import MockProvider
from orchestrator.providers.openai_compatible_provider import OpenAICompatibleProvider


def _prompt():
    return BuiltPrompt(
        role="architect",
        sections=[
            PromptSection("system", "role", static=True, cache_breakpoint=True),
            PromptSection("global_memory", "env", static=True),
            PromptSection("project_memory", "conv", static=True, cache_breakpoint=True),
            PromptSection("phase", "dod", static=True, cache_breakpoint=True),
            PromptSection("dynamic_state", "diff", static=False),
        ],
    )


def test_anthropic_request_places_cache_breakpoints_before_dynamic():
    p = AnthropicProvider(ProviderConfig(name="a", kind="anthropic"))
    req = p.build_request(
        CompletionRequest(prompt=_prompt(), model="claude-opus-5-5", effort="high", temperature=0.7)
    )
    marked = [b for b in req["system"] if "cache_control" in b]
    assert 1 <= len(marked) <= MAX_BREAKPOINTS
    assert "cache_control" in req["system"][-1]  # last static block is cached
    assert req["messages"][0]["content"].endswith("diff")
    assert req["output_config"] == {"effort": "high"}
    assert "temperature" not in req  # Claude 5.x rejects sampling params


def test_openai_compatible_request_is_prefix_stable():
    p = OpenAICompatibleProvider(
        ProviderConfig(
            name="q", kind="openai_compatible", base_url="env:NOPE|https://example.invalid/v1"
        )
    )
    req = p.build_request(
        CompletionRequest(prompt=_prompt(), model="qwen3.8-flash", temperature=0.2)
    )
    assert req["messages"][0]["role"] == "system" and "diff" not in req["messages"][0]["content"]
    assert req["messages"][1]["content"].endswith("diff") and req["temperature"] == 0.2
    assert p.config.resolved_base_url() == "https://example.invalid/v1"


def test_validate_config_reports_missing_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a = AnthropicProvider(
        ProviderConfig(name="a", kind="anthropic", api_key_env="ANTHROPIC_API_KEY")
    )
    assert any("ANTHROPIC_API_KEY" in p for p in a.validate_config())
    q = OpenAICompatibleProvider(
        ProviderConfig(name="q", kind="openai_compatible", api_key_env="QWEN_API_KEY")
    )
    assert any("base_url" in p for p in q.validate_config())


def test_mock_provider_simulates_review_outcomes():
    m = MockProvider(ProviderConfig(name="m", kind="mock"))

    def run(tags):
        return asyncio.run(
            m.complete(
                CompletionRequest(
                    prompt=_prompt(), model="mock", metadata={"purpose": "review", "tags": tags}
                )
            )
        ).text

    assert '"APPROVED"' in run([])
    assert '"CHANGES_REQUESTED"' in run(["mock-reject"])
    assert "{" not in run(["mock-ambiguous"])
