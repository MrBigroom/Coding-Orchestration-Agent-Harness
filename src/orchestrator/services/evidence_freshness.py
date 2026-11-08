"""Committed evidence freshness and the one-command evidence bundle (T-15).

A committed evidence file is only trustworthy while the HEAD it was measured at is the commit
under review. Before T-15 nothing in the gate compared the two, so a round could harden its
assertions and still be reviewed against an older copy of its own tests (T-4 round 5: 66 of 68
files carried the round-4 tip). This module is that comparison, plus the single generator that
makes keeping a bundle honest cheap.

Four rules shape it, each of which was a measured defect in an earlier round:

* **The stamp is a legacy vocabulary, not one format.** Nine shapes exist in the repository
  (``# HEAD=<sha>``, ``# HEAD <sha>``, ``HEAD: <sha>``, pipe-delimited, prose in a comment,
  ``== commit under test: ... ==``, short SHAs, ...). All are parsed; a short SHA is
  prefix-resolved against the commit under verification, never string-compared. A stamp-shaped
  line the parser cannot read is reported stale, never fresh.
* **A stamp is header metadata.** Only the first :data:`STAMP_WINDOW` lines of a file are
  scanned, and a bare ``HEAD <sha>`` is only recognized at the start of a line or inside a
  leading ``#`` comment. Captured test output that merely *mentions* a stamp (parametrized test
  ids embed the shapes) can therefore neither make a bundle look stale nor be rewritten.
* **HEAD moves for reasons the Worker does not control.** A different SHA is not automatically
  stale: harness recovery commits and operator hand-fixes are identified by their git
  *trailers* (``git show --format=%(trailers)``, never a substring search of the body), and an
  evidence-only commit is one that touches nothing but ``verify-evidence/`` and **not** the
  ``capture.sh`` generator. A stamp that differs only by those is fresh with the explanation.
* **Deletion is a state of its own.** A file committed earlier on the branch and gone at the
  reviewed commit is a finding naming the paths and the deleting commit, not ``not_applicable``.
  Deleting evidence must not be rewarded with a silent pass.

The generator cannot lie by construction: it refuses a dirty tracked worktree (so the HEAD it
stamps is the tree it measured), runs the committed ``capture.sh`` and nothing else, canonicalises
**only the files the capture pass rewrote**, and writes ``manifest.json``. Files the pass did not
touch keep their old stamps and are reported stale by the check — there is no stamp-only mode,
because re-stamping old content at a new HEAD is exactly the failure this modules exists to catch.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.utils.git import head_sha, run_git

EVIDENCE_ROOT = "verify-evidence"
CHECK_NAME = "committed evidence freshness"
CANONICAL_STAMP = "# HEAD={sha}"
MANIFEST_NAME = "manifest.json"
CAPTURE_NAME = "capture.sh"
SELF_CHECK_NAME = "freshness_selfcheck.txt"
#: Number of leading lines that may carry a stamp. Stamps are header metadata; capturing a
#: transcript whose body happens to contain a stamp-shaped line must not rewrite or stale a file.
STAMP_WINDOW = 20

#: Git trailer keys, without their colon. Trailer parsing is git's own (`%(trailers)`), so a
#: commit body that *mentions* one of these words in prose is not exempt.
RECOVERY_TRAILER = "Orchestrator-Recovery"
HAND_FIX_TRAILER = "Operator-Hand-Fix"

#: Internal states. The *check record* status is a frozen vocabulary (pass/fail/skipped/
#: not_applicable, docs/frozen-contracts.md); :func:`check_status` maps onto it.
STATE_PASS = "pass"  # noqa: S105 - a check state, not a credential
STATE_STALE = "stale"
STATE_NOT_APPLICABLE = "not_applicable"

_HEX = r"[0-9a-fA-F]{7,40}"
_NOT_HEX = r"(?![0-9a-fA-F])"
# Order matters: the assignment form first, so `# HEAD=<sha> (short) branch=...` is one stamp.
_STAMP_PATTERNS = (
    re.compile(rf"^\s*#?\s*HEAD\s*=\s*(?P<sha>{_HEX}){_NOT_HEX}"),
    re.compile(rf"^\s*#?\s*HEAD\s*:\s*(?P<sha>{_HEX}){_NOT_HEX}"),
    re.compile(rf"^\s*#?\s*HEAD\s+(?P<sha>{_HEX}){_NOT_HEX}"),
    # Prose is only accepted when the line is a comment: `# T-18 full suite at HEAD 46bb10b`.
    re.compile(rf"^\s*#.*?\bHEAD\s+(?P<sha>{_HEX}){_NOT_HEX}"),
    re.compile(rf"^\s*==\s*commit under test:.*?@\s*(?P<sha>{_HEX})\s*==", re.IGNORECASE),
)
#: A line that *looks* like a stamp this parser failed on. Deliberately narrow: the assignment
#: form and the T-6 commit-under-test marker. `HEAD tree  : <sha>` is a key/value table row, not a
#: stamp, and must not be reported as an unparsable one.
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
        }


def check_status(result: FreshnessResult, strict: bool = False) -> str:
    """Map the freshness state onto the frozen verification check-status vocabulary.

    ``stale`` is reported, not failed: the check-record status Literal in
    ``docs/frozen-contracts.md`` is frozen at ``pass | fail | skipped | not_applicable`` and this
    phase has no contract-change authorization, so staleness rides in the row's ``evidence``
    string and in the ``evidence_freshness_stale`` build-log payload; ``skipped`` is the
    "compared but not certified" bucket. ``fail`` is reserved for the operator's opt-in strict
    rule (``ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT=true``).
    """
    if result.state == STATE_PASS:
        return "pass"  # noqa: S105 - frozen status value
    if result.state == STATE_STALE:
        return "fail" if strict else "skipped"
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
    """All HEAD stamps in the first ``window`` lines, plus stamp-shaped lines that did not parse."""
    stamps: list[EvidenceStamp] = []
    unparsed: list[str] = []
    for number, line in enumerate(text.splitlines()[:window], 1):
        found = None
        for pattern in _STAMP_PATTERNS:
            match = pattern.search(line)
            if match:
                found = match.group("sha")
                break
        if found:
            canonical = bool(re.fullmatch(rf"\s*#\s*HEAD={_HEX}\s*", line))
            stamps.append(EvidenceStamp(found, line, number, canonical=canonical))
        elif _LOOKS_LIKE_STAMP.match(line):
            unparsed.append(line)
    return stamps, unparsed


def _is_stamp_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in _STAMP_PATTERNS)


def _manifest_stamps(text: str) -> tuple[list[EvidenceStamp], list[str]]:
    """``manifest.json`` carries its HEAD as a field, not in the header window."""
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
    """The commit's trailer block, as git itself parses it (empty when there is none)."""
    out = _git_text(repo, ["show", "-s", "--format=%(trailers)", sha])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _commit_is_exempt(repo: Path, sha: str) -> str | None:
    """Why this commit may move HEAD without making evidence stale, or None."""
    keys = {line.split(":", 1)[0].strip() for line in _trailers(repo, sha)}
    if RECOVERY_TRAILER in keys:
        return "harness recovery commit"
    if HAND_FIX_TRAILER in keys:
        return "operator hand-fix commit"
    files = [
        line.strip()
        for line in _git_text(
            repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha]
        ).splitlines()
        if line.strip()
    ]
    if not files:
        return "empty commit"
    evidence_only = all(path.startswith(f"{EVIDENCE_ROOT}/") for path in files)
    # The generator is code, not evidence: editing capture.sh changes what every number in the
    # bundle means, so a commit that touches it is not an evidence-only commit.
    touches_generator = any(Path(path).name == CAPTURE_NAME for path in files)
    if evidence_only and not touches_generator:
        return "evidence-only commit"
    return None


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return run_git(["merge-base", "--is-ancestor", older, newer], repo, check=False).code == 0


