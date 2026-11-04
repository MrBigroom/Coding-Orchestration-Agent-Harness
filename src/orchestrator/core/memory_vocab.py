"""The memory scope vocabularies — the only scopes any injector can read.

Lives in `core`, BELOW both `core/schemas.py` and `services/memory_service.py`, for one
reason: the API request model and the service boundary must refuse the same shapes with the
same words (T-4 DoD 2), and `memory_service` imports `schemas`, so `schemas` cannot import
`memory_service` without a circular import. The vocabulary is the shared contract; both
boundaries read it from here. `memory_service` re-exports every name in this module, so
`memory_service.GLOBAL_SCOPES`, `m.coerce_scope`, … keep resolving exactly as before.

  memory/global/{environment,preferences,patterns}.md
  memory/projects/<slug>/{conventions,decisions,gotchas}.md
  memory/roles/{architect,worker,supervisor}.md

A row stored with a scope outside its own category's vocabulary is invisible to every prompt
(task T-4), so every boundary checks MEMBERSHIP here and not just shape.
"""

from __future__ import annotations

from orchestrator.core.models import MemoryCategory, Role
from orchestrator.utils.slug import slugify

# ------------------------------------------------------------------ vocabularies
# These three tuples are the ONLY scopes any injector can read. Tests import these names.
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


# ------------------------------------------------------------------ resolution


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


def check_role_name(name: str) -> str:
    """Validate a ROLE NAME, or raise an error naming the role vocabulary.

    The `role` column — not `scope` — is what `for_prompt` selects on and what `_file_for`
    keys the filename off first, so an out-of-vocabulary role is the *more* damaging of the
    two: `role='logbook-writer' scope='worker'` renders to `roles/logbook-writer.md`, which
    no prompt reads, while looking injectable to a naive scope-only check. This is the single
    message used by `_validate` (so create, update and import all refuse the same shape for
    the same reason), by the CLI's `--role` (a plain string), and by `MemoryCreate`'s
    request-model validator, which is what makes the API's 422 name the role FILES instead
    of Pydantic's bare enum list.
    """
    if role_memory_file(name) is None:
        aliases = ", ".join(f"{k} -> {v}" for k, v in sorted(ROLE_SCOPE_ALIASES.items()))
        raise MemoryError_(
            f"role name {name!r} cannot be injected; allowed role memory files: "
            f"{'|'.join(ROLE_SCOPES)} (role names {aliases} are accepted and resolve to "
            f"their file)"
        )
    return name


def vocab_error(category: str, scope: str) -> MemoryError_:
    """The rejection message for one category, always naming that category's vocabulary."""
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
            raise vocab_error(category, scope or "")
        return key
    slug = slugify(scope or "")
    if slug in vocab:
        return slug
    raise vocab_error(category, scope or "")


def substitution_note(category: str, requested: str, stored: str) -> str:
    return (
        f"{category} scope {requested!r} is not in "
        f"{'|'.join(SCOPE_VOCABULARY.get(category, ()))}; stored as {stored!r} "
        f"(deterministic fallback, not inferred from the words)"
    )


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
    return fallback, substitution_note(category, scope, fallback)
