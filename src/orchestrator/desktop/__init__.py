"""Desktop app: a native window (pywebview on WebKitGTK) around the local GUI.

The launcher attaches to an orchestrator that is already running on the configured port.
Otherwise it starts `orchestrator serve` as a detached child process, so the server can
keep running in the background after the window closes. On close it stops the server it
owns, unless runs are active or queued; then it asks.

Install the launcher (app-menu entry + icon) with `orchestrator desktop install`."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from orchestrator.core.config import get_settings
from orchestrator.core.logging import get_logger

log = get_logger(__name__)

APP_ID = "coding-orchestrator"
APP_NAME = "Coding Orchestrator"
ICON = Path(__file__).with_name("icon.svg")


class DesktopError(RuntimeError):
    pass


@dataclass
class Server:
    url: str
    pid: int | None  # None: attached to a server this app didn't start
    proc: subprocess.Popen | None = None


def _state_file() -> Path:
    return get_settings().home / "desktop-server.json"


def _headers() -> dict[str, str]:
    from orchestrator.core.security import TOKEN_HEADER

    token = get_settings().token
    return {TOKEN_HEADER: token} if token else {}


def is_orchestrator(url: str) -> bool:
    try:
        r = httpx.get(f"{url}/api/health", timeout=1.5)
        return r.status_code == 200 and r.json().get("ok") is True and "db_revision" in r.json()
    except (httpx.HTTPError, ValueError):
        return False


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_state() -> dict | None:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return None


def _write_state(pid: int, url: str) -> None:
    _state_file().write_text(json.dumps({"pid": pid, "url": url}))


def _clear_state() -> None:
    with contextlib.suppress(FileNotFoundError):
        _state_file().unlink()


def ensure_server() -> Server:
    """Attach to a running orchestrator or start one in the background."""
    s = get_settings()
    host = "127.0.0.1" if s.bind_host in ("0.0.0.0", "::") else s.bind_host  # noqa: S104
    url = f"http://{host}:{s.bind_port}"
    state = _read_state()
    if state and _pid_alive(state["pid"]) and is_orchestrator(state["url"]):
        return Server(state["url"], state["pid"])  # our own background server from last time
    if is_orchestrator(url):
        return Server(url, None)
    port = s.bind_port
    if _port_in_use(host, port):
        port = _free_port(host)
        url = f"http://{host}:{port}"
    s.ensure_dirs()
    logfile = Path(s.log_root) / "desktop-server.log"
    env = {**os.environ, "ORCHESTRATOR_AUTOSTART_BROWSER": "false"}
    with logfile.open("ab") as out:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "orchestrator", "serve", "--host", host, "--port", str(port)],
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,  # survives the window if the user keeps it running
        )
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise DesktopError(f"the server exited during startup; see {logfile}")
        if is_orchestrator(url):
            _write_state(proc.pid, url)
            return Server(url, proc.pid, proc)
        time.sleep(0.25)
    proc.terminate()
    raise DesktopError(f"the server did not start within 45 s; see {logfile}")


def activity(url: str) -> tuple[int, int]:
    """(active runs, queued tasks); (0, 0) when the dashboard can't be read."""
    try:
        d = httpx.get(f"{url}/api/dashboard", headers=_headers(), timeout=3).json()
        return len(d.get("active_runs", [])), len(d.get("queued", []))
    except (httpx.HTTPError, ValueError, AttributeError):
        return 0, 0


