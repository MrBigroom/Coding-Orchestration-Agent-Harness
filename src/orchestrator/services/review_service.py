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
and an absence finding is checked as either an existence claim (refuted by the worktree) or a
visibility claim (checked against the supplied patch and the truncation manifest) before it can
requeue a round. A review whose every remaining finding was a refuted claim is routed to
NEEDS_HUMAN: the harness never upgrades a reviewer's verdict to APPROVED, because "the model was
wrong about one absence" is not evidence that the code is correct and accepting a task is not
the harness's call to make (T-11 round-2 reviewer finding 1).
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


def evidence_delivery_finding(paths: list[str]) -> ReviewFinding | None:
    """The harness finding for compensation artifacts the diff budget could not carry.

    Kept as a function so the message is asserted directly in a unit test (T-16 DoD 7) and the
    review path cannot drift from it.
    """
    if not paths:
        return None
    return ReviewFinding(
        file_or_area=", ".join(paths),
        issue=(
            "harness-detected evidence delivery: the reviewer diff budget could not carry "
            f"{len(paths)} compensation artifact(s) in full; this is a review-input budget "
            "failure, not missing Worker evidence"
        ),
        violates=(
            "review input completeness (compensation artifacts must be funded or reported; "
            "this is a harness budget gate, not a task-specific DoD item)"
        ),
    )


