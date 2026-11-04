"""Architect gate review: build the review prompt from artifacts only, call the architect
profile, parse its verdict strictly, and persist review.md / review.json.

Parsing policy (fail closed):
  - JSON object present and valid -> use it; if a VERDICT line also exists it must agree;
  - otherwise a `VERDICT:` line + `FINDINGS:` list is parsed;
  - anything else -> CHANGES_REQUESTED with an "ambiguous review output" finding, raw kept.
Deterministically-detected missing artifacts are always findings; an APPROVED verdict with
missing artifacts is downgraded to CHANGES_REQUESTED.

T-11: the exact prompt sent to the reviewer is archived as `review_payload.md` with a truncation
manifest, harness-detected and model-asserted missing artifacts are kept in separate sections,
and an absence finding is checked against the task worktree before it can requeue a round.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlmodel import Session

from orchestrator.core.models import Project, Review, ReviewVerdict, Role, Run, Task
from orchestrator.core.schemas import ReviewFinding, ReviewResult
from orchestrator.providers.base import ProviderError
from orchestrator.services import (
    artifact_service,
    git_service,
    log_service,
    project_service,
    prompt_builder,
    provider_service,
    review_audit,
)
from orchestrator.services.prompt_builder import DynamicState
from orchestrator.utils import structured_log
from orchestrator.utils.json_output import JSONExtractionError, extract_json

_VERDICT = re.compile(r"(?im)^\s*VERDICT:\s*(APPROVED|CHANGES_REQUESTED|NEEDS_HUMAN)\s*$")
_FINDING = re.compile(r"(?m)^\s*(?:-|\d+[.)])\s+(.+)$")
_EXEC_CLAIM = re.compile(
    r"(?i)\bI\s+(?:have\s+)?(?:ran|run|re-ran|reran|executed|grepped|tested)\b"
)

AMBIGUOUS = ReviewFinding(
    file_or_area="review output",
    issue="review output was ambiguous or unparseable; raw output stored",
    violates="review.json contract",
)


def parse_review(text: str) -> tuple[ReviewResult, bool]:
    """Return (result, ambiguous)."""
    line = _VERDICT.search(text or "")
    line_verdict = line.group(1) if line else None
    try:
        data = extract_json(text)
        if isinstance(data, dict) and "verdict" in data:
            result = ReviewResult.model_validate(data)
            if line_verdict and line_verdict != result.verdict.value:
                return ReviewResult(
                    verdict=ReviewVerdict.CHANGES_REQUESTED,
                    findings=[AMBIGUOUS],
                    notes=["VERDICT line and JSON verdict disagree"],
                ), True
            return result, False
    except (JSONExtractionError, ValueError):
        pass
    if line_verdict:
        findings_part = text[line.end() :] if line else ""
        if "FINDINGS:" in findings_part:
            findings_part = findings_part.split("FINDINGS:", 1)[1]
        findings = []
        for m in _FINDING.finditer(findings_part):
            item = m.group(1).strip()
            if item.lower() in ("none", "none.", "n/a"):
                continue
            area, _, issue = item.partition(":")
            findings.append(
                ReviewFinding(
                    file_or_area=area.strip() if issue else "general", issue=(issue or item).strip()
                )
            )
        return ReviewResult(verdict=ReviewVerdict(line_verdict), findings=findings), False
    return ReviewResult(verdict=ReviewVerdict.CHANGES_REQUESTED, findings=[AMBIGUOUS]), True


def missing_artifacts(run_dir_files: set[str], handoff_synthesized: bool) -> list[str]:
    missing = []
    if "handoff.json" not in run_dir_files or handoff_synthesized:
        missing.append("worker handoff.json (missing or synthesized by orchestrator)")
    if "patch.diff" not in run_dir_files:
        missing.append("diff/patch")
    if "test.log" not in run_dir_files:
        missing.append("test log")
    if "verification.json" not in run_dir_files:
        missing.append("supervisor verification narrative")
    return missing


def recovery_section(session: Session, task: Task, run: Run) -> str:
    """The reviewer-payload section for harness-created recovery commits."""
    commits = project_service.recovery_commits_for(session, task, run)
    if not commits:
        return (
            "none — every commit on this branch was authored by a Worker\n"
            "A recovery commit is never by itself a reason to reject, and never a reason to "
            "approve either: it marks content whose author never checked it."
        )
    return (
        f"{len(commits)} commit(s) on this branch were created by the ORCHESTRATOR, not by the "
        "Worker. Their content was NEVER self-checked by the Worker: it was committed by the "
        "harness after a turn aborted (timeout/failure/cancel) with edits still uncommitted, and "
        "may contain undefined names, half-finished edits, scratch files or contract-file edits. "
        "Judge it as unreviewed third-party code and hold it to the same bar as Worker code.\n\n"
        + git_service.format_recovery_commit_list(commits)
    )


def harness_findings_section(
    session: Session,
    task: Task,
    run: Run,
    project: Project | None,
    diff_issues: list[review_audit.DiffIssue] | None = None,
) -> str:
    """Deterministic findings the harness computed; a story cannot contradict them."""
    cwd = Path(run.worktree_path) if run.worktree_path else None
    findings = project_service.contract_findings(task, project, cwd)
    commits = project_service.recovery_commits_for(session, task, run)
    lines = [
        f"recovery commits on this branch (harness-created, never self-checked): {len(commits)}",
        f"contract files watched: {', '.join(project_service.contract_files())}",
        f"contract files changed by this run: {', '.join(findings['changed']) or 'none'}",
        "authorized by an explicit grant in this task's stored Definition of Done / acceptance "
        f"criteria / frozen contracts: {', '.join(findings['authorized']) or 'none'}",
    ]
    if findings["unauthorized"]:
        lines.append(
            "UNAUTHORIZED contract file change(s), detected by the harness: "
            + ", ".join(findings["unauthorized"])
            + ". A phase may change a contract file only when its own DoD/acceptance criteria "
            "explicitly GRANT it (docs/build-loop.md, \"Unauthorized contract edits\") — naming "
            "the file in prose, or forbidding touching it, is not a grant. The Worker's own "
            "assertion of authorization does not count."
        )
    lines.append(review_audit.render_diff_completeness(diff_issues or []))
    return "\n".join(lines)


def delivery_evidence_section(session: Session, task: Task, run: Run) -> str:
    """The reviewer-payload section for handoff provenance and claims-vs-commits (T-10 DoD 7).

    Rendered from the archived `run.json`, i.e. the same numbers the run artifacts carry, so a
    reviewer who wants to check them has the commit SHAs and the porcelain output in front of them
    instead of the Worker's summary of them.
    """
    text = artifact_service.render_delivery_evidence_md(
        artifact_service.handoff_delivery_evidence(run.id),
        artifact_service.handoff_arrival(run.id),
    )
    if text:
        return text
    return (
        "no delivery evidence was archived for this run (a run created before T-10, or an agent "
        "task with no worktree): read the handoff's claims as unverified."
    )


def render_review_payload(
    *,
    input_text: str,
    truncations: list[dict],
    harness_missing: list[str],
    reviewer_missing: list[str],
    absence_checks: list[review_audit.AbsenceCheck],
    diff_issues: list[review_audit.DiffIssue],
) -> str:
    """The archived reviewer input plus the provenance of every absence/truncation claim.

    `input_text` is written verbatim: it is `static_text() + dynamic_text()` exactly as the
    provider received it. The post-review sections exist because an absence claim needs a
    provenance label, and a truncation needs to be named next to the text it removed.
    """
    harness_list = "\n".join(f"- {m}" for m in harness_missing) or "- none"
    reviewer_list = "\n".join(f"- {m}" for m in reviewer_missing) or "- none"
    diff_text = review_audit.render_diff_completeness(diff_issues)
    return f"""# Review payload

