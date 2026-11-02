"""Token usage per harness run, and the per-(harness, model) efficiency stats that harness
routing learns from.

Usage is parsed from the harness's own stdout (see adapters.common.parse_usage) and stored
in `runs.usage` (existing JSON column) with `source: "harness"` and the model it ran.

Efficiency metric: weighted tokens spent on a (harness, model) pair, over *all* its runs
(failed runs and CHANGES_REQUESTED rounds included), divided by the number of distinct
tasks it got accepted. Weights approximate relative credit cost on Model Studio: cache
hits cost 20% of input (implicit cache) and output costs ~3x input (qwen3.8-max lists
$2 / $6 per 1M tokens). They rank harnesses; they are not a bill.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.adapters.common import parse_usage
from orchestrator.core.models import ChatSession, Run, Task, TaskStatus

CACHED_INPUT_WEIGHT = 0.2
OUTPUT_WEIGHT = 3.0
MIN_ACCEPTED_TASKS = 3  # samples needed before stats may override the configured order


def harness_usage(kind: str, stdout: str, model: str | None) -> dict[str, Any] | None:
    usage = parse_usage(kind, stdout)
    if usage is None:
        return None
    return {**usage, "source": "harness", "harness": kind, "model": model}


def weighted_tokens(usage: dict[str, Any]) -> float:
    cached = usage.get("cache_read_input_tokens", 0) or 0
    uncached = usage.get("uncached_input_tokens")
    if uncached is None:
        uncached = max((usage.get("input_tokens", 0) or 0) - cached, 0)
    out = usage.get("output_tokens", 0) or 0
    return uncached + CACHED_INPUT_WEIGHT * cached + OUTPUT_WEIGHT * out


@dataclass
class PairStats:
    harness: str
    model: str | None
    runs: int = 0
    weighted_tokens: float = 0.0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    accepted_tasks: int = 0

    @property
    def cache_hit_rate(self) -> float | None:
        return self.cached_input_tokens / self.input_tokens if self.input_tokens else None

    @property
    def weighted_per_accepted(self) -> float | None:
        return self.weighted_tokens / self.accepted_tasks if self.accepted_tasks else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "model": self.model,
            "runs": self.runs,
            "accepted_tasks": self.accepted_tasks,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit_rate": self.cache_hit_rate,
            "weighted_tokens_per_accepted_task": self.weighted_per_accepted,
        }


def harness_stats(
    session: Session, model: str | None = None
) -> dict[tuple[str, str | None], PairStats]:
    runs = session.exec(select(Run).where(col(Run.harness_adapter).is_not(None))).all()
    stats: dict[tuple[str, str | None], PairStats] = {}
    accepted_by_pair: dict[tuple[str, str | None], set[int]] = {}
    accepted_ids = {
        t.id for t in session.exec(select(Task).where(Task.status == TaskStatus.accepted))
    }
    for r in runs:
        u = r.usage or {}
        if u.get("source") != "harness":
            continue
        key = (r.harness_adapter or "", u.get("model"))
        if model is not None and key[1] != model:
            continue
        s = stats.setdefault(key, PairStats(*key))
        s.runs += 1
        s.weighted_tokens += weighted_tokens(u)
        s.input_tokens += u.get("input_tokens", 0) or 0
        s.cached_input_tokens += u.get("cache_read_input_tokens", 0) or 0
        s.output_tokens += u.get("output_tokens", 0) or 0
        if r.task_id in accepted_ids:
            accepted_by_pair.setdefault(key, set()).add(r.task_id)
    for key, tasks in accepted_by_pair.items():
        stats[key].accepted_tasks = len(tasks)
    return stats


# Where cache stats come from: harness runs of pipeline tasks, Chat sessions, and
# standalone runs (which are chat sessions nobody steers).
CACHE_SOURCES = ("task", "chat", "standalone")


def cache_stats(session: Session) -> list[dict[str, Any]]:
    """Prompt-cache hit rate per (source, harness, model), for the Prompt Cache page.

    A chat session is counted under the model it ended on, so one that switched models
    mid-way is attributed to the last one."""
    rows: dict[tuple[str, str, str | None], dict[str, Any]] = {}

    def add(key: tuple[str, str, str | None], usage: dict[str, Any]) -> None:
        row = rows.setdefault(
            key,
            {
                "source": key[0],
                "harness": key[1],
                "model": key[2],
                "count": 0,
                "requests": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
            },
        )
        row["count"] += 1
        row["requests"] += usage.get("requests", 0) or 0
        row["input_tokens"] += usage.get("input_tokens", 0) or 0
        row["cached_input_tokens"] += usage.get("cache_read_input_tokens", 0) or 0
        row["output_tokens"] += usage.get("output_tokens", 0) or 0

    for run in session.exec(select(Run).where(col(Run.harness_adapter).is_not(None))):
        u = run.usage or {}
        if u.get("source") == "harness" and u.get("input_tokens"):
            add(("task", run.harness_adapter or "", u.get("model")), u)
    for chat in session.exec(select(ChatSession)):
        u = chat.usage or {}
        if u.get("input_tokens"):
            add(("standalone" if chat.standalone else "chat", chat.adapter, chat.model), u)
    for row in rows.values():
        row["cache_hit_rate"] = row["cached_input_tokens"] / row["input_tokens"]
    order = {source: i for i, source in enumerate(CACHE_SOURCES)}
    return sorted(
        rows.values(), key=lambda r: (order[r["source"]], -r["input_tokens"], r["harness"])
    )
