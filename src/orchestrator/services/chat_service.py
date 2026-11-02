"""Interactive harness sessions: lifecycle, workspace, transcript and live fan-out.

A chat is separate from the task pipeline: it has no supervisor or review gate. The harness
process lives inside the server, so sessions end when the server stops; their transcripts
stay. A transcript is <home>/chats/<id>/events.jsonl, written through secret redaction.
Consecutive text deltas are stored as one event so that redaction sees whole sentences.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import deque
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.adapters.chat.base import ChatDriver, DriverError, DriverOptions
from orchestrator.adapters.chat.registry import HARNESSES, UNSUPPORTED
from orchestrator.core.config import get_settings
from orchestrator.core.db import session_scope
from orchestrator.core.logging import get_logger
from orchestrator.core.models import (
    Agent,
    ChatSession,
    ChatStatus,
    HarnessAdapter,
    ModelProfile,
    Project,
    utcnow,
)
from orchestrator.core.schemas import ChatSessionCreate
from orchestrator.services import (
    adapter_service,
    agent_service,
    git_service,
    settings_service,
)
from orchestrator.utils.git import run_git, status_porcelain
from orchestrator.utils.redaction import redact
from orchestrator.utils.slug import slugify

log = get_logger(__name__)
MAX_LIVE_SESSIONS = 4
MAX_TOOL_TEXT = 20_000
ENDED = {ChatStatus.closed, ChatStatus.failed, ChatStatus.interrupted}
STREAMED = {"text", "thinking"}


class ChatError(ValueError):
    pass


class ChatDisabled(ChatError):
    pass


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        text = redact(value)
        if len(text) > MAX_TOOL_TEXT:
            return f"{text[:MAX_TOOL_TEXT]}\n… truncated ({len(text):,} characters)"
        return text
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _transcript(chat_id: int) -> Path:
    return get_settings().chats_root / str(chat_id) / "events.jsonl"


def history(chat_id: int) -> list[dict[str, Any]]:
    live = manager.get(chat_id)
    if live:
        live.flush()
    path = _transcript(chat_id)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


class LiveSession:
    def __init__(self, chat_id: int, standalone: bool = False, preamble: str = "") -> None:
        self.id = chat_id
        self.standalone = standalone  # nobody is attached; standalone_service answers for it
        self.preamble = preamble  # goes ahead of the first message, and stays off the transcript
        self.driver: ChatDriver | None = None
        self.status: str = ChatStatus.starting
        self.subscribers: set[asyncio.Queue] = set()
        self.pending: deque[str] = deque()
        self.seq = 0
        self.closing = False
        self.exited = False
        self._buffer: dict[str, Any] | None = None
        self._tasks: set[asyncio.Task] = set()
        self._path = _transcript(chat_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # -- events ----------------------------------------------------------------
    def emit(self, event: dict[str, Any]) -> None:
        if event["type"] == "exit":
            self._on_exit(event)
            return
        self.seq += 1
        event = {**_clean(event), "seq": self.seq}
        if event["type"] in STREAMED:
            self._buffer_delta(event)
        else:
            self.flush()
            self._write(event)
        for queue in self.subscribers:
            queue.put_nowait(event)
        self._track(event)  # after fan-out: tracking can emit, and effects follow their cause

    def _buffer_delta(self, event: dict[str, Any]) -> None:
        if self._buffer and self._buffer["type"] != event["type"]:
            self.flush()
        if self._buffer:
            self._buffer["delta"] += event["delta"]
            self._buffer["seq"] = event["seq"]
        else:
            self._buffer = dict(event)

    def flush(self) -> None:
        if self._buffer:
            buffered, self._buffer = self._buffer, None
            self._write({**buffered, "delta": redact(buffered["delta"])})

    def _write(self, event: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def _track(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "status":
            self._on_status(event["status"])
        elif kind in ("approval", "approval_resolved"):
            self._sync_approval_status()
        elif kind == "config":
            fields = {k: event[k] for k in ("model", "effort", "permission") if event.get(k)}
            if fields:
                self._save(**fields)
        elif kind == "usage":
            self._save(usage={k: v for k, v in event.items() if k not in ("type", "seq")})

    def _on_status(self, status: str) -> None:
        self.status = status
        self._save(status=status)
        if status == ChatStatus.idle and self.pending and not self.closing:
            self.spawn(self._send(self.pending.popleft()))

    def _sync_approval_status(self) -> None:
        waiting = bool(self.driver and self.driver.approvals)
        if self.status in (ChatStatus.busy, ChatStatus.awaiting_approval):
            status = ChatStatus.awaiting_approval if waiting else ChatStatus.busy
            if status != self.status:
                self.status = status
                self.emit({"type": "status", "status": status})

    def _save(self, **fields: Any) -> None:
        with session_scope() as s:
            row = s.get(ChatSession, self.id)
            if row:
                for key, value in fields.items():
                    setattr(row, key, value)
                row.updated_at = utcnow()
                s.add(row)
                s.commit()

    def _on_exit(self, event: dict[str, Any]) -> None:
        if self.closing:
            return
        self.exited = True
        detail = f": {event['message']}" if event.get("message") else ""
        message = f"The harness exited (code {event.get('code')}){detail}"
        self.emit({"type": "error", "message": message})
        self._save(error=redact(message))
        self.spawn(self.shutdown(ChatStatus.failed))

    # -- actions ---------------------------------------------------------------
    def spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    async def start(self, driver: ChatDriver) -> None:
        self.driver = driver
        try:
            await driver.start()
        except Exception as e:  # noqa: BLE001 - any startup failure must reach the GUI
            log.warning("chat %s failed to start: %s", self.id, e)
            if not self.exited:  # _on_exit has already reported it
                self.emit({"type": "error", "message": f"Could not start the harness: {e}"})
                self._save(error=redact(str(e)))
            await self.shutdown(ChatStatus.failed)
            return
        self.emit({"type": "status", "status": ChatStatus.idle})

    async def prompt(self, text: str) -> None:
        if not text.strip():
            return
        self.emit({"type": "user", "text": text})
        with session_scope() as s:
            row = s.get(ChatSession, self.id)
            if row and not row.title:
                row.title = redact(" ".join(text.split()))[:80]
                s.add(row)
                s.commit()
        if self.status == ChatStatus.starting:
            self.pending.append(text)
            return
        working = self.status in (ChatStatus.busy, ChatStatus.awaiting_approval)
        if working and not self._can_steer():
            self.pending.append(text)
            self.emit({"type": "notice", "message": "Queued until this turn ends."})
            return
        await self._send(text)

    def _can_steer(self) -> bool:
        return bool(self.driver and self.driver.can_steer)

    async def _send(self, text: str) -> None:
        assert self.driver
        if self.preamble:
            text, self.preamble = f"{self.preamble}\n\nRequest:\n{text}", ""
        try:
            await self.driver.prompt(text)
        except DriverError as e:
            self.emit({"type": "error", "message": str(e)})

    async def interrupt(self) -> None:
        assert self.driver
        self.pending.clear()
        await self.driver.interrupt()

    async def shutdown(self, status: str = ChatStatus.closed) -> None:
        if self.closing:
            return
        self.closing = True
        self.pending.clear()
        manager.discard(self.id)
        if self.driver:
            for approval_id in list(self.driver.approvals):
                self.driver.resolved(approval_id, None)
            await self.driver.close()
        self.emit({"type": "status", "status": status})
        self.flush()
        for queue in self.subscribers:
            queue.put_nowait(None)


class ChatManager:
    def __init__(self) -> None:
        self.live: dict[int, LiveSession] = {}

    def get(self, chat_id: int) -> LiveSession | None:
        return self.live.get(chat_id)

    def discard(self, chat_id: int) -> None:
        self.live.pop(chat_id, None)

    def unattended_count(self) -> int:
        return sum(s.standalone for s in self.live.values())

    async def close_all(self) -> None:
        await asyncio.gather(*(s.shutdown() for s in list(self.live.values())))


manager = ChatManager()


def recover(session: Session) -> None:
    """Sessions can't outlive the server, so any still open from a previous one are over."""
    open_chats = col(ChatSession.status).not_in([s.value for s in ENDED])
    for row in session.exec(select(ChatSession).where(open_chats)):
        if row.id not in manager.live:
            row.status = ChatStatus.interrupted
            session.add(row)
    session.commit()


