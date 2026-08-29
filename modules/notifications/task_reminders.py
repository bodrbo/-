"""Telegram notifications for fleet and tuning task assignments."""

import datetime as dt
import html

from .rules import (
    EVENT_TASK_ASSIGNED,
    EVENT_TASK_UNACCEPTED_3H,
    EVENT_TASK_UNACCEPTED_6H,
    notification_rule,
)


ASSIGNMENT_DEFECT = "defect"
ASSIGNMENT_TUNING = "tuning"
ASSIGNMENT_TYPES = (ASSIGNMENT_DEFECT, ASSIGNMENT_TUNING)


def init_schema(db):
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS task_notification_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_type TEXT NOT NULL,
            assignment_id INTEGER NOT NULL,
            notification_event TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            delivery_status TEXT,
            UNIQUE(assignment_type, assignment_id, notification_event)
        )
        """
    )


def _format_number(value):
    number = float(value or 0)
    if number.is_integer():
        return f"{int(number):,}".replace(",", " ")
    return f"{number:,.2f}".replace(",", " ").replace(".", ",")


def _assignment(db, assignment_type, assignment_id):
    if assignment_type == ASSIGNMENT_DEFECT:
        return db.execute(
            "SELECT a.id, a.employee_name, a.rate, a.norm_hours, a.comment, "
            "a.assignment_status, a.assigned_at, d.description AS task_name, "
            "d.boat AS context_name, NULL AS order_id, 'Судно' AS context_label "
            "FROM defect_assignments a "
            "JOIN boat_defects d ON d.id = a.defect_id "
            "WHERE a.id = ?",
            (assignment_id,),
        ).fetchone()
    if assignment_type == ASSIGNMENT_TUNING:
        return db.execute(
            "SELECT a.id, a.employee_name, a.rate, a.norm_hours, a.comment, "
            "a.assignment_status, a.assigned_at, i.work_name AS task_name, "
            "CASE WHEN o.equipment_type = 'motor' "
            "THEN COALESCE(NULLIF(o.motor_model, ''), 'Мотор') "
            "ELSE COALESCE(NULLIF(o.boat_model, ''), 'Лодка') END AS context_name, "
            "o.id AS order_id, 'Тюнинг-заказ' AS context_label "
            "FROM tuning_item_assignments a "
            "JOIN tuning_order_items i ON i.id = a.item_id "
            "JOIN tuning_orders o ON o.id = i.order_id "
            "WHERE a.id = ?",
            (assignment_id,),
        ).fetchone()
    raise ValueError(f"Unknown assignment type: {assignment_type}")


def _notification_text(assignment, event):
    rule = notification_rule(event)
    context = html.escape(assignment["context_name"] or "—")
    task_name = html.escape(assignment["task_name"] or "—")
    if assignment["order_id"] is not None:
        context = f"№{assignment['order_id']} · {context}"
    amount = float(assignment["rate"]) * float(assignment["norm_hours"])
    payment = (
        f"{_format_number(amount)} ₽ "
        f"({_format_number(assignment['rate'])} ₽ × "
        f"{_format_number(assignment['norm_hours'])} ч.)"
    )
    comment = (assignment["comment"] or "").strip()
    comment_line = f"\nКомментарий: {html.escape(comment)}" if comment else ""
    if event == EVENT_TASK_ASSIGNED:
        heading = "📋 <b>Вам поручена задача</b>"
        timing = "Откройте раздел «Мои задачи», чтобы принять или отклонить её."
    else:
        heading = "⏰ <b>Задача всё ещё ожидает вашего ответа</b>"
        timing = (
            f"Прошло {rule.delay_hours} ч. после поручения. "
            "Пожалуйста, примите или отклоните задачу."
        )
    return (
        f"{heading}\n"
        f"{assignment['context_label']}: {context}\n"
        f"Задача: {task_name}{comment_line}\n"
        f"Вознаграждение: {payment}\n\n"
        f"{timing}"
    )


def _record_delivery(db, assignment_type, assignment_id, event, attempted_at, status):
    db.execute(
        "INSERT OR IGNORE INTO task_notification_deliveries "
        "(assignment_type, assignment_id, notification_event, attempted_at, delivery_status) "
        "VALUES (?, ?, ?, ?, ?)",
        (assignment_type, assignment_id, event, attempted_at, str(status)),
    )


def notify_task_assigned(
    db, assignment_type, assignment_id, employee_sender, now=None
):
    """Notify the assignee immediately after a valid assignment is created."""
    assignment = _assignment(db, assignment_type, assignment_id)
    if assignment is None:
        return None
    now = now or dt.datetime.now()
    status = employee_sender(
        db,
        assignment["employee_name"],
        _notification_text(assignment, EVENT_TASK_ASSIGNED),
    )
    _record_delivery(
        db,
        assignment_type,
        assignment_id,
        EVENT_TASK_ASSIGNED,
        now.strftime("%Y-%m-%d %H:%M"),
        status,
    )
    db.commit()
    return status


def _due_assignments(db, event, cutoff):
    rows = []
    for assignment_type, table_name in (
        (ASSIGNMENT_DEFECT, "defect_assignments"),
        (ASSIGNMENT_TUNING, "tuning_item_assignments"),
    ):
        assignments = db.execute(
            f"SELECT a.id FROM {table_name} a "
            "WHERE a.assignment_status = 'pending' AND a.assigned_at <= ? "
            "AND NOT EXISTS ("
            "SELECT 1 FROM task_notification_deliveries d "
            "WHERE d.assignment_type = ? AND d.assignment_id = a.id "
            "AND d.notification_event = ?"
            ") ORDER BY a.assigned_at, a.id",
            (cutoff, assignment_type, event),
        ).fetchall()
        rows.extend((assignment_type, row["id"]) for row in assignments)
    return rows


def send_due_task_reminders(db, employee_sender, now=None):
    """Send idempotent 3h/6h reminders for assignments still pending.

    The 6-hour checkpoint is processed first. If the cron was unavailable at
    three hours, the employee receives one current 6-hour reminder instead of
    two stale messages at once; the missed 3-hour checkpoint is recorded as
    superseded.
    """
    now = now or dt.datetime.now()
    attempted_at = now.strftime("%Y-%m-%d %H:%M")
    stats = {"sent_3h": 0, "sent_6h": 0}
    checkpoints = (
        (EVENT_TASK_UNACCEPTED_6H, "sent_6h"),
        (EVENT_TASK_UNACCEPTED_3H, "sent_3h"),
    )
    for event, stat_key in checkpoints:
        rule = notification_rule(event)
        cutoff = (now - dt.timedelta(hours=rule.delay_hours)).strftime(
            "%Y-%m-%d %H:%M"
        )
        for assignment_type, assignment_id in _due_assignments(db, event, cutoff):
            assignment = _assignment(db, assignment_type, assignment_id)
            if assignment is None or assignment["assignment_status"] != "pending":
                continue
            status = employee_sender(
                db,
                assignment["employee_name"],
                _notification_text(assignment, event),
            )
            _record_delivery(
                db, assignment_type, assignment_id, event, attempted_at, status
            )
            if event == EVENT_TASK_UNACCEPTED_6H:
                _record_delivery(
                    db,
                    assignment_type,
                    assignment_id,
                    EVENT_TASK_UNACCEPTED_3H,
                    attempted_at,
                    "superseded_by_6h",
                )
            stats[stat_key] += 1
    db.commit()
    return stats