def render_review_payload(
    *,
    input_text: str,
    truncations: list[dict],
    harness_missing: list[str],
    reviewer_missing: list[str],
    absence_checks: list[review_audit.AbsenceCheck],
    diff_issues: list[review_audit.DiffIssue],
    supplied: review_audit.SuppliedEvidence,
    universe: review_audit.PathUniverse | None = None,
) -> str:
    """The archived reviewer input plus the provenance of every absence/truncation claim.

    `input_text` is written verbatim: it is `static_text() + dynamic_text()` exactly as the
    provider received it. The post-review sections exist because an absence claim needs a
    provenance label, and a truncation needs to be named next to the text it removed.

    T-22 DoD 4: the absence section also carries the *resolution* evidence — which path was tried,
    where it was found, which sources were searched — because a mechanical verdict is only as
    trustworthy as its resolver, and run 38 proved a resolver can be wrong 15 times in one review.
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

## What the reviewer was actually given

{review_audit.render_supplied_summary(supplied, universe)}

## Harness-detected missing artifacts

Computed by the orchestrator from the run directory; a model did not assert these.

{harness_list}

## Reviewer-asserted missing artifacts

Supplied by the reviewing model in its own output. Checked mechanically below before they can
cost a round.

{reviewer_list}

## Absence checks (existence claims vs visibility claims)

Each row states how the claim was resolved to a path before it was judged, and lists every
candidate when a bare name matched more than one file.

{review_audit.render_absence_checks(absence_checks)}

Tally: {review_audit.tally_verdicts(absence_checks)}

## Diff completeness (git diffstat vs the rendered reviewer diff)

Audited against the budgeted text the prompt builder actually emitted, not the archived
`patch.diff` (T-16 DoD 3). `patch.diff` remains the full record.

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
    # T-16 DoD 3/7: render the budgeted diff ONCE, then audit that exact text for completeness and
    # hand the same text to the prompt builder. The archived patch.diff stays the full record.
    recorder = prompt_builder.TruncationRecorder()
    rendered_diff = prompt_builder.render_diff_for_prompt(
        patch_text, prompt_builder.get_settings().max_diff_tokens, recorder
    )
    # A preliminary audit lets the reviewer prompt name budget omissions before the aggregate
    # context budget runs. It is NOT the audit of record: after build_prompt the same checks run
    # again against `prompt.rendered_diff`, which is re-extracted from the final dynamic section
    # (T-16 review round 1, finding 5). The second pass is what proves the audited text is the
    # text sent.
    pre_undelivered = prompt_builder.undelivered_compensation(
        [record.as_dict() for record in recorder.records]
    )
    pre_diff_issues = review_audit.diff_completeness(
        cwd, base_branch, patch_text, rendered=rendered_diff
    )
    harness_text = harness_findings_section(session, task, run, project, pre_diff_issues)
    if pre_undelivered:
        harness_text += (
            "\nHARNESS FINDING: the reviewer diff budget could not deliver "
            f"{len(pre_undelivered)} compensation artifact(s) in full: "
            + ", ".join(pre_undelivered)
            + ". This is a harness-detected delivery failure (T-16 DoD 7), not missing Worker "
            "evidence. Do not file it as an evidence gap; the review records it separately."
        )
    state = DynamicState(
        handoff=(artifact_service.read_text(run.id, "handoff.md") or "")
        + "\n\nhandoff.json:\n"
        + (artifact_service.read_text(run.id, "handoff.json") or "MISSING"),
        test_summary=artifact_service.read_text(run.id, "test_summary.txt"),
        verification=artifact_service.read_text(run.id, "verification.md"),
        diff=patch_text,
        rendered_diff=rendered_diff,
        recovery=recovery_section(session, task, run),
        harness_findings=harness_text,
        delivery_evidence=delivery_evidence_section(session, task, run),
        extra={
            "Harness-detected missing artifacts (computed by the orchestrator, not asserted by "
            "a model)": "\n".join(missing) or "none",
        },
    )
    prompt = prompt_builder.build_prompt(
        session, Role.architect, task, project, state, recorder=recorder
    )
    # The audit of record: `prompt.rendered_diff` is the block left in the FINAL dynamic_state
    # after enforce_budget, so the diffstat is compared with the bytes the reviewer was sent.
    as_sent_diff = prompt.rendered_diff
    diff_issues = review_audit.diff_completeness(
        cwd, base_branch, patch_text, rendered=as_sent_diff
    )
    undelivered_evidence = prompt_builder.undelivered_compensation(
        [record.as_dict() for record in recorder.records],
        rendered=as_sent_diff,
        patch=patch_text,
    )
    audit_changed_after_context_budget = (
        diff_issues != pre_diff_issues or undelivered_evidence != pre_undelivered
    )
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
    # What the reviewer was actually given, derived from the same recorder that performed every
    # cut. Visibility claims are checked against this, never against file existence.
    supplied = review_audit.supplied_evidence(
        as_sent_diff,
        prompt.truncations,
        review_audit.changed_paths(cwd, base_branch),
    )
    # T-22 DoD 1: one repository view for the whole round, so a bare filename in a finding and a
    # bare filename in `missing_artifacts` resolve against the same paths.
    universe = review_audit.build_universe(cwd, supplied)
    absence_checks = review_audit.check_findings(
        result.findings, cwd, supplied, universe
    ) + review_audit.check_missing_artifacts(model_missing, cwd, supplied, universe)
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
            f"absence check [{check.status}] {check.claim}: {check.detail} "
            f"({check.claim_note()})"
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

    # T-16 DoD 7: evidence the diff budget could not carry is a harness-detected delivery
    # failure, named as a finding so no reviewer has to infer it from an absent file.
    if undelivered_evidence:
        finding = evidence_delivery_finding(undelivered_evidence)
        if finding is not None:
            result.findings.append(finding)
        result.notes.append(
            "harness-detected: compensation artifacts the reviewer diff budget could not "
            "deliver in full: " + ", ".join(undelivered_evidence)
        )
        log_service.log_event(
            session,
            "review_evidence_undelivered",
            structured_log.attach(
                "the reviewer diff budget could not carry the task's own evidence",
                {
                    "task_id": task.id,
                    "run_id": run.id,
                    "paths": undelivered_evidence,
                    "task_evidence": "harness-detected: not missing Worker evidence",
                },
            ),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )

    # T-11 DoD 7: a repository attribute or a missing patch section is a harness finding, so a
    # Worker cannot hide evidence by configuring `.gitattributes`. Pre-existing attributes are
    # reported (informational) but never become a finding against this run.
    if audit_changed_after_context_budget:
        result.notes.append(
            "harness-detected: the aggregate prompt context budget changed the diff after the "
            "preliminary audit; completeness and supplied-evidence are reported against the "
            f"final {len(as_sent_diff.encode('utf-8'))}-byte diff block actually sent"
        )
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
    # CHANGES_REQUESTED finding was an absence claim the harness refuted, do NOT requeue — but do
    # NOT accept either. Refuting a claim proves the reviewer was wrong about the evidence, not
    # that the code is correct, and changing a verdict to APPROVED would let a heuristic decide
    # who may accept a task (T-11 round-2 reviewer finding 1: that was a fail-open gate). The
    # verdict stays a reviewer verdict; the harness records what it could not resolve and routes
    # to a human. `NEEDS_HUMAN` -> `blocked`, so no Worker round is consumed either.
    if (
        result.verdict == ReviewVerdict.CHANGES_REQUESTED
        and refuted
        and not result.findings
        and not result.missing_artifacts
        and not ambiguous
    ):
        result.verdict = ReviewVerdict.NEEDS_HUMAN
        result.findings.append(
            ReviewFinding(
                file_or_area="review verdict",
                issue=(
                    "the reviewer requested changes, but every remaining finding was an absence "
                    "claim the harness mechanically refuted; the harness neither requeues a "
                    "round for a refuted claim nor accepts a task on one — a human must decide"
                ),
                violates="T-11 DoD 3 (refuted findings must not consume a round, and never accept)",
            )
        )
        result.notes.append(
            "verdict held at NEEDS_HUMAN (never upgraded): the only remaining findings were "
            "absence claims the repository or the archived input mechanically refuted"
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

    # DoD 2: review.json must say which findings the harness can confirm mechanically. Absence
    # provenance is recorded above; this is the one-line summary an auditor reads first, and since
    # T-22 DoD 4 it also breaks `confirmed` down by how the path was resolved, so a confident batch
    # resting on unresolvable names is visible without re-reading every row.
    if absence_checks:
        result.notes.append(
            "mechanical absence checks: "
            + review_audit.tally_verdicts(absence_checks)
            + f" (of {len(result.findings)} finding(s) kept, {len(refuted)} refuted finding(s) "
            "removed)"
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
            supplied=supplied,
            universe=universe,
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
