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

# The vocabularies and the pure scope/role resolution live in core (T-4 DoD 2): the API
# request model in `core/schemas.py` has to refuse the same shapes with the same words, and
# it cannot import THIS module (memory_service imports schemas -> circular import). Every
# name is re-imported here so `memory_service.GLOBAL_SCOPES`, `memory_service.coerce_scope`
# and the rest keep resolving exactly as they did before the move. Three of the lines below
# are marked as intentional RE-EXPORTS: nothing in this module calls them, but
# `memory_service.ROLE_SCOPE_ALIASES`, `memory_service.scope_vocabulary` and
# `memory_service.substitution_note` belong to the surface tests and callers read, and
# removing them would break those imports.
from orchestrator.core.memory_vocab import (
    FALLBACK_SCOPE,
    GLOBAL_SCOPES,
    PROJECT_SCOPES,
    ROLE_FILES,
    ROLE_MEMORY_KEY,
    ROLE_PROJECT_SCOPES,
    ROLE_SCOPE_ALIASES,  # noqa: F401 - re-export
    ROLE_SCOPES,
    SCOPE_VOCABULARY,
    MemoryError_,
    check_role_name,
    coerce_scope,
    normalize_scope,
    role_memory_file,
    scope_vocabulary,  # noqa: F401 - re-export
    substitution_note,  # noqa: F401 - re-export
    vocab_error,
)
from orchestrator.core.models import MemoryCategory, MemoryEntry, Role, Setting, utcnow
from orchestrator.core.schemas import MemoryCreate, MemorySuggestion, MemoryUpdate
from orchestrator.utils.memory_text import (
    build_df,
    build_term_df,
    duplicate_detail,
    duplicate_reason,
    is_duplicate_pair,
    is_rot_prone,
    is_subject_pair,
    subject_reason,
)
from orchestrator.utils.paths import safe_join
from orchestrator.utils.redaction import REDACTED, redact_with_report
from orchestrator.utils.slug import slugify
from orchestrator.utils.truncate import estimate_tokens


@dataclass
class SuggestionRecord:
    """One structured outcome per suggestion: what happened, and the words for the log.

    `kind` is the STRUCTURE the build log derives its event name from (`dropped`,
    `substituted`, `linked`); `message` is free text. Callers must never infer the kind from
    the shape of `message` -- a note that happens to start with "dropped " is not a drop.
    """

    kind: str
    message: str


