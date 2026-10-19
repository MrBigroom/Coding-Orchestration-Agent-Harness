import pytest

from orchestrator.core.models import RunStatus as R
from orchestrator.core.models import TaskStatus as T
from orchestrator.core.state import (
    TASK_TRANSITIONS,
    TERMINAL_RUN_STATES,
    InvalidTransition,
    can_transition_task,
    check_run_transition,
    check_task_transition,
)


@pytest.mark.parametrize(
    "src,dst",
    [
        ("draft", "planned"),
        ("planned", "queued"),
        ("queued", "in_progress"),
        ("in_progress", "review"),
        ("review", "accepted"),
        ("review", "queued"),
        ("in_progress", "blocked"),
        ("blocked", "queued"),
        ("rejected", "queued"),
        ("accepted", "archived"),
        ("in_progress", "planned"),
        ("blocked", "review"),
    ],
)
def test_allowed_task_transitions(src, dst):
    assert check_task_transition(src, dst) == T(dst)


@pytest.mark.parametrize(
    "src,dst",
    [
        ("draft", "accepted"),
        ("planned", "accepted"),
        ("queued", "accepted"),
        ("accepted", "queued"),
        ("archived", "planned"),
        ("review", "in_progress"),
        ("blocked", "accepted"),
    ],
)
def test_illegal_task_transitions(src, dst):
    with pytest.raises(InvalidTransition):
        check_task_transition(src, dst)


def test_accept_only_reachable_from_review():
    sources = {s for s, nxt in TASK_TRANSITIONS.items() if T.accepted in nxt}
    assert sources == {T.review}


def test_archived_is_terminal():
    assert not any(can_transition_task("archived", s) for s in T)


def test_run_transitions_and_terminals():
    assert check_run_transition("pending", "preparing") == R.preparing
    assert check_run_transition("running", "timeout") == R.timeout
    with pytest.raises(InvalidTransition):
        check_run_transition("completed", "running")
    assert TERMINAL_RUN_STATES == {R.completed, R.failed, R.cancelled, R.timeout}
