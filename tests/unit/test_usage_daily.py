"""Context windows for the Chat pane, and per-day usage for the Usage page."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.core.context_windows import builtin
from orchestrator.core.models import ModelProfile, Provider, UsageEvent
from orchestrator.services import usage_service
from orchestrator.services.provider_service import _total_input


@pytest.mark.parametrize(
    ("model", "tokens"),
    [
        ("claude-opus-5-5", 1_000_000),
        ("anthropic/claude-sonnet-5-5[1m]", 1_000_000),
        ("opus", 1_000_000),
        ("claude-opus-4-6", 1_000_000),
        ("claude-haiku-4-5", 200_000),
        ("claude-sonnet-4-20250514", 200_000),
        ("qwen3.8-flash", 1_000_000),
        ("deepseek-v4.1-flash-1017", 1_048_576),
        ("glm-5.3", 1_048_576),
        ("mock-worker", None),
        (None, None),
    ],
)
def test_builtin_context_windows(model, tokens):
    assert builtin(model) == tokens


def test_profile_context_window_overrides_the_table(session):
    provider = Provider(name="p", kind="openai_compatible")
    session.add(provider)
    session.flush()
    session.add(
        ModelProfile(
            name="w",
            role="worker",
            provider_id=provider.id,
            model_name="qwen3.8-flash",
            context_window=262_144,
        )
    )
    session.commit()
    assert usage_service.context_window(session, "Qwen3.8-Flash") == 262_144
    assert usage_service.context_window(session, "glm-5.3") == 1_048_576


def test_daily_groups_by_day_and_model_and_fills_the_range(session):
    now = datetime.now(UTC)

    def event(days_ago: int, model: str | None, source: str = "chat", inp: int = 100):
        session.add(
            UsageEvent(
                created_at=now - timedelta(days=days_ago),
                source=source,
                model=model,
                input_tokens=inp,
                cached_input_tokens=inp // 2,
                output_tokens=10,
                requests=1,
            )
        )

    event(0, "a")
    event(0, "a")
    event(0, None)
    event(1, "b", source="task")
    event(40, "a")  # outside the range
    session.commit()

    out = usage_service.daily(session, days=7)
    assert len(out["days"]) == 7
    assert out["days"][-1] == datetime.now().astimezone().date().isoformat()
    by_key = {(r["day"], r["model"]): r for r in out["rows"]}
    today = out["days"][-1]
    assert by_key[(today, "a")]["input_tokens"] == 200
    assert by_key[(today, "a")]["requests"] == 2
    assert (today, "unknown") in by_key
    assert len(out["rows"]) == 3

    tasks_only = usage_service.daily(session, days=7, source="task")
    assert [r["model"] for r in tasks_only["rows"]] == ["b"]


def test_anthropic_usage_counts_cache_in_input():
    u = {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 85}
    assert _total_input(u)["input_tokens"] == 100
    openai_shaped = {"input_tokens": 100, "cache_read_input_tokens": 85}
    assert _total_input(openai_shaped) == openai_shaped
