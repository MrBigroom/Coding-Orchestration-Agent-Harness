"""Committed evidence freshness and the one-command evidence bundle (T-15).

A committed evidence file is only trustworthy while the HEAD it was measured at is the commit
under review. Before T-15 nothing compared the two, so a round could harden its assertions and be
reviewed against an older copy of its own tests (T-4 round 5: 66 of 68 files carried the round-4
tip). Four rules, each a measured defect in an earlier round:

* The stamp is a legacy vocabulary, not one format: nine shapes exist, all are parsed, a short SHA
  is prefix-resolved against the commit under verification, and an unreadable `HEAD=` line is stale.
* A stamp is header metadata: only the first :data:`STAMP_WINDOW` lines are scanned and a bare
  `HEAD <sha>` counts only at a line start or inside a leading `#` comment, so captured output that
  *mentions* a stamp can neither stale a bundle nor be rewritten.
* The comparison target is the nearest ancestor of the commit under verification that is not
  **evidence-only** (touches nothing outside `verify-evidence/`) and not empty. An evidence-only
  commit changes no code, so a bundle delivered by one is honestly stamped at the code HEAD it was
  measured at; that is what lets a bundle report `pass` on the commit that ships it. Every commit
  that changes code — including a recovery commit that swept uncommitted source and an operator
  hand-fix — becomes the target and makes an older stamp stale. Recovery and hand-fix commits are
  identified by their git **trailers** (`%(trailers)`, never a substring of the body); they are
  *explained, not certified*: the reason names the commit and the trailer, and the bundle still
  has to be refreshed. Empty commits are walked past like evidence-only ones.
* Deletion is its own state: a file committed earlier on the branch and gone at the reviewed commit
  is a finding naming the paths and the deleting commit, never `not_applicable`.
* The status vocabulary is frozen and `stale` is the fifth value, approved as CCR-T15-01
  (`docs/frozen-contracts.md`, `docs/contract-change-request-T15-01-evidence-freshness-status.md`).
  A compared-and-out-of-date bundle reports `stale`, never `skipped`; `fail` stays reserved for the
  operator's opt-in strict mode, and the check never flips a verdict by itself.

The generator cannot lie by construction: it refuses a worktree with **any** uncommitted input —
tracked modifications *and* untracked source/test files, because `capture.sh` would measure code the
commit does not contain — requires the generator to be committed at `HEAD`, runs that committed
script and nothing else (no arbitrary command string), canonicalises **only the files that pass
rewrote**, and writes `manifest.json` with the pass mode (`fast` or `full`). A file the pass did not
rewrite keeps its old stamp and reads stale — there is no stamp-only mode, because re-stamping
content that was not regenerated is this module's whole point. A subset refresh selects a **named**
file the committed manifest declares as an output (`--only <name>`); an unknown name is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.utils.git import head_sha, run_git

EVIDENCE_ROOT = "verify-evidence"
CHECK_NAME = "committed evidence freshness"
CANONICAL_STAMP = "# HEAD={sha} mode={mode}"
MANIFEST_NAME = "manifest.json"
CAPTURE_NAME = "capture.sh"
SELF_CHECK_NAME = "freshness_selfcheck.txt"
AT_TIP_NAME = "freshness_at_tip.txt"
#: The hand-maintained index. Prose a human edits is not a measurement, so it is not hashed into
#: the manifest (an edited paragraph must not put a false sha256 in the bundle it describes).
INDEX_NAME = "README.md"
#: Bundle bookkeeping the driver writes after the capture: listed as a file, but never a reason for
#: a "rewrite" count or a false manifest hash.
NOT_MEASURED = (MANIFEST_NAME, AT_TIP_NAME, INDEX_NAME)
#: The check is a property of code-writing tasks; an `agent`/`review` task writes no code and has no
#: evidence bundle. Both call sites (supervisor verification and the Architect review) use this one
#: predicate so a finding can never appear without a matching verification row.
REVIEWED_KINDS = ("coding", "maintenance")
STAMP_WINDOW = 20  # stamps are header metadata; a transcript body may mention one harmlessly
MODE_FAST = "fast"
MODE_FULL = "full"
RECOVERY_TRAILER = "Orchestrator-Recovery"
HAND_FIX_TRAILER = "Operator-Hand-Fix"

#: Internal states, mapped onto the frozen check-record vocabulary by :func:`check_status`.
#: `stale` became a first-class value with CCR-T15-01 (operator approval, 2026-11-08).
STATE_PASS = "pass"  # noqa: S105 - a check state, not a credential
STATE_STALE = "stale"
STATE_NOT_APPLICABLE = "not_applicable"

_HEX = r"[0-9a-fA-F]{7,40}"
_NOT_HEX = r"(?![0-9a-fA-F])"
_STAMP_PATTERNS = (
    re.compile(rf"^\s*#?\s*HEAD\s*=\s*(?P<sha>{_HEX}){_NOT_HEX}"),
    re.compile(rf"^\s*#?\s*HEAD\s*:\s*(?P<sha>{_HEX}){_NOT_HEX}"),
    re.compile(rf"^\s*#?\s*HEAD\s+(?P<sha>{_HEX}){_NOT_HEX}"),
    re.compile(rf"^\s*#.*?\bHEAD\s+(?P<sha>{_HEX}){_NOT_HEX}"),  # prose, inside a comment
    re.compile(rf"^\s*==\s*commit under test:.*?@\s*(?P<sha>{_HEX})\s*==", re.IGNORECASE),
)
#: A stamp-shaped line this parser failed on. Deliberately narrow: the assignment/colon forms and
#: the T-6 marker. `HEAD tree  : <sha>` is a table row, not an unparsable stamp.
_LOOKS_LIKE_STAMP = re.compile(r"^\s*#?\s*HEAD\s*[=:]|^\s*==\s*commit under test", re.IGNORECASE)


class EvidenceBundleError(ValueError):
    """The bundle cannot be regenerated (dirty tree, missing generator, failed capture, no HEAD)."""


@dataclass(frozen=True)
class EvidenceStamp:
    sha: str
    line: str
    line_number: int
    canonical: bool = False


@dataclass
class FreshnessResult:
    state: str
    evidence: str = ""
    notes: str = ""
    #: The commit the stamps were compared against: the nearest non-evidence-only, non-empty
    #: ancestor of the commit under verification. Named in every reason string.
    compared_commit: str | None = None
    #: The commit under verification itself, which may be an evidence-only commit on top of it.
    commit_under_verification: str | None = None
    stamped_files: int = 0
    stale_paths: list[str] = field(default_factory=list)
    stale_reasons: dict[str, str] = field(default_factory=dict)
    deleted_paths: list[str] = field(default_factory=list)
    deleted_by: dict[str, str] = field(default_factory=dict)
    unparsed_paths: list[str] = field(default_factory=list)
    fresh_with_explanation: dict[str, str] = field(default_factory=dict)

    @property
    def stale_count(self) -> int:
        return len(self.stale_paths)


@dataclass
class BundleResult:
    label: str
    evidence_dir: str
    head: str
    head_short: str
    command: str
    generated_at: str
    files: list[str]
    rewritten: list[str]
    manifest: str
    duration_seconds: float = 0.0
    mode: str = MODE_FAST
    only: list[str] = field(default_factory=list)
    steps: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "evidence_dir": self.evidence_dir,
            "head": self.head,
            "head_short": self.head_short,
            "command": self.command,
            "generated_at": self.generated_at,
            "files": self.files,
            "rewritten": self.rewritten,
            "rewritten_count": len(self.rewritten),
            "manifest": self.manifest,
            "duration_seconds": round(self.duration_seconds, 2),
            "mode": self.mode,
            "only": self.only,
            "steps": self.steps,
        }


def applies_to_kind(kind: object) -> bool:
    """True for the task kinds that write code and therefore carry an evidence bundle."""
    return str(getattr(kind, "value", kind)) in REVIEWED_KINDS


def check_status(result: FreshnessResult, strict: bool = False) -> str:
    """Map the freshness state onto the check-status vocabulary (five values, CCR-T15-01).

    A compared-and-out-of-date bundle is `stale` — reported, never silently fresh and never a
    failing check unless the operator opts in with `ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT=true`.
    Absent or unstamped evidence is `not_applicable`, a fresh bundle `pass`.
    """
    if result.state == STATE_PASS:
        return "pass"  # noqa: S105 - frozen status value
    if result.state == STATE_STALE:
        return "fail" if strict else "stale"
    return "not_applicable"


def normalize_label(label: str) -> str:
    label = str(label).strip()
    return f"T-{label}" if label.isdigit() else label


def evidence_dir(root: Path | str, label: str) -> Path:
    return Path(root) / EVIDENCE_ROOT / normalize_label(label)


def _git_text(repo: Path, args: list[str]) -> str:
    result = run_git(args, repo, check=False)
    return result.stdout if result.code == 0 else ""


def _resolve_commit(repo: Path, ref: str | None) -> str | None:
    if not ref:
        return None
    r = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo, check=False)
    return r.stdout.strip() if r.code == 0 and r.stdout.strip() else None


def _short(repo: Path, sha: str) -> str:
    r = run_git(["rev-parse", "--short", sha], repo, check=False)
    return r.stdout.strip() if r.code == 0 and r.stdout.strip() else sha[:7]


def parse_stamps(text: str, window: int = STAMP_WINDOW) -> tuple[list[EvidenceStamp], list[str]]:
    """HEAD stamps in the first ``window`` lines, plus stamp-shaped lines that did not parse."""
    stamps: list[EvidenceStamp] = []
    unparsed: list[str] = []
    for number, line in enumerate(text.splitlines()[:window], 1):
        found = next(
            (m.group("sha") for p in _STAMP_PATTERNS if (m := p.search(line)) is not None), None
        )
        if found:
            canonical = bool(
                re.fullmatch(rf"\s*#\s*HEAD={_HEX}(\s+mode=(fast|full))?\s*", line)
            )
            stamps.append(EvidenceStamp(found, line, number, canonical=canonical))
        elif _LOOKS_LIKE_STAMP.match(line):
            unparsed.append(line)
    return stamps, unparsed


def _is_stamp_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in _STAMP_PATTERNS)


def _manifest_stamps(text: str) -> tuple[list[EvidenceStamp], list[str]]:
    """`manifest.json` carries its HEAD as a field, not in the header window."""
    try:
        data = json.loads(text)
    except ValueError:
        return [], []
    if not isinstance(data, dict):
        return [], []
    stamp = data.get("stamp")
    if isinstance(stamp, str):
        return parse_stamps(stamp, window=1)
    head = data.get("head")
    if isinstance(head, str) and re.fullmatch(_HEX, head):
        return [EvidenceStamp(head, f'"head": "{head}"', 1, canonical=True)], []
    return [], []


def _stamps_for_file(path: str, text: str) -> tuple[list[EvidenceStamp], list[str]]:
    if Path(path).name == MANIFEST_NAME:
        stamps, unparsed = _manifest_stamps(text)
        if stamps:
            return stamps, unparsed
    return parse_stamps(text)


def _trailers(repo: Path, sha: str) -> list[str]:
    """The commit's trailer block as git parses it (empty when there is none)."""
    out = _git_text(repo, ["show", "-s", "--format=%(trailers)", sha])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _author(repo: Path, sha: str) -> str:
    """`Name <email>` of the commit's author, recorded next to an exemption trailer.

    A trailer is forgeable prose; the author is metadata the committer does not type into the
    message. It never changes the status either — it is context for the human reading the reason.
    """
    return _git_text(repo, ["show", "-s", "--format=%an <%ae>", sha]).strip() or "unknown"


