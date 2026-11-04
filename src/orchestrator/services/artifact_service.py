"""Per-run artifact directory management (frozen layout):

runs/<run_id>/
  prompt.md            worker prompt sent to the harness
  stdout.log stderr.log
  patch.diff diffstat.txt
  test.log             verification command output
  handoff.md handoff.json
  review.md review.json  (+ review.raw.txt when output was ambiguous)
  verification.md verification.json
  run.json             run metadata snapshot

evidence/<kind>/<key>/     out-of-band submissions archived instead of discarded (T-10).
                           The `runs/` layout above is a frozen contract and no new file name
                           may be added to it without a contract-change request, so a handoff
                           found in a worktree before any run of the new round exists to hold
                           it lands here, outside the repository, named `.json` (not `.log`,
                           which this repo gitignores).

`run.json` carries two additive keys written by `run_service` when a handoff is ingested
(T-10 DoD 7): `handoff_arrival` (how the submission reached these artifacts) and
`handoff_delivery_evidence` (what git says about the claims it contains). Both are read back
from the archived artifact by the Supervisor and the reviewer payload, so a human, a gate and a
model all see the same numbers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.core.config import get_settings
from orchestrator.core.schemas import Handoff, ReviewResult, VerificationResult
from orchestrator.utils.paths import safe_join
from orchestrator.utils.redaction import redact

# How a handoff reached the run artifacts. Recorded in `run.json`, never inferred from prose.
HANDOFF_ARRIVAL_CLEAN_EXIT = "clean_exit"
HANDOFF_ARRIVAL_SALVAGED = "salvaged"
HANDOFF_ARRIVAL_SYNTHESIZED = "synthesized"
HANDOFF_ARRIVALS = (
    HANDOFF_ARRIVAL_CLEAN_EXIT,
    HANDOFF_ARRIVAL_SALVAGED,
    HANDOFF_ARRIVAL_SYNTHESIZED,
)

# `task.handoff_status` for a submission salvaged from a non-clean exit (T-10 DoD 4). Distinct
# from a Worker's own status string, from SYNTHESIZED (orchestrator-authored) and from the
# RUN_* markers, and it is a value of an existing free-text column: no schema change.
HANDOFF_STATUS_SALVAGED = "SALVAGED"

# How each `files_changed` claim of a handoff is backed by git, computed at ingestion time.
CLAIM_WORKER_COMMIT = "worker_commit"
CLAIM_RECOVERY_COMMIT = "recovery_commit_only"
CLAIM_UNCOMMITTED = "uncommitted_only"
CLAIM_PRIOR_RUN = "prior_run_commit"
CLAIM_ABSENT = "absent"
CLAIM_STATUSES = (
    CLAIM_WORKER_COMMIT,
    CLAIM_RECOVERY_COMMIT,
    CLAIM_UNCOMMITTED,
    CLAIM_PRIOR_RUN,
    CLAIM_ABSENT,
)


def artifact_root() -> Path:
    root = get_settings().artifact_root
    assert root
    root.mkdir(parents=True, exist_ok=True)
    return root


def evidence_dir(kind: str, key: str) -> Path:
    """A directory under ORCHESTRATOR_HOME/evidence/<kind>/<key>/, created on demand.

    `kind` and `key` are single path segments: `safe_join` rejects absolute paths and `..` so a
    caller can never write outside the evidence root."""
    root = get_settings().evidence_root
    root.mkdir(parents=True, exist_ok=True)
    d = safe_join(root, kind, key)
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_dir(run_id: int) -> Path:
    d = safe_join(artifact_root(), str(int(run_id)))
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_text(run_id: int, name: str, content: str, redact_content: bool = True) -> Path:
    p = safe_join(run_dir(run_id), name)
    p.write_text(redact(content) if redact_content else content, encoding="utf-8")
    return p


def write_json(run_id: int, name: str, data: Any) -> Path:
    p = safe_join(run_dir(run_id), name)
    p.write_text(redact(json.dumps(data, indent=2, default=str)), encoding="utf-8")
    return p


def read_text(run_id: int, name: str) -> str | None:
    p = safe_join(run_dir(run_id), name)
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None


def read_json(run_id: int, name: str) -> dict[str, Any] | None:
    """An archived JSON artifact, or None when it is absent or unreadable.

    Readers must never crash on their own tool's artifact: a corrupt `run.json` means "no
    metadata", not "no review"."""
    raw = read_text(run_id, name)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def handoff_arrival(run_id: int | None) -> str | None:
    """How this run's handoff arrived, read back from the archived `run.json`."""
    if run_id is None:
        return None
    data = read_json(run_id, "run.json")
    if not data:
        return None
    arrival = data.get("handoff_arrival")
    return arrival if isinstance(arrival, str) else None


