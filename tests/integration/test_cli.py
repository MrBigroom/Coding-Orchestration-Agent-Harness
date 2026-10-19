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


@requires_git
def test_demo_and_cleanup_dry_run(home):
    r = runner.invoke(app, ["demo"])
    assert r.exit_code == 0 and "accepted" in r.output
    r = runner.invoke(app, ["cleanup", "--dry-run"])
    assert r.exit_code == 0 and "would remove" in r.output


def test_reset_requires_flag(home):
    runner.invoke(app, ["init"])
    assert runner.invoke(app, ["reset"]).exit_code == 2
    assert runner.invoke(app, ["reset", "--i-understand-data-loss"]).exit_code == 0