def _manifest_declared_inputs(repo: Path, sha: str, rel_dir: str) -> set[str]:
    """Paths the manifest at ``sha`` declares as a generator/input, relative to ``rel_dir``.

    An evidence-only commit is certified only by its file list, so a generator that happens to live
    under ``verify-evidence/`` must be declared by the manifest instead of being hardcoded by name.
    Otherwise any helper generator would be mistaken for evidence. A legacy bundle with no manifest
    still has its ``capture.sh`` treated as a generator.
    """
    declared: set[str] = {CAPTURE_NAME}
    text = _git_text(repo, ["show", f"{sha}:{rel_dir}/{MANIFEST_NAME}"])
    if not text.strip():
        return declared
    try:
        data = json.loads(text)
    except ValueError:
        return declared
    if not isinstance(data, dict):
        return declared
    generator = data.get("generator")
    if isinstance(generator, str) and generator.strip():
        declared.add(Path(generator.strip()).name)
    inputs = data.get("inputs")
    if isinstance(inputs, list):
        declared.update(Path(str(path)).name for path in inputs if str(path).strip())
    return declared


def _generator_paths(repo: Path, sha: str, files: list[str]) -> set[str]:
    """Repo-relative generator/input paths at ``sha`` for every evidence dir in ``files``."""
    directories = {
        Path(path).parent.as_posix() for path in files if path.startswith(f"{EVIDENCE_ROOT}/")
    }
    generators: set[str] = set()
    for directory in directories:
        for name in _manifest_declared_inputs(repo, sha, directory):
            generators.add(f"{directory}/{name}")
    return generators


