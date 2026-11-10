from typer.testing import CliRunner

from orchestrator.cli import app
from tests.conftest import requires_git

runner = CliRunner()


def test_init_doctor_and_lists(home):
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0 and "database migrated" in r.output
    for cmd in (
        ["providers", "list"],
        ["models", "list"],
        ["adapters", "list"],
        ["project", "list"],
        ["task", "list"],
        ["memory", "list"],
        ["schedule", "list"],
    ):
        res = runner.invoke(app, cmd)
        assert res.exit_code == 0, (cmd, res.output)


def test_task_lifecycle_commands(home):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["task", "create", "CLI task", "--tag", "mock", "--dod", "works"])
    assert r.exit_code == 0 and "T-1" in r.output
    assert runner.invoke(app, ["task", "block", "T-1", "--reason", "waiting"]).exit_code == 0
    assert runner.invoke(app, ["task", "unblock", "T-1"]).exit_code == 0
    assert runner.invoke(app, ["task", "cancel", "T-1"]).exit_code == 0  # dequeue
    r = runner.invoke(app, ["task", "accept", "T-1"])
    assert r.exit_code == 1 and "error" in r.output  # not in review
    assert runner.invoke(app, ["task", "dry-run", "T-1"]).exit_code == 0
    assert runner.invoke(app, ["prompt-preview", "--task", "T-1"]).exit_code == 0
    assert runner.invoke(app, ["memory", "add", "A note", "--approve"]).exit_code == 0
    assert runner.invoke(app, ["memory", "search", "note"]).exit_code == 0
    r = runner.invoke(app, ["schedule", "add", "s1", "*/10 * * * *"])
    assert r.exit_code == 0
    assert runner.invoke(app, ["schedule", "disable", "s1"]).exit_code == 0
    assert runner.invoke(app, ["schedule", "add", "bad", "nope"]).exit_code == 1
    r = runner.invoke(
        app, ["schedule", "add", "once", "--at", "2099-01-02 09:00", "--timezone", "Asia/Tokyo"]
    )
    assert r.exit_code == 0 and "2099-01-02 00:00" in r.stdout
    assert runner.invoke(app, ["schedule", "add", "none"]).exit_code == 1


@requires_git
def test_project_doctor_run_commands_flag(home, tmp_path):
    """--run-commands is opt-in: plain doctor never executes project code."""
    from orchestrator.services import git_service
    from tests.conftest import make_repo

    repo = make_repo(tmp_path / "cli-repo")
    runner.invoke(app, ["init"])
    assert runner.invoke(app, ["project", "add", "cli-repo", str(repo)]).exit_code == 0

    r = runner.invoke(app, ["project", "doctor", "cli-repo"])
    assert r.exit_code == 0, r.output
    assert git_service.managed_worktrees() == []  # nothing was executed, nothing created

    h = runner.invoke(app, ["project", "doctor", "--help"])
    assert "--run-commands" in h.output
    assert "--env-parity" in h.output
    assert "NOT the gate" in h.output

    # no commands configured -> the opt-in check reports that and still exits 0
    r = runner.invoke(app, ["project", "doctor", "cli-repo", "--run-commands"])
    assert r.exit_code == 0, r.output
    assert "worktree" in r.output


@requires_git
def test_demo_and_cleanup_dry_run(home):
    r = runner.invoke(app, ["demo"])
    assert r.exit_code == 0 and "accepted" in r.output
    r = runner.invoke(app, ["cleanup", "--dry-run"])
    assert r.exit_code == 0 and "would remove" in r.output


