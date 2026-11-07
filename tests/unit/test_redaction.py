"""Anchoring the credential patterns, and what is NOT anchored.

T-8 DoD 10 added the lookbehind to the two `sk-` patterns so task/branch/phase names survive
redaction. The other credential patterns keep their historical `` anchors untouched — changing
them would widen a redaction control on a phase that was not asked to, and one of them
(`OPENAI_TOKEN_ghp_…`) is therefore missed today. That gap was found while writing these cases,
is measured by `test_underscore_joined_other_credentials_are_a_recorded_gap_not_a_regression`,
and is handed to the Architect as a separate decision instead of being silently fixed or
silently ignored.
"""
import time
from pathlib import Path

from orchestrator.utils import structured_log
from orchestrator.utils.redaction import (
    REDACTED,
    is_sensitive_path,
    redact,
    strip_sensitive_files_from_diff,
)


def test_redacts_provider_keys():
    text = "key sk-ant-api03-abcdefghijklmnopqrstuvwx and sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    out = redact(text)
    assert "sk-ant-api03" not in out and "sk-proj-ABC" not in out
    assert out.count(REDACTED) == 2


# --- T-8 DoD 10: the sk- patterns are anchored, so operational text survives -----------------
# `log_service.log_event` redacts EVERY event's details, and `redact()` used to run
# `sk-(?:proj-)?[A-Za-z0-9_\-]{20,}` with no leading word boundary — which matches INSIDE any
# word ending in "sk". Measured damage: build_log_entries.id=86 (merge_candidate) printed
# "git merge --no-ff ta[REDACTED]", i.e. an operator instruction whose branch name was destroyed
# and therefore not copy-pasteable, and a findings_json payload lost the branch value while
# STILL parsing — a machine consumer silently read a wrong name instead of failing loudly.


def test_branch_task_and_phase_names_survive_redaction():
    survivors = [
        # the real branch names of T-6 and T-8, both hit in production by the old pattern
        "git -C /repo checkout main && git -C /repo merge --no-ff "
        "task-8-recovery-commits-after-a-timeout-commit-half-fin",
        "task-6-verification-commands-cannot-run-in-a-worktree-n",
        "phase-3-risk-assessment-and-masked-payloads",
        "mask-1 risk-level-2 disk-skiplist-8",
        "subtask-7-and-task-7-are-the-same-thing",
        "src/orchestrator/services/task_service.py:137 ROUND_FIELDS",
    ]
    for text in survivors:
        assert redact(text) == text, text


def test_findings_json_payload_survives_redaction():
    line = structured_log.attach(
        "unauthorized contract file change(s) in this run",
        {
            "branch": "task-8-recovery-commits-after-a-timeout-commit-half-fin",
            "task_id": 8,
            "unauthorized": ["docs/frozen-contracts.md"],
            "recovery_commits": ["ba77a43"],
        },
    )
    out = redact(line)
    assert out == line, out  # not merely parseable — byte-identical, values intact
    assert structured_log.parse(out)["branch"].startswith("task-8-recovery-commits")


