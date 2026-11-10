"""One Worker run, end to end, as a state machine over artifacts:

  pending -> preparing (route, worktree, prompt) -> running (harness or direct API)
          -> testing (supervisor verification) -> completed | failed | timeout | cancelled

then hands over to the Supervisor gate (review/accept/requeue/block/merge).
"""

from __future__ import annotations

import hashlib
import json
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import Session

from orchestrator.adapters.base import AdapterResult, RunContext, TemplateError
from orchestrator.core.events import bus
from orchestrator.core.models import (
    Project,
    Review,
    Role,
    Run,
    RunStatus,
    Task,
    TaskKind,
    TaskStatus,
    utcnow,
)
from orchestrator.core.schemas import Handoff, MemorySuggestion
from orchestrator.core.state import check_run_transition
from orchestrator.providers.base import ProviderError
from orchestrator.services import (
    adapter_service,
    artifact_service,
    git_service,
    log_service,
    memory_service,
    process_service,
    prompt_builder,
    provider_service,
    supervisor_service,
    task_service,
    usage_service,
)
from orchestrator.services.prompt_builder import DynamicState
from orchestrator.utils import structured_log
from orchestrator.utils.git import head_sha, run_git, status_porcelain
from orchestrator.utils.redaction import redact

# T-10: a run that was killed (timeout, non-zero exit, cancel) may already hold a complete,
# valid submission in the worktree. Discarding it burned whole rounds (measured on run 11, where
# a 36 KB READY_FOR_REVIEW handoff was thrown away 53 seconds after it was written, and on run 13,
# where a cancelled run's handoff described work that did not exist). The non-clean-exit paths now
# try to ingest the handoff and continue to the normal gate. Documented in docs/build-loop.md,
# "Salvaging a handoff from a killed run".
SALVAGE_MODE_NON_CLEAN_EXIT = "non_clean_exit"
SALVAGE_MODE_PRE_ROUND_ARCHIVE = "pre_round_archive"
SALVAGE_MODE_ALREADY_ARCHIVED = "already_archived"
EVIDENCE_KIND_PRE_ROUND = "pre-round-handoff"


@dataclass
class RoutePlan:
    harness_adapter: str | None
    harness_reason: str
    provider_profile: str
    model: str
    provider_reason: str
    command_preview: str | None
    worktree_path: str | None
    branch: str | None
    would_block: bool = False

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _set_run(session: Session, run: Run, status: str, **fields: Any) -> Run:
    check_run_transition(run.status, status)
    run.status = RunStatus(status).value
    for k, v in fields.items():
        setattr(run, k, v)
    session.add(run)
    session.commit()
    bus.publish(run.id or 0, {"type": "status", "status": run.status})
    # Terminal success event. `run_failed`/`run_timeout` are emitted on the failure paths of
    # `_execute`; this is the matching chokepoint for a run that finished normally, so a
    # consumer (the T-9 notification fan-out, the build-log GUI) never has to INFER success from
    # a task_status transition. Additive: no existing event name or line shape changed.
    if run.status == RunStatus.completed:
        log_service.log_event(
            session,
            "run_completed",
            f"{run.short_id} finished normally (exit {run.exit_code})",
            role="worker",
            task_id=run.task_id,
            run_id=run.id,
            commit_sha=run.commit_sha,
        )
    return run


def _project(session: Session, task: Task) -> Project | None:
    return session.get(Project, task.project_id) if task.project_id else None


def dry_run(session: Session, task: Task) -> RoutePlan:
    """What would run, without executing anything or touching git."""
    project = _project(session, task)
    role = Role.agent if task.kind == TaskKind.agent else Role.worker
    profile = provider_service.choose_profile(session, task, role)
    harness = adapter_service.choose_harness(
        session, task, profile.profile.name, profile.profile.model_name
    )
    preview = None
    wt = None
    if task.kind != TaskKind.agent and project:
        wt = str(git_service.worktree_path(project.slug, task.id or 0))
    if harness.adapter and task.kind != TaskKind.agent:
        adapter = adapter_service.get_adapter(session, harness.adapter)
        ctx = RunContext(
            run_id=0,
            task_id=task.id or 0,
            cwd=Path(wt or "."),
            prompt_file=Path("prompt.md"),
            artifacts_dir=Path("runs/<run_id>"),
            handoff_path=Path(wt or ".") / git_service.ORCH_DIR / "handoff.json",
            branch=task.branch_name or "",
            phase=task_service.phase_label(task),
            tags=task.tags,
            model=None,
        )
        preview = shlex.join(adapter.command_preview(ctx))
    would_block = (
        harness.adapter is None and task.kind != TaskKind.agent and "will block" in harness.reason
    )
    return RoutePlan(
        harness_adapter=None if task.kind == TaskKind.agent else harness.adapter,
        harness_reason="agent tasks call the model directly"
        if task.kind == TaskKind.agent
        else harness.reason,
        provider_profile=profile.profile.name,
        model=profile.profile.model_name,
        provider_reason=profile.reason,
        command_preview=preview,
        worktree_path=wt,
        branch=task.branch_name,
        would_block=would_block,
    )


def _latest_feedback(session: Session, task: Task) -> str | None:
    from sqlmodel import col, select

    from orchestrator.services.review_service import worker_actionable_findings

    review = session.exec(
        select(Review).where(Review.task_id == task.id).order_by(col(Review.id).desc())
    ).first()
    if not review or review.verdict == "APPROVED":
        return None
    # A harness-owned evidence-budget finding is not something a Worker can fix; sending it back
    # would ask for an evidence-compression commit, which T-16 DoD 8(a) forbids.
    items = "\n".join(
        f"- {f.get('file_or_area')}: {f.get('issue')} (violates {f.get('violates') or 'n/a'})"
        for f in worker_actionable_findings(review.findings)
    )
    return f"Verdict: {review.verdict}\n{items}"


def _last_aborted_run(session: Session, task: Task, exclude_id: int | None) -> Run | None:
    """The most recent run that ended aborted (timeout/failure/cancel) — the one whose half-
    finished edits a pre-run recovery sweep is about to commit."""
    for r in reversed(task_service.runs_for(session, task)):
        if r.id != exclude_id and r.status in (
            RunStatus.timeout,
            RunStatus.failed,
            RunStatus.cancelled,
        ):
            return r
    return None


