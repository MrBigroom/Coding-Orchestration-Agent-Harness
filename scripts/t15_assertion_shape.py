#!/usr/bin/env python3
"""T-15 DoD 4: the assertion SHAPE rule for the two memory test files.

Run from the repo root (the evidence bundle runs it at the HEAD it certifies):

    python3 scripts/t15_assertion_shape.py

It pins no counts, so a later task (T-17 touches these files next) may add or split assertions
without breaking it. Two rules, both about shape:

1. No reason is asserted by substring: `assert "..." in row["why"]` still passes when the reason
   grows a suffix that means something else. Use an exact value (or a structured field).
2. A remaining conjoined `assert a and b` must be documented AT THE SITE with a `# one outcome:`
   comment claiming the two fields are one fact, because a conjoined assertion cannot say which
   half failed. Every surviving site is printed with its line and that justification, so the
   inventory is the committed evidence rather than a bare count.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FILES = (
    "tests/unit/test_memory_scopes.py",
    "tests/integration/test_memory_scope_boundary.py",
)
MARKER = "one outcome"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _one_line(node: ast.AST, text: str) -> str:
    return " ".join((ast.get_source_segment(text, node) or "").split())


def _justification(lines: list[str], lineno: int) -> str:
    """The nearest comment line above the assert that carries the `one outcome` marker."""
    for line in reversed(lines[max(0, lineno - 4) : lineno - 1]):
        if MARKER in line:
            return line.strip().lstrip("# ").strip()
    return ""


def shape_failures(root: Path | None = None) -> list[str]:
    """Every violation of the two rules, as `file:line: reason`. Empty means the shape holds."""
    base = Path(root) if root else repo_root()
    failures: list[str] = []
    for name in FILES:
        path = base / name
        if not path.is_file():  # a caller pointing at a partial tree
            failures.append(f"{name}: file is missing from {base}")
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        tree = ast.parse(text, filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            source = _one_line(node.test, text)
            # Rule 1: the forbidden reason-substring shape.
            if (
                isinstance(node.test, ast.Compare)
                and any(isinstance(op, ast.In) for op in node.test.ops)
                and any(
                    isinstance(part, ast.Subscript)
                    for part in (node.test.left, *node.test.comparators)
                )
                and ('"why"' in source or "'why'" in source)
            ):
                failures.append(f"{name}:{node.lineno}: reason asserted by substring: {source}")
            # Rule 2: a conjoined assert must carry its one-outcome comment.
            if isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.And):
                if not _justification(lines, node.lineno):
                    failures.append(
                        f"{name}:{node.lineno}: conjoined assert has no `# one outcome:` comment"
                    )
    return failures


def report(root: Path | None = None) -> int:
    base = Path(root) if root else repo_root()
    for name in FILES:
        path = base / name
        if not path.is_file():
            print(f"{name}: missing")
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        tree = ast.parse(text, filename=name)
        conjoined = [
            (node.lineno, _justification(lines, node.lineno))
            for node in ast.walk(tree)
            if isinstance(node, ast.Assert)
            and isinstance(node.test, ast.BoolOp)
            and isinstance(node.test.op, ast.And)
        ]
        equality_reasons = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Assert)
            and isinstance(node.test, ast.Compare)
            and any(isinstance(op, ast.Eq) for op in node.test.ops)
            and any(
                isinstance(part, ast.Subscript)
                for part in (node.test.left, *node.test.comparators)
            )
            and ('"why"' in _one_line(node.test, text) or "'why'" in _one_line(node.test, text))
        ]
        print(f"{name}: conjoined_asserts={len(conjoined)} (all must be justified, none counted)")
        for lineno, why in conjoined:
            print(f"  l.{lineno}: one outcome -> {why or 'UNDOCUMENTED'}")
        print(f"{name}: reason_equality_asserts={len(equality_reasons)} at {equality_reasons}")
    failures = shape_failures(base)
    print("assertion_shape=PASS" if not failures else "assertion_shape=FAIL")
    for failure in failures:
        print(failure)
    return 0 if not failures else 1


def main() -> int:
    return report()


if __name__ == "__main__":
    sys.exit(main())