def _commit_class(repo: Path, sha: str) -> tuple[bool, str | None]:
    """(walkable, reason).

    ``walkable`` is True only for a commit that cannot change measured content (evidence-only or
    empty): :func:`_comparison_target` walks past those to find the code HEAD a stamp may name. A
    parsed trailer is a reason, but **not** a walkable class: a Worker can write any trailer into
    its own code commit, so a recovery/hand-fix commit is explained and the bundle still reads
    stale. That is the T-8 boundary: detection is the trailer, certification is the file list.
    """
    keys = {line.split(":", 1)[0].strip() for line in _trailers(repo, sha)}
    parents = _git_text(repo, ["rev-list", "--parents", "-n", "1", sha]).split()[1:]
    if len(parents) > 1:
        # A merge can bring in any content; `diff-tree` on a merge shows nothing by default, so it
        # must never be mistaken for the "empty commit" that a walk-back skips.
        return False, "merge commit (explained, not certified)"
    files = [
        line.strip()
        for line in _git_text(
            repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha]
        ).splitlines()
        if line.strip()
    ]
    generators = _generator_paths(repo, sha, files)
    evidence_only = (
        bool(files)
        and all(path.startswith(f"{EVIDENCE_ROOT}/") for path in files)
        and not (set(files) & generators)
    )
    if evidence_only:
        return True, "evidence-only commit"
    if not files:
        return True, "empty commit"
    if RECOVERY_TRAILER in keys:
        return False, "harness recovery commit (explained, not certified)"
    if HAND_FIX_TRAILER in keys:
        return False, "operator hand-fix commit (explained, not certified)"
    return False, None


def _parents(repo: Path, sha: str) -> list[str]:
    return _git_text(repo, ["rev-list", "--parents", "-n", "1", sha]).split()[1:]


def _comparison_target(repo: Path, commit: str) -> str:
    """Nearest ancestor (including ``commit``) that is not evidence-only and not empty.

    An evidence-only commit changes no code, so a stamp naming its parent code HEAD is truthful
    about what it measured; an empty commit changes nothing at all. Both are walked past. Every
    commit that changes code — a recovery sweep or an operator hand-fix included — stops the walk
    and becomes the target a stamp must match. ``commit`` is returned unchanged when it already is
    a code commit.
    """
    current = commit
    for _ in range(10000):
        walkable, _reason = _commit_class(repo, current)
        if not walkable:
            return current
        parents = _parents(repo, current)
        if not parents:
            return current  # a root commit has no older target to walk to
        current = parents[0]
    return current


