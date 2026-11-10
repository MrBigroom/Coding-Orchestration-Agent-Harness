"""Interactive harness sessions: lifecycle, workspace, transcript and live fan-out.

A chat is separate from the task pipeline: it has no supervisor or review gate. The harness
process lives inside the server, so sessions end when the server stops; their transcripts
stay. A transcript is <home>/chats/<id>/events.jsonl, written through secret redaction;
images sent with a prompt are kept in images/ beside it, and the transcript names them.
Other files are saved in the session's working directory, under .orchestrator/attachments/
(git-excluded), and the message tells the agent where; it opens them with its own tools.
Consecutive text deltas are stored as one event so that redaction sees whole sentences.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.adapters.chat.base import ChatDriver, DriverError, DriverOptions, Image
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
from orchestrator.core.schemas import ChatSessionCreate, MemorySuggestion
from orchestrator.services import (
    adapter_service,
    agent_service,
    git_service,
    memory_service,
    settings_service,
    usage_service,
)
from orchestrator.utils.git import is_repo, run_git, status_porcelain
from orchestrator.utils.redaction import redact
from orchestrator.utils.slug import slugify
from orchestrator.utils.truncate import truncate_tail

log = get_logger(__name__)
MAX_LIVE_SESSIONS = 4
MAX_TOOL_TEXT = 20_000
ENDED = {ChatStatus.closed, ChatStatus.failed, ChatStatus.interrupted}
STREAMED = {"text", "thinking"}
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
IMAGE_NAME = re.compile(r"\d+\.(png|jpg|gif|webp)")
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5_000_000
MAX_DIFF_CHARS = 400_000  # the Changes panel's diff; bigger ones are cut
MAX_NEW_FILES = 50  # untracked files shown as new in that diff
REPLAY_TOKENS = 24_000  # the newest part of a transcript replayed to a fresh harness
REPLAY_INTRO = (
    "This session continues an earlier conversation in this same workspace. The harness "
    "restarted and couldn't restore it, so here is the transcript so far (tool output "
    "left out). Pick up where it left off."
)
REPLAY_NOTICE = (
    "Continued. This harness can't restore its conversation, so your next message brings "
    "the transcript so far."
)
MAX_FILES = 5
MAX_FILE_BYTES = 5_000_000
# Images and files together. Base64 grows this by a third, which keeps a prompt under
# uvicorn's 16 MiB WebSocket limit.
MAX_ATTACHMENTS_TOTAL = 10_000_000
ATTACHMENTS = PurePath(git_service.ORCH_DIR, "attachments")
UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


MAX_MEMORY_SUGGESTIONS = 5
MEMORY_REQUEST = """Before we go on: look back over this conversation and suggest up to \
{limit} things worth remembering for future work. Pick durable facts, preferences, \
conventions, decisions or gotchas, not details that only matter to this one task. Don't use \
any tools; answer from the conversation alone.