def test_credentials_are_still_redacted_wherever_they_appear():
    """The anchor must not weaken secret-hunting: every pattern still fires at a real prefix
    start — bare, after `=`, after a quote, after `_`, after a hyphen, and inside a JSON
    payload. The `_` case is why the anchor is a lookbehind and not `\b`: `_` is a word
    character, so `\bsk-` would miss `TOKEN_sk-proj-…`, a shape keys really do appear in."""
    secrets = [
        "sk-ant-api03-abcdefghijklmnopqrstuvwx",
        "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
        "sk-abcdefghijklmnopqrstuvwxyz0123456789",
        "ghp_abcdefghijklmnopqrstuvwxyz1234",
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP",
        "glpat-abcdefghijklmnopqrstuvw",
        "xoxb-123456789012-abcdefghijklmnopqrst",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p67",  # AIza + exactly 35 chars (39 total)
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuvwxyz123456",
    ]
    for secret in secrets:
        shapes = [
            secret,
            f"OPENAI_KEY={secret}",
            f'"token": "{secret}"',
            f"prefix-{secret}",  # hyphen-joined
            structured_log.attach("event", {"branch": "task-8-x", "value": secret}),
        ]
        if secret.startswith("sk-"):
            # underscore-joined: `\b` would MISS this ( `_` is a word character), which is exactly
            # why the sk- anchor is a lookbehind and not a literal word boundary
            shapes.append(f"OPENAI_TOKEN_{secret}")
        for shape in shapes:
            out = redact(shape)
            assert secret not in out, shape
            assert REDACTED in out, shape
    # a real key embedded in a branch-named merge command: key goes, branch stays
    line = "merge --no-ff task-8-recovery sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    out = redact(line)
    assert "task-8-recovery" in out and "sk-proj-" not in out
    # and the case that makes the anchor choice non-trivial, end to end
    glued = "OPENAI_TOKEN_sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    assert "sk-proj-" not in redact(glued), redact(glued)


def test_build_log_details_keep_operational_commands_copy_pasteable(session, initialized):
    """The damage was measured in the build log, so the regression is asserted there: every
    event's details go through `redact()` (log_service.l.28)."""
    from orchestrator.services import git_service, log_service


    branch = "task-8-recovery-commits-after-a-timeout-commit-half-fin"
    repo_hint = "/run/media/mrbigroom/Backup2TB/Projects/Coding-Orchestration-Agent-Harness"
    cmd = git_service.merge_command(Path(repo_hint), "main", branch)
    entry = log_service.log_event(session, "merge_candidate", f"merge gate disabled; run: {cmd}")
    assert branch in entry.details, entry.details
    assert REDACTED not in entry.details
    assert entry.details == f"merge gate disabled; run: {cmd}"


