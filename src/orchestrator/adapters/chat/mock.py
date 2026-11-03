from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from orchestrator.adapters.chat.base import ChatDriver, DriverError, Image

OPTIONS = [
    {"id": "allow", "label": "Allow", "tone": "allow"},
    {"id": "deny", "label": "Deny", "tone": "deny"},
]
COMMANDS = [
    {"name": "review", "description": "Review the working tree"},
    {"name": "summarize", "description": "Summarize the session so far"},
]


class MockDriver(ChatDriver):
    """Offline harness for demos and tests. Each prompt thinks, asks to write a file into
    the workspace, writes it if allowed, and answers. No process and no model."""

    kind = "mock"
    images = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._turn: asyncio.Task | None = None
        self._answer: asyncio.Future | None = None
        self._turns = 0
        self._session_id = ""

    @property
    def _saved(self) -> Path | None:
        """Its "conversation": the turn count, kept so Continue can restore it."""
        return self.options.state_dir / "mock-session.json" if self.options.state_dir else None

    async def start(self) -> None:
        if self.options.resume:
            saved = (
                json.loads(self._saved.read_text()) if self._saved and self._saved.is_file() else {}
            )
            if saved.get("id") != self.options.resume:
                raise DriverError("the mock harness has no saved conversation to continue")
            self._turns = saved["turns"]
        self._session_id = self.options.resume or (f"mock-{uuid.uuid4()}" if self._saved else "")
        self.emit(
            {
                "type": "config",
                "model": self.options.model,
                "effort": self.options.effort,
                "permission": self.options.permission,
                "images": True,
                "commands": COMMANDS,
                "harness_session": self._session_id or None,
            }
        )

    async def prompt(self, text: str, images: list[Image] | None = None) -> None:
        self.emit({"type": "status", "status": "busy"})
        if images:
            text = f"{text} [{len(images)} image(s)]".strip()
        self._turn = asyncio.create_task(self._run(text))

    async def _run(self, text: str) -> None:
        self._turns += 1
        name = f"mock-chat-{self._turns}.md"
        try:
            self.emit({"type": "thinking", "delta": "Planning a small file change."})
            await asyncio.sleep(0.05)
            allowed = self.options.permission == "yolo"
            if not allowed:
                self._answer = asyncio.get_running_loop().create_future()
                action = {"kind": "write", "paths": [name]}
                self.ask("write-1", "Allow write?", name, OPTIONS, action)
                allowed = await self._answer == "allow"
            self.emit({"type": "tool_start", "id": name, "name": "write", "input": {"path": name}})
            if allowed:
                (self.options.cwd / name).write_text(f"{text}\n")
            output = "wrote the file" if allowed else "denied"
            self.emit({"type": "tool_end", "id": name, "output": output, "error": not allowed})
            for word in f"Mock harness received: {text}".split(" "):
                self.emit({"type": "text", "delta": f"{word} "})
                await asyncio.sleep(0.01)
            self.add_usage(100, 40, 20, total_input=True, context=100)
            if self._saved:
                self._saved.parent.mkdir(parents=True, exist_ok=True)
                self._saved.write_text(json.dumps({"id": self._session_id, "turns": self._turns}))
        finally:
            self.emit({"type": "status", "status": "idle"})

    async def interrupt(self) -> None:
        if "write-1" in self.approvals:
            self.resolved("write-1", None)
        if self._turn:
            self._turn.cancel()

    async def respond(self, approval_id: str, choice: str) -> None:
        if approval_id not in self.approvals or not self._answer:
            raise DriverError("that approval is no longer pending")
        self.resolved(approval_id, choice)
        self._answer.set_result(choice)

    async def set_model(self, model: str) -> None:
        self.emit({"type": "config", "model": model})

    async def set_effort(self, effort: str) -> None:
        self.emit({"type": "config", "effort": effort})

    async def set_permission(self, permission: str) -> None:
        self.options.permission = permission
        self.emit({"type": "config", "permission": permission})

    async def on_record(self, record: dict[str, Any]) -> None:
        return None