def _require_enabled(session: Session) -> None:
    if not settings_service.get(session, "allow_interactive_sessions"):
        raise ChatDisabled("interactive sessions are off (allow_interactive_sessions=false)")


def _models_for(session: Session, row: HarnessAdapter) -> list[str]:
    """Enabled profiles whose harness route includes this adapter, workers first, then any
    models the harness always offers."""
    routes = adapter_service.harness_routes(session)
    profiles = session.exec(select(ModelProfile).where(ModelProfile.enabled)).all()
    ranked = sorted(profiles, key=lambda p: p.role != "worker")
    names = [
        p.model_name
        for p in ranked
        if row.name in adapter_service.route_for(routes, p.name, p.model_name)
    ]
    profile = HARNESSES.get(row.kind)
    return list(dict.fromkeys([*names, *(profile.models if profile else [])]))


def harness_problem(session: Session, adapter_name: str) -> str | None:
    """Why this adapter can't run a session right now, or None if it can."""
    row = adapter_service.get_row(session, adapter_name)
    return _reason_unavailable(row, adapter_service.get_adapter(session, adapter_name).detect())


def _reason_unavailable(row: HarnessAdapter, detected: bool) -> str | None:
    if row.kind in UNSUPPORTED:
        return UNSUPPORTED[row.kind]
    if row.kind not in HARNESSES:
        return "This adapter has no interactive driver."
    if not row.enabled:
        return "Disabled. Turn it on in Adapters."
    return None if detected else f"`{row.command}` wasn't found on PATH."


