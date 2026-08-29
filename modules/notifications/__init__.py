"""Role-based Telegram notification routing."""

from .rules import (
    EVENT_FLEET_CHECKLIST_PROBLEM,
    EVENT_FLEET_DEFECT_CREATED,
    EVENT_FLEET_EXTRA_DEFECT,
    EVENT_TASK_ASSIGNED,
    EVENT_TASK_UNACCEPTED_3H,
    EVENT_TASK_UNACCEPTED_6H,
    EVENT_TUNING_WORK_APPROVED,
    NOTIFICATION_RULES,
    NotificationRule,
    notification_rule,
)
from .services import DispatchResult, dispatch_notification, dispatch_photos
from .task_reminders import (
    ASSIGNMENT_DEFECT,
    ASSIGNMENT_TUNING,
    init_schema,
    notify_task_assigned,
    send_due_task_reminders,
)

__all__ = (
    "DispatchResult",
    "ASSIGNMENT_DEFECT",
    "ASSIGNMENT_TUNING",
    "EVENT_FLEET_CHECKLIST_PROBLEM",
    "EVENT_FLEET_DEFECT_CREATED",
    "EVENT_FLEET_EXTRA_DEFECT",
    "EVENT_TUNING_WORK_APPROVED",
    "EVENT_TASK_ASSIGNED",
    "EVENT_TASK_UNACCEPTED_3H",
    "EVENT_TASK_UNACCEPTED_6H",
    "NOTIFICATION_RULES",
    "NotificationRule",
    "dispatch_notification",
    "dispatch_photos",
    "init_schema",
    "notify_task_assigned",
    "notification_rule",
    "send_due_task_reminders",
)