def stop_server(pid: int, timeout: float = 15) -> None:
    """SIGTERM the server's process group (uvicorn shuts down gracefully), then SIGKILL."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        time.sleep(0.2)
    if _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
    _clear_state()


def stop_background() -> bool:
    """Stop a server a previous app session left running. True if one was stopped."""
    state = _read_state()
    if not state or not _pid_alive(state["pid"]):
        _clear_state()
        return False
    stop_server(state["pid"])
    return True


def _set_app_id() -> None:
    """Wayland app_id / X11 WM_CLASS, so compositors match the window to the .desktop file."""
    try:
        from gi.repository import GLib

        GLib.set_prgname(APP_ID)
        GLib.set_application_name(APP_NAME)
    except ImportError:
        pass


def ask_close(parent: object, message: str, on_answer) -> None:
    """Three-way GTK dialog (Keep running / Stop server / Cancel), shown on the GTK main
    loop. `on_answer` gets "keep", "stop" or "cancel" on a worker thread."""
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    choices = {1: "keep", 2: "stop"}

    def show() -> bool:
        dlg = Gtk.MessageDialog(
            transient_for=parent,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Tasks are still running",
        )
        dlg.format_secondary_text(message)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Stop server", 2, "Keep running", 1)
        dlg.set_default_response(1)

        def on_response(d: Gtk.Dialog, response: int) -> None:
            d.destroy()
            choice = choices.get(response, "cancel")
            threading.Thread(target=on_answer, args=(choice,), daemon=True).start()

        dlg.connect("response", on_response)
        dlg.show_all()
        return False

    GLib.idle_add(show)


def run(debug: bool = False) -> None:
    try:
        import webview
    except ImportError as e:
        raise DesktopError(
            "pywebview is not installed. Run: uv pip install -p .venv -e '.[desktop]'"
        ) from e

    server = ensure_server()
    owned = server.pid is not None
    _set_app_id()
    settings = get_settings()
    storage = settings.home / "desktop-webview"
    storage.mkdir(parents=True, exist_ok=True)

    window = webview.create_window(
        APP_NAME,
        server.url,
        width=1440,
        height=920,
        min_size=(900, 600),
        text_select=True,
        zoomable=True,
        background_color="#F9FAFD",
    )
    decided = threading.Event()  # set once closing is allowed

    def answer(choice: str) -> None:
        if choice == "cancel":
            return
        if choice == "stop" and server.pid:
            stop_server(server.pid)
        decided.set()
        window.destroy()

    def on_closing() -> bool:
        if not owned or decided.is_set():
            return True
        active, queued = activity(server.url)
        if active or queued:
            parts = [f"{active} run(s) active"] if active else []
            if queued:
                parts.append(f"{queued} task(s) queued")
            message = (
                f"{' and '.join(parts)}. Keep the orchestrator running in the background so "
                "they can finish? Reopen the app to see them, or run `orchestrator desktop "
                "stop` later. Stopping now interrupts active runs."
            )
            ask_close(window.native, message, answer)
            return False  # keep the window open until the user decides
        decided.set()
        if server.pid:
            stop_server(server.pid)
        return True

    window.events.closing += on_closing
    webview.start(
        gui="gtk",
        debug=debug,
        private_mode=False,  # keep localStorage (theme, token) between launches
        storage_path=str(storage),
        icon=str(ICON),
    )


# ---------------------------------------------------------------- launcher install


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def desktop_entry(exec_path: str, workdir: Path) -> str:
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.5",
            f"Name={APP_NAME}",
            "GenericName=Coding agent orchestrator",
            "Comment=Architect, Supervisor and Worker coding agents with gated merges",
            f'Exec="{exec_path}" desktop',
            # .env is read from the working directory.
            f"Path={workdir}",
            f"Icon={APP_ID}",
            "Terminal=false",
            "Categories=Development;IDE;",
            "Keywords=AI;agents;orchestrator;coding;LLM;",
            f"StartupWMClass={APP_ID}",
            "StartupNotify=true",
            "",
        ]
    )


def install(workdir: Path) -> list[Path]:
    exe = shutil.which("orchestrator") or str(Path(sys.executable).with_name("orchestrator"))
    if not Path(exe).exists():
        raise DesktopError("can't find the `orchestrator` executable next to this Python")
    data = _data_home()
    entry = data / "applications" / f"{APP_ID}.desktop"
    icon = data / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"
    entry.parent.mkdir(parents=True, exist_ok=True)
    icon.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(desktop_entry(str(Path(exe).resolve()), workdir.resolve()))
    entry.chmod(0o755)
    shutil.copyfile(ICON, icon)
    for cmd in (
        ["update-desktop-database", str(entry.parent)],
        ["gtk-update-icon-cache", "-q", "-t", str(data / "icons" / "hicolor")],
    ):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, check=False, capture_output=True)  # noqa: S603
    return [entry, icon]


def uninstall() -> list[Path]:
    data = _data_home()
    removed = []
    for p in (
        data / "applications" / f"{APP_ID}.desktop",
        data / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg",
    ):
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed
