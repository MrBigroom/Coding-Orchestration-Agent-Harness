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
from orchestrator.utils.paths import safe_join
from orchestrator.utils.redaction import redact
from orchestrator.utils.slug import slugify

# ---------------------------------------------------------------- vocabularies
# These three tuples are the ONLY scopes any injector can read. A row stored with a
# scope outside its own category's vocabulary is invisible to every prompt (task T-4),
# so the boundary checks membership and not just shape. Tests import these names.
GLOBAL_SCOPES: tuple[str, ...] = ("environment", "preferences", "patterns")
PROJECT_SCOPES: tuple[str, ...] = ("conventions", "decisions", "gotchas")
ROLE_FILES: tuple[str, ...] = ("architect", "worker", "supervisor")
# The scope vocabulary for category=role: the role-memory FILE names, i.e. ROLE_FILES
# after applying the ROLE_MEMORY_KEY aliases (reviewer -> architect, agent -> worker).
# `reviewer`/`agent` are accepted at the boundary as role NAMES and resolved to a file;
# they are never scopes of their own. There was no ROLE_SCOPES before T-4, which is why
# an out-of-vocabulary check could only ever be written for two of three categories.
ROLE_SCOPES: tuple[str, ...] = ROLE_FILES

SCOPE_VOCABULARY: dict[str, tuple[str, ...]] = {
    MemoryCategory.global_: GLOBAL_SCOPES,
    MemoryCategory.project: PROJECT_SCOPES,
    MemoryCategory.role: ROLE_SCOPES,
}
# Deterministic fallback scope used to COERCE a suggestion whose scope is out of
# vocabulary. Only global and project have one: a role suggestion with no resolvable
# role would land in a file that no injector reads, so it is dropped with a warning.
FALLBACK_SCOPE: dict[str, str] = {
    MemoryCategory.global_: "environment",
    MemoryCategory.project: "gotchas",
}

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
# Role names accepted for category=role, resolved through ROLE_MEMORY_KEY.
ROLE_SCOPE_ALIASES: dict[str, str] = {k.value: v for k, v in ROLE_MEMORY_KEY.items()}


class MemoryError_(ValueError):
    pass