def _commits_between(repo: Path, older: str, newer: str) -> list[str]:
    out = _git_text(repo, ["rev-list", "--reverse", f"{older}..{newer}"])
    return [line.strip() for line in out.splitlines() if line.strip()]


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


def _committed_files(repo: Path, commit: str, directory: str) -> list[str]:
    out = _git_text(repo, ["ls-tree", "-r", "--name-only", commit, "--", directory])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _worktree_files(root: Path, directory: str) -> list[str]:
    base = root / directory
    if not base.is_dir():
        return []
    return sorted(
        path.relative_to(root).as_posix() for path in base.rglob("*") if path.is_file()
    )


def _deleted_evidence(
    repo: Path, commit: str, directory: str, base_branch: str | None, current: set[str]
) -> tuple[list[str], dict[str, str]]:
    """Paths under ``directory`` deleted between the branch base and ``commit``, still gone."""
    spec = f"{base_branch}..{commit}" if base_branch else commit
    if base_branch and _git_text(repo, ["rev-list", "--count", spec]).strip() == "0":
        # The commit is on the base branch itself (or the branch never moved): search its own
        # history rather than an empty range, which would hide the deletion entirely.
        spec = commit
    out = _git_text(
        repo,
        ["log", "--format=commit %H", "--name-status", "--diff-filter=D", spec, "--", directory],
    )
    deleted: dict[str, str] = {}
    deleting_commit = ""
    for line in out.splitlines():
        if line.startswith("commit "):
            deleting_commit = line.split(None, 1)[1].strip()
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].startswith("D"):
            # `git log` is newest first: the first deletion seen is the one that removed it.
            deleted.setdefault(parts[1].strip(), deleting_commit)
    gone = {path: sha for path, sha in deleted.items() if path not in current}
    return sorted(gone), gone


