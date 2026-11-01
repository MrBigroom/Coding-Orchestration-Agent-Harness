# Desktop app

`orchestrator desktop` opens the GUI in a native window: pywebview on your system's
WebKitGTK. The **Coding Orchestrator** app-menu entry runs the same command.

## Install

```bash
make desktop-install        # or: ./scripts/desktop-install.sh
```

This installs the `desktop` extra (`pywebview[gtk]`) into `.venv`, rebuilds the GUI, and
writes the following files for the current user:

- `~/.local/share/applications/coding-orchestrator.desktop`
- `~/.local/share/icons/hicolor/scalable/apps/coding-orchestrator.svg`

The launcher runs this repo's `.venv/bin/orchestrator desktop` with the repo as its
working directory, so `.env` is picked up. If you move the repo, re-run the install.
`make desktop-uninstall` removes both files.

System requirements are GTK 3, WebKit2GTK 4.1 and GObject introspection. On Arch/CachyOS
these are `webkit2gtk-4.1` and `python-gobject`'s dependencies.

## What happens when it opens

1. **Your own background server:** if a server the app started earlier is still running,
   the app reattaches to it.
2. **An existing orchestrator** (for example `make start`) on the configured
   host/port: the app **attaches** to it and never stops it.
3. **No server running:** the app starts `orchestrator serve` as a detached process and
   logs to `$ORCHESTRATOR_HOME/logs/desktop-server.log`. If the port is taken by
   something else, it picks a free port.

Web storage (theme, token) is kept in `$ORCHESTRATOR_HOME/desktop-webview`.

## What happens when it closes

The app only ever stops a server it started:

- **Nothing active or queued:** the server stops with the window.
- **Runs active, tasks queued or chat sessions open:** a dialog offers **Keep running** (the server stays up
  in the background; reopen the app to reattach), **Stop server** (active runs are
  interrupted and recovered as failed on the next start, and chat sessions end), or **Cancel**.

To stop a background server later, run `orchestrator desktop stop`.

## Notes

- The window's Wayland app_id / X11 class is `coding-orchestrator`, which matches
  `StartupWMClass` in the launcher. Use it for compositor rules, for example in Hyprland:
  `windowrule = float, class:coding-orchestrator`.
- `orchestrator desktop --debug` enables the WebKit inspector.
- Attaching uses whatever code that server is running. After pulling or merging changes,
  restart any long-running `make start` server.
