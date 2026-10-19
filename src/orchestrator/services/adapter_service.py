"""Harness adapter management and worker-harness routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.base import AdapterSpec, BaseHarnessAdapter
from orchestrator.adapters.registry import ADAPTER_CLASSES, build_adapter, default_specs
from orchestrator.core.models import HarnessAdapter, Task, TaskRisk


def seed_defaults(session: Session) -> None:
    have = {a.name for a in session.exec(select(HarnessAdapter))}
    for spec in default_specs():
        if spec.name not in have:
            session.add(HarnessAdapter(
                name=spec.name, kind=spec.kind, command=spec.command,
                args_template_json=spec.args_template, env_vars_json=spec.env_vars,
                timeout_seconds=spec.timeout_seconds, enabled=spec.enabled,
                capabilities_json=spec.capabilities,
                notes=ADAPTER_CLASSES[spec.kind].description,
            ))
    session.commit()


def spec_from_row(row: HarnessAdapter) -> AdapterSpec:
    return AdapterSpec(name=row.name, kind=row.kind, command=row.command,
                       args_template=list(row.args_template_json),
                       env_vars=list(row.env_vars_json), timeout_seconds=row.timeout_seconds,
                       enabled=row.enabled, capabilities=dict(row.capabilities_json))


def get_row(session: Session, name: str) -> HarnessAdapter:
    row = session.exec(select(HarnessAdapter).where(HarnessAdapter.name == name)).first()
    if not row:
        raise KeyError(f"adapter {name!r} not found")
    return row


def get_adapter(session: Session, name: str) -> BaseHarnessAdapter:
    return build_adapter(spec_from_row(get_row(session, name)))


def describe(row: HarnessAdapter) -> dict[str, Any]:
    adapter = build_adapter(spec_from_row(row))
    cls = ADAPTER_CLASSES[row.kind]
    return {
        "id": row.id, "name": row.name, "kind": row.kind, "command": row.command,
        "args_template": row.args_template_json, "env_vars": row.env_vars_json,
        "timeout_seconds": row.timeout_seconds, "enabled": row.enabled,
        "capabilities": adapter.capabilities, "experimental": cls.experimental,
        "detected": adapter.detect(), "binary": adapter.resolved_binary(),
        "verify_hint": cls.verify_hint, "notes": row.notes,
    }


def available(session: Session, name: str) -> bool:
    row = session.exec(select(HarnessAdapter).where(HarnessAdapter.name == name)).first()
    return bool(row and row.enabled and build_adapter(spec_from_row(row)).detect())


@dataclass
class HarnessChoice:
    adapter: str | None  # None means "direct API worker" (no harness)
    reason: str


def choose_harness(session: Session, task: Task) -> HarnessChoice:
    """Routing intent from the product spec. Explicit assignment always wins."""
    tags = set(task.tags)
    if task.assigned_harness_adapter:
        return HarnessChoice(task.assigned_harness_adapter, "explicitly assigned on task")
    if "mock" in tags or any(t.startswith("mock-") for t in tags):
        return HarnessChoice("mock", "task tagged for the mock harness")
    for tag, name in (("deepseek", "deepseek_harness"), ("glm", "zcode"), ("qwen", "qwen_code")):
        if tag in tags and available(session, name):
            return HarnessChoice(name, f"task tagged `{tag}` and {name} is available")
    if available(session, "opencode"):
        return HarnessChoice("opencode", "OpenCode is the preferred general harness")
    if available(session, "aider"):
        return HarnessChoice("aider", "fallback to Aider")
    if task.risk == TaskRisk.low:
        return HarnessChoice(None, "no harness available; low-risk task -> direct API worker")
    return HarnessChoice(None, "no harness available and task is not simple; will block")