def test_underscore_joined_other_credentials_are_a_recorded_gap_not_a_regression():
    """Pins the CURRENT (pre-existing) behaviour of the non-sk- patterns so a later phase cannot
    lose it by accident, and states in the test what is missed: `_` is a word character, so the
    historical `\\b` anchors do not fire after `TOKEN_`. This is NOT caused by DoD 10 — the `sk-`
    lookbehind was chosen precisely so the sk- family DOES fire there."""
    missed = {
        "OPENAI_TOKEN_ghp_abcdefghijklmnopqrstuvwxyz1234": "gh[pousr]_",
        "GITLAB_TOKEN_glpat-abcdefghijklmnopqrstuvw": "glpat-",
        "SLACK_TOKEN_xoxb-123456789012-abcdefghijklmnopqrst": "xox[baprs]-",
        "AWS_TOKEN_AKIAIOSFODNN7EXAMPLE": "AKIA",
    }
    for text, pattern in missed.items():
        assert redact(text) == text, (pattern, text)  # recorded gap
        # the same credential in a position the historical anchor does cover still redacts
        assert redact(f"{text.split('_', 2)[-1]}") != text.split("_", 2)[-1], pattern
    # and it is unchanged from the pre-T-8 code: the lookbehind was applied to the sk- family only
    assert redact("OPENAI_TOKEN_sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345") != (
        "OPENAI_TOKEN_sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    )


def test_redacts_assignments_and_bearer():
    out = redact(
        'QWEN_API_KEY=abc123secret\n"password": "hunter22"\nAuthorization: Bearer abc.def.ghijkl'
    )
    assert "abc123secret" not in out and "hunter22" not in out and "abc.def.ghijkl" not in out


def test_redacts_private_key_block():
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----"
    assert redact(f"x\n{pem}\ny") == f"x\n{REDACTED}\ny"


def test_redacts_secret_values_from_environment(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "zzzz-very-secret-value")
    assert "zzzz-very-secret-value" not in redact("token is zzzz-very-secret-value")


def test_leaves_normal_text_alone():
    text = "ran 3 tests in 0.2s; api_key_env=QWEN_API_KEY is set"
    assert redact("ran 3 tests in 0.2s") == "ran 3 tests in 0.2s"
    assert "QWEN_API_KEY" in redact(text)


def test_token_counts_are_not_secrets():
    usage = '{"inputTokens":400,"totalTokens":1007,"cache_read_input_tokens": 600}'
    assert redact(usage) == usage
    assert redact("max_tokens=16000") == "max_tokens=16000"
    assert REDACTED in redact('{"access_token": "abcd1234efgh"}')


def test_long_unbroken_text_redacts_quickly():
    started = time.monotonic()
    for text in ("x" * 1_000_000, "token" * 200_000, "secret-" * 150_000):
        redact(text)
    assert time.monotonic() - started < 5
    assert REDACTED in redact("x" * 1000 + " MY_LONG_API_KEY_NAME=abcd1234")
    assert REDACTED in redact("API_TOKEN=12345678")  # singular key: still redacted


def test_sensitive_paths_and_diff_stripping():
    assert is_sensitive_path(".env") and is_sensitive_path("config/.env.local")
    assert is_sensitive_path("keys/id_ed25519") and is_sensitive_path("certs/server.pem")
    assert not is_sensitive_path(".env.example") and not is_sensitive_path("src/env.py")
    diff = "diff --git a/.env b/.env\n+SECRET=1\ndiff --git a/app.py b/app.py\n+print('hi')\n"
    out = strip_sensitive_files_from_diff(diff)
    assert "SECRET=1" not in out and "print('hi')" in out and "sensitive file omitted" in out


def test_dotenv_loaded_without_overriding(tmp_path, monkeypatch):
    import os

    from orchestrator.core.config import load_env_file

    (tmp_path / ".env").write_text("ZZ_TEST_PROVIDER_KEY=from-file\nZZ_ALREADY_SET=from-file\n")
    monkeypatch.setenv("ZZ_ALREADY_SET", "from-env")
    monkeypatch.delenv("ZZ_TEST_PROVIDER_KEY", raising=False)
    load_env_file(tmp_path / ".env")
    try:
        assert os.environ["ZZ_TEST_PROVIDER_KEY"] == "from-file"
        assert os.environ["ZZ_ALREADY_SET"] == "from-env"
    finally:
        os.environ.pop("ZZ_TEST_PROVIDER_KEY", None)


# --- T-16 DoD 2/5(b): prose token counts and self-declared placeholders survive ----------------
# The exact shape that reached a reviewer in runs/52/review_payload.md was
# `max_diff_tokens=[REDACTED]`: the generated budget preamble ends the number with `;`, the old
# `val.isdigit()` test saw `12000;`, and the redactor swallowed a configuration number that was
# itself describing the diff budget. The damaged string is pinned here so a regression is named
# by the same text the Architect saw.


def test_inline_token_counts_in_prose_survive_byte_identically():
    lines = [
        "[review diff budget: max_diff_tokens=12000; 12 file(s); per-file budget 1975 tokens",
        "max_diff_tokens=4000",
        "input_tokens=123",
        '"inputTokens": 400',
        "totalTokens: 1007, cache_read_input_tokens=600",
    ]
    for line in lines:
        assert redact(line) == line, line
    damaged = "max_diff_tokens=[REDACTED]"
    # The exact broken output must never be produced from the real number...
    assert damaged not in redact(lines[0])
    # ...while an archive that already contains the marker is not rewritten further.
    assert redact(damaged) == damaged


def test_synthetic_placeholder_assignments_in_test_literals_survive():
    """R-52 fixture: the redactor had masked the strings whose point is that they are fake.

    These are ASSERTED TEST LITERALS, not secrets; a reviewer saw `ANTHROPIC_API_KEY=[REDACTED]`
    and filed a finding that contradicted the synthesis helper. Both directions are asserted
    below, so the placeholder exemption cannot weaken a real credential shape.
    """
    from tests.integration.test_env_hermeticity import SEEDED_ENV_PAIRS

    flat = "".join(f"{name}={value}\n" for name, value in SEEDED_ENV_PAIRS)
    assert redact(flat) == flat
    literals = [
        'assert "ANTHROPIC_API_KEY=synthetic-not-a-secret" in observed.read_text()',
        'assert "OPERATOR_API_KEY=not-a-secret-0000" in text',
        'assert "OPERATOR_SUPABASE_URL=https://synthetic.invalid" in text',
        'assert "OPERATOR_EMAIL=synthetic@example.invalid" in text',
    ]
    for line in literals:
        assert redact(line) == line, line
    # A real key shape under the same name still redacts first; the exemption is for the value
    # shape, never for the credential patterns.
    real = "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwx"
    assert "sk-ant-api03" not in redact(real)
    # A random-looking value that merely starts with the word "synthetic" is not a placeholder.
    assert REDACTED in redact("ANTHROPIC_API_KEY=syntheticrealvalue123")


def test_a_real_secret_with_a_placeholder_prefix_is_still_redacted():
    """Review round 1 finding 4: the exemption is placeholder VOCABULARY, not arbitrary content.

    The earlier regex accepted `placeholder` + a delimiter + anything, so a generic assignment
    such as `PASSWORD=placeholder-but-actually-a-secret-123` was left intact. These all keep a
    placeholder word at the start and must still lose the value.
    """
    damaged = [
        "PASSWORD=placeholder-but-actually-a-secret-123",
        "ANTHROPIC_API_KEY=synthetic-real-key-12345",
        "SECRET=fake-credential-abcdef123456",
        "TOKEN=dummy-token-ABCDEFGHIJKLMNOP",
        "API_KEY=not-a-secret-but-really-XYZ9876",
    ]
    for line in damaged:
        out = redact(line)
        assert REDACTED in out, line
        assert line.split("=", 1)[1] not in out, out
    # The narrow vocabulary still accepts the exact synthetic fixtures the gate needs.
    for line in (
        "PASSWORD=placeholder-0001",
        "ANTHROPIC_API_KEY=synthetic-not-a-secret",
        "OPERATOR_API_KEY=not-a-secret-0002",
    ):
        assert redact(line) == line, line


def test_angle_bracket_and_url_placeholders_do_not_exempt_real_values():
    """Review round 2 finding 2: a bracketed value or a URL with userinfo is not a placeholder.

    The earlier `_PLACEHOLDER_VALUE` accepted ANY `<...>` and any URL ending in `.invalid`,
    including `PASSWORD=<actual-secret>` and `https://user:pass@example.invalid`, so generic
    secret assignments survived. The exemption is now placeholder vocabulary only, while real
    credential shapes and the exact R-52 fixtures keep their behaviour.
    """
    damaged = [
        "PASSWORD=<actual-secret>",
        "TOKEN=<hunter2>",
        "API_KEY=<abc123def456>",
        "PASSWORD=https://user:pass@example.invalid",
        "TOKEN=user:pass@example.invalid",
        "SECRET=https://synthetic:s3cret@example.invalid",
    ]
    for line in damaged:
        out = redact(line)
        assert REDACTED in out, line
        assert line.split("=", 1)[1] not in out, out
    safe = [
        "PASSWORD=<your-api-key>",
        "TOKEN=<insert-token-here>",
        "URL=https://synthetic.invalid",
        "EMAIL=synthetic@example.invalid",
        "ANTHROPIC_API_KEY=synthetic-not-a-secret",
    ]
    for line in safe:
        assert redact(line) == line, line
    # The exact broken marker from the review payload is never produced from the real number.
    assert "max_diff_tokens=[REDACTED]" not in redact("max_diff_tokens=4000")


def test_redaction_report_names_the_pattern_class_and_count_without_the_secret():
    from orchestrator.utils.redaction import RedactionHit, redact_with_report

    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwx"
    report = redact_with_report(f"one {secret} two {secret}")
    assert report.text.count(REDACTED) == 2
    assert report.hits == (RedactionHit("sk-ant", 2),)
    assert secret not in str(report.hits)
    # Benign token counts produce no redaction row at all.
    assert redact_with_report("max_diff_tokens=4000").hits == ()
