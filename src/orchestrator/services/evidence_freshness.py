"""Committed evidence freshness and the one-command evidence bundle.

A committed evidence file is only trustworthy while the HEAD it was measured at is the commit
under review. Before T-15 nothing in the gate compared the two, so a round could harden its
assertions and still be reviewed against an older copy of its own tests (measured on T-4 round 5:
66 of 68 files carried the round-4 tip). This module is the deterministic equality test, plus the
single generator that makes keeping a bundle honest cheap.

Two rules shape the implementation:

* The stamp formats are a legacy vocabulary, not one format. Nine shapes exist in the repository
  (``# HEAD=<sha>``, ``# HEAD <sha>``, prose, pipe-delimited, short SHAs, ...). The parser accepts
  all of them; a short SHA is prefix-resolved against the commit under verification, never
  string-compared. A stamp-shaped line the parser cannot read is reported as stale, never fresh.
* A different SHA is not automatically stale. HEAD moves for reasons the Worker does not control:
  harness recovery commits (``Orchestrator-Recovery:``), operator hand-fixes
  (``Operator-Hand-Fix:``), and evidence-only commits. A stamp that differs only by those commits
  is reported as fresh with the explanation, so the check does not manufacture false staleness.

Deleted evidence is its own state: if a file existed at an earlier commit on the same branch and
is gone at the commit under verification, that is a finding naming the paths and the deleting
commit, not ``not_applicable``. Deletion must not be rewarded with a silent pass.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.utils.git import head_sha, run_git

EVIDENCE_ROOT = "verify-evidence"
CHECK_NAME = "committed evidence freshness"
CANONICAL_STAMP = "# HEAD={sha}"
MANIFEST_NAME = "manifest.json"
CAPTURE_NAME = "capture.sh"
RECOVERY_TRAILER = "Orchestrator-Recovery:"
HAND_FIX_TRAILER = "Operator-Hand-Fix:"

STATUS_PASS = "pass"  # noqa: S105 - a check status, not a credential
STATUS_STALE = "stale"
STATUS_NOT_APPLICABLE = "not_applicable"

_HEX = r"[0-9a-fA-F]{7,40}"
_STAMP_PATTERNS = (
    re.compile(rf"^\s*#?\s*HEAD\s*=\s*(?P<sha>{_HEX})\b"),
    re.compile(rf"^\s*#?\s*HEAD\s+(?P<sha>{_HEX})\b"),
    re.compile(rf"(?i)\bHEAD\s+(?P<sha>{_HEX})\b"),
    re.compile(rf"(?i)^\s*==\s*commit under test:.*?@\s*(?P<sha>{_HEX})\s*=="),
)
_STAMP_LOOKING = re.compile(r"(?i)\bHEAD\b|commit under test")


class EvidenceBundleError(ValueError):
    """The bundle cannot be regenerated (missing generator, failed capture, no HEAD)."""


@dataclass(frozen=True)
class EvidenceStamp:
    sha: str
    line: str
    line_number: int
    canonical: bool = False


@dataclass
class FreshnessResult:
    status: str
    evidence: str = ""
    notes: str = ""
    compared_commit: str | None = None
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

    @property
    def status_for_check(self) -> str:
        return self.status


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
        }


def evidence_dir(root: Path | str, label: str) -> Path:
    return Path(root) / EVIDENCE_ROOT / normalize_label(label)


def normalize_label(label: str) -> str:
    label = str(label).strip()
    if label.isdigit():
        return f"T-{label}"
    return label


def _resolve_commit(repo: Path, ref: str | None) -> str | None:
    if not ref:
        return None
    r = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo, check=False)
    return r.stdout.strip() if r.code == 0 and r.stdout.strip() else None


def _short(repo: Path, sha: str) -> str:
    r = run_git(["rev-parse", "--short", sha], repo, check=False)
    return r.stdout.strip() if r.code == 0 and r.stdout.strip() else sha[:7]


def parse_stamps(text: str) -> tuple[list[EvidenceStamp], list[str]]:
    """All HEAD stamps in ``text`` plus stamp-shaped lines the parser could not read.

    The canonical form is ``# HEAD=<full sha>``. Everything else is a legacy shape accepted
    forever so old evidence remains checkable without a mass rewrite.
    """
    stamps: list[EvidenceStamp] = []
    unparsed: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        found = None
        for pattern in _STAMP_PATTERNS:
            match = pattern.search(line)
            if match:
                found = match.group("sha")
                break
        if found:
            canonical = bool(re.fullmatch(rf"\s*#\s*HEAD={_HEX}\s*", line))
            stamps.append(EvidenceStamp(found, line, number, canonical=canonical))
            continue
        stripped = line.lstrip()
        if _STAMP_LOOKING.search(line) and (
            stripped.startswith("#") or stripped.startswith("HEAD") or stripped.startswith("==")
        ):
            unparsed.append(line)
    return stamps, unparsed


def _is_stamp_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in _STAMP_PATTERNS)


def _git_text(repo: Path, args: list[str]) -> str:
    result = run_git(args, repo, check=False)
    return result.stdout if result.code == 0 else ""


def _commits_between(repo: Path, older: str, newer: str) -> list[str]:
    out = _git_text(repo, ["rev-list", "--reverse", f"{older}..{newer}"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return (
        run_git(["merge-base", "--is-ancestor", older, newer], repo, check=False).code == 0
    )


def _commit_is_exempt(repo: Path, sha: str) -> str | None:
    """Why this commit may move HEAD without making evidence stale, or None."""
    body = _git_text(repo, ["show", "-s", "--format=%B", sha])
    if RECOVERY_TRAILER in body:
        return "harness recovery commit"
    if HAND_FIX_TRAILER in body:
        return "operator hand-fix commit"
    files = [
        line.strip()
        for line in _git_text(
            repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha]
        ).splitlines()
        if line.strip()
    ]
    if files and all(path.startswith(f"{EVIDENCE_ROOT}/") for path in files):
        return "evidence-only commit"
    if not files:
        return "empty commit"
    return None


def _stamp_verdict(repo: Path, stamp: EvidenceStamp, target: str) -> tuple[bool, str]:
    full = _resolve_commit(repo, stamp.sha)
    if full is None:
        return False, f"stamp {stamp.sha} does not resolve to a commit"
    if full == target:
        return True, "matches the commit under verification"
    if not _is_ancestor(repo, full, target):
        return False, (
            f"stamp {_short(repo, full)} is not an ancestor of the commit under verification "
            f"{_short(repo, target)}"
        )
    commits = _commits_between(repo, full, target)
    exempt = [(sha, _commit_is_exempt(repo, sha)) for sha in commits]
    if all(reason for _sha, reason in exempt):
        labels = ", ".join(f"{_short(repo, sha)} ({reason})" for sha, reason in exempt)
        return True, f"differs only by exempt commit(s): {labels}"
    blocking = ", ".join(_short(repo, sha) for sha, reason in exempt if not reason)
    return False, (
        f"stamp {_short(repo, full)} predates non-evidence commit(s) {blocking} on the way to "
        f"{_short(repo, target)}"
    )


def _manifest_stamps(text: str) -> tuple[list[EvidenceStamp], list[str]]:
    try:
        data = json.loads(text)
    except ValueError:
        return [], []
    if not isinstance(data, dict):
        return [], []
    stamp = data.get("stamp")
    if isinstance(stamp, str):
        return parse_stamps(stamp)
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


def _evidence_paths(repo: Path, commit: str, directory: str) -> list[str]:
    out = _git_text(repo, ["ls-tree", "-r", "--name-only", commit, "--", directory])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _deleted_evidence(
    repo: Path, commit: str, directory: str, base_branch: str | None, current: set[str]
) -> tuple[list[str], dict[str, str]]:
    """Paths under ``directory`` deleted between the branch base and ``commit``, still gone."""
    spec = f"{base_branch}..{commit}" if base_branch else commit
    if base_branch:
        ahead = _git_text(repo, ["rev-list", "--count", f"{base_branch}..{commit}"]).strip()
        if ahead == "0":
            # The commit is on the base branch itself (or the branch never moved): search its
            # own history rather than an empty range. In the normal task-branch case the range
            # is what keeps unrelated base-branch deletions out of this task's finding.
            spec = commit
    out = _git_text(
        repo,
        [
            "log",
            "--format=commit %H",
            "--name-status",
            "--diff-filter=D",
            spec,
            "--",
            directory,
        ],
    )
    deleted: dict[str, str] = {}
    deleting_commit = ""
    for line in out.splitlines():
        if line.startswith("commit "):
            deleting_commit = line.split(None, 1)[1].strip()
            continue
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].startswith("D"):
            continue
        path = parts[1].strip()
        # ``git log`` is newest first: the first deletion we see is the one that removed it.
        deleted.setdefault(path, deleting_commit)
    gone = {path: sha for path, sha in deleted.items() if path not in current}
    return sorted(gone), gone


def check_freshness(
    repo: Path | str | None,
    commit: str | None,
    label: str,
    base_branch: str | None = None,
    *,
    directory: str | None = None,
) -> FreshnessResult:
    """Compare every committed evidence stamp against ``commit`` (the run's commit_sha)."""
    if repo is None:
        return FreshnessResult(STATUS_NOT_APPLICABLE, "no worktree to inspect")
    root = Path(repo)
    target = _resolve_commit(root, commit)
    if target is None:
        return FreshnessResult(
            STATUS_NOT_APPLICABLE,
            f"no resolvable commit under verification ({commit or 'none'})",
        )
    rel_dir = directory or f"{EVIDENCE_ROOT}/{normalize_label(label)}"
    target_short = _short(root, target)
    paths = _evidence_paths(root, target, rel_dir)
    stale_paths: list[str] = []
    stale_reasons: dict[str, str] = {}
    unparsed_paths: list[str] = []
    fresh_with_explanation: dict[str, str] = {}
    stamped_files = 0

    for path in paths:
        show = run_git(["show", f"{target}:{path}"], root, check=False)
        if show.code != 0:
            continue
        text = show.stdout
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
                why
                for ok, why in verdicts
                if ok and why != "matches the commit under verification"
            ]
            if explanations:
                fresh_with_explanation[path] = explanations[0]
            continue
        stale_paths.append(path)
        stale_reasons[path] = "; ".join(why for _ok, why in verdicts)

    deleted_paths, deleted_by = _deleted_evidence(
        root, target, rel_dir, base_branch, set(paths)
    )

    if stale_paths or deleted_paths:
        parts = [
            f"{len(stale_paths)} stale of {stamped_files} stamped evidence file(s) against "
            f"{target_short} ({target})"
        ]
        if deleted_paths:
            parts.append(f"{len(deleted_paths)} deleted evidence file(s)")
        notes = [f"{path}: {reason}" for path, reason in stale_reasons.items()]
        notes += [
            f"{path}: deleted by {deleted_by[path]} and absent at {target_short}"
            for path in deleted_paths
        ]
        if fresh_with_explanation:
            notes += [
                f"{path}: {reason}" for path, reason in fresh_with_explanation.items()
            ]
        return FreshnessResult(
            STATUS_STALE,
            evidence="; ".join(parts),
            notes="\n".join(notes),
            compared_commit=target,
            stamped_files=stamped_files,
            stale_paths=sorted(stale_paths),
            stale_reasons=stale_reasons,
            deleted_paths=deleted_paths,
            deleted_by=deleted_by,
            unparsed_paths=unparsed_paths,
            fresh_with_explanation=fresh_with_explanation,
        )

    if stamped_files == 0:
        reason = (
            f"no stamped evidence under {rel_dir} at {target_short}"
            if paths
            else f"no committed evidence under {rel_dir} at {target_short}"
        )
        return FreshnessResult(
            STATUS_NOT_APPLICABLE,
            evidence=reason,
            compared_commit=target,
            stamped_files=0,
        )

    notes = [
        f"{path}: {reason}" for path, reason in fresh_with_explanation.items()
    ]
    return FreshnessResult(
        STATUS_PASS,
        evidence=f"{stamped_files} stamped evidence file(s) match {target_short} ({target})",
        notes="\n".join(notes),
        compared_commit=target,
        stamped_files=stamped_files,
        fresh_with_explanation=fresh_with_explanation,
    )