def _stamp_verdict(repo: Path, stamp: EvidenceStamp, target: str) -> tuple[bool, str]:
    """Compare one stamp with the comparison target, naming that target in every reason."""
    full = _resolve_commit(repo, stamp.sha)
    if full is None:
        return False, f"stamp {stamp.sha} does not resolve to a commit"
    if full == target:
        return True, "matches the commit under verification"
    target_name = f"{_short(repo, target)} ({target})"
    if run_git(["merge-base", "--is-ancestor", full, target], repo, check=False).code != 0:
        return False, (
            f"stamp {_short(repo, full)} is not an ancestor of the comparison target "
            f"{target_name}"
        )
    commits = [
        line.strip()
        for line in _git_text(repo, ["rev-list", "--reverse", f"{full}..{target}"]).splitlines()
        if line.strip()
    ]
    classes = [(sha, *_commit_class(repo, sha)) for sha in commits]
    explained = [
        f"{_short(repo, sha)} ({reason}; author {_author(repo, sha)})"
        for sha, walkable, reason in classes
        if reason and not walkable
    ]
    blocking = [
        _short(repo, sha) for sha, walkable, reason in classes if not reason and not walkable
    ]
    skipped = [_short(repo, sha) for sha, walkable, _reason in classes if walkable]
    if explained and not blocking:
        detail = (
            "differs only by commit(s) with an exemption trailer: "
            + ", ".join(explained)
            + "; the trailer only explains the move, it does not certify the content (it is "
            "forgeable prose, so forging one changes the explanation and never the status)"
        )
    else:
        detail = "predates non-evidence commit(s) " + ", ".join(
            blocking or [_short(repo, sha) for sha, walkable, _why in classes if not walkable]
        )
    if skipped:
        detail += "; walked past evidence-only/empty commit(s) " + ", ".join(skipped)
    return False, f"stamp {_short(repo, full)} {detail}, so it is stale against {target_name}"


def _committed_files(repo: Path, commit: str, directory: str) -> list[str]:
    out = _git_text(repo, ["ls-tree", "-r", "--name-only", commit, "--", directory])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _worktree_files(root: Path, directory: str) -> list[str]:
    base = root / directory
    if not base.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in base.rglob("*") if p.is_file())


def _deleted_evidence(
    repo: Path, commit: str, directory: str, base_branch: str | None, current: set[str]
) -> tuple[list[str], dict[str, str]]:
    """Paths under ``directory`` deleted between the branch base and ``commit``, still gone."""
    spec = f"{base_branch}..{commit}" if base_branch else commit
    if base_branch and _git_text(repo, ["rev-list", "--count", spec]).strip() == "0":
        # The commit is on the base branch itself: searching an empty range would hide deletions.
        spec = commit
    out = _git_text(
        repo,
        ["log", "--format=commit %H", "--name-status", "--diff-filter=D", spec, "--", directory],
    )
    deleted: dict[str, str] = {}
    deleting = ""
    for line in out.splitlines():
        if line.startswith("commit "):
            deleting = line.split(None, 1)[1].strip()
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].startswith("D"):
            # `git log` is newest first: the first deletion seen is the one that removed it.
            deleted.setdefault(parts[1].strip(), deleting)
    gone = {path: sha for path, sha in deleted.items() if path not in current}
    return sorted(gone), gone