@dataclass
class SuggestionOutcome:
    """What the suggestion path stored, and what it refused to store.

    `notes` carries every substitution and every drop in submission order; the caller
    writes them to the build log / chat stream so the operator sees a scope that was
    quietly rewritten instead of an entry that silently never reaches a prompt.
    """

    created: list[MemoryEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def moved(self) -> list[str]:
        """Just the substitution notes (drops start with 'dropped ')."""
        return [n for n in self.notes if not n.startswith("dropped ")]


@dataclass
class ScopeRepair:
    """Result of `fix_scopes`: rows moved, rows left alone, dead files removed."""

    moved: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    removed_files: list[Path] = field(default_factory=list)
    # Dead rendered files that were only REPORTED because gc was off (`--no-gc`). A dry run
    # fills `removed_files` with what it would remove, never `ghost_files`.
    ghost_files: list[Path] = field(default_factory=list)
    dry_run: bool = False

    @property
    def moved_ids(self) -> list[int]:
        return [int(m["id"]) for m in self.moved]


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


def scope_vocabulary(category: str) -> tuple[str, ...]:
    """The injectable scopes for one category, or an error naming them."""
    vocab = SCOPE_VOCABULARY.get(category)
    if vocab is None:
        raise MemoryError_(
            f"unknown memory category {category!r}; allowed: global|project|role"
        )
    return vocab


def role_memory_file(name: str | None) -> str | None:
    """Resolve a role name (including the reviewer/agent aliases) to its memory file.

    Returns None when the name cannot reach any role memory file. Dict lookups work with
    plain strings because `Role` is a StrEnum, which is how `for_prompt` already matches.
    """
    if not name:
        return None
    key = ROLE_MEMORY_KEY.get(name, name)
    return key if key in ROLE_SCOPES else None


def _vocab_error(category: str, scope: str) -> MemoryError_:
    vocab = SCOPE_VOCABULARY.get(category, ())
    allowed = "|".join(vocab)
    if category == MemoryCategory.role:
        alias = ", ".join(f"{k} -> {v}" for k, v in sorted(ROLE_SCOPE_ALIASES.items()))
        return MemoryError_(
            f"role memory scope {scope!r} is not injectable; allowed scopes for role: "
            f"{allowed} (role names {alias} are accepted and resolve to their file)"
        )
    return MemoryError_(
        f"memory scope {scope!r} is not injectable; allowed scopes for {category}: {allowed}"
    )


def normalize_scope(category: str, scope: str) -> str:
    """The canonical, injectable scope for (category, scope).

    Raises `MemoryError_` naming the allowed values when the scope cannot be reached by
    its own category — this is the single boundary check used by create, update, import
    and the suggestion path, so the API and the CLI reject the same rows for the same
    reason.
    """
    vocab = scope_vocabulary(category)
    if category == MemoryCategory.role:
        key = role_memory_file(scope)
        if key is None:
            raise _vocab_error(category, scope or "")
        return key
    slug = slugify(scope or "")
    if slug in vocab:
        return slug
    raise _vocab_error(category, scope or "")


def coerce_scope(category: str, scope: str) -> tuple[str, str]:
    """Repair an out-of-vocabulary scope by falling back. Returns (scope, note).

    `note` is empty when the scope was already injectable. The fallback is the WHOLE rule:
    a scope that is not in its category's vocabulary goes to `FALLBACK_SCOPE[category]`
    (`gotchas` for project, `environment` for global) and is reported. Nothing is ever
    *inferred* from the words inside a proposed scope, because the scope decides which
    roles receive the note — guessing that `decisions-and-gating` means `decisions` would
    silently route a Worker's gotcha to the Architect and the Supervisor, and a substring
    match fires inside unrelated words. A predictable place to find it beats a clever
    guess, and the operator sees the substitution either way.

    Categories with no fallback (`role`) re-raise: inventing a role would file someone's
    note under the wrong agent, so the suggestion path drops it with a warning instead of
    storing an unreachable row.
    """
    try:
        return normalize_scope(category, scope), ""
    except MemoryError_ as e:
        err = e
    fallback = FALLBACK_SCOPE.get(category)
    if fallback is None:
        raise err
    return fallback, _substitution_note(category, scope, fallback)


def _substitution_note(category: str, requested: str, stored: str) -> str:
    return (
        f"{category} scope {requested!r} is not in "
        f"{'|'.join(SCOPE_VOCABULARY.get(category, ()))}; stored as {stored!r} "
        f"(deterministic fallback, not inferred from the words)"
    )


def role_file_key(entry: MemoryEntry) -> str | None:
    """The role-memory file a row maps to, by the rule `for_prompt` and `_file_for` use.

    Identical to the pre-T-4 resolution — `role`, falling back to `scope`, through
    `ROLE_MEMORY_KEY` — and deliberately NOT checked against `ROLE_FILES`: a legacy row
    with a non-file role and a valid scope must keep rendering to (and being judged by) the
    same file it always did. Task T-4 changes no role-injection semantics; membership is
    tested by the caller so an uninjectable row is reported instead of re-homed.
    """
    name = entry.role or entry.scope
    if not name:
        return None
    return ROLE_MEMORY_KEY.get(name, name)


def reachable_roles(entry: MemoryEntry) -> tuple[str, ...]:
    """Which prompt roles would actually read this entry's scope, once approved."""
    if entry.category == MemoryCategory.global_:
        if entry.scope not in GLOBAL_SCOPES:
            return ()
        return tuple(r.value for r in Role)
    if entry.category == MemoryCategory.project:
        if entry.scope not in PROJECT_SCOPES or not entry.project_slug:
            return ()
        return tuple(
            r.value for r, scopes in ROLE_PROJECT_SCOPES.items() if entry.scope in scopes
        )
    key = role_file_key(entry)
    if key not in ROLE_FILES:  # None, or a name no injector reads (e.g. role='logbook')
        return ()
    return tuple(r.value for r in Role if ROLE_MEMORY_KEY.get(r.value, r.value) == key)


def injectability(entry: MemoryEntry) -> tuple[bool, str]:
    """(ok, reason). reason names the vocabulary that the entry failed."""
    if entry.category not in SCOPE_VOCABULARY:
        return False, f"unknown category {entry.category!r}"
    if entry.category == MemoryCategory.project and not entry.project_slug:
        return False, "project memory requires project_slug"
    if reachable_roles(entry):
        return True, ""
    if entry.category == MemoryCategory.role:
        name = entry.role or entry.scope
        allowed = "|".join(ROLE_SCOPES)
        if not name:
            return False, f"role memory needs a role or a scope resolving to one of {allowed}"
        # `role='logbook-writer'` renders to roles/logbook-writer.md, which no role prompt
        # reads. Name the file (it is the frozen rendering rule, unchanged by T-4) and the
        # vocabulary it has to resolve into.
        return False, (
            f"role memory renders to {_file_for(entry).name}, which no role prompt reads: "
            f"role={entry.role!r} scope={entry.scope!r} must resolve to one of {allowed} "
            f"(reviewer -> architect, agent -> worker)"
        )
    return False, str(_vocab_error(entry.category, entry.scope or ""))


def is_injectable(entry: MemoryEntry) -> bool:
    return injectability(entry)[0]


def _file_for(entry: MemoryEntry) -> Path:
    root = memory_root()
    if entry.category == MemoryCategory.global_:
        return safe_join(root, "global", f"{slugify(entry.scope)}.md")
    if entry.category == MemoryCategory.project:
        return safe_join(
            root, "projects", slugify(entry.project_slug or "unknown"), f"{slugify(entry.scope)}.md"
        )
    # Unchanged from before T-4 on purpose: rendering is a frozen contract and role
    # injection is out of this task's scope. `role`, else `scope`, through ROLE_MEMORY_KEY.
    name = entry.role or entry.scope
    return safe_join(root, "roles", f"{slugify(ROLE_MEMORY_KEY.get(name, name))}.md")


def _validate(entry: MemoryEntry, *, check_scope: bool = True) -> None:
    """Boundary validation shared by create(), update(), import and the suggestion path.

    Every category is vocabulary-checked (T-4 DoD 2): before this, category=global and
    category=project got no scope check at all and category=role was only half-guarded.
    `check_scope=False` is what `update()` passes when the caller supplied no scope, so the
    CONTENT of a legacy row stays editable — refusing that would lock the operator out of
    the very rows `fix-scopes` exists to repair.
    """
    if entry.category == MemoryCategory.project and not entry.project_slug:
        raise MemoryError_("project memory requires project_slug")
    if not check_scope:
        return
    if entry.category == MemoryCategory.role:
        if entry.role and role_memory_file(entry.role) is None:
            raise _vocab_error(entry.category, entry.role)
        key = role_memory_file(entry.role) or role_memory_file(entry.scope)
        if key is None:
            raise _vocab_error(entry.category, entry.scope or entry.role or "")
        if not entry.role:
            # Historic behaviour kept: a role row may carry its role in `scope`.
            entry.role = entry.scope
        entry.scope = key
        return
    entry.scope = normalize_scope(entry.category, entry.scope)


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
    """The created entries only. Kept for callers that do not read the notes."""
    return suggest_from_handoff_report(
        session, suggestions, source, project_slug, role=role
    ).created


def suggest_from_handoff_report(
    session: Session,
    suggestions: list[MemorySuggestion],
    source: str,
    project_slug: str | None,
    role: str = "worker",
) -> SuggestionOutcome:
    """Store worker/architect suggestions as unapproved entries for Supervisor review.

    T-4: a Worker's proposed scope is never trusted. `project`/`global` suggestions whose
    scope is outside their category vocabulary are coerced to a deterministic scope and
    every substitution is reported in `notes` (the caller writes them to the build log,
    where the operator sees them); `role` suggestions that resolve to no role memory file
    are dropped with a warning instead of being stored. Nothing is stored with a scope no
    injector can read.
    """
    outcome = SuggestionOutcome()
    existing = {
        (e.category, e.scope, e.project_slug, e.content.strip()) for e in list_entries(session)
    }
    for s in suggestions:
        try:
            category = MemoryCategory(s.category)
        except ValueError:
            outcome.notes.append(
                f"dropped suggestion from {source}: unknown category {s.category!r}; "
                f"allowed: global|project|role"
            )
            continue
        slug = slugify(s.scope)
        try:
            if category == MemoryCategory.role:
                file_key = role_memory_file(slug)
                if file_key is None:
                    raise MemoryError_(str(_vocab_error(category, slug)))
                scope = file_key
                role_value = slug if slug in Role.__members__ else role
                create_role = Role(role_value) if role_value in Role.__members__ else Role.worker
            else:
                scope, note = coerce_scope(category, slug)
                if note:
                    outcome.notes.append(f"memory suggestion from {source}: {note}")
                create_role = None
            proj = (
                slugify(project_slug)
                if category == MemoryCategory.project and project_slug
                else None
            )
            key = (category, scope, proj, redact(s.content.strip()))
            if key in existing:
                continue
            existing.add(key)
            outcome.created.append(
                create(
                    session,
                    MemoryCreate(
                        category=category,
                        scope=scope,
                        content=s.content,
                        project_slug=project_slug if category == MemoryCategory.project else None,
                        role=create_role,
                        tags=s.tags,
                        source=source,
                        approved=False,
                    ),
                )
            )
        except (ValueError, MemoryError_) as e:
            # A drop is an operator-visible decision, never a silent `continue`.
            outcome.notes.append(f"dropped suggestion from {source}: {e}")
    return outcome


def update(session: Session, entry_id: int, data: MemoryUpdate) -> MemoryEntry:
    """Apply an edit. A scope the caller supplies is checked like at creation; a stored
    legacy scope that is out of vocabulary does not freeze the row.

    Validation happens on a throwaway preview BEFORE anything is assigned, so a rejected
    PATCH leaves the stored row and its rendered markdown exactly as they were — the
    "unchanged" guarantee does not depend on session rollback behaviour.
    """
    entry = _get(session, entry_id)
    new_content = (
        redact(data.content.strip()) if data.content is not None else entry.content
    )
    new_tags = sorted(set(data.tags)) if data.tags is not None else entry.tags_json
    preview = MemoryEntry(
        category=entry.category,
        scope=data.scope if data.scope is not None else entry.scope,
        project_slug=entry.project_slug,
        role=entry.role,
        content=new_content,
    )
    _validate(preview, check_scope=data.scope is not None)
    old_file = _file_for(entry)  # before the mutation: the file this row used to render into
    entry.content = new_content
    entry.tags_json = new_tags
    entry.scope = preview.scope  # normalized by _validate when a scope was supplied
    entry.role = preview.role
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


# ---------------------------------------------------------------- repair (T-4)


def canonical_file(path: Path) -> bool:
    """Is this rendered file part of the layout that `ensure_layout` writes and that the
    injectors name? An empty canonical file is not an orphan; a file whose stem is not a
    vocabulary scope (e.g. `projects/<slug>/<slug>.md`) can never be read, so it is."""
    root = memory_root()
    try:
        rel = path.relative_to(root).parts
    except ValueError:
        return False
    if len(rel) < 2 or not rel[-1].endswith(".md"):
        return False
    kind, stem = rel[0], rel[-1][: -len(".md")]
    if kind == "global":
        return len(rel) == 2 and stem in GLOBAL_SCOPES
    if kind == "roles":
        return len(rel) == 2 and stem in ROLE_FILES
    if kind == "projects":
        return len(rel) == 3 and stem in PROJECT_SCOPES
    return False


def in_layout_dir(path: Path) -> bool:
    """Is the file somewhere `ensure_layout`/`render_file` write: global/, roles/ or
    projects/<slug>/ — and nothing deeper, and nothing outside the memory root?"""
    root = memory_root()
    try:
        rel = path.relative_to(root).parts
    except ValueError:
        return False
    if len(rel) == 2:
        return rel[0] in ("global", "roles") and rel[1].endswith(".md")
    if len(rel) == 3:
        return rel[0] == "projects" and rel[1] and rel[2].endswith(".md")
    return False


_MD_MARKER_LINE = re.compile(r"^- .+  <!-- mem:\d+ -->\s*$")


def _renderer_output(path: Path) -> bool:
    """Could `render_file` have written this text? Only then may the GC delete it.

    `render_file` emits exactly `# <stem>` as its first line, then blank lines and
    `- <content>  <!-- mem:N -->` marker lines. So the title line must equal `# ` + the
    filename stem, and every other non-blank line must be a marker line. Anything else —
    prose, an un-marked bullet, a different or deeper heading, an `ensure_layout` header
    such as `# Project X — decisions` — disqualifies the whole file.

    This matters because `docs/memory.md` tells operators to hand-edit these files and
    index the lines with `memory import --markdown`: a hand-authored note must never be
    swept up by a garbage collector. The measured orphan (`# ai-livestreaming-avatar\n`,
    left title-only when entry #5 was re-scoped by hand) matches this predicate exactly.
    """
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (OSError, UnicodeDecodeError):
        return False
    if not lines or lines[0] != f"# {path.stem}":
        return False
    return all(_MD_MARKER_LINE.match(ln) for ln in lines[1:])


def orphan_files(session: Session) -> list[Path]:
    """Rendered markdown files that are dead output: no owning approved entry, not a name
    `ensure_layout` owns, and text that only `render_file` could have produced.

    A hand-authored file fails `_renderer_output`, so it is never listed here.
    """
    root = memory_root()
    if not root.exists():
        return []
    owned = {_file_for(e) for e in list_entries(session, status="approved")}
    return [
        p
        for p in sorted(root.rglob("*.md"))
        if p not in owned
        and in_layout_dir(p)
        and not canonical_file(p)
        and _renderer_output(p)
    ]


def gc_orphan_files(session: Session, *, dry_run: bool = False) -> list[Path]:
    """Delete dead scope-files so a re-scoped entry cannot leave a title-only ghost behind.

    Only rows are ever left untouched (nothing here deletes a `memory_entries` row, and no
    canonical layout file is removed); the files it deletes are derived output that passes
    `orphan_files` — unowned, outside the canonical names, and text `render_file` wrote.
    Hand-written markdown in the tree is protected by `_renderer_output`.
    """
    root = memory_root()
    removed: list[Path] = []
    for path in orphan_files(session):
        removed.append(path)
        if dry_run:
            continue
        path.unlink(missing_ok=True)
        parent = path.parent
        if parent != root and parent.relative_to(root).parts[0] == "projects":
            if not any(parent.iterdir()):
                parent.rmdir()
    return removed


def fix_scopes(session: Session, *, dry_run: bool = False, gc: bool = True) -> ScopeRepair:
    """Migrate entries already stored with an out-of-vocabulary scope.

    Rows are moved, never deleted, and rejected rows are left exactly as they are. Both
    the OLD and the NEW markdown file are re-rendered so the old file cannot keep a stale
    `<!-- mem:ID -->` line, then — with `gc=True`, which is what the CLI's `--no-gc` turns
    off — dead rendered files are collected (the measured orphan was
    `memory/projects/<slug>/<slug>.md`, left title-only when entry #5 was re-scoped by
    hand). `orphan_files` is deliberately narrow: unowned AND not a canonical layout name
    AND text `render_file` could have written, so hand-authored markdown indexed via
    `memory import --markdown` is never deleted.

    Idempotent: a second run moves nothing. A dry run computes the same report and writes
    nothing, including the file list.
    """
    repair = ScopeRepair(dry_run=dry_run)
    for entry in list_entries(session):
        ok, reason = injectability(entry)
        if ok:
            continue
        detail: dict[str, Any] = {
            "id": entry.id,
            "category": entry.category,
            "scope": entry.scope,
            "reason": reason,
        }
        if entry.rejected:
            repair.skipped.append({**detail, "why": "rejected rows are left alone"})
            continue
        if entry.category == MemoryCategory.role:
            repair.skipped.append(
                {
                    **detail,
                    "why": "role memory has no fallback file: an unresolvable role would be "
                    "invented, so the row is reported instead (approve it under a real role "
                    "or reject it). Note that for_prompt selects on the `role` column, so a "
                    "legacy row with an empty role and a valid scope is a shape the boundary "
                    "can no longer create (_validate fills role from scope).",
                }
            )
            continue
        try:
            new_scope, note = coerce_scope(entry.category, entry.scope)
        except MemoryError_ as e:  # pragma: no cover - the vocabularies above always fall back
            repair.skipped.append({**detail, "why": str(e)})
            continue
        if new_scope == entry.scope:
            # The scope is in vocabulary but the row is still unreachable (e.g. a project
            # row with no project_slug). Repairing a scope cannot fix that; report it.
            repair.skipped.append({**detail, "why": f"scope already in vocabulary: {reason}"})
            continue
        old_file = _file_for(entry)
        # A row that moves away from a non-canonical file leaves that file with no owner;
        # the GC below collects it. Computed before the mutation so a dry run can say so.
        other_owners = {
            _file_for(e)
            for e in list_entries(session, status="approved")
            if e.id != entry.id
        }
        preview = MemoryEntry(
            category=entry.category,
            scope=new_scope,
            project_slug=entry.project_slug,
            role=entry.role,
            content=entry.content,
        )
        moved = {
            "id": entry.id,
            "category": entry.category,
            "project_slug": entry.project_slug,
            "from": entry.scope,
            "to": new_scope,
            "note": note,
            "approved": entry.approved,
            "old_file": str(old_file),
            "new_file": str(_file_for(preview)),
            "collect_old_file": not canonical_file(old_file) and old_file not in other_owners,
        }
        repair.moved.append(moved)
        if dry_run:
            continue
        entry.scope = new_scope
        _validate(entry)
        entry.updated_at = utcnow()
        session.add(entry)
        session.commit()
        if entry.approved:
            render_file(session, old_file)
            render_file(session, _file_for(entry))
        elif old_file.exists():
            # An unapproved row never rendered, but a stale copy from an earlier approval
            # may still name it.
            render_file(session, old_file)
    if gc:
        for path in gc_orphan_files(session, dry_run=dry_run):
            repair.removed_files.append(path)
    else:
        # --no-gc: the operator still has to SEE the dead files, they are just not deleted.
        repair.ghost_files = orphan_files(session)
    return repair


# ---------------------------------------------------------------- injection


def for_prompt(session: Session, role: str, project_slug: str | None) -> dict[str, str]:
    """Approved memory for a role, split into the three cached prefix sections."""
    approved = list_entries(session, status="approved")
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


def _import_keys(category: str, scope: str, project_slug: str | None, role: str | None,
                 content: str) -> set[tuple[Any, ...]]:
    """Every tuple that identifies this fact, before AND after the scope repair.

    A stored legacy row keeps its dead scope until `fix-scopes` runs, so an import of the
    same export would otherwise add a SECOND row with the same content under the fallback
    scope and render the line twice. Recognising both spellings keeps `import` idempotent
    in both directions; a repairable category contributes its coerced scope too.
    """
    keys = {(category, scope, project_slug, role, content)}
    if category in FALLBACK_SCOPE:
        try:
            repaired, _ = coerce_scope(category, scope)
            keys.add((category, repaired, project_slug, role, content))
        except MemoryError_:
            pass
    return keys


def import_all(session: Session, payload: dict[str, Any]) -> int:
    """Import entries; skips duplicates (same category/scope/project/role/content).

    T-4: an export written before scope validation existed carries free-form scopes, so the
    scope is repaired on the way in rather than dropping a good fact over a vocabulary
    mistake — and the duplicate check runs on BOTH the stored and the REPAIRED scope, so
    importing a legacy export neither duplicates a row that is still awaiting `fix-scopes`
    nor re-adds anything on a second pass.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise MemoryError_("invalid memory export: expected {'entries': [...]}")
    existing: set[tuple[Any, ...]] = set()
    for e in list_entries(session):
        existing |= _import_keys(e.category, e.scope, e.project_slug, e.role, e.content)
    count = 0
    touched: set[Path] = set()
    for raw in payload["entries"]:
        content = redact(str(raw.get("content", "")).strip())
        category = raw.get("category")
        if not content or category not in {c.value for c in MemoryCategory}:
            continue
        entry = MemoryEntry(
            category=category,
            scope=slugify(str(raw.get("scope") or "")),
            project_slug=raw.get("project_slug"),
            role=raw.get("role"),
            content=content,
            source=raw.get("source", "import"),
            tags_json=list(raw.get("tags_json") or []),
            approved=bool(raw.get("approved")),
            rejected=bool(raw.get("rejected")),
        )
        raw_keys = _import_keys(
            category, entry.scope, entry.project_slug, entry.role, content
        )
        if raw_keys & existing:
            continue
        try:
            if entry.category in FALLBACK_SCOPE:
                entry.scope, _ = coerce_scope(entry.category, entry.scope)
            _validate(entry)
        except MemoryError_:
            continue
        key = (entry.category, entry.scope, entry.project_slug, entry.role, entry.content)
        existing |= raw_keys | {key}
        session.add(entry)
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
                # A dead scope-file (an orphan named after something other than a
                # vocabulary scope) is repaired on the way in, never indexed as another
                # entry no injector can reach.
                scope, _ = coerce_scope(MemoryCategory.global_, slugify(path.stem))
                data = MemoryCreate(
                    category=MemoryCategory.global_,
                    scope=scope,
                    content=content,
                    source="markdown",
                )
            elif rel[0] == "projects" and len(rel) == 3:
                scope, _ = coerce_scope(MemoryCategory.project, slugify(path.stem))
                data = MemoryCreate(
                    category=MemoryCategory.project,
                    scope=scope,
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