Reply with only a JSON array in a ```json block. Each item is \
{{"category": ..., "scope": ..., "content": "...", "tags": [...]}}:
- "global", with scope "environment", "preferences" or "patterns"
- "role", with scope "architect", "worker" or "supervisor"
{project}
Write each content as one or two self-contained sentences. Reply with [] if nothing is \
worth keeping."""
MEMORY_PROJECT = (
    '- "project" (this session\'s project), with scope "conventions", "decisions" or "gotchas"'
)
MEMORY_NO_PROJECT = '- no "project" items: this session has no project'
JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ChatError(ValueError):
    pass


@dataclass
class File:
    name: str  # safe to use as a file name
    data: bytes


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


def _images_dir(chat_id: int) -> Path:
    return _transcript(chat_id).parent / "images"


def image_path(chat_id: int, name: str) -> Path | None:
    if not IMAGE_NAME.fullmatch(name):
        return None
    path = _images_dir(chat_id) / name
    return path if path.is_file() else None


def _decode(data: Any, what: str) -> bytes:
    try:
        return base64.b64decode(str(data or ""), validate=True)
    except ValueError as e:
        raise ChatError(f"{what} wasn't valid base64") from e


def _safe_name(name: Any) -> str:
    stem = UNSAFE_NAME.sub("-", PurePath(str(name or "")).name).strip(".-")
    if len(stem) > 80:
        base, dot, ext = stem.rpartition(".")
        stem = f"{base[: 79 - len(ext)]}.{ext}" if dot and len(ext) <= 10 else stem[:80]
    return stem or "file"


def parse_attachments(raw_images: Any, raw_files: Any) -> tuple[list[Image], list[File]]:
    """Images ([{"data": base64, "mime"}]) and files ([{"name", "data": base64}]) sent with
    a prompt, checked against the limits."""
    images: list[Image] = []
    files: list[File] = []
    total = 0
    if raw_images:
        if not isinstance(raw_images, list) or len(raw_images) > MAX_IMAGES:
            raise ChatError(f"send at most {MAX_IMAGES} images at a time")
        for item in raw_images:
            mime = item.get("mime") if isinstance(item, dict) else None
            if not isinstance(mime, str) or mime not in IMAGE_TYPES:
                raise ChatError("images must be PNG, JPEG, GIF or WebP")
            size = len(_decode(item.get("data"), "an image"))
            if not size or size > MAX_IMAGE_BYTES:
                raise ChatError(f"each image must be under {MAX_IMAGE_BYTES // 1_000_000} MB")
            total += size
            images.append(Image(data=str(item["data"]), mime=mime))
    if raw_files:
        if not isinstance(raw_files, list) or len(raw_files) > MAX_FILES:
            raise ChatError(f"send at most {MAX_FILES} files at a time")
        for item in raw_files:
            if not isinstance(item, dict):
                raise ChatError("a file was missing its name or contents")
            data = _decode(item.get("data"), "a file")
            if len(data) > MAX_FILE_BYTES:
                raise ChatError(f"each file must be under {MAX_FILE_BYTES // 1_000_000} MB")
            total += len(data)
            files.append(File(name=_safe_name(item.get("name")), data=data))
    if total > MAX_ATTACHMENTS_TOTAL:
        raise ChatError(
            f"attachments in one message must total under {MAX_ATTACHMENTS_TOTAL // 1_000_000} MB"
        )
    return images, files


def parse_memory_suggestions(reply: str) -> list[MemorySuggestion] | None:
    """The suggestions in an agent's reply to MEMORY_REQUEST, or None if it has no JSON
    array. Items that don't fit the shape are dropped."""
    blocks = JSON_BLOCK.findall(reply)
    candidates = [blocks[-1]] if blocks else []
    if "[" in reply and "]" in reply:
        candidates.append(reply[reply.index("[") : reply.rindex("]") + 1])
    for candidate in candidates:
        try:
            items = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(items, list):
            continue
        suggestions = []
        for item in items[:MAX_MEMORY_SUGGESTIONS]:
            try:
                suggestions.append(MemorySuggestion.model_validate(item))
            except ValueError:
                continue
        return suggestions
    return None


def _state_dir(chat_id: int) -> Path:
    """Where a harness keeps the conversation that Continue restores."""
    return _transcript(chat_id).parent / "harness"


def _last_seq(chat_id: int) -> int:
    path = _transcript(chat_id)
    if not path.is_file():
        return 0
    lines = path.read_text().splitlines()
    return json.loads(lines[-1]).get("seq", 0) if lines else 0


def replay(chat_id: int) -> str:
    """The transcript as text, for a harness that can't restore its own conversation. Tool
    output and thinking are left out; the agent can look at the files again."""
    parts: list[str] = []
    reply = ""
    for event in history(chat_id):
        kind = event["type"]
        if kind == "text":
            reply += event.get("delta", "")
            continue
        if reply.strip():
            parts.append(f"Assistant: {reply.strip()}")
        reply = ""
        if kind == "user":
            extra = [f"{len(event['images'])} image(s)"] if event.get("images") else []
            extra += event.get("files") or []
            note = f" [attached: {', '.join(extra)}]" if extra else ""
            parts.append(f"User: {event.get('text', '')}{note}")
        elif kind == "tool_start":
            parts.append(f"(used {event.get('name') or 'a tool'})")
    if reply.strip():
        parts.append(f"Assistant: {reply.strip()}")
    return truncate_tail("\n\n".join(parts), REPLAY_TOKENS)


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
        self.pending: deque[tuple[str, list[Image]]] = deque()
        self.seq = _last_seq(chat_id)  # a continued session appends to its transcript
        self.closing = False
        self.exited = False
        # While a harness tries to restore its conversation, its errors and exit are the
        # fallback's business rather than the session's.
        self._restoring = False
        self._buffer: dict[str, Any] | None = None
        self._memory_from: int | None = None  # seq where a memory-suggestion turn began
        self._tasks: set[asyncio.Task] = set()
        self._path = _transcript(chat_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # -- events ----------------------------------------------------------------
    def emit(self, event: dict[str, Any]) -> None:
        if self._restoring and event["type"] in ("exit", "error", "status"):
            return
        if event["type"] == "exit":
            self._on_exit(event)
            return
        if event["type"] == "usage" and not event.get("context_window"):
            event = {**event, "context_window": self._context_window()}
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
            keys = ("model", "effort", "permission", "harness_session")
            fields = {k: event[k] for k in keys if event.get(k)}
            if fields:
                self._save(**fields)
            if event.get("model") and self.driver and self.driver.usage.get("requests"):
                self.emit({"type": "usage", **self.driver.usage})  # the new model's window
        elif kind == "usage":
            self._on_usage(event)

    def _on_usage(self, event: dict[str, Any]) -> None:
        """Keeps the running totals on the row, and logs the turn for the Usage page."""
        with session_scope() as s:
            row = s.get(ChatSession, self.id)
            if not row:
                return
            row.usage = {k: v for k, v in event.items() if k not in ("type", "seq", "turn")}
            row.updated_at = utcnow()
            s.add(row)
            s.commit()
            if turn := event.get("turn"):
                source = "standalone" if self.standalone else "chat"
                usage_service.record(source, row.adapter, row.model, turn)

    def _context_window(self) -> int | None:
        with session_scope() as s:
            row = s.get(ChatSession, self.id)
            return usage_service.context_window(s, row.model) if row else None

    def _on_status(self, status: str) -> None:
        self.status = status
        self._save(status=status)
        if status == ChatStatus.idle and self._memory_from is not None:
            self._collect_memory_suggestions()
        if status == ChatStatus.idle and self.pending and not self.closing:
            self.spawn(self._send(*self.pending.popleft()))

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

    async def start(
        self, driver: ChatDriver, fallback: Callable[[], ChatDriver] | None = None
    ) -> None:
        """`fallback` makes the driver a restore attempt: if it can't start, the fallback's
        driver starts instead."""
        if fallback:
            self.driver, self._restoring = driver, True
            try:
                await driver.start()
            except Exception as e:  # noqa: BLE001 - every failure falls back
                log.info("chat %s couldn't restore its conversation: %s", self.id, e)
                await driver.close()
                message = (
                    f"The harness couldn't restore its conversation ({e}). It starts fresh, "
                    "and your next message brings the transcript so far."
                )
            else:
                message = "Continued. The harness restored its conversation."
                fallback = None
            finally:
                self._restoring = False
            self.emit({"type": "notice", "message": message})
            if fallback is None:
                self.emit({"type": "status", "status": ChatStatus.idle})
                return
            self.exited = False
            driver = fallback()
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

    async def prompt(
        self,
        text: str,
        images: list[Image] | None = None,
        files: list[File] | None = None,
        shown: str | None = None,
    ) -> None:
        """`shown` replaces `text` in the transcript, for requests the GUI makes itself."""
        images, files = images or [], files or []
        if not text.strip() and not images and not files:
            return
        if images and not (self.driver and self.driver.images):
            raise ChatError("this model doesn't take images, so the message wasn't sent")
        event: dict[str, Any] = {"type": "user", "text": shown if shown is not None else text}
        if images:
            event["images"] = self._store(images)
        if files:
            event["files"] = paths = self._store_files(files)
            listed = "\n".join(f"- {p}" for p in paths)
            text = f"{text}\n\nAttached files, saved in your working directory:\n{listed}".strip()
        self.emit(event)
        with session_scope() as s:
            row = s.get(ChatSession, self.id)
            if row and not row.title:
                fallback = PurePath(event["files"][0]).name if files else "Image"
                row.title = redact(" ".join(event["text"].split()))[:80] or fallback
                s.add(row)
                s.commit()
        if self.status == ChatStatus.starting:
            self.pending.append((text, images))
            return
        working = self.status in (ChatStatus.busy, ChatStatus.awaiting_approval)
        if working and not self._can_steer():
            self.pending.append((text, images))
            self.emit({"type": "notice", "message": "Queued until this turn ends."})
            return
        await self._send(text, images)

    def _store(self, images: list[Image]) -> list[str]:
        """Keeps the images beside the transcript, which names them instead of holding them."""
        folder = _images_dir(self.id)
        folder.mkdir(exist_ok=True)
        start = len(list(folder.iterdir()))
        names = []
        for n, image in enumerate(images, start + 1):
            name = f"{n}.{IMAGE_TYPES[image.mime]}"
            (folder / name).write_bytes(base64.b64decode(image.data))
            names.append(name)
        return names

    def _store_files(self, files: list[File]) -> list[str]:
        """Saves the files where the agent works, numbered so names never collide, and
        returns their paths relative to that folder."""
        with session_scope() as s:
            row = s.get(ChatSession, self.id)
            cwd = Path(row.cwd) if row else None
        if cwd is None or not cwd.is_dir():
            raise ChatError("this session has no working directory to save files in")
        if is_repo(cwd):
            git_service._ensure_excluded(cwd)
        folder = cwd / ATTACHMENTS
        folder.mkdir(parents=True, exist_ok=True)
        start = len(list(folder.iterdir()))
        paths = []
        for n, file in enumerate(files, start + 1):
            name = f"{n}-{file.name}"
            (folder / name).write_bytes(file.data)
            paths.append(str(ATTACHMENTS / name))
        return paths

    async def suggest_memories(self) -> None:
        """Asks the agent, in this session, for memory entries worth keeping. They're stored
        unapproved, like a task handoff's suggestions, once its reply is in."""
        if self.standalone:
            raise ChatError("a standalone run can't be asked for memory suggestions")
        if self.status != ChatStatus.idle:
            raise ChatError("wait for the agent to finish this turn, then ask again")
        project = MEMORY_PROJECT if self._project_slug() else MEMORY_NO_PROJECT
        request = MEMORY_REQUEST.format(limit=MAX_MEMORY_SUGGESTIONS, project=project)
        self._memory_from = self.seq
        await self.prompt(request, shown="Suggest memories from this session")

    def _project_slug(self) -> str | None:
        with session_scope() as s:
            row = s.get(ChatSession, self.id)
            project = s.get(Project, row.project_id) if row and row.project_id else None
            return project.slug if project else None

    def _collect_memory_suggestions(self) -> None:
        start, self._memory_from = self._memory_from, None
        self.flush()
        reply = "".join(
            e.get("delta", "")
            for e in history(self.id)
            if e["type"] == "text" and e["seq"] > (start or 0)
        )
        suggestions = parse_memory_suggestions(reply)
        if suggestions is None:
            self.emit({"type": "notice", "message": "No memory suggestions could be read."})
            return
        with session_scope() as s:
            outcome = memory_service.suggest_from_handoff_report(
                s, suggestions, source=f"chat:{self.id}", project_slug=self._project_slug()
            )
            added = outcome.created
        if added:
            n = len(added)
            message = f"Added {n} memory suggestion{'s' if n > 1 else ''} to review on Memory."
        elif outcome.linked:
            message = (
                f"Linked {len(outcome.linked)} suggestion"
                f"{'s' if len(outcome.linked) != 1 else ''} to existing memory."
            )
        else:
            message = "No new memory suggestions."
        self.emit({"type": "notice", "message": message})
        # T-4/T-13: a rewritten scope, a duplicate link or a dropped suggestion is said out
        # loud here as well as in the log — the alternative is a silent orphan row.
        for note in outcome.notes:
            self.emit({"type": "notice", "message": f"Memory: {note}"})

    def _can_steer(self) -> bool:
        return bool(self.driver and self.driver.can_steer)

    async def _send(self, text: str, images: list[Image] | None = None) -> None:
        assert self.driver
        if self.preamble:
            text, self.preamble = f"{self.preamble}\n\nRequest:\n{text}", ""
        try:
            await self.driver.prompt(text, images)
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


def _context_windows(session: Session, models: list[str | None]) -> dict[str, int]:
    found = {m: usage_service.context_window(session, m) for m in models if m}
    return {m: w for m, w in found.items() if w}


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
        state_dir=None if standalone else _state_dir(chat.id),
    )
    options.context_windows = _context_windows(session, [options.model, *options.models])
    live.spawn(live.start(profile.driver(harness, options, live.emit)))
    return chat


CONTINUABLE = {ChatStatus.closed, ChatStatus.interrupted}


async def continue_session(session: Session, chat_id: int) -> ChatSession:
    """Start an ended session's harness again in the same workspace. The harness restores
    its own conversation when it can; otherwise the next message carries the transcript."""
    _require_enabled(session)
    chat = get(session, chat_id)
    if manager.get(chat_id):
        raise ChatError("this session is already running")
    if chat.standalone:
        raise ChatError("a standalone run can't be continued; run the agent again")
    if chat.status not in CONTINUABLE:
        raise ChatError(f"a {chat.status} session can't be continued")
    if len(manager.live) - manager.unattended_count() >= MAX_LIVE_SESSIONS:
        raise ChatError(f"at most {MAX_LIVE_SESSIONS} sessions can run at once; end one first")
    project = session.get(Project, chat.project_id) if chat.project_id else None
    if chat.project_id and not project:
        raise ChatError("its project was removed, so there's no workspace to continue in")
    if project and chat.workspace == "worktree" and not Path(chat.cwd).is_dir():
        _workspace(chat, project, None)  # cleaned up; the branch is kept, so bring it back
    if not Path(chat.cwd).is_dir():
        raise ChatError(f"its working folder no longer exists: {chat.cwd}")
    row = adapter_service.get_row(session, chat.adapter)
    harness = adapter_service.get_adapter(session, chat.adapter)
    if reason := _reason_unavailable(row, harness.detect()):
        raise ChatError(reason)
    profile = HARNESSES[row.kind]
    agent = None
    if chat.agent_name:
        agent = session.exec(select(Agent).where(Agent.name == chat.agent_name)).first()
    chat.status = ChatStatus.starting
    chat.error = None
    chat.updated_at = utcnow()
    session.add(chat)
    session.commit()
    session.refresh(chat)

    live = LiveSession(chat_id)
    manager.live[chat_id] = live
    live.emit({"type": "status", "status": ChatStatus.starting})
    options = DriverOptions(
        cwd=Path(chat.cwd),
        branch=chat.branch_name or "",
        model=chat.model if profile.uses_profile_models else None,
        effort=chat.effort,
        permission=chat.permission,
        models=_models_for(session, row) if profile.uses_profile_models else [],
        state_dir=_state_dir(chat_id),
        resume=chat.harness_session,
    )
    options.context_windows = _context_windows(session, [options.model, *options.models])
    earlier = replay(chat_id)
    instructions = agent_service.instructions_block(agent) if agent else ""

    def fresh() -> ChatDriver:
        live.preamble = "\n\n".join(
            p
            for p in (
                instructions,
                f"{REPLAY_INTRO}\n<transcript>\n{earlier}\n</transcript>" if earlier else "",
            )
            if p
        )
        return profile.driver(harness, replace(options, resume=None), live.emit)

    if chat.harness_session:
        live.spawn(live.start(profile.driver(harness, options, live.emit), fallback=fresh))
    else:
        live.emit({"type": "notice", "message": REPLAY_NOTICE})
        live.spawn(live.start(fresh()))
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
        images, files = parse_attachments(message.get("images"), message.get("files"))
        await live.prompt(str(message.get("text") or ""), images, files)
        return
    if live.driver is None or live.status == ChatStatus.starting:
        raise ChatError("the harness is still starting")
    if kind == "interrupt":
        await live.interrupt()
    elif kind == "suggest_memories":
        await live.suggest_memories()
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
            "diff": "",
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
        "diff": "",
    }
    if chat.workspace == "worktree":
        result["merge_command"] = git_service.merge_command(
            Path(project.path), project.base_branch, chat.branch_name or ""
        )
    if path.is_dir():
        result["uncommitted"] = status_porcelain(path)
        # Against the branch point for a worktree (committed and uncommitted work), against
        # HEAD in the project's own checkout, plus new files git doesn't track yet.
        since = "HEAD"
        if chat.workspace == "worktree":
            spread = f"{project.base_branch}...HEAD"
            result["diffstat"] = run_git(["diff", "--stat", spread], path).stdout
            fork = run_git(["merge-base", project.base_branch, "HEAD"], path, check=False)
            since = fork.stdout.strip() or since
        diff = run_git(["diff", since], path, check=False).stdout
        new = run_git(["ls-files", "--others", "--exclude-standard", "-z"], path, check=False)
        for name in [n for n in new.stdout.split("\0") if n][:MAX_NEW_FILES]:
            if len(diff) > MAX_DIFF_CHARS:
                break
            # --no-index exits 1 when the files differ, which they always do here.
            added = run_git(["diff", "--no-index", "--", "/dev/null", name], path, check=False)
            diff += added.stdout
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + f"\n… diff truncated at {MAX_DIFF_CHARS:,} characters\n"
        result["diff"] = redact(diff)
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
