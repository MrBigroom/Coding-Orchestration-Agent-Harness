"""Model provider interface (frozen contract after baseline approval)."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptSection:
    name: str
    content: str
    static: bool
    # Place a cache breakpoint after this section (Anthropic). Ignored by other providers.
    cache_breakpoint: bool = False


@dataclass
class BuiltPrompt:
    role: str
    sections: list[PromptSection]
    # Additive: what the builder had to drop to fit its budgets. `review_service` archives this
    # next to the exact prompt text so a truncated reviewer input can never look complete.
    truncations: list[dict[str, Any]] = field(default_factory=list)
    # Additive (T-16 DoD 3): the budgeted diff text actually rendered into this prompt. Diff
    # completeness is audited against THIS, not against the archived full patch.diff.
    rendered_diff: str = ""
    # Approved memory demoted by the per-section memory budget (id/bucket/scope/tokens/reason).
    # Never rendered into the prompt; surfaced in prompt preview as a warning.
    memory_exclusions: list[dict[str, Any]] = field(default_factory=list)
    # Warnings raised when a memory section approaches its cap.
    memory_warnings: list[str] = field(default_factory=list)

    @property
    def static_sections(self) -> list[PromptSection]:
        return [s for s in self.sections if s.static and s.content.strip()]

    @property
    def dynamic_sections(self) -> list[PromptSection]:
        return [s for s in self.sections if not s.static and s.content.strip()]

    def static_text(self) -> str:
        return "\n\n".join(f"## {s.name}\n{s.content}" for s in self.static_sections)

    def dynamic_text(self) -> str:
        parts = [f"## {s.name}\n{s.content}" for s in self.dynamic_sections]
        return "\n\n".join(parts) or "(no dynamic state)"


@dataclass
class CompletionRequest:
    prompt: BuiltPrompt
    model: str
    max_tokens: int = 16_000
    temperature: float | None = None
    effort: str | None = None
    timeout_seconds: int = 600
    # Free-form hints (purpose, tags, task id). Never contains secrets.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionResult:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None


class ProviderError(RuntimeError):
    pass


class ProviderRefusal(ProviderError):
    pass


@dataclass
class ProviderConfig:
    name: str
    kind: str
    base_url: str | None = None
    api_key_env: str | None = None

    def resolved_base_url(self) -> str | None:
        """`env:VAR` or `env:VAR|default` lets base URLs live in .env instead of the DB."""
        url = self.base_url or ""
        if url.startswith("env:"):
            var, _, default = url[4:].partition("|")
            return os.environ.get(var) or default or None
        return url or None


class BaseProvider(ABC):
    kind: str = "base"

    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    def validate_config(self) -> list[str]:
        """Return a list of configuration problems (empty if OK). Must not do network I/O."""

    def detect(self) -> bool:
        """True if the provider looks usable locally (config present, SDK importable)."""
        return not self.validate_config()

    async def health(self) -> dict[str, Any]:
        problems = self.validate_config()
        return {"ok": not problems, "problems": problems, "network_checked": False}

    @abstractmethod
    async def complete(self, req: CompletionRequest) -> CompletionResult: ...

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        """Optional; default yields the full completion once."""
        res = await self.complete(req)
        yield res.text
