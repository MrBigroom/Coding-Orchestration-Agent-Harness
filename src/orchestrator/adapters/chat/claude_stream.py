from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

from orchestrator.adapters.chat.base import (
    RESPONSE_TIMEOUT,
    ChatDriver,
    DriverError,
    Image,
    slash_commands,
    text_of,
)
from orchestrator.adapters.common import _n

ALWAYS = "always"
READ_TOOLS = {"Read", "Grep", "Glob", "LS"}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit"}


class ClaudeDriver(ChatDriver):
    """Claude Code's stream-json mode (`claude -p --input-format stream-json`), the protocol
    its SDKs use. `--permission-prompt-tool stdio` sends each permission prompt here as a
    `can_use_tool` control request. Every Claude model takes images.

    Runs on the signed-in subscription: like the batch adapter, ANTHROPIC_API_KEY is not
    passed through. Effort has no live control, so changing it restarts the process
    between turns with `--resume`, which keeps the conversation.
    """

    kind = "claude_cli"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._ids = itertools.count(1)
        self._session_id: str | None = None
        self._busy = False
        self._interrupted = False
        self._model = self.options.model

    def _argv(self) -> list[str]:
        argv = [self.adapter.spec.command, "-p", "--verbose"]
        argv += ["--input-format", "stream-json", "--output-format", "stream-json"]
        argv += ["--include-partial-messages", "--permission-prompt-tool", "stdio"]
        if self.options.permission:
            argv += ["--permission-mode", self.options.permission]
        if self._model:
            argv += ["--model", self._model]
        if self.options.effort:
            argv += ["--effort", self.options.effort]
        if self._session_id:
            argv += ["--resume", self._session_id]
        return argv

    async def start(self) -> None:
        await self.spawn(self._argv(), {})
        init = await self._control("initialize")
        self.emit(
            {
                "type": "config",
                "model": self._model,
                "effort": self.options.effort,
                "permission": self.options.permission,
                "images": True,
                # What this mode accepts: skills, custom and plugin commands, and built-ins
                # such as /compact; interactive-only ones aren't listed.
                "commands": slash_commands(init.get("commands")),
            }
        )

    async def _control(self, subtype: str, **fields: Any) -> dict[str, Any]:
        assert self.process
        request_id = f"req-{next(self._ids)}"
        future = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        try:
            await self.process.send(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": subtype, **fields},
                }
            )
            return await asyncio.wait_for(future, RESPONSE_TIMEOUT)
        except TimeoutError as e:
            tail = " | ".join(self.process.stderr_tail)
            raise DriverError(f"Claude did not answer {subtype}: {tail[-500:]}") from e
        finally:
            self._waiting.pop(request_id, None)

    async def prompt(self, text: str, images: list[Image] | None = None) -> None:
        assert self.process
        self._busy = True
        self.emit({"type": "status", "status": "busy"})
        content: str | list[dict[str, Any]] = text
        if images:
            content = [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": i.mime, "data": i.data},
                }
                for i in images
            ]
            if text:
                content.append({"type": "text", "text": text})
        await self.process.send(
            {
                "type": "user",
                "session_id": self._session_id or "",
                "parent_tool_use_id": None,
                "message": {"role": "user", "content": content},
            }
        )

    async def interrupt(self) -> None:
        self._interrupted = True
        for approval_id in list(self.approvals):
            await self._answer(
                approval_id,
                {"behavior": "deny", "message": "Interrupted by the user.", "interrupt": True},
                None,
            )
        await self._control("interrupt")

    async def respond(self, approval_id: str, choice: str) -> None:
        pending = self.approvals.get(approval_id)
        if not pending:
            raise DriverError("that approval is no longer pending")
        if choice == "deny":
            answer: dict[str, Any] = {"behavior": "deny", "message": "Denied by the user."}
        else:
            answer = {"behavior": "allow", "updatedInput": pending["input"]}
            if choice == ALWAYS:
                answer["updatedPermissions"] = pending["suggestions"]
        await self._answer(approval_id, answer, choice)

    async def _answer(self, approval_id: str, answer: dict[str, Any], choice: str | None) -> None:
        assert self.process
        await self.process.send(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": approval_id, "response": answer},
            }
        )
        self.resolved(approval_id, choice)

    async def set_model(self, model: str) -> None:
        await self._control("set_model", model=model)
        self._model = model
        self.emit({"type": "config", "model": model})

    async def set_effort(self, effort: str) -> None:
        if self._busy:
            raise DriverError("effort can change between turns; stop or wait for this one")
        assert self.process
        await self.process.close()
        self.options.effort = effort
        await self.spawn(self._argv(), {})
        await self._control("initialize")
        self.emit({"type": "config", "effort": effort})

    async def set_permission(self, permission: str) -> None:
        await self._control("set_permission_mode", mode=permission)
        self.options.permission = permission
        self.emit({"type": "config", "permission": permission})

    async def on_record(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "stream_event":
            self._on_stream(record.get("event") or {})
        elif kind == "assistant":
            for block in (record.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    self.emit(
                        {
                            "type": "tool_start",
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input"),
                        }
                    )
        elif kind == "user":
            for block in (record.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.emit(
                        {
                            "type": "tool_end",
                            "id": block.get("tool_use_id"),
                            "output": text_of(block.get("content")),
                            "error": bool(block.get("is_error")),
                        }
                    )
        elif kind == "system" and record.get("subtype") == "init":
            self._session_id = record.get("session_id")
            self._model = record.get("model") or self._model
            self.emit({"type": "config", "model": self._model})
        elif kind == "result":
            self._on_result(record)
        elif kind == "control_request":
            self._on_control_request(record)
        elif kind == "control_response":
            self._on_control_response(record.get("response") or {})
        elif kind == "control_cancel_request":
            if (request_id := record.get("request_id")) in self.approvals:
                self.resolved(request_id, None)

    def _on_stream(self, event: dict[str, Any]) -> None:
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            self.emit({"type": "text", "delta": delta.get("text", "")})
        elif delta.get("type") == "thinking_delta":
            self.emit({"type": "thinking", "delta": delta.get("thinking", "")})

    def _on_result(self, record: dict[str, Any]) -> None:
        usage = record.get("usage")
        if isinstance(usage, dict):
            self.add_usage(
                _n(usage.get("input_tokens")) + _n(usage.get("cache_creation_input_tokens")),
                _n(usage.get("cache_read_input_tokens")),
                _n(usage.get("output_tokens")),
                total_input=False,
            )
        if record.get("is_error") and not self._interrupted:
            self.emit({"type": "error", "message": str(record.get("result") or "Claude failed")})
        self._busy = False
        self._interrupted = False
        self.emit({"type": "status", "status": "idle"})

    def _on_control_request(self, record: dict[str, Any]) -> None:
        request = record.get("request") or {}
        if request.get("subtype") != "can_use_tool":
            return
        approval_id = record["request_id"]
        tool_input = request.get("input") or {}
        suggestions = request.get("permission_suggestions") or []
        options = [{"id": "allow", "label": "Allow", "tone": "allow"}]
        if suggestions:
            options.append({"id": ALWAYS, "label": "Always allow", "tone": "allow"})
        options.append({"id": "deny", "label": "Deny", "tone": "deny"})
        tool = request.get("tool_name") or "tool"
        action = {"kind": "read" if tool in READ_TOOLS else "other", "paths": []}
        if tool in EDIT_TOOLS:
            action = {"kind": "write", "paths": [str(tool_input.get("file_path") or "")]}
        self.ask(approval_id, f"Allow {tool}?", json.dumps(tool_input, indent=2), options, action)
        self.approvals[approval_id].update(input=tool_input, suggestions=suggestions)

    def _on_control_response(self, response: dict[str, Any]) -> None:
        future = self._waiting.get(response.get("request_id") or "")
        if not future or future.done():
            return
        if response.get("subtype") == "success":
            future.set_result(response.get("response") or {})
        else:
            future.set_exception(DriverError(response.get("error") or "Claude refused the request"))
