"""Personal Telegram notifications for changes in the trip schedule."""

import datetime as dt
import html
import logging

from modules.notifications import (
    EVENT_SCHEDULE_ASSIGNED,
    EVENT_SCHEDULE_BOAT_CHANGED,
    EVENT_SCHEDULE_CANCELLED,
    EVENT_SCHEDULE_RESCHEDULED,
    notification_rule,
)

from . import repository
from .constants import CREW_ROLES


LOGGER = logging.getLogger(__name__)
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def item_snapshot(db, item_id):
    """Capture notification-relevant state before or after a committed change."""
    item = repository.get_item(db, item_id)
    if item is None:
        return None
    return {
        "id": item["id"],
        "service_name": item["service_name"],
        "boat": item["boat"],
        "starts_at": item["starts_at"],
        "ends_at": item["ends_at"],
        "assignments": tuple(
            {
                "employee_id": row["employee_id"],
                "employee_name": row["employee_name"],
                "role": row["role"],
            }
            for row in repository.list_assignments(db, item_id)
        ),
    }


def _safe(value):
    return html.escape(str(value or ""), quote=False)


def _interval(snapshot):
    try:
        starts_at = dt.datetime.strptime(snapshot["starts_at"], "%Y-%m-%d %H:%M")
        ends_at = dt.datetime.strptime(snapshot["ends_at"], "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return _safe(snapshot["starts_at"])
    date_label = (
        f"{starts_at.day} {MONTHS[starts_at.month - 1]} {starts_at.year}"
    )
    if starts_at.date() == ends_at.date():
        return f"{date_label}, {starts_at:%H:%M}–{ends_at:%H:%M}"
    end_label = f"{ends_at.day} {MONTHS[ends_at.month - 1]} {ends_at.year}"
    return f"{date_label}, {starts_at:%H:%M} — {end_label}, {ends_at:%H:%M}"


def _assignment_map(snapshot):
    if snapshot is None:
        return {}
    return {
        assignment["employee_id"]: assignment
        for assignment in snapshot["assignments"]
    }


def _base_lines(snapshot, assignment=None):
    lines = [
        f"Рейс: <b>{_safe(snapshot['service_name'])}</b>",
        f"Когда: {_interval(snapshot)}",
        f"Судно: {_safe(snapshot['boat'])}",
    ]
    if assignment is not None:
        role = CREW_ROLES.get(assignment["role"], assignment["role"])
        lines.append(f"Ваша роль: {_safe(role)}")
    return lines


def _deliver(db, event, employee_name, text, employee_notifier, deliveries):
    rule = notification_rule(event)
    if rule.delivery != "immediate" or rule.recipient != "assigned_employee":
        raise ValueError("Schedule notifications require an assigned employee rule")
    try:
        status = employee_notifier(db, employee_name, text)
    except Exception as error:  # Notification failure must not undo a trip edit.
        LOGGER.exception("Schedule Telegram notification failed")
        status = "error: {}".format(error)
    deliveries.append({
        "event": event,
        "employee_name": employee_name,
        "status": status,
    })


def notify_item_changes(db, before, after, employee_notifier):
    """Compare two snapshots and send at most one message per employee."""
    if employee_notifier is None or (before is None and after is None):
        return []

    deliveries = []
    before_assignments = _assignment_map(before)
    after_assignments = _assignment_map(after)

    if after is None:
        for assignment in before_assignments.values():
            text = "\n".join(
                ["❌ <b>Рейс отменён</b>"] + _base_lines(before, assignment)
            )
            _deliver(
                db, EVENT_SCHEDULE_CANCELLED,
                assignment["employee_name"], text,
                employee_notifier, deliveries,
            )
        return deliveries

    new_employee_ids = set(after_assignments) - set(before_assignments)
    if before is None:
        new_employee_ids = set(after_assignments)
    for employee_id in sorted(new_employee_ids):
        assignment = after_assignments[employee_id]
        text = "\n".join(
            ["🗓 <b>Вам назначен новый рейс</b>"]
            + _base_lines(after, assignment)
        )
        _deliver(
            db, EVENT_SCHEDULE_ASSIGNED,
            assignment["employee_name"], text,
            employee_notifier, deliveries,
        )

    if before is None:
        return deliveries

    time_changed = (
        before["starts_at"] != after["starts_at"]
        or before["ends_at"] != after["ends_at"]
    )
    boat_changed = before["boat"] != after["boat"]
    if not time_changed and not boat_changed:
        return deliveries

    common_employee_ids = set(before_assignments) & set(after_assignments)
    event = (
        EVENT_SCHEDULE_RESCHEDULED
        if time_changed
        else EVENT_SCHEDULE_BOAT_CHANGED
    )
    for employee_id in sorted(common_employee_ids):
        assignment = after_assignments[employee_id]
        lines = [
            "⏰ <b>Рейс перенесён</b>"
            if time_changed
            else "🚤 <b>У рейса изменено судно</b>",
            f"Рейс: <b>{_safe(after['service_name'])}</b>",
        ]
        if time_changed:
            lines.extend([
                f"Было: {_interval(before)}",
                f"Стало: {_interval(after)}",
            ])
        else:
            lines.append(f"Когда: {_interval(after)}")
        if boat_changed:
            lines.extend([
                f"Судно было: {_safe(before['boat'])}",
                f"Судно стало: {_safe(after['boat'])}",
            ])
        role = CREW_ROLES.get(assignment["role"], assignment["role"])
        lines.append(f"Ваша роль: {_safe(role)}")
        _deliver(
            db, event, assignment["employee_name"], "\n".join(lines),
            employee_notifier, deliveries,
        )
    return deliveries
