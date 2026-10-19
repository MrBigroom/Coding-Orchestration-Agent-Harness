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


def test_sensitive_paths_and_diff_stripping():
    assert is_sensitive_path(".env") and is_sensitive_path("config/.env.local")
    assert is_sensitive_path("keys/id_ed25519") and is_sensitive_path("certs/server.pem")
    assert not is_sensitive_path(".env.example") and not is_sensitive_path("src/env.py")
    diff = "diff --git a/.env b/.env\n+SECRET=1\ndiff --git a/app.py b/app.py\n+print('hi')\n"
    out = strip_sensitive_files_from_diff(diff)
    assert "SECRET=1" not in out and "print('hi')" in out and "sensitive file omitted" in out
