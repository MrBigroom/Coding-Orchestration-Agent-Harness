import time

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
