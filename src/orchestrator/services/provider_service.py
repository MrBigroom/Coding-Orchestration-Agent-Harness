"""Provider/model-profile management, routing by role, and structured completions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlmodel import Session, select

from orchestrator.core.models import ModelProfile, Provider, Role, Task, TaskKind, TaskSource
from orchestrator.core.security import env_is_set
from orchestrator.providers.base import (
    BuiltPrompt,
    CompletionRequest,
    CompletionResult,
    PromptSection,
    ProviderConfig,
    ProviderError,
)
from orchestrator.providers.registry import build_provider
from orchestrator.utils.json_output import JSONExtractionError, json_instruction, parse_model

M = TypeVar("M", bound=BaseModel)

# Default base URL for Alibaba Model Studio Token Plan (OpenAI-compatible). The user chose
# Token Plan as default; see docs/providers.md for the ToS caveat about automated use.
TOKEN_PLAN_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

DEFAULT_PROVIDERS: list[dict[str, Any]] = [
    {"name": "mock", "kind": "mock", "notes": "Offline deterministic provider (demo/tests)."},
    {"name": "anthropic", "kind": "anthropic", "base_url": "env:ANTHROPIC_BASE_URL",
     "api_key_env": "ANTHROPIC_API_KEY", "notes": "Official Anthropic API (API key auth)."},
    {"name": "qwen-token-plan", "kind": "openai_compatible", "base_url": f"env:QWEN_OPENAI_BASE_URL|{TOKEN_PLAN_BASE_URL}",
     "api_key_env": "QWEN_API_KEY",
     "notes": "Alibaba Model Studio (Token Plan by default). WARNING: Token Plan terms restrict "
              "use to interactive coding tools and forbid automated scripts/backends; "
              "violations can suspend the plan. Point QWEN_OPENAI_BASE_URL at the "
              "pay-as-you-go endpoint to avoid this."},
    {"name": "deepseek", "kind": "openai_compatible", "base_url": "env:DEEPSEEK_BASE_URL|https://api.deepseek.com",
     "api_key_env": "DEEPSEEK_API_KEY", "enabled": False,
     "notes": "Direct DeepSeek API (model `deepseek-flash` serves V4.1-Flash)."},
    {"name": "glm", "kind": "openai_compatible", "base_url": "env:GLM_OPENAI_BASE_URL|https://api.z.ai/api/paas/v4",
     "api_key_env": "GLM_API_KEY", "enabled": False,
     "notes": "Direct Z.ai API. Coding Plan uses https://api.z.ai/api/coding/paas/v4."},
    {"name": "hermes", "kind": "openai_compatible", "base_url": "env:HERMES_OPENAI_BASE_URL",
     "api_key_env": "HERMES_API_KEY", "enabled": False,
     "notes": "Optional Nous Hermes / local OpenAI-compatible server. Never a default."},
]

# (name, role, provider, model, effort, enabled, notes). Model IDs are editable placeholders.
DEFAULT_PROFILES: list[tuple[str, str, str, str, str | None, bool, str]] = [
    ("architect-opus", "architect", "anthropic", "claude-opus-5-5", "high", True,
     "Planning, gate review, high-risk final review. Reserve for architect work."),
    ("supervisor-qwen-max", "supervisor", "qwen-token-plan", "qwen3.8-max", None, True,
     "Primary supervisor (recommendation-only LLM)."),
    ("supervisor-sonnet", "supervisor", "anthropic", "claude-sonnet-5-5", "medium", True,
     "Alternate supervisor."),
    ("worker-qwen-flash", "worker", "qwen-token-plan", "qwen3.8-flash", None, True,
     "Default cheap worker / direct-API fallback."),
    ("worker-deepseek-flash", "worker", "qwen-token-plan", "deepseek-v4.1-flash", None, True,
     "DeepSeek worker via Token Plan (direct API model id would be `deepseek-flash`)."),
    ("worker-glm", "worker", "qwen-token-plan", "glm-5.3", None, True, "GLM worker via Token Plan."),
    ("agent-flash", "agent", "qwen-token-plan", "qwen3.8-flash", None, True,
     "Background/cron agent tasks."),
    ("hermes-local", "agent", "hermes", "hermes", None, False,
     "Optional; only used when explicitly selected."),
    ("mock-architect", "architect", "mock", "mock-architect", None, True, "Offline."),
    ("mock-supervisor", "supervisor", "mock", "mock-supervisor", None, True, "Offline."),
    ("mock-worker", "worker", "mock", "mock-worker", None, True, "Offline."),
    ("mock-agent", "agent", "mock", "mock-agent", None, True, "Offline."),
]


def seed_defaults(session: Session) -> None:
    existing = {p.name for p in session.exec(select(Provider))}
    for spec in DEFAULT_PROVIDERS:
        if spec["name"] not in existing:
            session.add(Provider(**{"enabled": True, **spec}))
    session.commit()
    providers = {p.name: p for p in session.exec(select(Provider))}
    have = {m.name for m in session.exec(select(ModelProfile))}
    for name, role, prov, model, effort, enabled, notes in DEFAULT_PROFILES:
        if name not in have:
            session.add(ModelProfile(name=name, role=role, provider_id=providers[prov].id,
                                     model_name=model, effort=effort, enabled=enabled,
                                     notes=notes))
    session.commit()


def provider_config(p: Provider) -> ProviderConfig:
    return ProviderConfig(name=p.name, kind=p.kind, base_url=p.base_url,
                          api_key_env=p.api_key_env)


def provider_status(p: Provider) -> dict[str, Any]:
    impl = build_provider(provider_config(p))
    problems = impl.validate_config()
    base = provider_config(p).resolved_base_url()
    return {
        "id": p.id, "name": p.name, "kind": p.kind, "enabled": p.enabled,
        "base_url": p.base_url, "base_url_resolved": bool(base) or p.kind != "openai_compatible",
        "api_key_env": p.api_key_env, "api_key_set": env_is_set(p.api_key_env),
        "configured": not problems, "problems": problems, "notes": p.notes,
    }


def get_profile(session: Session, name: str) -> ModelProfile:
    prof = session.exec(select(ModelProfile).where(ModelProfile.name == name)).first()
    if not prof:
        raise KeyError(f"model profile {name!r} not found")
    return prof


def _first(session: Session, *names: str) -> ModelProfile | None:
    for n in names:
        p = session.exec(select(ModelProfile).where(ModelProfile.name == n)).first()
        if p and p.enabled:
            return p
    return None


@dataclass
class ProfileChoice:
    profile: ModelProfile
    reason: str


def choose_profile(session: Session, task: Task | None, role: str) -> ProfileChoice:
    """Routing intent from the product spec. Explicit assignment always wins."""
    tags = set(task.tags) if task else set()
    if task and task.assigned_provider_profile and role in (Role.worker, Role.agent):
        return ProfileChoice(get_profile(session, task.assigned_provider_profile),
                             "explicitly assigned on task")
    if "mock" in tags:
        p = _first(session, f"mock-{'architect' if role == Role.reviewer else role}")
        if p:
            return ProfileChoice(p, "task tagged `mock`")
    if role in (Role.architect, Role.reviewer):
        p = _first(session, "architect-opus")
        if p:
            return ProfileChoice(p, "architect/review uses the Opus profile")
    if role == Role.supervisor:
        p = _first(session, "supervisor-qwen-max", "supervisor-sonnet")
        if p:
            return ProfileChoice(p, "supervisor default (qwen3.8-max, Sonnet alternate)")
    escalated = bool(task and (task.source == TaskSource.escalation or "escalate" in tags))
    if role == Role.agent or (task and task.kind in (TaskKind.agent, TaskKind.maintenance)):
        if escalated:
            p = _first(session, "architect-opus")
            if p:
                return ProfileChoice(p, "agent task explicitly escalated")
        if "hermes" in tags:
            p = _first(session, "hermes-local")
            if p:
                return ProfileChoice(p, "explicitly tagged `hermes`")
        p = _first(session, "agent-flash", "worker-qwen-flash")
        if p:
            return ProfileChoice(p, "background/agent tasks prefer flash-class models")
    for tag, name in (("deepseek", "worker-deepseek-flash"), ("glm", "worker-glm"),
                      ("qwen", "worker-qwen-flash")):
        if tag in tags:
            p = _first(session, name)
            if p:
                return ProfileChoice(p, f"task tagged `{tag}`")
    p = _first(session, "worker-qwen-flash")
    if p:
        return ProfileChoice(p, "default worker profile")
    any_role = session.exec(select(ModelProfile).where(ModelProfile.role == role,
                                                       ModelProfile.enabled == True)).first()  # noqa: E712
    if any_role:
        return ProfileChoice(any_role, f"first enabled {role} profile")
    raise ProviderError(f"no enabled model profile for role {role}")


async def complete(session: Session, profile: ModelProfile, prompt: BuiltPrompt,
                   metadata: dict[str, Any] | None = None) -> CompletionResult:
    prov = session.get(Provider, profile.provider_id)
    if not prov:
        raise ProviderError(f"provider for profile {profile.name} missing")
    if not prov.enabled:
        raise ProviderError(f"provider {prov.name} is disabled")
    impl = build_provider(provider_config(prov))
    req = CompletionRequest(prompt=prompt, model=profile.model_name,
                            max_tokens=profile.max_tokens, temperature=profile.temperature,
                            effort=profile.effort, timeout_seconds=profile.timeout_seconds,
                            metadata=metadata or {})
    return await impl.complete(req)


async def complete_json(session: Session, profile: ModelProfile, prompt: BuiltPrompt,
                        model: type[M], metadata: dict[str, Any] | None = None,
                        ) -> tuple[M, CompletionResult]:
    """Request JSON, validate with Pydantic, retry once with the error, then fail clearly."""
    prompt.sections.append(PromptSection("output_format", json_instruction(model), static=False))
    res = await complete(session, profile, prompt, metadata)
    try:
        return parse_model(res.text, model), res
    except JSONExtractionError as first_err:
        prompt.sections.append(PromptSection(
            "correction",
            f"Your previous output was invalid: {first_err}. Previous output:\n"
            f"{res.text[:4000]}\nReturn only the corrected JSON object.",
            static=False))
        res2 = await complete(session, profile, prompt, metadata)
        try:
            return parse_model(res2.text, model), res2
        except JSONExtractionError as e:
            raise ProviderError(f"provider returned invalid JSON twice: {e}") from e


def key_env_report() -> dict[str, bool]:
    names = ["ANTHROPIC_API_KEY", "QWEN_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY",
             "HERMES_API_KEY"]
    return {n: bool(os.environ.get(n)) for n in names}
