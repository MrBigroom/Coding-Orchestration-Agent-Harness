from pathlib import Path

import pytest

from orchestrator.services import git_service
from orchestrator.utils.git import GitError, branch_exists, current_branch, run_git
from tests.conftest import make_repo, requires_git

pytestmark = requires_git


def _commit(path, *args: str) -> None:
    run_git(["-c", "user.name=w", "-c", "user.email=w@w", *args], path)


def test_worktree_create_reuse_and_isolation(home, tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt = git_service.ensure_worktree(repo, "repo", 7, "phase-1-x", "main")
    assert wt.created and wt.path == home / "worktrees" / "repo" / "7"
    assert current_branch(wt.path) == "phase-1-x" and current_branch(repo) == "main"
    (wt.path / "new.txt").write_text("hi")
    sha = git_service.recovery_commit(wt.path, "test recovery")
    assert sha and "orchestrator-recovery" in run_git(["log", "-1", "--format=%s"], wt.path).stdout
    assert not (repo / "new.txt").exists()  # main checkout untouched
    patch, stat = git_service.diff_against_base(wt.path, "main")
    assert "new.txt" in patch and "new.txt" in stat
    again = git_service.ensure_worktree(repo, "repo", 7, "phase-1-x", "main")
    assert not again.created and again.path == wt.path
    # .orchestrator/ scratch is git-excluded
    (wt.path / ".orchestrator" / "handoff.json").write_text("{}")
    assert not git_service.is_dirty(wt.path)


def test_refuses_unmanaged_existing_directory(home, tmp_path):
    repo = make_repo(tmp_path / "repo")
    path = git_service.worktree_path("repo", 9)
    path.mkdir(parents=True)
    with pytest.raises(GitError, match="not managed"):
        git_service.ensure_worktree(repo, "repo", 9, "phase-2-y", "main")


def test_cleanup_only_managed_and_never_deletes_branches(home, tmp_path):
    repo = make_repo(tmp_path / "repo")
    git_service.ensure_worktree(repo, "repo", 1, "phase-1-a", "main")
    infos = git_service.managed_worktrees()
    assert len(infos) == 1
    assert "would remove" in git_service.remove_worktree(repo, infos[0], dry_run=True)
    assert infos[0]["path"].exists()
    assert "removed" in git_service.remove_worktree(repo, infos[0], dry_run=False)
    assert not infos[0]["path"].exists()
    assert branch_exists(repo, "phase-1-a")  # branch kept
    # A non-managed directory under the root is never touched.
    stray = git_service.worktree_path("repo", 2)
    stray.mkdir(parents=True)
    fake = {"path": stray, "marker": Path(stray.parent / ".managed" / "2")}
    assert "not managed" in git_service.remove_worktree(repo, fake, dry_run=False)
    assert stray.exists()


def test_merge_gate_refuses_tracked_dirt_but_not_untracked(home, tmp_path):
    """T-8 DoD 9: the merge gate keys on TRACKED dirtiness. Uncommitted changes to versioned
    files refuse a merge; an untracked local file cannot be damaged by merging another branch
    into this one, so it must not refuse — and it is still named in the refusal message."""
    repo = make_repo(tmp_path / "repo")
    wt = git_service.ensure_worktree(repo, "repo", 3, "phase-3-m", "main")
    (wt.path / "f.txt").write_text("x")
    git_service.recovery_commit(wt.path, "change")

    # untracked-only dirt: the base checkout is "clean" for merge purposes
    (repo / "scratch-dir").mkdir()
    (repo / "scratch-dir" / "local.txt").write_text("untracked, not versioned")
    assert git_service.is_dirty(repo) and not git_service.tracked_dirty(repo)
    sha = git_service.merge_no_ff(repo, "main", "phase-3-m", "Merge phase-3-m")
    parents = run_git(["rev-list", "--parents", "-n", "1", sha], repo).stdout.split()
    assert len(parents) == 3  # --no-ff merge commit has two parents
    assert (repo / "scratch-dir" / "local.txt").exists()  # the merge did not touch it

    # tracked dirt still refuses, and the untracked half is reported rather than hidden
    _commit(repo, "add", "scratch-dir/local.txt")
    _commit(repo, "commit", "-q", "-m", "now a versioned file")
    (repo / "scratch-dir" / "local.txt").write_text("uncommitted tracked change")
    (repo / "untracked-neighbour.txt").write_text("ignored for the verdict")
    assert git_service.tracked_dirty(repo)
    with pytest.raises(GitError, match="uncommitted changes to tracked"):
        git_service.merge_no_ff(repo, "main", "phase-3-m", "msg")
    with pytest.raises(GitError, match="untracked-neighbour.txt"):
        git_service.merge_no_ff(repo, "main", "phase-3-m", "msg")
    assert branch_exists(repo, "phase-3-m")  # branch kept


def test_recovery_commit_never_stages_secrets(home, tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt = git_service.ensure_worktree(repo, "repo", 4, "phase-4-s", "main")
    (wt.path / ".env").write_text("API_KEY=nope\n")
    (wt.path / "code.py").write_text("x = 1\n")
    git_service.recovery_commit(wt.path, "leftovers")
    files = run_git(["show", "--name-only", "--format=", "HEAD"], wt.path).stdout.split()
    assert files == ["code.py"]
    assert (wt.path / ".env").exists()  # left untracked, not committed
    # Only a secret left over -> no commit at all.
    head = run_git(["rev-parse", "HEAD"], wt.path).stdout
    assert git_service.recovery_commit(wt.path, "only secret") is None
    assert run_git(["rev-parse", "HEAD"], wt.path).stdout == head
