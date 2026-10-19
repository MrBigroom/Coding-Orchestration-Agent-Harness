from orchestrator.core.schemas import TaskCreate
from orchestrator.services import queue_service, settings_service, task_service


def test_claim_is_atomic_and_single_winner(session):
    t = task_service.create(session, TaskCreate(title="a", queue=True))
    assert queue_service.claim(session, t.id) is True
    assert queue_service.claim(session, t.id) is False
    session.refresh(t)
    assert t.status == "in_progress"


def test_claim_next_respects_priority_and_limits(session):
    low = task_service.create(session, TaskCreate(title="low", priority="low", queue=True))
    high = task_service.create(session, TaskCreate(title="high", priority="high", queue=True))
    settings_service.update(session, {"max_parallel_runs": 1})
    first = queue_service.claim_next(session)
    assert first.id == high.id
    assert queue_service.claim_next(session) is None  # global limit reached
    settings_service.update(session, {"max_parallel_runs": 5})
    assert queue_service.claim_next(session).id == low.id
    assert queue_service.claim_next(session) is None  # nothing queued


def test_per_project_limit(session, tmp_path):
    from orchestrator.core.schemas import ProjectCreate
    from orchestrator.services import project_service

    (tmp_path / "p").mkdir()
    p = project_service.create(session, ProjectCreate(name="p", path=str(tmp_path / "p")))
    for i in range(2):
        task_service.create(session, TaskCreate(title=f"t{i}", project_id=p.id, queue=True))
    assert queue_service.claim_next(session) is not None
    assert queue_service.claim_next(session) is None  # per-project default is 1
