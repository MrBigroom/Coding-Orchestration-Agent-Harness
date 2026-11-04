"""Notification trigger vocabulary (T-9), kept free of service imports for settings_service."""

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


# The master is OFF on a new install. Human-must-act triggers default subscribed; noisy
# machinery events are silent until explicitly enabled.
TRIGGERS: dict[str, NotificationTrigger] = {
    "run_completed": NotificationTrigger(
        "run_completed", "Run completed", "Review the handoff, then accept or request changes.",
        terminal_run=True,
    ),
    "run_failed": NotificationTrigger(
        "run_failed", "Run failed", "Open the task, read the failure, then retry or block it.",
        terminal_run=True,
    ),
    "run_timeout": NotificationTrigger(
        "run_timeout", "Run timed out", "Open the task and decide whether to retry or block it.",
        terminal_run=True,
    ),
    "run_cancelled": NotificationTrigger(
        "run_cancelled", "Run cancelled", "Open the task and decide whether to retry or block it.",
        terminal_run=True,
    ),
    "task_review": NotificationTrigger(
        "task_review", "Task awaiting review", "Open the task and accept it or request changes."
    ),
    "task_blocked": NotificationTrigger(
        "task_blocked", "Task blocked", "Resolve the blocker: unblock, retry, or accept."
    ),
    "escalated": NotificationTrigger(
        "escalated", "Task escalated", "Open the task; escalation needs an Architect review."
    ),
    "merge_candidate": NotificationTrigger(
        "merge_candidate", "Merge candidate ready", "Merge the accepted phase branch."
    ),
    "merged": NotificationTrigger(
        "merged", "Branch merged", "No action needed unless a follow-up task is required."
    ),
    "merge_refused": NotificationTrigger(
        "merge_refused", "Merge refused", "Resolve the merge problem, then merge manually."
    ),
    "worktree_created": NotificationTrigger(
        "worktree_created", "Worktree created", "No action needed.", noisy=True
    ),
    "recovery_commit": NotificationTrigger(
        "recovery_commit", "Recovery commit", "Inspect content the Worker never self-checked.",
        noisy=True,
    ),
    "supervisor_verification": NotificationTrigger(
        "supervisor_verification", "Supervisor verification", "No action needed.", noisy=True
    ),
    "architect_review": NotificationTrigger(
        "architect_review", "Architect review", "No action needed.", noisy=True
    ),
    "nits_deferred": NotificationTrigger(
        "nits_deferred", "Nits deferred", "Read the deferred findings when convenient.", noisy=True
    ),
    "schedule_fired": NotificationTrigger(
        "schedule_fired", "Schedule fired", "No action needed.", noisy=True
    ),
}

CORE_TRIGGERS = tuple(t.key for t in TRIGGERS.values() if not t.noisy)
NOISY_TRIGGERS = tuple(t.key for t in TRIGGERS.values() if t.noisy)
TERMINAL_RUN_TRIGGERS = tuple(t.key for t in TRIGGERS.values() if t.terminal_run)
DIRECT_EVENTS = frozenset(t.key for t in TRIGGERS.values())


def trigger_for_event(event: str, details: str = "") -> NotificationTrigger | None:
    """Map an existing build-log event to a notification trigger, or None."""
    if event in DIRECT_EVENTS:
        return TRIGGERS[event]
    if event == "task_status":
        if "-> review" in details:
            return TRIGGERS["task_review"]
        if "-> blocked" in details:
            return TRIGGERS["task_blocked"]
    return None


def setting_default(trigger: NotificationTrigger) -> bool:
    return not trigger.noisy
