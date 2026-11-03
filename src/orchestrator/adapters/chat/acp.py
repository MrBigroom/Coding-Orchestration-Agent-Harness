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

PROTOCOL_VERSION = 1
START_TIMEOUT = 60
# `ask` for what an agent changes; OpenCode otherwise allows both by default.
OPENCODE_ASK = {"edit": "ask", "bash": "ask", "webfetch": "ask"}
ALLOW_KINDS = ("allow_once", "allow_always")
ACTION_KINDS = {
    "read": "read",
    "search": "read",
    "think": "read",
    "edit": "write",
    "delete": "write",
    "move": "write",
}
PATH_KEYS = ("path", "file_path", "filePath", "absolute_path")


def _flatten(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Config option values, which may be nested one level in groups."""
    flat: list[dict[str, Any]] = []
    for option in options:
        if isinstance(option.get("options"), list):
            flat += option["options"]
        else:
            flat.append(option)
    return flat


def _action(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
    paths = [loc["path"] for loc in call.get("locations") or [] if loc.get("path")]
    paths += [str(raw[k]) for k in PATH_KEYS if raw.get(k)]
    kind = ACTION_KINDS.get(call.get("kind"), "other")
    return {"kind": kind, "paths": list(dict.fromkeys(paths))}


class AcpDriver(ChatDriver):
    """Agent Client Protocol over stdio: Qwen Code, OpenCode and DeepSeek Harness.

    What each harness can change is read from its `session/new` reply rather than assumed:
    Qwen reports `modes` and `models`, OpenCode reports model and mode config options, and
    DeepSeek Harness reports model and reasoning-effort config options. Handshakes checked
    on 2026-11-01 against Qwen Code 0.25.1, OpenCode 1.18.35 and dsh 0.2.0-rc.2.
    """

    kind = "acp"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._ids = itertools.count(1)
        self._session_id = ""
        self._config: dict[str, dict[str, Any]] = {}  # category -> config option
        self._models: list[dict[str, Any]] = []
        self._model = ""
        self._modes: list[dict[str, Any]] = []
        self._mode = ""
        self._turn: asyncio.Task | None = None
        self._open_tools: set[str] = set()
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._loading = False  # session/load replays the history, which the transcript has
        self._resumable = False

    @property
    def _permission_set_at_start(self) -> bool:
        """DeepSeek Harness sandboxes by a mode chosen with an env var and has no live switch."""
        return self.adapter.kind == "deepseek_harness"

    def _argv(self, env: dict[str, str]) -> list[str]:
        command = self.adapter.spec.command
        if self.adapter.kind == "qwen_code":
            auth = ["--auth-type", "openai"] if "OPENAI_API_KEY" in env else []
            return [command, "--acp", *auth]
        if self.adapter.kind == "opencode":
            return [command, "acp"]
        return [command, "--profile", "acp"]

    async def start(self) -> None:
        env = self.prepared_env()
        if self.adapter.kind == "opencode":
            env["OPENCODE_PERMISSION"] = json.dumps(OPENCODE_ASK)
        if self._permission_set_at_start and self.options.permission:
            env["DSH_PERMISSION_MODE"] = self.options.permission
        await self.spawn(self._argv(env), env)
        init = await self._request(
            "initialize",
            timeout=START_TIMEOUT,
            protocolVersion=PROTOCOL_VERSION,
            clientCapabilities={"fs": {"readTextFile": False, "writeTextFile": False}},
        )
        capabilities = (init or {}).get("agentCapabilities") or {}
        prompts = capabilities.get("promptCapabilities") or {}
        self.images = bool(prompts.get("image"))
        can_load = bool(capabilities.get("loadSession"))
        if self.options.resume:
            if not can_load:
                raise DriverError("this harness can't reload a conversation")
            self._loading = True
            try:
                session = await self._request(
                    "session/load",
                    timeout=START_TIMEOUT,
                    sessionId=self.options.resume,
                    cwd=str(self.options.cwd),
                    mcpServers=[],
                )
            finally:
                self._loading = False
            self._session_id = self.options.resume
        else:
            session = await self._request(
                "session/new", timeout=START_TIMEOUT, cwd=str(self.options.cwd), mcpServers=[]
            )
            self._session_id = session["sessionId"]
        self._resumable = can_load
        self._read_session(session or {})
        await self._apply_initial_choices()
        self._publish_config()

    def _read_session(self, session: dict[str, Any]) -> None:
        for option in session.get("configOptions") or []:
            self._config[option.get("category") or option.get("id")] = option
        models = session.get("models") or {}
        self._models = [
            {"id": m["modelId"], "name": m.get("name") or m["modelId"]}
            for m in models.get("availableModels") or []
        ]
        self._model = models.get("currentModelId") or ""
        modes = session.get("modes") or {}
        self._modes = [
            {"id": m["id"], "label": m.get("name") or m["id"], "description": m.get("description")}
            for m in modes.get("availableModes") or []
        ]
        self._mode = modes.get("currentModeId") or ""
        if not self._modes and (option := self._config.get("mode")):
            self._modes = [
                {
                    "id": o["value"],
                    "label": o.get("name") or o["value"],
                    "description": o.get("description"),
                }
                for o in _flatten(option.get("options") or [])
            ]
            self._mode = option.get("currentValue") or ""
        if not self._models and (option := self._config.get("model")):
            self._models = [
                {"id": o["value"], "name": o.get("name") or o["value"]}
                for o in _flatten(option.get("options") or [])
            ]
            self._model = option.get("currentValue") or ""

    def _resolve_model(self, wanted: str) -> str | None:
        """A harness's id for a model name; Qwen, for one, shows it only in the label."""
        for m in self._models:
            if wanted in (m["id"], m["name"]):
                return m["id"]
        for m in self._models:
            if m["name"].endswith(f" {wanted}") or m["id"].endswith(f"/{wanted}"):
                return m["id"]
        return None

    async def _apply_initial_choices(self) -> None:
        if self.options.model:
            model = self._resolve_model(self.options.model)
            if model is None:
                self.emit(
                    {
                        "type": "notice",
                        "message": f"{self.options.model} isn't offered by this harness; "
                        "using its default model.",
                    }
                )
            elif model != self._model:
                await self.set_model(model)
        if self.options.effort and "thought_level" in self._config:
            try:
                await self.set_effort(self.options.effort)
            except DriverError as e:  # DeepSeek Harness lists levels a model then refuses
                self.emit({"type": "notice", "message": f"Using the default effort: {e}"})
        wanted = self.options.permission
        if wanted and self._modes and wanted != self._mode and not self._permission_set_at_start:
            await self.set_permission(wanted)

    def _levels(self) -> list[str]:
        option = self._config.get("thought_level") or {}
        return [o["value"] for o in _flatten(option.get("options") or [])]

    def _publish_config(self) -> None:
        effort = self._config.get("thought_level") or {}
        config = {
            "type": "config",
            "model": self._model,
            "models": self._models,
            "effort": effort.get("currentValue"),
            "efforts": self._levels(),
            "images": self.images,
            # Only a harness that can reload it gets one; Continue replays the rest.
            "harness_session": self._session_id if self._resumable else None,
        }
        if self._permission_set_at_start:
            config |= {"permission": self.options.permission, "permission_locked": True}
        else:
            config |= {"permission": self._mode or None, "permissions": self._modes}
        self.emit(config)

    async def _request(self, method: str, timeout: float = RESPONSE_TIMEOUT, **params: Any) -> Any:
        assert self.process
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        try:
            await self.process.send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as e:
            tail = " | ".join(self.process.stderr_tail)
            raise DriverError(f"no answer to {method}: {tail[-500:]}") from e
        finally:
            self._waiting.pop(request_id, None)

    async def prompt(self, text: str, images: list[Image] | None = None) -> None:
        self.emit({"type": "status", "status": "busy"})
        blocks = [{"type": "image", "data": i.data, "mimeType": i.mime} for i in images or []]
        if text or not blocks:
            blocks.append({"type": "text", "text": text})
        self._turn = asyncio.create_task(self._run_turn(blocks))

    async def _run_turn(self, blocks: list[dict[str, Any]]) -> None:
        try:
            # A turn lasts as long as the agent works, so it has no timeout of its own.
            result = await self._request(
                "session/prompt",
                timeout=86_400,
                sessionId=self._session_id,
                prompt=blocks,
            )
            self._on_turn_end(result or {})
        except DriverError as e:
            self.emit({"type": "error", "message": str(e)})
        finally:
            self._close_open_tools()
            self.emit({"type": "status", "status": "idle"})

    def _on_turn_end(self, result: dict[str, Any]) -> None:
        reason = result.get("stopReason")
        if reason not in (None, "end_turn", "cancelled"):
            self.emit({"type": "notice", "message": f"The turn ended early: {reason}."})
        usage = result.get("usage") or (result.get("_meta") or {}).get("usage")
        if isinstance(usage, dict):
            cached = _n(usage.get("cachedReadTokens") or usage.get("cacheReadTokens"))
            inp = _n(usage.get("inputTokens"))
            # Harnesses disagree on whether inputTokens includes cached tokens (OpenCode
            # doesn't), and a total can't be smaller than one of its parts.
            self.add_usage(inp, cached, _n(usage.get("outputTokens")), total_input=inp >= cached)

    def _close_open_tools(self) -> None:
        for tool_id in self._open_tools:
            self.emit({"type": "tool_end", "id": tool_id, "output": "", "error": True})
        self._open_tools.clear()

    async def interrupt(self) -> None:
        assert self.process
        for approval_id in list(self.approvals):
            await self._reply_permission(approval_id, {"outcome": "cancelled"}, None)
        await self.process.send(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": self._session_id},
            }
        )

    async def respond(self, approval_id: str, choice: str) -> None:
        if approval_id not in self.approvals:
            raise DriverError("that approval is no longer pending")
        await self._reply_permission(
            approval_id, {"outcome": "selected", "optionId": choice}, choice
        )

    async def _reply_permission(self, approval_id: str, outcome: dict, choice: str | None) -> None:
        assert self.process
        request_id = self.approvals[approval_id]["request_id"]
        await self.process.send(
            {"jsonrpc": "2.0", "id": request_id, "result": {"outcome": outcome}}
        )
        self.resolved(approval_id, choice)

    async def _set_config(self, category: str, value: str) -> None:
        option = self._config[category]
        result = await self._request(
            "session/set_config_option",
            sessionId=self._session_id,
            configId=option["id"],
            value=value,
        )
        option["currentValue"] = value
        for updated in (result or {}).get("configOptions") or []:
            self._config[updated.get("category") or updated.get("id")] = updated

    async def set_model(self, model: str) -> None:
        resolved = self._resolve_model(model)
        if resolved is None:
            raise DriverError(f"{model} isn't offered by this harness")
        # Qwen reports its models both ways and implements session/set_model.
        if self.adapter.kind == "qwen_code":
            await self._request("session/set_model", sessionId=self._session_id, modelId=resolved)
        else:
            await self._set_config("model", resolved)
        self._model = resolved
        self._publish_config()

    async def set_effort(self, effort: str) -> None:
        if "thought_level" not in self._config:
            raise DriverError("this harness has no effort setting")
        await self._set_config("thought_level", effort)
        self._publish_config()

    async def set_permission(self, permission: str) -> None:
        if self._permission_set_at_start:
            raise DriverError("this harness's sandbox mode is fixed when the session starts")
        if not self._modes:
            raise DriverError("this harness has no permission modes")
        if self.adapter.kind == "qwen_code":
            await self._request("session/set_mode", sessionId=self._session_id, modeId=permission)
        else:
            await self._set_config("mode", permission)
        self._mode = permission
        self._publish_config()

    async def on_record(self, record: dict[str, Any]) -> None:
        if "method" in record:
            await self._on_message(record)
            return
        future = self._waiting.get(record.get("id"))  # type: ignore[arg-type]
        if future and not future.done():
            if "error" in record:
                error = record["error"] or {}
                future.set_exception(DriverError(str(error.get("message") or error)))
            else:
                future.set_result(record.get("result"))

    async def _on_message(self, record: dict[str, Any]) -> None:
        assert self.process
        method = record["method"]
        params = record.get("params") or {}
        if method == "session/update":
            self._on_update(params.get("update") or {})
        elif method == "session/request_permission":
            self._on_permission(record["id"], params)
        elif "id" in record:
            await self.process.send(
                {
                    "jsonrpc": "2.0",
                    "id": record["id"],
                    "error": {"code": -32601, "message": f"{method} is not supported"},
                }
            )

    def _on_permission(self, request_id: Any, params: dict[str, Any]) -> None:
        call = params.get("toolCall") or {}
        # DeepSeek Harness sends only the id here; the details came with the tool call.
        call = {**self._tool_calls.get(call.get("toolCallId"), {}), **call}
        options = [
            {
                "id": o["optionId"],
                "label": o.get("name") or o["optionId"],
                "tone": "allow" if o.get("kind") in ALLOW_KINDS else "deny",
            }
            for o in params.get("options") or []
        ]
        approval_id = f"permission-{request_id}"
        raw = call.get("rawInput")
        detail = json.dumps(raw, indent=2) if raw else text_of(call.get("content"))
        title = call.get("title") or "Allow this action?"
        self.ask(approval_id, title, detail, options, _action(call))
        self.approvals[approval_id]["request_id"] = request_id

    def _on_update(self, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        if self._loading and kind not in ("current_mode_update", "available_commands_update"):
            return
        if kind == "agent_message_chunk":
            self.emit({"type": "text", "delta": text_of(update.get("content"))})
        elif kind == "agent_thought_chunk":
            self.emit({"type": "thinking", "delta": text_of(update.get("content"))})
        elif kind in ("tool_call", "tool_call_update"):
            self._on_tool(kind, update)
        elif kind == "current_mode_update":
            self._mode = update.get("currentModeId") or self._mode
            self.emit({"type": "config", "permission": self._mode})
        elif kind == "available_commands_update":
            commands = slash_commands(update.get("availableCommands"))
            self.emit({"type": "config", "commands": commands})
        elif kind == "usage_update":
            # ACP's context report: tokens in context now, and the window size.
            if used := _n(update.get("used")):
                self.set_context(used, _n(update.get("size")) or None)

    def _on_tool(self, kind: str, update: dict[str, Any]) -> None:
        tool_id = update.get("toolCallId") or ""
        if kind == "tool_call":
            self._open_tools.add(tool_id)
            self._tool_calls[tool_id] = update
            self.emit(
                {
                    "type": "tool_start",
                    "id": tool_id,
                    "name": update.get("title") or update.get("kind") or "tool",
                    "input": update.get("rawInput"),
                }
            )
        status = update.get("status")
        if status in ("completed", "failed") and tool_id in self._open_tools:
            self._open_tools.discard(tool_id)
            raw = update.get("rawOutput")
            output = text_of(update.get("content")) or (json.dumps(raw) if raw else "")
            self.emit(
                {"type": "tool_end", "id": tool_id, "output": output, "error": status == "failed"}
            )