## Exact reviewer input (static_text() + dynamic_text())

<!-- Written verbatim from BuiltPrompt; no re-rendering and no silent edits. -->

{input_text}

## Truncation manifest

{prompt_builder.render_truncation_manifest(truncations)}

## Harness-detected missing artifacts

Computed by the orchestrator from the run directory; a model did not assert these.

{harness_list}

## Reviewer-asserted missing artifacts

Supplied by the reviewing model in its own output. Checked mechanically below before they can
cost a round.

{reviewer_list}

## Absence checks against the task worktree

{review_audit.render_absence_checks(absence_checks)}

## Diff completeness (git diffstat vs the supplied patch)

{diff_text}
"""


async def architect_review(
    session: Session,
    task: Task,
    run: Run,
    project: Project | None,
    handoff_synthesized: bool = False,
) -> Review:
    assert run.id is not None and task.id is not None
    files = {a["name"] for a in artifact_service.list_artifacts(run.id)}
    missing = missing_artifacts(files, handoff_synthesized)
    cwd = Path(run.worktree_path) if run.worktree_path else None
    base_branch = project.base_branch if project else None
    patch_text = artifact_service.read_text(run.id, "patch.diff") or ""
    diff_issues = review_audit.diff_completeness(cwd, base_branch, patch_text)
    state = DynamicState(
        handoff=(artifact_service.read_text(run.id, "handoff.md") or "")
        + "\n\nhandoff.json:\n"
        + (artifact_service.read_text(run.id, "handoff.json") or "MISSING"),
        test_summary=artifact_service.read_text(run.id, "test_summary.txt"),
        verification=artifact_service.read_text(run.id, "verification.md"),
        diff=patch_text,
        recovery=recovery_section(session, task, run),
        harness_findings=harness_findings_section(session, task, run, project, diff_issues),
        delivery_evidence=delivery_evidence_section(session, task, run),
        extra={
            "Harness-detected missing artifacts (computed by the orchestrator, not asserted by "
            "a model)": "\n".join(missing) or "none",
        },
    )
    prompt = prompt_builder.build_prompt(session, Role.architect, task, project, state)
    # T-11 DoD 1: archive the exact bytes the reviewer received, plus what the builder dropped.
    # The artifact name is declared as PROPOSED in
    # docs/contract-change-request-T11-review-payload.md; docs/frozen-contracts.md is unchanged.
    input_text = prompt.static_text() + "\n\n" + prompt.dynamic_text()
    choice = provider_service.choose_profile(session, task, Role.architect)
    raw = ""
    try:
        res = await provider_service.complete(
            session,
            choice.profile,
            prompt,
            metadata={
                "purpose": "review",
                "tags": task.tags,
                "task_id": task.id,
                "missing_artifacts": missing,
            },
        )
        raw = res.text
        result, ambiguous = parse_review(raw)
    except ProviderError as e:
        result = ReviewResult(
            verdict=ReviewVerdict.NEEDS_HUMAN,
            findings=[
                ReviewFinding(
                    file_or_area="architect provider",
                    issue=str(e),
                    violates="review could not be obtained",
                )
            ],
        )
        ambiguous = False
        raw = f"provider error: {e}"

    if ambiguous:
        artifact_service.write_text(run.id, "review.raw.txt", raw)

    model_missing = list(result.missing_artifacts)
    absence_checks = review_audit.check_findings(result.findings, cwd) + (
        review_audit.check_missing_artifacts(model_missing, cwd)
    )
    kept: list[ReviewFinding] = []
    refuted: list[ReviewFinding] = []
    for i, finding in enumerate(result.findings):
        status = review_audit.finding_status(absence_checks, i)
        if status == "refuted":
            refuted.append(finding)
        else:
            kept.append(finding)
        if status != "unchecked":
            result.notes.append(
                f"absence finding [{status}] {finding.file_or_area}: {finding.issue}"
            )
    result.findings = kept
    for check in absence_checks:
        result.notes.append(
            f"absence check [{check.status}] {check.claim}: {check.detail} (source: {check.source})"
        )
    for finding in refuted:
        result.notes.append(
            "refuted absence finding removed before it could cost a round: "
            f"{finding.file_or_area} — {finding.issue}"
        )

    # Reviewer-asserted missing artifacts are separated from the harness list, and only survive
    # when the worktree does not mechanically refute them.
    result.missing_artifacts = [f"harness-detected: {m}" for m in missing]
    for i, item in enumerate(model_missing):
        checks = [
            c for c in absence_checks if c.source == "missing_artifact" and c.finding_index == i
        ]
        if checks and all(c.status == "refuted" for c in checks):
            result.notes.append(
                f"refuted reviewer-asserted missing artifact: {item} (present in the worktree)"
            )
            continue
        result.missing_artifacts.append(f"reviewer-asserted: {item}")

    # T-11 DoD 7: a repository attribute or a missing patch section is a harness finding, so a
    # Worker cannot hide evidence by configuring `.gitattributes`. Pre-existing attributes are
    # reported (informational) but never become a finding against this run.
    for issue in diff_issues:
        if issue.blocks:
            result.findings.append(
                ReviewFinding(
                    file_or_area=issue.path,
                    issue="harness-detected diff completeness: " + issue.detail,
                    violates="T-11 DoD 7 (review input completeness)",
                )
            )
    if diff_issues:
        result.notes.append(
            "harness-detected diff completeness: "
            + review_audit.render_diff_completeness(diff_issues).replace("\n", " | ")
        )
        log_service.log_event(
            session,
            "review_diff_incomplete",
            structured_log.attach(
                "the supplied diff does not show every changed file",
                {
                    "task_id": task.id,
                    "run_id": run.id,
                    "issues": [issue.as_dict() for issue in diff_issues],
                },
            ),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )

    if missing and result.verdict == ReviewVerdict.APPROVED:
        result.verdict = ReviewVerdict.CHANGES_REQUESTED
        result.findings.append(
            ReviewFinding(
                file_or_area="artifacts",
                issue="required artifacts missing: " + "; ".join(missing),
                violates="handoff completeness (Architect check 7)",
            )
        )

    # A round is only requeued on a finding a human/model can act on. If every remaining
    # CHANGES_REQUESTED finding was an absence claim the worktree refutes, soften the verdict
    # instead of paying for another Worker round.
    if (
        result.verdict == ReviewVerdict.CHANGES_REQUESTED
        and refuted
        and not result.findings
        and not result.missing_artifacts
        and not ambiguous
    ):
        result.verdict = ReviewVerdict.APPROVED
        result.notes.append(
            "verdict softened to APPROVED: the only remaining findings were absence claims that "
            "the task worktree mechanically refuted"
        )
    if refuted:
        log_service.log_event(
            session,
            "review_absence_refuted",
            structured_log.attach(
                "the reviewer asserted missing evidence that the repository disproves",
                {
                    "task_id": task.id,
                    "run_id": run.id,
                    "refuted": [f.model_dump() for f in refuted],
                    "checks": [c.as_dict() for c in absence_checks if c.status == "refuted"],
                    "verdict": result.verdict.value,
                },
            ),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )

    if _EXEC_CLAIM.search(raw):
        result.notes.append(
            "Reviewer claimed to have executed something; the Architect has no "
            "execution access, so that claim is disregarded (verify yourself)."
        )

    reviewer = f"{choice.profile.name} ({choice.profile.model_name})"
    artifact_service.write_json(run.id, "review.json", result.model_dump(mode="json"))
    artifact_service.write_text(
        run.id, "review.md", artifact_service.render_review_md(result, reviewer)
    )
    artifact_service.write_text(
        run.id,
        "review_payload.md",
        render_review_payload(
            input_text=input_text,
            truncations=prompt.truncations,
            harness_missing=missing,
            reviewer_missing=model_missing,
            absence_checks=absence_checks,
            diff_issues=diff_issues,
        ),
        redact_content=False,
    )
    review = Review(
        task_id=task.id,
        run_id=run.id,
        reviewer_kind="architect",
        reviewer_model=choice.profile.model_name,
        verdict=result.verdict.value,
        findings=[f.model_dump() for f in result.findings],
        reasoning="\n".join(result.notes) or None,
    )
    session.add(review)
    task.review_status = result.verdict.value
    session.add(task)
    session.commit()
    session.refresh(review)
    log_service.log_event(
        session,
        "architect_review",
        f"{result.verdict.value}; {len(result.findings)} finding(s)",
        role="architect",
        task_id=task.id,
        run_id=run.id,
    )
    return review
