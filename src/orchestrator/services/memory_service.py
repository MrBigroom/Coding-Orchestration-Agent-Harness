"""Persistent, role-aware shared memory.

SQLite (`memory_entries`) is the index and source of truth; approved entries are rendered
to markdown under ORCHESTRATOR_HOME/memory so they are human-readable and diffable:

  memory/global/{environment,preferences,patterns}.md
  memory/projects/<slug>/{conventions,decisions,gotchas}.md
  memory/roles/{architect,worker,supervisor}.md

Only approved entries are injected into prompts, in deterministic (id) order, as part of
the cached static prefix.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, or_, select

from orchestrator.core.config import get_settings
from orchestrator.core.models import MemoryCategory, MemoryEntry, Role, utcnow
from orchestrator.core.schemas import MemoryCreate, MemorySuggestion, MemoryUpdate
from orchestrator.utils.paths import safe_join
from orchestrator.utils.redaction import redact
from orchestrator.utils.slug import slugify

GLOBAL_SCOPES = ("environment", "preferences", "patterns")
PROJECT_SCOPES = ("conventions", "decisions", "gotchas")
ROLE_FILES = ("architect", "worker", "supervisor")

# Which project scopes each role receives.
ROLE_PROJECT_SCOPES: dict[str, tuple[str, ...]] = {
    Role.architect: ("decisions", "conventions"),
    Role.reviewer: ("decisions", "conventions"),
    Role.worker: ("conventions", "gotchas"),
    Role.agent: ("conventions", "gotchas"),
    Role.supervisor: ("decisions", "gotchas"),
}
# Role memory file used for each role (reviewer shares the architect file).
ROLE_MEMORY_KEY = {Role.reviewer: "architect", Role.agent: "worker"}


class MemoryError_(ValueError):
    pass


def memory_root() -> Path:
    root = get_settings().memory_root
    assert root
    return root


def ensure_layout(project_slugs: list[str] | None = None) -> None:
    root = memory_root()
    for scope in GLOBAL_SCOPES:
        _touch(root / "global" / f"{scope}.md", f"# Global — {scope}\n")
    for r in ROLE_FILES:
        _touch(root / "roles" / f"{r}.md", f"# Role — {r}\n")
    for slug in project_slugs or []:
        for scope in PROJECT_SCOPES:
            _touch(safe_join(root, "projects", slugify(slug)) / f"{scope}.md",
                   f"# Project {slug} — {scope}\n")


def _touch(path: Path, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(header)


def _file_for(entry: MemoryEntry) -> Path:
    root = memory_root()
    if entry.category == MemoryCategory.global_:
        return safe_join(root, "global", f"{slugify(entry.scope)}.md")
    if entry.category == MemoryCategory.project:
        return safe_join(root, "projects", slugify(entry.project_slug or "unknown"),
                         f"{slugify(entry.scope)}.md")
    role = entry.role or entry.scope
    return safe_join(root, "roles", f"{slugify(ROLE_MEMORY_KEY.get(role, role))}.md")


def _validate(entry: MemoryEntry) -> None:
    if entry.category == MemoryCategory.project and not entry.project_slug:
        raise MemoryError_("project memory requires project_slug")
    if entry.category == MemoryCategory.role and not (entry.role or entry.scope in ROLE_FILES):
        raise MemoryError_("role memory requires role")
    if entry.category == MemoryCategory.role and not entry.role:
        entry.role = entry.scope


def render_file(session: Session, path: Path) -> None:
    """Re-render one markdown file from all approved entries that map to it."""
    entries = [e for e in session.exec(select(MemoryEntry).where(MemoryEntry.approved == True))  # noqa: E712
               if _file_for(e) == path]
    entries.sort(key=lambda e: e.id or 0)
    title = path.stem
    body = "\n".join(f"- {e.content.strip()}  <!-- mem:{e.id} -->" for e in entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{body}\n" if body else f"# {title}\n")


def create(session: Session, data: MemoryCreate) -> MemoryEntry:
    entry = MemoryEntry(
        category=data.category.value, scope=data.scope,
        project_slug=slugify(data.project_slug) if data.project_slug else None,
        role=data.role.value if data.role else None, content=redact(data.content.strip()),
        source=data.source, tags_json=sorted(set(data.tags)), approved=data.approved,
    )
    _validate(entry)
    session.add(entry)
    session.commit()
    session.refresh(entry)
    if entry.approved:
        render_file(session, _file_for(entry))
    return entry


def suggest_from_handoff(session: Session, suggestions: list[MemorySuggestion],
                         source: str, project_slug: str | None,
                         role: str = "worker") -> list[MemoryEntry]:
    """Store worker/architect suggestions as unapproved entries for Supervisor review."""
    created = []
    for s in suggestions:
        try:
            scope = slugify(s.scope)
            created.append(create(session, MemoryCreate(
                category=MemoryCategory(s.category), scope=scope, content=s.content,
                project_slug=project_slug if s.category == "project" else None,
                role=Role(scope) if s.category == "role" and scope in Role.__members__ else
                (Role(role) if s.category == "role" else None),
                tags=s.tags, source=source, approved=False,
            )))
        except (ValueError, MemoryError_):
            continue
    return created


def update(session: Session, entry_id: int, data: MemoryUpdate) -> MemoryEntry:
    entry = _get(session, entry_id)
    old_file = _file_for(entry)
    if data.content is not None:
        entry.content = redact(data.content.strip())
    if data.scope is not None:
        entry.scope = data.scope
    if data.tags is not None:
        entry.tags_json = sorted(set(data.tags))
    entry.updated_at = utcnow()
    session.add(entry)
    session.commit()
    if entry.approved:
        render_file(session, old_file)
        render_file(session, _file_for(entry))
    return entry


def approve(session: Session, entry_id: int) -> MemoryEntry:
    entry = _get(session, entry_id)
    entry.approved, entry.rejected, entry.updated_at = True, False, utcnow()
    session.add(entry)
    session.commit()
    render_file(session, _file_for(entry))
    return entry


def reject(session: Session, entry_id: int) -> MemoryEntry:
    entry = _get(session, entry_id)
    was_approved = entry.approved
    entry.approved, entry.rejected, entry.updated_at = False, True, utcnow()
    session.add(entry)
    session.commit()
    if was_approved:
        render_file(session, _file_for(entry))
    return entry


def _get(session: Session, entry_id: int) -> MemoryEntry:
    entry = session.get(MemoryEntry, entry_id)
    if not entry:
        raise KeyError(f"memory entry {entry_id} not found")
    return entry


def list_entries(session: Session, *, category: str | None = None, scope: str | None = None,
                 role: str | None = None, project_slug: str | None = None,
                 status: str | None = None) -> list[MemoryEntry]:
    q = select(MemoryEntry)
    if category:
        q = q.where(MemoryEntry.category == category)
    if scope:
        q = q.where(MemoryEntry.scope == scope)
    if role:
        q = q.where(MemoryEntry.role == role)
    if project_slug:
        q = q.where(MemoryEntry.project_slug == project_slug)
    if status == "approved":
        q = q.where(MemoryEntry.approved == True)  # noqa: E712
    elif status == "pending":
        q = q.where(MemoryEntry.approved == False, MemoryEntry.rejected == False)  # noqa: E712
    elif status == "rejected":
        q = q.where(MemoryEntry.rejected == True)  # noqa: E712
    return list(session.exec(q.order_by(col(MemoryEntry.id))))


def search(session: Session, query: str) -> list[MemoryEntry]:
    like = f"%{query}%"
    q = select(MemoryEntry).where(or_(col(MemoryEntry.content).ilike(like),
                                      col(MemoryEntry.scope).ilike(like)))
    return list(session.exec(q.order_by(col(MemoryEntry.id))))


def pending_count(session: Session) -> int:
    return len(list_entries(session, status="pending"))


# ---------------------------------------------------------------- injection


def for_prompt(session: Session, role: str, project_slug: str | None) -> dict[str, str]:
    """Approved memory for a role, split into the three cached prefix sections."""
    approved = list_entries(session, status="approved")
    role_key = ROLE_MEMORY_KEY.get(role, role)
    glob = [e for e in approved if e.category == MemoryCategory.global_]
    role_mem = [e for e in approved if e.category == MemoryCategory.role
                and ROLE_MEMORY_KEY.get(e.role or "", e.role) == role_key]
    scopes = ROLE_PROJECT_SCOPES.get(role, ())
    proj = [e for e in approved if e.category == MemoryCategory.project
            and project_slug and e.project_slug == slugify(project_slug) and e.scope in scopes]

    def fmt(entries: list[MemoryEntry]) -> str:
        by_scope: dict[str, list[str]] = {}
        for e in entries:
            by_scope.setdefault(e.scope, []).append(e.content.strip())
        return "\n".join(
            f"### {scope}\n" + "\n".join(f"- {c}" for c in items)
            for scope, items in sorted(by_scope.items())
        )

    return {"global": fmt(glob), "role": fmt(role_mem), "project": fmt(proj)}


# ---------------------------------------------------------------- export/import


def export_all(session: Session) -> dict[str, Any]:
    return {
        "version": 1,
        "entries": [
            {k: getattr(e, k) for k in ("category", "scope", "project_slug", "role", "content",
                                        "source", "tags_json", "approved", "rejected")}
            for e in list_entries(session)
        ],
    }


def import_all(session: Session, payload: dict[str, Any]) -> int:
    """Import entries; skips exact duplicates (same category/scope/project/role/content)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise MemoryError_("invalid memory export: expected {'entries': [...]}")
    existing = {(e.category, e.scope, e.project_slug, e.role, e.content)
                for e in list_entries(session)}
    count = 0
    touched: set[Path] = set()
    for raw in payload["entries"]:
        key = (raw.get("category"), raw.get("scope"), raw.get("project_slug"), raw.get("role"),
               redact(str(raw.get("content", "")).strip()))
        if key in existing or not key[4]:
            continue
        entry = MemoryEntry(category=key[0], scope=key[1], project_slug=key[2], role=key[3],
                            content=key[4], source=raw.get("source", "import"),
                            tags_json=list(raw.get("tags_json") or []),
                            approved=bool(raw.get("approved")),
                            rejected=bool(raw.get("rejected")))
        if entry.category not in {c.value for c in MemoryCategory}:
            continue
        try:
            _validate(entry)
        except MemoryError_:
            continue
        session.add(entry)
        existing.add(key)
        count += 1
        if entry.approved:
            touched.add(_file_for(entry))
    session.commit()
    for path in touched:
        render_file(session, path)
    return count


