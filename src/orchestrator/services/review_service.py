"""Architect gate review: build the review prompt from artifacts only, call the architect
profile, parse its verdict strictly, and persist review.md / review.json.

Parsing policy (fail closed):
  - JSON object present and valid -> use it; if a VERDICT line also exists it must agree;
  - otherwise a `VERDICT:` line + `FINDINGS:` list is parsed;
  - anything else -> CHANGES_REQUESTED with an "ambiguous review output" finding, raw kept.
Deterministically-detected missing artifacts are always findings; an APPROVED verdict with
missing artifacts is downgraded to CHANGES_REQUESTED.
"""

from __future__ import annotations

import re

from sqlmodel import Session

from orchestrator.core.models import Project, Review, ReviewVerdict, Role, Run, Task
from orchestrator.core.schemas import ReviewFinding, ReviewResult
from orchestrator.providers.base import ProviderError
from orchestrator.services import artifact_service, log_service, prompt_builder, provider_service
from orchestrator.services.prompt_builder import DynamicState
from orchestrator.utils.json_output import JSONExtractionError, extract_json

_VERDICT = re.compile(r"(?im)^\s*VERDICT:\s*(APPROVED|CHANGES_REQUESTED|NEEDS_HUMAN)\s*$")
_FINDING = re.compile(r"(?m)^\s*(?:-|\d+[.)])\s+(.+)$")
_EXEC_CLAIM = re.compile(r"(?i)\bI\s+(?:have\s+)?(?:ran|run|re-ran|reran|executed|grepped|tested)\b")

AMBIGUOUS = ReviewFinding(file_or_area="review output",
                          issue="review output was ambiguous or unparseable; raw output stored",
                          violates="review.json contract")


def parse_review(text: str) -> tuple[ReviewResult, bool]:
    """Return (result, ambiguous)."""
    line = _VERDICT.search(text or "")
    line_verdict = line.group(1) if line else None
    try:
        data = extract_json(text)
        if isinstance(data, dict) and "verdict" in data:
            result = ReviewResult.model_validate(data)
            if line_verdict and line_verdict != result.verdict.value:
                return ReviewResult(verdict=ReviewVerdict.CHANGES_REQUESTED,
                                    findings=[AMBIGUOUS],
                                    notes=["VERDICT line and JSON verdict disagree"]), True
            return result, False
    except (JSONExtractionError, ValueError):
        pass
    if line_verdict:
        findings_part = text[line.end():] if line else ""
        if "FINDINGS:" in findings_part:
            findings_part = findings_part.split("FINDINGS:", 1)[1]
        findings = []
        for m in _FINDING.finditer(findings_part):
            item = m.group(1).strip()
            if item.lower() in ("none", "none.", "n/a"):
                continue
            area, _, issue = item.partition(":")
            findings.append(ReviewFinding(file_or_area=area.strip() if issue else "general",
                                          issue=(issue or item).strip()))
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


async def architect_review(session: Session, task: Task, run: Run, project: Project | None,
                           handoff_synthesized: bool = False) -> Review:
    assert run.id is not None and task.id is not None
    files = {a["name"] for a in artifact_service.list_artifacts(run.id)}
    missing = missing_artifacts(files, handoff_synthesized)
    state = DynamicState(
        handoff=(artifact_service.read_text(run.id, "handoff.md") or "") + "\n\nhandoff.json:\n"
        + (artifact_service.read_text(run.id, "handoff.json") or "MISSING"),
        test_summary=artifact_service.read_text(run.id, "test_summary.txt"),
        verification=artifact_service.read_text(run.id, "verification.md"),
        diff=artifact_service.read_text(run.id, "patch.diff"),
        extra={"Missing artifacts detected by orchestrator": "\n".join(missing) or "none"},
    )
    prompt = prompt_builder.build_prompt(session, Role.architect, task, project, state)
    choice = provider_service.choose_profile(session, task, Role.architect)
    raw = ""
    try:
        res = await provider_service.complete(
            session, choice.profile, prompt,
            metadata={"purpose": "review", "tags": task.tags, "task_id": task.id,
                      "missing_artifacts": missing})
        raw = res.text
        result, ambiguous = parse_review(raw)
    except ProviderError as e:
        result = ReviewResult(
            verdict=ReviewVerdict.NEEDS_HUMAN,
            findings=[ReviewFinding(file_or_area="architect provider", issue=str(e),
                                    violates="review could not be obtained")])
        ambiguous = False
        raw = f"provider error: {e}"

    if ambiguous:
        artifact_service.write_text(run.id, "review.raw.txt", raw)
    for m in missing:
        if m not in result.missing_artifacts:
            result.missing_artifacts.append(m)
    if missing and result.verdict == ReviewVerdict.APPROVED:
        result.verdict = ReviewVerdict.CHANGES_REQUESTED
        result.findings.append(ReviewFinding(
            file_or_area="artifacts", issue="required artifacts missing: " + "; ".join(missing),
            violates="handoff completeness (Architect check 7)"))
    if _EXEC_CLAIM.search(raw):
        result.notes.append("Reviewer claimed to have executed something; the Architect has no "
                            "execution access, so that claim is disregarded (verify yourself).")

    reviewer = f"{choice.profile.name} ({choice.profile.model_name})"
    artifact_service.write_json(run.id, "review.json", result.model_dump(mode="json"))
    artifact_service.write_text(run.id, "review.md",
                                artifact_service.render_review_md(result, reviewer))
    review = Review(task_id=task.id, run_id=run.id, reviewer_kind="architect",
                    reviewer_model=choice.profile.model_name, verdict=result.verdict.value,
                    findings=[f.model_dump() for f in result.findings],
                    reasoning="\n".join(result.notes) or None)
    session.add(review)
    task.review_status = result.verdict.value
    session.add(task)
    session.commit()
    session.refresh(review)
    log_service.log_event(session, "architect_review", f"{result.verdict.value}; "
                          f"{len(result.findings)} finding(s)", role="architect",
                          task_id=task.id, run_id=run.id)
    return review
