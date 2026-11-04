"""Mechanical checks around a review: absent evidence, and whether the supplied diff is whole.

Two independent jobs, both required by T-11:

  - **absence claims** (DoD 2/3): when the reviewer says a file, test name or line is missing,
    check it against the task worktree before the verdict can cost another round. A refuted
    claim is recorded and cannot drive a requeue.
  - **diff completeness** (DoD 7): compare the changed-file list the harness computes from git
    with the patch that was actually shipped to the reviewer, and surface files a repository
    attribute made invisible (`-diff`, `export-ignore`). A Worker must not be able to opt out of
    review by editing `.gitattributes`.

Everything here is offline: local git commands and local files only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.services.prompt_builder import split_diff_files
from orchestrator.utils.git import run_git

# A finding is only an absence claim when it uses absence language; otherwise it is a normal
# review finding and no mechanical check applies.
_ABSENCE = re.compile(
    r"(?i)\b(missing|absent|not\s+present|does\s+not\s+exist|no\s+such|not\s+supplied|"
    r"not\s+included|not\s+found|omitted|truncated|not\s+visible|not\s+archived|unavailable|"
    r"isn'?t\s+present|never\s+provided)\b"
)
_FILE = re.compile(
    r"(?<![\w./-])((?:\.{0,2}/)?(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|md|json|txt|ts|tsx|js|jsx|html|css|toml|ini|yaml|yml|sh|log|diff|patch|cfg|env))"
    r"(?![\w/])"
)
_TEST = re.compile(r"\b(test_[A-Za-z0-9_]+)\b")
_LINE = re.compile(r":(?P<line>\d{1,7})\b")

EVIDENCE_OR_SOURCE_PREFIXES = (
    "src/",
    "tests/",
    "scripts/",
    "verify-evidence/",
    "evidence/",
)


@dataclass
class AbsenceCheck:
    finding_index: int
    claim: str
    status: str  # confirmed | refuted | unchecked
    detail: str
    source: str = "finding"  # finding | missing_artifact

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding_index": self.finding_index,
            "claim": self.claim,
            "status": self.status,
            "detail": self.detail,
            "source": self.source,
        }


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


def _check_claim_text(
    cwd: Path | None, text: str, *, source: str = "finding", index: int = -1
) -> list[AbsenceCheck]:
    """Extract concrete file/test/line claims from one absence finding and check each one."""
    if not _ABSENCE.search(text or ""):
        return []
    if cwd is None or not cwd.is_dir():
        return [
            AbsenceCheck(
                finding_index=index,
                claim=text.strip(),
                status="unchecked",
                detail="no task worktree was available for a mechanical existence check",
                source=source,
            )
        ]
    claims: list[AbsenceCheck] = []

    for match in _FILE.finditer(text):
        raw = match.group(1).strip().rstrip(".,;:)}]")
        rel = raw.removeprefix("./")
        target = (cwd / rel).resolve()
        try:
            target.relative_to(cwd.resolve())
        except ValueError:
            claims.append(
                AbsenceCheck(
                    finding_index=index,
                    claim=raw,
                    status="unchecked",
                    detail="path is outside the task worktree; not checked",
                    source=source,
                )
            )
            continue
        if target.is_file():
            line_match = _LINE.search(text[match.end() : match.end() + 12])
            if line_match:
                wanted = int(line_match.group("line"))
                count = _line_count(target)
                if count >= wanted:
                    claims.append(
                        AbsenceCheck(
                            finding_index=index,
                            claim=f"{raw}:{wanted}",
                            status="refuted",
                            detail=f"file exists with {count} lines in the worktree",
                            source=source,
                        )
                    )
                else:
                    claims.append(
                        AbsenceCheck(
                            finding_index=index,
                            claim=f"{raw}:{wanted}",
                            status="confirmed",
                            detail=f"file exists but has only {count} lines",
                            source=source,
                        )
                    )
            else:
                claims.append(
                    AbsenceCheck(
                        finding_index=index,
                        claim=raw,
                        status="refuted",
                        detail="file exists in the task worktree",
                        source=source,
                    )
                )
        elif target.exists():
            claims.append(
                AbsenceCheck(
                    finding_index=index,
                    claim=raw,
                    status="refuted",
                    detail="path exists in the task worktree (not a regular file)",
                    source=source,
                )
            )
        else:
            claims.append(
                AbsenceCheck(
                    finding_index=index,
                    claim=raw,
                    status="confirmed",
                    detail="path is absent from the task worktree",
                    source=source,
                )
            )

    for match in _TEST.finditer(text):
        name = match.group(1)
        found = _find_test_name(cwd, name)
        if found:
            claims.append(
                AbsenceCheck(
                    finding_index=index,
                    claim=name,
                    status="refuted",
                    detail=f"test function name found in {found}",
                    source=source,
                )
            )
        else:
            claims.append(
                AbsenceCheck(
                    finding_index=index,
                    claim=name,
                    status="unchecked",
                    detail="test name was not found by the local source scan (may live elsewhere)",
                    source=source,
                )
            )

    if not claims:
        claims.append(
            AbsenceCheck(
                finding_index=index,
                claim=text.strip(),
                status="unchecked",
                detail="no concrete file, test name or line could be extracted from the claim",
                source=source,
            )
        )
    return claims


def _aggregate(checks: list[AbsenceCheck]) -> str:
    if not checks:
        return "unchecked"
    statuses = {c.status for c in checks}
    if "confirmed" in statuses:
        return "confirmed"
    if statuses == {"refuted"}:
        return "refuted"
    return "unchecked"


def check_findings(findings: list[Any], cwd: Path | None) -> list[AbsenceCheck]:
    """Check every finding that asserts absence. Non-absence findings are left alone."""
    out: list[AbsenceCheck] = []
    for i, finding in enumerate(findings):
        if isinstance(finding, dict):
            text = " ".join(str(finding.get(k, "")) for k in ("file_or_area", "issue", "violates"))
        else:
            text = " ".join(
                str(getattr(finding, k, "")) for k in ("file_or_area", "issue", "violates")
            )
        out.extend(_check_claim_text(cwd, text, index=i))
    return out


def check_missing_artifacts(items: list[str], cwd: Path | None) -> list[AbsenceCheck]:
    """Reviewer-supplied `missing_artifacts` entries are claims too; check them the same way."""
    out: list[AbsenceCheck] = []
    for i, item in enumerate(items):
        out.extend(
            _check_claim_text(cwd, f"{item} is missing", source="missing_artifact", index=i)
        )
    return out


def finding_status(checks: list[AbsenceCheck], index: int) -> str:
    return _aggregate([c for c in checks if c.finding_index == index and c.source == "finding"])


def render_absence_checks(checks: list[AbsenceCheck]) -> str:
    if not checks:
        return "No reviewer absence claim needed a mechanical check."
    lines = [
        "| claim | source | status | evidence |",
        "|---|---|---|---|",
    ]
    for c in checks:
        claim = c.claim.replace("|", "\\|")
        detail = c.detail.replace("|", "\\|")
        lines.append(f"| {claim} | {c.source} | **{c.status}** | {detail} |")
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