def _synthesize_handoff(
    task: Task, run: Run, files: list[str], branch: str, result: AdapterResult
) -> Handoff:
    return Handoff(
        phase=task_service.phase_label(task),
        branch=branch,
        status="READY_FOR_REVIEW",
        summary="No valid handoff.json was produced by the worker. This handoff was "
        "synthesized by the orchestrator from observed evidence only.",
        files_changed=files,
        assumptions=["synthesized by orchestrator; worker self-check unavailable"],
        blockers=[] if result.exit_code == 0 else [f"worker exit code {result.exit_code}"],
    )


# ------------------------------------------------------ T-10: handoff salvage + provenance


def read_handoff_file(path: Path) -> tuple[Handoff | None, str | None]:
    """Read a worktree `handoff.json`. Returns `(handoff, error)`.

    `(None, None)` means "no file" — there is nothing to salvage. `(None, reason)` means a file
    exists but cannot be used: that is a logged fact, never an exception, because a truncated
    write from a killed process is the normal shape of this case.
    """
    if not path.is_file():
        return None, None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return None, f"{path} could not be read: {e}"
    if not raw.strip():
        return None, f"{path} is empty (0 usable bytes)"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"{path} is not valid JSON ({e}); {len(raw)} bytes on disk"
    try:
        return Handoff.model_validate(data), None
    except ValueError as e:
        return None, f"{path} is valid JSON but does not validate as Handoff: {_one_line(str(e))}"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())[:600]