@dataclass
class SuggestionOutcome:
    """What the suggestion path stored, what it linked, and what it refused to store.

    `notes` carries every substitution, every duplicate link and every drop in submission
    order; the caller writes them to the build log / chat stream so the operator sees a
    scope that was quietly rewritten, or a fact that was linked instead of stored, instead
    of an entry that silently never reaches a prompt. `records` is the same list with the
    kind made explicit, so no consumer has to match message prefixes.
    """

    created: list[MemoryEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    records: list[SuggestionRecord] = field(default_factory=list)
    #: Restatements recorded as pending rows. Each record names the existing entry, the new row
    #: that carries the fact (so its content is persisted, not only logged) and the matcher
    #: numbers behind the link. Nothing is auto-rejected; the operator decides in triage.
    linked: list[dict[str, Any]] = field(default_factory=list)

    def note(self, kind: str, message: str) -> None:
        """Record one outcome for BOTH the human log line and the structured consumer."""
        self.notes.append(message)
        self.records.append(SuggestionRecord(kind=kind, message=message))

    @property
    def moved(self) -> list[str]:
        """Just the scope-substitution notes (drops and duplicate links are not moves)."""
        return [r.message for r in self.records if r.kind == "substituted"]


REDACTION_TAG_PREFIX = "redaction-substitution:"
#: Ingest duplicate marker. The memory_entries shape is frozen, so a linked restatement is a
#: normal pending row whose existing tags_json column names the entry it restates; triage
#: reads the tag and reports it as `linked-duplicate`. It is never auto-rejected: dropping it
#: without an operator command would be the silent ingest decision T-17 DoD 1 forbids.
DUPLICATE_TAG_PREFIX = "duplicate-of:"
#: The rest of the ingest link record, stored next to it in the SAME frozen tags_json column so
#: triage can show WHY the row was linked (T-17 review round 4, finding 3). The numbers and the
#: shared terms are the matcher's own output; the reason string is rebuilt from them, never
#: retyped, so a threshold change cannot leave stale prose on a row.
DUPLICATE_MATCH_PREFIX = "duplicate-match:"
DUPLICATE_RATIO_PREFIX = "duplicate-ratio:"
DUPLICATE_THRESHOLD_PREFIX = "duplicate-threshold:"
DUPLICATE_TERMS_PREFIX = "duplicate-terms:"
#: Tag-driven value order. Higher value is kept longer; an untagged entry behaves exactly as
#: it did before T-13's remainder (content class, then id as the last tiebreak only).
PINNED_TAGS = frozenset({"pinned", "durable"})
TIER_TAGS = {"tier:1": 3, "tier:2": 2, "tier:3": 1}
DEFAULT_TIER_RANK = 2


def _line_hits(text: str) -> dict[str, list[int]]:
    """Which line(s) each redaction pattern class matched (for the per-entry record)."""
    out: dict[str, list[int]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        for hit in redact_with_report(line).hits:
            out.setdefault(hit.pattern_class, []).append(number)
    return out


def _redaction_tags(text: str, report) -> list[str]:
    """Per-entry record of a rewrite: pattern class, count, and where it matched."""
    if not report.changed:
        return []
    located = _line_hits(text)
    tags = []
    for hit in report.hits:
        lines = sorted(set(located.get(hit.pattern_class, [])))
        where = ",".join(str(n) for n in lines) if lines else "content"
        tags.append(f"{REDACTION_TAG_PREFIX}{hit.pattern_class}:count={hit.count}:lines={where}")
    return sorted(tags)


def _redact_memory_content(text: str) -> tuple[str, list[str]]:
    """Redact durable memory text AND return the visible substitution record.

    Memory is not a log line: a benign `KEY: value` phrase must never be silently rewritten.
    `utils/redaction.py` stays the one security control, so real credentials are still
    removed; when that happens the entry carries the pattern class, count and line(s) in
    `tags_json` (an existing column, so the frozen memory_entries shape does not change).
    `memory_triage` flags any entry with such a record or a marker as garbled, and
    `select_for_prompt`/`render_file` refuse to inject it until it is reworded.
    """
    report = redact_with_report(text)
    return report.text, _redaction_tags(text, report)


def _combine_tags(
    base: list[str] | None, redaction: list[str], *, preserve_existing: bool = False
) -> list[str]:
    """User tags plus the CURRENT redaction record; a stale record is always dropped.

    `preserve_existing` is for a payload that already carries a record and whose content was
    redacted earlier (an export/import round trip): the record is re-stated rather than
    silently lost. An edit that rewrites clean content passes the default, so re-wording a
    garbled entry clears its old record.
    """
    existing = [t for t in (base or []) if t.startswith(REDACTION_TAG_PREFIX)]
    kept = [t for t in (base or []) if not t.startswith(REDACTION_TAG_PREFIX)]
    if preserve_existing and not redaction:
        redaction = existing
    return sorted(set(kept) | set(redaction))


def is_garbled(entry: MemoryEntry) -> bool:
    """Was this entry's text rewritten on write (marker and/or a substitution record)?"""
    return REDACTED in (entry.content or "") or any(
        t.startswith(REDACTION_TAG_PREFIX) for t in (entry.tags_json or [])
    )


def duplicate_of(entry: MemoryEntry) -> int | None:
    """The entry id this row was recorded as restating at ingest, or None."""
    for tag in entry.tags_json or []:
        if isinstance(tag, str) and tag.startswith(DUPLICATE_TAG_PREFIX):
            try:
                return int(tag[len(DUPLICATE_TAG_PREFIX) :])
            except ValueError:
                return None
    return None


def duplicate_record(entry: MemoryEntry) -> dict[str, Any] | None:
    """The whole ingest link record: anchor id, matcher, its numbers/terms and the reason.

    Read back out of `tags_json` (the frozen schema has no column for it), so triage shows
    the reason ON THE ROW instead of leaving it in a transient outcome and the build log.
    A row linked before this record existed carries only `duplicate-of:<id>`; its reason is
    `None` and triage falls back to naming the anchor.
    """
    anchor = duplicate_of(entry)
    if anchor is None:
        return None
    tags = {t.split(":", 1)[0]: t.split(":", 1)[1] for t in (entry.tags_json or [])
            if isinstance(t, str) and ":" in t}
    match = tags.get(DUPLICATE_MATCH_PREFIX.rstrip(":"))
    ratio: float | None = None
    threshold: float | None = None
    terms: list[str] = []
    try:
        if tags.get(DUPLICATE_RATIO_PREFIX.rstrip(":")) is not None:
            ratio = float(tags[DUPLICATE_RATIO_PREFIX.rstrip(":")])
        if tags.get(DUPLICATE_THRESHOLD_PREFIX.rstrip(":")) is not None:
            threshold = float(tags[DUPLICATE_THRESHOLD_PREFIX.rstrip(":")])
    except ValueError:
        ratio = threshold = None
    raw_terms = tags.get(DUPLICATE_TERMS_PREFIX.rstrip(":"))
    if raw_terms:
        terms = [t for t in raw_terms.split(",") if t]
    if match == "rare-token" and ratio is not None and threshold is not None:
        reason: str | None = duplicate_reason(ratio, threshold)
    elif match == "subject" and terms:
        reason = subject_reason(terms)
    else:
        reason = None
    return {
        "existing_id": anchor,
        "match": match,
        "ratio": ratio,
        "threshold": threshold,
        "terms": terms,
        "reason": reason,
    }


def _duplicate_tags(match: dict[str, Any]) -> list[str]:
    """The tags that record a link AND its evidence in the existing tags_json column."""
    tags = [
        f"{DUPLICATE_TAG_PREFIX}{match['existing_id']}",
        f"{DUPLICATE_MATCH_PREFIX}{match['match']}",
    ]
    if match.get("ratio") is not None:
        tags.append(f"{DUPLICATE_RATIO_PREFIX}{match['ratio']:.3f}")
    if match.get("threshold") is not None:
        tags.append(f"{DUPLICATE_THRESHOLD_PREFIX}{match['threshold']:.2f}")
    if match.get("terms"):
        tags.append(f"{DUPLICATE_TERMS_PREFIX}{','.join(match['terms'][:5])}")
    return tags


def _duplicate_reason(match: dict[str, Any]) -> str:
    """The reason built from the matcher's actual numbers/terms (never retyped prose)."""
    if match["match"] == "rare-token":
        return duplicate_reason(match["ratio"], match["threshold"])
    return subject_reason(match["terms"])


def _refuse_garbled(entry: MemoryEntry, *, action: str) -> None:
    """Refuse an approval-shaped write of a row whose text was rewritten on write.

    Every path that can store or flip an APPROVED row calls this, not only `approve()`:
    `create(approved=True)`, `import_all` and `approve`. An approved garbled row is not
    injected (and triage flags it), but storing one approved is a silent decision the
    operator never made, so the write is refused until the text is reworded. There is no
    override: DoD 6 says a marked row is unapprovable until reworded.
    """
    if not is_garbled(entry):
        return
    ident = f"#{entry.id}" if entry.id is not None else f"content {entry.content[:60]!r}"
    raise MemoryError_(
        f"{action} refused: {ident} carries a redaction marker or substitution record; "
        "reword it (PATCH content) before approving: the pipeline would otherwise render "
        "the garbled text into every future prompt"
    )


# Injection buckets -> (setting name for the cap).
MEMORY_BUDGETS = {
    "global": "max_memory_tokens_global",
    "role": "max_memory_tokens_role",
    "project": "max_memory_tokens_project",
}


def _effective_settings(session: Session | None) -> dict[str, Any]:
    """Runtime settings (settings table over env defaults) when a session is available.

    Without this the caps would be env-only while `settings_service.EDITABLE` advertises them
    as runtime-editable: PATCH /api/settings and the GUI would accept an edit that injection
    silently ignores — a machine consumer reading the WRONG value instead of failing loudly.
    """
    if session is None:
        return {}
    from orchestrator.services import settings_service  # local: avoid an import cycle

    try:
        values = settings_service.defaults()
        for row in session.exec(select(Setting)):
            if row.key in settings_service.EDITABLE:
                values[row.key] = row.value_json
        return values
    except Exception:  # a half-initialised home must not break prompt building
        return {}


def memory_budgets(session: Session | None = None) -> dict[str, int]:
    s = get_settings()
    values = _effective_settings(session)
    enabled = bool(values.get("memory_budget_enabled", s.memory_budget_enabled))
    if not enabled:
        return {k: 0 for k in MEMORY_BUDGETS}
    return {k: int(values.get(attr, getattr(s, attr))) for k, attr in MEMORY_BUDGETS.items()}


def memory_budgets_enabled(session: Session | None = None) -> bool:
    s = get_settings()
    values = _effective_settings(session)
    return bool(values.get("memory_budget_enabled", s.memory_budget_enabled))


def memory_warn_fraction(session: Session | None = None) -> float:
    return float(
        _effective_settings(session).get(
            "memory_budget_warn_fraction", get_settings().memory_budget_warn_fraction
        )
    )


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
        return tuple(r.value for r, scopes in ROLE_PROJECT_SCOPES.items() if entry.scope in scopes)
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
    return False, str(vocab_error(entry.category, entry.scope or ""))


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
        # The column that decides which prompt reads the row is checked BEFORE the scope, so
        # `role='logbook-writer' scope='worker'` is refused instead of rendering to a file no
        # injector opens. Same refusal from create(), update() and import_all().
        if entry.role:
            check_role_name(entry.role)
        key = role_memory_file(entry.role) or role_memory_file(entry.scope)
        if key is None:
            raise vocab_error(entry.category, entry.scope or entry.role or "")
        if not entry.role:
            # Historic behaviour kept: a role row may carry its role in `scope`.
            entry.role = entry.scope
        entry.scope = key
        return
    entry.scope = normalize_scope(entry.category, entry.scope)


def render_file(session: Session, path: Path) -> None:
    """Re-render one markdown file from all approved entries that map to it.

    A garbled entry (one whose text was rewritten on write) is deliberately left out: the
    current pipeline used to render the marker into every future prompt with no warning.
    The row stays approved in SQLite; triage reports it as needing a reword.
    """
    entries = [
        e
        for e in session.exec(select(MemoryEntry).where(MemoryEntry.approved == True))  # noqa: E712
        if _file_for(e) == path and not is_garbled(e)
    ]
    entries.sort(key=lambda e: e.id or 0)
    title = path.stem
    body = "\n".join(f"- {e.content.strip()}  <!-- mem:{e.id} -->" for e in entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{body}\n" if body else f"# {title}\n")


def create(
    session: Session, data: MemoryCreate, *, override: bool = False
) -> MemoryEntry:
    """Create a row. `approved=True` is an APPROVAL and is refused if it would evict.

    The approval gate lives in `approve()`; a creation path that could write an approved row
    directly would otherwise be the silent-eviction hole T-17 review round 1 found (CLI
    `memory add --approve`, `POST /api/memory {approved:true}`, `memory import`).
    """
    content, redaction_tags = _redact_memory_content(data.content.strip())
    entry = MemoryEntry(
        category=data.category.value,
        scope=data.scope,
        project_slug=slugify(data.project_slug) if data.project_slug else None,
        # `getattr(..., "value", ...)`: MemoryCreate types `role` as the Role enum, so an
        # HTTP caller cannot send an unknown one — but the service boundary must still refuse
        # that shape (an unvalidated object, an import payload, a future caller) with the
        # vocabulary error rather than crashing on `.value`.
        role=getattr(data.role, "value", data.role) or None,
        content=content,
        source=data.source,
        tags_json=_combine_tags(data.tags, redaction_tags, preserve_existing=True),
        approved=data.approved,
    )
    _validate(entry)
    action = f"creating approved entry {entry.content[:60]!r}"
    evicted: list[dict[str, Any]] = []
    if entry.approved:
        _refuse_garbled(entry, action=action)
        evicted = _refuse_eviction(session, entry, override=override, action=action)
    session.add(entry)
    session.commit()
    session.refresh(entry)
    if entry.approved:
        render_file(session, _file_for(entry))
        _log_eviction_override(session, action, evicted)
    return entry


def suggest_from_handoff(
    session: Session,
    suggestions: list[MemorySuggestion],
    source: str,
    project_slug: str | None,
    role: str = "worker",
    *,
    report: bool = False,
) -> list[MemoryEntry] | SuggestionOutcome:
    """The created entries only, or the full outcome with `report=True`.

    The list-returning default is kept for every existing caller. `report=True` is how a test
    (or a new caller) sees the duplicate links and substitution notes without changing the
    shape those callers already use.
    """
    outcome = suggest_from_handoff_report(
        session, suggestions, source, project_slug, role=role
    )
    return outcome if report else outcome.created


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
    all_entries = list_entries(session)
    existing = {(e.category, e.scope, e.project_slug, e.content.strip()) for e in all_entries}
    # The duplicate index is the same rare-token containment triage uses, plus the
    # identifier-subject matcher (rare words alone miss the topical clusters the operator
    # measured). Build both document frequencies over every stored entry plus this batch, so
    # the domain's common words/identifiers are not "rare" merely because a bucket is small
    # (a whole-text ratio would fuse unrelated facts).
    suggestion_texts = [s.content for s in suggestions]
    corpus = [e.content for e in all_entries] + suggestion_texts
    df = build_df(corpus)
    tdf = build_term_df(corpus)
    by_domain: dict[tuple[str, str | None, str | None], list[MemoryEntry]] = {}
    for e in all_entries:
        if not e.approved:
            continue  # only an existing APPROVED fact is a link target
        domain = (
            e.category,
            e.project_slug if e.category == MemoryCategory.project else None,
            role_file_key(e) if e.category == MemoryCategory.role else None,
        )
        by_domain.setdefault(domain, []).append(e)
    for s in suggestions:
        try:
            category = MemoryCategory(s.category)
        except ValueError:
            outcome.note(
                "dropped",
                f"dropped suggestion from {source}: unknown category {s.category!r}; "
                f"allowed: global|project|role",
            )
            continue
        slug = slugify(s.scope)
        try:
            if category == MemoryCategory.role:
                file_key = role_memory_file(slug)
                if file_key is None:
                    raise MemoryError_(str(vocab_error(category, slug)))
                scope = file_key
                role_value = slug if slug in Role.__members__ else role
                create_role = Role(role_value) if role_value in Role.__members__ else Role.worker
            else:
                scope, note = coerce_scope(category, slug)
                if note:
                    outcome.note(
                        "substituted", f"memory suggestion from {source}: {note}"
                    )
                create_role = None
            proj = (
                slugify(project_slug)
                if category == MemoryCategory.project and project_slug
                else None
            )
            redacted_content, _ = _redact_memory_content(
                s.content.strip()
            )
            key = (category, scope, proj, redacted_content)
            if key in existing:
                continue  # byte-identical: no row, exactly as before
            domain = (
                category,
                proj,
                scope if category == MemoryCategory.role else None,
            )
            match = _linked_duplicate(
                redacted_content, by_domain.get(domain, []), df, tdf
            )
            if match is not None:
                existing_id = match["existing_id"]
                reason = _duplicate_reason(match)
                already = next(
                    (
                        e
                        for e in all_entries
                        if duplicate_of(e) == existing_id
                        and not e.rejected
                        and (
                            is_duplicate_pair(e.content, redacted_content, df)[0]
                            or is_subject_pair(e.content, redacted_content, tdf)[0]
                        )
                    ),
                    None,
                )
                if already is not None:
                    record = {
                        "existing_id": existing_id,
                        "entry_id": already.id,
                        "content": redacted_content,
                        "reason": f"already recorded as linked to #{existing_id}",
                    }
                    outcome.linked.append(record)
                    outcome.note(
                        "linked",
                        f"linked suggestion from {source} to existing memory #{existing_id} "
                        f"(already recorded as pending row #{already.id})",
                    )
                    continue
                linked_entry = create(
                    session,
                    MemoryCreate(
                        category=category,
                        scope=scope,
                        content=s.content,
                        project_slug=project_slug if category == MemoryCategory.project else None,
                        role=create_role,
                        tags=list(s.tags or []) + _duplicate_tags(match),
                        source=source,
                        approved=False,
                    ),
                )
                record = {
                    "existing_id": existing_id,
                    "entry_id": linked_entry.id,
                    "content": redacted_content,
                    "reason": reason,
                    "match": match["match"],
                    "ratio": match.get("ratio"),
                    "threshold": match.get("threshold"),
                    "terms": list(match.get("terms", [])),
                }
                outcome.linked.append(record)
                existing.add((category, scope, proj, redacted_content))
                outcome.note(
                    "linked",
                    f"linked suggestion from {source} to existing memory #{existing_id} as "
                    f"pending row #{linked_entry.id}: {reason}",
                )
                all_entries.append(linked_entry)
                continue
            existing.add(key)
            # Deliberately NOT added to `by_domain`: only an existing APPROVED fact is a
            # link target. A row created earlier in this same batch is still pending, so
            # linking a later suggestion to it would record "linked to existing memory
            # #X" for an unapproved #X (T-17 review round 3, finding 3).
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
            outcome.note("dropped", f"dropped suggestion from {source}: {e}")
    return outcome


def _linked_duplicate(
    content: str,
    candidates: list[MemoryEntry],
    df: dict[str, int],
    tdf: dict[str, int],
) -> dict[str, Any] | None:
    """The first existing entry that states `content` (or the same subject), as a record.

    Rare-token containment is tried first (`memory_text.is_duplicate_pair`), then the
    identifier-subject matcher. Both are containment-style predicates on extracted terms;
    neither is a whole-text similarity ratio, which on this corpus fuses unrelated entries.
    Deterministic: candidates are in id order. The returned record carries the matcher's own
    numbers and shared terms, so the tags written to the row can be rebuilt into the reason.
    """
    for candidate in candidates:
        is_dup, ratio, threshold = duplicate_detail(content, candidate.content, df)
        if is_dup and candidate.id is not None:
            return {
                "existing_id": candidate.id,
                "match": "rare-token",
                "ratio": ratio,
                "threshold": threshold,
                "terms": [],
            }
        same_subject, shared = is_subject_pair(content, candidate.content, tdf)
        if same_subject and candidate.id is not None:
            return {
                "existing_id": candidate.id,
                "match": "subject",
                "ratio": None,
                "threshold": None,
                "terms": list(shared),
            }
    return None


def update(
    session: Session, entry_id: int, data: MemoryUpdate, *, override: bool = False
) -> MemoryEntry:
    """Apply an edit. A scope the caller supplies is checked like at creation; a stored
    legacy scope that is out of vocabulary does not freeze the row.

    Validation happens on a throwaway preview BEFORE anything is assigned, so a rejected
    PATCH leaves the stored row and its rendered markdown exactly as they were — the
    "unchanged" guarantee does not depend on session rollback behaviour.

    An edit to an APPROVED row that makes the section cost more (a longer text, or a tag
    change that drops its value rank) is an approval-shaped write: it can push another
    approved row out of the injected section, so it goes through the same eviction gate as
    `create`/`approve`/`import` (T-17 review round 4, finding 4). `override=True` is the
    explicit opt-in and records the casualties in the build log.
    """
    entry = _get(session, entry_id)
    if data.content is not None:
        new_content, redaction_tags = _redact_memory_content(data.content.strip())
    else:
        new_content = entry.content
        redaction_tags = [t for t in entry.tags_json if t.startswith(REDACTION_TAG_PREFIX)]
    new_tags = _combine_tags(
        data.tags if data.tags is not None else entry.tags_json, redaction_tags
    )
    preview = MemoryEntry(
        category=entry.category,
        scope=data.scope if data.scope is not None else entry.scope,
        project_slug=entry.project_slug,
        role=entry.role,
        content=new_content,
    )
    _validate(preview, check_scope=data.scope is not None)
    old_file = _file_for(entry)  # before the mutation: the file this row used to render into
    action = f"editing #{entry.id}"
    evicted: list[dict[str, Any]] = []
    if entry.approved:
        priced = MemoryEntry(
            id=entry.id,
            category=entry.category,
            scope=preview.scope,
            project_slug=entry.project_slug,
            role=preview.role,
            content=new_content,
            tags_json=new_tags,
            approved=True,
        )
        # An edit that would leave an APPROVED row garbled is refused like every other
        # approval-shaped write: the pipeline would stop injecting it with no warning.
        _refuse_garbled(priced, action=action)
        # Only GROWTH is gated: a shorter edit cannot push another row out. A tag change that
        # lowers this row's own value rank demotes the row itself, not a neighbour, and is
        # reported by triage/budget like any other demotion.
        if _line_tokens(new_content) > _line_tokens(entry.content):
            evicted = _refuse_eviction(
                session, priced, override=override, action=action, replacing=True
            )
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
        _log_eviction_override(session, action, evicted)
    return entry


class EvictionRefused(MemoryError_):
    """An approval-shaped write refused because it would demote approved rows.

    Carries the casualty list as structure (`payload`) as well as prose, so the API can put
    it in the frozen `{"error", "detail"}` body and a GUI can act on the ids instead of
    matching a substring of the message (T-17 review round 3, finding 4).
    """

    def __init__(self, message: str, payload: dict[str, Any]):
        super().__init__(message)
        self.payload = payload


def _eviction_impact(
    session: Session, entry: MemoryEntry, *, treat_as_new: bool, replacing: bool = False
) -> dict[str, Any]:
    """Which approved rows `entry` would demote, from the same selection the prompt uses.

    `treat_as_new` is for a row that is not committed yet (create/import): the entry is
    priced through `also_approved`, and an already-approved row is not double-counted.

    `replacing` is the EDIT shape (T-17 review round 4): the row keeps its id and approval but
    its new text/tags stand in for the stored row, so the price is computed against the
    selection with that one row substituted — not added beside itself.

    The row ITSELF is included (`"self": True`) when approving it would leave it out of
    the injected section. That is the common rot-prone-pending case: the approval would
    succeed, the CLI would print "approved #N", and nothing would say #N never reaches a
    prompt. It is a casualty like any other, so it is refused by default and recorded on
    override.
    """
    if entry.id is not None and entry.approved and not replacing:
        return {"entry_id": entry.id, "evicted": []}
    # An uncommitted row has id None, and `_keep_priority` treats None as the OLDEST id, so
    # pricing it directly would demote the new row itself and report "no eviction". Price a
    # detached copy with a synthetic newest id instead, without touching the row that will be
    # inserted (assigning an id on a session-bound object would insert that primary key).
    priced = entry
    replace: list[MemoryEntry] | None = None
    if entry.id is None:
        newest = max((e.id or 0 for e in list_entries(session)), default=0)
        priced = MemoryEntry(
            category=entry.category,
            scope=entry.scope,
            project_slug=entry.project_slug,
            role=entry.role,
            content=entry.content,
            tags_json=list(entry.tags_json or []),
            approved=True,
        )
        priced.id = newest + 1
    elif replacing:
        replace = [entry]
    projects: tuple[str | None, ...] = (
        (priced.project_slug,) if priced.category == MemoryCategory.project else (None,)
    )
    evicted: dict[int, dict[str, Any]] = {}
    for role in (Role.architect.value, Role.worker.value, Role.supervisor.value):
        for project_slug in projects:
            before = select_for_prompt(session, role, project_slug)
            if replace is not None:
                after = select_for_prompt(
                    session, role, project_slug, replace_approved=replace
                )
            else:
                after = select_for_prompt(session, role, project_slug, also_approved=[priced])
            already = {x["id"] for x in before.excluded}
            for row in after.excluded:
                if row["id"] in already:
                    continue
                is_self = entry.id is not None and row["id"] == entry.id
                if row["id"] == priced.id and not is_self:
                    # The synthetic newest id of a create/import price: never a casualty.
                    continue
                record = evicted.setdefault(
                    row["id"],
                    {
                        "id": row["id"],
                        "bucket": row["bucket"],
                        "scope": row["scope"],
                        "tokens": row.get("tokens", 0),
                        "reason": row.get("reason", ""),
                        "roles": [],
                        "self": is_self,
                    },
                )
                if is_self:
                    record["self"] = True
                if role not in record["roles"]:
                    record["roles"].append(role)
    return {"entry_id": entry.id, "evicted": sorted(evicted.values(), key=lambda r: r["id"])}


def _casualty_text(row: dict[str, Any]) -> str:
    if row.get("self"):
        return (
            f"#{row['id']} ({row['bucket']}/{row['scope']}; this row itself would not be "
            "injected)"
        )
    return (
        f"#{row['id']} ({row['bucket']}/{row['scope']}; roles "
        f"{','.join(row['roles']) or 'none'})"
    )


def _log_eviction_override(
    session: Session, action: str, evicted: list[dict[str, Any]]
) -> None:
    """Record the demoted ids when an operator explicitly overrode the eviction gate.

    The approval returned is the ACTUAL result (the row is approved), and this event is
    what says which approved rows will no longer be injected as a result. Nothing here
    runs on the refused path, so a refusal is never recorded as an override.
    """
    if not evicted:
        return
    from orchestrator.services import log_service  # local: keep the module import light

    ids = ", ".join(f"#{row['id']}" for row in evicted)
    self_ids = [f"#{row['id']}" for row in evicted if row.get("self")]
    details = f"{action} overrode the eviction guard; demoted from injection: {ids}"
    if self_ids:
        details += f" (including the approved row itself: {', '.join(self_ids)})"
    log_service.log_event(session, "memory_eviction_override", details)


def _refuse_eviction(
    session: Session,
    entry: MemoryEntry,
    *,
    override: bool,
    action: str,
    replacing: bool = False,
) -> list[dict[str, Any]]:
    """Refuse an approval-shaped write that would evict an approved row; return casualties.

    Shared by `create(approved=True)`, `import_all`, `approve()` and an edit that grows or
    devalues an approved row (`replacing=True`). `override=True` is the explicit operator
    opt-in; even then nothing is deleted, the evicted rows are reported by budget/triage as
    not injected and the caller records their ids in a build-log event via
    `_log_eviction_override`. `action` names the write in the message, so a caller can tell
    an import refusal from a CLI add.
    """
    impact = _eviction_impact(session, entry, treat_as_new=True, replacing=replacing)
    if not impact["evicted"] or override:
        return impact["evicted"]
    casualties = ", ".join(_casualty_text(row) for row in impact["evicted"])
    payload = {
        "action": action,
        "entry_id": impact["entry_id"],
        "evicted": impact["evicted"],
    }
    raise EvictionRefused(
        f"{action} would evict approved entr"
        f"{'y' if len(impact['evicted']) == 1 else 'ies'} {casualties} from the injected "
        "section; nothing was written. Condense/reword first, or repeat with override=true "
        "(the evicted rows stay approved but are not injected)",
        payload,
    )


def approval_impact(session: Session, entry_id: int) -> dict[str, Any]:
    """Which approved entries a pending approval would demote, and in which section.

    Memory is a zero-sum static section: approving over a cap evicts another approved row.
    The comparison is the same deterministic selection the prompt will use, so the answer
    cannot drift from the render.
    """
    entry = _get(session, entry_id)
    if entry.approved:
        return {"entry_id": entry.id, "evicted": []}
    return _eviction_impact(session, entry, treat_as_new=True)


def approve(session: Session, entry_id: int, *, override: bool = False) -> MemoryEntry:
    """Approve a row. Over-cap eviction is refused by default and explained.

    `override=True` is the explicit operator opt-in; even then the evicted rows stay
    approved in SQLite and are reported by triage/budget, never deleted.
    """
    entry = _get(session, entry_id)
    action = f"approving #{entry.id}"
    _refuse_garbled(entry, action=action)
    evicted = _refuse_eviction(session, entry, override=override, action=action)
    entry.approved, entry.rejected, entry.updated_at = True, False, utcnow()
    session.add(entry)
    session.commit()
    render_file(session, _file_for(entry))
    _log_eviction_override(session, action, evicted)
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
        if p not in owned and in_layout_dir(p) and not canonical_file(p) and _renderer_output(p)
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
            _file_for(e) for e in list_entries(session, status="approved") if e.id != entry.id
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
    """Approved memory for a role, split into the three cached prefix sections.

    Convenience wrapper around `select_for_prompt`; use the latter when you need the
    exclusion report (prompt preview, `orchestrator memory triage`).
    """
    return select_for_prompt(session, role, project_slug).sections


def _tag_rank(entry: MemoryEntry) -> int:
    """Explicit operator value: pinned/durable, then tier:1, tier:2/untagged, tier:3."""
    tags = {t.strip().lower() for t in (entry.tags_json or [])}
    if tags & PINNED_TAGS:
        return 4
    for tag, rank in TIER_TAGS.items():
        if tag in tags:
            return rank
    return DEFAULT_TIER_RANK


def _keep_priority(entry: MemoryEntry) -> tuple[int, int, int]:
    """Higher = keep. Pin/tier from `tags_json` first, then content class, then id last.

    No new column: the value signal is the existing tags_json. An untagged set keeps the
    pre-T-13 behaviour exactly (rot-prone demoted first, then the oldest id), while a
    pinned entry is demoted last and an unpinned tier:3 entry first. Content-derived only:
    no clock, no file access, so the rendered text stays byte-identical across calls.
    """
    return (
        _tag_rank(entry),
        0 if is_rot_prone(entry.content) else 1,
        entry.id or 0,
    )


def _exclusion_reason(entry: MemoryEntry, bucket: str, budget: int) -> str:
    tags = {t.strip().lower() for t in (entry.tags_json or [])}
    if tags & PINNED_TAGS:
        return (
            f"pinned/durable but the {bucket} section still cannot fit it within its "
            f"budget of {budget} tokens"
        )
    if "tier:3" in tags:
        return (
            f"tagged tier:3 (lowest value) and the {bucket} section is over its budget "
            f"of {budget} tokens"
        )
    if is_rot_prone(entry.content):
        return (
            f"rot-prone (names a task/round/SHA) and the {bucket} section is over "
            f"its budget of {budget} tokens"
        )
    return (
        f"no pinned/durable/tier tag and the {bucket} section is over its budget of "
        f"{budget} tokens; id #{entry.id} is only the last tiebreak"
    )


def _line_tokens(content: str) -> int:
    return estimate_tokens(f"- {content.strip()}\n")


def _select_bucket(
    bucket: str, entries: list[MemoryEntry], budget: int
) -> tuple[list[MemoryEntry], list[dict[str, Any]]]:
    """Keep entries within `budget` tokens; return (kept sorted by id, excluded with reasons).

    `budget <= 0` disables the cap. Rule, stated so it is reviewable: if the bucket already
    fits, everything is injected. If it does not, demote in `_keep_priority` order -- tag
    value (`pinned`/`durable`, then `tier:1`..`tier:3`), then content class (rot-prone
    first), and id only as the final tiebreak. `id` is a timestamp proxy, not a measure of
    worth; every exclusion carries a reason that prompt preview, triage and budget show.
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
        excluded.append(
            {
                "id": entry.id,
                "bucket": bucket,
                "scope": entry.scope,
                "tokens": cost[entry.id],
                "reason": _exclusion_reason(entry, bucket, budget),
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
    replace_approved: list[MemoryEntry] | None = None,
) -> MemorySelection:
    """Approved memory for a role, capped per injected section (see MEMORY_BUDGETS).

    `also_approved` is a what-if list of unapproved entries treated as approved for the
    calculation only (used by `orchestrator memory triage` to price a batch). Nothing is
    written.

    `replace_approved` is the EDIT what-if: each listed row stands in for the stored row with
    the same id (same approval, new text/tags), which is how `update()` prices an edit without
    adding the row beside itself. Nothing is written.

    A garbled entry (redaction marker or substitution record) is reported as excluded and
    is never injected, because it is not a fact the operator authored.

    Memory is a static prompt section, so `prompt_builder.enforce_budget` can never trim it;
    uncapped growth squeezes diff/handoff/verification evidence instead. Capping happens here
    so the squeeze is visible, reviewable and reported.
    """
    budgets = budgets if budgets is not None else memory_budgets(session)
    approved = list_entries(session, status="approved")
    if replace_approved:
        replaced = {e.id for e in replace_approved}
        approved = [e for e in approved if e.id not in replaced] + list(replace_approved)
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
        garbled = sorted((e for e in entries if is_garbled(e)), key=lambda e: e.id or 0)
        usable = [e for e in entries if not is_garbled(e)]
        kept, excluded = _select_bucket(bucket, usable, budgets.get(bucket, -1))
        selection.excluded.extend(excluded)
        for e in garbled:
            selection.excluded.append(
                {
                    "id": e.id,
                    "bucket": bucket,
                    "scope": e.scope,
                    "tokens": _line_tokens(e.content),
                    "reason": (
                        "contains a redaction marker or substitution record; reword it before "
                        "it can be injected"
                    ),
                }
            )
        by_scope: dict[str, list[str]] = {}
        for e in kept:
            by_scope.setdefault(e.scope, []).append(e.content.strip())
        text = "\n".join(
            f"### {scope}\n" + "\n".join(f"- {c}" for c in items)
            for scope, items in sorted(by_scope.items())
        )
        selection.sections[bucket] = text
        selection.used[bucket] = estimate_tokens(text)
    selection.warnings = _budget_warnings(selection, session)
    return selection


def _budget_warnings(selection: MemorySelection, session: Session | None = None) -> list[str]:
    """Tripwire before the guillotine: warn as a section approaches its cap."""
    if not memory_budgets_enabled(session):
        return []
    frac = memory_warn_fraction(session)
    out = []
    for bucket, cap in selection.budgets.items():
        if cap <= 0:
            continue
        used = selection.used.get(bucket, 0)
        if used >= frac * cap:
            demoted = [x for x in selection.excluded if x["bucket"] == bucket]
            names = ", ".join(f"#{x['id']}" for x in demoted)
            out.append(
                f"memory section '{bucket}' is at {used}/{cap} tokens "
                f"({round(100 * used / cap)}% of its cap)"
                + (
                    f"; already demoted: {names}"
                    if demoted
                    else ""
                )
                + " - condense or merge entries before approving more"
            )
    return out


# ---------------------------------------------------------------- export/import


def condense(
    session: Session,
    project_slug: str,
    *,
    apply_ids: list[int] | None = None,
    rewrite_content: str | None = None,
    override: bool = False,
) -> dict[str, Any]:
    """Report merge candidates; write only when the caller names the exact ids.

    A dry run (the default) changes no row and no rendered file. `apply_ids` must name the
    complete id set of one or more proposals (survivor + every absorbed row); a partial or
    unknown set is refused before anything is written. Applying means `update()` on the
    survivor and `reject()` on the absorbed rows, both through this module, so redaction and
    markdown re-rendering stay on the one sanctioned path.

    `project_slug` is required: condense is a per-project operation, and the old optional form
    grouped global and role rows with project rows into one "proposal". `rewrite_content` is
    the explicit way to strip task framing while keeping the rule; it is only allowed for one
    proposal. Without it the proposal's own `content` is written -- for a subject cluster that
    is the union of every member's text, so no absorbed fact is dropped.

    Losing a fact is worse than keeping a duplicate, so an apply without `--content` is
    REFUSED whenever the surviving text does not literally contain an absorbed row's content
    (T-17 review round 4, finding 2). The rare-token branch keeps the survivor's wording, so
    it can only be applied with an explicit `--content`; the subject branch writes the union
    and passes. `override` is the same explicit opt-in as the approval gate, needed when the
    longer survivor would push another approved row out of the capped section.
    """
    from orchestrator.services import memory_triage as mt  # local: avoids an import cycle

    proposals = mt.condense_proposals(session, project_slug)
    result: dict[str, Any] = {
        "project_slug": project_slug,
        "proposals": proposals,
        "applied": [],
        "dry_run": True,
    }
    if not apply_ids:
        return result
    try:
        requested = {int(i) for i in apply_ids}
    except (TypeError, ValueError) as e:
        raise MemoryError_(f"--apply needs integer memory ids: {e}") from e
    known = {i for p in proposals for i in p["ids"]}
    unknown = sorted(requested - known)
    if unknown:
        raise MemoryError_(
            "refusing to apply: "
            + ", ".join(f"#{i}" for i in unknown)
            + " is not part of any current merge candidate (run the dry run first)"
        )
    touched = [p for p in proposals if requested & set(p["ids"])]
    incomplete = [p for p in touched if not set(p["ids"]).issubset(requested)]
    if incomplete:
        missing = sorted({i for p in incomplete for i in p["ids"]} - requested)
        raise MemoryError_(
            "refusing a partial merge: --apply must also name "
            + ", ".join(f"#{i}" for i in missing)
        )
    if rewrite_content is not None and len(touched) != 1:
        raise MemoryError_(
            "--content can rewrite exactly one surviving row; name one proposal's ids"
        )
    for proposal in touched:
        survivor_id = proposal["survivor"]
        new_content = rewrite_content if rewrite_content is not None else proposal["content"]
        if rewrite_content is None:
            # Literal containment, BOTH branches: a reworded duplicate's survivor does not
            # contain the absorbed wording, so it needs an explicit --content. Never absorb
            # a row whose text the surviving row does not carry.
            dropped = [
                absorbed_id
                for absorbed_id in proposal["absorbed"]
                if _get(session, absorbed_id).content.strip() not in new_content
            ]
            if dropped:
                raise MemoryError_(
                    "refusing to apply: the surviving text of #"
                    f"{survivor_id} does not contain the content of "
                    + ", ".join(f"#{i}" for i in dropped)
                    + "; pass --content carrying every absorbed fact, because losing a fact "
                    "is worse than keeping a duplicate"
                )
        update(
            session,
            survivor_id,
            MemoryUpdate(content=new_content, tags=proposal["tags"]),
            override=override,
        )
        for absorbed_id in proposal["absorbed"]:
            reject(session, absorbed_id)
        result["applied"].append(
            {
                "survivor": survivor_id,
                "rejected": proposal["absorbed"],
                "ids": proposal["ids"],
                "distinct_facts": proposal["distinct_facts"],
                "tokens_saved": proposal["tokens_saved"],
            }
        )
    result["dry_run"] = False
    return result


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


def _import_keys(
    category: str, scope: str, project_slug: str | None, role: str | None, content: str
) -> set[tuple[Any, ...]]:
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


def import_all(session: Session, payload: dict[str, Any], *, override: bool = False) -> int:
    """Import entries; skips duplicates (same category/scope/project/role/content).

    T-4: an export written before scope validation existed carries free-form scopes, so the
    scope is repaired on the way in rather than dropping a good fact over a vocabulary
    mistake — and the duplicate check runs on BOTH the stored and the REPAIRED scope, so
    importing a legacy export neither duplicates a row that is still awaiting `fix-scopes`
    nor re-adds anything on a second pass.

    An imported APPROVED row is an approval and is refused by default when it would evict an
    already-approved row (`override=True` restores the old restore-the-backup behaviour).
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise MemoryError_("invalid memory export: expected {'entries': [...]}")
    existing: set[tuple[Any, ...]] = set()
    for e in list_entries(session):
        existing |= _import_keys(e.category, e.scope, e.project_slug, e.role, e.content)
    count = 0
    overridden: list[dict[str, Any]] = []
    touched: set[Path] = set()
    for raw in payload["entries"]:
        content, redaction_tags = _redact_memory_content(str(raw.get("content", "")).strip())
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
            tags_json=_combine_tags(
                list(raw.get("tags_json") or []), redaction_tags, preserve_existing=True
            ),
            approved=bool(raw.get("approved")),
            rejected=bool(raw.get("rejected")),
        )
        raw_keys = _import_keys(category, entry.scope, entry.project_slug, entry.role, content)
        if raw_keys & existing:
            continue
        try:
            if entry.category in FALLBACK_SCOPE:
                entry.scope, _ = coerce_scope(entry.category, entry.scope)
            _validate(entry)
        except MemoryError_:
            continue
        if entry.approved:
            action = f"importing approved entry {content[:60]!r}"
            _refuse_garbled(entry, action=action)
            overridden.extend(
                _refuse_eviction(session, entry, override=override, action=action)
            )
        key = (entry.category, entry.scope, entry.project_slug, entry.role, entry.content)
        existing |= raw_keys | {key}
        session.add(entry)
        count += 1
        if entry.approved:
            touched.add(_file_for(entry))
    session.commit()
    for path in touched:
        render_file(session, path)
    _log_eviction_override(session, "importing an approved memory export", overridden)
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
