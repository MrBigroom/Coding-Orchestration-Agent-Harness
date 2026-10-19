"""Safe path helpers. Anything served or deleted must resolve inside a managed root."""

from __future__ import annotations

from pathlib import Path

MANAGED_MARKER = ".orchestrator-managed"


class UnsafePathError(ValueError):
    pass


def safe_join(root: Path, *parts: str) -> Path:
    """Join `parts` under `root`, rejecting absolute paths, `..` escapes, and symlink escapes."""
    root_resolved = Path(root).resolve()
    for p in parts:
        if not p:
            continue
        if Path(p).is_absolute() or "\x00" in p:
            raise UnsafePathError(f"absolute or invalid path segment: {p!r}")
    candidate = root_resolved.joinpath(*[p for p in parts if p]).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise UnsafePathError("path escapes managed root")
    return candidate


def is_within(root: Path, path: Path) -> bool:
    try:
        safe_join(root, str(Path(path).resolve().relative_to(Path(root).resolve())))
        return True
    except (ValueError, UnsafePathError):
        return False


def write_marker(directory: Path, note: str = "") -> None:
    (directory / MANAGED_MARKER).write_text(note or "managed by coding-orchestrator\n")


def has_marker(directory: Path) -> bool:
    return (directory / MANAGED_MARKER).is_file()