def harnesses(session: Session) -> dict[str, Any]:
    out = []
    for row in session.exec(select(HarnessAdapter).order_by(HarnessAdapter.name)):
        info = adapter_service.describe(row)
        reason = _reason_unavailable(row, info["detected"])
        profile = HARNESSES.get(row.kind)
        models: list[str] = []
        if row.kind == "mock":
            models = ["mock-worker"]
        elif profile and profile.uses_profile_models:
            models = _models_for(session, row)
        out.append(
            {
                "name": row.name,
                "kind": row.kind,
                "available": reason is None,
                "reason": reason,
                "models": models,
                "efforts": profile.efforts if profile else [],
                "default_effort": profile.default_effort if profile else None,
                "permissions": [vars(m) for m in profile.permissions] if profile else [],
                "default_permission": profile.default_permission if profile else None,
                "note": profile.note if profile else None,
            }
        )
    return {
        "enabled": settings_service.get(session, "allow_interactive_sessions"),
        "harnesses": out,
    }


def _workspace(chat: ChatSession, project: Project | None, folder: Path | None) -> None:
    if not project:
        # Inside the chat's own folder, so deleting the session removes it too.
        folder = folder or _transcript(chat.id or 0).parent / "workspace"
        folder.mkdir(parents=True, exist_ok=True)
        chat.cwd = str(folder)
        return
    if chat.workspace == "checkout":
        chat.cwd = project.path
        return
    if chat.workspace == "snapshot":
        chat.cwd = str(
            git_service.ensure_snapshot(
                Path(project.path), project.slug, f"chat-{chat.id}", project.base_branch
            ).path
        )
        return
    branch = f"chat/{chat.id}-{slugify(project.slug)}"
    info = git_service.ensure_worktree(
        Path(project.path), project.slug, f"chat-{chat.id}", branch, project.base_branch
    )
    chat.cwd = str(info.path)
    chat.branch_name = info.branch


