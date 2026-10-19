"""Slug and identifier helpers."""

from __future__ import annotations

import re
import unicodedata


def slugify(value: str, max_len: int = 48) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:max_len].rstrip("-") or "item"


def phase_branch(phase_number: int | None, slug: str, task_id: int | None = None) -> str:
    """Branch name format: phase-N-<slug>. Tasks without a phase use task-<id>-<slug>."""
    if phase_number is not None:
        return f"phase-{phase_number}-{slugify(slug)}"
    return f"task-{task_id}-{slugify(slug)}"
