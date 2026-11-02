"""Cache hit rates for the Prompt Cache page: pipeline runs, Chat and standalone runs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestrator.core.models import ChatSession, Project, Run, Task
from orchestrator.services.usage_service import cache_stats


def usage(inp: int, cached: int, requests: int = 1, **extra) -> dict:
    return {
        "input_tokens": inp,
        "cache_read_input_tokens": cached,
        "output_tokens": 10,
        "requests": requests,
        **extra,
    }


def chat(adapter: str, model: str, u: dict, standalone: bool = False) -> ChatSession:
    return ChatSession(adapter=adapter, model=model, cwd="/tmp", usage=u, standalone=standalone)


def test_cache_stats_group_by_source_harness_and_model(session):
    project = Project(name="p", slug="p", path="/tmp/p")
    session.add(project)
    session.flush()
    task = Task(short_id="T-1", title="t", project_id=project.id)
    session.add(task)
    session.flush()
    session.add(
        Run(
            short_id="R-1",
            task_id=task.id,
            harness_adapter="pi",
            usage=usage(1000, 900, 4, source="harness", model="qwen3.8-flash"),
        )
    )
    session.add(
        Run(
            short_id="R-2",
            task_id=task.id,
            harness_adapter="pi",
            usage=usage(1000, 940, 6, source="harness", model="qwen3.8-flash"),
        )
    )
    # Not harness-reported (a direct provider call) or empty: left out.
    session.add(
        Run(short_id="R-3", task_id=task.id, harness_adapter="pi", usage={"input_tokens": 5})
    )
    session.add(Run(short_id="R-4", task_id=task.id, harness_adapter="pi", usage={}))
    session.add(chat("pi", "qwen3.8-max", usage(4000, 3000, 8)))
    session.add(chat("pi", "qwen3.8-max", usage(1000, 1000, 2)))
    session.add(chat("pi", "qwen3.8-flash", usage(500, 100, 1), standalone=True))
    session.add(chat("opencode", "big-pickle", {}))  # never sent a message
    session.commit()

    rows = cache_stats(session)
    assert [(r["source"], r["harness"], r["model"]) for r in rows] == [
        ("task", "pi", "qwen3.8-flash"),
        ("chat", "pi", "qwen3.8-max"),
        ("standalone", "pi", "qwen3.8-flash"),
    ]
    task, chats, standalone = rows
    assert (task["count"], task["requests"], task["cached_input_tokens"]) == (2, 10, 1840)
    assert task["cache_hit_rate"] == pytest.approx(0.92)
    assert (chats["count"], chats["input_tokens"]) == (2, 5000)
    assert chats["cache_hit_rate"] == pytest.approx(0.8)
    assert standalone["cache_hit_rate"] == pytest.approx(0.2)


def test_cache_stats_endpoint(initialized):
    from orchestrator.api.main import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1:8321") as client:
        r = client.get("/api/cache-stats")
    assert r.status_code == 200 and r.json() == {"rows": []}
