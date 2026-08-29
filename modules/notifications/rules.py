"""One editable map of business events to Telegram recipient positions.

Add a rule here when a new event needs a role-based notification. Delivery
code deliberately lives elsewhere, so changing recipients does not require
touching Flask routes or Telegram API calls.
"""

from dataclasses import dataclass


EVENT_TUNING_WORK_APPROVED = "tuning.work_approved"
EVENT_FLEET_DEFECT_CREATED = "fleet.defect_created"
EVENT_FLEET_CHECKLIST_PROBLEM = "fleet.checklist_problem"
EVENT_FLEET_EXTRA_DEFECT = "fleet.extra_defect"


@dataclass(frozen=True)
class NotificationRule:
    event: str
    positions: tuple[str, ...]
    delivery: str
    description: str


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
}


def notification_rule(event: str) -> NotificationRule:
    """Return the configured rule and fail loudly for an unknown event."""
    try:
        return NOTIFICATION_RULES[event]
    except KeyError as exc:
        raise ValueError(f"Unknown notification event: {event}") from exc