def handoff_delivery_evidence(run_id: int | None) -> dict[str, Any] | None:
    """The git-computed claims-vs-commits evidence attached at handoff ingestion (T-10 DoD 7)."""
    if run_id is None:
        return None
    data = read_json(run_id, "run.json")
    if not data:
        return None
    evidence = data.get("handoff_delivery_evidence")
    return evidence if isinstance(evidence, dict) else None


def list_artifacts(run_id: int) -> list[dict[str, Any]]:
    d = run_dir(run_id)
    return [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(d.iterdir())
        if p.is_file() and p.name not in {"CANCEL", "pid"}
    ]


# ---------------------------------------------------------------- renderers


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- none"


def render_handoff_md(h: Handoff) -> str:
    dod = (
        "\n".join(
            f"- [{'x' if d.met else ' '}] {d.item} — {d.evidence or 'no evidence'}"
            for d in h.dod_self_check
        )
        or "- none supplied"
    )
    deps = "\n".join(f"- {d.name} {d.version}: {d.reason}" for d in h.new_dependencies) or "- none"
    t = h.tests
    return f"""# Handoff — {h.phase}

**Status:** {h.status}
**Branch:** {h.branch}

## Summary
{h.summary}

## Files changed
{_bullets(h.files_changed)}

## Tests
`{t.command or "not run"}` -> passed {t.passed}, failed {t.failed}, skipped {t.skipped}
Log: {t.log_path or "n/a"}

## DoD self-check
{dod}

## New dependencies
{deps}

## Assumptions
{_bullets(h.assumptions)}

## Questions
{_bullets(h.questions)}

## Blockers
{_bullets(h.blockers)}
"""


def render_review_md(r: ReviewResult, reviewer: str) -> str:
    findings = (
        "\n".join(
            f"{i}. **{f.file_or_area}** — {f.issue} (violates: {f.violates or 'n/a'})"
            for i, f in enumerate(r.findings, 1)
        )
        or "none"
    )
    return f"""# Review ({reviewer})

VERDICT: {r.verdict.value}

FINDINGS:
{findings}

## Missing artifacts
{_bullets(r.missing_artifacts)}

## Notes
{_bullets(r.notes)}
"""


MAX_COMMITS_IN_EVIDENCE = 40