_MD_LINE = re.compile(r"^- (?P<content>.+?)(\s+<!-- mem:\d+ -->)?\s*$")


def import_markdown_tree(session: Session) -> int:
    """Index hand-edited markdown lines that are not yet in SQLite (as pending entries)."""
    root = memory_root()
    known = {e.content for e in list_entries(session)}
    added = 0
    for path in root.rglob("*.md"):
        rel = path.relative_to(root).parts
        for line in path.read_text().splitlines():
            m = _MD_LINE.match(line)
            if not m or m.group("content") in known:
                continue
            content = m.group("content")
            if rel[0] == "global":
                data = MemoryCreate(category=MemoryCategory.global_, scope=slugify(path.stem),
                                    content=content, source="markdown")
            elif rel[0] == "projects" and len(rel) == 3:
                data = MemoryCreate(category=MemoryCategory.project, scope=slugify(path.stem),
                                    project_slug=rel[1], content=content, source="markdown")
            elif rel[0] == "roles" and path.stem in Role.__members__:
                data = MemoryCreate(category=MemoryCategory.role, scope=path.stem,
                                    role=Role(path.stem), content=content, source="markdown")
            else:
                continue
            create(session, data)
            known.add(content)
            added += 1
    return added


def dumps_export(session: Session) -> str:
    return json.dumps(export_all(session), indent=2, default=str)
