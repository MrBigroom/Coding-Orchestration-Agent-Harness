"""Alembic baseline upgrade/downgrade against a populated database."""

import sqlite3

from orchestrator.core import db
from orchestrator.core.schemas import MemoryCreate, ProjectCreate, TaskCreate


def test_upgrade_downgrade_populated_db(initialized, tmp_path):
    from orchestrator.services import memory_service, project_service, task_service

    (tmp_path / "p").mkdir()
    with db.session_scope() as s:
        p = project_service.create(s, ProjectCreate(name="p", path=str(tmp_path / "p")))
        task_service.create(s, TaskCreate(title="t", project_id=p.id, tags=["x"]))
        memory_service.create(
            s, MemoryCreate(category="global", scope="patterns", content="m", approved=True)
        )
    assert db.current_revision() == "0001_baseline"
    db_path = str(initialized / "orchestrator.db")
    db.dispose_engines()

    db.downgrade()
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert tables <= {"alembic_version"}

    db.upgrade()
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        assert {
            "tasks",
            "runs",
            "reviews",
            "verifications",
            "memory_entries",
            "projects",
            "providers",
            "model_profiles",
            "harness_adapters",
            "schedules",
            "settings",
            "build_log_entries",
        } <= tables
    assert db.current_revision() == "0001_baseline"


def test_models_match_migration(initialized):
    """No drift between SQLModel metadata and the migrated schema."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlmodel import SQLModel

    with db.get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    assert diff == [], diff
