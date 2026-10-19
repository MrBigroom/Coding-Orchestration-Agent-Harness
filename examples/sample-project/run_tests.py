"""Dependency-free test runner for the sample project (prints pytest-style counts)."""

import sys
import traceback

from calc import add, mul


def test_add():
    assert add(2, 3) == 5


def test_mul():
    assert mul(4, 5) == 20


def test_mock_change_file_is_markdown():
    # Passes before and after the mock worker appends to MOCK_CHANGE.md.
    import pathlib

    p = pathlib.Path("MOCK_CHANGE.md")
    assert not p.exists() or p.read_text().startswith("# Mock changes")


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAILED {name}")
                traceback.print_exc()
    print(f"{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
