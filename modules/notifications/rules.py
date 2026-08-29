"""One editable map of business events to Telegram recipient positions.

Add a rule here when a new event needs a role-based notification. Delivery
code deliberately lives elsewhere, so changing recipients does not require
touching Flask routes or Telegram API calls.
"""

from dataclasses import dataclass
from typing import Optional, Tuple


EVENT_TUNING_WORK_APPROVED = "tuning.work_approved"
EVENT_FLEET_DEFECT_CREATED = "fleet.defect_created"
EVENT_FLEET_CHECKLIST_PROBLEM = "fleet.checklist_problem"
EVENT_FLEET_EXTRA_DEFECT = "fleet.extra_defect"
EVENT_TASK_ASSIGNED = "task.assigned"
EVENT_TASK_UNACCEPTED_3H = "task.unaccepted_3h"
EVENT_TASK_UNACCEPTED_6H = "task.unaccepted_6h"


@dataclass(frozen=True)
class NotificationRule:
    event: str
    positions: Tuple[str, ...]
    delivery: str
    description: str
    recipient: str = "positions"
    delay_hours: Optional[int] = None


NOTIFICATION_RULES = {
    EVENT_TUNING_WORK_APPROVED: NotificationRule(
        event=EVENT_TUNING_WORK_APPROVED,
        positions=("Тюнингмэн", "Менеджер по работе с клиентами"),
        delivery="immediate",
        description="Клиент согласовал дополнительную работу в тюнинг-заказе",
    ),
    EVENT_FLEET_DEFECT_CREATED: NotificationRule(
        event=EVENT_FLEET_DEFECT_CREATED,
        positions=("Тюнингмэн",),
        delivery="immediate",
        description="Капитан сообщил о новой неисправности судна",
    ),
    EVENT_FLEET_CHECKLIST_PROBLEM: NotificationRule(
        event=EVENT_FLEET_CHECKLIST_PROBLEM,
        positions=("Тюнингмэн",),
        delivery="immediate",
        description="В обязательном пункте чек-листа обнаружена проблема",
    ),
    EVENT_FLEET_EXTRA_DEFECT: NotificationRule(
        event=EVENT_FLEET_EXTRA_DEFECT,
        positions=("Тюнингмэн",),
        delivery="immediate",
        description="Во время осмотра найдена неисправность вне чек-листа",
    ),
    EVENT_TASK_ASSIGNED: NotificationRule(
        event=EVENT_TASK_ASSIGNED,
        positions=(),
        delivery="immediate",
        description="Сотруднику поручена новая задача",
        recipient="assigned_employee",
        delay_hours=0,
    ),
    EVENT_TASK_UNACCEPTED_3H: NotificationRule(
        event=EVENT_TASK_UNACCEPTED_3H,
        positions=(),
        delivery="delayed",
        description="Задача не принята через 3 часа после поручения",
        recipient="assigned_employee",
        delay_hours=3,
    ),
    EVENT_TASK_UNACCEPTED_6H: NotificationRule(
        event=EVENT_TASK_UNACCEPTED_6H,
        positions=(),
        delivery="delayed",
        description="Задача не принята через 6 часов после поручения",
        recipient="assigned_employee",
        delay_hours=6,
    ),
}


def notification_rule(event: str) -> NotificationRule:
    """Return the configured rule and fail loudly for an unknown event."""
    try:
        return NOTIFICATION_RULES[event]
    except KeyError as exc:
        raise ValueError(f"Unknown notification event: {event}") from exc
