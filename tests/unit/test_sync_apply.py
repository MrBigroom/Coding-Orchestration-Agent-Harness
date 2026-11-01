from uuid import uuid4

from sqlmodel import select

from orchestrator.core.db import session_scope
from orchestrator.core.models import MemoryEntry, Project, Task
from orchestrator.services import sync_service


def _project_payload(name="Remote", notes=None):
    return {
        "name": name,
        "slug": name.lower(),
        "base_branch": "main",
        "test_command": None,
        "lint_command": None,
        "build_command": None,
        "allowed_commands": [],
        "notes": notes,
        "created_at": "2026-10-28T10:00:00Z",
        "updated_at": "2026-10-28T10:00:00Z",
    }


def test_remote_apply_creates_related_rows_without_device_paths(initialized):
    project_id = str(uuid4())
    task_id = str(uuid4())
    records = [
        {
            "entity_type": "task",
            "entity_id": task_id,
            "version": 1,
            "origin_device": "other-device",
            "payload": {
                "short_id": "T-remote",
                "title": "Downloaded task",
                "project_ref": project_id,
            },
        },
        {
            "entity_type": "project",
            "entity_id": project_id,
            "version": 1,
            "origin_device": "other-device",
            "payload": _project_payload(),
        },
    ]
    with session_scope() as session:
        result = sync_service.apply_remote_records(session, records)
        project = session.exec(select(Project).where(Project.slug == "remote")).one()
        task = session.exec(select(Task).where(Task.short_id == "T-remote")).one()
        assert result["applied"] == 2 and result["errors"] == 0
        assert project.path == ""
        assert task.project_id == project.id
        assert session.get(sync_service.SyncEntity, project_id).local_id == str(project.id)
        assert session.get(sync_service.SyncEntity, task_id).local_id == str(task.id)


def test_concurrent_remote_edit_becomes_conflict(initialized):
    entity_id = str(uuid4())
    remote = {
        "entity_type": "project",
        "entity_id": entity_id,
        "version": 1,
        "origin_device": "other-device",
        "payload": _project_payload(),
    }
    with session_scope() as session:
        sync_service.apply_remote_records(session, [remote])
        project = session.exec(select(Project).where(Project.slug == "remote")).one()
        project.notes = "local edit"
        session.add(project)
        session.commit()
        sync_service.scan_local(session)

        changed = {**remote, "version": 2, "payload": _project_payload(notes="remote edit")}
        result = sync_service.apply_remote_records(session, [changed])
        mapping = session.get(sync_service.SyncEntity, entity_id)
        assert result["conflicts"] == 1
        assert project.notes == "local edit"
        assert mapping.conflict_json["version"] == 2
        assert mapping.dirty is True


def test_matching_seeded_memory_is_adopted_not_duplicated(initialized):
    with session_scope() as session:
        sync_service.scan_local(session)
        memory = session.exec(select(MemoryEntry)).first()
        payload = sync_service.canonical_payload(
            session, sync_service.SPEC_BY_TYPE["memory_entry"], memory
        )
        before = len(session.exec(select(MemoryEntry)).all())
        remote_id = str(uuid4())
        result = sync_service.apply_remote_records(
            session,
            [
                {
                    "entity_type": "memory_entry",
                    "entity_id": remote_id,
                    "version": 1,
                    "origin_device": "other-device",
                    "payload": payload,
                }
            ],
        )
        assert result["adopted"] == 1
        assert len(session.exec(select(MemoryEntry)).all()) == before
        mapping = session.get(sync_service.SyncEntity, remote_id)
        assert mapping.local_id == str(memory.id) and mapping.dirty is False
