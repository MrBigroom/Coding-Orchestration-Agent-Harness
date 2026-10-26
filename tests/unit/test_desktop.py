"""Desktop launcher logic that doesn't need a display (the window itself is manual-test)."""

import os

from orchestrator import desktop


def test_desktop_entry_points_at_repo_and_matches_app_id(tmp_path):
    entry = desktop.desktop_entry("/x/.venv/bin/orchestrator", tmp_path)
    assert 'Exec="/x/.venv/bin/orchestrator" desktop' in entry
    assert f"Path={tmp_path}" in entry
    assert f"StartupWMClass={desktop.APP_ID}" in entry and f"Icon={desktop.APP_ID}" in entry


def test_install_and_uninstall(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    paths = desktop.install(tmp_path)
    assert all(p.exists() for p in paths)
    assert paths[0].read_text().startswith("[Desktop Entry]")
    assert sorted(desktop.uninstall()) == sorted(paths)
    assert desktop.uninstall() == []


def test_attaches_to_running_orchestrator(initialized, monkeypatch):
    monkeypatch.setattr(desktop, "is_orchestrator", lambda url: True)
    server = desktop.ensure_server()
    assert server.pid is None and server.url.endswith(":8321")


def test_reuses_own_background_server(initialized, monkeypatch):
    desktop._write_state(os.getpid(), "http://127.0.0.1:9999")
    monkeypatch.setattr(desktop, "is_orchestrator", lambda url: url.endswith(":9999"))
    server = desktop.ensure_server()
    assert server.pid == os.getpid() and server.url.endswith(":9999")


def test_stop_background_without_server(initialized):
    assert desktop.stop_background() is False
    desktop._write_state(2**22 + 12345, "http://127.0.0.1:1")  # pid that doesn't exist
    assert desktop.stop_background() is False and not desktop._state_file().exists()
