import pytest

from orchestrator.utils.paths import UnsafePathError, safe_join


def test_safe_join_rejects_escapes(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    assert safe_join(root, "1", "handoff.json") == (root / "1" / "handoff.json").resolve()
    for bad in ("../x", "1/../../x", "/etc/passwd", "a\x00b"):
        with pytest.raises(UnsafePathError):
            safe_join(root, bad)


def test_safe_join_rejects_symlink_escape(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(UnsafePathError):
        safe_join(root, "link", "secret")