def _read_evidence(repo: Path, commit: str, path: str, from_worktree: bool) -> str | None:
    if not from_worktree:
        show = run_git(["show", f"{commit}:{path}"], repo, check=False)
        return show.stdout if show.code == 0 else None
    try:
        return (repo / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def check_freshness(
    repo: Path | str | None,
    commit: str | None,
    label: str,
    base_branch: str | None = None,
    *,
    directory: str | None = None,
    from_worktree: bool = False,
    ignore: frozenset[str] | None = None,
) -> FreshnessResult:
    """Compare every committed evidence stamp against ``commit`` (the run's ``commit_sha``).

    Stamps are compared with the **comparison target**: the nearest ancestor of ``commit`` that is
    not evidence-only and not empty (see :func:`_comparison_target`). An evidence-only commit
    changes no code, so a bundle delivered by one is honestly stamped at the code HEAD it measured;
    every code commit after that bundle — recovery sweep or operator hand-fix included — becomes
    the target and makes it stale. ``from_worktree=True`` reads the files on disk; the generator
    uses it to certify the bundle it just wrote, before that bundle is committed.
    """
    if repo is None:
        return FreshnessResult(STATE_NOT_APPLICABLE, "no worktree to inspect")
    root = Path(repo)
    under = _resolve_commit(root, commit)
    if under is None:
        return FreshnessResult(
            STATE_NOT_APPLICABLE, f"no resolvable commit under verification ({commit or 'none'})"
        )
    target = _comparison_target(root, under)
    rel_dir = directory or f"{EVIDENCE_ROOT}/{normalize_label(label)}"
    target_short = _short(root, target)
    under_short = _short(root, under)
    paths = (
        _worktree_files(root, rel_dir)
        if from_worktree
        else _committed_files(root, under, rel_dir)
    )
    stale_paths: list[str] = []
    stale_reasons: dict[str, str] = {}
    unparsed_paths: list[str] = []
    fresh_by_exemption: dict[str, str] = {}
    stamped_files = 0

    for path in paths:
        if ignore and Path(path).name in ignore:
            continue
        text = _read_evidence(root, under, path, from_worktree)
        if text is None:
            continue
        stamps, unparsed = _stamps_for_file(path, text)
        if unparsed:
            unparsed_paths.append(path)
            stale_paths.append(path)
            stale_reasons[path] = "unparsable HEAD stamp: " + " | ".join(
                u.strip() for u in unparsed
            )
            continue
        if not stamps:
            continue
        stamped_files += 1
        verdicts = [_stamp_verdict(root, stamp, target) for stamp in stamps]
        if any(ok for ok, _why in verdicts):
            explanations = [
                why for ok, why in verdicts if ok and why != "matches the commit under verification"
            ]
            if explanations:
                fresh_by_exemption[path] = explanations[0]
            continue
        stale_paths.append(path)
        stale_reasons[path] = "; ".join(why for _ok, why in verdicts)

    deleted_paths, deleted_by = _deleted_evidence(root, under, rel_dir, base_branch, set(paths))
    common = {
        "compared_commit": target,
        "commit_under_verification": under,
        "stamped_files": stamped_files,
        "stale_paths": sorted(stale_paths),
        "stale_reasons": stale_reasons,
        "deleted_paths": deleted_paths,
        "deleted_by": deleted_by,
        "unparsed_paths": unparsed_paths,
        "fresh_with_explanation": fresh_by_exemption,
    }
    if stale_paths or deleted_paths:
        parts = [
            f"{len(stale_paths)} stale of {stamped_files} stamped evidence file(s) against "
            f"{target_short} ({target})"
        ]
        if under != target:
            parts.append(
                f"commit under verification {under_short} ({under}) is evidence-only/empty on top "
                "of that target"
            )
        if deleted_paths:
            parts.append(f"{len(deleted_paths)} deleted evidence file(s)")
        notes = [f"{path}: {reason}" for path, reason in stale_reasons.items()]
        notes += [
            f"{path}: deleted by {deleted_by[path]} and absent at {target_short}"
            for path in deleted_paths
        ]
        notes += [f"{path}: {reason}" for path, reason in fresh_by_exemption.items()]
        return FreshnessResult(STATE_STALE, "; ".join(parts), "\n".join(notes), **common)

    if stamped_files == 0:
        reason = (
            f"no stamped evidence under {rel_dir} at {under_short}"
            if paths
            else f"no committed evidence under {rel_dir} at {under_short}"
        )
        return FreshnessResult(
            STATE_NOT_APPLICABLE, evidence=reason, compared_commit=target, stamped_files=0
        )
    # Say which sense of "fresh" applies: an exact match, or fresh only through exempt commits.
    tail = (
        f"; {len(fresh_by_exemption)} fresh only via exempt commit(s)"
        if fresh_by_exemption
        else ""
    )
    where = f"{stamped_files} stamped evidence file(s) fresh at {target_short} ({target})"
    if under != target:
        where += (
            f"; commit under verification {under_short} ({under}) is evidence-only/empty on top "
            "of that target"
        )
    return FreshnessResult(
        STATE_PASS,
        evidence=where + tail,
        notes="\n".join(f"{path}: {reason}" for path, reason in fresh_by_exemption.items()),
        **common,
    )


def review_finding(result: FreshnessResult) -> dict[str, str]:
    """The harness-detected finding body for the Architect payload."""
    return {
        "file_or_area": (
            ", ".join(result.stale_paths + result.deleted_paths) or "committed evidence"
        ),
        "issue": (
            "harness-detected evidence freshness: "
            f"{result.stale_count} stale and {len(result.deleted_paths)} deleted committed "
            f"evidence file(s) against the commit under verification. {result.evidence}. "
            "Refresh the bundle with one command (`orchestrator task evidence <id>`); it "
            "regenerates every file it can from the current tip. Details: "
            f"{result.notes.replace(chr(10), ' | ')}"
        ),
        "violates": "T-15 DoD 1 (committed evidence must match the commit under review)",
    }


def canonicalize_stamps(path: Path, head: str, mode: str = MODE_FAST) -> bool:
    """Rewrite the header stamps in one file to the canonical line. True if bytes changed."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    stamps, _unparsed = parse_stamps(text)
    if not stamps:
        return False
    out: list[str] = []
    inserted = False
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        if number <= STAMP_WINDOW and _is_stamp_line(line):
            if not inserted:
                out.append(
                    CANONICAL_STAMP.format(sha=head, mode=mode)
                    + ("\n" if line.endswith("\n") else "")
                )
                inserted = True
            continue
        out.append(line)
    new_text = "".join(out)
    if not inserted or new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def _hash_tree(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _tool_versions(root: Path) -> dict[str, str]:
    git = run_git(["--version"], root, check=False)
    versions = {"python": platform.python_version(), "git": git.stdout.strip() or "unavailable"}
    try:
        import pytest  # noqa: PLC0415 - optional, only for the manifest

        versions["pytest"] = pytest.__version__
    except Exception:  # pragma: no cover - pytest is present in the gated environment
        versions["pytest"] = "unavailable"
    return versions


def _manifest_payload(
    root: Path,
    directory: Path,
    label: str,
    head: str,
    command: str,
    previous: dict | None,
    mode: str = MODE_FAST,
    only: tuple[str, ...] = (),
) -> dict:
    generator = f"{directory.relative_to(root).as_posix()}/{CAPTURE_NAME}"
    # `files` lists what the pass PRODUCED; the generator is an input and is declared separately,
    # so `--only capture.sh` can never be accepted as a declared output.
    inputs = [CAPTURE_NAME]
    produced = [
        {"path": path, "sha256": digest}
        for path, digest in _hash_tree(directory).items()
        if path not in NOT_MEASURED and path not in inputs
    ]
    payload = {
        "label": label,
        "head": head,
        "head_short": _short(root, head),
        "stamp": CANONICAL_STAMP.format(sha=head, mode=mode),
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "only": sorted(only),
        "command": command,
        "generator": generator,
        "inputs": inputs,
        "tool_versions": _tool_versions(root),
        "files": produced,
    }
    # A second run at the same HEAD with the same files is a byte-for-byte no-op, timestamp
    # included; otherwise the command would dirty its own manifest and DoD 5(d) could not hold.
    comparable = {k: v for k, v in payload.items() if k != "generated_at"}
    if previous:
        old = {k: v for k, v in previous.items() if k != "generated_at"}
        if old == comparable and isinstance(previous.get("generated_at"), str):
            payload["generated_at"] = previous["generated_at"]
    return payload


def _write_self_check(
    root: Path,
    directory: Path,
    label: str,
    head: str,
    mode: str = MODE_FAST,
    only: tuple[str, ...] = (),
) -> None:
    """Record the pass's own worktree-basis check. Unstamped: it certifies the files around it.

    Written LAST in the pass, after every measured file and the timing record, and it prints the
    commit it compared. It cannot name the commit that will *deliver* the bundle (a file cannot
    contain the SHA of the commit that adds it), so the committed-view proof at the delivered tip is
    :func:`write_at_tip_report`.
    """
    result = check_freshness(
        root,
        head,
        label,
        None,
        from_worktree=True,
        ignore=frozenset({MANIFEST_NAME, SELF_CHECK_NAME}),
    )
    (directory / SELF_CHECK_NAME).write_text(
        "\n".join(
            [
                "# Self-check written LAST by the one bundle pass (not hand-edited, no HEAD",
                "# stamp: it certifies the files around it; the gate reports the committed view).",
                f"# command: orchestrator task evidence {label}"
                + (" --full" if mode == MODE_FULL else "")
                + (f" --only {','.join(only)}" if only else ""),
                f"mode={mode}",
                f"only={','.join(only) or 'all files this pass produces'}",
                "# rewrites and wall time are reported by that command; this file records only",
                "# what the check saw, so a no-op second run is byte-identical.",
                "basis=worktree",
                "scope=every evidence file except manifest.json and this file",
                f"commit_under_verification={result.commit_under_verification}",
                f"compared_commit={result.compared_commit}",
                f"status={result.state}",
                f"stamped_files={result.stamped_files}",
                f"stale_count={result.stale_count}",
                f"deleted_count={len(result.deleted_paths)}",
                f"evidence={result.evidence or 'n/a'}",
                f"notes={result.notes or 'none'}",
                "committed_view=the stamps name this HEAD; the commit adding this bundle must be "
                "evidence-only, and the gate then reports pass with that explanation; run "
                "`orchestrator task evidence <id> --at-tip` at the delivered tip for the "
                "committed-view record (see tests/unit/test_evidence_freshness.py::"
                "test_the_generated_bundle_is_fresh_once_committed).",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _worktree_generator_paths(root: Path, rel_dir: str) -> set[str]:
    """Repo-relative paths the worktree manifest declares as a generator/input.

    Any declared generator or helper input counts, not one hardcoded filename: an edited helper
    would run while the bundle claims the committed one.
    """
    declared: set[str] = {f"{rel_dir}/{CAPTURE_NAME}"}
    manifest = root / rel_dir / MANIFEST_NAME
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return declared
    if not isinstance(data, dict):
        return declared
    names: list[str] = []
    generator = data.get("generator")
    if isinstance(generator, str) and generator.strip():
        names.append(generator.strip())
    inputs = data.get("inputs")
    if isinstance(inputs, list):
        names.extend(str(path).strip() for path in inputs if str(path).strip())
    for name in names:
        declared.add(name if name.startswith(f"{rel_dir}/") else f"{rel_dir}/{Path(name).name}")
    return declared


def _uncommitted_inputs(root: Path, rel_dir: str) -> list[str]:
    """Uncommitted inputs the generator would measure, plus every declared generator/input.

    Untracked paths count. The generator runs against the tree on disk, so an untracked source or
    test file would be measured while the stamp names a commit that does not contain it — a stamp
    certifying code the commit does not have. Paths under the evidence directory are exempt because
    the pass writes them, except every path the manifest declares as a generator or input, which is
    code and must be committed to be trusted.
    """
    inputs = _worktree_generator_paths(root, rel_dir)
    out: list[str] = []
    for line in _git_text(root, ["status", "--porcelain", "-uall"]).splitlines():
        if not line.strip():
            continue
        code, path = line[:2].strip(), line[3:].strip()
        if " -> " in path:  # a rename: the destination is the path that exists now
            path = path.split(" -> ", 1)[1].strip()
        if path.startswith(f"{rel_dir}/") and path not in inputs:
            continue
        out.append(f"{path} [{code or '??'}]")
    return out


# ------------------------------------------------------------------ timing (DoD 3)


def _git_dir(root: Path) -> Path:
    out = _git_text(root, ["rev-parse", "--absolute-git-dir"]).strip()
    return Path(out) if out else root / ".git"


def _timing_state_path(root: Path, label: str) -> Path:
    """Per-worktree scratch record, inside the git dir so it never pollutes the evidence diff."""
    return _git_dir(root) / "orchestrator-evidence" / f"{label}.timings.json"


def _parse_steps(stdout: str) -> dict[str, int]:
    """`T15_STEP|name|milliseconds` lines emitted by a committed capture script."""
    steps: dict[str, int] = {}
    for line in (stdout or "").splitlines():
        if not line.startswith("T15_STEP|"):
            continue
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[2].strip().isdigit():
            steps[parts[1].strip()] = int(parts[2].strip())
    return steps


def _record_timing(root: Path, label: str, mode: str, record: dict) -> dict:
    path = _timing_state_path(root, label)
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    data[mode] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return data


def _write_timing_txt(
    directory: Path, head: str, data: dict, mode: str = MODE_FULL
) -> bool:
    """One committed file holding BOTH pass modes: wall time, file count and rewrite count.

    Only a ``--full`` pass writes it. A file that changes on every run would dirty the bundle the
    default pass just certified, so the fast pass records its numbers in the scratch state only and
    the next ``--full`` pass commits both. Every number quoted in the bundle's README or a handoff
    must come from this file. Each row carries the HEAD its own pass measured, so a `mode=fast` row
    left over from an earlier tip never inherits the `--full` pass's stamp (one measured source per
    row).
    """
    lines = [
        CANONICAL_STAMP.format(sha=head, mode=mode),
        "# Both pass modes in one file. Only a --full pass writes it, so the default fast pass",
        "# stays byte-identical on a re-run (DoD 5d); the next --full pass refreshes both.",
        "# Every row carries the HEAD ITS OWN pass measured: a fast row may name an older code",
        "# HEAD than the header stamp, and it is not a restamp of the full pass's commit.",
        "# wall_seconds is this driver pass; files/rewritten count what the GENERATOR saw and",
        "# rewrote in that pass (manifest.json, the self-check and this file are written after the",
        "# capture and are not counted). Quote these numbers, never a scratch or /tmp log.",
    ]
    for name in (MODE_FAST, MODE_FULL):
        record = data.get(name)
        if not isinstance(record, dict):
            lines.append(f"mode={name} head=- wall_seconds=not-recorded files=- rewritten=-")
            continue
        steps = " ".join(
            f"{key}_ms={value}" for key, value in sorted((record.get("steps") or {}).items())
        )
        lines.append(
            f"mode={name} head={record.get('head', '-')} "
            f"wall_seconds={float(record.get('wall_seconds', 0.0)):.2f} "
            f"files={record.get('files', '-')} rewritten={record.get('rewritten', '-')}"
            + (f" {steps}" if steps else "")
        )
    path = directory / "timing.txt"
    text = "\n".join(lines) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def _committed_outputs(root: Path, head: str, rel_dir: str) -> list[str] | None:
    """The outputs the COMMITTED manifest declares, or None when there is no usable manifest."""
    text = _git_text(root, ["show", f"{head}:{rel_dir}/{MANIFEST_NAME}"])
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return None
    paths = [f.get("path") if isinstance(f, dict) else f for f in files]
    return [str(p) for p in paths if p]


def regenerate_bundle(
    root: Path | str,
    label: str,
    *,
    directory: str | None = None,
    timeout: int = 900,
    full: bool = False,
    only: list[str] | tuple[str, ...] | None = None,
) -> BundleResult:
    """Regenerate one evidence directory in a single invocation.

    Runs **only** the committed ``capture.sh`` — never an arbitrary command string, because a
    command the branch does not contain cannot be reproduced from it and can re-stamp content it did
    not measure. It canonicalises only the files that pass rewrote, then writes ``timing.txt`` (full
    pass), ``freshness_selfcheck.txt`` and ``manifest.json``. ``full=True`` sets ``T15_FULL=1`` and
    records ``mode=full``; the default fast pass is the one that has to finish in well under a
    minute. ``only`` refreshes named outputs the committed manifest declares, and an unrecognised
    name is refused.
    """
    root = Path(root).resolve()
    if not (root / ".git").exists():
        raise EvidenceBundleError(f"{root} is not a git worktree")
    # Resolve BOTH sides: `is_relative_to` without `resolve()` is a lexical check, so a path with
    # `..` could pass it while pointing outside the worktree.
    directory = Path(directory).resolve() if directory else evidence_dir(root, label)
    if not directory.is_relative_to(root):
        raise EvidenceBundleError(f"evidence directory {directory} is outside {root}")
    rel_dir = directory.relative_to(root).as_posix()
    label = normalize_label(label)
    raw_head = head_sha(root)
    if not raw_head:
        raise EvidenceBundleError("cannot resolve HEAD in the worktree")
    # Stamp the code HEAD this pass certifies, which is the nearest non-evidence-only ancestor when
    # the worktree sits on a bundle commit: an evidence-only commit changes no code, so its parent
    # is the HEAD the measurements really describe. That is what lets a bundle report pass on the
    # commit that ships it (operator ruling 2026-11-09 (d), finding 1).
    head = _comparison_target(root, raw_head)
    script_rel = f"{rel_dir}/{CAPTURE_NAME}"
    if not (directory / CAPTURE_NAME).is_file():
        raise EvidenceBundleError(
            f"no committed generator at {script_rel}; a bundle is refreshed by a command the "
            "branch contains, so commit one"
        )
    committed = _git_text(root, ["ls-tree", "-r", "--name-only", raw_head, "--", script_rel])
    if script_rel not in committed.splitlines():
        raise EvidenceBundleError(
            f"{script_rel} is not committed at {raw_head[:7]}; a stamp must be reproducible from "
            "branch, so commit the generator before regenerating"
        )
    selected = tuple(dict.fromkeys(str(n).strip() for n in (only or ()) if str(n).strip()))
    if selected:
        declared = _committed_outputs(root, raw_head, rel_dir)
        if declared is None:
            raise EvidenceBundleError(
                f"--only needs a committed {rel_dir}/{MANIFEST_NAME} at {raw_head[:7]} to know "
                "which files the generator produces; run a full pass and commit it first"
            )
        declared_inputs = _manifest_declared_inputs(root, raw_head, rel_dir)
        inputs_named = [name for name in selected if name in declared_inputs]
        if inputs_named:
            raise EvidenceBundleError(
                f"--only names {', '.join(inputs_named)}, which the manifest declares as a "
                "generator/input, not an output; inputs are run, never selected"
            )
        unknown = [name for name in selected if name not in declared]
        if unknown:
            raise EvidenceBundleError(
                f"--only names {', '.join(unknown)}, which the committed generator does not "
                f"declare as an output; recognised names at {raw_head[:7]}: "
                + ", ".join(sorted(declared))
            )
    dirty = _uncommitted_inputs(root, rel_dir)
    if dirty:
        raise EvidenceBundleError(
            "worktree has uncommitted inputs outside the generated evidence — tracked changes or "
            f"untracked files — or an edited generator (e.g. {dirty[0]}); a stamp would name a "
            "HEAD whose tree was never measured. Commit the code this bundle certifies, then "
            "regenerate."
        )
    before = _hash_tree(directory)
    started = time.monotonic()
    mode = MODE_FULL if full else MODE_FAST
    env = os.environ.copy()
    if full:
        env["T15_FULL"] = "1"
    if selected:
        env["T15_ONLY"] = ",".join(selected)
    try:
        proc = subprocess.run(
            [str(directory / CAPTURE_NAME)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise EvidenceBundleError(f"evidence generator timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
        raise EvidenceBundleError(
            f"evidence generator exited {proc.returncode}: " + " | ".join(tail)
        )
    if not directory.is_dir():
        raise EvidenceBundleError(f"generator produced no directory at {rel_dir}")
    captured = _hash_tree(directory)
    for rel in sorted(captured):
        if Path(rel).name in NOT_MEASURED:
            continue
        if selected and rel not in selected:
            # A file this pass did not select keeps whatever stamp it had: unstamped is a lie, and
            # only the pass that re-ran its command may write a stamp.
            continue
        if before.get(rel) != captured[rel]:
            canonicalize_stamps(directory / rel, head, mode)
    duration = time.monotonic() - started
    record = {
        "head": head,
        "wall_seconds": round(duration, 2),
        "files": len([p for p in captured if p != MANIFEST_NAME]),
        "rewritten": len(
            [p for p in set(before) | set(captured) if before.get(p) != captured.get(p)]
        ),
        "steps": _parse_steps(proc.stdout),
        "command": script_rel + (" --full" if full else "")
        + (f" --only {','.join(selected)}" if selected else ""),
    }
    timings = _record_timing(root, label, mode, record)
    if full:
        _write_timing_txt(directory, head, timings, mode)
    _write_self_check(root, directory, label, head, mode, selected)

    manifest_path = directory / MANIFEST_NAME
    previous = None
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
    payload = _manifest_payload(
        root, directory, label, head, script_rel, previous, mode, selected
    )
    new_text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != new_text:
        manifest_path.write_text(new_text, encoding="utf-8")

    after = _hash_tree(directory)
    changed = set(before) | set(captured) | set(after)
    rewritten = sorted(path for path in changed if before.get(path) != after.get(path))
    return BundleResult(
        label=label,
        evidence_dir=rel_dir,
        head=head,
        head_short=_short(root, head),
        command=script_rel,
        generated_at=payload["generated_at"],
        files=sorted(path for path in after if path != MANIFEST_NAME),
        rewritten=rewritten,
        manifest=f"{rel_dir}/{MANIFEST_NAME}",
        duration_seconds=duration,
        mode=mode,
        only=list(selected),
        steps=record["steps"],
    )


def write_at_tip_report(
    root: Path | str, label: str, commit: str = "HEAD"
) -> tuple[FreshnessResult, Path]:
    """Record the COMMITTED-VIEW check at the delivered tip, into an unstamped report file.

    Run LAST, after the commit that adds the bundle: it prints BOTH the delivered tip it was run at
    and the comparison target it resolved to (the nearest non-evidence-only ancestor), so a reader
    can see which code HEAD the stamps were measured at. It refuses to write unless the check
    passes — a report that says `pass` when it does not would be the same lie as a stamp on
    unmeasured content.
    """
    root = Path(root).resolve()
    label = normalize_label(label)
    result = check_freshness(root, commit, label)
    path = evidence_dir(root, label) / AT_TIP_NAME
    if result.state != STATE_PASS:
        raise EvidenceBundleError(
            f"refusing to write {AT_TIP_NAME}: bundle is {result.state} against "
            f"{result.compared_commit or commit} ({result.evidence})"
        )
    short = _short(root, result.compared_commit or "")
    delivered = result.commit_under_verification or ""
    path.write_text(
        "\n".join(
            [
                "# Committed-view freshness at the delivered tip (not hand-edited, no HEAD stamp:",
                "# this is a report about the commits it names, not evidence measured at it).",
                f"# command: orchestrator task evidence {label} --at-tip",
                "basis=committed",
                f"delivered_tip={delivered}",
                f"delivered_tip_short={_short(root, delivered) if delivered else ''}",
                f"compared_commit={result.compared_commit}",
                f"compared_short={short}",
                f"status={result.state}",
                f"stamped_files={result.stamped_files}",
                f"stale_count={result.stale_count}",
                f"deleted_count={len(result.deleted_paths)}",
                f"evidence={result.evidence}",
                f"notes={result.notes or 'none'}",
                "delivery=the comparison target is the nearest ancestor of the delivered tip that "
                "is not an evidence-only or empty commit; the commit adding this report touches "
                "only verify-evidence/, so the same check at the final tip reports pass naming "
                f"that code HEAD ({short}).",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return result, path


__all__ = [
    "AT_TIP_NAME",
    "INDEX_NAME",
    "NOT_MEASURED",
    "CAPTURE_NAME",
    "CHECK_NAME",
    "EVIDENCE_ROOT",
    "MANIFEST_NAME",
    "MODE_FAST",
    "MODE_FULL",
    "REVIEWED_KINDS",
    "SELF_CHECK_NAME",
    "STAMP_WINDOW",
    "BundleResult",
    "EvidenceBundleError",
    "EvidenceStamp",
    "FreshnessResult",
    "applies_to_kind",
    "canonicalize_stamps",
    "check_freshness",
    "check_status",
    "evidence_dir",
    "normalize_label",
    "parse_stamps",
    "regenerate_bundle",
    "review_finding",
    "write_at_tip_report",
]
