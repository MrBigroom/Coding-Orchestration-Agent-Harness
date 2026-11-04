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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, or_, select

from orchestrator.core.config import get_settings
from orchestrator.core.models import MemoryCategory, MemoryEntry, Role, utcnow
from orchestrator.core.schemas import MemoryCreate, MemorySuggestion, MemoryUpdate
from orchestrator.utils.memory_text import is_rot_prone
from orchestrator.utils.paths import safe_join
from orchestrator.utils.redaction import redact
from orchestrator.utils.slug import slugify
from orchestrator.utils.truncate import estimate_tokens

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

# Injection buckets -> (setting name for the cap).
MEMORY_BUDGETS = {
    "global": "max_memory_tokens_global",
    "role": "max_memory_tokens_role",
    "project": "max_memory_tokens_project",
}


def memory_budgets() -> dict[str, int]:
    s = get_settings()
    if not s.memory_budget_enabled:
        return {k: 0 for k in MEMORY_BUDGETS}
    return {k: int(getattr(s, attr)) for k, attr in MEMORY_BUDGETS.items()}


def memory_budgets_enabled() -> bool:
    return get_settings().memory_budget_enabled


@dataclass
class MemorySelection:
    """What gets injected, and what the budget left out (never silently)."""

    sections: dict[str, str] = field(default_factory=dict)  # global/role/project -> text
    used: dict[str, int] = field(default_factory=dict)  # bucket -> rendered tokens
    budgets: dict[str, int] = field(default_factory=dict)
    excluded: list[dict[str, Any]] = field(default_factory=list)  # id/bucket/reason/tokens
    warnings: list[str] = field(default_factory=list)  # approaching a cap

    @property
    def total_tokens(self) -> int:
        return sum(self.used.values())

    def headroom(self) -> dict[str, float]:
        """Fraction of each cap in use (0 when the cap is disabled)."""
        return {
            b: (round(self.used.get(b, 0) / v, 3) if v > 0 else 0.0)
            for b, v in self.budgets.items()
        }


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
            _touch(
                safe_join(root, "projects", slugify(slug)) / f"{scope}.md",
                f"# Project {slug} — {scope}\n",
            )