@requires_git
def test_cleanup_sweeps_orphaned_parity_residue(home, tmp_path):
    """T-20 DoD 2/3: `orchestrator cleanup` reclaims a killed parity run's tree."""
    from orchestrator.services import git_service
    from tests.conftest import make_repo

    assert runner.invoke(app, ["init"]).exit_code == 0
    repo = make_repo(tmp_path / "cleanup-parity")
    snapshot = git_service.ensure_snapshot(repo, "cleanup-parity", "env-parity", "main")
    (snapshot.path / ".env").write_text("SYNTHETIC_PARITY=synthetic\n")
    (snapshot.path / ".env.999-dead.parity").write_text("partial\n")

    dry = runner.invoke(app, ["cleanup", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "would remove parity residue" in dry.output
    assert snapshot.path.exists() and (snapshot.path / ".env").exists()

    real = runner.invoke(app, ["cleanup", "--no-dry-run"])
    assert real.exit_code == 0, real.output
    assert "removed parity residue" in real.output
    assert not snapshot.path.exists()
    assert git_service.managed_worktrees() == []


def test_reset_requires_flag(home):
    runner.invoke(app, ["init"])
    assert runner.invoke(app, ["reset"]).exit_code == 2
    assert runner.invoke(app, ["reset", "--i-understand-data-loss"]).exit_code == 0


def test_memory_triage_and_budget_commands(home, tmp_path):
    import json

    repo = tmp_path / "demo-repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "orch_verify.sh").write_text("#!/bin/sh\n")
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert (
        runner.invoke(
            app, ["project", "add", "Demo", str(repo), "--test-command", "make test"]
        ).exit_code
        == 0
    )
    added = runner.invoke(
        app,
        [
            "memory",
            "add",
            "T-9 decisions: (1) the gate blocks when an artifact is missing.",
            "--category",
            "project",
            "--scope",
            "gotchas",
            "--project",
            "demo",
        ],
    )
    assert added.exit_code == 0 and "pending" in added.output

    r = runner.invoke(app, ["memory", "triage"])
    assert r.exit_code == 0, r.output
    assert "rewrite-durable" in r.output and "task_shape" in r.output
    assert "if the whole queue were approved" in r.output
    assert runner.invoke(app, ["memory", "triage", "--action", "merge"]).exit_code == 0

    j = runner.invoke(app, ["memory", "triage", "--json"])
    assert j.exit_code == 0
    report = json.loads(j.output[j.output.index("{") :])
    assert report["totals"]["entries"] == 1 and report["rows"][0]["bucket"] == "project"
    assert report["budget_enabled"] is True

    b = runner.invoke(app, ["memory", "budget", "--project", "demo"])
    assert b.exit_code == 0 and "caps" in b.output


def test_memory_add_approve_over_the_cap_is_refused_unless_override(home, monkeypatch):
    from orchestrator.core import config as config_mod

    assert runner.invoke(app, ["init"]).exit_code == 0
    common = ["--category", "project", "--scope", "gotchas", "--project", "demo"]
    assert runner.invoke(app, ["memory", "add", "Keep rule " + "alpha " * 10, *common, "--approve"]).exit_code == 0
    monkeypatch.setenv("ORCHESTRATOR_MAX_MEMORY_TOKENS_PROJECT", "20")
    config_mod.reset_settings_cache()

    refused = runner.invoke(
        app, ["memory", "add", "New approved rule " + "gamma " * 10, *common, "--approve"]
    )
    assert refused.exit_code == 1, refused.output
    assert "nothing was written" in refused.output
    assert "override" in refused.output
    accepted = runner.invoke(
        app,
        ["memory", "add", "New approved rule " + "gamma " * 10, *common, "--approve", "--override"],
    )
    assert accepted.exit_code == 0, accepted.output


def test_memory_condense_requires_a_project(home):
    assert runner.invoke(app, ["init"]).exit_code == 0
    missing = runner.invoke(app, ["memory", "condense"])
    assert missing.exit_code == 2


def test_memory_condense_is_a_dry_run_until_apply_names_the_ids(home):
    import json

    assert runner.invoke(app, ["init"]).exit_code == 0
    for content in (
        "The wrapper must resolve the interpreter inside the worktree before running tests.",
        "Resolve the interpreter inside the worktree before running tests with the wrapper.",
    ):
        assert runner.invoke(
            app,
            [
                "memory", "add", content,
                "--category", "project", "--scope", "gotchas",
                "--project", "demo", "--approve",
            ],
        ).exit_code == 0

    dry = runner.invoke(app, ["memory", "condense", "--project", "demo"])
    assert dry.exit_code == 0, dry.output
    assert "dry run" in dry.output and "proposal keep" in dry.output

    report = runner.invoke(app, ["memory", "condense", "--project", "demo", "--json"])
    data = json.loads(report.output[report.output.index("{"):])
    proposal = data["proposals"][0]
    args = ["memory", "condense", "--project", "demo"]
    for entry_id in proposal["ids"]:
        args += ["--apply", str(entry_id)]
    # A rare-token duplicate keeps the survivor's wording, which does not contain the
    # absorbed row's text: without merged --content the apply must refuse (round-4 F2).
    refused = runner.invoke(app, args)
    assert refused.exit_code == 1, refused.output
    # Rich wraps the message: compare on whitespace-collapsed text, not raw output lines.
    assert "does not contain the content of" in " ".join(refused.output.split())
    applied = runner.invoke(
        app, args + ["--content", "Resolve the interpreter in the worktree before tests."]
    )
    assert applied.exit_code == 0, applied.output
    assert "applied: kept" in applied.output
