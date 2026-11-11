"""What `orchestrator cleanup` may remove, and why (T-39).

Task worktrees are protected by their task's status: `accepted`/`archived`/`rejected` are
removable and anything else is skipped unless `--include-active` is passed.

Chat worktrees cannot be protected that way. Their marker is `chat-<id>`, not a task id, so the
old guard (`info["task_id"].isdigit()`) resolved no owner and the tree fell straight through to
`remove_worktree` -- a `busy` session whose agent process holds the tree as its cwd was
indistinguishable from a long-closed one, and survived only by the accident of being dirty.

A chat worktree is therefore judged by OWNERSHIP, in this order:

1. the live-process check: any process whose cwd is the tree (or inside it) refuses it, so the
   protection does not depend on `chat_sessions` being current. This reuses T-20's liveness rule
   (`git_service.process_is_alive`) and is what refuses the tree even when the row says `closed`;
2. the session row, mapped by `chat_sessions.cwd` -- never by parsing the marker name, because
   marker-name parsing is the defect this exists to remove. Any status other than `closed` is
   treated as live, so the set of protecting states is read from `ChatStatus`, not hard-coded;
3. chat worktrees are opt-in (`--include-chats`, default off). With it, closed-session trees are
   reclaimable; a session that is not closed still needs `--include-live-chats`, whose help text
   states that it can delete the directory of a running session.

Removal stays last: `remove_worktree` still refuses a dirty tree, which remains the final line of
defence (not the only one). Every outcome is classified from what was verified afterwards, so the
counts and summary can only report what actually happened (T-39 DoD 4).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from orchestrator.core.models import ChatSession, ChatStatus, Project, Task
from orchestrator.services import git_service

DONE_TASK_STATUSES = frozenset({"accepted", "archived", "rejected"})

REMOVED = git_service.REMOVAL_REMOVED
WOULD_REMOVE = git_service.REMOVAL_WOULD_REMOVE
SKIPPED = git_service.REMOVAL_SKIPPED
FAILED = git_service.REMOVAL_FAILED
REFUSED = "refused"
KINDS = (REMOVED, WOULD_REMOVE, SKIPPED, REFUSED, FAILED)

PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class CleanupAction:
    """One worktree's planned or performed outcome, with the reason behind it."""

    kind: str
    path: Path
    message: str
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": str(self.path), "message": self.message}


def _key(path: str | Path) -> str:
    return os.path.realpath(str(path))


def cwd_owner_pids(path: Path) -> list[int]:
    """Pids that hold `path` (or a directory inside it) as their cwd, proved alive.

    The check is the OS's own view, so a `chat_sessions` row that still says `closed` after a
    restart cannot cost the operator the directory a live harness is running in. A pid is
    reported only after `git_service.process_is_alive` (T-20's `os.kill(pid, 0)` rule) confirms
    it, so a recycled `/proc` entry is not mistaken for a holder.
    """
    target = _key(path)
    pids: list[int] = []
    if not PROC_ROOT.is_dir():
        return pids
    try:
        entries = list(PROC_ROOT.iterdir())
    except OSError:
        return pids
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        current = Path(cwd)
        if current == Path(target) or Path(target) in current.parents:
            if git_service.process_is_alive(pid):
                pids.append(pid)
    return sorted(pids)


def _chat_sessions_by_cwd(session: Session) -> dict[str, list[ChatSession]]:
    out: dict[str, list[ChatSession]] = {}
    for chat in session.exec(select(ChatSession)):
        if chat.cwd:
            out.setdefault(_key(chat.cwd), []).append(chat)
    for chats in out.values():
        chats.sort(key=lambda c: c.id or 0)
    return out


def _skip(path: Path, reason: str) -> CleanupAction:
    return CleanupAction(SKIPPED, path, f"skip {path}: {reason}", reason)


def _refuse(path: Path, reason: str) -> CleanupAction:
    return CleanupAction(REFUSED, path, f"refuse {path}: {reason}", reason)


