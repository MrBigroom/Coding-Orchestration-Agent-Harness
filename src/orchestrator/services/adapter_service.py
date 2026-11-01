"""Harness adapter management and worker-harness routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.base import AdapterSpec, BaseHarnessAdapter
from orchestrator.adapters.registry import ADAPTER_CLASSES, build_adapter, default_specs
from orchestrator.core.models import HarnessAdapter, Task, TaskRisk

# Templates shipped by earlier versions. A row still holding one of these was never edited
# by the user, so it is upgraded to the current default (with its new capabilities/env).
LEGACY_DEFAULT_ARGS: dict[str, list[list[str]]] = {
    "qwen_code": [["--approval-mode", "auto-edit", "?--model={model}", "-p", "{prompt}"]],
    "deepseek_harness": [["run", "--cwd", "{cwd}", "--prompt-file", "{prompt_file}"]],
    "zcode": [["run", "--cwd", "{cwd}", "--prompt-file", "{prompt_file}"]],
    "opencode": [["run", "?--model={model}", "{prompt}"]],
    "claude_cli": [["-p", "{prompt}", "--permission-mode", "acceptEdits", "?--model={model}"]],
}
LEGACY_DEFAULT_COMMANDS = {"zcode": ["zcode-cli"]}


def _upgrade_unedited(row: HarnessAdapter, spec: AdapterSpec) -> bool:
    if list(row.args_template_json) not in LEGACY_DEFAULT_ARGS.get(row.kind, []):
        return False
    row.args_template_json = list(spec.args_template)
    if row.command in LEGACY_DEFAULT_COMMANDS.get(row.kind, []):
        row.command = spec.command
    row.capabilities_json = {**spec.capabilities, **dict(row.capabilities_json)}
    row.env_vars_json = list(dict.fromkeys([*row.env_vars_json, *spec.env_vars]))
    row.notes = ADAPTER_CLASSES[spec.kind].description
    return True


def seed_defaults(session: Session) -> None:
    rows = {a.name: a for a in session.exec(select(HarnessAdapter))}
    have = set(rows)
    for spec in default_specs():
        if spec.name in rows and _upgrade_unedited(rows[spec.name], spec):
            session.add(rows[spec.name])
        if spec.name not in have:
            session.add(
                HarnessAdapter(
                    name=spec.name,
                    kind=spec.kind,
                    command=spec.command,
                    args_template_json=spec.args_template,
                    env_vars_json=spec.env_vars,
                    timeout_seconds=spec.timeout_seconds,
                    enabled=spec.enabled,
                    capabilities_json=spec.capabilities,
                    notes=ADAPTER_CLASSES[spec.kind].description,
                )
            )
    session.commit()


def spec_from_row(row: HarnessAdapter) -> AdapterSpec:
    return AdapterSpec(
        name=row.name,
        kind=row.kind,
        command=row.command,
        args_template=list(row.args_template_json),
        env_vars=list(row.env_vars_json),
        timeout_seconds=row.timeout_seconds,
        enabled=row.enabled,
        capabilities=dict(row.capabilities_json),
    )


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
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "command": row.command,
        "args_template": row.args_template_json,
        "env_vars": row.env_vars_json,
        "timeout_seconds": row.timeout_seconds,
        "enabled": row.enabled,
        "capabilities": adapter.capabilities,
        "experimental": cls.experimental,
        "detected": adapter.detect(),
        "binary": adapter.resolved_binary(),
        "verify_hint": cls.verify_hint,
        "notes": row.notes,
    }


def available(session: Session, name: str) -> bool:
    row = session.exec(select(HarnessAdapter).where(HarnessAdapter.name == name)).first()
    return bool(row and row.enabled and build_adapter(spec_from_row(row)).detect())


@dataclass
class HarnessChoice:
    adapter: str | None  # None means "direct API worker" (no harness)
    reason: str


# Preferred harnesses per worker model, keyed by model-name prefix (longest match wins),
# ordered by measured token overhead (docs/harness-routing.md, scripts/harness-bench).
# Overridden/extended by the `harness_routing` setting, whose keys may also be exact
# profile names. Unavailable adapters are skipped.
DEFAULT_HARNESS_ROUTES: dict[str, list[str]] = {
    "deepseek": ["deepseek_harness", "pi", "opencode", "aider"],
    "qwen": ["pi", "qwen_code", "opencode", "aider"],
    "kimi": ["pi", "opencode", "aider"],
    "glm": ["pi", "opencode", "aider"],
    "claude": ["claude_cli", "opencode"],
}


def harness_routes(session: Session) -> dict[str, list[str]]:
    from orchestrator.services import settings_service

    return {**DEFAULT_HARNESS_ROUTES, **(settings_service.get(session, "harness_routing") or {})}


def route_for(routes: dict[str, list[str]], profile: str | None, model: str | None) -> list[str]:
    if profile and profile in routes:
        return routes[profile]
    matches = [k for k in routes if model and model.startswith(k)]
    return routes[max(matches, key=len)] if matches else []


def _worker_profile(session: Session, task: Task) -> tuple[str | None, str | None]:
    from orchestrator.providers.base import ProviderError
    from orchestrator.services import provider_service

    try:
        p = provider_service.choose_profile(session, task, "worker").profile
    except ProviderError:
        return None, None
    return p.name, p.model_name


def _by_measured_efficiency(
    session: Session, candidates: list[str], model: str | None
) -> tuple[str, str] | None:
    """Best candidate by weighted tokens per accepted task, once at least two candidates
    have MIN_ACCEPTED_TASKS accepted tasks on this model. Otherwise None (keep order)."""
    from orchestrator.services import usage_service

    stats = usage_service.harness_stats(session, model)
    scored = [
        (s.weighted_per_accepted, name)
        for name in candidates
        if (s := stats.get((name, model))) and s.accepted_tasks >= usage_service.MIN_ACCEPTED_TASKS
    ]
    if len(scored) < 2:
        return None
    best, name = min(scored)
    detail = ", ".join(f"{n} {v:,.0f}" for v, n in sorted(scored))
    return name, f"lowest weighted tokens per accepted task on {model} ({detail})"


def choose_harness(
    session: Session, task: Task, profile: str | None = None, model: str | None = None
) -> HarnessChoice:
    """Routing intent from the product spec. Explicit assignment always wins, then tags,
    then the worker model's preferred harnesses, then OpenCode/Aider."""
    tags = set(task.tags)
    if task.assigned_harness_adapter:
        return HarnessChoice(task.assigned_harness_adapter, "explicitly assigned on task")
    if "mock" in tags or any(t.startswith("mock-") for t in tags):
        return HarnessChoice("mock", "task tagged for the mock harness")
    for tag, name in (("deepseek", "deepseek_harness"), ("glm", "zcode"), ("qwen", "qwen_code")):
        if tag in tags and available(session, name):
            return HarnessChoice(name, f"task tagged `{tag}` and {name} is available")
    if profile is None and model is None:
        profile, model = _worker_profile(session, task)
    candidates = [
        n for n in route_for(harness_routes(session), profile, model) if available(session, n)
    ]
    if candidates:
        learned = _by_measured_efficiency(session, candidates, model)
        if learned:
            return HarnessChoice(*learned)
        return HarnessChoice(
            candidates[0],
            f"preferred harness for {model or profile} (route: {', '.join(candidates)})",
        )
    if available(session, "opencode"):
        return HarnessChoice("opencode", "OpenCode is the preferred general harness")
    if available(session, "aider"):
        return HarnessChoice("aider", "fallback to Aider")
    if task.risk == TaskRisk.low:
        return HarnessChoice(None, "no harness available; low-risk task -> direct API worker")
    return HarnessChoice(None, "no harness available and task is not simple; will block")