def review_finding(result: FreshnessResult) -> dict[str, str]:
    """The harness-detected finding body for the Architect payload (rendered elsewhere)."""
    paths = ", ".join(result.stale_paths + result.deleted_paths)
    return {
        "file_or_area": paths or "committed evidence",
        "issue": (
            "harness-detected evidence freshness: "
            f"{result.stale_count} stale and {len(result.deleted_paths)} deleted committed "
            f"evidence file(s) against the commit under verification. {result.evidence}. "
            "The Worker can refresh the bundle with `orchestrator task evidence <id>`. "
            f"Details: {result.notes.replace(chr(10), ' | ')}"
        ),
        "violates": "T-15 DoD 1 (committed evidence must match the commit under review)",
    }


def canonicalize_stamps(path: Path, head: str) -> bool:
    """Rewrite every stamp in one file to the single canonical line. Returns True if changed."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    stamps, _unparsed = parse_stamps(text)
    if not stamps:
        return False
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inserted = False
    for line in lines:
        if _is_stamp_line(line):
            if not inserted:
                newline = "\n" if line.endswith("\n") else ""
                out.append(CANONICAL_STAMP.format(sha=head) + newline)
                inserted = True
            continue
        out.append(line)
    if not inserted:
        return False
    new_text = "".join(out)
    if new_text == text:
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
    versions = {
        "python": platform.python_version(),
        "git": git.stdout.strip() or "unavailable",
    }
    try:
        import pytest  # noqa: PLC0415 - optional, only for the manifest

        versions["pytest"] = pytest.__version__
    except Exception:  # pragma: no cover - pytest is present in the gated environment
        versions["pytest"] = "unavailable"
    return versions


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _manifest_payload(
    root: Path,
    directory: Path,
    label: str,
    head: str,
    command: str,
    previous: dict | None,
) -> dict:
    files = _hash_tree(directory)
    produced = [
        {"path": path, "sha256": digest}
        for path, digest in files.items()
        if path != MANIFEST_NAME
    ]
    payload = {
        "label": normalize_label(label),
        "head": head,
        "head_short": _short(root, head),
        "stamp": CANONICAL_STAMP.format(sha=head),
        "generated_at": _utcnow(),
        "command": command,
        "tool_versions": _tool_versions(root),
        "files": produced,
    }
    # A second run at the same HEAD with the same produced files is a no-op, timestamp included:
    # otherwise the regeneration command would dirty its own manifest every time and DoD 5(d)
    # (a clean second run) could never hold.
    comparable = {k: v for k, v in payload.items() if k != "generated_at"}
    if previous:
        old_comparable = {k: v for k, v in previous.items() if k != "generated_at"}
        if old_comparable == comparable and isinstance(previous.get("generated_at"), str):
            payload["generated_at"] = previous["generated_at"]
    return payload


def regenerate_bundle(
    root: Path | str,
    label: str,
    *,
    command: str | None = None,
    directory: str | None = None,
    capture: bool = True,
    timeout: int = 900,
) -> BundleResult:
    """Regenerate one evidence directory in a single invocation.

    ``capture.sh`` is the committed task-specific generator. The command runs it, canonicalises
    every stamp it wrote, and writes ``manifest.json`` with the HEAD, timestamp, command line,
    tool versions and produced files. Files whose bytes did not change are not rewritten.
    """
    root = Path(root)
    if not (root / ".git").exists():
        raise EvidenceBundleError(f"{root} is not a git worktree")
    directory = Path(directory) if directory else evidence_dir(root, label)
    if not directory.is_relative_to(root):
        raise EvidenceBundleError(f"evidence directory {directory} is outside {root}")
    rel_dir = str(directory.relative_to(root))
    label = normalize_label(label)
    before = _hash_tree(directory)
    invoked = command
    if capture:
        if command:
            argv = shlex.split(command)
        else:
            script = directory / CAPTURE_NAME
            if not script.is_file():
                raise EvidenceBundleError(
                    f"no committed generator at {script.relative_to(root)}; commit one "
                    "(a capture.sh) or pass --command"
                )
            argv = [str(script)] if script.stat().st_mode & 0o111 else ["bash", str(script)]
            invoked = f"{script.relative_to(root)}"
        try:
            proc = subprocess.run(
                argv,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EvidenceBundleError(f"evidence generator timed out after {timeout}s") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
            raise EvidenceBundleError(
                f"evidence generator exited {proc.returncode}: " + " | ".join(tail)
            )
    head = head_sha(root)
    if not head:
        raise EvidenceBundleError("cannot resolve HEAD in the worktree")
    if not directory.is_dir():
        raise EvidenceBundleError(f"generator produced no directory at {rel_dir}")

    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != MANIFEST_NAME:
            canonicalize_stamps(path, head)

    manifest_path = directory / MANIFEST_NAME
    previous = None
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
    command_line = invoked or (f"{CAPTURE_NAME}" if capture else "stamp refresh only")
    payload = _manifest_payload(root, directory, label, head, command_line, previous)
    new_text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != new_text:
        manifest_path.write_text(new_text, encoding="utf-8")

    after = _hash_tree(directory)
    rewritten = sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )
    return BundleResult(
        label=label,
        evidence_dir=rel_dir,
        head=head,
        head_short=_short(root, head),
        command=command_line,
        generated_at=payload["generated_at"],
        files=sorted(path for path in after if path != MANIFEST_NAME),
        rewritten=rewritten,
        manifest=str(manifest_path.relative_to(root)),
    )


__all__ = [
    "CHECK_NAME",
    "EVIDENCE_ROOT",
    "EvidenceBundleError",
    "EvidenceStamp",
    "FreshnessResult",
    "BundleResult",
    "canonicalize_stamps",
    "check_freshness",
    "evidence_dir",
    "normalize_label",
    "parse_stamps",
    "regenerate_bundle",
    "review_finding",
]