async def create(session: Session, body: ChatSessionCreate) -> ChatSession:
    _require_enabled(session)
    if len(manager.live) - manager.unattended_count() >= MAX_LIVE_SESSIONS:
        raise ChatError(f"at most {MAX_LIVE_SESSIONS} sessions can run at once; end one first")
    project = None
    if body.project_id is not None:
        project = session.get(Project, body.project_id)
        if not project:
            raise KeyError(f"project {body.project_id} not found")
    agent = None
    if body.agent_id is not None:
        agent = session.get(Agent, body.agent_id)
        if not agent:
            raise KeyError(f"agent {body.agent_id} not found")
        if not agent.enabled:
            raise ChatError(f"agent {agent.name!r} is disabled")
    row = adapter_service.get_row(session, body.adapter)
    profile = HARNESSES.get(row.kind)
    permission = body.permission or (profile.default_permission if profile else None)
    if profile and permission and permission not in {m.id for m in profile.permissions}:
        raise ChatError(f"{body.adapter} has no permission mode {permission!r}")
    return await open_session(
        session,
        adapter=body.adapter,
        project=project,
        model=body.model,
        effort=body.effort,
        permission=permission,
        workspace=body.workspace if project else "folder",
        agent=agent,
    )


async def open_session(
    session: Session,
    *,
    adapter: str,
    project: Project | None,
    workspace: str,
    model: str | None = None,
    effort: str | None = None,
    permission: str | None = None,
    title: str = "",
    folder: Path | None = None,
    standalone: bool = False,
    output_dir: Path | None = None,
    agent: Agent | None = None,
) -> ChatSession:
    """Create a session row and workspace, and start the harness."""
    row = adapter_service.get_row(session, adapter)
    harness = adapter_service.get_adapter(session, adapter)
    if reason := _reason_unavailable(row, harness.detect()):
        raise ChatError(reason)
    profile = HARNESSES[row.kind]
    permission = permission or profile.default_permission
    effort = effort or profile.default_effort
    if not profile.uses_profile_models:
        model = None
    elif not model:
        known = ["mock-worker"] if row.kind == "mock" else _models_for(session, row)
        model = known[0] if known else None
    chat = ChatSession(
        title=title,
        project_id=project.id if project else None,
        adapter=adapter,
        model=model,
        effort=effort,
        permission=permission,
        workspace=workspace,
        standalone=standalone,
        agent_name=agent.name if agent else None,
        cwd=project.path if project else str(folder or ""),
    )
    session.add(chat)
    session.commit()
    session.refresh(chat)
    assert chat.id is not None
    try:
        _workspace(chat, project, folder)
    except Exception:
        session.delete(chat)
        session.commit()
        raise
    session.add(chat)
    session.commit()
    session.refresh(chat)

    live = LiveSession(
        chat.id, standalone, agent_service.instructions_block(agent) if agent else ""
    )
    manager.live[chat.id] = live
    options = DriverOptions(
        cwd=Path(chat.cwd),
        branch=chat.branch_name or "",
        model=model,
        effort=effort,
        permission=permission,
        models=_models_for(session, row) if profile.uses_profile_models else [],
        output_dir=output_dir,
    )
    live.spawn(live.start(profile.driver(harness, options, live.emit)))
    return chat


def get(session: Session, chat_id: int) -> ChatSession:
    chat = session.get(ChatSession, chat_id)
    if not chat:
        raise KeyError(f"chat session {chat_id} not found")
    return chat


def list_sessions(session: Session) -> list[ChatSession]:
    newest = col(ChatSession.updated_at).desc()
    return list(session.exec(select(ChatSession).order_by(newest).limit(100)))


