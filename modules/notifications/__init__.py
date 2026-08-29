"""Role-based Telegram notification routing."""

from .rules import (
    EVENT_FLEET_CHECKLIST_PROBLEM,
    EVENT_FLEET_DEFECT_CREATED,
    EVENT_FLEET_EXTRA_DEFECT,
    EVENT_TUNING_WORK_APPROVED,
    NOTIFICATION_RULES,
    NotificationRule,
    notification_rule,
)
from .services import DispatchResult, dispatch_notification, dispatch_photos

__all__ = (
    "DispatchResult",
    "EVENT_FLEET_CHECKLIST_PROBLEM",
    "EVENT_FLEET_DEFECT_CREATED",
    "EVENT_FLEET_EXTRA_DEFECT",
    "EVENT_TUNING_WORK_APPROVED",
    "NOTIFICATION_RULES",
    "NotificationRule",
    "dispatch_notification",
    "dispatch_photos",
    "notification_rule",
)