def _read_evidence(repo: Path, commit: str, path: str, from_worktree: bool) -> str | None:
    if not from_worktree:
        show = run_git(["show", f"{commit}:{path}"], repo, check=False)
        return show.stdout if show.code == 0 else None
    target = repo / path
    try:
        return target.read_text(encoding="utf-8")
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

    ``from_worktree=True`` reads the files on disk instead of the commit's tree; the generator
    uses it to certify the bundle it just wrote, before that bundle is committed.
    """
    if repo is None:
        return FreshnessResult(STATE_NOT_APPLICABLE, "no worktree to inspect")
    root = Path(repo)
    target = _resolve_commit(root, commit)
    if target is None:
        return FreshnessResult(
            STATE_NOT_APPLICABLE,
            f"no resolvable commit under verification ({commit or 'none'})",
        )
    rel_dir = directory or f"{EVIDENCE_ROOT}/{normalize_label(label)}"
    target_short = _short(root, target)
    paths = (
        _worktree_files(root, rel_dir)
        if from_worktree
        else _committed_files(root, target, rel_dir)
    )
    stale_paths: list[str] = []
    stale_reasons: dict[str, str] = {}
    unparsed_paths: list[str] = []
    fresh_with_explanation: dict[str, str] = {}
    stamped_files = 0

    for path in paths:
        if ignore and Path(path).name in ignore:
            continue
        text = _read_evidence(root, target, path, from_worktree)
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
                fresh_with_explanation[path] = explanations[0]
            continue
        stale_paths.append(path)
        stale_reasons[path] = "; ".join(why for _ok, why in verdicts)

    deleted_paths, deleted_by = _deleted_evidence(root, target, rel_dir, base_branch, set(paths))

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
        notes += [f"{path}: {reason}" for path, reason in fresh_with_explanation.items()]
        return FreshnessResult(
            STATE_STALE,
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
            STATE_NOT_APPLICABLE, evidence=reason, compared_commit=target, stamped_files=0
        )

    # Say which sense of "fresh" applies: an exact match, or fresh only through exempt commits.
    # The earlier round's self-check said "match <sha>" while its notes named a different commit.
    exempt_note = (
        f"; {len(fresh_with_explanation)} fresh only via exempt commit(s)"
        if fresh_with_explanation
        else ""
    )
    return FreshnessResult(
        STATE_PASS,
        evidence=(
            f"{stamped_files} stamped evidence file(s) fresh at {target_short} ({target})"
            f"{exempt_note}"
        ),
        notes="\n".join(f"{path}: {reason}" for path, reason in fresh_with_explanation.items()),
        compared_commit=target,
        stamped_files=stamped_files,
        fresh_with_explanation=fresh_with_explanation,
    )


def review_finding(result: FreshnessResult) -> dict[str, str]:
    """The harness-detected finding body for the Architect payload."""
    paths = ", ".join(result.stale_paths + result.deleted_paths)
    return {
        "file_or_area": paths or "committed evidence",
        "issue": (
            "harness-detected evidence freshness: "
            f"{result.stale_count} stale and {len(result.deleted_paths)} deleted committed "
            f"evidence file(s) against the commit under verification. {result.evidence}. "
            "Refresh the bundle with one command (`orchestrator task evidence <id>`), which "
            f"regenerates every file it can from the current tip. Details: "
            f"{result.notes.replace(chr(10), ' | ')}"
        ),
        "violates": "T-15 DoD 1 (committed evidence must match the commit under review)",
    }


def canonicalize_stamps(path: Path, head: str) -> bool:
    """Rewrite the (header) stamps in one file to the canonical line. True if bytes changed."""
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
    for number, line in enumerate(lines, 1):
        if number <= STAMP_WINDOW and _is_stamp_line(line):
            if not inserted:
                out.append(CANONICAL_STAMP.format(sha=head) + ("\n" if line.endswith("\n") else ""))
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
    versions = {"python": platform.python_version(), "git": git.stdout.strip() or "unavailable"}
    try:
        import pytest  # noqa: PLC0415 - optional, only for the manifest

        versions["pytest"] = pytest.__version__
    except Exception:  # pragma: no cover - pytest is present in the gated environment
        versions["pytest"] = "unavailable"
    return versions


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _manifest_payload(
    root: Path, directory: Path, label: str, head: str, command: str, previous: dict | None
) -> dict:
    produced = [
        {"path": path, "sha256": digest}
        for path, digest in _hash_tree(directory).items()
        if path != MANIFEST_NAME
    ]
    payload = {
        "label": label,
        "head": head,
        "head_short": _short(root, head),
        "stamp": CANONICAL_STAMP.format(sha=head),
        "generated_at": _utcnow(),
        "command": command,
        "tool_versions": _tool_versions(root),
        "files": produced,
    }
    # A second run at the same HEAD with the same produced files is a byte-for-byte no-op,
    # timestamp included; otherwise the command would dirty its own manifest every time and
    # DoD 5(d) (a clean second run) could never hold.
    comparable = {k: v for k, v in payload.items() if k != "generated_at"}
    if previous:
        old = {k: v for k, v in previous.items() if k != "generated_at"}
        if old == comparable and isinstance(previous.get("generated_at"), str):
            payload["generated_at"] = previous["generated_at"]
    return payload


def _dirty_tracked(root: Path, rel_dir: str) -> list[str]:
    out = _git_text(root, ["status", "--porcelain", "-uno"])
    dirty = []
    for line in out.splitlines():
        path = line[3:].strip()
        if not path or path.startswith(f"{rel_dir}/"):
            continue
        dirty.append(path)
    return dirty


def _write_self_check(root: Path, directory: Path, label: str, head: str) -> Path:
    """Record the generator's own worktree-basis check next to the bundle it just wrote."""
    # The manifest and this file are generator bookkeeping: the manifest is written after this
    # pass and this file is written by it, so counting either would make a no-op run rewrite
    # itself. The gate still checks the manifest's own stamp.
    result = check_freshness(
        root, head, label, None, from_worktree=True,
        ignore=frozenset({MANIFEST_NAME, SELF_CHECK_NAME}),
    )
    lines = [
        "# Evidence self-check written by the one bundle pass (not hand-edited, no HEAD stamp:",
        "# it certifies the files around it, and the committed view is reported by the gate).",
        f"# command: orchestrator task evidence {label}",
        "# rewritten files, wall time and the command line are reported by that command itself;",
        "# this file records only what the check saw, so a no-op second run is byte-identical.",
        "basis=worktree",
        "scope=every evidence file except manifest.json and this file",
        f"compared_commit={result.compared_commit}",
        f"status={result.state}",
        f"stamped_files={result.stamped_files}",
        f"stale_count={result.stale_count}",
        f"deleted_count={len(result.deleted_paths)}",
        f"evidence={result.evidence or 'n/a'}",
        f"notes={result.notes or 'none'}",
        "committed_view=the stamps above name this HEAD; the next commit must be evidence-only, "
        "and the gate then reports pass with that explanation (see tests/unit/"
        "test_evidence_freshness.py::test_the_generated_bundle_is_fresh_once_committed).",
    ]
    path = directory / SELF_CHECK_NAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def regenerate_bundle(
    root: Path | str,
    label: str,
    *,
    command: str | None = None,
    directory: str | None = None,
    timeout: int = 900,
) -> BundleResult:
    """Regenerate one evidence directory in a single invocation.

    Runs the committed ``capture.sh`` (or ``command``), canonicalises **only the files that pass
    rewrote**, writes ``manifest.json`` and ``freshness_selfcheck.txt``. There is no stamp-only
    mode: a file the generator did not rewrite keeps its old stamp and is reported stale.
    """
    root = Path(root)
    if not (root / ".git").exists():
        raise EvidenceBundleError(f"{root} is not a git worktree")
    directory = Path(directory) if directory else evidence_dir(root, label)
    if not directory.is_relative_to(root):
        raise EvidenceBundleError(f"evidence directory {directory} is outside {root}")
    rel_dir = directory.relative_to(root).as_posix()
    label = normalize_label(label)
    dirty = _dirty_tracked(root, rel_dir)
    if dirty:
        raise EvidenceBundleError(
            "worktree has uncommitted tracked changes outside "
            f"{rel_dir} (e.g. {dirty[0]}); a stamp would name a HEAD whose tree was never "
            "measured. Commit the code this bundle certifies, then regenerate."
        )
    head = head_sha(root)
    if not head:
        raise EvidenceBundleError("cannot resolve HEAD in the worktree")
    before = _hash_tree(directory)
    started = time.monotonic()
    if command:
        argv, invoked = shlex.split(command), command
    else:
        script = directory / CAPTURE_NAME
        if not script.is_file():
            raise EvidenceBundleError(
                f"no committed generator at {rel_dir}/{CAPTURE_NAME}; commit one or pass --command"
            )
        argv = [str(script)]
        invoked = f"{rel_dir}/{CAPTURE_NAME}"
    try:
        proc = subprocess.run(
            argv, cwd=root, capture_output=True, text=True, timeout=timeout, check=False
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
    capture_hashes = _hash_tree(directory)
    for rel in sorted(capture_hashes):
        if Path(rel).name != MANIFEST_NAME and before.get(rel) != capture_hashes[rel]:
            canonicalize_stamps(directory / rel, head)
    canonical_hashes = _hash_tree(directory)
    changed = set(before) | set(canonical_hashes)
    duration = time.monotonic() - started
    _write_self_check(root, directory, label, head)

    manifest_path = directory / MANIFEST_NAME
    previous = None
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
    payload = _manifest_payload(root, directory, label, head, invoked, previous)
    new_text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != new_text:
        manifest_path.write_text(new_text, encoding="utf-8")

    after = _hash_tree(directory)
    changed |= set(after)
    rewritten = sorted(path for path in changed if before.get(path) != after.get(path))
    return BundleResult(
        label=label,
        evidence_dir=rel_dir,
        head=head,
        head_short=_short(root, head),
        command=invoked,
        generated_at=payload["generated_at"],
        files=sorted(path for path in after if path != MANIFEST_NAME),
        rewritten=rewritten,
        manifest=f"{rel_dir}/{MANIFEST_NAME}",
        duration_seconds=duration,
    )


__all__ = [
    "CAPTURE_NAME",
    "CHECK_NAME",
    "EVIDENCE_ROOT",
    "MANIFEST_NAME",
    "SELF_CHECK_NAME",
    "STAMP_WINDOW",
    "BundleResult",
    "EvidenceBundleError",
    "EvidenceStamp",
    "FreshnessResult",
    "canonicalize_stamps",
    "check_freshness",
    "check_status",
    "evidence_dir",
    "normalize_label",
    "parse_stamps",
    "regenerate_bundle",
    "review_finding",
]
