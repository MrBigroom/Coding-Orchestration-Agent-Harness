"""T-45: every operator-facing command hint must be a command the real CLI accepts.

The shipped recovery hint told the operator to run ``orchestrator sync reinit --dry-run``, which
exits with ``No such option: --dry-run``: the dry run is the default and has no flag. A suite that
compared the hint to its own literal stayed green, so the audit below is driven from the code and
from the real Typer/Click tree, never from a hand-maintained list or a rendered phrase.

What is checked, and how
------------------------
* **Recovery strings the transport can emit** are produced by driving the classifier (every
  ``RECOVERY_BY_KIND`` key and every response class it handles), not by hard-coding the sentences.
  Each command is parsed with ``make_context`` so no callback, database or network is touched, and
  an AST pass over ``sync_transport`` asserts every string literal that mentions ``orchestrator ``
  is one of the emitted strings, so a new branch cannot ship an unvalidated hint.
* **Python source**: an AST walk over every ``src/orchestrator/**/*.py`` string expression
  (``Constant``, ``JoinedStr`` and concatenation ``BinOp``, so f-strings and composed fragments are
  included) resolves every ``orchestrator <group> <command>`` / ``orchestrator <root-command>``
  against the real app. Every ``--flag`` in the same string *after* that command is matched
  against that command's declared options -- including a flag written in its own backticks or
  span, which is the shape that previously escaped the audit.
* **GUIs**: every command-shaped string in ``src/orchestrator/api/static_fallback/index.html`` and
  every ``web/src/**/*.ts``/``*.tsx`` file is extracted from the raw file text (not only
  ``<code>``/``className="mono"`` spans), and its flags are resolved the same way. ``<code>``
  blocks and mono spans are additionally parsed end-to-end through Click.
* **Notification bodies**: the ``sync_failed`` body is asserted to name the runnable command and
  the skip behaviour (payload conflicts are reported as ``conflicts_skipped`` and left untouched),
  because a structured hint that promises a repair reinit cannot make is the same defect from the
  other direction.

Placeholder rule: ``<id>``/``{label}``-style holes, plus required positional arguments, get the
concrete value ``T-45`` before parsing, so a command *template* is validated as an operator would
run it. Option values (``--args '[...]'``) are left as written; Click only needs a value.

Classes this test cannot validate (listed, not silently skipped)
----------------------------------------------------------------
1. Markdown prose in ``README.md`` and ``docs/*.md``. Those are operator documentation, not code
   the harness emits; DoD 1 corrects them by hand and ``live_cli.txt`` guards the stale string with
   ``git grep``. Files read/grepped for this project: ``README.md``, ``docs/cloud-sync.md``,
   ``docs/decisions.md``.
2. A bare flag or bare subcommand fragment with no ``orchestrator`` binary and no command earlier
   in the *same* string expression (for example a sentence that says "add ``--at-tip``" without
   naming the command). It cannot be attributed to one command without guessing; the composed
   fragments in ``evidence_freshness.py`` are covered because the concatenation is unparsed as one
   expression before scanning.
3. Non-command English that happens to contain the word "orchestrator" (for example "the
   orchestrator configures per run"); the word after it does not resolve to a real root command or
   group, so it is skipped rather than guessed at.
4. Historical evidence under ``verify-evidence/`` is deliberately not scanned: it records what
   earlier rounds measured, including the broken string.

Files/paths scanned to make the absence claim above: ``src/orchestrator/**/*.py`` (115 files),
``web/src/**/*.tsx`` and ``web/src/**/*.ts`` (29 files), and
``src/orchestrator/api/static_fallback/index.html``.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path

import click
import httpx
import pytest
from typer.main import get_command

from orchestrator.cli import app
from orchestrator.core.notification_vocab import TRIGGERS
from orchestrator.services import sync_transport

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "orchestrator"
FALLBACK_UI = SRC_ROOT / "api" / "static_fallback" / "index.html"
WEB_SRC = REPO_ROOT / "web" / "src"

# ``orchestrator <first>`` or ``orchestrator <first> <second>``; resolution decides which are
# real commands, so prose and CSS variables never reach the parser.
COMMAND = re.compile(r"orchestrator\s+([a-z][a-z0-9-]*)(?:\s+([a-z][a-z0-9-]*))?")
FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")
PLACEHOLDER = re.compile(r"<[^>]+>|\{[^}]+\}")
BACKTICKED = re.compile(r"`{1,2}(orchestrator [^`\n]+)`{1,2}")
CODE_BLOCK = re.compile(r"<code>(orchestrator [^<]+)</code>")
MONO_SPAN = re.compile(r'className="mono">(orchestrator [^<]+)</span>')


def _roots() -> dict[str, click.Command]:
    return dict(get_command(app).commands)


def _accepted_flags(command: click.Command) -> set[str]:
    """The long options the real command declares (renames make this set change)."""
    flags: set[str] = set()
    for param in command.params:
        for opt in (*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ())):
            if opt.startswith("--"):
                flags.add(opt)
    return flags


def _parse_cli(command: str) -> None:
    """Parse one concrete command with the real CLI tree, without executing its callback."""
    argv = shlex.split(PLACEHOLDER.sub("T-45", command))
    assert argv and argv[0] == "orchestrator", command
    if len(argv) == 2 and argv[1] in {"--help", "--version"}:
        return  # a root-level flag, not a subcommand hint
    tree = get_command(app)
    context = tree.context_class(tree, info_name="orchestrator")
    rest = argv[1:]
    while hasattr(tree, "resolve_command"):
        name, tree, rest = tree.resolve_command(context, rest)
        context = tree.context_class(tree, info_name=name, parent=context)
    parsed = tree.make_context(context.info_name, rest, parent=context.parent)
    parsed.close()


def _parse_command_path(tokens: list[str], command: click.Command) -> None:
    """Parse a command path, filling only the required arguments/options a hint omits."""
    argv = list(tokens)
    for param in command.params:
        # Typer vendors its own click, so duck-type the parameter kind instead of isinstance().
        if getattr(param, "param_type_name", None) == "argument" and param.required:
            count = param.nargs if isinstance(param.nargs, int) and param.nargs > 0 else 1
            argv.extend(["T-45"] * count)
        elif getattr(param, "param_type_name", None) == "option" and param.required:
            flag = param.opts[0] if param.opts else param.name
            argv.extend([flag, "T-45"])
    _parse_cli("orchestrator " + " ".join(argv))


def _resolved_commands(text: str, roots: dict[str, click.Command]):
    """Yield ``(tokens, command, match_start, match_end)`` for real command-shaped matches."""
    for match in COMMAND.finditer(text):
        first, second = match.group(1), match.group(2)
        command = roots.get(first)
        if command is None:
            continue
        if hasattr(command, "commands"):  # a group: the next word must be one of its commands
            if second is None or second not in command.commands:
                continue
            yield [first, second], command.commands[second], match.start(), match.end()
        else:
            yield [first], command, match.start(), match.end()


def _audit_text(text: str) -> dict[str, dict[str, object]]:
    """Resolve every command-shaped string and the flags that follow it in the same text.

    The segment for a command runs to the next command-shaped match, so a flag written in its own
    backticks or its own JSX span is still validated against the command it belongs to. Resolution
    against the real tree is what skips prose: "the orchestrator configures" is not a command.
    """
    roots = _roots()
    matches = list(_resolved_commands(text, roots))
    audit: dict[str, dict[str, object]] = {}
    for index, (tokens, command, start, end) in enumerate(matches):
        segment_end = matches[index + 1][2] if index + 1 < len(matches) else len(text)
        flags = set(FLAG.findall(text, end, segment_end))
        name = "orchestrator " + " ".join(tokens)
        entry = audit.setdefault(name, {"command": command, "flags": set(), "start": start})
        entry["flags"] = set(entry["flags"]) | flags
    return audit


def _assert_commands_are_real(audit: dict[str, dict[str, object]]) -> None:
    for name, entry in audit.items():
        command = entry["command"]
        assert hasattr(command, "params"), name
        _parse_command_path(name.split()[1:], command)
        unknown = sorted(set(entry["flags"]) - _accepted_flags(command))
        assert not unknown, f"{name!r} carries flags the CLI does not declare: {unknown}"


def _string_expressions(module: ast.Module):
    """Every string expression in a module, including f-strings and ``+`` concatenations."""
    for node in ast.walk(module):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, (ast.JoinedStr, ast.BinOp)):
            try:
                text = ast.unparse(node)
            except Exception:  # noqa: BLE001, S112 - unparse is best-effort, never the only source
                continue
            if "orchestrator " in text:
                yield getattr(node, "lineno", 0), text


def _audit_python_tree() -> dict[str, dict[str, object]]:
    audit: dict[str, dict[str, object]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, text in _string_expressions(module):
            for name, entry in _audit_text(text).items():
                where = f"{path.relative_to(REPO_ROOT)}:{lineno}"
                entry["sources"] = set(entry.get("sources", set())) | {where}
                existing = audit.get(name)
                if existing is None:
                    audit[name] = entry
                else:
                    existing["flags"] = set(existing["flags"]) | set(entry["flags"])
                    existing["sources"] = set(existing["sources"]) | set(entry["sources"])
    return audit


def _audit_gui_files() -> dict[str, dict[str, object]]:
    files = [FALLBACK_UI, *sorted(WEB_SRC.rglob("*.tsx")), *sorted(WEB_SRC.rglob("*.ts"))]
    audit: dict[str, dict[str, object]] = {}
    for path in files:
        for name, entry in _audit_text(path.read_text(encoding="utf-8")).items():
            where = str(path.relative_to(REPO_ROOT))
            entry["sources"] = set(entry.get("sources", set())) | {where}
            existing = audit.get(name)
            if existing is None:
                audit[name] = entry
            else:
                existing["flags"] = set(existing["flags"]) | set(entry["flags"])
                existing["sources"] = set(existing["sources"]) | set(entry["sources"])
    return audit


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


def test_every_recovery_string_the_transport_can_emit_parses_against_the_cli():
    emitted = _emitted_recovery_strings()
    audits = {label: _audit_text(text) for label, text in emitted.items()}
    assert any(audits.values()), f"no command-shaped recovery hint found in {sorted(emitted)}"
    for audit in audits.values():
        _assert_commands_are_real(audit)
        for text in audit:
            _parse_cli(text)

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


def test_sync_failed_notification_names_a_runnable_command_and_the_skip_behaviour():
    """The body must not point at a repair reinit cannot perform (reinit skips payload conflicts)."""
    action = TRIGGERS["sync_failed"].action
    assert "orchestrator sync reinit" in action
    assert "conflicts_skipped" in action
    assert "left untouched" in action
    assert "--dry-run" not in action
    audit = _audit_text(action)
    _assert_commands_are_real(audit)
    for command in audit:
        _parse_cli(command)


def test_the_validator_rejects_the_flag_that_shipped():
    """Control: the check above is only useful if it fails on the old hint."""
    with pytest.raises(Exception) as excinfo:
        _parse_cli("orchestrator sync reinit --dry-run")
    assert type(excinfo.value).__name__ == "NoSuchOption"
    assert "No such option: --dry-run" in str(excinfo.value)


def test_the_recovery_enumeration_catches_a_broken_hint_runtime_patched(monkeypatch):
    """Mutation check: the enumeration test must fail if the emitted hint itself is wrong."""
    monkeypatch.setattr(
        sync_transport,
        "VERSION_REJECTION_RECOVERY",
        "run `orchestrator sync reinit --dry-run`",
    )
    emitted = _emitted_recovery_strings()
    with pytest.raises(AssertionError):
        for text in emitted.values():
            _assert_commands_are_real(_audit_text(text))


def test_a_flag_in_its_own_backticks_breaks_the_audit_when_it_is_wrong():
    """The previous audit parsed only the command span, so a separate ``--flag`` escaped it."""
    good = "run `orchestrator sync reinit` (the dry run is the default and `--apply` writes)"
    bad = "run `orchestrator sync reinit` (the dry run is the default and `--dry-run` writes)"
    assert _audit_text(good)["orchestrator sync reinit"]["flags"] == {"--apply"}
    _assert_commands_are_real(_audit_text(good))
    with pytest.raises(AssertionError, match="--dry-run"):
        _assert_commands_are_real(_audit_text(bad))


def test_every_command_shaped_string_in_the_python_source_parses_against_the_cli():
    audit = _audit_python_tree()
    assert len(audit) >= 10, sorted(audit)  # a scan that found nothing would pass vacuously
    sources = set().union(*(entry["sources"] for entry in audit.values()))
    files = {source.rsplit(":", 1)[0] for source in sources}
    assert "src/orchestrator/services/sync_transport.py" in files
    assert "src/orchestrator/core/notification_vocab.py" in files
    assert "src/orchestrator/services/evidence_freshness.py" in files
    assert "src/orchestrator/cli.py" in files
    assert "src/orchestrator/desktop/__init__.py" in files
    assert "orchestrator sync reinit" in audit
    assert "orchestrator task evidence" in audit
    # The f-string/composed fragments that name evidence flags are resolved with their command.
    evidence_flags = audit["orchestrator task evidence"]["flags"]
    assert {"--at-tip", "--full", "--only"} <= evidence_flags, evidence_flags
    _assert_commands_are_real(audit)


def test_every_command_shaped_string_in_the_guis_parses_and_carries_its_flags():
    audit = _audit_gui_files()
    assert "orchestrator sync reinit" in audit
    _assert_commands_are_real(audit)
    reinit = audit["orchestrator sync reinit"]
    assert "--apply" in reinit["flags"], reinit
    assert {"src/orchestrator/api/static_fallback/index.html",
            "web/src/pages/Settings.tsx"} <= reinit["sources"]

    fallback = FALLBACK_UI.read_text(encoding="utf-8")
    settings = (WEB_SRC / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    # End-to-end Click parse of the templates the GUIs print, including option values.
    for command in CODE_BLOCK.findall(fallback) + MONO_SPAN.findall(settings):
        _parse_cli(command)
    for name, text in (("static fallback", fallback), ("Settings.tsx", settings)):
        assert "orchestrator sync reinit --dry-run" not in text, name
        assert "orchestrator sync reinit" in text, name
        assert "--dry-run" not in text, name
        assert "conflicts_skipped" in text, name
