"""Context windows (max prompt tokens) of models the orchestrator ships profiles for.

A model profile's `context_window` overrides this table. Unknown models get None, and the
Chat pane then shows the tokens in context without a limit. Figures as of 2026-11; the
non-Claude ones come from provider listings, so edit the profile if yours differ.
"""

from __future__ import annotations

import re

# First match wins, so narrower patterns go first.
KNOWN: list[tuple[re.Pattern[str], int]] = [
    # Claude 4.5 and older (and Haiku 4.5) are 200K; every later Claude model is 1M.
    (re.compile(r"claude-(opus|sonnet|haiku)-4(-[015])?(-\d{8})?$|claude-3"), 200_000),
    (re.compile(r"claude-|^(fable|opus|sonnet|haiku)$"), 1_000_000),
    (re.compile(r"qwen3\.8"), 1_000_000),
    (re.compile(r"deepseek-v4"), 1_048_576),
    (re.compile(r"glm-5\.3"), 1_048_576),
]


def normalize(model: str) -> str:
    """`anthropic/claude-opus-5-5[1m]` -> `claude-opus-5-5`."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    name = name.removeprefix("anthropic.")
    return re.sub(r"\[.*\]$", "", name)


def builtin(model: str | None) -> int | None:
    if not model:
        return None
    name = normalize(model)
    for pattern, tokens in KNOWN:
        if pattern.search(name):
            return tokens
    return None
