"""Shared fixtures: every test gets an isolated ORCHESTRATOR_HOME and no network."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.core import config as config_mod
from orchestrator.core import db as db_mod
from orchestrator.core.config import PROVIDER_ENV_NAMES, settings_env_names

# DERIVED from the Settings model (T-19 DoD 1), not hand-kept: a repo-level `.env` leaks into
# os.environ when a test module imports the app at import time (get_settings() -> load_env_file()
# -> load_dotenv), and that survives the chdir below. Provider SDKs read their own variables
# directly, so those stay explicit.
AMBIENT_ENV: tuple[str, ...] = (*PROVIDER_ENV_NAMES, *settings_env_names())

# The environment before any test module was imported. Collection-time `.env` reads export
# arbitrary keys (OPENAI_*, ANTHROPIC_BASE_URL, proxies), so restoring this baseline — not just
# AMBIENT_ENV — is what makes "a repo-level .env is inert" true.
_ENVIRON_AT_IMPORT: dict[str, str] = dict(os.environ)


def env_names_not_covered(
    clear_list: tuple[str, ...] | None = None,
    model: type | None = None,
) -> set[str]:
    """Env-visible names the hermeticity guard would leave untouched (model-aware, so an aliased
    field counts). Callers can override the clear-list to model the old hand-kept shape."""
    if clear_list is not None:
        covered = set(clear_list)
    elif model is None:
        covered = set(AMBIENT_ENV)
    else:
        covered = set((*PROVIDER_ENV_NAMES, *settings_env_names(model)))
    visible = set(PROVIDER_ENV_NAMES) | set(settings_env_names(model))
    return visible - covered


def restore_import_environment() -> None:
    """Undo everything a collection-time `.env` read exported into this process.

    Only keys absent at conftest import are dropped and existing values are restored. `PYTEST_*`
    is preserved: pytest writes its per-test bookkeeping var and pops it on teardown.
    """
    for name in [
        n for n in os.environ if n not in _ENVIRON_AT_IMPORT and not n.startswith("PYTEST_")
    ]:
        os.environ.pop(name, None)
    for name, value in _ENVIRON_AT_IMPORT.items():
        if os.environ.get(name) != value:
            os.environ[name] = value


def clear_ambient_environment() -> None:
    """Make this process look like a tracked-files-only worktree, then drop the settings cache.

    Matching is case-insensitive: pydantic matches ORCHESTRATOR_* that way, so a lowercase key in
    `.env` is still ambient configuration.
    """
    targets = {name.lower() for name in AMBIENT_ENV}
    for name in [n for n in os.environ if n.lower() in targets]:
        os.environ.pop(name, None)
    config_mod.reset_settings_cache()


@pytest.fixture(scope="session", autouse=True)
def _hermetic_environment():
    """Re-apply environment hygiene AFTER collection imports have had their chance.

    A module-level `import orchestrator.api.main` executes create_app() during collection, which can
    load the repo `.env` while cwd is still the repo. This restores the import-time environment and
    closes both readers (get_settings()'s helper and pydantic-settings' `env_file`), so a
    cwd-relative `.env` is never ambient for the rest of the session; an explicit path still works.
    """
    mp = pytest.MonkeyPatch()
    original_load_env_file = config_mod.load_env_file

    def guarded_load_env_file(path=".env") -> None:
        candidate = Path(path)
        if not candidate.is_absolute() and candidate == Path(".env"):
            return
        original_load_env_file(path)

    mp.setattr(config_mod, "load_env_file", guarded_load_env_file)
    mp.setitem(config_mod.Settings.model_config, "env_file", None)
    restore_import_environment()
    clear_ambient_environment()
    try:
        yield
    finally:
        mp.undo()
        restore_import_environment()
        clear_ambient_environment()


@pytest.fixture(autouse=True)
def _no_real_notification_daemon(monkeypatch):
    """No test may reach the operator's real daemon or D-Bus session (T-18 DoD 5)."""
    from orchestrator.services import notification_service

    monkeypatch.setattr(notification_service, "_run_client",
                        lambda argv: notification_service.ClientReply(
                            argv[0], "blocked by the test suite"))
    monkeypatch.setattr(notification_service, "probe_capabilities",
                        lambda: notification_service.UNPROBED)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "orch-home"
    known = {name.lower() for name in AMBIENT_ENV}
    for name in [n for n in os.environ if n.lower() in known]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(h))
    monkeypatch.setenv("ORCHESTRATOR_ENABLE_WORKER", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLE_SCHEDULER", "false")
    monkeypatch.delenv("ORCHESTRATOR_DB_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # keep any repo-level .env out of the settings
    config_mod.reset_settings_cache()
    db_mod.dispose_engines()
    yield h
    db_mod.dispose_engines()
    config_mod.reset_settings_cache()


@pytest.fixture
def initialized(home) -> Path:
    from orchestrator.services import bootstrap

    bootstrap.init()
    return home


@pytest.fixture
def session(initialized):
    with db_mod.session_scope() as s:
        yield s


def make_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, env=env)
    for name, content in (files or {"README.md": "# test\n"}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, env=env)
    return path


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


# --- T-16 DoD 8: expensive environment/parity scenarios are opt-in, and the gate's own review
# setup must not register or remove a worktree in the operator's repository. ------------------

PARITY_ENV = "ORCH_ENV_PARITY_TESTS"


def parity_tests_enabled() -> bool:
    return os.environ.get(PARITY_ENV) == "1"


def pytest_collection_modifyitems(config, items) -> None:
    """Skip `parity`-marked tests unless the operator opts in.

    The marker exists so a nested-suite/env-parity scenario is never an accidental member of the
    gated suite: one of them spawns a second pytest process and clones the checkout, and DoD 8
    bounds what the gate may cost per round. It is deliberately a marker plus a hook (not a
    module-level skip) so the env var can include the tests on demand: set
    `ORCH_ENV_PARITY_TESTS=1` and they run (add `-m parity` to select only them). `-m parity`
    WITHOUT the env var still skips them, because this hook runs after marker selection.
    """
    if parity_tests_enabled():
        return
    skip = pytest.mark.skip(reason=f"parity scenario; opt in with {PARITY_ENV}=1")
    for item in items:
        if "parity" in item.keywords:
            item.add_marker(skip)


def _git_common_dir(repo: Path) -> Path | None:
    res = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if res.returncode != 0:
        return None
    common = Path(res.stdout.strip())
    if not common.is_absolute():
        common = (repo / common).resolve()
    return common


def worktree_listing(repo: Path) -> list[str]:
    """`git worktree list --porcelain` entries for the repository containing `repo`."""
    common = _git_common_dir(repo)
    if common is None:
        return []
    listing = subprocess.run(
        ["git", "--git-dir", str(common), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if listing.returncode != 0:
        return []
    return sorted(
        line.strip() for line in listing.stdout.splitlines() if line.startswith("worktree ")
    )


def worktree_fingerprint(repo: Path) -> dict[str, object]:
    """A stable snapshot of a repository's registered worktrees.

    Only facts that really mean "a registration changed" are compared: the `git worktree list
    --porcelain` listing and the registry entry names under `<common>/worktrees`. mtimes are
    deliberately absent. An index refresh, a `FETCH_HEAD`/`ORIG_HEAD` write, a pack rewrite, the
    harness's own merge or a doctor probe all touch the shared git directory without adding or
    removing a worktree, so an mtime-based failure would flake every gated run (T-16 review
    round 5, finding 1). Transient add+prune is caught by WorktreeMutationSpy instead. An
    unreadable registry raises, never returns an empty list, so the session guard cannot
    silently skip itself.
    """
    common = _git_common_dir(repo)
    if common is None:
        raise RuntimeError(f"{repo} is not inside a git repository")
    listing = subprocess.run(
        ["git", "--git-dir", str(common), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if listing.returncode != 0:
        raise RuntimeError(
            "git worktree list failed: "
            + (listing.stderr.strip() or listing.stdout.strip() or f"exit {listing.returncode}")
        )
    registry = common / "worktrees"
    entries: list[str] = []
    if registry.exists():
        try:
            entries = sorted(path.name for path in registry.iterdir())
        except OSError as exc:
            raise RuntimeError(f"cannot read worktree registry {registry}: {exc}") from exc
    return {
        "listing": sorted(
            line.strip()
            for line in listing.stdout.splitlines()
            if line.startswith("worktree ")
        ),
        "registry": str(registry),
        "entries": entries,
    }


def worktree_fingerprint_changed(
    before: dict[str, object], after: dict[str, object]
) -> bool:
    return before.get("listing") != after.get("listing") or before.get("entries") != after.get(
        "entries"
    )


def worktree_guard_failure(
    before: dict[str, object],
    after: dict[str, object],
    recorded: list[list[str]],
) -> str | None:
    """Describe a guard violation without claiming a cause the evidence cannot prove.

    A registry entry change with no recorded suite call may come from the operator, the harness
    or another checkout, so the message reports both facts instead of blaming the suite
    (T-16 review round 5, finding 1(c)).
    """
    registry_changed = worktree_fingerprint_changed(before, after)
    if not registry_changed and not recorded:
        return None
    calls = "; ".join(" ".join(call) for call in recorded) if recorded else "none"
    listing_detail = f"before={before.get('listing')} after={after.get('listing')}"
    if recorded:
        return (
            "the gated suite ran `git worktree add|remove|prune` against the operator "
            f"repository, which the gate forbids. recorded git worktree calls: {calls}. "
            f"registry entries changed during the session: {registry_changed}. {listing_detail}"
        )
    return (
        "the operator repository's registry entries changed during the session; the suite's "
        f"own recorded git worktree calls were: {calls}. No cause is attributed to the suite: "
        "another process (the harness or the operator) may have changed the registry. "
        f"{listing_detail}"
    )


def _worktree_mutation_argv(argv: object) -> bool:
    if not isinstance(argv, (list, tuple)) or not argv:
        return False
    tokens = [str(part) for part in argv]
    if Path(tokens[0]).name.lower() not in {"git", "git.exe"}:
        return False
    return any(
        tokens[index] == "worktree" and tokens[index + 1] in {"add", "remove", "prune"}
        for index in range(len(tokens) - 1)
    )


def _invocation_targets(argv: object, cwd: object) -> list[Path]:
    if not isinstance(argv, (list, tuple)):
        return []
    tokens = [str(part) for part in argv]
    raw: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-C" and index + 1 < len(tokens):
            raw.append(tokens[index + 1])
            index += 2
            continue
        if token.startswith("--git-dir="):
            raw.append(token.split("=", 1)[1])
        elif token == "--git-dir" and index + 1 < len(tokens):
            raw.append(tokens[index + 1])
            index += 1
        elif token.startswith("--work-tree="):
            raw.append(token.split("=", 1)[1])
        elif token == "--work-tree" and index + 1 < len(tokens):
            raw.append(tokens[index + 1])
            index += 1
        index += 1
    if cwd is not None:
        raw.append(str(cwd))
    return [Path(item) for item in raw]


def _git_dir_for(path: Path) -> Path | None:
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if resolved.is_file():
        resolved = resolved.parent
    for candidate in (resolved, *resolved.parents):
        dotgit = candidate / ".git"
        try:
            if dotgit.is_dir():
                return dotgit.resolve()
            if dotgit.is_file():
                text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
                if text.startswith("gitdir:"):
                    target = Path(text.split(":", 1)[1].strip())
                    if not target.is_absolute():
                        target = candidate / target
                    return target.resolve()
        except OSError:
            continue
    return None


def _targets_operator_repo(target: Path, operator_common: Path) -> bool:
    try:
        resolved = target.resolve()
    except OSError:
        return False
    if resolved == operator_common or resolved.is_relative_to(operator_common):
        return True
    git_dir = _git_dir_for(resolved)
    if git_dir is None:
        return False
    return git_dir == operator_common or git_dir.is_relative_to(operator_common)


class WorktreeMutationSpy:
    """Record operator-repository `worktree add|remove|prune` calls as they happen.

    A start/end snapshot cannot see a transient add+prune, and an mtime is noisy because
    ordinary git activity touches the shared directory (review round 5, finding 1(a)/(b)). The
    spy observes the call itself and only for the operator's repository, so worktrees created
    inside a test's own temp repo are ignored.
    """

    def __init__(self, operator_common: Path) -> None:
        self.operator_common = Path(operator_common).resolve()
        self.records: list[list[str]] = []

    def _record(self, argv: object, cwd: object) -> None:
        if not _worktree_mutation_argv(argv):
            return
        if not any(
            _targets_operator_repo(target, self.operator_common)
            for target in _invocation_targets(argv, cwd)
        ):
            return
        record = [str(part) for part in argv]  # type: ignore[union-attr]
        if record not in self.records:
            self.records.append(record)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_run = subprocess.run
        original_popen = subprocess.Popen
        spy = self

        def run(*args, **kwargs):
            spy._record(args[0] if args else kwargs.get("args"), kwargs.get("cwd"))
            return original_run(*args, **kwargs)

        class SpyPopen(original_popen):  # type: ignore[misc, valid-type]
            def __init__(self, *args, **kwargs):
                spy._record(args[0] if args else kwargs.get("args"), kwargs.get("cwd"))
                original_popen.__init__(self, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(subprocess, "Popen", SpyPopen)

        from orchestrator.utils import git as git_mod

        original_run_git = git_mod.run_git

        def run_git(args, cwd, *rest, **kwargs):
            spy._record(["git", *[str(part) for part in args]], cwd)
            return original_run_git(args, cwd, *rest, **kwargs)

        monkeypatch.setattr(git_mod, "run_git", run_git)


def operator_repo_worktrees() -> list[str]:
    repo = Path(__file__).resolve().parents[1]
    return worktree_listing(repo)


def operator_repo_worktree_fingerprint() -> dict[str, object]:
    return worktree_fingerprint(Path(__file__).resolve().parents[1])


@pytest.fixture(scope="session", autouse=True)
def _operator_repo_worktrees_untouched():
    """The gated suite may create worktrees in its own temp repos, never in the operator's.

    Measured motivation (T-16 DoD 8, reviews.26 finding 7): a test that registered a worktree in
    the shared repository while the gate was running perturbed the very state the gate was
    inspecting. Persistent changes are compared by registry entry names, transient add+prune is
    caught by an in-process spy, and neither depends on a shared-git-directory mtime, which
    ordinary git activity changes (T-16 review round 5, finding 1). A registry it cannot read is
    still a loud failure rather than a silent skip (review round 1, finding 2).
    """
    if shutil.which("git") is None:
        pytest.skip("git is not installed: the operator worktree guard cannot run")
    repo = Path(__file__).resolve().parents[1]
    try:
        common = _git_common_dir(repo)
        if common is None:
            raise RuntimeError(f"{repo} is not inside a git repository")
        before = worktree_fingerprint(repo)
    except RuntimeError as exc:
        pytest.fail(f"cannot fingerprint the operator repository worktree registry: {exc}")

    spy = WorktreeMutationSpy(common)
    patch = pytest.MonkeyPatch()
    spy.install(patch)
    try:
        yield
    finally:
        patch.undo()

    try:
        after = worktree_fingerprint(repo)
    except RuntimeError as exc:
        pytest.fail(f"cannot re-read the operator repository worktree registry: {exc}")
    failure = worktree_guard_failure(before, after, spy.records)
    assert failure is None, failure
