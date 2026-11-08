#!/usr/bin/env python3
"""T-15 DoD 4: report the assertion shape of the two memory test files.

Run from the repo root (the evidence bundle runs it at the HEAD it certifies):

    python3 scripts/t15_assertion_shape.py

It does not pin counts, so a later task may add or split assertions without breaking T-17. It
checks the rule the review asked for: every remaining conjoined ``assert a and b`` is documented
at the site as ONE outcome, and no reason is asserted by substring.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FILES = (
    "tests/unit/test_memory_scopes.py",
    "tests/integration/test_memory_scope_boundary.py",
)


def _one_line(node: ast.AST, text: str) -> str:
    return " ".join((ast.get_source_segment(text, node) or "").split())


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for name in FILES:
        path = root / name
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        tree = ast.parse(text, filename=name)
        conjoined = []
        substring_reasons = []
        equality_reasons = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            if isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.And):
                above = "\n".join(lines[max(0, node.lineno - 4) : node.lineno - 1])
                conjoined.append((node.lineno, "one outcome" in above))
            # `assert "..." in repair.skipped[0]["why"]` is the forbidden reason-substring shape.
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and any(isinstance(op, ast.In) for op in test.ops)
                and any(
                    isinstance(part, ast.Subscript) for part in (test.left, *test.comparators)
                )
            ):
                source = _one_line(test, text)
                if '"why"' in source or "'why'" in source:
                    substring_reasons.append((node.lineno, source))
            if (
                isinstance(test, ast.Compare)
                and any(isinstance(op, ast.Eq) for op in test.ops)
                and any(
                    isinstance(part, ast.Subscript) for part in (test.left, *test.comparators)
                )
            ):
                source = _one_line(test, text)
                if '"why"' in source or "'why'" in source:
                    equality_reasons.append((node.lineno, source))
        undocumented = [line for line, documented in conjoined if not documented]
        failures.extend(
            f"{name}:{line}: conjoined assert has no `# one outcome:` comment"
            for line in undocumented
        )
        failures.extend(
            f"{name}:{line}: reason asserted by substring: {source}"
            for line, source in substring_reasons
        )
        print(f"{name}: conjoined_asserts={len(conjoined)} undocumented={len(undocumented)}")
        print(f"{name}: reason_substring_asserts={len(substring_reasons)}")
        print(f"{name}: reason_equality_asserts={len(equality_reasons)}")

    print("assertion_shape=PASS" if not failures else "assertion_shape=FAIL")
    for failure in failures:
        print(failure)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())