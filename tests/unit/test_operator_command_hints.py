"""T-45: every operator-facing command hint must be a command the real CLI accepts.

The shipped recovery hint told the operator to run ``orchestrator sync reinit --dry-run``, which
exits with ``No such option: --dry-run``: the dry run is the default and has no flag. The tests
below do not compare a literal to itself -- they hand every command-shaped hint the code can emit
to the real Typer/Click tree and fail on ``NoSuchOption``, a missing argument or an unknown
command. Parsing uses ``make_context`` and never invokes a callback, so a validation pass cannot
touch a database, the network or a worktree.

Placeholder rule: ``<id>``/``{label}``-style holes are substituted with the concrete value
``T-45`` before parsing, so a command *template* is validated as an operator would run it.

Enumeration rule: the recovery strings are produced by driving the code path (every
``RECOVERY_BY_KIND`` key plus every response class the classifier handles), and an AST scan of
``sync_transport`` asserts that every string literal in the module that mentions
``orchestrator `` is one of those emitted strings. A new classification branch therefore cannot
ship a hint the suite never saw. A second pass scans the whole ``src/orchestrator`` tree for
backticked and ``# command:`` hints, and the two GUIs are read for their command templates.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path

import httpx
import pytest
from typer.main import get_command

from orchestrator.cli import app
from orchestrator.services import sync_transport

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "orchestrator"
FALLBACK_UI = SRC_ROOT / "api" / "static_fallback" / "index.html"
SETTINGS_UI = REPO_ROOT / "web" / "src" / "pages" / "Settings.tsx"

BACKTICKED_COMMAND = re.compile(r"`{1,2}(orchestrator [^`\n]+)`{1,2}")
GENERATED_COMMAND = re.compile(r"# command: (orchestrator [^\n\"']+)")
PLACEHOLDER = re.compile(r"<[^>]+>|\{[^}]+\}")


def _parse_cli(command: str) -> None:
    """Parse one command with the real CLI tree, without executing its callback."""
    argv = shlex.split(PLACEHOLDER.sub("T-45", command))
    assert argv and argv[0] == "orchestrator", command
    if len(argv) == 2 and argv[1] in {"--help", "--version"}:
        return  # a root-level flag, not a subcommand hint
    command_tree = get_command(app)
    context = command_tree.context_class(command_tree, info_name="orchestrator")
    rest = argv[1:]
    while hasattr(command_tree, "resolve_command"):
        name, command_tree, rest = command_tree.resolve_command(context, rest)
        context = command_tree.context_class(command_tree, info_name=name, parent=context)
    parsed = command_tree.make_context(context.info_name, rest, parent=context.parent)
    parsed.close()


def _emitted_recovery_strings() -> dict[str, str]:
    """Every recovery sentence the transport can produce, keyed by its code path."""
    emitted = {
        f"recovery_for({kind!r})": sync_transport.recovery_for(kind)
        for kind in sync_transport.RECOVERY_BY_KIND
    }
    responses = {
        "version rejection": (400, "invalid version transition: 0 -> 3"),
        "credential refusal": (401, "invalid credentials"),
        "server error": (503, "service unavailable"),
        "request timeout": (408, "request timeout"),
        "rate limited": (429, "rate limited"),
    }
    for label, (status, message) in responses.items():
        error = sync_transport.SupabaseSyncClient._response_error(
            httpx.Response(status, json={"message": message})
        )
        if "recovery" in error.detail:
            emitted[f"_response_error -> {label}"] = error.detail["recovery"]
    return emitted


def _source_commands(root: Path) -> dict[str, str]:
    """Every backticked or generated ``orchestrator ...`` hint in the source tree."""
    commands: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in (BACKTICKED_COMMAND, GENERATED_COMMAND):
            for match in pattern.finditer(text):
                commands.setdefault(" ".join(match.group(1).split()), str(path.relative_to(REPO_ROOT)))
    return commands


def test_every_recovery_string_the_transport_can_emit_parses_against_the_cli():
    emitted = _emitted_recovery_strings()
    commands = [
        command
        for text in emitted.values()
        for command in BACKTICKED_COMMAND.findall(text)
    ]
    assert commands, f"no command-shaped recovery hint found in {sorted(emitted)}"
    for command in commands:
        _parse_cli(command)

    rejection = [text for text in emitted.values() if "sync reinit" in text]
    assert rejection == [sync_transport.VERSION_REJECTION_RECOVERY]

    # A new literal in this module must be reachable from the emitted set, so a new error branch
    # cannot add an unvalidated hint without turning this assertion red.
    module = ast.parse((SRC_ROOT / "services" / "sync_transport.py").read_text(encoding="utf-8"))
    literals = {
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "orchestrator " in node.value
    }
    assert literals <= set(emitted.values()), literals - set(emitted.values())


def test_version_rejection_recovery_names_a_command_that_runs_and_does_not_over_promise():
    error = sync_transport.SupabaseSyncClient._response_error(
        httpx.Response(400, json={"message": "invalid version transition: 0 -> 3"})
    )
    recovery = error.detail["recovery"]
    assert recovery == sync_transport.VERSION_REJECTION_RECOVERY
    assert "orchestrator sync reinit --dry-run" not in recovery
    assert "dry run is the default" in recovery
    assert "--apply" in recovery
    assert "conflicts_skipped" in recovery
    assert "left untouched" in recovery
    _parse_cli("orchestrator sync reinit")
    _parse_cli("orchestrator sync reinit --apply")


def test_the_validator_rejects_the_flag_that_shipped():
    """Control: the check above is only useful if it fails on the old hint."""
    with pytest.raises(Exception) as excinfo:
        _parse_cli("orchestrator sync reinit --dry-run")
    assert type(excinfo.value).__name__ == "NoSuchOption"
    assert "No such option: --dry-run" in str(excinfo.value)


def test_every_operator_command_hint_in_the_source_tree_parses_against_the_cli():
    commands = _source_commands(SRC_ROOT)
    assert len(commands) >= 10, commands  # the regex must keep finding the known hints
    for command, where in commands.items():
        try:
            _parse_cli(command)
        except Exception as exc:  # noqa: BLE001 - report the file and hint, not a bare traceback
            pytest.fail(f"{where} emits {command!r}, which the CLI rejects: {exc}")


def test_the_guis_name_commands_the_cli_accepts_and_drop_the_bad_flag():
    fallback = FALLBACK_UI.read_text(encoding="utf-8")
    settings = SETTINGS_UI.read_text(encoding="utf-8")
    commands = re.findall(r"<code>(orchestrator [^<]+)</code>", fallback)
    commands += re.findall(r'className="mono">(orchestrator [^<]+)</span>', settings)
    assert commands, "no GUI command templates found"
    for command in commands:
        _parse_cli(command)

    for name, text in (("static fallback", fallback), ("Settings.tsx", settings)):
        assert "orchestrator sync reinit --dry-run" not in text, name
        assert "orchestrator sync reinit" in text, name
        assert "--dry-run" not in text, name
