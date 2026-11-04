"""Read-only memory triage: budget accounting, rot/staleness checks, merge candidates.

Three questions, answered deterministically from stored rows plus live project state:

1. `budget_projection` — what does approved memory cost per role now, and what would
   approving the whole pending queue cost? Memory is a *static* prompt section, so
   `prompt_builder.enforce_budget` can never trim it; uncapped growth squeezes the dynamic
   evidence (diff, handoff, verification) instead.
2. `audit_entry` — is this entry still true, and will it stay true? Content-derived rot
   flags come from `utils/memory_text`; live checks compare claims against the project
   checkout (paths, branches) and the `projects` row (test/lint commands).
3. `triage` — one row per entry: flags, groupings, token cost, and a suggested action.

Nothing here writes. Approving, rejecting and editing stay in `memory_service` and the
existing CLI/API/GUI paths, so the gate remains human by construction. Live checks are
advisory: an unreadable checkout yields "unknown", never "false".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from orchestrator.core.models import MemoryEntry, Project, Role
from orchestrator.services import memory_service
from orchestrator.utils.git import is_repo, run_git
from orchestrator.utils.memory_text import (
    ROT_FLAGS,
    build_df,
    entity_terms,
    is_duplicate_pair,
    referenced_paths,
    rot_flags,
)
from orchestrator.utils.truncate import estimate_tokens

TRIAGE_ROLES = (Role.architect, Role.worker, Role.supervisor)
BUCKETS = ("global", "role", "project")
ACTION_ORDER = (
    "fix-stale",
    "rewrite-durable",
    "merge",
    "amend",
    "split",
    "verify-claims",
    "approve",
)

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "worktrees",
        "site-packages",
    }
)

_BRANCH_CLAIM = re.compile(
    r"\b(?:branch|worktree branch)\s+([A-Za-z0-9][A-Za-z0-9._/-]{2,60})", re.I
)
# Prose that reads like a branch name but is not one. A real branch reference here always
# carries a separator (task-6, phase-8-doc-authoring) -- 'branch names' does not.
_BRANCH_STOPWORDS = frozenset(
    {
        "names",
        "name",
        "exists",
        "not",
        "the",
        "and",
        "only",
        "current",
        "new",
        "same",
        "each",
        "every",
        "your",
        "its",
        "creation",
        "history",
    }
)
_NO_TEST_CMD = re.compile(
    r"\btest (?:command|config|runner)s?\b[^.]{0,60}"
    r"\b(not configured|not set|null|none|missing|absent|unset)\b",
    re.I,
)
# Only merge-state *claims* are stale signals. Bare "unmerged" is too common in prose about
# hypothetical damage ("an unmerged branch would migrate the live DB") to be worth a flag.
_UNMERGED = re.compile(
    r"\b(?:pending merge|not yet merged|not merged yet|still unmerged|awaiting merge)\b", re.I
)
_FLAG_LABELS = dict(ROT_FLAGS)
# Codes that mean "this entry asserts something that is not true right now".
_HARD_STALE = frozenset(
    {"contradicts-live-config", "stale-branch-claim", "stale-merge-claim", "unknown-project"}
)
# Codes that mean "this entry will not be true once its task closes".
_TASK_SHAPED = frozenset({"task_shape", "bare_sha", "forward_looking"})


@dataclass
class RepoIndex:
    """Relative paths of one checkout, so entry claims about files and dirs can be tested."""

    root: Path | None
    paths: list[str] = field(default_factory=list)
    dirs: frozenset[str] = frozenset()
    usable: bool = False
    top: frozenset[str] = frozenset()

    def has(self, claim: str) -> bool:
        """Present in the working tree as a file *or a directory* (entries cite both)."""
        c = claim.strip("/").lower()
        if not c:
            return True
        if c in self.dirs:
            return True
        base = c.split("/")[-1]
        for p in self.paths:
            if p == c or p.endswith("/" + c) or c.endswith("/" + p):
                return True
            if len(base) > 4 and p.split("/")[-1] == base:
                return True
        for d in self.dirs:
            if d == c or d.endswith("/" + c) or c.endswith("/" + d):
                return True
        return False

    def was_committed(self, claim: str) -> bool:
        """Was this path ever committed on any branch?

        A gated run commits its evidence on the phase branch, so a file that exists only
        there (or existed and was later removed) is not a false claim. Absence from the base
        working tree is not proof of falsehood.
        """
        if not (self.root and is_repo(self.root)):
            return False
        for spec in (claim, f"{claim}/*"):
            r = run_git(
                ["log", "--all", "--diff-filter=A", "--max-count=1", "--name-only", "--", spec],
                self.root,
                check=False,
            )
            if r.code == 0 and r.stdout.strip():
                return True
        return False

    def repo_shaped(self, claim: str) -> bool:
        """Is this a claim about a repository path, or just prose with a slash in it?

        'timeout/failed/cancelled' and 'architect-claude-cli/opus' are prose. A real claim
        either carries a file extension or starts with a directory that exists at the repo
        top. Brace expansions ('web/{src,index.html,tsconfig.json}') are shell syntax, not
        paths, and anything inside a pruned directory (node_modules, .venv, dist) is not
        evidence: we do not index it, so we must not call it missing.
        """
        c = claim.strip("/").lower()
        if any(ch in c for ch in "{}*"):
            return False
        segs = c.split("/")
        if any(seg in SKIP_DIRS for seg in segs):
            return False
        if segs[0] in _RUNTIME_PREFIXES or segs[0].rstrip("/").lower() == "orchestrator_home":
            return False
        if segs[-1] in _RUNTIME_BASENAMES:
            return False
        if _FILE_EXT.search(c):
            return True
        return len(segs) > 1 and segs[0] in self.top

    def has_runtime(self, claim: str) -> bool:
        """True when the claim matches an orchestrator runtime path (memory tree, worktrees)."""
        c = claim.strip("/").lower()
        base = c.split("/")[-1]
        return any(p == c or p.endswith("/" + c) or p == base for p in _runtime_paths())


def _split_top(paths: list[str]) -> frozenset[str]:
    return frozenset({p.split("/")[0] for p in paths})


def index_repo(project: Project | None, limit: int = 30_000) -> RepoIndex:
    if not project:
        return RepoIndex(None)
    root = Path(project.path)
    if not root.is_dir():
        return RepoIndex(root)
    idx = RepoIndex(root, usable=True)
    dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]
        rel = Path(dirpath).relative_to(root).as_posix()
        if rel != ".":
            dirs.add(rel.lower())
        for name in filenames:
            idx.paths.append(f"{rel}/{name}".lower() if rel != "." else name.lower())
            if len(idx.paths) >= limit:
                break
    idx.dirs = frozenset(dirs)
    idx.top = _split_top(idx.paths) | {d.split("/")[0] for d in dirs}
    return idx


_runtime_cache: dict[str, list[str]] = {}


def _runtime_paths() -> list[str]:
    """Paths under the memory tree: entries cite role/global/project markdown files there."""
    root = memory_service.memory_root()
    key = str(root)
    if key not in _runtime_cache:
        out: list[str] = []
        if root.is_dir():
            for f in root.rglob("*.md"):
                out.append(f.relative_to(root).as_posix().lower())
        _runtime_cache[key] = out
    return _runtime_cache[key]


def _project_for(session: Session, entry: MemoryEntry) -> Project | None:
    if not entry.project_slug:
        return None
    return session.exec(select(Project).where(Project.slug == entry.project_slug)).first()


# Paths that live under ORCHESTR_HOME at run time rather than in the checkout.
# Roots that live under ORCHESTRATOR_HOME, not in a checkout: claiming a file there is not a
# claim about repository layout, and the checker has no business calling it missing.
_RUNTIME_PREFIXES = frozenset(
    {
        "runs",
        ".orchestrator",
        "worktrees",
        "verify-evidence",
        "evidence",
        "logs",
        "memory",
        "chats",
        "agent-outputs",
        "orchestrator_home",
        "build-log.jsonl",
    }
)
_RUNTIME_BASENAMES = (
    "handoff.json",
    "handoff.md",
    "run.json",
    "review.json",
    "review.md",
    "review.raw.txt",
    "review_payload.md",
    "verification.json",
    "verification.md",
    "prompt.md",
    "patch.diff",
    "diffstat.txt",
    "test.log",
    "test_summary.txt",
    "stdout.log",
    "stderr.log",
)
_FILE_EXT = re.compile(
    r"\.(?:py|md|sh|ts|tsx|js|jsx|json|sql|toml|html|txt|log|lock|diff|yaml|yml|ini|cfg)$"
)


def _looks_like_branch(name: str) -> bool:
    n = name.strip(".,;:").lower()
    if n in _BRANCH_STOPWORDS or not re.search(r"[/_-]|\d", n):
        return False
    return True


def audit_entry(
    session: Session,
    entry: MemoryEntry,
    *,
    repo_cache: dict[str, RepoIndex] | None = None,
) -> list[dict[str, str]]:
    """Rot flags from the text, then live claim checks against the project checkout."""
    out: list[dict[str, str]] = [
        {"code": c, "detail": _FLAG_LABELS.get(c, c)} for c in rot_flags(entry.content)
    ]
    project = _project_for(session, entry)
    if entry.category == "project" and not project:
        out.append(
            {
                "code": "unknown-project",
                "detail": f"no registered project has slug '{entry.project_slug}'",
            }
        )
        return out
    if project is None:
        return out

    cache = repo_cache if repo_cache is not None else {}
    idx = cache.setdefault(project.slug, index_repo(project))
    if not idx.usable:
        out.append(
            {
                "code": "repo-unavailable",
                "detail": f"{project.path} is not a directory; path claims not checked",
            }
        )
        return out

    for claim in referenced_paths(entry.content):
        if not idx.repo_shaped(claim) or idx.has_runtime(claim):
            continue
        if not idx.has(claim) and not idx.was_committed(claim):
            first = claim.strip("/").lower().split("/")[0]
            memory_shaped = first in {"roles", "global", "projects"}
            out.append(
                {
                    "code": "missing-path",
                    "detail": (
                        f"'{claim}' is in neither the memory tree nor any branch of "
                        f"{project.slug} (a hypothetical path, or a rename)"
                        if memory_shaped
                        else f"'{claim}' is in neither the {project.slug} working tree nor "
                        "its history (a hypothetical path, or a rename)"
                    ),
                }
            )
    for branch in _BRANCH_CLAIM.findall(entry.content):
        name = branch.strip(".,;:")
        if not _looks_like_branch(name) or not is_repo(project.path):
            continue
        exists = (
            run_git(
                ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
                project.path,
                check=False,
            ).code
            == 0
        )
        if not exists:
            out.append(
                {
                    "code": "stale-branch-claim",
                    "detail": f"branch '{name}' no longer exists in {project.slug}",
                }
            )
    if project.test_command and _NO_TEST_CMD.search(entry.content):
        out.append(
            {
                "code": "contradicts-live-config",
                "detail": f"entry claims there is no test command; the project runs "
                f"'{project.test_command}'",
            }
        )
    if _UNMERGED.search(entry.content):
        out.append(
            {
                "code": "stale-merge-claim",
                "detail": "entry describes work as unmerged; re-check against "
                f"{project.base_branch}",
            }
        )
    return out


def duplicate_groups(entries: list[MemoryEntry]) -> list[list[int]]:
    """Pending groups that state the same fact in different words.

    Uses rare-token containment, never a whole-text ratio: on a single-project corpus every
    pair shares the domain vocabulary, and ratio-based dedup collapses everything (measured:
    461 of 595 pairs above 0.72 while the true duplicate count was zero).
    """
    if len(entries) < 2:
        return []
    df = build_df([e.content for e in entries])
    parent = {e.id: e.id for e in entries}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    content = {e.id: e.content for e in entries}
    ids = [e.id for e in entries]
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            dup, _ratio = is_duplicate_pair(content[a], content[b], df)
            if dup:
                root_a, root_b = find(a), find(b)
                if root_a != root_b:
                    parent[root_b] = root_a
    groups: dict[int, list[int]] = {}
    for e in entries:
        groups.setdefault(find(e.id), []).append(e.id)
    return sorted((sorted(g) for g in groups.values() if len(g) > 1), key=lambda g: g[0])


def related_approved(
    session: Session, entry: MemoryEntry, *, min_shared: int = 2
) -> list[dict[str, Any]]:
    """Approved rows about the same code entities — candidates to AMEND instead of add."""
    mine = entity_terms(entry.content)
    if not mine:
        return []
    hits: list[dict[str, Any]] = []
    for other in memory_service.list_entries(session, status="approved"):
        if other.project_slug != entry.project_slug or other.id == entry.id:
            continue
        shared = mine & entity_terms(other.content)
        if len(shared) < min_shared:
            continue
        hits.append(
            {
                "id": other.id,
                "scope": other.scope,
                "shared_entities": sorted(shared)[:6],
                "overlap": round(len(shared) / max(1, len(mine)), 2),
            }
        )
    hits.sort(key=lambda h: -len(h["shared_entities"]))
    return hits[:3]


def _suggest(
    flags: list[dict[str, str]],
    dups: list[int],
    amends: list[dict[str, Any]],
    tokens: int,
    budget_left: int,
    status: str = "pending",
) -> tuple[str, str]:
    """One suggested action + reason. Advisory only; the operator decides.

    The budget branch answers a question about a row that has NOT been approved yet ("would
    this fit?"). Approved rows are already injected or already demoted, and that fact is
    reported by `warnings` and `excluded`, so applying it there would mislabel a full
    section as four unrelated rows each needing a merge.
    """
    codes = {f["code"] for f in flags}
    if codes & _HARD_STALE:
        return (
            "fix-stale",
            "asserts state that is not true now; leaving it injected teaches the false claim",
        )
    if codes & _TASK_SHAPED:
        return (
            "rewrite-durable",
            "task/round-shaped: true today, false once that task closes. Keep the durable "
            "rule, move the task detail to docs or the build log",
        )
    if dups:
        return "merge", "same fact as " + ", ".join(f"#{i}" for i in dups) + "; keep one row"
    if amends:
        return (
            "amend",
            "overlaps approved "
            + ", ".join(f"#{a['id']}" for a in amends)
            + "; edit that entry instead of adding a row",
        )
    if "multi_fact" in codes:
        return "split", "several facts in one entry; it cannot be retracted or edited singly"
    if "missing-path" in codes:
        missing = [f["detail"] for f in flags if f["code"] == "missing-path"]
        return "verify-claims", "; ".join(missing[:2])
    if status == "pending" and budget_left - tokens < 0:
        return (
            "merge",
            f"no room in the section budget ({budget_left} tok left, entry costs {tokens})",
        )
    if {"absolute_date", "relative_time"} & codes:
        return "rewrite-durable", "date/relative-time anchored; state the rule, not the day"
    return "approve", "no flags and it fits the section budget"


def triage(
    session: Session,
    *,
    status: str = "pending",
    project_slug: str | None = None,
    role: str = Role.worker.value,
) -> dict[str, Any]:
    """The triage report: one row per entry with flags, groupings, cost, suggestion."""
    entries = memory_service.list_entries(session, status=status, project_slug=project_slug)
    groups = duplicate_groups(entries)
    group_of: dict[int, list[int]] = {}
    for g in groups:
        for i in g:
            group_of[i] = [x for x in g if x != i]

    budgets = memory_service.memory_budgets(session)
    current = memory_service.select_for_prompt(session, role, project_slug)
    left = {b: max(0, budgets.get(b, 0) - current.used.get(b, 0)) for b in BUCKETS}

    repo_cache: dict[str, RepoIndex] = {}
    rows: list[dict[str, Any]] = []
    for e in entries:
        flags = audit_entry(session, e, repo_cache=repo_cache)
        tokens = estimate_tokens(f"- {e.content.strip()}\n")
        bucket = e.category if e.category in ("global", "role") else "project"
        dups = group_of.get(e.id or 0, [])
        amends = [] if dups else related_approved(session, e)
        action, reason = _suggest(flags, dups, amends, tokens, left.get(bucket, 0), status)
        rows.append(
            {
                "id": e.id,
                "category": e.category,
                "scope": e.scope,
                "project_slug": e.project_slug,
                "role": e.role,
                "source": e.source,
                "bucket": bucket,
                "tokens": tokens,
                "flags": flags,
                "duplicates_of": dups,
                "amend_candidates": amends,
                "suggested_action": action,
                "reason": reason,
                "excerpt": e.content[:160],
            }
        )
    rows.sort(key=lambda r: r["id"] or 0)
    return {
        "status": status,
        "project_slug": project_slug,
        "role": role,
        "budgets": budgets,
        "budget_enabled": memory_service.memory_budgets_enabled(session),
        "current_usage": current.used,
        "budget_left": left,
        "headroom": current.headroom(),
        "warnings": current.warnings,
        "excluded_now": current.excluded,
        "projection": budget_projection(session, project_slug),
        "totals": {
            "entries": len(rows),
            "tokens": sum(r["tokens"] for r in rows),
            "by_action": {
                a: sum(1 for r in rows if r["suggested_action"] == a) for a in ACTION_ORDER
            },
            "merge_groups": groups,
        },
        "rows": rows,
    }


def budget_projection(session: Session, project_slug: str | None = None) -> dict[str, Any]:
    """Per role: memory tokens now, and after approving the entire pending queue."""
    pend = memory_service.list_entries(session, status="pending", project_slug=project_slug)
    out: dict[str, Any] = {}
    for r in TRIAGE_ROLES:
        now = memory_service.select_for_prompt(session, r.value, project_slug)
        after = memory_service.select_for_prompt(session, r.value, project_slug, also_approved=pend)
        out[r.value] = {
            "sections_now": now.used,
            "sections_after": after.used,
            "total_now": now.total_tokens,
            "total_after": after.total_tokens,
            "headroom": now.headroom(),
            "warnings": now.warnings,
            "excluded_now": sorted(x["id"] for x in now.excluded),
            "excluded_after": sorted(x["id"] for x in after.excluded),
        }
    return out


def budget_report(session: Session, project_slug: str | None = None) -> dict[str, Any]:
    """Static cost of memory + the pending queue, independent of any triage row."""
    return {
        "budgets": memory_service.memory_budgets(session),
        "budget_enabled": memory_service.memory_budgets_enabled(session),
        "project_slug": project_slug,
        "roles": budget_projection(session, project_slug),
        "pending_count": memory_service.pending_count(session),
        "pending_tokens": sum(
            estimate_tokens(f"- {e.content.strip()}\n")
            for e in memory_service.list_entries(session, status="pending")
        ),
    }
