"""`orchestrator init` / `doctor` logic shared by the CLI and API."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from typing import Any

from sqlmodel import select

from orchestrator.core import db
from orchestrator.core.config import get_settings
from orchestrator.core.db import session_scope
from orchestrator.core.models import HarnessAdapter, Project, Provider
from orchestrator.services import adapter_service, memory_service, provider_service

DEFAULT_GLOBAL_MEMORY = [
    ("environment", "Primary dev environment: CachyOS Linux with Hyprland (Wayland)."),
    ("preferences", "Prefer GUIs on top of headless CLI agents; structured artifacts over chat."),
]


def init() -> dict[str, Any]:
    s = get_settings()
    s.ensure_dirs()
    db.upgrade()
    with session_scope() as session:
        provider_service.seed_defaults(session)
        adapter_service.seed_defaults(session)
        slugs = [p.slug for p in session.exec(select(Project))]
        memory_service.ensure_layout(slugs)
        if not memory_service.list_entries(session, category="global"):
            from orchestrator.core.models import MemoryCategory
            from orchestrator.core.schemas import MemoryCreate

            for scope, content in DEFAULT_GLOBAL_MEMORY:
                memory_service.create(session, MemoryCreate(
                    category=MemoryCategory.global_, scope=scope, content=content,
                    source="init", approved=True))
    return {"home": str(s.home), "db": s.db_url, "revision": db.current_revision()}


def doctor() -> list[dict[str, Any]]:
    s = get_settings()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "", level: str = "error") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail,
                       "level": "ok" if ok else level})

    add("python >= 3.12", sys.version_info >= (3, 12), sys.version.split()[0])
    add("git on PATH", shutil.which("git") is not None)
    add("orchestrator home", s.home.exists(), str(s.home))
    try:
        rev = db.current_revision()
        add("database migrated", rev is not None, f"revision {rev}")
    except Exception as e:  # noqa: BLE001
        add("database migrated", False, str(e))
        return checks
    add("bind host is local", s.is_local_bind, s.bind_host, level="warn")
    if not s.is_local_bind:
        add("token required for non-local bind", bool(s.token),
            "ORCHESTRATOR_TOKEN must be set when binding beyond localhost")
    for mod in ("anthropic", "openai", "apscheduler"):
        add(f"python package {mod}", importlib.util.find_spec(mod) is not None)
    dist = s.frontend_dist
    add("frontend build", bool(dist and dist.exists()) or _default_dist_exists(),
        "fallback static UI will be served" if not _default_dist_exists() else "", level="warn")
    with session_scope() as session:
        for p in session.exec(select(Provider)):
            st = provider_service.provider_status(p)
            if p.kind == "mock":
                continue
            add(f"provider {p.name}", st["configured"] or not p.enabled,
                "disabled" if not p.enabled else "; ".join(st["problems"]) or "configured",
                level="warn")
        for a in session.exec(select(HarnessAdapter)):
            d = adapter_service.describe(a)
            add(f"adapter {a.name}", d["detected"] or not a.enabled,
                ("disabled" if not a.enabled else
                 (d["binary"] or f"`{a.command}` not found")) +
                (" [experimental]" if d["experimental"] else ""), level="warn")
    return checks


def _default_dist_exists() -> bool:
    from orchestrator.api.main import frontend_dist_dir

    return frontend_dist_dir() is not None
