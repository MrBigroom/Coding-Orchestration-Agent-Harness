"""Machine-readable payload carried inside the frozen build-log `details` string.

The build-log line shape `{timestamp, phase, task_id, run_id, role, event, details,
commit_sha}` is a frozen contract and the current event names stay frozen, so a structured
field is appended to `details` instead of added as a new key:

    <human summary> | findings_json={<json object}>

`findings_json=` is always the LAST segment, so readers take the remainder of the string and
`json.loads` it; old consumers keep showing the human summary. Legacy lines (no marker) parse
to None.
"""

from __future__ import annotations

import json
from typing import Any

MARKER = " | findings_json="


def attach(summary: str, payload: dict[str, Any]) -> str:
    """Human summary first, structured payload last."""
    return f"{summary}{MARKER}{json.dumps(payload, sort_keys=True)}"


def parse(details: str) -> dict[str, Any] | None:
    """The reader for `attach`; None when the line carries no structured payload."""
    idx = (details or "").rfind(MARKER)
    if idx == -1:
        return None
    try:
        data = json.loads(details[idx + len(MARKER) :])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def summary_of(details: str) -> str:
    """The human part of a line, with the structured segment removed."""
    idx = (details or "").rfind(MARKER)
    return (details or "")[:idx] if idx != -1 else (details or "")