def _chat_action(
    info: dict,
    chats: list[ChatSession],
    *,
    dry_run: bool,
    include_chats: bool,
    include_live_chats: bool,
    projects: dict[str, Project],
) -> CleanupAction:
    path: Path = info["path"]
    owners = cwd_owner_pids(path)
    if owners and not include_live_chats:
        who = ", ".join(str(pid) for pid in owners)
        state = (
            "chat session "
            + ", ".join(f"{c.id} is {c.status}" for c in chats)
            if chats
            else "no chat session row"
        )
        return _refuse(path, f"live process {who} has it as its cwd ({state})")
    if chats and not include_live_chats:
        live = [c for c in chats if c.status != ChatStatus.closed]
        if live:
            chat = live[0]
            return _skip(
                path,
                f"chat session {chat.id} is {chat.status} "
                "(pass --include-live-chats to remove the directory of a running session)",
            )
    if not include_chats and not include_live_chats:
        if chats:
            return _skip(
                path,
                f"chat session {chats[0].id} is closed (pass --include-chats to remove)",
            )
        return _skip(path, "no chat session row (pass --include-chats to remove)")
    return _remove(info, projects, dry_run)


def _remove(info: dict, projects: dict[str, Project], dry_run: bool) -> CleanupAction:
    path: Path = info["path"]
    project = projects.get(info["project_slug"])
    outcome = git_service.remove_worktree_outcome(
        Path(project.path) if project else None, info, dry_run=dry_run
    )
    reason = outcome.message.split(": ", 1)[1] if ": " in outcome.message else outcome.message
    return CleanupAction(outcome.kind, path, outcome.message, reason)


def plan(
    session: Session,
    *,
    dry_run: bool = True,
    include_active: bool = False,
    include_chats: bool = False,
    include_live_chats: bool = False,
) -> list[CleanupAction]:
    """Decide (and, when not a dry run, perform) every managed worktree's cleanup.

    The parity snapshot is skipped here exactly as before: it has its own lock-aware sweep,
    which remains unchanged and runs before this plan.
    """
    projects = {p.slug: p for p in session.exec(select(Project))}
    chats_by_cwd = _chat_sessions_by_cwd(session)
    actions: list[CleanupAction] = []
    for info in git_service.managed_worktrees():
        if info["task_id"] == git_service.PARITY_TASK_ID:
            continue
        path: Path = info["path"]
        # A numeric marker names a task; any other marker is a chat worktree. This is only a
        # CLASSIFICATION. The owning session is resolved through `chat_sessions.cwd` below,
        # never by parsing `chat-<id>` (marker-name parsing is the T-39 defect).
        if not str(info["task_id"]).isdigit():
            actions.append(
                _chat_action(
                    info,
                    chats_by_cwd.get(_key(path), []),
                    dry_run=dry_run,
                    include_chats=include_chats,
                    include_live_chats=include_live_chats,
                    projects=projects,
                )
            )
            continue
        task = session.get(Task, int(info["task_id"]))
        if task and task.status not in DONE_TASK_STATUSES and not include_active:
            actions.append(_skip(path, f"task {task.short_id} is {task.status}"))
            continue
        actions.append(_remove(info, projects, dry_run))
    return actions


def counts(actions: list[CleanupAction]) -> dict[str, int]:
    out = {kind: 0 for kind in KINDS}
    for action in actions:
        out[action.kind] = out.get(action.kind, 0) + 1
    return out


def summary(actions: list[CleanupAction], *, dry_run: bool) -> str:
    """The last line, derived only from the actions above it (T-39 DoD 4)."""
    c = counts(actions)
    removed = "would remove" if dry_run else "removed"
    head = "cleanup (dry run)" if dry_run else "cleanup"
    return (
        f"{head}: {c[WOULD_REMOVE] if dry_run else c[REMOVED]} {removed}, "
        f"{c[SKIPPED]} skipped, {c[REFUSED]} refused, {c[FAILED]} failed"
    )


def as_dict(actions: list[CleanupAction], *, dry_run: bool) -> dict:
    c = counts(actions)
    return {
        "dry_run": dry_run,
        "actions": [a.as_dict() for a in actions],
        "counts": c,
        "failed": c[FAILED],
        "ok": c[FAILED] == 0,
        "summary": summary(actions, dry_run=dry_run),
    }
