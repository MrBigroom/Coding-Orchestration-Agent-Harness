from orchestrator.core.db import session_scope
from orchestrator.core.models import Setting
from orchestrator.core.schemas import ProjectCreate, TaskCreate
from orchestrator.services import project_service, settings_service, sync_service, task_service


def test_scan_assigns_stable_uuid_and_detects_local_changes(initialized, tmp_path):
    (tmp_path / "project").mkdir()
    with session_scope() as session:
        project = project_service.create(
            session, ProjectCreate(name="Portable", path=str(tmp_path / "project"))
        )
        first = sync_service.scan_local(session)
        mapping = sync_service.get_mapping(session, "project", str(project.id))
        assert first["new"] > 0 and first["pending"] > 0
        assert mapping is not None and mapping.dirty is True
        assert mapping.version == 1 and mapping.synced_version == 0
        uuid = mapping.uuid

        sync_service.mark_synced(session, mapping)
        second = sync_service.scan_local(session)
        assert second["new"] == 0 and second["changed"] == 0
        assert sync_service.get_mapping(session, "project", str(project.id)).uuid == uuid

        project.notes = "changed on this device"
        session.add(project)
        session.commit()
        third = sync_service.scan_local(session)
        mapping = sync_service.get_mapping(session, "project", str(project.id))
        assert third["changed"] == 1
        assert mapping.uuid == uuid and mapping.version == 2
        assert mapping.dirty is True and mapping.synced_version == 1


def test_payloads_use_uuids_and_exclude_device_paths(initialized, tmp_path):
    (tmp_path / "project").mkdir()
    with session_scope() as session:
        project = project_service.create(
            session, ProjectCreate(name="Portable", path=str(tmp_path / "project"))
        )
        task = task_service.create(
            session, TaskCreate(title="Synchronize", project_id=project.id)
        )
        sync_service.scan_local(session)
        project_mapping = sync_service.get_mapping(session, "project", str(project.id))

        project_payload = sync_service.canonical_payload(
            session, sync_service.SPEC_BY_TYPE["project"], project
        )
        task_payload = sync_service.canonical_payload(
            session, sync_service.SPEC_BY_TYPE["task"], task
        )
        assert "path" not in project_payload
        assert "project_id" not in task_payload
        assert task_payload["project_ref"] == project_mapping.uuid


def test_device_unsafe_settings_are_not_synchronized(initialized):
    with session_scope() as session:
        settings_service.update(
            session,
            {
                "worker_enabled": False,
                "allow_gui_commands": True,
                "supabase_sync_enabled": True,
                "supabase_sync_interval_seconds": 30,
                "theme": "light",
            },
        )
        sync_service.scan_local(session)
        assert sync_service.get_mapping(session, "setting", "worker_enabled") is None
        assert sync_service.get_mapping(session, "setting", "allow_gui_commands") is None
        assert sync_service.get_mapping(session, "setting", "supabase_sync_enabled") is None
        assert (
            sync_service.get_mapping(session, "setting", "supabase_sync_interval_seconds") is None
        )
        assert sync_service.get_mapping(session, "setting", "theme") is not None
        assert session.get(Setting, "worker_enabled").value_json is False


def test_device_id_is_stable(initialized):
    with session_scope() as session:
        first = sync_service.scan_local(session)["device_id"]
        second = sync_service.scan_local(session)["device_id"]
        assert first == second
        assert sync_service.get_state(session, "device_id") == first
