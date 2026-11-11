"""Notification trigger vocabulary (T-9), free of service imports for settings_service."""

from __future__ import annotations

from dataclasses import dataclass

MASTER_SETTING = "notifications_enabled"


@dataclass(frozen=True)
class NotificationTrigger:
    key: str
    label: str
    action: str
    terminal_run: bool = False
    noisy: bool = False

    @property
    def setting(self) -> str:
        return f"notification_{self.key}"


def _t(key, label, action, terminal=False, noisy=False):
    return NotificationTrigger(key, label, action, terminal, noisy)


# Master is OFF on a new install. Human-must-act triggers default subscribed; noisy machinery
# events stay silent until explicitly enabled.
TRIGGERS: dict[str, NotificationTrigger] = {
    "run_completed": _t(
        "run_completed", "Run completed", "Review the handoff; accept or request changes.",
        terminal=True,
    ),
    "run_failed": _t(
        "run_failed", "Run failed", "Read the failure, then retry or block.", terminal=True,
    ),
    "run_timeout": _t(
        "run_timeout", "Run timed out", "Decide whether to retry or block.", terminal=True,
    ),
    "run_cancelled": _t(
        "run_cancelled", "Run cancelled", "Decide whether to retry or block.", terminal=True,
    ),
    "standalone_run": _t(
        "standalone_run", "Standalone report ready",
        "Open the report named in the notification.", terminal=True,
    ),
    "task_review": _t("task_review", "Task awaiting review", "Accept it or request changes."),
    "task_blocked": _t("task_blocked", "Task blocked", "Unblock, retry, or accept."),
    "escalated": _t("escalated", "Task escalated", "Open the task for an Architect review."),
    "merge_candidate": _t("merge_candidate", "Merge candidate ready", "Merge the accepted branch."),
    "merged": _t("merged", "Branch merged", "No action unless a follow-up is needed."),
    "merge_refused": _t("merge_refused", "Merge refused", "Resolve it, then merge manually."),
    # T-41: a stopped or repeatedly failing background sync is human-must-act, so it defaults
    # subscribed; the notification is emitted once per failure streak, never once per retry.
    "sync_failed": _t(
        "sync_failed", "Cloud sync needs attention",
        "Run `orchestrator sync status`; if versions drifted, run `orchestrator sync reinit` "
        "(dry run by default; `--apply` writes). It never touches payload conflicts: they are "
        "reported as conflicts_skipped and left untouched.",
    ),
    "worktree_created": _t("worktree_created", "Worktree created", "No action needed.", noisy=True),
    "recovery_commit": _t(
        "recovery_commit", "Recovery commit", "Inspect unself-checked content.", noisy=True,
    ),
    "supervisor_verification": _t(
        "supervisor_verification", "Supervisor verification", "No action needed.", noisy=True,
    ),
    "architect_review": _t("architect_review", "Architect review", "No action needed.", noisy=True),
    "nits_deferred": _t(
        "nits_deferred", "Nits deferred", "Read the deferred findings later.", noisy=True,
    ),
    "schedule_fired": _t("schedule_fired", "Schedule fired", "No action needed.", noisy=True),
}

CORE_TRIGGERS = tuple(t.key for t in TRIGGERS.values() if not t.noisy)
NOISY_TRIGGERS = tuple(t.key for t in TRIGGERS.values() if t.noisy)
TERMINAL_RUN_TRIGGERS = tuple(t.key for t in TRIGGERS.values() if t.terminal_run)
DIRECT_EVENTS = frozenset(TRIGGERS)


def _task_status_target(details: str) -> str | None:
    """Parse the real transition shape: `<src> -> <dst>[: reason]`."""
    head = details.partition(":")[0]
    return head.rsplit(" -> ", 1)[1].strip() if " -> " in head else None


def trigger_for_event(event: str, details: str = "") -> NotificationTrigger | None:
    """Map an existing build-log event to a notification trigger, or None."""
    if event in DIRECT_EVENTS:
        return TRIGGERS[event]
    if event == "task_status":
        # Tie the match to the status vocabulary, not a substring: `review_notes` must not fire.
        target = _task_status_target(details)
        if target in ("review", "blocked"):
            return TRIGGERS[f"task_{target}"]
    return None


def setting_default(trigger: NotificationTrigger) -> bool:
    return not trigger.noisy
