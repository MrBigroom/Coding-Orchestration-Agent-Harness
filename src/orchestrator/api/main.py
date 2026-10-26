"""FastAPI application: REST API under /api, WebSocket under /ws, GUI at /."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from orchestrator import __version__
from orchestrator.core.config import get_settings
from orchestrator.core.logging import configure_logging, get_logger
from orchestrator.core.state import InvalidTransition
from orchestrator.providers.base import ProviderError
from orchestrator.utils.git import GitError
from orchestrator.utils.paths import UnsafePathError

log = get_logger(__name__)
PACKAGE_DIR = Path(__file__).resolve().parent
FALLBACK_DIR = PACKAGE_DIR / "static_fallback"


def frontend_dist_dir() -> Path | None:
    s = get_settings()
    candidates = [s.frontend_dist] if s.frontend_dist else []
    candidates += [PACKAGE_DIR.parents[2] / "web" / "dist", PACKAGE_DIR / "static_dist"]
    for c in candidates:
        if c and (Path(c) / "index.html").is_file():
            return Path(c)
    return None


class Background:
    """Queue worker + scheduler living inside the server process."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task] = set()
        self.loop_task: asyncio.Task | None = None
        self.scheduler = None

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def worker_loop(self) -> None:
        from orchestrator.core.db import session_scope
        from orchestrator.services import queue_service, settings_service

        while True:
            try:
                with session_scope() as s:
                    enabled = settings_service.get(s, "worker_enabled")
                    task = queue_service.claim_next(s) if enabled else None
                if task is not None:
                    self.spawn(self._execute(task.id))
                    continue
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("worker loop error")
            await asyncio.sleep(2)

    async def _execute(self, task_id: int) -> None:
        from orchestrator.core.db import session_scope
        from orchestrator.services import run_service, task_service

        with session_scope() as s:
            task = task_service.get(s, task_id)
            try:
                await run_service.execute(s, task)
            except Exception:  # noqa: BLE001
                log.exception("run for task %s crashed", task_id)


background = Background()


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_logging(s.log_level)
    s.ensure_dirs()
    from orchestrator.core import db

    if db.current_revision() is None:
        from orchestrator.services import bootstrap

        bootstrap.init()
    if not s.is_local_bind:
        log.warning(
            "!!! Orchestrator bound to %s — reachable beyond localhost. Token auth is %s.",
            s.bind_host,
            "ON" if s.token else "OFF",
        )
    if s.enable_worker:
        background.loop_task = asyncio.create_task(background.worker_loop())
    if s.enable_scheduler:
        from orchestrator.services.scheduler_service import SchedulerRunner

        background.scheduler = SchedulerRunner()
        background.scheduler.start()
    yield
    if background.loop_task:
        background.loop_task.cancel()
        with suppress(asyncio.CancelledError):
            await background.loop_task
    if background.scheduler:
        background.scheduler.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="coding-orchestrator",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )

    def err(status: int, error: str, detail=None) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": error, "detail": detail})

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        return err(e.status_code, str(e.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        return err(422, "validation error", jsonable(e.errors()))

    @app.exception_handler(InvalidTransition)
    async def _transition(_: Request, e: InvalidTransition):
        return err(409, str(e))

    @app.exception_handler(UnsafePathError)
    async def _path(_: Request, e: UnsafePathError):
        return err(400, "unsafe path", str(e))

    @app.exception_handler(GitError)
    async def _git(_: Request, e: GitError):
        return err(409, "git error", str(e))

    @app.exception_handler(ProviderError)
    async def _provider(_: Request, e: ProviderError):
        return err(502, "provider error", str(e))

    @app.exception_handler(KeyError)
    async def _key(_: Request, e: KeyError):
        return err(404, e.args[0] if e.args else "not found")

    @app.exception_handler(ValueError)
    async def _value(_: Request, e: ValueError):
        return err(400, str(e))

    from orchestrator.api.routes import (
        agents,
        artifacts,
        core,
        memory,
        providers,
        schedules,
        tasks,
        ws,
    )

    for module in (core, tasks, artifacts, providers, schedules, memory, agents):
        app.include_router(module.router, prefix="/api")
    app.include_router(ws.router)

    dist = frontend_dist_dir()
    root = dist or FALLBACK_DIR
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "ws/")):
            raise HTTPException(404, "not found")
        candidate = (root / path).resolve()
        if path and candidate.is_file() and root.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(root / "index.html")

    return app


def jsonable(obj):
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(obj, custom_encoder={Exception: str})


app = create_app()
