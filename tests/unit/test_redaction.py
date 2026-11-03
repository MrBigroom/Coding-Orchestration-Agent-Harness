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
    start — bare, after `=`, after a quote, after a hyphen, and inside a JSON payload."""
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
        for shape in (
            secret,
            f"OPENAI_KEY={secret}",
            f'"token": "{secret}"',
            f"prefix-{secret}",  # glued to a word: `\b` still matches, so it still redacts
            structured_log.attach("event", {"branch": "task-8-x", "value": secret}),
        ):
            out = redact(shape)
            assert secret not in out, shape
            assert REDACTED in out, shape
    # a real key embedded in a branch-named merge command: key goes, branch stays
    line = "merge --no-ff task-8-recovery sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    out = redact(line)
    assert "task-8-recovery" in out and "sk-proj-" not in out


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
