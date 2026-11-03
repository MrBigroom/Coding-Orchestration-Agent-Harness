"""T-6: verification commands must be path-independent inside a worktree.

A gated run executes the project's test command with cwd = the task worktree, but
`git worktree add` copies tracked files only (no `.venv`), and an editable install in the
main checkout's venv makes `import <pkg>` resolve to the MAIN checkout: the suite goes
green while verifying code that is not the branch under review.

These tests build a throwaway git repo with a fake package installed *editable* into a
throwaway venv and reproduce that trap offline. No network, no API keys, nothing installed
from outside the standard library.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from orchestrator.core.schemas import ProjectCreate
from orchestrator.services import git_service, project_service
from orchestrator.services.process_service import run_simple_sync
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_SRC = REPO_ROOT / "scripts" / "orch_verify.sh"
MODULE = "vercheck"
# The main checkout is green; a branch that regresses it is not.
GOOD_SRC = 'VALUE = "good"\n'
BROKEN_SRC = 'VALUE = "broken"\n'
TEST_FILE = 'import vercheck\n\n\ndef test_value():\n    assert vercheck.VALUE == "good"\n'


def bare_env(**extra: str) -> dict[str, str]:
    """PATH and HOME only: the child must not inherit VIRTUAL_ENV or PYTHONPATH by accident."""
    return {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), **extra}


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=300
    )


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env
    ).stdout


def _fake_venv(main: Path) -> Path:
    """A real venv, created offline, with the fake package installed the editable way: a
    `.pth` pointing at the main checkout's `src`, plus the `direct_url.json` that marks it
    editable — and one more `.pth` so that pytest is importable inside it."""
    venv = main / ".venv"
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)],
            check=True,
            capture_output=True,
            env=bare_env(),
        )
    except (subprocess.CalledProcessError, OSError):  # pragma: no cover - env-dependent
        pytest.skip("python -m venv is not usable in this environment")
    site = next(venv.glob("lib/python*/site-packages"))
    (site / f"_editable_impl_{MODULE}.pth").write_text(f"{main / 'src'}\n")
    (site / "outer.pth").write_text(f"{sysconfig.get_paths()['purelib']}\n")
    dist = site / f"{MODULE}-0.1.0.dist-info"
    dist.mkdir()
    (dist / "direct_url.json").write_text(
        '{"url": "file://' + str(main) + '", "dir_info": {"editable": true}}'
    )
    return venv


@pytest.fixture(scope="module")
def fake(tmp_path_factory) -> dict:
    """A throwaway project: editable-installed package, green suite, wrapper committed."""
    main = make_repo(
        tmp_path_factory.mktemp("fakeproj") / "main",
        {
            ".gitignore": ".venv/\n__pycache__/\n.pytest_cache/\n",
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            f"src/{MODULE}/__init__.py": GOOD_SRC,
            "tests/test_value.py": TEST_FILE,
            "scripts/orch_verify.sh": WRAPPER_SRC.read_text(),
        },
    )
    # git stores the exec bit in the tree and `worktree add` restores it, which is what
    # lets the configured command be argv[0] = scripts/orch_verify.sh (exec'd, no shell).
    (main / "scripts" / "orch_verify.sh").chmod(0o755)
    git(main, "add", "scripts/orch_verify.sh")
    git(main, "commit", "-q", "-m", "carry the exec bit")
    assert "100755" in git(main, "ls-files", "-s", "scripts/orch_verify.sh")
    venv = _fake_venv(main)
    assert not git(main, "status", "--porcelain").strip()  # .venv stays ignored
    return {"main": main, "python": venv / "bin" / "python"}


def snapshot(fake: dict, key: str) -> Path:
    """A throwaway worktree of the base branch under ORCHESTRATOR_HOME."""
    return git_service.ensure_snapshot(fake["main"], "fakeproj", key, "main").path


def regress(wt: Path) -> None:
    """The branch under review breaks the package; its tests still expect "good"."""
    (wt / "src" / MODULE / "__init__.py").write_text(BROKEN_SRC)
    git(wt, "commit", "-qam", "regress the package")


def marker_row(output: str) -> dict[str, str]:
    rows = project_service.parse_orch_verify(output)
    assert rows, f"no ORCH_VERIFY marker in:\n{output}"
    return rows[0]


def test_suite_imports_the_checkout_it_runs_in():
    """Our own suite must test this worktree, not the main checkout (DoD 2's proof)."""
    import orchestrator

    top = Path(git(REPO_ROOT, "rev-parse", "--show-toplevel").strip()).resolve()
    resolved = Path(orchestrator.__file__).resolve()
    assert resolved.is_relative_to(top), f"imported {resolved}, expected under {top}"


def test_this_repo_commits_the_wrapper_executable():
    """DoD 1's "committed wrapper": the exec bit has to be in THIS repo's index/tree, not
    only in a fixture that chmods its own copy — `git worktree add` restores the mode from
    the tree, and the gate execs argv[0] without a shell, so a 100644 script is exit 126."""
    mode = git(REPO_ROOT, "ls-files", "-s", "scripts/orch_verify.sh").split()
    assert mode[:1] == ["100755"], f"index mode {mode}, expected 100755"
    assert "100755" in git(REPO_ROOT, "ls-tree", "-r", "HEAD", "--", "scripts/orch_verify.sh")
    assert os.access(WRAPPER_SRC, os.X_OK)


def test_wrapper_asserts_the_resolution_the_tool_will_actually_use(fake, home):
    """The resolution probe must run in the same directory the tool runs in.

    `python -` and `python -m pytest` both put the CURRENT directory first on sys.path, so a
    probe run before `cd "$ROOT"` asserts a different resolution than the one used. A stale
    shadow copy of the package in the invoking directory proves it: whichever directory wins,
    that is what the marker reports, and it must be the worktree `src` the tools then import.
    """
    wt = snapshot(fake, "cwdprobe")
    shadow = wt / "tools"
    shadow.mkdir(exist_ok=True)
    (shadow / MODULE).mkdir(exist_ok=True)
    (shadow / f"{MODULE}.py").write_text('VALUE = "shadow-of-the-invoking-directory"\n')

    from_shadow_dir = run(
        [str(wt / "scripts" / "orch_verify.sh"), "test"],
        shadow,
        bare_env(ORCH_VERIFY_PACKAGE=MODULE),
    )
    row = marker_row(from_shadow_dir.stdout + from_shadow_dir.stderr)
    assert row["worktree_root"] == str(wt)
    assert row["module_file"] == str(wt / "src" / MODULE / "__init__.py"), (
        "the wrapper asserted a path from the invoking directory, not from the worktree root"
    )
    assert from_shadow_dir.returncode == 0, from_shadow_dir.stdout + from_shadow_dir.stderr


def test_wrapper_resolves_extra_paths_against_the_worktree_root(fake, home):
    """Extra path arguments are relative to the worktree root even when the command is
    invoked from a subdirectory (the gate always uses cwd = the worktree root)."""
    wt = snapshot(fake, "subdir")
    result = run(
        [str(wt / "scripts" / "orch_verify.sh"), "test", "tests/test_value.py"],
        wt / "src",
        bare_env(ORCH_VERIFY_PACKAGE=MODULE),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "orch-verify: pytest target tests/test_value.py" in result.stdout


def test_without_the_wrapper_the_import_resolves_outside_the_worktree(fake, home):
    """(a) the trap, reproduced: the gate goes green on the wrong checkout."""
    wt = snapshot(fake, "trap")
    assert wt != fake["main"] and wt.parent == home / "worktrees" / "fakeproj"
    regress(wt)

    probe = run(
        [str(fake["python"]), "-c", f"import {MODULE}; print({MODULE}.__file__)"],
        wt,
        bare_env(),
    )
    assert probe.returncode == 0, probe.stderr
    resolved = Path(probe.stdout.strip())
    assert resolved.is_relative_to(fake["main"]), f"expected the main checkout: {resolved}"
    assert not resolved.is_relative_to(wt), "the worktree must NOT be the one imported"

    # ... and the worktree's own suite goes green against that foreign code.
    result = run([str(fake["python"]), "-m", "pytest", "tests"], wt, bare_env())
    assert result.returncode == 0, result.stdout + result.stderr  # 0 == the bug
    assert "1 passed" in result.stdout
    assert (wt / "src" / MODULE / "__init__.py").read_text() == BROKEN_SRC


def test_wrapper_resolves_inside_the_worktree_and_stops_the_false_pass(fake, home):
    """(b) with the wrapper the same run imports THIS worktree — and fails honestly."""
    wt = snapshot(fake, "fixed")
    regress(wt)
    result = run(
        [str(wt / "scripts" / "orch_verify.sh"), "test"],
        wt,
        # a stale/inherited VIRTUAL_ENV must not be trusted by the wrapper
        bare_env(ORCH_VERIFY_PACKAGE=MODULE, VIRTUAL_ENV="/nonexistent/venv"),
    )
    out = result.stdout + result.stderr
    row = marker_row(out)
    assert row["module"] == MODULE
    assert row["module_file"] == str(wt / "src" / MODULE / "__init__.py")
    assert row["worktree_root"] == str(wt)
    assert str(fake["python"]) in out  # the main repo venv, by absolute path
    assert "orch-verify: test root" in out
    assert result.returncode != 0, "a broken branch must not pass: " + out
    assert "1 failed" in out


def test_wrapper_passes_in_a_clean_worktree(fake, home):
    """DoD 2: from a clean worktree with no venv of its own it runs, and is green."""
    wt = snapshot(fake, "clean")
    assert not (wt / ".venv").exists()
    result = run(
        [str(wt / "scripts" / "orch_verify.sh"), "test"],
        wt,
        bare_env(ORCH_VERIFY_PACKAGE=MODULE),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert f"orch-verify: {MODULE}.__file__ = {wt / 'src' / MODULE / '__init__.py'}" in (
        result.stdout
    )


def test_wrapper_guard_trips_when_the_resolution_is_wrong(fake, home):
    """(c) wrong resolution -> loud, non-zero, and the tool never runs."""
    stray = fake["main"] / "src" / "straypkg"
    try:
        stray.mkdir(exist_ok=True)
        (stray / "__init__.py").write_text('VALUE = "main-only"\n')  # untracked: not in wt
        wt = snapshot(fake, "guard")
        assert not (wt / "src" / "straypkg").exists()

        result = run(
            [str(wt / "scripts" / "orch_verify.sh"), "test"],
            wt,
            bare_env(ORCH_VERIFY_PACKAGE="straypkg"),
        )
        assert result.returncode == 3, result.stdout + result.stderr
        assert "OUTSIDE the worktree" in result.stderr
        assert "1 passed" not in result.stdout  # pytest was never reached
        assert marker_row(result.stderr)["module_file"].startswith(str(fake["main"]))
    finally:
        shutil.rmtree(stray, ignore_errors=True)


def test_wrapper_rejects_bad_usage_and_non_repos(fake, home):
    wt = snapshot(fake, "usage")
    bad = run([str(wt / "scripts" / "orch_verify.sh"), "nope"], wt, bare_env())
    assert bad.returncode == 2 and "usage:" in bad.stderr
    outside = run([str(WRAPPER_SRC), "resolve"], home, bare_env())
    assert outside.returncode == 4 and "not inside a git repository" in outside.stderr


def test_wrapper_keeps_the_exec_bit_into_worktrees(fake, home):
    wt = snapshot(fake, "mode")
    mode = (wt / "scripts" / "orch_verify.sh").stat().st_mode
    assert mode & stat.S_IXUSR, "git must carry the exec bit into the worktree"


def test_doctor_reports_the_trap_when_the_command_is_not_path_independent(fake, home, session):
    """(d) the same trap surfaces at registration time instead of mid-gate."""
    project = project_service.create(
        session,
        ProjectCreate(
            name="Trap Project",
            path=str(fake["main"]),
            test_command=f"{fake['python']} -m pytest tests",
        ),
    )
    checks = project_service.doctor(project, run_commands=True)
    by_name = {c["name"]: c for c in checks}
    assert by_name["test in worktree"]["ok"] is True  # it exits 0 — that is the false pass
    trap = by_name["test resolves inside the worktree"]
    assert trap["ok"] is False
    assert "not path-independent" in trap["detail"]
    assert "orch_verify.sh" in trap["detail"]


def test_doctor_passes_for_the_wrapper_command(fake, home, session):
    project = project_service.create(
        session,
        ProjectCreate(
            name="Wrapped Project",
            path=str(fake["main"]),
            test_command=f"env ORCH_VERIFY_PACKAGE={MODULE} scripts/orch_verify.sh test",
        ),
    )
    checks = project_service.doctor(project, run_commands=True)
    by_name = {c["name"]: c for c in checks}
    assert by_name["test in worktree"]["ok"] is True, by_name
    resolved = by_name["test resolves inside the worktree"]
    assert resolved["ok"] is True, resolved["detail"]
    assert "vercheck -> " in resolved["detail"]
    # the throwaway worktree is removed again, no marker left behind
    assert git_service.managed_worktrees() == []


def test_doctor_static_checks_still_describe_the_main_checkout(home, session, tmp_path):
    """Pin of the static check's semantics: doctor answers "would this run in the main
    checkout?", and does NOT go looking for the file on the base branch. T-6 only adds the
    opt-in --run-commands probe; changing what the static check means was not in scope."""
    main = tmp_path / "branchonly"
    (main / "src").mkdir(parents=True)
    (main / "src" / "pkg.py").write_text("")
    git(main, "init", "-q", "-b", "main")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "no wrapper in the checkout")
    project = project_service.create(
        session,
        ProjectCreate(name="Branch Only", path=str(main), test_command="scripts/verify.sh test"),
    )
    check = {c["name"]: c for c in project_service.doctor(project)}["test command"]
    assert not check["ok"] and check["detail"] == "`scripts/verify.sh` not found"


def test_doctor_removes_the_worktree_when_a_command_fails(fake, home, session):
    """Failure paths must not leave a worktree behind in ORCHESTRATOR_HOME: a red command,
    and a command that cannot even be started (exit code None)."""
    for label, command in (
        ("red", f"{sys.executable} -c 'raise SystemExit(3)'"),
        ("missing-executable", "/nonexistent/dir/orch_verify.sh test"),
    ):
        project = project_service.create(
            session,
            ProjectCreate(name=f"Fail {label}", path=str(fake["main"]), test_command=command),
        )
        checks = project_service.doctor(project, run_commands=True)
        by_name = {c["name"]: c for c in checks}
        assert by_name["test in worktree"]["ok"] is False, (label, by_name)
        assert by_name["test resolves inside the worktree"]["ok"] is False, label
        assert git_service.managed_worktrees() == [], f"{label} left a worktree behind"
        assert not [c for c in checks if c["name"] == "throwaway worktree removed"], label


def test_doctor_survives_a_missing_base_branch(home, session, tmp_path):
    """A base branch that no longer exists must read as a failed check, never as an exception
    out of the CLI — both for the static check and for the opt-in probe, where
    `git_service.ensure_snapshot` raises GitError before any worktree is created."""
    main = make_repo(tmp_path / "gonebase")  # scripts/verify.sh is not in it
    project = project_service.create(
        session,
        ProjectCreate(
            name="Gone Base", path=str(main), base_branch="no-such-branch",
            test_command="scripts/verify.sh test",
        ),
    )
    checks = {c["name"]: c for c in project_service.doctor(project, run_commands=True)}
    assert checks[f"base branch '{project.base_branch}' exists"]["ok"] is False
    assert checks["test command"]["ok"] is False
    assert "not found" in checks["test command"]["detail"]
    probe = checks["commands run in a worktree"]
    assert probe["ok"] is False and "does not exist" in probe["detail"]
    assert git_service.managed_worktrees() == []


def test_doctor_never_runs_commands_without_the_flag(fake, home, session):
    project = project_service.create(
        session,
        ProjectCreate(
            name="Quiet Project", path=str(fake["main"]), test_command=f"{fake['python']} -m pytest"
        ),
    )
    checks = project_service.doctor(project)
    assert not [c for c in checks if "worktree" in c["name"]]
    assert git_service.managed_worktrees() == []


def test_run_simple_sync_matches_run_simple_semantics(tmp_path):
    code, out, timed_out = run_simple_sync([sys.executable, "-c", "print('hi')"], tmp_path, 60)
    assert (code, timed_out) == (0, False) and "hi" in out
    code, out, timed_out = run_simple_sync(["definitely-not-an-executable"], tmp_path, 60)
    assert code is None and timed_out is False and "failed to start" in out
    code, out, timed_out = run_simple_sync(
        [sys.executable, "-c", "import time; time.sleep(20)"], tmp_path, 1
    )
    assert timed_out is True and code != 0
