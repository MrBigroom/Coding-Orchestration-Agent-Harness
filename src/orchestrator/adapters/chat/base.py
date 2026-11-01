"""Interactive harness sessions: one long-lived CLI process driven over stdin/stdout.

Batch runs go through `adapters.base`; a chat turn is not a run, so drivers live here and
leave that contract alone. Every driver translates its harness's protocol into the same
events, which `services.chat_service` persists and streams to the GUI:

  {"type": "text" | "thinking", "delta": str}
  {"type": "tool_start", "id", "name", "input"}, {"type": "tool_end", "id", "output", "error"}
  {"type": "approval", "id", "title", "detail", "options": [{"id", "label", "tone"}]}
  {"type": "approval_resolved", "id", "choice"}
  {"type": "status", "status": "busy" | "idle"}
  {"type": "usage", ...the shape documented in adapters.common}
  {"type": "config", "model", "effort", "permission", "models", "efforts", "permissions"}
  {"type": "notice" | "error", "message": str}
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.adapters.base import BaseHarnessAdapter, RunContext
from orchestrator.adapters.common import _usage
from orchestrator.core.logging import get_logger
from orchestrator.core.security import child_env
from orchestrator.services.process_service import MAX_LINE_BYTES, _terminate, git_identity_env

log = get_logger(__name__)
Emit = Callable[[dict[str, Any]], None]
RESPONSE_TIMEOUT = 30


class DriverError(RuntimeError):
    pass


@dataclass
class DriverOptions:
    cwd: Path
    branch: str = ""
    model: str | None = None
    effort: str | None = None
    permission: str | None = None
    # Models the session may switch to, for harnesses the orchestrator configures.
    models: list[str] = field(default_factory=list)


class ChatProcess:
    """A harness child speaking JSON lines. Stdout is split on LF only, because JSON
    strings may hold U+2028, and read continuously so the child never stalls on the pipe."""

    def __init__(
        self,
        argv: list[str],
        cwd: Path,
        env_names: list[str],
        extra_env: dict[str, str],
        on_record: Callable[[dict[str, Any]], Awaitable[None]],
        on_exit: Callable[[int | None], Awaitable[None]],
    ):
        self.argv = argv
        self.cwd = cwd
        self.env = child_env(env_names, {**git_identity_env(), **extra_env})
        self.on_record = on_record
        self.on_exit = on_exit
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: deque[str] = deque(maxlen=20)
        self.closing = False
        self._pumps: list[asyncio.Task] = []

    async def start(self) -> None:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(self.cwd),
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group, so tool children die with it
                limit=MAX_LINE_BYTES,
            )
        except (FileNotFoundError, PermissionError) as e:
            raise DriverError(f"failed to start {self.argv[0]!r}: {e}") from e
        self._pumps = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while raw := await self.proc.stdout.readline():
            try:
                record = json.loads(raw)
            except ValueError:
                continue  # stray log output on stdout is not protocol data
            if isinstance(record, dict):
                try:
                    await self.on_record(record)
                except Exception:  # noqa: BLE001 - one bad record must not stop the stream
                    log.exception("chat record handler failed")
        code = await self.proc.wait()
        if not self.closing:
            await self.on_exit(code)

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while raw := await self.proc.stderr.readline():
            if line := raw.decode(errors="replace").strip():
                self.stderr_tail.append(line)

    async def send(self, record: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin or self.proc.stdin.is_closing():
            raise DriverError("the harness process is not running")
        try:
            self.proc.stdin.write(json.dumps(record).encode() + b"\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise DriverError("the harness process is not running") from e

    async def close(self, grace: float = 3) -> None:
        self.closing = True
        proc = self.proc
        if not proc:
            return
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        for sig, wait in ((None, grace), (15, 2), (9, 2)):
            if proc.returncode is not None:
                break
            if sig is not None:
                _terminate(proc, sig)
            try:
                await asyncio.wait_for(proc.wait(), wait)
            except TimeoutError:
                continue
        for pump in self._pumps:
            pump.cancel()


class ChatDriver(ABC):
    kind = "base"
    # True when a prompt sent mid-turn steers the running agent instead of waiting.
    can_steer = False

    def __init__(self, adapter: BaseHarnessAdapter, options: DriverOptions, emit: Emit):
        self.adapter = adapter
        self.options = options
        self.emit = emit
        self.process: ChatProcess | None = None
        self.approvals: dict[str, dict[str, Any]] = {}
        self._waiting: dict[Any, asyncio.Future] = {}  # requests awaiting the harness's reply
        self.usage = _usage(0, 0, 0, 0, total_input=True)
        self._tmp = tempfile.TemporaryDirectory(prefix=f"orch-chat-{self.kind}-")
        self.tmp = Path(self._tmp.name)

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def prompt(self, text: str) -> None: ...

    @abstractmethod
    async def interrupt(self) -> None: ...

    @abstractmethod
    async def respond(self, approval_id: str, choice: str) -> None: ...

    @abstractmethod
    async def set_model(self, model: str) -> None: ...

    @abstractmethod
    async def set_effort(self, effort: str) -> None: ...

    @abstractmethod
    async def set_permission(self, permission: str) -> None: ...

    async def close(self) -> None:
        if self.process:
            await self.process.close()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------------
    def prepared_env(self) -> dict[str, str]:
        """The adapter's managed config (endpoint, key by name), reused from batch runs."""
        prepare = getattr(self.adapter, "prepare", None)
        if prepare is None:
            return {}
        ctx = RunContext(
            run_id=0,
            task_id=0,
            cwd=self.options.cwd,
            prompt_file=self.tmp / "prompt",
            artifacts_dir=self.tmp,
            handoff_path=self.tmp / "handoff.json",
            branch=self.options.branch,
            phase="chat",
            model=self.options.model,
        )
        return prepare(ctx, self.tmp)

    async def spawn(self, argv: list[str], extra_env: dict[str, str]) -> None:
        self.process = ChatProcess(
            argv,
            self.options.cwd,
            self.adapter.child_env_names(),
            extra_env,
            self.on_record,
            self.on_exit,
        )
        await self.process.start()

    @abstractmethod
    async def on_record(self, record: dict[str, Any]) -> None: ...

    async def on_exit(self, code: int | None) -> None:
        tail = " | ".join(self.process.stderr_tail) if self.process else ""
        for future in self._waiting.values():
            if not future.done():
                future.set_exception(DriverError(f"the harness exited: {tail[-500:]}"))
        self.emit({"type": "exit", "code": code, "message": tail[-1500:]})

    def add_usage(self, inp: int, cached: int, out: int, *, total_input: bool) -> None:
        turn = _usage(inp, cached, out, 1, total_input=total_input)
        self.usage = {k: self.usage.get(k, 0) + v for k, v in turn.items()}
        self.emit({"type": "usage", **self.usage})

    def ask(self, approval_id: str, title: str, detail: str, options: list[dict[str, str]]) -> None:
        self.approvals[approval_id] = {"options": options}
        self.emit(
            {
                "type": "approval",
                "id": approval_id,
                "title": title,
                "detail": detail,
                "options": options,
            }
        )

    def resolved(self, approval_id: str, choice: str | None) -> None:
        self.approvals.pop(approval_id, None)
        self.emit({"type": "approval_resolved", "id": approval_id, "choice": choice})


def text_of(content: Any) -> str:
    """Text from the content-block lists every protocol here uses."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return text_of(content.get("content") or content.get("text") or "")
    if isinstance(content, list):
        return "\n".join(t for t in (text_of(c) for c in content) if t)
    return ""
