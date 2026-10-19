"""SQLite engine/session management and Alembic integration.

Schema is owned by Alembic migrations (src/orchestrator/migrations). `create_all`
is never used against a real database after baseline.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from orchestrator.core.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_engines: dict[str, Engine] = {}


def _set_sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def get_engine(db_url: str | None = None) -> Engine:
    url = db_url or get_settings().db_url
    assert url
    if url not in _engines:
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})
        if url.startswith("sqlite"):
            event.listen(engine, "connect", _set_sqlite_pragmas)
        _engines[url] = engine
    return _engines[url]


def dispose_engines() -> None:
    for e in _engines.values():
        e.dispose()
    _engines.clear()


@contextmanager
def session_scope(db_url: str | None = None) -> Iterator[Session]:
    with Session(get_engine(db_url), expire_on_commit=False) as session:
        yield session


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def alembic_config(db_url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url or get_settings().db_url or "")
    return cfg


def upgrade(db_url: str | None = None, revision: str = "head") -> None:
    logging.getLogger("alembic").setLevel(logging.WARNING)
    command.upgrade(alembic_config(db_url), revision)


def downgrade(db_url: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(db_url), revision)


def current_revision(db_url: str | None = None) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with get_engine(db_url).connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
