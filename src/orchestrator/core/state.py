"""Task and run state machines. Frozen contract after baseline approval.

All status changes must go through `transition_task` / `transition_run` so illegal
moves are rejected consistently from CLI, API, scheduler and the build loop.
"""

from __future__ import annotations

from orchestrator.core.models import RunStatus as R
from orchestrator.core.models import TaskStatus as T


class InvalidTransition(ValueError):
    def __init__(self, entity: str, src: str, dst: str):
        super().__init__(f"illegal {entity} transition: {src} -> {dst}")
        self.entity, self.src, self.dst = entity, src, dst


TASK_TRANSITIONS: dict[T, set[T]] = {
    T.draft: {T.planned, T.queued, T.archived},
    T.planned: {T.draft, T.queued, T.blocked, T.archived},
    # queued -> planned is "cancel before start".
    T.queued: {T.in_progress, T.planned, T.blocked, T.archived},
    # in_progress -> planned covers cancel and failed/timeout runs (retry re-queues).
    T.in_progress: {T.review, T.blocked, T.planned, T.rejected},
    # review -> queued is CHANGES_REQUESTED: the Worker re-applies on the same branch.
    T.review: {T.accepted, T.rejected, T.queued, T.blocked},
    # blocked -> review lets an operator take the decision (accept/reject) directly.
    T.blocked: {T.queued, T.planned, T.review, T.rejected, T.archived},
    T.rejected: {T.queued, T.planned, T.archived},
    T.accepted: {T.archived},
    T.archived: set(),
}

RUN_TRANSITIONS: dict[R, set[R]] = {
    R.pending: {R.preparing, R.cancelled, R.failed},
    R.preparing: {R.running, R.failed, R.cancelled},
    R.running: {R.testing, R.completed, R.failed, R.cancelled, R.timeout},
    R.testing: {R.completed, R.failed, R.cancelled, R.timeout},
    R.completed: set(),
    R.failed: set(),
    R.cancelled: set(),
    R.timeout: set(),
}

TERMINAL_RUN_STATES = {s for s, nxt in RUN_TRANSITIONS.items() if not nxt}
ACTIVE_RUN_STATES = {R.pending, R.preparing, R.running, R.testing}


def can_transition_task(src: str, dst: str) -> bool:
    return T(dst) in TASK_TRANSITIONS[T(src)]


def can_transition_run(src: str, dst: str) -> bool:
    return R(dst) in RUN_TRANSITIONS[R(src)]


def check_task_transition(src: str, dst: str) -> T:
    if not can_transition_task(src, dst):
        raise InvalidTransition("task", src, dst)
    return T(dst)


def check_run_transition(src: str, dst: str) -> R:
    if not can_transition_run(src, dst):
        raise InvalidTransition("run", src, dst)
    return R(dst)