def handoff_snapshot(path: Path) -> dict[str, Any] | None:
    """A cheap fingerprint of the handoff file as it stands right now.

    Used to answer one question at the end of a run: is the file still the exact leftover that was
    already in the worktree when the round started? A stat-only check is not enough (a same-size
    rewrite with a coarse timestamp would collide), so the digest is included. An unreadable file
    still gets a stat-based fingerprint; ingestion will report why it cannot be used.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = None
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "sha256": digest}


def _normalize_rel(path: str) -> str:
    return path.strip().removeprefix("./").removeprefix("/")


def delivery_evidence(
    wt: Path,
    base_branch: str,
    handoff: Handoff | None,
    *,
    arrival: str,
    exit_state: str,
    uncommitted_before_sweep: list[str] | None = None,
    baseline_sha: str | None = None,
) -> dict[str, Any]:
    """What git says about a handoff's claims, computed at ingestion time (T-10 DoD 7).

    A handoff is a *statement*. `git log base..HEAD`, the diffstat and `git status --porcelain
    -uall` are evidence. The measured case this exists for (run 13, 2026-11-04): a cancelled run
    whose 1164-byte handoff claimed four code/doc fixes while it had made zero commits and the
    worktree had no tracked modifications — and the old "claimed files exist on disk" check passed,
    because the files did exist as uncommitted scratch.

    Claims are scoped to THIS run, not to the whole phase branch. `baseline_sha` is the branch head
    when the run started (the previous run's `commit_sha`, or the merge-base on a first run):
    commits after it were created by this run, commits before it belong to earlier rounds. A retry
    that creates zero commits but claims files an earlier round touched is exactly the laundering
    path this split closes — it is classified `prior_run_commit`, never `worker_commit`.

    Each claim is classified as exactly one of `artifact_service.CLAIM_STATUSES`, first match
    wins: `worker_commit` (delivered by a Worker-authored commit created by this run),
    `recovery_commit_only` (on the branch, but only inside a harness recovery commit this run that
    no Worker self-checked), `uncommitted_only` (a promise, still on the worktree, not on the
    branch), `prior_run_commit` (on the branch, but created by an earlier round), `absent`
    (nowhere). The structured flag `claims_all_backed_by_this_run_commits` is the one-line answer
    a reviewer can act on.
    """
    commits = git_service.commits_against_base(wt, base_branch)
    run_commits = (
        git_service.commits_against_base(wt, baseline_sha) if baseline_sha else list(commits)
    )
    run_shas = {c.sha for c in run_commits}
    prior_commits = [c for c in commits if c.sha not in run_shas]
    worker_paths: set[str] = set()
    recovery_paths: set[str] = set()
    for c in run_commits:
        paths = {_normalize_rel(f["path"]) for f in c.files}
        (recovery_paths if c.recovery else worker_paths).update(paths)
    prior_paths: set[str] = set()
    for c in prior_commits:
        prior_paths.update(_normalize_rel(f["path"]) for f in c.files)
    split = git_service.status_split(wt)
    uncommitted = {_normalize_rel(p) for p in (*split.tracked_modified, *split.untracked_added)}
    claims = [_normalize_rel(f) for f in (handoff.files_changed if handoff else [])]
    status: dict[str, str] = {}
    for claim in claims:
        if claim in worker_paths:
            status[claim] = artifact_service.CLAIM_WORKER_COMMIT
        elif claim in recovery_paths:
            status[claim] = artifact_service.CLAIM_RECOVERY_COMMIT
        elif claim in uncommitted:
            status[claim] = artifact_service.CLAIM_UNCOMMITTED
        elif claim in prior_paths:
            status[claim] = artifact_service.CLAIM_PRIOR_RUN
        else:
            status[claim] = artifact_service.CLAIM_ABSENT
    counts: dict[str, int] = {}
    for s in status.values():
        counts[s] = counts.get(s, 0) + 1
    _, diffstat = git_service.diff_against_base(wt, base_branch)
    return {
        "arrival": arrival,
        "exit_state": exit_state,
        "base_branch": base_branch,
        "baseline_sha": baseline_sha,
        "commits": [c.as_dict() for c in commits],
        "commit_count": len(commits),
        "run_commits": [c.as_dict() for c in run_commits],
        "run_commit_count": len(run_commits),
        "prior_run_commits": [c.as_dict() for c in prior_commits],
        "prior_run_commit_count": len(prior_commits),
        "worker_commit_count": sum(1 for c in run_commits if not c.recovery),
        "recovery_commit_count": sum(1 for c in run_commits if c.recovery),
        "diffstat": diffstat,
        "porcelain_at_ingestion": [
            line for line in status_porcelain(wt).splitlines() if line.strip()
        ],
        "uncommitted_before_harness_sweep": sorted(uncommitted_before_sweep or []),
        "claimed_files": sorted(claims),
        "claimed_count": len(claims),
        "claim_status": status,
        "claim_counts": counts,
        "claims_all_backed_by_this_run_commits": bool(claims)
        and all(s == artifact_service.CLAIM_WORKER_COMMIT for s in status.values()),
    }


def _run_json_payload(
    run: Run, task: Task, harness_name: str | None, result: AdapterResult
) -> dict[str, Any]:
    return {
        "run": run.short_id,
        "task": task.short_id,
        "adapter": harness_name,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "cancelled": result.cancelled,
        "process_ms": result.duration_ms,
    }


def _write_run_json(
    run: Run,
    task: Task,
    harness_name: str | None,
    result: AdapterResult,
    arrival: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Write `run.json`, optionally attaching handoff provenance (additive keys, same file name).

    The archived copy is the single source of truth: the Supervisor reads it back rather than
    re-deriving, so the gate, the reviewer payload and a human opening the artifact cannot end up
    quoting three different numbers for one run.
    """
    payload = _run_json_payload(run, task, harness_name, result)
    if arrival:
        payload["handoff_arrival"] = arrival
    if evidence is not None:
        payload["handoff_delivery_evidence"] = evidence
    artifact_service.write_json(run.id, "run.json", payload)


def archive_handoff_artifacts(run: Run, handoff: Handoff) -> None:
    artifact_service.write_json(run.id, "handoff.json", handoff.model_dump(mode="json"))
    artifact_service.write_text(run.id, "handoff.md", artifact_service.render_handoff_md(handoff))


def exit_state_text(run: Run, result: AdapterResult, status: str | None = None) -> str:
    """The run's exit state in one line, for the salvage event and the reviewer payload.

    `status` overrides `run.status` for the clean-exit path, which archives this text before the
    run is moved to its terminal state: recording `status=running` there would describe the row
    mid-transition rather than the exit that produced the submission.
    """
    why = (
        "timed out (adapter timeout killed the process)"
        if result.timed_out
        else "cancelled (cancel marker)"
        if result.cancelled
        else "non-zero exit"
        if result.exit_code != 0
        else "harness reported an error while exiting 0"
    )
    return (
        f"run {run.short_id} status={status or run.status} exit_code={result.exit_code} "
        f"timed_out={result.timed_out} cancelled={result.cancelled} "
        f"error={_one_line(result.error or '') or 'none'} -> {why}"
    )


def _salvage_log_payload(
    task: Task,
    run: Run,
    result: AdapterResult,
    handoff: Handoff | None,
    mode: str,
    evidence: dict[str, Any] | None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": mode,
        "task_id": task.id,
        "run_id": run.id,
        "run_status": run.status,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "cancelled": result.cancelled,
        "error": _one_line(result.error or ""),
        "handoff_status": handoff.status if handoff else None,
        "claimed_files": len(handoff.files_changed) if handoff else 0,
        "salvaged": handoff is not None,
    }
    if evidence is not None:
        payload["claims_all_backed_by_this_run_commits"] = evidence[
            "claims_all_backed_by_this_run_commits"
        ]
        payload["claim_counts"] = evidence["claim_counts"]
        payload["commit_count"] = evidence["commit_count"]
        payload["run_commit_count"] = evidence["run_commit_count"]
        payload["prior_run_commit_count"] = evidence["prior_run_commit_count"]
        payload["recovery_commit_count"] = evidence["recovery_commit_count"]
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _store_memory_suggestions(
    session: Session,
    suggestions: list[MemorySuggestion],
    *,
    source: str,
    project_slug: str | None,
    role: str,
    task: Task | None = None,
    run: Run | None = None,
) -> memory_service.SuggestionOutcome:
    """Store a run's memory suggestions, and put every scope the harness rewrote in the log.

    T-4 DoD 1: a suggestion is never stored with a scope no injector can read, and a
    coercion or a drop is an operator-visible event, not a silent `continue`. The notes go
    to the build log as `memory_scope_substituted` / `memory_suggestion_dropped`, one line
    per affected suggestion, so the run's log tells the truth about what was filed where.
    """
    outcome = memory_service.suggest_from_handoff_report(
        session, suggestions, source=source, project_slug=project_slug, role=role
    )
    for note in outcome.notes:
        if note.startswith("dropped "):
            event = "memory_suggestion_dropped"
        elif note.startswith("linked "):
            event = "memory_suggestion_linked"
        else:
            event = "memory_scope_substituted"
        log_service.log_event(
            session,
            event,
            note,
            role="system",
            task_id=task.id if task else None,
            run_id=run.id if run else None,
        )
    return outcome


def try_salvage_handoff(
    session: Session,
    task: Task,
    run: Run,
    project: Project,
    wt: Path,
    handoff_path: Path,
    harness_name: str | None,
    result: AdapterResult,
    uncommitted_before_sweep: list[str],
    stale_snapshot: dict[str, Any] | None = None,
    baseline_sha: str | None = None,
) -> Handoff | None:
    """Ingest a handoff the killed Worker already wrote (T-10 DoD 1 + 2).

    Returns the salvaged Handoff when the worktree holds one that validates, else None — and None
    means "behave exactly as before this task existed": `RUN_TIMEOUT`/`RUN_FAILED` on the task and
    the task back to `planned`. A present-but-unusable file logs `handoff_salvage_failed` instead of
    crashing the run, because a half-written file is the expected shape of a killed process.
    """
    handoff, error = read_handoff_file(handoff_path)
    if stale_snapshot is not None and handoff_snapshot(handoff_path) == stale_snapshot:
        # The round-start archive failed, so the previous round's file was left in place — and the
        # Worker never replaced it. Ingesting it here would attribute a previous round's
        # submission to this run, which is exactly the provenance failure the salvage path exists
        # to avoid. Fail closed: this round produced nothing and is reported as incomplete.
        log_service.log_event(
            session,
            "handoff_salvage_failed",
            structured_log.attach(
                f"{run.short_id} ({run.status}) left a handoff.json in the worktree, but it is "
                "byte-for-byte unchanged since the round started: it is the previous round's "
                "leftover (the round-start archive could not copy it), not this run's submission. "
                "Refusing to salvage it and falling back to the normal non-clean-exit path.",
                _salvage_log_payload(task, run, result, None, SALVAGE_MODE_NON_CLEAN_EXIT, None)
                | {"reason": "unchanged since round start", "stale": True},
            ),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
        return None
    if handoff is None and error is None:
        return None
    if handoff is None:
        log_service.log_event(
            session,
            "handoff_salvage_failed",
            structured_log.attach(
                f"{run.short_id} ({run.status}) left {handoff_path} in the worktree but it could "
                f"not be ingested; falling back to the {run.status} path: {error}",
                _salvage_log_payload(task, run, result, None, SALVAGE_MODE_NON_CLEAN_EXIT, None)
                | {"reason": error},
            ),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
        return None
    evidence = delivery_evidence(
        wt,
        project.base_branch,
        handoff,
        arrival=artifact_service.HANDOFF_ARRIVAL_SALVAGED,
        exit_state=exit_state_text(run, result),
        uncommitted_before_sweep=uncommitted_before_sweep,
        baseline_sha=baseline_sha,
    )
    archive_handoff_artifacts(run, handoff)
    _write_run_json(
        run,
        task,
        harness_name,
        result,
        arrival=artifact_service.HANDOFF_ARRIVAL_SALVAGED,
        evidence=evidence,
    )
    if handoff.memory_suggestions:
        # Pending, never auto-approved: a suggestion from a killed run is no more self-certifying
        # than the handoff it came with, and the Supervisor still reviews it.
        _store_memory_suggestions(
            session,
            handoff.memory_suggestions,
            source=f"salvaged-handoff:{run.short_id}",
            project_slug=project.slug,
            role="worker",
            task=task,
            run=run,
        )
    log_service.log_event(
        session,
        "handoff_salvaged",
        structured_log.attach(
            f"{run.short_id} was killed ({run.status}) after the Worker had already written a "
            f"valid handoff.json to the worktree, out of band: the adapter never returned it on a "
            f"clean exit, so the old code path discarded it. Salvaged into {run.artifacts_path} as "
            f"handoff.json + handoff.md; run.status stays {run.status} and the Supervisor gate "
            f"(not the Worker's own claim) decides the round. Claims backed only by uncommitted "
            f"worktree state: "
            f"{'yes' if not evidence['claims_all_backed_by_this_run_commits'] else 'none'}.",
            _salvage_log_payload(
                task, run, result, handoff, SALVAGE_MODE_NON_CLEAN_EXIT, evidence
            ),
        ),
        role="supervisor",
        task_id=task.id,
        run_id=run.id,
        commit_sha=run.commit_sha,
    )
    return handoff


def _previous_round_run(session: Session, task: Task, run: Run) -> Run | None:
    """The run whose submission a worktree handoff at round start most likely is."""
    for r in task_service.runs_for(session, task):
        if r.id != run.id:
            return r
    return None


def _submission_note(
    session: Session,
    task: Task,
    run: Run,
    *,
    leftover_present: bool,
    archived_before_round: bool,
) -> str | None:
    """State what became of the previous round's submission (T-12 DoD 3).

    The next prompt is built in the round that archives/unlinks the leftover; without this note a
    retry after a timeout looks like a first attempt. T-10 archives before the unlink, so the
    honest wording is "archived at round start", not "deleted".
    """
    prev = _previous_round_run(session, task, run)
    if prev is None:
        if not leftover_present:
            return None
        status = "no previous round run is recorded"
    else:
        prev_status = supervisor_service.submission_status(prev)
        status = f"previous round {prev.short_id} ({prev.status}): {prev_status}"
    lines = [f"Previous round submission: {status}."]
    if leftover_present:
        if archived_before_round:
            lines.append(
                "A handoff.json from that round was still in the worktree at the start of this "
                "round; it was archived to the orchestrator's evidence store before the "
                "round-start unlink, so the worktree copy was removed only after a copy existed."
            )
        else:
            lines.append(
                "A handoff.json from that round was still in the worktree at the start of this "
                "round, but archiving it failed; it was left in place and will not be read as "
                "this round's submission."
            )
    else:
        lines.append("No handoff.json was present in the worktree when this round started.")
    return " ".join(lines)


def _run_baseline_sha(
    session: Session, task: Task, run: Run, wt: Path, base_branch: str
) -> str | None:
    """The branch head this run started from, used to scope claims to THIS run (T-10 DoD 7).

    The previous run's `commit_sha` is already on the runs table and is the honest baseline: it is
    where the last round stopped, so `baseline..HEAD` is what this round created. A first run falls
    back to the merge-base with the base branch (the same scope `git log base..HEAD` has always
    used). A stale or unrelated sha is ignored and the merge-base is used instead, so a force-moved
    branch cannot make old work look new.
    """
    for r in task_service.runs_for(session, task):
        if r.id == run.id or not r.commit_sha:
            continue
        ancestor = run_git(
            ["merge-base", "--is-ancestor", r.commit_sha, "HEAD"], wt, check=False
        )
        if ancestor.code == 0:
            return r.commit_sha
    merged = run_git(["merge-base", base_branch, "HEAD"], wt, check=False)
    return merged.stdout.strip() if merged.code == 0 and merged.stdout.strip() else None


def archive_before_round_start(
    session: Session,
    task: Task,
    run: Run,
    project: Project,
    handoff_path: Path,
) -> bool:
    """Never destroy a pre-existing handoff.json when a round starts (T-10 DoD 3).

    The round-start `unlink(missing_ok=True)` was the second half of the bug: a retry after a
    timeout actively deleted the previous round's submission, so the operator had to copy the file
    out in the gap. Returns True when the caller may unlink — i.e. the submission is provably
    archived (already in `runs/<prev>/handoff.json`, or copied to ORCHESTRATOR_HOME/evidence/).
    On any archive failure it returns False and the file is left alone: keeping a stale handoff in
    the scratch dir is recoverable, losing one is not.
    """
    if not handoff_path.is_file():
        return True
    try:
        raw = handoff_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log_service.log_event(
            session,
            "handoff_salvage_failed",
            structured_log.attach(
                f"{handoff_path} exists but could not be read before the round-start unlink; "
                "leaving it in place (the unlink was skipped so nothing is destroyed)",
                {"task_id": task.id, "run_id": run.id, "reason": str(e)},
            ),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
        return False
    handoff, error = read_handoff_file(handoff_path)
    prev = _previous_round_run(session, task, run)
    if handoff is not None and prev is not None and prev.id is not None:
        archived = artifact_service.read_json(prev.id, "handoff.json")
        if archived == handoff.model_dump(mode="json"):
            # SALVAGE_MODE_ALREADY_ARCHIVED: already archived byte-for-byte in that run's
            # artifacts, so nothing is at risk and nothing is copied. Deliberately not an event —
            # an event here would fire on every ordinary retry and mean nothing.
            return True
    key = (
        f"{project.slug}-{task.short_id}-"
        f"{prev.short_id if prev else 'no-previous-run'}-"
        f"{utcnow().strftime('%Y%m%dT%H%M%S%f')}"
    )
    try:
        dest = artifact_service.evidence_dir(EVIDENCE_KIND_PRE_ROUND, key)
        (dest / "handoff.json").write_text(raw, encoding="utf-8")
        if handoff is not None:
            (dest / "handoff.md").write_text(
                artifact_service.render_handoff_md(handoff), encoding="utf-8"
            )
    except OSError as e:
        log_service.log_event(
            session,
            "handoff_salvage_failed",
            structured_log.attach(
                f"could not archive {handoff_path} to the evidence root before the round-start "
                "unlink; leaving it in place (the unlink was skipped so nothing is destroyed)",
                {"task_id": task.id, "run_id": run.id, "reason": str(e)},
            ),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
        return False
    log_service.log_event(
        session,
        "handoff_salvaged",
        structured_log.attach(
            f"a handoff.json from the previous round was still in the worktree at the start of "
            f"{run.short_id}; archived to {dest} before the round-start unlink "
            f"(source run: {prev.short_id + ' (' + prev.status + ')' if prev else 'unknown'}; "
            f"{'valid handoff' if handoff else 'unusable: ' + (error or 'unknown')}), "
            "so a retry can no longer destroy a submission",
            {
                "mode": SALVAGE_MODE_PRE_ROUND_ARCHIVE,
                "task_id": task.id,
                "run_id": run.id,
                "source_run": prev.short_id if prev else None,
                "source_run_status": prev.status if prev else None,
                "archived_to": str(dest),
                "valid": handoff is not None,
                "parse_error": error,
                "bytes": len(raw),
            },
        ),
        role="supervisor",
        task_id=task.id,
        run_id=run.id,
    )
    return True


async def _gate_salvaged(
    session: Session, task: Task, run: Run, project: Project, handoff: Handoff
) -> dict[str, Any]:
    """Continue to the normal verification + review path carrying a salvaged submission.

    The run's status is already terminal (`timeout`/`failed`/`cancelled`) and the frozen run
    vocabulary has no transition out of a terminal state, so the gate runs WITHOUT moving the run —
    deliberately not `testing`/`completed`: the process really was killed, and pretending otherwise
    would widen the frozen vocabulary and hide the fact that the Worker never saw the gate result.
    `task.handoff_status` becomes `SALVAGED` (distinct from the Worker's own status, from
    `SYNTHESIZED` and from `RUN_*`); see docs/build-loop.md, "Salvaging a handoff from a
    killed run".
    """
    assert run.id is not None
    task.handoff_status = artifact_service.HANDOFF_STATUS_SALVAGED
    session.add(task)
    session.commit()
    if handoff.status == "BLOCKED":
        task_service.block(
            session,
            task,
            f"worker emitted BLOCKED handoff (salvaged from a {run.status} run, so the Worker "
            "never saw this exit): " + "; ".join(handoff.blockers),
            role="supervisor",
        )
        return {
            "run_id": run.id,
            "status": run.status,
            "error": run.error,
            "salvaged": True,
            "decision": "blocked",
            "blockers": handoff.blockers,
        }
    task_service.transition(
        session,
        task,
        TaskStatus.review,
        f"salvaged handoff from a {run.status} run; the Supervisor gate decides on evidence",
        role="supervisor",
    )
    outcome = await supervisor_service.after_run(session, task, run, project, handoff, False)
    return {"run_id": run.id, "status": run.status, "error": run.error, "salvaged": True, **outcome}


async def execute(
    session: Session,
    task: Task,
    harness_override: str | None = None,
    profile_override: str | None = None,
) -> dict[str, Any]:
    """Run the Worker for a task that is already `in_progress` (claimed)."""
    if task.status != TaskStatus.in_progress:
        raise task_service.TaskError(
            f"task {task.short_id} must be in_progress (claimed); it is {task.status}"
        )
    project = _project(session, task)
    run = Run(
        short_id=f"tmp-{uuid.uuid4().hex}",
        task_id=task.id or 0,
        status=RunStatus.pending,
        branch_name=task.branch_name,
    )
    session.add(run)
    session.flush()
    run.short_id = f"R-{run.id}"
    assert run.id is not None
    run.artifacts_path = str(artifact_service.run_dir(run.id))
    session.add(run)
    session.commit()
    log_service.log_event(
        session,
        "run_created",
        run.short_id,
        role="supervisor",
        task_id=task.id,
        run_id=run.id,
        phase=task_service.phase_label(task),
    )

    try:
        return await _execute(session, task, run, project, harness_override, profile_override)
    except Exception as e:  # never leave a run dangling in a non-terminal state
        session.rollback()
        session.refresh(run)
        session.refresh(task)
        if run.status not in ("completed", "failed", "cancelled", "timeout"):
            _set_run(
                session, run, RunStatus.failed, error=redact(str(e))[:2000], finished_at=utcnow()
            )
        if task.status == TaskStatus.in_progress:
            reason = f"run failed: {e}"
            if not supervisor_service.enforce_attempt_limit(session, task, run, reason):
                task_service.transition(
                    session,
                    task,
                    TaskStatus.planned,
                    f"{reason}{supervisor_service.submission_reason_suffix(run)}",
                    role="supervisor",
                )
        log_service.log_event(
            session, "run_error", str(e), role="supervisor", task_id=task.id, run_id=run.id
        )
        return {"run_id": run.id, "status": run.status, "error": str(e)}


async def _execute(
    session: Session,
    task: Task,
    run: Run,
    project: Project | None,
    harness_override: str | None,
    profile_override: str | None,
) -> dict[str, Any]:
    assert run.id is not None and task.id is not None
    started = utcnow()
    _set_run(session, run, RunStatus.preparing, started_at=started)
    # A new round starts with no handoff/review/verification/merge verdict attached to it; the
    # markers describe the PREVIOUS round and would contradict `in_progress` (T-8 DoD 7).
    task_service.clear_round_fields(session, task, reason=f"run {run.short_id} started")

    if task.kind == TaskKind.agent or (task.kind != TaskKind.agent and project is None):
        return await _execute_direct(session, task, run, project, profile_override)

    assert project is not None
    if profile_override:
        task.assigned_provider_profile = profile_override
    profile = provider_service.choose_profile(session, task, Role.worker).profile
    harness_name = (
        harness_override
        or adapter_service.choose_harness(session, task, profile.name, profile.model_name).adapter
    )
    if harness_name is None:
        if task.risk == "low":
            return await _execute_direct(session, task, run, project, profile_override)
        _set_run(
            session,
            run,
            RunStatus.failed,
            error="no suitable harness available",
            finished_at=utcnow(),
        )
        task_service.block(session, task, "no harness available for a non-simple task")
        return {"run_id": run.id, "status": run.status, "decision": "blocked"}

    adapter = adapter_service.get_adapter(session, harness_name)
    if not adapter.spec.enabled:
        raise RuntimeError(f"adapter {harness_name} is disabled")
    if not adapter.detect():
        raise RuntimeError(f"adapter {harness_name}: `{adapter.spec.command}` not found on PATH")

    assert task.branch_name
    wt = git_service.ensure_worktree(
        Path(project.path), project.slug, task.id, task.branch_name, project.base_branch
    )
    if wt.created:
        log_service.log_event(
            session,
            "worktree_created",
            str(wt.path),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
    # Scope delivery evidence to what THIS run creates, not to the whole phase branch. Measure it
    # before the pre-run recovery sweep so this run's own sweep is not attributed to an earlier
    # round (a retry that creates zero commits must not inherit the previous round's delivery).
    baseline_sha = _run_baseline_sha(session, task, run, wt.path, project.base_branch)
    # Supervisor duty: check git status BEFORE (re)trying; recover dirty trees safely. The
    # trailer names the aborted run whose work is being swept, not the run that finds it.
    leftover_of = _last_aborted_run(session, task, run.id)
    sweep_for = (leftover_of or run).short_id
    rec = git_service.recovery_commit_info(
        wt.path, f"leftover changes from {sweep_for}", run_short_id=sweep_for
    )
    if rec:
        log_service.log_event(
            session,
            "recovery_commit",
            git_service.recovery_event_details("dirty worktree before retry", rec),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
            commit_sha=rec.sha,
        )
    handoff_path = wt.path / git_service.ORCH_DIR / "handoff.json"
    handoff_path.parent.mkdir(exist_ok=True)
    # Do NOT just unlink: a submission left over from the previous round is work, and deleting it
    # was the second half of the bug (a retry after a timeout actively destroyed the round it was
    # meant to recover). Archive first, unlink only when the copy provably exists (T-10 DoD 3).
    leftover_present = handoff_path.is_file()
    archived_before_round = archive_before_round_start(session, task, run, project, handoff_path)
    if archived_before_round:
        handoff_path.unlink(missing_ok=True)
    # Only survives that attempt when the archive failed. Remember the exact leftover: if it is
    # still identical when the run ends, it is the previous round's file and must never be read as
    # this round's submission (DoD 3 + DoD 6).
    stale_handoff = handoff_snapshot(handoff_path)

    # A branch that carries harness wreckage must not pass it on silently: the Worker is told
    # which commits are recovery commits and is required to re-verify before handing off.
    recovery_list = git_service.recovery_commits(wt.path, project.base_branch)
    recovery_state = ""
    if recovery_list:
        recovery_state = (
            f"{len(recovery_list)} commit(s) on {task.branch_name} were created by the "
            "orchestrator as `chore(orchestrator-recovery)` recovery commits of leftover "
            "worktree output after an aborted turn. They carry the "
            "`Orchestrator-Recovery:` trailer. Their content was NEVER self-checked by a "
            "Worker: it may contain undefined names, half-finished edits, or edits to frozen "
            "contract files.\n"
            "Before writing handoff.json you MUST:\n"
            "1. re-verify the branch with the project's lint and test commands (run them via "
            "the configured project command, from this worktree), and\n"
            "2. repair or explicitly report every defect these commits introduced, and\n"
            "3. if any of them touched a contract file this phase does not authorize, emit a "
            "BLOCKED handoff with a contract-change request instead of continuing.\n\n"
            + git_service.format_recovery_commit_list(recovery_list)
        )
        log_service.log_event(
            session,
            "recovery_notice_included",
            f"{len(recovery_list)} recovery commit(s) listed in the prompt for {run.short_id}",
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )

    state = DynamicState(
        feedback=_latest_feedback(session, task),
        recovery=recovery_state,
        submission=_submission_note(
            session,
            task,
            run,
            leftover_present=leftover_present,
            archived_before_round=archived_before_round,
        ),
    )
    prompt = prompt_builder.build_prompt(session, Role.worker, task, project, state)
    prompt_text = prompt.static_text() + "\n\n" + prompt.dynamic_text()
    prompt_file = artifact_service.write_text(run.id, "prompt.md", prompt_text)

    ctx = RunContext(
        run_id=run.id,
        task_id=task.id,
        cwd=wt.path,
        prompt_file=prompt_file,
        artifacts_dir=artifact_service.run_dir(run.id),
        handoff_path=handoff_path,
        branch=task.branch_name,
        phase=task_service.phase_label(task),
        tags=task.tags,
        model=profile.model_name if harness_name != "mock" else None,
        timeout_seconds=adapter.spec.timeout_seconds,
        context_window=usage_service.context_window(session, profile.model_name),
    )
    try:
        preview = redact(shlex.join(adapter.command_preview(ctx)))
    except TemplateError as e:
        raise RuntimeError(str(e)) from e
    _set_run(
        session,
        run,
        RunStatus.running,
        harness_adapter=harness_name,
        provider_profile=profile.name,
        worktree_path=str(wt.path),
        branch_name=task.branch_name,
        command_preview=preview,
    )

    result = await adapter.run(ctx, process_service.run_process)
    finished = utcnow()
    duration = int((finished - started).total_seconds() * 1000)
    if result.stdout_path and result.stdout_path.is_file():
        usage = usage_service.harness_usage(
            adapter.spec.kind, result.stdout_path.read_text(errors="replace"), ctx.model
        )
        if usage:
            run.usage = usage  # persisted by the next _set_run commit
            usage_service.record("task", harness_name, ctx.model, usage)
    _write_run_json(run, task, harness_name, result)

    # Collect whatever landed, even on failure (evidence for the next retry). The dirt is
    # snapshotted BEFORE the sweep: after it `git status` is clean by construction, so a Worker that
    # promised work it never committed would leave no trace of the promise (T-10 DoD 7).
    split_before_sweep = git_service.status_split(wt.path)
    dirt_before_sweep = sorted(
        {*split_before_sweep.tracked_modified, *split_before_sweep.untracked_added}
    )
    rec = git_service.recovery_commit_info(
        wt.path, f"uncommitted worker output from {run.short_id}", run_short_id=run.short_id
    )
    if rec:
        log_service.log_event(
            session,
            "recovery_commit",
            git_service.recovery_event_details("worker left uncommitted changes", rec),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
            commit_sha=rec.sha,
        )
    patch, stat = git_service.diff_against_base(wt.path, project.base_branch)
    artifact_service.write_text(run.id, "patch.diff", patch)
    artifact_service.write_text(run.id, "diffstat.txt", stat)
    sha = head_sha(wt.path)

    if result.cancelled:
        _set_run(
            session,
            run,
            RunStatus.cancelled,
            exit_code=result.exit_code,
            error="cancelled",
            finished_at=finished,
            duration_ms=duration,
            commit_sha=sha,
        )
        # T-9 DoD 8: the fourth terminal status gets its named event here, in the cancel branch,
        # immediately after the status is committed and before handoff ingestion. Additive only:
        # no existing event name changed. A consumer (the T-9 fan-out) no longer has to infer a
        # cancellation from a task_status line.
        log_service.log_event(
            session,
            "run_cancelled",
            f"{run.short_id} cancelled (exit {result.exit_code})",
            role="worker",
            task_id=task.id,
            run_id=run.id,
            commit_sha=sha,
        )
        salvaged = try_salvage_handoff(
            session,
            task,
            run,
            project,
            wt.path,
            handoff_path,
            harness_name,
            result,
            dirt_before_sweep,
            stale_snapshot=stale_handoff,
            baseline_sha=baseline_sha,
        )
        if salvaged is None:
            reason = "run cancelled"
            if not supervisor_service.enforce_attempt_limit(session, task, run, reason):
                task_service.transition(
                    session,
                    task,
                    TaskStatus.planned,
                    f"{reason}{supervisor_service.submission_reason_suffix(run)}",
                    role="supervisor",
                )
            return {"run_id": run.id, "status": run.status}
        return await _gate_salvaged(session, task, run, project, salvaged)
    if result.timed_out or result.exit_code != 0 or result.error:
        status = RunStatus.timeout if result.timed_out else RunStatus.failed
        _set_run(
            session,
            run,
            status,
            exit_code=result.exit_code,
            error=result.error or f"exit code {result.exit_code}",
            finished_at=finished,
            duration_ms=duration,
            commit_sha=sha,
        )
        log_service.log_event(
            session, f"run_{status}", run.error or "", role="worker", task_id=task.id, run_id=run.id
        )
        salvaged = try_salvage_handoff(
            session,
            task,
            run,
            project,
            wt.path,
            handoff_path,
            harness_name,
            result,
            dirt_before_sweep,
            stale_snapshot=stale_handoff,
            baseline_sha=baseline_sha,
        )
        if salvaged is None:
            task.handoff_status = "RUN_" + status.upper()
            reason = f"run {status}"
            if not supervisor_service.enforce_attempt_limit(session, task, run, reason):
                task_service.transition(
                    session,
                    task,
                    TaskStatus.planned,
                    f"{reason}{supervisor_service.submission_reason_suffix(run)}",
                    role="supervisor",
                )
            return {"run_id": run.id, "status": run.status, "error": run.error}
        return await _gate_salvaged(session, task, run, project, salvaged)

    stale_unchanged = stale_handoff is not None and handoff_snapshot(handoff_path) == stale_handoff
    if stale_unchanged:
        # A clean exit that never replaced the leftover: do not read the previous round's handoff
        # as this round's work. Synthesize from observed evidence instead. (The archive failure
        # that left the file in place is already logged; this records why it was not ingested.)
        log_service.log_event(
            session,
            "handoff_salvage_failed",
            structured_log.attach(
                f"{run.short_id} exited cleanly but the handoff.json in the worktree is "
                "byte-for-byte unchanged since the round started; it is a previous round's "
                "leftover, not this round's submission, so it is not ingested",
                {
                    "task_id": task.id,
                    "run_id": run.id,
                    "reason": "unchanged since round start",
                    "stale": True,
                },
            ),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
        handoff = None
    else:
        handoff = adapter.parse_result(ctx, result)
    synthesized = handoff is None
    if handoff is None:
        handoff = _synthesize_handoff(
            task,
            run,
            git_service.changed_files(wt.path, project.base_branch),
            task.branch_name,
            result,
        )
    arrival = (
        artifact_service.HANDOFF_ARRIVAL_SYNTHESIZED
        if synthesized
        else artifact_service.HANDOFF_ARRIVAL_CLEAN_EXIT
    )
    archive_handoff_artifacts(run, handoff)
    # The clean-exit path shares the provenance machinery with the salvage path (T-10 DoD 7): the
    # evidence is computed once, archived, and read back by the gate and the reviewer payload.
    _write_run_json(
        run,
        task,
        harness_name,
        result,
        arrival=arrival,
        evidence=delivery_evidence(
            wt.path,
            project.base_branch,
            handoff,
            arrival=arrival,
            exit_state=exit_state_text(run, result, status=RunStatus.completed),
            uncommitted_before_sweep=dirt_before_sweep,
            baseline_sha=baseline_sha,
        ),
    )
    if not synthesized and handoff.memory_suggestions:
        _store_memory_suggestions(
            session,
            handoff.memory_suggestions,
            source=f"handoff:{run.short_id}",
            project_slug=project.slug,
            role="worker",
            task=task,
            run=run,
        )
    task.handoff_status = handoff.status if not synthesized else "SYNTHESIZED"
    session.add(task)
    session.commit()

    if handoff.status == "BLOCKED":
        _set_run(
            session,
            run,
            RunStatus.completed,
            exit_code=result.exit_code,
            finished_at=finished,
            duration_ms=duration,
            commit_sha=sha,
        )
        task_service.block(
            session,
            task,
            "worker emitted BLOCKED handoff: " + "; ".join(handoff.blockers),
            role="supervisor",
        )
        return {
            "run_id": run.id,
            "status": run.status,
            "decision": "blocked",
            "blockers": handoff.blockers,
        }

    _set_run(session, run, RunStatus.testing, exit_code=result.exit_code, commit_sha=sha)
    task_service.transition(session, task, TaskStatus.review, "handoff received", role="supervisor")
    try:
        outcome = await supervisor_service.after_run(
            session, task, run, project, handoff, synthesized
        )
    finally:
        finished = utcnow()
        session.refresh(run)
        if run.status == RunStatus.testing:
            _set_run(
                session,
                run,
                RunStatus.completed,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
            )
    return {"run_id": run.id, "status": run.status, **outcome}


async def _execute_direct(
    session: Session, task: Task, run: Run, project: Project | None, profile_override: str | None
) -> dict[str, Any]:
    """Agent tasks (and harness-less low-risk tasks): call the model directly.
    Output is stored as an artifact; no code is written to any repository."""
    assert run.id is not None
    if profile_override:
        task.assigned_provider_profile = profile_override
    role = Role.agent if task.kind == TaskKind.agent else Role.worker
    choice = provider_service.choose_profile(session, task, role)
    prompt = prompt_builder.build_prompt(
        session, role, task, project, DynamicState(feedback=_latest_feedback(session, task))
    )
    artifact_service.write_text(
        run.id, "prompt.md", prompt.static_text() + "\n\n" + prompt.dynamic_text()
    )
    _set_run(
        session,
        run,
        RunStatus.running,
        provider_profile=choice.profile.name,
        harness_adapter=None,
        command_preview=f"direct API: {choice.profile.model_name}",
    )
    try:
        handoff, res = await provider_service.complete_json(
            session,
            choice.profile,
            prompt,
            Handoff,
            metadata={
                "purpose": "agent",
                "tags": task.tags,
                "task_id": task.id,
                "phase": task_service.phase_label(task),
                "branch": task.branch_name or "",
            },
        )
    except ProviderError as e:
        finished = utcnow()
        _set_run(session, run, RunStatus.failed, error=redact(str(e)), finished_at=finished)
        reason = f"provider error: {e}"
        if not supervisor_service.enforce_attempt_limit(session, task, run, reason):
            task_service.transition(
                session,
                task,
                TaskStatus.planned,
                f"{reason}{supervisor_service.submission_reason_suffix(run)}",
                role="supervisor",
            )
        return {"run_id": run.id, "status": run.status, "error": str(e)}
    artifact_service.write_text(run.id, "stdout.log", res.text)
    artifact_service.write_json(run.id, "handoff.json", handoff.model_dump(mode="json"))
    artifact_service.write_text(run.id, "handoff.md", artifact_service.render_handoff_md(handoff))
    artifact_service.write_text(run.id, "patch.diff", "")
    finished = utcnow()
    run.usage = res.usage
    task.handoff_status = handoff.status
    if handoff.memory_suggestions:
        _store_memory_suggestions(
            session,
            handoff.memory_suggestions,
            source=f"agent:{run.short_id}",
            project_slug=project.slug if project else None,
            role=role,
            task=task,
            run=run,
        )
    _set_run(
        session,
        run,
        RunStatus.completed,
        exit_code=0,
        finished_at=finished,
        duration_ms=int((finished - (run.started_at or finished)).total_seconds() * 1000),
    )
    if handoff.status == "BLOCKED":
        task_service.block(session, task, "agent BLOCKED: " + "; ".join(handoff.blockers))
        return {"run_id": run.id, "status": run.status, "decision": "blocked"}
    task_service.transition(
        session, task, TaskStatus.review, "agent output received", role="supervisor"
    )
    outcome = await supervisor_service.after_run(session, task, run, project, handoff, False)
    return {"run_id": run.id, "status": run.status, **outcome}


async def run_task_now(
    session: Session, task: Task, harness: str | None = None, profile: str | None = None
) -> dict[str, Any]:
    """CLI/API helper: queue if needed, claim this exact task, and execute it."""
    from orchestrator.services import queue_service

    if task.status in (
        TaskStatus.draft,
        TaskStatus.planned,
        TaskStatus.rejected,
        TaskStatus.blocked,
    ):
        task_service.queue(session, task)
    if task.status != TaskStatus.queued:
        raise task_service.TaskError(f"task {task.short_id} is {task.status}; cannot run")
    if not queue_service.claim(session, task.id or 0):
        raise task_service.TaskError(f"task {task.short_id} was claimed by another runner")
    session.refresh(task)
    return await execute(session, task, harness, profile)


def cancel(session: Session, task: Task) -> dict[str, Any]:
    run = task_service.active_run(session, task)
    if run and run.id is not None:
        (artifact_service.run_dir(run.id) / process_service.CANCEL_MARKER).write_text("cancel\n")
        log_service.log_event(
            session,
            "cancel_requested",
            run.short_id,
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
        return {"cancel_requested": run.short_id}
    if task.status == TaskStatus.queued:
        task_service.transition(session, task, TaskStatus.planned, "cancelled before start")
        return {"dequeued": task.short_id}
    if task.status == TaskStatus.in_progress:
        # Claimed but no live run (e.g. crashed runner): release it.
        task_service.transition(session, task, TaskStatus.planned, "released stale claim")
        return {"released": task.short_id}
    raise task_service.TaskError(f"nothing to cancel for {task.short_id} ({task.status})")


def retry(
    session: Session, task: Task, harness: str | None = None, profile: str | None = None
) -> Task:
    if harness:
        task.assigned_harness_adapter = harness
    if profile:
        task.assigned_provider_profile = profile
    task.source = "retry"
    session.add(task)
    session.commit()
    if task.status in (TaskStatus.planned, TaskStatus.rejected, TaskStatus.blocked):
        return task_service.queue(session, task)
    raise task_service.TaskError(f"cannot retry a task in status {task.status}")


def escalate(session: Session, task: Task) -> Task:
    """Escalate: route the next run/review through the architect (Opus) profile."""
    tags = set(task.tags) | {"escalate"}
    task.tags = sorted(tags)
    task.risk = "high"
    task.source = "escalation"
    session.add(task)
    session.commit()
    log_service.log_event(
        session,
        "escalated",
        "risk=high; architect review required",
        role="supervisor",
        task_id=task.id,
    )
    return task


def human_decision(session: Session, task: Task, accept: bool, reason: str) -> dict[str, Any]:
    """Operator accept/reject of a task waiting in `review` (or reject from blocked)."""
    project = _project(session, task)
    if accept:
        run = task_service.latest_run(session, task)
        if task.status == TaskStatus.blocked and run is not None:
            task_service.transition(
                session, task, TaskStatus.review, "operator taking the decision", role="supervisor"
            )
        if task.status != TaskStatus.review or run is None:
            raise task_service.TaskError("only tasks in `review` with a run can be accepted")
        log_service.log_event(
            session, "human_accept", reason or "", role="supervisor", task_id=task.id
        )
        return supervisor_service.accept(session, task, run, project, None, None)
    task_service.transition(
        session, task, TaskStatus.rejected, reason or "rejected by human", role="supervisor"
    )
    return {"decision": "rejected"}


def run_summary(session: Session, run: Run) -> dict[str, Any]:
    data = json.loads(run.model_dump_json())
    data["artifacts"] = artifact_service.list_artifacts(run.id) if run.id else []
    return data