async def handle(session: Session, live: LiveSession, message: dict[str, Any]) -> None:
    """One message from the GUI."""
    kind = message.get("type")
    if live.standalone:  # nobody steers a standalone run; Stop ends it
        if kind != "interrupt":
            raise ChatError("this is a standalone run, so it can only be stopped")
        await live.shutdown()
        return
    _require_enabled(session)
    if kind == "prompt":
        await live.prompt(str(message.get("text") or ""))
        return
    if live.driver is None or live.status == ChatStatus.starting:
        raise ChatError("the harness is still starting")
    if kind == "interrupt":
        await live.interrupt()
    elif kind == "approval":
        await live.driver.respond(str(message.get("id")), str(message.get("choice")))
    elif kind == "set_model":
        await live.driver.set_model(str(message.get("value")))
        live.emit({"type": "notice", "message": "Model changed. Its prompt cache starts cold."})
    elif kind == "set_effort":
        await live.driver.set_effort(str(message.get("value")))
    elif kind == "set_permission":
        await live.driver.set_permission(str(message.get("value")))
    else:
        raise ChatError(f"unknown message {kind!r}")


async def close(chat_id: int) -> None:
    if live := manager.get(chat_id):
        await live.shutdown()


def _project(session: Session, chat: ChatSession) -> Project:
    project = session.get(Project, chat.project_id or 0)
    if not project:
        raise KeyError(f"project {chat.project_id} not found")
    return project


def changes(session: Session, chat: ChatSession) -> dict[str, Any]:
    if chat.project_id is None:  # a plain folder, with no git to report on
        return {
            "isolated": True,
            "branch": None,
            "base_branch": "",
            "merge_enabled": False,
            "uncommitted": "",
            "diffstat": "",
        }
    project = _project(session, chat)
    path = Path(chat.cwd)
    result: dict[str, Any] = {
        "isolated": chat.workspace == "worktree",
        "branch": chat.branch_name,
        "base_branch": project.base_branch,
        "merge_enabled": settings_service.get(session, "merge_enabled"),
        "uncommitted": "",
        "diffstat": "",
    }
    if chat.workspace == "worktree":
        result["merge_command"] = git_service.merge_command(
            Path(project.path), project.base_branch, chat.branch_name or ""
        )
    if path.is_dir():
        result["uncommitted"] = status_porcelain(path)
        if chat.workspace == "worktree":
            spread = f"{project.base_branch}...HEAD"
            result["diffstat"] = run_git(["diff", "--stat", spread], path).stdout
    return result


def merge(session: Session, chat: ChatSession) -> str:
    """Merge a finished session's branch, behind the same gate as task merges."""
    if chat.workspace != "worktree" or not chat.branch_name:
        raise ChatError("this session has no branch, so there is nothing to merge")
    if manager.get(chat.id or 0):
        raise ChatError("end the session before merging its branch")
    if not settings_service.get(session, "merge_enabled"):
        raise ChatError("merging is off (merge_enabled=false); use the manual merge command")
    project = _project(session, chat)
    path = Path(chat.cwd)
    if path.is_dir():
        git_service.recovery_commit(path, f"chat session {chat.id}")
    message = f"Merge chat session {chat.id}: {chat.title or chat.branch_name}"
    return git_service.merge_no_ff(
        Path(project.path), project.base_branch, chat.branch_name, message
    )


def remove_worktree(project: Project, chat: ChatSession) -> str:
    key = f"chat-{chat.id}"
    info = {
        "path": git_service.worktree_path(project.slug, key),
        "marker": git_service._marker(project.slug, key),
    }
    return git_service.remove_worktree(Path(project.path), info, dry_run=False)


def delete(session: Session, chat: ChatSession) -> None:
    """Remove the transcript and worktree. A dirty worktree stays, and the branch always does."""
    if manager.get(chat.id or 0):
        raise ChatError("end the session before deleting it")
    project = session.get(Project, chat.project_id or 0)
    if chat.workspace in ("worktree", "snapshot") and project:
        remove_worktree(project, chat)
    shutil.rmtree(_transcript(chat.id or 0).parent, ignore_errors=True)
    session.delete(chat)
    session.commit()
