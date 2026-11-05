"""Mechanical checks around a review: absent evidence, and whether the supplied diff is whole.

Two independent jobs, both required by T-11:

  - **absence claims** (DoD 2/3): when the reviewer says a file, test name or line is missing,
    check it against the task worktree before the verdict can cost another round. A refuted
    claim is recorded and cannot drive a requeue.
  - **diff completeness** (DoD 7): compare the changed-file list the harness computes from git
    with the patch that was actually shipped to the reviewer, and surface files a repository
    attribute made invisible (`-diff`, `export-ignore`). A Worker must not be able to opt out of
    review by editing `.gitattributes`.

One distinction is load-bearing (T-11 round-2 reviewer finding 2): a claim that something is
missing from the **repository** is refuted by the file existing, but a claim that something was
not **supplied to the reviewer** is not — existence does not refute a visibility claim. Those
claims are checked against the archived truncation manifest and the supplied patch instead, and
an unknowable one is marked `unchecked`, never `refuted`.

T-22 closes the mirror-image hole: **a claim is resolved to a repository path before it is
judged.** Measured on reviews.28 (run 38, T-19), 15 of 18 mechanical verdicts were `confirmed`
against BARE FILENAMES (`capture.sh`, `t19_tests.txt`, `full_suite.txt`, ...) while the real
paths were tracked at `verify-evidence/T-19/<name>`. The checker looked for the literal name at
the worktree root, found nothing, and returned the strongest verdict available — a confident
false absence. So every path claim now goes through `resolve_claim_path()` against a
`PathUniverse` built from the five sources that could name a path (supplied patch, diffstat,
truncation manifest, `git ls-tree` at the reviewed commit, worktree filesystem):

  - exactly one match  -> the claim is judged at that resolved path, and the row records how it
    was resolved;
  - several matches    -> `unchecked` / `unresolvable-claim`, listing EVERY candidate; one is
    never picked;
  - no match and the claim gave only a bare name -> `unchecked` / `unresolvable-claim` with the
    reason `no path could be resolved from the claim`;
  - no match and the claim itself named a directory-qualified path -> `confirmed`, because that
    path is now shown absent from all five sources, and the row names the sources searched.

`confirmed` therefore requires a located path; a failed lookup can only ever produce
`unchecked`. The three states a reviewer acts on are labelled separately (DoD 3):
`omitted-from-review-input` (present in the repository, withheld by the diff budget — T-16's
problem, not the Worker's), `absent-at-reviewed-commit` (a real evidence gap) and
`unresolvable-claim` (the checker cannot judge). The frozen `confirmed | refuted | unchecked`
provenance vocabulary in `docs/frozen-contracts.md` is unchanged; the state labels ride in the
existing notes/detail fields.

Everything here is offline: local git commands and local files only.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.services.prompt_builder import split_diff_files
from orchestrator.utils.git import run_git

# A finding is only an absence claim when it uses absence language; otherwise it is a normal
# review finding and no mechanical check applies.
_ABSENCE = re.compile(
    r"(?i)\b(missing|absent|not\s+present|does\s+not\s+exist|no\s+such|not\s+supplied|"
    r"not\s+included|not\s+found|omitted|truncated|not\s+visible|not\s+archived|unavailable|"
    r"isn'?t\s+present|never\s+provided|not\s+shown|cut\s+off|dropped)\b"
)
# Language that makes the claim about what the REVIEWER RECEIVED rather than about the repo.
_VISIBILITY = re.compile(
    r"(?i)\b(supplied|provided|visible|shown|seen\s+by\s+the\s+reviewer|truncated|cut\s+off|"
    r"omitted|dropped|not\s+in\s+the\s+(?:diff|patch|payload|prompt|handoff)|in\s+the\s+supplied\s+"
    r"(?:diff|patch|payload)|not\s+included|not\s+supplied|not\s+visible|not\s+shown|not\s+archived)\b"
)
_FILE = re.compile(
    r"(?<![\w./-])((?:\.{0,2}/)?(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|md|json|txt|ts|tsx|js|jsx|html|css|toml|ini|yaml|yml|sh|log|diff|patch|cfg|env))"
    r"(?![\w/])"
)
_TEST = re.compile(r"\b(test_[A-Za-z0-9_]+)\b")
_LINE = re.compile(r":(?P<line>\d{1,7})\b")

# --- the five sources a path claim is resolved against (T-22 DoD 1) -----------------
SOURCE_PATCH = "supplied patch"
SOURCE_DIFFSTAT = "diffstat"
SOURCE_MANIFEST = "truncation manifest"
SOURCE_LS_TREE = "git ls-tree HEAD"
SOURCE_WORKTREE = "worktree filesystem"
# Canonical order, so "sources searched" reads the same in every row.
ALL_SOURCES = (
    SOURCE_PATCH,
    SOURCE_DIFFSTAT,
    SOURCE_MANIFEST,
    SOURCE_LS_TREE,
    SOURCE_WORKTREE,
)
# Presence in the repository means one of these two named it; patch/diffstat/manifest name what
# the reviewer received, not what the branch contains.
_REPOSITORY_SOURCES = (SOURCE_LS_TREE, SOURCE_WORKTREE)

# Directories never walked for a basename lookup: dependency and build trees are not evidence,
# and scanning them would be slow and full of collisions.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
}

# The frozen provenance vocabulary (docs/frozen-contracts.md: confirmed | refuted | unchecked).
STATUS_CONFIRMED = "confirmed"
STATUS_REFUTED = "refuted"
STATUS_UNCHECKED = "unchecked"

# --- the three states a reviewer actually needs (T-22 DoD 3) ------------------------
# `confirmed` alone does not say which of them happened, which is how run 38 read as 15 evidence
# gaps. Each state has its own wording in the detail, and its own count in the tally.
STATE_ABSENT = "absent-at-reviewed-commit"
STATE_OMITTED = "omitted-from-review-input"
STATE_UNRESOLVED = "unresolvable-claim"
STATE_UNDECIDED = "undecided"
STATE_PRESENT = "present-in-repository"
STATE_SUPPLIED = "supplied-in-full"
ALL_STATES = (
    STATE_ABSENT,
    STATE_OMITTED,
    STATE_UNRESOLVED,
    STATE_UNDECIDED,
    STATE_PRESENT,
    STATE_SUPPLIED,
)

# --- how a verdict was reached, so a tally can be audited (T-22 DoD 4) --------------
BASIS_RESOLUTION = "path-resolution"
BASIS_EXPLICIT = "explicit-named-path"
BASIS_MANIFEST = "manifest-record"
BASIS_FAILED_LOOKUP = "failed-lookup"
BASIS_NOT_A_PATH = "not-a-path-claim"


def normalize_claim_path(raw: str) -> str:
    """The repository-relative form of a claimed path, without `./` noise."""
    text = (raw or "").strip().strip("\"'`").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    while text.startswith("../"):
        text = text[3:]
    return text.lstrip("/")


def claim_is_explicit(normalized: str) -> bool:
    """True when the claim itself names a location, not just a basename.

    `verify-evidence/T-19/capture.sh` says where the file would be, so its absence can be shown.
    `capture.sh` does not: it is a name that has to be looked up, and a lookup that finds nothing
    proves only that the lookup found nothing (T-22 DoD 2).
    """
    return "/" in normalized


@dataclass
class PathUniverse:
    """Every path a reviewer claim could refer to, tagged with the source that names it."""

    head_sha: str = ""
    entries: dict[str, set[str]] = field(default_factory=dict)
    searched: list[str] = field(default_factory=list)

    def add(self, path: str, source: str) -> None:
        path = normalize_claim_path(path)
        if not path:
            return
        self.entries.setdefault(path, set()).add(source)
        if source not in self.searched:
            self.searched.append(source)

    def has(self, path: str) -> bool:
        return normalize_claim_path(path) in self.entries

    def names(self, path: str) -> tuple[str, ...]:
        sources = self.entries.get(normalize_claim_path(path), set())
        return tuple(s for s in ALL_SOURCES if s in sources)

    def in_repository(self, path: str) -> bool:
        return bool(set(self.names(path)) & set(_REPOSITORY_SOURCES))

    def by_basename(self, name: str) -> list[str]:
        target = normalize_claim_path(name)
        return sorted(p for p in self.entries if p.rsplit("/", 1)[-1] == target)

    def source_list(self) -> str:
        return ", ".join(s for s in ALL_SOURCES if s in self.searched) or "no source available"

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in ALL_SOURCES}
        for sources in self.entries.values():
            for s in sources:
                out[s] = out.get(s, 0) + 1
        return out


def _tracked_paths(cwd: Path) -> tuple[list[str], str]:
    """`git ls-tree -r HEAD` at the reviewed commit: what the branch actually contains."""
    res = run_git(["ls-tree", "-r", "--name-only", "-z", "HEAD"], cwd, check=False)
    if res.code != 0:
        return [], ""
    sha = run_git(["rev-parse", "HEAD"], cwd, check=False)
    return [p for p in res.stdout.split("\0") if p.strip()], sha.stdout.strip()


def _filesystem_paths(cwd: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            try:
                out.append(str(full.relative_to(cwd)))
            except ValueError:
                continue
    return out


def build_universe(cwd: Path | None, supplied: SuppliedEvidence | None) -> PathUniverse:
    """Collect the paths the reviewer was supplied, the diffstat, the manifest and git.

    Every source is searched even when an earlier one already matches: a verdict has to be able
    to say which sources were checked, and `confirmed` absence is only honest against the whole
    set (T-22 DoD 1/2).
    """
    universe = PathUniverse()
    if supplied is not None:
        for path in supplied.diff_supplied:
            universe.add(path, SOURCE_PATCH)
        for path in supplied.changed_paths:
            universe.add(path, SOURCE_DIFFSTAT)
        for record in supplied.truncations:
            item = str(record.get("item", ""))
            if item.startswith("diff:"):
                universe.add(item[5:], SOURCE_MANIFEST)
    if cwd is not None and cwd.is_dir():
        tracked, head = _tracked_paths(cwd)
        universe.head_sha = head
        for path in tracked:
            universe.add(path, SOURCE_LS_TREE)
        for path in _filesystem_paths(cwd):
            universe.add(path, SOURCE_WORKTREE)
    return universe


@dataclass(frozen=True)
class PathResolution:
    """What a claimed path resolved to, and why — the evidence a verdict rests on."""

    claim: str
    tried: str
    path: str  # "" when the claim could not be resolved to one path
    basis: str
    reason: str
    found_in: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return bool(self.path)


def resolve_claim_path(raw: str, universe: PathUniverse) -> PathResolution:
    """Resolve one claimed path against every source, never guessing between candidates."""
    tried = normalize_claim_path(raw)
    if not tried:
        return PathResolution(raw, tried, "", BASIS_NOT_A_PATH, "the claim named no path")
    if universe.has(tried):
        return PathResolution(
            raw,
            tried,
            tried,
            BASIS_RESOLUTION,
            "the claim's own path is listed by the sources searched",
            found_in=universe.names(tried),
        )
    if not claim_is_explicit(tried):
        matches = universe.by_basename(tried)
        if len(matches) == 1:
            only = matches[0]
            return PathResolution(
                raw,
                tried,
                only,
                BASIS_RESOLUTION,
                f"the bare name matched exactly one path among the sources searched: {only}",
                found_in=universe.names(only),
            )
        if len(matches) > 1:
            return PathResolution(
                raw,
                tried,
                "",
                BASIS_FAILED_LOOKUP,
                f"{len(matches)} paths share this bare name; none was picked",
                candidates=tuple(matches),
            )
        return PathResolution(
            raw,
            tried,
            "",
            BASIS_FAILED_LOOKUP,
            "no path could be resolved from the claim: this bare name is listed by none of the "
            "sources searched",
        )
    # A directory-qualified path that nothing lists: the claim names its own location, so the
    # lookup did not fail — the path is genuinely absent, and every source says so.
    return PathResolution(
        raw,
        tried,
        tried,
        BASIS_EXPLICIT,
        "the claim itself names this path and no source lists it anywhere",
    )


EVIDENCE_OR_SOURCE_PREFIXES = (
    "src/",
    "tests/",
    "scripts/",
    "verify-evidence/",
    "evidence/",
)


@dataclass
class SuppliedEvidence:
    """What actually reached the reviewer, derived from the archived prompt/truncation records.

    `diff_cut` maps a path to `(bytes_included, bytes_available)` for every file the per-file
    diff budget cut; `diff_supplied` is every path that has a section in the supplied patch;
    `changed_paths` is the git diffstat; `section_cut` says the whole dynamic section was shaved
    by the aggregate context budget, which makes per-file statements unknowable.
    """

    diff_supplied: set[str] = field(default_factory=set)
    diff_cut: dict[str, tuple[int, int]] = field(default_factory=dict)
    changed_paths: set[str] = field(default_factory=set)
    section_cut: bool = False
    truncations: list[dict[str, Any]] = field(default_factory=list)

    def cut_table(self) -> str:
        if not self.diff_cut:
            return "none"
        return ", ".join(
            f"{p} ({inc}/{avail} bytes)" for p, (inc, avail) in sorted(self.diff_cut.items())
        )


@dataclass
class AbsenceCheck:
    finding_index: int
    claim: str
    status: str  # confirmed | refuted | unchecked (frozen vocabulary)
    detail: str
    source: str = "finding"  # finding | missing_artifact
    # T-22: which of the three reviewer-facing states the verdict belongs to, and the resolution
    # evidence it rests on. Additive to the frozen status vocabulary: `state` is rendered into the
    # existing notes/detail fields, review.json keys do not change.
    state: str = ""
    resolved_path: str = ""
    basis: str = ""
    tried: str = ""
    found_in: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding_index": self.finding_index,
            "claim": self.claim,
            "status": self.status,
            "detail": self.detail,
            "source": self.source,
            "state": self.state,
            "resolved_path": self.resolved_path,
            "basis": self.basis,
            "tried": self.tried,
            "found_in": list(self.found_in),
            "candidates": list(self.candidates),
            "sources": list(self.sources),
        }

    def sources_note(self) -> str:
        return ", ".join(self.sources) if self.sources else "no source was available"

    def found_note(self) -> str:
        """Where the claim was found, or the explicit statement that nothing named it."""
        if self.candidates:
            return "candidates: " + ", ".join(self.candidates)
        if self.found_in:
            return f"{self.resolved_path} found in: " + ", ".join(self.found_in)
        return f"{self.resolved_path or self.tried} listed by none of these sources"

    def claim_note(self) -> str:
        """One-line provenance suffix: how the verdict was reached, for review.md notes."""
        bits = [f"source: {self.source}"]
        if self.state:
            bits.append(f"state: {self.state}")
        if self.basis:
            bits.append(f"basis: {self.basis}")
        bits.append(f"tried: {self.tried or self.claim}")
        bits.append(f"resolved: {self.resolved_path or 'NO PATH'}")
        bits.append(f"searched: {self.sources_note()}")
        return "; ".join(bits)


@dataclass
class DiffIssue:
    path: str
    kind: str  # attribute | binary | no_hunk
    detail: str
    blocks: bool
    pre_existing: bool = False
    attribute: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "detail": self.detail,
            "blocks": self.blocks,
            "pre_existing": self.pre_existing,
            "attribute": self.attribute,
        }


def _is_evidence_or_source(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in EVIDENCE_OR_SOURCE_PREFIXES)


def _line_count(path: Path) -> int:
    try:
        return sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
    except OSError:
        return 0


def _find_test_name(cwd: Path, name: str) -> str | None:
    """Cheap local search for a test function by name. Never a shell command."""
    roots = [cwd / "tests", cwd / "src"]
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            try:
                if name in path.read_text(encoding="utf-8", errors="replace"):
                    return str(path.relative_to(cwd))
            except OSError:
                continue
    return None


def changed_paths(cwd: Path | None, base_branch: str | None) -> set[str]:
    """The same changed-file set the harness diffstat check uses (T-11 DoD 7)."""
    if cwd is None or not cwd.is_dir() or not base_branch:
        return set()
    return {path for _a, _d, path in _numstat(cwd, base_branch)}


def supplied_evidence(
    patch: str,
    truncations: list[dict[str, Any]] | None,
    changed: Iterable[str] = (),
) -> SuppliedEvidence:
    """Derive what the reviewer was given from the recorder that performed the cuts.

    A file is "cut" only when the prompt builder recorded a truncation for it, which is the same
    code that produced the text, so the manifest and this check cannot drift apart.
    """
    cut: dict[str, tuple[int, int]] = {}
    section_cut = False
    for record in truncations or []:
        item = str(record.get("item", ""))
        if item.startswith("diff:"):
            cut[item[5:]] = (
                int(record.get("bytes_included") or 0),
                int(record.get("bytes_available") or 0),
            )
        elif record.get("section") == "context_budget" and item == "dynamic_state":
            section_cut = True
    return SuppliedEvidence(
        diff_supplied={path for path, _chunk in split_diff_files(patch or "")},
        diff_cut=cut,
        changed_paths=set(changed),
        section_cut=section_cut,
        truncations=list(truncations or []),
    )


def _unresolved_claim(
    res: PathResolution, universe: PathUniverse, index: int, source: str
) -> AbsenceCheck:
    """A claim that never became a path: unchecked, with the failed lookup documented."""
    return AbsenceCheck(
        finding_index=index,
        claim=res.claim,
        status=STATUS_UNCHECKED,
        detail=f"{res.reason} (sources searched: {universe.source_list()})",
        source=source,
        state=STATE_UNRESOLVED,
        resolved_path="",
        basis=res.basis,
        tried=res.tried,
        candidates=res.candidates,
        sources=tuple(s for s in ALL_SOURCES if s in universe.searched),
    )


def _judged(
    res: PathResolution,
    universe: PathUniverse,
    index: int,
    source: str,
    *,
    status: str,
    state: str,
    detail: str,
    claim: str = "",
    basis: str = "",
) -> AbsenceCheck:
    """Build a verdict row with its resolution evidence attached (T-22 DoD 4)."""
    path = res.path
    return AbsenceCheck(
        finding_index=index,
        claim=claim or res.claim,
        status=status,
        detail=detail,
        source=source,
        state=state,
        resolved_path=path,
        basis=basis or res.basis,
        tried=res.tried,
        found_in=universe.names(path) if path else res.found_in,
        candidates=res.candidates,
        sources=tuple(s for s in ALL_SOURCES if s in universe.searched),
    )


def _repo_present(cwd: Path | None, universe: PathUniverse, path: str) -> bool:
    """Is this path in the repository at the reviewed commit (or on the worktree filesystem)?"""
    if not path:
        return False
    if cwd is not None and (cwd / normalize_claim_path(path)).is_file():
        return True
    return universe.in_repository(path)


def _check_visibility(
    cwd: Path | None,
    res: PathResolution,
    universe: PathUniverse,
    index: int,
    source: str,
    supplied: SuppliedEvidence | None,
) -> AbsenceCheck:
    """Check a "was not supplied to the reviewer" claim against what was actually supplied.

    Existence in the worktree is deliberately NOT used to refute one of these: a file can exist on
    disk and still have had every hunk withheld from the reviewer (T-11 round-2 reviewer finding
    2). What existence CAN do is name the state (T-22 DoD 3): a resolved path that is in the
    repository but did not reach the reviewer is `omitted-from-review-input` — a diff-budget
    failure owned by T-16, not a Worker evidence gap — and only a path the repository itself does
    not contain is `absent-at-reviewed-commit`. A name that resolves to nothing, or to several
    paths, is `unchecked`: that is exactly the shape of run 38's 15 false confirmations.
    """
    if not res.resolved:
        return _unresolved_claim(res, universe, index, source)
    path = res.path
    present = _repo_present(cwd, universe, path)
    if supplied is None:
        if present:
            return _judged(
                res,
                universe,
                index,
                source,
                status=STATUS_UNCHECKED,
                state=STATE_UNDECIDED,
                detail=(
                    "the path exists in the worktree, but presence in the supplied artifacts "
                    "cannot be decided without the archived truncation manifest"
                ),
            )
        return _judged(
            res,
            universe,
            index,
            source,
            status=STATUS_CONFIRMED,
            state=STATE_ABSENT,
            detail=(
                "absent-at-reviewed-commit: the resolved path is not in the task worktree and no "
                f"source lists it (no truncation manifest was available; searched: "
                f"{universe.source_list()})"
            ),
        )
    if supplied.section_cut:
        return _judged(
            res,
            universe,
            index,
            source,
            status=STATUS_UNCHECKED,
            state=STATE_UNDECIDED,
            detail=(
                "the whole dynamic_state section was shaved by the aggregate context budget, so "
                "per-file visibility cannot be decided from the archived manifest"
            ),
        )
    if path in supplied.diff_cut:
        included, available = supplied.diff_cut[path]
        cut_text = (
            f"this file was omitted entirely (0 of {available} bytes reached the reviewer)"
            if included == 0
            else f"only {included} of {available} bytes reached the reviewer"
        )
        return _judged(
            res,
            universe,
            index,
            source,
            status=STATUS_CONFIRMED,
            state=STATE_OMITTED if present else STATE_ABSENT,
            detail=(
                f"{STATE_OMITTED if present else STATE_ABSENT}: the resolved path is "
                f"{'in the repository' if present else 'not in the repository at this commit'} and "
                f"the archived truncation manifest says {cut_text} — "
                + (
                    "the evidence exists, the review input did not carry it (diff budget, owned by "
                    "T-16), so this is not a Worker evidence gap"
                    if present
                    else "there was nothing in the repository to supply"
                )
            ),
        )
    if path in supplied.diff_supplied:
        return _judged(
            res,
            universe,
            index,
            source,
            status=STATUS_REFUTED,
            state=STATE_SUPPLIED,
            detail=(
                "supplied to the reviewer in full: the patch has a section for it and the "
                "archived manifest records no truncation for it"
            ),
        )
    if path in supplied.changed_paths:
        return _judged(
            res,
            universe,
            index,
            source,
            status=STATUS_CONFIRMED,
            state=STATE_OMITTED if present else STATE_ABSENT,
            detail=(
                f"{STATE_OMITTED if present else STATE_ABSENT}: changed by this task and "
                + (
                    "present in the repository, but the supplied patch has no section for it — a "
                    "review-input gap, see the diff-completeness findings"
                    if present
                    else "listed by no source, so nothing was there to supply"
                )
            ),
        )
    if present:
        return _judged(
            res,
            universe,
            index,
            source,
            status=STATUS_UNCHECKED,
            state=STATE_UNDECIDED,
            detail=(
                "the path exists in the worktree but is not part of this task's diff, so presence "
                "in the supplied artifacts cannot be decided mechanically"
            ),
        )
    return _judged(
        res,
        universe,
        index,
        source,
        status=STATUS_CONFIRMED,
        state=STATE_ABSENT,
        detail=(
            "absent-at-reviewed-commit: the resolved path is listed by none of the sources "
            f"searched ({universe.source_list()})"
            + (f" at commit {universe.head_sha}" if universe.head_sha else "")
        ),
    )


def _manifest_topic_checks(
    text: str,
    supplied: SuppliedEvidence,
    index: int,
    source: str,
    universe: PathUniverse,
) -> list[AbsenceCheck]:
    """Match a vague truncation claim ("the handoff block was cut") to the manifest.

    The archived manifest is the evidence here, not a path lookup, so the basis is recorded as
    `manifest-record` and a reader can see the verdict rests on a recorded cut.
    """
    low = (text or "").lower()
    out: list[AbsenceCheck] = []
    for record in supplied.truncations:
        item = str(record.get("item", ""))
        is_path = item.startswith("diff:")
        topic = item[5:] if is_path else item
        if not topic or topic.lower() not in low:
            continue
        out.append(
            AbsenceCheck(
                finding_index=index,
                claim=topic,
                status=STATUS_CONFIRMED,
                detail=(
                    "the archived manifest records this cut: "
                    f"{record.get('bytes_included', 0)} of "
                    f"{record.get('bytes_available', 0)} bytes included "
                    f"(budget: {record.get('budget', '')})"
                ),
                source=source,
                state=STATE_OMITTED,
                resolved_path=topic if is_path else "",
                basis=BASIS_MANIFEST,
                tried=topic,
                found_in=universe.names(topic) if is_path else (),
                sources=tuple(s for s in ALL_SOURCES if s in universe.searched),
            )
        )
    return out


def _check_existence_claim(
    root: Path,
    raw: str,
    res: PathResolution,
    universe: PathUniverse,
    index: int,
    source: str,
    line_window: str,
) -> AbsenceCheck:
    """Judge "X is not in the repository" only after X has been resolved to a path.

    Run 38 confirmed `capture.sh` because `<worktree>/capture.sh` does not exist, while
    `verify-evidence/T-19/capture.sh` was tracked the whole time. Resolution comes first, and a
    claim that cannot be resolved is `unchecked`, never `confirmed` (T-22 DoD 1/2).
    """
    if not res.resolved:
        return _unresolved_claim(res, universe, index, source)
    rel = res.path
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return _judged(
            res,
            universe,
            index,
            source,
            status=STATUS_UNCHECKED,
            state=STATE_UNDECIDED,
            claim=raw,
            detail="path is outside the task worktree; not checked",
        )
    if target.is_file():
        line_match = _LINE.search(line_window)
        if line_match:
            wanted = int(line_match.group("line"))
            count = _line_count(target)
            if count >= wanted:
                return _judged(
                    res,
                    universe,
                    index,
                    source,
                    status=STATUS_REFUTED,
                    state=STATE_PRESENT,
                    claim=f"{raw}:{wanted}",
                    detail=(
                        f"file exists in the task worktree with {count} lines, so line {wanted} "
                        "is present"
                    ),
                )
            return _judged(
                res,
                universe,
                index,
                source,
                status=STATUS_CONFIRMED,
                state=STATE_ABSENT,
                claim=f"{raw}:{wanted}",
                detail=(
                    f"absent-at-reviewed-commit: the file is present at {rel} but has only "
                    f"{count} lines, so line {wanted} does not exist"
                ),
            )
        return _judged(
            res,
            universe,
            index,
            source,
            status=STATUS_REFUTED,
            state=STATE_PRESENT,
            claim=raw,
            detail=f"file exists in the task worktree at {rel}",
        )
    if target.exists():
        return _judged(
            res,
            universe,
            index,
            source,
            status=STATUS_REFUTED,
            state=STATE_PRESENT,
            claim=raw,
            detail=f"path exists in the task worktree at {rel} (not a regular file)",
        )
    if universe.has(rel):
        # Named by a source (tracked at HEAD, or in the patch/diffstat) but gone from the disk:
        # which of the two the claim meant is not decidable, so it is not confirmed either way.
        return _judged(
            res,
            universe,
            index,
            source,
            status=STATUS_UNCHECKED,
            state=STATE_UNDECIDED,
            claim=raw,
            detail=(
                f"{rel} is not on the worktree filesystem but is named by "
                f"{', '.join(universe.names(rel))}; the harness cannot tell which the claim meant"
            ),
        )
    return _judged(
        res,
        universe,
        index,
        source,
        status=STATUS_CONFIRMED,
        state=STATE_ABSENT,
        claim=raw,
        detail=(
            "absent-at-reviewed-commit: the path named by the claim is listed by none of the "
            f"sources searched ({universe.source_list()})"
            + (f" at commit {universe.head_sha}" if universe.head_sha else "")
        ),
    )


def _check_claim_text(
    cwd: Path | None,
    text: str,
    *,
    source: str = "finding",
    index: int = -1,
    supplied: SuppliedEvidence | None = None,
    universe: PathUniverse | None = None,
) -> list[AbsenceCheck]:
    """Extract concrete file/test/line claims from one absence finding and check each one.

    Every path claim is resolved to a repository path BEFORE it is judged (T-22 DoD 1), then the
    two families are checked differently:
      - "X is missing" (about the repository) — the file's existence refutes it;
      - "X was not supplied / was truncated" (about the review input) — existence proves nothing,
        so it is checked against the supplied patch and the truncation manifest, and marked
        `unchecked` when neither can decide it.
    """
    if not _ABSENCE.search(text or ""):
        return []
    visibility = bool(_VISIBILITY.search(text or "")) or source == "missing_artifact"
    if not visibility and (cwd is None or not cwd.is_dir()):
        return [
            AbsenceCheck(
                finding_index=index,
                claim=text.strip(),
                status=STATUS_UNCHECKED,
                detail="no task worktree was available for a mechanical existence check",
                source=source,
                state=STATE_UNDECIDED,
                basis=BASIS_NOT_A_PATH,
            )
        ]
    claims: list[AbsenceCheck] = []
    # Only reached for existence claims, where the worktree was required above.
    root = cwd if cwd is not None else Path(".")
    if universe is None:
        universe = build_universe(cwd, supplied)

    for match in _FILE.finditer(text):
        raw = match.group(1).strip().rstrip(".,;:)}]")
        res = resolve_claim_path(raw, universe)
        if visibility:
            claims.append(_check_visibility(cwd, res, universe, index, source, supplied))
            continue
        claims.append(
            _check_existence_claim(
                root, raw, res, universe, index, source, text[match.end() : match.end() + 12]
            )
        )

    for match in _TEST.finditer(text):
        name = match.group(1)
        found = _find_test_name(cwd, name) if cwd is not None else None
        if found and visibility and supplied is not None:
            claims.append(
                _check_visibility(
                    cwd, resolve_claim_path(found, universe), universe, index, source, supplied
                )
            )
            continue
        if found:
            claims.append(
                AbsenceCheck(
                    finding_index=index,
                    claim=name,
                    status=STATUS_REFUTED,
                    detail=f"test function name found in {found}",
                    source=source,
                    state=STATE_PRESENT,
                    resolved_path=found,
                    basis=BASIS_RESOLUTION,
                    tried=name,
                    found_in=universe.names(found),
                    sources=tuple(s for s in ALL_SOURCES if s in universe.searched),
                )
            )
        else:
            claims.append(
                AbsenceCheck(
                    finding_index=index,
                    claim=name,
                    status=STATUS_UNCHECKED,
                    detail="test name was not found by the local source scan (may live elsewhere)",
                    source=source,
                    state=STATE_UNRESOLVED,
                    basis=BASIS_FAILED_LOOKUP,
                    tried=name,
                    sources=tuple(s for s in ALL_SOURCES if s in universe.searched),
                )
            )

    if visibility and supplied is not None:
        claims.extend(_manifest_topic_checks(text, supplied, index, source, universe))
    if not claims:
        claims.append(
            AbsenceCheck(
                finding_index=index,
                claim=text.strip(),
                status=STATUS_UNCHECKED,
                detail="no concrete file, test name or line could be extracted from the claim",
                source=source,
                state=STATE_UNRESOLVED,
                basis=BASIS_NOT_A_PATH,
            )
        )
    # The same path is often named in both `file_or_area` and `issue`; one claim, one row.
    seen: set[tuple[str, str]] = set()
    unique: list[AbsenceCheck] = []
    for check in claims:
        key = (check.claim, check.status)
        if key in seen:
            continue
        seen.add(key)
        unique.append(check)
    return unique


def _aggregate(checks: list[AbsenceCheck]) -> str:
    if not checks:
        return "unchecked"
    statuses = {c.status for c in checks}
    if "confirmed" in statuses:
        return "confirmed"
    if statuses == {"refuted"}:
        return "refuted"
    return "unchecked"


def check_findings(
    findings: list[Any],
    cwd: Path | None,
    supplied: SuppliedEvidence | None = None,
    universe: PathUniverse | None = None,
) -> list[AbsenceCheck]:
    """Check every finding that asserts absence. Non-absence findings are left alone.

    `universe` is the set of paths a claim can be resolved against; it is built once per call (and
    shared with `check_missing_artifacts` by the review service) so every verdict in a round is
    judged against the same repository view.
    """
    out: list[AbsenceCheck] = []
    if universe is None:
        universe = build_universe(cwd, supplied)
    for i, finding in enumerate(findings):
        if isinstance(finding, dict):
            text = " ".join(str(finding.get(k, "")) for k in ("file_or_area", "issue", "violates"))
        else:
            text = " ".join(
                str(getattr(finding, k, "")) for k in ("file_or_area", "issue", "violates")
            )
        out.extend(_check_claim_text(cwd, text, index=i, supplied=supplied, universe=universe))
    return out


def check_missing_artifacts(
    items: list[str],
    cwd: Path | None,
    supplied: SuppliedEvidence | None = None,
    universe: PathUniverse | None = None,
) -> list[AbsenceCheck]:
    """Reviewer-supplied `missing_artifacts` entries are claims about the supplied artifacts.

    They are always checked as visibility claims: the item names something the reviewer says it
    did not receive, so a file existing in the worktree does not refute it (T-11 round-2
    reviewer finding 2). Run 38's worst batch arrived here as one brace-expanded item —
    `verify-evidence/T-19/{README.md,capture.sh,...}` — which the extractor turned into BARE
    names, so each name is resolved back to its tracked path before it can be judged (T-22 DoD 1).
    """
    out: list[AbsenceCheck] = []
    if universe is None:
        universe = build_universe(cwd, supplied)
    for i, item in enumerate(items):
        out.extend(
            _check_claim_text(
                cwd,
                f"{item} is missing",
                source="missing_artifact",
                index=i,
                supplied=supplied,
                universe=universe,
            )
        )
    return out


def finding_status(checks: list[AbsenceCheck], index: int) -> str:
    return _aggregate([c for c in checks if c.finding_index == index and c.source == "finding"])


def render_absence_checks(checks: list[AbsenceCheck]) -> str:
    if not checks:
        return "No reviewer absence claim needed a mechanical check."
    lines = [
        "| claim | source | status | checked against / evidence | state | how it was resolved | "
        "where it was found |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in checks:
        claim = c.claim.replace("|", "\\|")
        detail = c.detail.replace("|", "\\|")
        resolution = f"{c.basis or 'not a path claim'} (tried: {c.tried or c.claim})".replace(
            "|", "\\|"
        )
        lines.append(
            f"| {claim} | {c.source} | **{c.status}** | {detail} | {c.state or '-'} | "
            f"{resolution} | {c.found_note().replace('|', chr(92) + '|')} |"
        )
    lines.append("")
    lines.append(
        "Status vocabulary (frozen): `refuted` = the repository or the archived input disproves "
        "the claim. `confirmed` = the claim holds **and the claim resolved to a path**: a name "
        "that no source lists, or an explicit path absent from every source. `unchecked` = no "
        "mechanical decision was possible, so the finding keeps its normal weight "
        "(fail closed).\n\n"
        "State labels (T-22) say WHICH thing the verdict means, because a bare `confirmed` used to "
        "cover both a review-input gap and a Worker evidence gap: "
        f"`{STATE_OMITTED}` = the file is in the repository but did not reach the reviewer — a "
        "diff-budget failure owned by T-16, not a reason to send work back to the Worker; "
        f"`{STATE_ABSENT}` = not in the repository at the reviewed commit — a real evidence gap; "
        f"`{STATE_UNRESOLVED}` = the checker cannot judge the claim (a bare name matching nothing, "
        "or matching several paths — every candidate is listed and none is picked); "
        f"`{STATE_UNDECIDED}` = a located path whose visibility cannot be settled from the "
        f"archived manifest; `{STATE_PRESENT}`/`{STATE_SUPPLIED}` = refuted by the repository or "
        "by the supplied patch."
    )
    return "\n".join(lines)


def tally_verdicts(checks: list[AbsenceCheck]) -> str:
    """A self-auditing count: how many confident verdicts rest on a resolved path (T-22 DoD 4).

    The point of this line is the split inside `confirmed`. Run 38 printed "15 confirmed" and 15 of
    those came from a lookup that had nothing to match, so the number a human skimmed was exactly
    the number the machine could not support. A confirmed verdict reached from a failed lookup is
    impossible by construction here; if this ever prints a non-zero value, the resolver is broken.
    """
    by_status: dict[str, int] = {}
    by_state: dict[str, int] = {}
    by_basis: dict[str, int] = {}
    for c in checks:
        by_status[c.status] = by_status.get(c.status, 0) + 1
        if c.state:
            by_state[c.state] = by_state.get(c.state, 0) + 1
        if c.status == STATUS_CONFIRMED:
            key = c.basis or "unrecorded"
            by_basis[key] = by_basis.get(key, 0) + 1

    def n(value: str) -> int:
        return by_status.get(value, 0)

    def basis(value: str) -> int:
        return by_basis.get(value, 0)

    states = ", ".join(f"{count} {name}" for name, count in sorted(by_state.items())) or "none"
    return (
        f"{n(STATUS_CONFIRMED)} confirmed, {n(STATUS_REFUTED)} refuted, "
        f"{n(STATUS_UNCHECKED)} unchecked "
        f"[states: {states}; confirmed by path-resolution: {basis(BASIS_RESOLUTION)}, "
        f"by explicit-named-path: {basis(BASIS_EXPLICIT)}, "
        f"by manifest-record: {basis(BASIS_MANIFEST)}, "
        f"from a failed lookup: {basis(BASIS_FAILED_LOOKUP)} - a claim that resolves to no path is "
        f"reported as {STATE_UNRESOLVED}, never confirmed]"
    )


def render_universe(universe: PathUniverse) -> str:
    """The path set every verdict was resolved against, with the commit it describes."""
    counts = universe.counts()
    listed = ", ".join(
        f"{name} ({counts.get(name, 0)})" for name in ALL_SOURCES if name in universe.searched
    )
    return (
        f"- path claims were resolved against: {listed or 'no source was available'}\n"
        f"- distinct paths in the universe: {len(universe.entries)}"
        + (f" at commit {universe.head_sha}" if universe.head_sha else " (commit unknown)")
        + "\n- a bare name matching exactly one path is resolved to it; matching several is "
        "reported with every candidate and left `unchecked`; matching nothing is `unchecked` too"
    )


def render_supplied_summary(
    supplied: SuppliedEvidence, universe: PathUniverse | None = None
) -> str:
    """What the reviewer was actually given, stated next to the absence checks."""
    lines = [
        f"- changed files named by git: {len(supplied.changed_paths)}",
        f"- files with a section in the supplied patch: {len(supplied.diff_supplied)}",
        f"- files the truncation manifest records as cut: {supplied.cut_table()}",
        "- whole dynamic section shaved by the aggregate context budget: "
        + ("yes" if supplied.section_cut else "no"),
    ]
    if universe is not None:
        lines.append(render_universe(universe))
    return "\n".join(lines)


# ---------------------------------------------------------------- diff completeness (DoD 7)


def _numstat(cwd: Path, base_branch: str) -> list[tuple[str, str, str]]:
    """(adds, deletes, path) from the same comparison the archived diffstat describes."""
    res = run_git(
        ["diff", "--numstat", "--no-renames", f"{base_branch}...HEAD"], cwd, check=False
    )
    if res.code != 0:
        return []
    rows: list[tuple[str, str, str]] = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def _attributes(
    cwd: Path, paths: list[str], source: str | None = None
) -> dict[str, dict[str, str]]:
    if not paths:
        return {}
    args = ["check-attr", "-a"]
    if source:
        args += ["--source", source]
    args += ["--", *paths]
    res = run_git(args, cwd, check=False)
    out: dict[str, dict[str, str]] = {}
    for line in res.stdout.splitlines():
        parts = line.rsplit(": ", 2)
        if len(parts) != 3:
            continue
        path, attr, value = parts
        out.setdefault(path, {})[attr] = value
    return out


def _hidden_attributes(attrs: dict[str, str]) -> list[str]:
    out: list[str] = []
    if attrs.get("diff") == "unset":
        out.append("-diff")
    if attrs.get("export-ignore") == "set":
        out.append("export-ignore")
    return out


def diff_completeness(
    cwd: Path | None, base_branch: str | None, patch: str
) -> list[DiffIssue]:
    """Compare changed files, the supplied patch and repository attributes.

    Returns every issue found, including pre-existing ones. `blocks` is True only for an issue
    this task introduced: an attribute that was already set at the merge-base is reported as
    pre-existing and is not a reason to fail this run.
    """
    if cwd is None or not cwd.is_dir() or not base_branch:
        return []
    rows = _numstat(cwd, base_branch)
    if not rows:
        return []
    chunks = dict(split_diff_files(patch or ""))
    paths = [p for _a, _d, p in rows]
    current_attrs = _attributes(cwd, paths)
    merge_base = run_git(["merge-base", base_branch, "HEAD"], cwd, check=False).stdout.strip()
    base_attrs = _attributes(cwd, paths, source=merge_base) if merge_base else {}

    issues: list[DiffIssue] = []
    for adds, deletes, path in rows:
        before = _hidden_attributes(base_attrs.get(path, {}))
        after = _hidden_attributes(current_attrs.get(path, {}))
        pre_existing = bool(before) and set(after).issubset(set(before))
        for attr in after:
            issues.append(
                DiffIssue(
                    path=path,
                    kind="attribute",
                    detail=(
                        f"`{attr}` is set on {path}; git reports the file as "
                        f"{'binary' if attr == '-diff' else 'excluded'} and its hunks can be "
                        f"invisible to the reviewer"
                        + (
                            " (pre-existing at the merge-base, not introduced by this task)"
                            if pre_existing
                            else ""
                        )
                    ),
                    blocks=not pre_existing,
                    pre_existing=pre_existing,
                    attribute=attr,
                )
            )
        chunk = chunks.get(path)
        binary = adds == "-" or deletes == "-"
        metadata_only = bool(chunk) and any(
            line.startswith(
                (
                    "old mode ",
                    "new mode ",
                    "rename from ",
                    "rename to ",
                    "similarity index ",
                    "new file mode ",
                    "deleted file mode ",
                )
            )
            for line in chunk.splitlines()
        )
        has_hunk = bool(chunk) and any(line.startswith("@@") for line in chunk.splitlines())
        if binary:
            issues.append(
                DiffIssue(
                    path=path,
                    kind="binary",
                    detail=(
                        f"diffstat classifies {path} as binary (adds={adds}, deletes={deletes}), "
                        "so no hunk body reached the reviewer"
                    ),
                    blocks=_is_evidence_or_source(path) and not pre_existing,
                    pre_existing=pre_existing,
                    attribute=", ".join(after),
                )
            )
        elif chunk is None:
            issues.append(
                DiffIssue(
                    path=path,
                    kind="no_hunk",
                    detail=(
                        f"diffstat names {path} but the supplied patch has no file section for it"
                    ),
                    blocks=not pre_existing,
                    pre_existing=pre_existing,
                    attribute=", ".join(after),
                )
            )
        elif not has_hunk and not metadata_only and not (adds == deletes == "0"):
            issues.append(
                DiffIssue(
                    path=path,
                    kind="no_hunk",
                    detail=(
                        f"diffstat names {path} but its patch section has no hunk body "
                        f"(adds={adds}, deletes={deletes})"
                    ),
                    blocks=not pre_existing,
                    pre_existing=pre_existing,
                    attribute=", ".join(after),
                )
            )
    return issues


def render_diff_completeness(issues: list[DiffIssue]) -> str:
    if not issues:
        return (
            "diff completeness: every changed file named by git has a hunk in the supplied "
            "patch."
        )
    lines = [
        "diff completeness findings (computed from git and `git check-attr`, not asserted "
        "by a model):"
    ]
    for issue in issues:
        label = (
            "HARNESS FINDING"
            if issue.blocks
            else "informational (pre-existing or binary asset)"
        )
        lines.append(f"- {label} [{issue.kind}] {issue.path}: {issue.detail}")
    return "\n".join(lines)