def _touch(path: Path, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(header)


def _file_for(entry: MemoryEntry) -> Path:
    root = memory_root()
    if entry.category == MemoryCategory.global_:
        return safe_join(root, "global", f"{slugify(entry.scope)}.md")
    if entry.category == MemoryCategory.project:
        return safe_join(
            root, "projects", slugify(entry.project_slug or "unknown"), f"{slugify(entry.scope)}.md"
        )
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
    entries = [
        e
        for e in session.exec(select(MemoryEntry).where(MemoryEntry.approved == True))  # noqa: E712
        if _file_for(e) == path
    ]
    entries.sort(key=lambda e: e.id or 0)
    title = path.stem
    body = "\n".join(f"- {e.content.strip()}  <!-- mem:{e.id} -->" for e in entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{body}\n" if body else f"# {title}\n")


def create(session: Session, data: MemoryCreate) -> MemoryEntry:
    entry = MemoryEntry(
        category=data.category.value,
        scope=data.scope,
        project_slug=slugify(data.project_slug) if data.project_slug else None,
        role=data.role.value if data.role else None,
        content=redact(data.content.strip()),
        source=data.source,
        tags_json=sorted(set(data.tags)),
        approved=data.approved,
    )
    _validate(entry)
    session.add(entry)
    session.commit()
    session.refresh(entry)
    if entry.approved:
        render_file(session, _file_for(entry))
    return entry


def suggest_from_handoff(
    session: Session,
    suggestions: list[MemorySuggestion],
    source: str,
    project_slug: str | None,
    role: str = "worker",
) -> list[MemoryEntry]:
    """Store worker/architect suggestions as unapproved entries for Supervisor review."""
    created = []
    existing = {
        (e.category, e.scope, e.project_slug, e.content.strip()) for e in list_entries(session)
    }
    for s in suggestions:
        key = (
            s.category,
            slugify(s.scope),
            slugify(project_slug) if s.category == "project" and project_slug else None,
            redact(s.content.strip()),
        )
        if key in existing:
            continue
        existing.add(key)
        try:
            scope = slugify(s.scope)
            created.append(
                create(
                    session,
                    MemoryCreate(
                        category=MemoryCategory(s.category),
                        scope=scope,
                        content=s.content,
                        project_slug=project_slug if s.category == "project" else None,
                        role=Role(scope)
                        if s.category == "role" and scope in Role.__members__
                        else (Role(role) if s.category == "role" else None),
                        tags=s.tags,
                        source=source,
                        approved=False,
                    ),
                )
            )
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


def list_entries(
    session: Session,
    *,
    category: str | None = None,
    scope: str | None = None,
    role: str | None = None,
    project_slug: str | None = None,
    status: str | None = None,
) -> list[MemoryEntry]:
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
    q = select(MemoryEntry).where(
        or_(col(MemoryEntry.content).ilike(like), col(MemoryEntry.scope).ilike(like))
    )
    return list(session.exec(q.order_by(col(MemoryEntry.id))))


def pending_count(session: Session) -> int:
    return len(list_entries(session, status="pending"))


# ---------------------------------------------------------------- injection


def for_prompt(session: Session, role: str, project_slug: str | None) -> dict[str, str]:
    """Approved memory for a role, split into the three cached prefix sections.

    Convenience wrapper around `select_for_prompt`; use the latter when you need the
    exclusion report (prompt preview, `orchestrator memory triage`).
    """
    return select_for_prompt(session, role, project_slug).sections


def _keep_priority(entry: MemoryEntry) -> tuple[int, int]:
    """Higher = keep. Durable entries outrank task/round/SHA-shaped ones; within a class,
    newer (higher id) outranks older. Content-derived only: no clock, no file access, so the
    rendered text stays byte-identical across calls and does not churn the prompt cache."""
    return (0 if is_rot_prone(entry.content) else 1, entry.id or 0)


def _line_tokens(content: str) -> int:
    return estimate_tokens(f"- {content.strip()}\n")


def _select_bucket(
    bucket: str, entries: list[MemoryEntry], budget: int
) -> tuple[list[MemoryEntry], list[dict[str, Any]]]:
    """Keep entries within `budget` tokens; return (kept sorted by id, excluded with reasons).

    `budget <= 0` disables the cap. Rule, stated so it is reviewable: if the bucket already
    fits, everything is injected. If it does not, demote until it fits -- first the
    rot-prone entries (task/round/SHA-shaped) oldest-first, then the rest oldest-first.
    The newest entries are therefore never the ones silently dropped, and every exclusion
    carries a reason that prompt preview and `orchestrator memory triage` show.
    """
    cost = {e.id: _line_tokens(e.content) for e in entries}
    total = sum(cost.values())
    if budget <= 0 or total <= budget:
        return list(entries), []

    demotion_order = sorted(entries, key=_keep_priority)
    kept_ids = {e.id for e in entries}
    excluded: list[dict[str, Any]] = []
    for entry in demotion_order:
        if total <= budget:
            break
        total -= cost[entry.id]
        kept_ids.discard(entry.id)
        prone = is_rot_prone(entry.content)
        excluded.append(
            {
                "id": entry.id,
                "bucket": bucket,
                "scope": entry.scope,
                "tokens": cost[entry.id],
                "reason": (
                    f"rot-prone (names a task/round/SHA) and the {bucket} section is over "
                    f"its budget of {budget} tokens"
                    if prone
                    else f"oldest of the remaining durable entries while the {bucket} section "
                    f"is over its budget of {budget} tokens"
                ),
            }
        )
    kept = [e for e in entries if e.id in kept_ids]
    kept.sort(key=lambda e: e.id or 0)
    return kept, excluded


def select_for_prompt(
    session: Session,
    role: str,
    project_slug: str | None,
    *,
    budgets: dict[str, int] | None = None,
    also_approved: list[MemoryEntry] | None = None,
) -> MemorySelection:
    """Approved memory for a role, capped per injected section (see MEMORY_BUDGETS).

    `also_approved` is a what-if list of unapproved entries treated as approved for the
    calculation only (used by `orchestrator memory triage` to price a batch). Nothing is
    written.

    Memory is a static prompt section, so `prompt_builder.enforce_budget` can never trim it;
    uncapped growth squeezes diff/handoff/verification evidence instead. Capping happens here
    so the squeeze is visible, reviewable and reported.
    """
    budgets = budgets if budgets is not None else memory_budgets()
    approved = list_entries(session, status="approved")
    if also_approved:
        approved = approved + list(also_approved)
    role_key = ROLE_MEMORY_KEY.get(role, role)
    glob = [e for e in approved if e.category == MemoryCategory.global_]
    role_mem = [
        e
        for e in approved
        if e.category == MemoryCategory.role
        and ROLE_MEMORY_KEY.get(e.role or "", e.role) == role_key
    ]
    scopes = ROLE_PROJECT_SCOPES.get(role, ())
    proj = [
        e
        for e in approved
        if e.category == MemoryCategory.project
        and project_slug
        and e.project_slug == slugify(project_slug)
        and e.scope in scopes
    ]

    buckets = {"global": glob, "role": role_mem, "project": proj}
    selection = MemorySelection(budgets=dict(budgets))
    for bucket, entries in buckets.items():
        kept, excluded = _select_bucket(bucket, entries, budgets.get(bucket, -1))
        selection.excluded.extend(excluded)
        by_scope: dict[str, list[str]] = {}
        for e in kept:
            by_scope.setdefault(e.scope, []).append(e.content.strip())
        text = "\n".join(
            f"### {scope}\n" + "\n".join(f"- {c}" for c in items)
            for scope, items in sorted(by_scope.items())
        )
        selection.sections[bucket] = text
        selection.used[bucket] = estimate_tokens(text)
    selection.warnings = _budget_warnings(selection)
    return selection


def _budget_warnings(selection: MemorySelection) -> list[str]:
    """Tripwire before the guillotine: warn as a section approaches its cap."""
    if not get_settings().memory_budget_enabled:
        return []
    frac = get_settings().memory_budget_warn_fraction
    out = []
    for bucket, cap in selection.budgets.items():
        if cap <= 0:
            continue
        used = selection.used.get(bucket, 0)
        if used >= frac * cap:
            demoted = sum(1 for x in selection.excluded if x["bucket"] == bucket)
            out.append(
                f"memory section '{bucket}' is at {used}/{cap} tokens "
                f"({round(100 * used / cap)}% of its cap)"
                + (f"; {demoted} approved entry(ies) are already demoted" if demoted else "")
                + " - condense or merge entries before approving more"
            )
    return out


# ---------------------------------------------------------------- export/import


def export_all(session: Session) -> dict[str, Any]:
    return {
        "version": 1,
        "entries": [
            {
                k: getattr(e, k)
                for k in (
                    "category",
                    "scope",
                    "project_slug",
                    "role",
                    "content",
                    "source",
                    "tags_json",
                    "approved",
                    "rejected",
                )
            }
            for e in list_entries(session)
        ],
    }


def import_all(session: Session, payload: dict[str, Any]) -> int:
    """Import entries; skips exact duplicates (same category/scope/project/role/content)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise MemoryError_("invalid memory export: expected {'entries': [...]}")
    existing = {
        (e.category, e.scope, e.project_slug, e.role, e.content) for e in list_entries(session)
    }
    count = 0
    touched: set[Path] = set()
    for raw in payload["entries"]:
        key = (
            raw.get("category"),
            raw.get("scope"),
            raw.get("project_slug"),
            raw.get("role"),
            redact(str(raw.get("content", "")).strip()),
        )
        if key in existing or not key[4]:
            continue
        entry = MemoryEntry(
            category=key[0],
            scope=key[1],
            project_slug=key[2],
            role=key[3],
            content=key[4],
            source=raw.get("source", "import"),
            tags_json=list(raw.get("tags_json") or []),
            approved=bool(raw.get("approved")),
            rejected=bool(raw.get("rejected")),
        )
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
                data = MemoryCreate(
                    category=MemoryCategory.global_,
                    scope=slugify(path.stem),
                    content=content,
                    source="markdown",
                )
            elif rel[0] == "projects" and len(rel) == 3:
                data = MemoryCreate(
                    category=MemoryCategory.project,
                    scope=slugify(path.stem),
                    project_slug=rel[1],
                    content=content,
                    source="markdown",
                )
            elif rel[0] == "roles" and path.stem in Role.__members__:
                data = MemoryCreate(
                    category=MemoryCategory.role,
                    scope=path.stem,
                    role=Role(path.stem),
                    content=content,
                    source="markdown",
                )
            else:
                continue
            create(session, data)
            known.add(content)
            added += 1
    return added


def dumps_export(session: Session) -> str:
    return json.dumps(export_all(session), indent=2, default=str)
