"""T-39: `orchestrator cleanup` must judge a chat worktree by its session, not by its dirt.

Everything here runs offline against a throwaway ORCHESTRATOR_HOME: a fixture repo, fixture
worktrees created through `git_service.ensure_worktree`, real `chat_sessions` rows in the states
each test needs, and -- where liveness matters -- a real sleeping child process whose cwd is the
fixture worktree. Nothing points at the operator's live home, and the destructive path is only
ever invoked against the temporary home.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from orchestrator.cli import app
from orchestrator.core.db import session_scope
from orchestrator.core.models import ChatSession, ChatStatus, Project, Task
from orchestrator.services import cleanup_service, git_service
from orchestrator.utils.git import branch_exists, run_git
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

SLUG = "cleanup-chat"
runner = CliRunner()


def _project(repo: Path) -> None:
    with session_scope() as s:
        s.add(Project(name=SLUG, slug=SLUG, path=str(repo)))
        s.commit()


def _chat(cwd: Path, status: str) -> int:
    with session_scope() as s:
        chat = ChatSession(
            adapter="claude", cwd=str(cwd), status=status, workspace="worktree"
        )
        s.add(chat)
        s.commit()
        s.refresh(chat)
        assert chat.id is not None
        return chat.id


def _task(key: str, status: str) -> None:
    """A task row whose id is the worktree marker name, exactly as `cleanup` resolves it."""
    with session_scope() as s:
        s.add(Task(id=int(key), short_id=f"T-{key}", title=f"T-{key}", status=status))
        s.commit()


def _worktree(repo: Path, key: str) -> tuple[Path, Path]:
    """A clean managed worktree and its marker under the throwaway home."""
    branch = f"cleanup/{key}"
    path = git_service.ensure_worktree(repo, SLUG, key, branch, "main").path
    marker = git_service._marker(SLUG, key)
    assert marker.exists()
    assert not git_service.is_dirty(path), "fixture must be a CLEAN worktree"
    return path, marker


def _seed_repo(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "cleanup-repo")
    _project(repo)
    return repo


def _git_worktree_snapshot(repo: Path) -> dict:
    """Git's own view of the repo plus its worktree metadata, for the dry-run mutation check."""
    common = Path(
        run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo).stdout.strip()
    )
    meta = common / "worktrees"
    return {
        "listing": run_git(["worktree", "list", "--porcelain"], repo).stdout,
        "status": run_git(["status", "--porcelain"], repo).stdout,
        "meta": sorted(
            (p.name, p.stat().st_mtime_ns) for p in meta.iterdir()
        )
        if meta.is_dir()
        else [],
    }


def _record_lines(output: str, prefix: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith(prefix)]


