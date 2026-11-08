"""Deterministic Supervisor: independent verification, gate order, and the merge gate.

The Supervisor never writes feature code. It runs real commands (only the project's
configured test/lint/build commands), compares the evidence with the Worker's handoff,
believes the checks over the story, triggers the Architect review only when tests are
green and a handoff exists, and then accepts / requeues / blocks / merges.
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from orchestrator.core.config import get_settings
from orchestrator.core.models import (
    Project,
    Review,
    ReviewVerdict,
    Run,
    Task,
    TaskKind,
    TaskRisk,
    TaskStatus,
    Verification,
)
from orchestrator.core.schemas import Handoff, VerificationCheck, VerificationResult
from orchestrator.services import (
    artifact_service,
    evidence_freshness,
    git_service,
    log_service,
    process_service,
    project_service,
    review_service,
    settings_service,
    task_service,
)
from orchestrator.utils import structured_log
from orchestrator.utils.git import GitError, head_sha
from orchestrator.utils.truncate import parse_test_counts, summarize_test_output

VERIFY_TIMEOUT = 900

# Names of the harness-computed checks added by T-8. They are deterministic (a file list
# intersected with the contract docs the harness knows about, a git trailer lookup), so a
# finding here is never a model assertion.
CONTRACT_CHECK = "contract files unchanged"
RECOVERY_CHECK = "recovery commits (harness-created)"
# T-10: provenance of the submission itself, read back from the archived `run.json`. This is
# informational: the claims-vs-commits evidence is attached to the artifacts and the reviewer
# payload (DoD 7), not turned into a new gate. Harness recovery commits are content the Worker
# never self-checked, but they are still content on the branch, and T-8 owns the machinery that
# decides what to do about them; T-10 must not turn them into an automatic rejection here.
SALVAGE_CHECK = "handoff arrived out of band (salvaged)"


async def _run_command(
    label: str, command: str | None, cwd: Path, run_id: int
) -> tuple[VerificationCheck, str]:
    if not command:
        return VerificationCheck(
            name=label, status="not_applicable", notes=f"no {label} command configured for project"
        ), ""
    try:
        argv = project_service.command_argv(command)
    except project_service.ProjectError as e:
        return VerificationCheck(name=label, status="fail", command=command, notes=str(e)), ""
    code, output, timed_out = await process_service.run_simple(argv, cwd, VERIFY_TIMEOUT)
    status = "pass" if code == 0 and not timed_out else "fail"
    evidence = f"exit {code}" + (" (timed out)" if timed_out else "")
    return VerificationCheck(name=label, status=status, command=command, evidence=evidence), output


async def verify(
    session: Session,
    task: Task,
    run: Run,
    project: Project | None,
    handoff: Handoff | None,
    handoff_synthesized: bool,
) -> VerificationResult:
    assert run.id is not None
    checks: list[VerificationCheck] = []
    contradicts = False
    phase = task_service.phase_label(task)
    cwd = Path(run.worktree_path) if run.worktree_path else None

    # 1. Handoff present
    if handoff is None or handoff_synthesized:
        checks.append(
            VerificationCheck(
                name="handoff present",
                status="fail",
                evidence="worker did not produce a valid handoff.json",
            )
        )
    else:
        checks.append(
            VerificationCheck(
                name="handoff present", status="pass", evidence=f"status {handoff.status}"
            )
        )

    # 1b. Provenance of the submission itself (T-10). Read back from the archived `run.json`, not
    # re-derived, so the gate, the reviewer payload and a human quoting the artifacts agree. An
    # salvaged handoff is NOT worker self-certification: the run was killed, and the gate below is
    # what decides. Informational — it never approves and never rejects by itself.
    arrival = artifact_service.handoff_arrival(run.id)
    if arrival == artifact_service.HANDOFF_ARRIVAL_SALVAGED:
        evidence0 = artifact_service.handoff_delivery_evidence(run.id) or {}
        checks.append(
            VerificationCheck(
                name=SALVAGE_CHECK,
                status="pass",
                evidence=(
                    f"run.status={run.status} is unchanged; the submission was read off the "
                    "worktree, not returned by the adapter on a clean exit"
                ),
                notes=(
                    f"{evidence0.get('exit_state') or 'exit state unknown'}. The Worker process "
                    "was killed before it could submit, so this handoff is not a self-certified "
                    "completed run: the deterministic checks below and the Architect review decide "
                    "the round, and a green gate routes it to a human instead of auto-accepting it "
                    "(docs/build-loop.md, 'Salvaging a handoff from a killed run')."
                ),
            )
        )

    # 1c. Claims vs. commits (T-10 DoD 7) is deliberately NOT a verification check. The evidence
    # is computed at ingestion, archived in `run.json` and rendered into the reviewer payload; the
    # reviewer and the operator judge it. A claim that exists only as uncommitted worktree state is
    # a promise, and a harness recovery commit is content the Worker never self-checked — both are
    # labelled in that evidence, but neither may become a new automatic gate here: T-8 owns the
    # recovery-commit machinery and DoD 7 asks for provenance, not extra rejection.

    # 2. Claimed files exist on disk
    if cwd and handoff and handoff.files_changed:
        missing = [f for f in handoff.files_changed if not (cwd / f).exists()]
        actual = set(git_service.changed_files(cwd, project.base_branch)) if project else set()
        unclaimed = sorted(actual - set(handoff.files_changed))
        claimed_not_changed = sorted(set(handoff.files_changed) - actual) if project else []
        if missing or claimed_not_changed:
            contradicts = True
        checks.append(
            VerificationCheck(
                name="claimed files exist on disk",
                status="fail" if missing else "pass",
                evidence=f"{len(handoff.files_changed)} claimed, {len(missing)} missing",
                notes="; ".join(
                    filter(
                        None,
                        [
                            f"missing: {missing}" if missing else "",
                            f"claimed but not in diff: {claimed_not_changed}"
                            if claimed_not_changed
                            else "",
                            f"changed but not claimed: {unclaimed}" if unclaimed else "",
                        ],
                    )
                ),
            )
        )
    elif task.kind in (TaskKind.coding, TaskKind.maintenance):
        checks.append(
            VerificationCheck(
                name="claimed files exist on disk",
                status="skipped",
                notes="no files claimed in handoff",
            )
        )

    # 3. Tests (always write test.log so the reviewer sees real evidence or its absence)
    if cwd and project:
        test_check, out = await _run_command("tests", project.test_command, cwd, run.id)
        artifact_service.write_text(run.id, "test.log", out or "no test command configured\n")
        summary = summarize_test_output(
            out, 1500, None if not out else (0 if test_check.status == "pass" else 1)
        )
        artifact_service.write_text(run.id, "test_summary.txt", summary)
        if handoff and project.test_command:
            counts = parse_test_counts(out)
            claimed_green = handoff.tests.failed == 0 and handoff.tests.passed > 0
            if claimed_green and test_check.status == "fail":
                contradicts = True
                test_check.notes = "worker claimed green tests but the independent run failed"
            elif (
                handoff.tests.passed
                and counts["passed"]
                and handoff.tests.passed != counts["passed"]
            ):
                test_check.notes = (
                    f"count mismatch: worker claimed {handoff.tests.passed} "
                    f"passed, independent run saw {counts['passed']}"
                )
            if test_check.status == "pass":
                test_check.evidence += (
                    f"; passed {counts['passed']}, failed {counts['failed']},"
                    f" skipped {counts['skipped']}"
                )
        # T-19 DoD 3: say WHICH world produced this green. The runs/<run_id>/ layout and the
        # verification check-record shape are frozen, so this rides in the existing `evidence`
        # string rather than adding a new field or artifact. A gated worktree is tracked-files-only:
        # no local `.env`, no node_modules. A human comparing a gate pass with a red operator
        # checkout can see from verification.json that the two ran in different environments.
        test_check.evidence += f"; environment: {project_service.environment_summary(cwd)}"
        checks.append(test_check)
        for label, cmd in (("lint", project.lint_command), ("build", project.build_command)):
            c, out = await _run_command(label, cmd, cwd, run.id)
            if out:
                artifact_service.write_text(run.id, f"{label}.log", out)
            checks.append(c)
    else:
        artifact_service.write_text(run.id, "test.log", "no project worktree for this task\n")
        checks.append(
            VerificationCheck(
                name="tests", status="not_applicable", notes="task has no project worktree"
            )
        )

    # 4. Phase-scoped checks recorded as N/A with reasons unless in scope
    tags = set(task.tags)
    checks.append(
        VerificationCheck(
            name="migrations apply/downgrade",
            status="skipped" if "migrations" in tags else "not_applicable",
            notes="migration checks run via the project's test command when tagged `migrations`"
            if "migrations" in tags
            else "migrations not in scope for this phase",
        )
    )
    checks.append(
        VerificationCheck(
            name="endpoints against live stack",
            status="not_applicable",
            notes="no live-endpoint check configured for this phase"
            if "api" not in tags
            else "tag `api` set but no live-check command configured",
        )
    )
    checks.append(
        VerificationCheck(
            name="LLM output coherence",
            status="pass" if task.kind == TaskKind.agent and handoff else "not_applicable",
            notes="agent output parsed as structured JSON"
            if task.kind == TaskKind.agent
            else "phase does not involve LLM behaviour",
        )
    )

    # 4b. Committed evidence freshness (T-15 DoD 1). A pure comparison of the HEAD stamp in each
    # committed evidence file against the run's commit_sha, so the reviewer never has to discover
    # staleness by reading an old copy of the Worker's own tests. The status is `stale` (the fifth,
    # additive value approved as CCR-T15-01), `pass` for a fresh bundle and `not_applicable` when
    # the task has no evidence at all; `fail` is reserved for the strict env flag below.
    # Scope: code-writing kinds only, via the same predicate the Architect review uses, so a finding
    # can never appear in review.json without a matching verification row. This check never sets
    # `contradicts` and never by itself flips the verdict.
    if cwd and project and evidence_freshness.applies_to_kind(task.kind):
        freshness = evidence_freshness.check_freshness(
            cwd, run.commit_sha, task.short_id, project.base_branch
        )
        strict = get_settings().evidence_freshness_strict
        status = evidence_freshness.check_status(freshness, strict)
        stale = freshness.state == evidence_freshness.STATE_STALE
        checks.append(
            VerificationCheck(
                name=evidence_freshness.CHECK_NAME,
                status=status,
                evidence=freshness.evidence,
                notes=(
                    freshness.notes
                    + (
                        "\nstrict mode: staleness is a failing check "
                        "(ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT=true)"
                        if status == "fail"
                        else ""
                    )
                ),
            )
        )
        if stale:
            log_service.log_event(
                session,
                "evidence_freshness_stale",
                structured_log.attach(
                    "committed evidence does not match the commit under verification",
                    {
                        "state": freshness.state,
                        "check_status": status,
                        "task_id": task.id,
                        "run_id": run.id,
                        "commit_sha": run.commit_sha,
                        "compared_commit": freshness.compared_commit,
                        "stale_paths": freshness.stale_paths,
                        "deleted_paths": freshness.deleted_paths,
                        "unparsed_paths": freshness.unparsed_paths,
                        "stamped_files": freshness.stamped_files,
                        "strict": strict,
                    },
                ),
                role="supervisor",
                task_id=task.id,
                run_id=run.id,
                phase=phase,
                commit_sha=run.commit_sha,
            )

    # 5. Harness-computed findings, not model assertions (T-8 DoD 8 + 9). These run from the
    # Supervisor side on purpose: a Worker or reviewer that merely *claims* a contract change was
    # authorized cannot make the claim true — the file list and the stored DoD decide it. Worker
    # tasks only: an `agent`/`review` task answers questions and writes no code, and an
    # Architect decomposition run is *supposed* to write docs/plan.md.
    if task.kind in (TaskKind.coding, TaskKind.maintenance):
        recovery = project_service.recovery_commits_for(session, task, run)
        recovery_paths = {f["path"] for c in recovery for f in c.files}
        findings = project_service.contract_findings(task, project, cwd)
        if findings["unauthorized"]:
            from_recovery = sorted(set(findings["unauthorized"]) & recovery_paths)
            checks.append(
                VerificationCheck(
                    name=CONTRACT_CHECK,
                    status="fail",
                    evidence=f"{len(findings['unauthorized'])} contract file(s) changed",
                    notes=(
                        "changed without a Definition-of-Done item or acceptance criterion naming "
                        "them: " + ", ".join(findings["unauthorized"]) + ". "
                        + (
                            f"{', '.join(from_recovery)} arrived inside a recovery commit, so it "
                            "is content the Worker never self-checked. "
                            if from_recovery
                            else ""
                        )
                        + "Per docs/build-loop.md this phase may not proceed on its own: the "
                        "finding goes to the human (contract-change request flow)."
                    ),
                )
            )
            log_service.log_event(
                session,
                "contract_change_detected",
                structured_log.attach(
                    "unauthorized contract file change(s) in this run",
                    {
                        "unauthorized": findings["unauthorized"],
                        "authorized": findings["authorized"],
                        "contract_files": project_service.contract_files(),
                        "recovery_commit": bool(from_recovery),
                        "from_recovery_commit": from_recovery,
                        "recovery_commits": [c.short for c in recovery],
                        "task_id": task.id,
                        "run_id": run.id,
                    },
                ),
                role="supervisor",
                task_id=task.id,
                run_id=run.id,
                phase=task_service.phase_label(task),
                commit_sha=head_sha(cwd) if cwd else None,
            )
        elif findings["changed"] or project:
            checks.append(
                VerificationCheck(
                    name=CONTRACT_CHECK,
                    status="pass",
                    evidence=(
                        f"{len(findings['changed'])} contract file(s) changed, all authorized"
                        if findings["changed"]
                        else "no contract file changed"
                    ),
                    notes=(", ".join(findings["authorized"]) if findings["authorized"] else ""),
                )
            )

        checks.append(
            VerificationCheck(
                name=RECOVERY_CHECK,
                # Informational: a recovery commit is not itself a rejection reason. It always
                # reaches the reviewer (verification.md is part of the review payload) so nothing
                # is quietly folded into "the Worker's own work".
                status="pass",
                evidence=f"{len(recovery)} recovery commit(s) on this branch",
                notes=(
                    git_service.format_recovery_commit_list(recovery)
                    + "\ncontent was NEVER self-checked by the Worker"
                    if recovery
                    else ""
                ),
            )
        )

    # Computed last, so a harness-detected contract finding fails the verdict too (it is a
    # failing check like any other, not a footnote) — while `after_run` still routes it to the
    # human instead of a generic rework round.
    failed = [c for c in checks if c.status == "fail"]
    final = "fail" if failed or contradicts else "pass"

    result = VerificationResult(
        phase=phase, checks=checks, contradicts_worker=contradicts, final_result=final
    )
    artifact_service.write_json(run.id, "verification.json", result.model_dump(mode="json"))
    artifact_service.write_text(
        run.id, "verification.md", artifact_service.render_verification_md(result)
    )
    session.add(
        Verification(
            task_id=task.id,
            run_id=run.id,
            verifier_kind="supervisor",
            status=final,
            checks_json=[c.model_dump() for c in checks],
            evidence_path=str(artifact_service.run_dir(run.id)),
            contradicts_worker=contradicts,
        )
    )
    task.verification_status = final
    session.add(task)
    session.commit()
    log_service.log_event(
        session,
        "supervisor_verification",
        f"{final}; {len(failed)} failed check(s); contradicts_worker={contradicts}",
        role="supervisor",
        task_id=task.id,
        run_id=run.id,
    )
    return result


# T-12: a review round is a run that reached the gate (`testing` while gated, then `completed`).
# An *attempt* is every run that consumed an adapter window, including timeout/failed/cancelled.
# The old cap counted only successes, so a Worker that never submits was retried forever.
def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def run_attempt_stats(session: Session, task: Task) -> dict[str, int]:
    """Count verified rounds and every attempt from the runs table, not from prose.

    `completed` includes `testing` (the current run is gated then), matching old `_rounds_used`;
    timeout/failed/cancelled runs are attempts that bought no verified round.
    """
    counts = {"completed": 0, "timeout": 0, "failed": 0, "cancelled": 0}
    for r in task_service.runs_for(session, task):
        if r.status in ("completed", "testing"):
            counts["completed"] += 1
        elif r.status in ("timeout", "failed", "cancelled"):
            counts[r.status] += 1
    counts["attempts"] = sum(counts.values())
    counts["verified_rounds"] = counts["completed"]
    return counts


def attempt_summary(session: Session, task: Task) -> dict[str, int | str]:
    """The additive derived field rendered by Tasks/TaskDetail (T-12 DoD 2)."""
    stats = run_attempt_stats(session, task)
    return {
        **stats,
        "max_review_rounds": settings_service.get(session, "max_review_rounds"),
        "max_run_attempts_per_task": settings_service.get(session, "max_run_attempts_per_task"),
        "summary": (
            f"{_plural(stats['attempts'], 'attempt')}, "
            f"{_plural(stats['verified_rounds'], 'verified round')}"
        ),
    }


def _attempt_breakdown(stats: dict[str, int]) -> str:
    return (
        f"attempts={stats['attempts']} (completed={stats['completed']}, "
        f"timeout={stats['timeout']}, failed={stats['failed']}, "
        f"cancelled={stats['cancelled']}); verified rounds={stats['verified_rounds']}"
    )


def _fired_limits(
    stats: dict[str, int], review_limit: int, attempt_limit: int
) -> list[str]:
    fired: list[str] = []
    if stats["verified_rounds"] >= review_limit:
        fired.append(f"review round limit (max_review_rounds={review_limit})")
    if stats["attempts"] >= attempt_limit:
        fired.append(f"run attempt limit (max_run_attempts_per_task={attempt_limit})")
    return fired


def _block_for_limits(
    session: Session,
    task: Task,
    run: Run,
    reason: str,
    stats: dict[str, int],
    fired: list[str],
) -> None:
    which = " and ".join(fired)
    detail = f"{reason}; {which} reached — needs human; {_attempt_breakdown(stats)}"
    limits = ["review_rounds" if f.startswith("review") else "attempts" for f in fired]
    log_service.log_event(
        session,
        "task_limits_exhausted",
        structured_log.attach(
            f"{which} reached; {_attempt_breakdown(stats)}",
            {
                "limits": limits,
                "review_limit": settings_service.get(session, "max_review_rounds"),
                "attempt_limit": settings_service.get(session, "max_run_attempts_per_task"),
                **stats,
                "task_id": task.id,
                "run_id": run.id,
            },
        ),
        role="supervisor",
        task_id=task.id,
        run_id=run.id,
        phase=task_service.phase_label(task),
    )
    task_service.transition(session, task, TaskStatus.blocked, detail, role="supervisor")


def submission_status(run: Run) -> str:
    """How the previous round's submission reached (or failed to reach) its artifacts.

    Read back from `run.json` (`artifact_service.handoff_arrival`), never from Worker prose.
    """
    arrival = artifact_service.handoff_arrival(run.id)
    if arrival == artifact_service.HANDOFF_ARRIVAL_SALVAGED:
        return "salvaged from a killed run"
    if arrival == artifact_service.HANDOFF_ARRIVAL_CLEAN_EXIT:
        return "submitted on clean exit"
    if arrival == artifact_service.HANDOFF_ARRIVAL_SYNTHESIZED:
        return "absent (no usable handoff; the harness synthesized one)"
    return f"absent (run ended {run.status} without a usable handoff)"


def submission_reason_suffix(run: Run) -> str:
    """The submission-status clause every requeue/limit reason carries exactly once (DoD 3)."""
    return (
        f"; previous round submission ({run.short_id}, {run.status}): "
        f"{submission_status(run)}"
    )


def enforce_attempt_limit(session: Session, task: Task, run: Run, reason: str) -> bool:
    """Block a failed/timed-out/cancelled run once it has spent the attempt budget.

    Callers pass the bare exit reason; this function adds the submission clause exactly once.
    Returns True when it blocked, so the caller does not move the task again.
    """
    stats = run_attempt_stats(session, task)
    attempt_limit = settings_service.get(session, "max_run_attempts_per_task")
    if stats["attempts"] >= attempt_limit:
        _block_for_limits(
            session,
            task,
            run,
            f"{reason}{submission_reason_suffix(run)}",
            stats,
            [f"run attempt limit (max_run_attempts_per_task={attempt_limit})"],
        )
        return True
    return False


def _requeue_or_block(session: Session, task: Task, run: Run, reason: str) -> None:
    stats = run_attempt_stats(session, task)
    review_limit = settings_service.get(session, "max_review_rounds")
    attempt_limit = settings_service.get(session, "max_run_attempts_per_task")
    fired = _fired_limits(stats, review_limit, attempt_limit)
    if fired:
        _block_for_limits(
            session, task, run, f"{reason}{submission_reason_suffix(run)}", stats, fired
        )
    else:
        task.source = "retry"
        task_service.transition(
            session,
            task,
            TaskStatus.queued,
            f"{reason}{submission_reason_suffix(run)}; returned to Worker on the same branch",
            role="supervisor",
        )


def contract_change_hold(
    session: Session, task: Task, run: Run, project: Project | None
) -> list[str]:
    """Unauthorized contract-file edits route to the human, they do not buy another round.

    Decided this way (docs/build-loop.md, "Contract-change request flow"): only a human may
    change the product spec, plan or frozen contracts, so a requeue would ask the Worker to fix
    something it is not allowed to fix. Returns the unauthorized paths (empty = no hold).

    The question is recorded on the surfaces that already exist — `review_status=NEEDS_HUMAN`,
    the `verdict_recorded`/`contract_change_detected` build-log events, the verification
    narrative and the blocked transition reason — because Task has no human-question column and
    adding one would be a schema/contract change this phase is not authorized to make."""
    if task.kind not in (TaskKind.coding, TaskKind.maintenance):
        return []
    cwd = Path(run.worktree_path) if run.worktree_path else None
    unauthorized = project_service.contract_findings(task, project, cwd)["unauthorized"]
    if not unauthorized:
        return []
    question = (
        "harness-detected unauthorized contract file change: "
        + ", ".join(unauthorized)
        + ". This run's Definition of Done does not authorize it. Decide: accept the change and "
        "amend the contracts, reject it and redo the phase on a clean base, or authorize it in a "
        "new task (docs/build-loop.md, contract-change request flow)."
    )
    task.review_status = ReviewVerdict.NEEDS_HUMAN
    session.add(task)
    session.commit()
    log_service.log_event(
        session,
        "verdict_recorded",
        f"NEEDS_HUMAN {question} (run {run.short_id})",
        role="supervisor",
        task_id=task.id,
        run_id=run.id,
    )
    task_service.transition(session, task, TaskStatus.blocked, question, role="supervisor")
    return unauthorized


async def after_run(
    session: Session,
    task: Task,
    run: Run,
    project: Project | None,
    handoff: Handoff | None,
    handoff_synthesized: bool,
) -> dict:
    """Gate order after a completed, non-blocked worker run. Task must be in `review`."""
    verification = await verify(session, task, run, project, handoff, handoff_synthesized)
    tests_green = all(c.status != "fail" for c in verification.checks if c.name == "tests")
    outcome: dict = {"verification": verification.final_result}

    if not tests_green or handoff is None:
        _requeue_or_block(
            session, task, run, "tests not green or handoff missing; review not triggered"
        )
        outcome["decision"] = task.status
        return outcome

    # Checked before the reviewer runs: a contract edit is a human decision, and letting a model
    # review it first would let an APPROVED verdict wave the finding through.
    held = contract_change_hold(session, task, run, project)
    if held:
        outcome["decision"] = task.status
        outcome["contract_change"] = held
        return outcome

    needs_architect = (
        task.kind in (TaskKind.coding, TaskKind.maintenance, TaskKind.review)
        or task.risk == TaskRisk.high
    )
    review: Review | None = None
    if needs_architect:
        review = await review_service.architect_review(
            session, task, run, project, handoff_synthesized
        )
        outcome["verdict"] = review.verdict
        if review.verdict == ReviewVerdict.NEEDS_HUMAN:
            task_service.transition(
                session,
                task,
                TaskStatus.blocked,
                "architect verdict NEEDS_HUMAN",
                role="supervisor",
            )
            outcome["decision"] = task.status
            return outcome
        if review.verdict == ReviewVerdict.CHANGES_REQUESTED:
            _requeue_or_block(session, task, run, "architect requested changes")
            outcome["decision"] = task.status
            return outcome

    if verification.final_result != "pass":
        _requeue_or_block(
            session, task, run, "supervisor verification failed (believe the check)"
        )
        outcome["decision"] = task.status
        return outcome

    # Salvaged submission, everything else green. Decision (T-10 DoD 4): `handoff_status=SALVAGED`
    # and the round routes to NEEDS_HUMAN instead of `accepted`. Rationale: the Worker process was
    # killed, so it never saw this gate result and cannot apply reviewer feedback on the same
    # branch, and an early handoff is partly a statement of intent (T-10 DoD 7 measures that). The
    # submission is not lost and the operator is not told the round failed — the round waits for a
    # decision, with handoff/review/verification artifacts attached, and `orchestrator task
    # accept` / the GUI accept button takes it from there (blocked -> review -> accepted, the same
    # path an operator already uses for a contract-change hold). It is never auto-merged.
    if artifact_service.handoff_arrival(run.id) == artifact_service.HANDOFF_ARRIVAL_SALVAGED:
        question = (
            f"salvaged submission from run {run.short_id} ({run.status}): the Worker wrote a valid "
            "handoff.json before the process was killed. Verification passed and the Architect "
            f"review is on file ({review.verdict if review else 'n/a'}), but a killed run cannot "
            "react to feedback and an early handoff can promise more than it delivers — decide "
            "whether this round counts (accept) or goes back to the Worker (retry)."
        )
        task.review_status = ReviewVerdict.NEEDS_HUMAN
        session.add(task)
        session.commit()
        log_service.log_event(
            session,
            "verdict_recorded",
            f"NEEDS_HUMAN {question}",
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
        task_service.transition(session, task, TaskStatus.blocked, question, role="supervisor")
        outcome["decision"] = task.status
        outcome["salvaged"] = True
        return outcome

    outcome |= accept(session, task, run, project, review, verification)
    return outcome


def accept(
    session: Session,
    task: Task,
    run: Run,
    project: Project | None,
    review: Review | None,
    verification: VerificationResult | None,
) -> dict:
    """APPROVED + verification pass: accept, then merge if (and only if) the gate allows."""
    settings = settings_service.get_all(session)
    nits = [
        f"{f.get('file_or_area')}: {f.get('issue')}" for f in (review.findings if review else [])
    ]
    if nits:
        log_service.log_event(
            session,
            "nits_deferred",
            "Deferred in writing: " + " | ".join(nits),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
    task_service.transition(
        session,
        task,
        TaskStatus.accepted,
        "APPROVED and live verification passed" if review else "verification passed",
        role="supervisor",
    )
    result: dict = {"decision": "accepted"}
    if not project or not task.branch_name or not run.worktree_path:
        task.merge_status = "not_applicable"
        session.add(task)
        session.commit()
        return result

    repo = Path(project.path)
    cmd = git_service.merge_command(repo, project.base_branch, task.branch_name)
    # The merge candidate must be exactly what was verified and reviewed.
    current = head_sha(Path(run.worktree_path))
    if run.commit_sha and current != run.commit_sha:
        task.merge_status = "stale"
        session.add(task)
        session.commit()
        log_service.log_event(
            session,
            "merge_skipped",
            "branch moved after verification",
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
        return result | {"merge_command": cmd}

    if settings["merge_enabled"] and settings["auto_merge_enabled"]:
        message = merge_message(task, review, verification, nits)
        try:
            sha = git_service.merge_no_ff(repo, project.base_branch, task.branch_name, message)
            task.merge_status = "merged"
            log_service.log_event(
                session,
                "merged",
                f"{task.branch_name} -> {project.base_branch} (--no-ff)",
                role="supervisor",
                task_id=task.id,
                run_id=run.id,
                commit_sha=sha,
            )
            result |= {"merged": True, "merge_sha": sha}
        except GitError as e:
            task.merge_status = "merge_refused"
            log_service.log_event(
                session, "merge_refused", str(e), role="supervisor", task_id=task.id, run_id=run.id
            )
            result |= {"merged": False, "merge_command": cmd, "merge_error": str(e)}
    else:
        task.merge_status = "ready"
        log_service.log_event(
            session,
            "merge_candidate",
            f"merge gate disabled; run: {cmd}",
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
            commit_sha=run.commit_sha,
        )
        result |= {"merged": False, "merge_command": cmd}
    session.add(task)
    session.commit()
    return result


def merge_message(
    task: Task, review: Review | None, verification: VerificationResult | None, nits: list[str]
) -> str:
    checks = ", ".join(
        f"{c.name}={c.status}" for c in (verification.checks if verification else [])
    )
    return "\n".join(
        [
            f"Merge {task.branch_name}: {task.title}",
            "",
            f"Review verdict: {review.verdict if review else 'n/a'} "
            f"({review.reviewer_model if review else 'no architect review'})",
            f"Live verification: {verification.final_result if verification else 'n/a'} — {checks}",
            "Deferred gaps: " + ("; ".join(nits) if nits else "none"),
            "Regressions found+fixed: see build-log.jsonl for this task",
            f"Task: {task.short_id}",
        ]
    )


def manual_merge(session: Session, task: Task, project: Project) -> str:
    """Human-invoked merge of an accepted task (still requires merge_enabled)."""
    if task.status != TaskStatus.accepted:
        raise GitError("only accepted tasks can be merged")
    if not settings_service.get(session, "merge_enabled"):
        raise GitError(
            "merge gate disabled (merge_enabled=false); run the merge command "
            "yourself: "
            + git_service.merge_command(
                Path(project.path), project.base_branch, task.branch_name or ""
            )
        )
    assert task.branch_name
    sha = git_service.merge_no_ff(
        Path(project.path),
        project.base_branch,
        task.branch_name,
        merge_message(task, None, None, []),
    )
    task.merge_status = "merged"
    session.add(task)
    session.commit()
    log_service.log_event(
        session,
        "merged",
        f"manual merge {task.branch_name}",
        role="supervisor",
        task_id=task.id,
        commit_sha=sha,
    )
    return sha
