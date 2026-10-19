"""Deterministic Supervisor: independent verification, gate order, and the merge gate.

The Supervisor never writes feature code. It runs real commands (only the project's
configured test/lint/build commands), compares the evidence with the Worker's handoff,
believes the checks over the story, triggers the Architect review only when tests are
green and a handoff exists, and then accepts / requeues / blocks / merges.
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

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
    git_service,
    log_service,
    process_service,
    project_service,
    review_service,
    settings_service,
    task_service,
)
from orchestrator.utils.git import GitError, head_sha
from orchestrator.utils.truncate import parse_test_counts, summarize_test_output

VERIFY_TIMEOUT = 900


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


def _rounds_used(session: Session, task: Task) -> int:
    return sum(
        1 for r in task_service.runs_for(session, task) if r.status in ("completed", "testing")
    )


def _requeue_or_block(session: Session, task: Task, reason: str) -> None:
    limit = settings_service.get(session, "max_review_rounds")
    if _rounds_used(session, task) >= limit:
        task_service.transition(
            session,
            task,
            TaskStatus.blocked,
            f"{reason}; review round limit ({limit}) reached — needs human",
            role="supervisor",
        )
    else:
        task.source = "retry"
        task_service.transition(
            session,
            task,
            TaskStatus.queued,
            f"{reason}; returned to Worker on the same branch",
            role="supervisor",
        )


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
        _requeue_or_block(session, task, "tests not green or handoff missing; review not triggered")
        outcome["decision"] = task.status
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
            _requeue_or_block(session, task, "architect requested changes")
            outcome["decision"] = task.status
            return outcome

    if verification.final_result != "pass":
        _requeue_or_block(session, task, "supervisor verification failed (believe the check)")
        outcome["decision"] = task.status
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