def render_delivery_evidence_md(evidence: dict[str, Any], arrival: str | None = None) -> str:
    """The reviewer-payload rendering of `run.json.handoff_delivery_evidence` (T-10 DoD 7).

    Rendered from the SAME archived numbers the run artifacts carry, so a claim that exists only
    as uncommitted worktree state reads as a promise rather than as delivered work, and a claim
    carried only by a harness recovery commit is labelled as committed-but-not-self-checked.
    """
    if not evidence:
        return ""
    commits = evidence.get("commits") or []
    run_commits = evidence.get("run_commits")
    if run_commits is None:
        # Older archived evidence (before the baseline split) only had the whole-branch list.
        run_commits = commits
    prior_commits = evidence.get("prior_run_commits") or []
    status = evidence.get("claim_status") or {}
    counts = evidence.get("claim_counts") or {}
    lines = [f"handoff arrival: {arrival or evidence.get('arrival') or 'unknown'}"]
    if evidence.get("exit_state"):
        lines.append(f"run exit state: {evidence['exit_state']}")
    lines.append(
        f"commits on this branch since {evidence.get('base_branch', 'the base branch')}: "
        f"{evidence.get('commit_count', len(commits))} "
        f"(created by this run {evidence.get('run_commit_count', len(run_commits))}: "
        f"worker-authored {evidence.get('worker_commit_count', 0)}, harness recovery "
        f"{evidence.get('recovery_commit_count', 0)}; earlier rounds "
        f"{evidence.get('prior_run_commit_count', len(prior_commits))})"
    )
    lines.append(
        "run baseline: "
        + (
            evidence.get("baseline_sha")
            or f"merge-base with {evidence.get('base_branch', 'the base branch')} "
            "(no earlier run recorded)"
        )
    )
    for c in run_commits[:MAX_COMMITS_IN_EVIDENCE]:
        kind = "harness recovery commit, NEVER self-checked" if c.get("recovery") else "commit"
        paths = ", ".join(f"{f['status']} {f['path']}" for f in (c.get("files") or [])[:12])
        lines.append(f"  - {c.get('short')} [{kind}] {c.get('subject')} ({paths or 'no files'})")
    if len(run_commits) > MAX_COMMITS_IN_EVIDENCE:
        lines.append(f"  - (+{len(run_commits) - MAX_COMMITS_IN_EVIDENCE} more commits)")
    if prior_commits:
        lines.append(
            f"earlier commits on this branch, NOT created by this run ({len(prior_commits)}):"
        )
        for c in prior_commits[:MAX_COMMITS_IN_EVIDENCE]:
            lines.append(f"  - {c.get('short')} {c.get('subject')}")
        if len(prior_commits) > MAX_COMMITS_IN_EVIDENCE:
            lines.append(f"  - (+{len(prior_commits) - MAX_COMMITS_IN_EVIDENCE} more commits)")
    lines.append(f"diffstat vs merge-base:\n{evidence.get('diffstat') or '  (empty diff)'}")
    dirt = evidence.get("porcelain_at_ingestion") or []
    lines.append(
        "git status --porcelain -uall at ingestion: "
        + ("clean" if not dirt else "\n" + "\n".join(f"  {d}" for d in dirt))
    )
    swept = evidence.get("uncommitted_before_harness_sweep") or []
    lines.append(
        "uncommitted when the run ended (before the harness recovery sweep): "
        + (", ".join(swept) if swept else "none")
    )
    lines.append(
        "claims in handoff.files_changed "
        f"({evidence.get('claimed_count', len(status))}): "
        + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no files claimed")
    )
    for path, st in sorted(status.items()):
        lines.append(f"  - {path} -> {st}")
    lines.append(
        "structured flag: claims_all_backed_by_this_run_commits="
        f"{str(evidence.get('claims_all_backed_by_this_run_commits')).lower()}"
    )
    # Provenance, not a gate. Wording matters here: `recovery_commit_only` is already on the
    # branch (T-8's recovery machinery committed it), so calling it undelivered work would
    # contradict docs/build-loop.md and invite an LLM reviewer to reject a round for a sweep it
    # did not cause. Only `worker_commit` carries a self-check by THIS round behind it.
    lines.append(
        "provenance only, not a gate: `worker_commit` is a commit this run created and the "
        "Worker authored (a self-checked claim); `recovery_commit_only` IS on the branch but only "
        "inside a harness recovery commit no Worker self-checked, so judge it by re-verification "
        "like any other unreviewed commit; `uncommitted_only` exists only as worktree state and "
        "is NOT on the branch; `prior_run_commit` is on the branch but was created by an earlier "
        "round, so it is not this round's delivery; `absent` was not found. Only `worker_commit` "
        "has a self-check by this run behind it."
    )
    return "\n".join(lines)


def render_verification_md(v: VerificationResult) -> str:
    rows = "\n".join(
        f"| {c.name} | {c.status} | `{c.command or '-'}` | {c.evidence or '-'} | {c.notes or ''} |"
        for c in v.checks
    )
    return f"""# Supervisor verification — {v.phase}

**Final result:** {v.final_result}
**Contradicts worker:** {v.contradicts_worker}

| Check | Status | Command | Evidence | Notes |
|---|---|---|---|---|
{rows}
"""
