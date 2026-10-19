"""Token estimation, truncation, and test-output summarisation for LLM prompts."""

from __future__ import annotations

import re


def estimate_tokens(text: str) -> int:
    """Rough estimate (~4 chars/token). Good enough for budgeting and previews."""
    return max(0, (len(text) + 3) // 4)


def truncate_middle(text: str, max_tokens: int, marker: str = "\n... [truncated] ...\n") -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    budget = max_tokens * 4 - len(marker)
    head = budget * 2 // 3
    tail = budget - head
    return text[:head] + marker + (text[-tail:] if tail > 0 else "")


def truncate_tail(text: str, max_tokens: int) -> str:
    """Keep the end (most relevant for logs)."""
    if estimate_tokens(text) <= max_tokens:
        return text
    keep = max_tokens * 4
    return "... [earlier output truncated] ...\n" + text[-keep:]


_PYTEST_SUMMARY = re.compile(r"=+\s*(?P<body>[^=]*?(passed|failed|error|skipped)[^=]*?)\s*=+\s*$",
                             re.M)
_COUNT = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|deselected)")
_GENERIC_COUNT = re.compile(r"(?i)\b(\d+)\s+(tests?\s+)?(passed|failed|skipped|ok|failures?)\b")


def parse_test_counts(output: str) -> dict[str, int]:
    """Best-effort pass/fail/skip counts from pytest or generic test runners."""
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    m = None
    for m in _PYTEST_SUMMARY.finditer(output):
        pass
    source = m.group("body") if m else output
    found = False
    for n, kind in _COUNT.findall(source):
        found = True
        k = kind.rstrip("s") if kind.startswith("error") else kind
        if k in ("error",):
            counts["failed"] += int(n)
        elif k in counts:
            counts[k] += int(n)
    if not found:
        for n, _t, kind in _GENERIC_COUNT.findall(output):
            kind = kind.lower()
            if kind in ("passed", "ok"):
                counts["passed"] += int(n)
            elif kind.startswith("fail"):
                counts["failed"] += int(n)
            elif kind == "skipped":
                counts["skipped"] += int(n)
    return counts


def summarize_test_output(output: str, max_tokens: int, exit_code: int | None = None) -> str:
    """Counts + concise failure traces instead of raw logs."""
    counts = parse_test_counts(output)
    lines = [
        f"exit_code: {exit_code}",
        f"passed: {counts['passed']}  failed: {counts['failed']}  skipped: {counts['skipped']}",
    ]
    failures = _failure_blocks(output)
    if failures:
        lines.append("failures:")
        lines.append(truncate_middle("\n".join(failures), max(200, max_tokens - 50)))
    elif exit_code not in (0, None):
        lines.append("tail:")
        lines.append(truncate_tail(output, max(200, max_tokens - 50)))
    return "\n".join(lines)


def _failure_blocks(output: str) -> list[str]:
    blocks: list[str] = []
    for line in output.splitlines():
        if line.startswith(("FAILED ", "ERROR ")) or re.match(r"^E\s{2,}", line):
            blocks.append(line)
    return blocks[:60]