def test_busy_chat_worktree_is_skipped_and_named(initialized, tmp_path):
    """A CLEAN busy chat worktree survives, and the reason names the session (DoD 1/2/6a)."""
    repo = _seed_repo(tmp_path)
    path, marker = _worktree(repo, "chat-10")
    chat_id = _chat(path, ChatStatus.busy)

    r = runner.invoke(app, ["cleanup", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert f"skip {path}: chat session {chat_id} is busy" in r.output
    # The old code protected this tree only by dirt; a clean tree must now be skipped too.
    assert "has uncommitted changes" not in r.output
    assert path.exists() and marker.exists()

    # --include-active must not reach a chat worktree, and --include-chats is closed-only.
    for extra in (["--include-active"], ["--include-chats"]):
        r = runner.invoke(app, ["cleanup", "--no-dry-run", *extra])
        assert r.exit_code == 0, r.output
        assert f"skip {path}: chat session {chat_id} is busy" in r.output
        assert path.exists() and marker.exists()


def test_closed_chat_worktree_is_removable(initialized, tmp_path):
    """Closed sessions are reclaimable, but only through the explicit opt-in (DoD 3/6b)."""
    repo = _seed_repo(tmp_path)
    path, marker = _worktree(repo, "chat-5")
    chat_id = _chat(path, ChatStatus.closed)

    default = runner.invoke(app, ["cleanup", "--dry-run"])
    assert default.exit_code == 0, default.output
    assert (
        f"skip {path}: chat session {chat_id} is closed (pass --include-chats to remove)"
        in default.output
    )
    assert path.exists()

    dry = runner.invoke(app, ["cleanup", "--dry-run", "--include-chats"])
    assert dry.exit_code == 0, dry.output
    assert f"would remove worktree {path} (branch kept)" in dry.output
    assert path.exists() and marker.exists()

    real = runner.invoke(app, ["cleanup", "--no-dry-run", "--include-chats"])
    assert real.exit_code == 0, real.output
    assert f"removed worktree {path} (branch kept)" in real.output
    assert not path.exists()
    assert not marker.exists()
    # Branches are never deleted (DoD 5).
    assert branch_exists(repo, "cleanup/chat-5")


def test_live_cwd_refuses_even_when_session_row_says_closed(initialized, tmp_path):
    """The live-process check is independent of the database (DoD 2/6c)."""
    repo = _seed_repo(tmp_path)
    path, marker = _worktree(repo, "chat-7")
    chat_id = _chat(path, ChatStatus.closed)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], cwd=str(path)
    )
    try:
        # Popen sets cwd before exec, so the child already owns the directory.
        r = runner.invoke(app, ["cleanup", "--no-dry-run", "--include-chats"])
        assert r.exit_code == 0, r.output
        assert f"refuse {path}: live process {proc.pid} has it as its cwd" in r.output
        assert f"chat session {chat_id} is closed" in r.output
        assert path.exists() and marker.exists()

        # The separate explicit flag is the only override, and it can delete a running cwd.
        forced = runner.invoke(
            app, ["cleanup", "--no-dry-run", "--include-chats", "--include-live-chats"]
        )
        assert forced.exit_code == 0, forced.output
        assert f"removed worktree {path} (branch kept)" in forced.output
        assert not path.exists()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_task_worktrees_keep_their_existing_behaviour(initialized, tmp_path):
    """Accepted tasks stay removable; an in-progress task needs --include-active (DoD 4)."""
    repo = _seed_repo(tmp_path)
    accepted, _ = _worktree(repo, "41")
    _task("41", "accepted")
    progress, _ = _worktree(repo, "15")
    _task("15", "in_progress")

    dry = runner.invoke(app, ["cleanup", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert f"would remove worktree {accepted} (branch kept)" in dry.output
    assert f"skip {progress}: task T-15 is in_progress" in dry.output
    assert accepted.exists() and progress.exists()

    real = runner.invoke(app, ["cleanup", "--no-dry-run"])
    assert real.exit_code == 0, real.output
    assert f"removed worktree {accepted} (branch kept)" in real.output
    assert not accepted.exists()
    assert progress.exists()

    forced = runner.invoke(app, ["cleanup", "--no-dry-run", "--include-active"])
    assert forced.exit_code == 0, forced.output
    assert f"removed worktree {progress} (branch kept)" in forced.output
    assert not progress.exists()


def test_dry_run_mutates_nothing_including_git_metadata(initialized, tmp_path):
    """A preview writes nothing: no tree, no marker, no `.git/worktrees` entry (DoD 6f)."""
    repo = _seed_repo(tmp_path)
    closed, closed_marker = _worktree(repo, "chat-5")
    busy, busy_marker = _worktree(repo, "chat-10")
    _chat(closed, ChatStatus.closed)
    _chat(busy, ChatStatus.busy)
    accepted, accepted_marker = _worktree(repo, "41")
    _task("41", "accepted")

    before = _git_worktree_snapshot(repo)
    for extra in ([], ["--include-chats"], ["--include-active", "--include-chats"]):
        r = runner.invoke(app, ["cleanup", "--dry-run", *extra])
        assert r.exit_code == 0, r.output
        assert _git_worktree_snapshot(repo) == before
        for path, marker in (
            (closed, closed_marker),
            (busy, busy_marker),
            (accepted, accepted_marker),
        ):
            assert path.exists() and marker.exists()


def test_failed_removal_is_counted_as_a_failure_not_a_removal(initialized, tmp_path):
    """A removal that did not happen is reported and counted as a failure (DoD 4)."""
    repo = _seed_repo(tmp_path)
    path, marker = _worktree(repo, "41")
    _task("41", "accepted")
    slug_dir = path.parent
    os.chmod(slug_dir, 0o500)  # cannot unlink the tree or its marker from here
    try:
        r = runner.invoke(app, ["cleanup", "--no-dry-run"])
        assert r.exit_code == 1, r.output
        assert f"failed to remove worktree {path}" in r.output
        assert _record_lines(r.output, "removed worktree") == []
        assert path.exists() and marker.exists()
        assert "cleanup: 0 removed, 0 skipped, 0 refused, 1 failed" in r.output
    finally:
        os.chmod(slug_dir, 0o700)


def test_printed_counts_and_json_match_the_actual_outcomes(initialized, tmp_path):
    """Counts are derived from what happened, and the output is parseable (DoD 4/5/6)."""
    repo = _seed_repo(tmp_path)
    closed, _ = _worktree(repo, "chat-5")
    _chat(closed, ChatStatus.closed)
    busy, _ = _worktree(repo, "chat-10")
    busy_id = _chat(busy, ChatStatus.busy)
    accepted, _ = _worktree(repo, "41")
    _task("41", "accepted")
    progress, _ = _worktree(repo, "15")
    _task("15", "in_progress")

    dry = runner.invoke(app, ["cleanup", "--dry-run", "--include-chats", "--json"])
    assert dry.exit_code == 0, dry.output
    data = json.loads(dry.output)
    assert data["counts"] == {
        "removed": 0,
        "would_remove": 2,
        "skipped": 2,
        "refused": 0,
        "failed": 0,
    }
    assert data["ok"] is True
    assert {a["kind"] for a in data["actions"]} == {"would_remove", "skipped"}
    assert any(
        a["path"] == str(busy) and a["kind"] == "skipped" for a in data["actions"]
    )
    assert f"chat session {busy_id} is busy" in dry.output

    human = runner.invoke(app, ["cleanup", "--dry-run", "--include-chats"])
    assert human.exit_code == 0, human.output
    # One record per line: the whole sentence (path included) is on the record's line.
    assert f"would remove worktree {accepted} (branch kept)" in _record_lines(
        human.output, "would remove worktree"
    )
    assert f"would remove worktree {closed} (branch kept)" in _record_lines(
        human.output, "would remove worktree"
    )
    assert "cleanup (dry run): 2 would remove, 2 skipped, 0 refused, 0 failed" in human.output

    real = runner.invoke(app, ["cleanup", "--no-dry-run", "--include-chats"])
    assert real.exit_code == 0, real.output
    assert "cleanup: 2 removed, 2 skipped, 0 refused, 0 failed" in real.output
    assert not closed.exists() and not accepted.exists()
    assert busy.exists() and progress.exists()


def _fake_info(path: Path) -> dict:
    return {"path": path, "project_slug": SLUG, "task_id": "chat-10"}


def test_the_live_process_check_is_what_refuses_a_closed_session(monkeypatch, tmp_path):
    """Pin that the refusal is the cwd check and that removal is never attempted (DoD 2/6c)."""
    path = tmp_path / "worktrees" / SLUG / "chat-10"
    chat = SimpleNamespace(id=10, status=ChatStatus.closed)
    monkeypatch.setattr(cleanup_service, "cwd_owner_pids", lambda _path: [4242])
    attempted: list[tuple] = []
    monkeypatch.setattr(
        cleanup_service.git_service,
        "remove_worktree_outcome",
        lambda *a, **k: attempted.append(a),
    )
    action = cleanup_service._chat_action(
        _fake_info(path),
        [chat],
        dry_run=True,
        include_chats=True,
        include_live_chats=False,
        projects={},
    )
    assert action.kind == cleanup_service.REFUSED
    assert "live process 4242 has it as its cwd" in action.message
    assert "chat session 10 is closed" in action.message
    assert attempted == []


def test_without_the_live_process_a_closed_session_is_removable(monkeypatch, tmp_path):
    """The same closed session becomes a planned removal once nothing holds its cwd (DoD 3)."""
    path = tmp_path / "worktrees" / SLUG / "chat-10"
    chat = SimpleNamespace(id=10, status=ChatStatus.closed)
    monkeypatch.setattr(cleanup_service, "cwd_owner_pids", lambda _path: [])
    monkeypatch.setattr(
        cleanup_service.git_service,
        "remove_worktree_outcome",
        lambda _repo, info, dry_run=True: git_service.RemovalOutcome(
            git_service.REMOVAL_WOULD_REMOVE,
            info["path"],
            f"would remove worktree {info['path']} (branch kept)",
        ),
    )
    action = cleanup_service._chat_action(
        _fake_info(path),
        [chat],
        dry_run=True,
        include_chats=True,
        include_live_chats=False,
        projects={},
    )
    assert action.kind == cleanup_service.WOULD_REMOVE


def test_counts_and_summary_read_the_kind_not_the_wording():
    """A record is classified by its outcome, so prose cannot inflate the removed count (DoD 4)."""
    actions = [
        cleanup_service.CleanupAction(
            cleanup_service.FAILED, Path("/a"), "removed worktree /a (branch kept)"
        ),
        cleanup_service.CleanupAction(
            cleanup_service.SKIPPED, Path("/b"), "would remove worktree /b (branch kept)"
        ),
    ]
    counted = cleanup_service.counts(actions)
    assert counted[cleanup_service.FAILED] == 1
    assert counted[cleanup_service.REMOVED] == 0
    assert counted[cleanup_service.WOULD_REMOVE] == 0
    assert (
        cleanup_service.summary(actions, dry_run=False)
        == "cleanup: 0 removed, 1 skipped, 0 refused, 1 failed"
    )
