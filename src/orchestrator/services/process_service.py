"""Subprocess execution with log capture, timeout, and cross-process cancellation.

- argv lists only (asyncio.create_subprocess_exec), never a shell;
- minimal environment (core.security.child_env) plus declared variable names;
- stdout/stderr are redacted and written line by line to <artifacts>/stdout.log|stderr.log
  and published on the in-process event bus; with `stdout_file` the child writes stdout to
  that file instead of a pipe, and it is logged the same way after the child exits;
- cancellation: anyone (API, CLI in another process) writes <artifacts>/CANCEL; the
  runner polls for it and terminates the process group.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import TextIO

from orchestrator.adapters.base import AdapterResult
from orchestrator.core.config import get_settings
from orchestrator.core.events import bus
from orchestrator.core.security import child_env
from orchestrator.utils.redaction import redact

CANCEL_MARKER = "CANCEL"
POLL_SECONDS = 0.25
# Longest output line the log pumps accept. JSON modes print whole messages on one line
# (Pi reaches ~500 KB), and asyncio's 64 KiB default would kill the pump and stall the child.
MAX_LINE_BYTES = 32 * 1024 * 1024


def git_identity_env() -> dict[str, str]:
    s = get_settings()
    return {
        "GIT_AUTHOR_NAME": s.git_user_name,
        "GIT_AUTHOR_EMAIL": s.git_user_email,
        "GIT_COMMITTER_NAME": s.git_user_name,
        "GIT_COMMITTER_EMAIL": s.git_user_email,
        # Never let a child block on an interactive git/ssh prompt.
        "GIT_TERMINAL_PROMPT": "0",
    }


def _log_line(fh: TextIO, raw: bytes, run_id: int, name: str) -> None:
    line = redact(raw.decode(errors="replace").rstrip("\n"))
    fh.write(line + "\n")
    fh.flush()
    bus.publish(run_id, {"type": "log", "stream": name, "line": line})


async def _pump(stream: asyncio.StreamReader, path: Path, run_id: int, name: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        while True:
            raw = await stream.readline()
            if not raw:
                break
            _log_line(fh, raw, run_id, name)


def _replay(source: Path, path: Path, run_id: int, name: str) -> None:
    with source.open("rb") as raw, path.open("a", encoding="utf-8") as fh:
        for line in raw:
            _log_line(fh, line, run_id, name)


def _terminate(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


async def run_process(
    argv: list[str],
    cwd: Path,
    env_names: list[str],
    artifacts_dir: Path,
    run_id: int,
    timeout_seconds: int,
    stdin_data: bytes | None = None,
    log_prefix: str = "",
    extra_env: dict[str, str] | None = None,
    stdout_file: Path | None = None,
) -> AdapterResult:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    out_path = artifacts_dir / f"{log_prefix}stdout.log"
    err_path = artifacts_dir / f"{log_prefix}stderr.log"
    cancel_path = artifacts_dir / CANCEL_MARKER
    env = child_env(env_names, {**git_identity_env(), **(extra_env or {})})
    started = time.monotonic()
    stdout_sink = stdout_file.open("wb") if stdout_file else None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=stdout_sink or asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group so we can kill children too
            limit=MAX_LINE_BYTES,
        )
    except (FileNotFoundError, PermissionError) as e:
        err_path.write_text(f"failed to start {argv[0]!r}: {e}\n")
        return AdapterResult(
            exit_code=None,
            error=f"failed to start: {e}",
            stdout_path=out_path,
            stderr_path=err_path,
        )
    finally:
        if stdout_sink:
            stdout_sink.close()

    (artifacts_dir / f"{log_prefix}pid").write_text(str(proc.pid))
    assert proc.stderr
    pumps = [asyncio.create_task(_pump(proc.stderr, err_path, run_id, "stderr"))]
    if proc.stdout:
        pumps.append(asyncio.create_task(_pump(proc.stdout, out_path, run_id, "stdout")))
    if stdin_data is not None and proc.stdin:
        proc.stdin.write(stdin_data)
        await proc.stdin.drain()
        proc.stdin.close()

    timed_out = cancelled = False
    deadline = started + timeout_seconds
    while proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass
        if proc.returncode is not None:
            break
        if cancel_path.exists():
            cancelled = True
        elif time.monotonic() > deadline:
            timed_out = True
        if cancelled or timed_out:
            _terminate(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                _terminate(proc, signal.SIGKILL)
                await proc.wait()
            break

    # Pipes can be held open by orphaned grandchildren; don't wait forever.
    _, pending = await asyncio.wait(pumps, timeout=5)
    for t in pending:
        t.cancel()
    if stdout_file:
        _replay(stdout_file, out_path, run_id, "stdout")
    duration_ms = int((time.monotonic() - started) * 1000)
    error = None
    if timed_out:
        error = f"timed out after {timeout_seconds}s"
    elif cancelled:
        error = "cancelled"
    return AdapterResult(
        exit_code=proc.returncode,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_ms=duration_ms,
        stdout_path=out_path,
        stderr_path=err_path,
        error=error,
    )


async def run_simple(
    argv: list[str], cwd: Path, timeout_seconds: int = 600, env_names: list[str] | None = None
) -> tuple[int | None, str, bool]:
    """Run a short verification command; returns (exit_code, combined_output, timed_out)."""
    # Don't leave bytecode caches in the worktree being verified.
    env = child_env(env_names or [], {**git_identity_env(), "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as e:
        return None, f"failed to start {argv[0]!r}: {e}", False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        _terminate(proc, signal.SIGKILL)
        await proc.wait()
        return proc.returncode, "timed out", True
    return proc.returncode, redact(out.decode(errors="replace")), False


async def run_capture(
    argv: list[str],
    cwd: Path,
    env_names: list[str],
    stdin_data: bytes | None,
    timeout_seconds: int,
    extra_env: dict[str, str] | None = None,
) -> tuple[int | None, str, str, bool]:
    """Run a command feeding stdin; return (exit_code, stdout, stderr, timed_out).
    Output is returned unredacted for parsing — callers must redact before persisting."""
    env = child_env(env_names, extra_env)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as e:
        return None, "", f"failed to start {argv[0]!r}: {e}", False
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin_data), timeout=timeout_seconds)
    except TimeoutError:
        _terminate(proc, signal.SIGKILL)
        await proc.wait()
        return proc.returncode, "", "timed out", True
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"), False
