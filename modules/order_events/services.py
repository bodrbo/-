"""Order events: the log of a tuning / dealer order — a new request (arrives
by itself from the site's feedback form), a dialog with the client and a
visit. A visit with a host also becomes a task of the host in the tuning
schedule (the task is created through the injected `make_visit_task`)."""

import datetime as dt

from flask import session

from . import repository
from .constants import (
    DEFAULT_REMINDER_LEAD_MINUTES, DEFAULT_TASK_MINUTES, DEFAULT_VISIT_MINUTES, EVENT_LABELS,
    MANUAL_TYPES, MAX_VISIT_MINUTES, MIN_VISIT_MINUTES, TASK_DURATION_CHOICES,
    TASK_TITLE_SUGGESTIONS, TEXT_MAX_LENGTH, VISIT_DURATION_CHOICES,
)


def _parse_date(raw):
    try:
        return dt.date.fromisoformat(str(raw or "").strip()).isoformat()
    except ValueError:
        return None


def _parse_time(raw):
    try:
        return dt.datetime.strptime(str(raw or "").strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def parse_manual_event(form):
    """-> (error, data). data: kind, text, event_date, event_time,
    duration_minutes, host_employee."""
    kind = str(form.get("kind", "")).strip()
    if kind not in {item["value"] for item in MANUAL_TYPES}:
        return "Выберите тип события: диалог или визит.", None
    text = str(form.get("text", "")).strip()
    if len(text) > TEXT_MAX_LENGTH:
        return f"Текст слишком длинный — не больше {TEXT_MAX_LENGTH} символов.", None
    if kind == "dialog" and not text:
        return "Опишите диалог — что обсудили с клиентом.", None
    event_date = _parse_date(form.get("event_date"))
    if event_date is None:
        return "Укажите дату события.", None
    data = {
        "kind": kind, "text": text, "event_date": event_date, "event_time": "",
        "duration_minutes": None, "host_employee": "",
    }
    if kind == "visit":
        event_time = _parse_time(form.get("event_time"))
        if event_time is None:
            return "Укажите время визита.", None
        try:
            minutes = int(str(form.get("duration_minutes") or DEFAULT_VISIT_MINUTES).strip())
        except ValueError:
            return "Продолжительность визита указана неверно.", None
        if not MIN_VISIT_MINUTES <= minutes <= MAX_VISIT_MINUTES:
            return "Продолжительность визита — от 5 минут до 12 часов.", None
        start = int(event_time[:2]) * 60 + int(event_time[3:])
        if start + minutes > 24 * 60:
            return "Визит должен закончиться до конца суток — сократите продолжительность или начните раньше.", None
        data.update(
            event_time=event_time, duration_minutes=minutes,
            host_employee=str(form.get("host_employee", "")).strip(),
        )
    return None, data


def add_manual_event(db, order_kind, order_id, form, author_admin_id, context, make_visit_task, now=None):
    """Adds a dialog or a visit. `context`: {"client": client name, "label":
    "Заказ №5" (how the order is named), "tuning_order_id": the tuning order
    the host's task belongs to, or None}. Returns (ok, message)."""
    error, data = parse_manual_event(form)
    if error:
        return False, error
    now = now or dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    task_id = None
    message = "Событие добавлено."
    if data["kind"] == "visit" and data["host_employee"]:
        title = "Визит: " + (context.get("client") or context.get("label") or "клиент")
        comment = context.get("label", "")
        if data["text"]:
            comment = (comment + ". " if comment else "") + data["text"]
        ok, task_message, task_id = make_visit_task(
            db, data["host_employee"], title, comment, data["event_date"], data["event_time"],
            data["duration_minutes"] / 60.0, context.get("tuning_order_id"),
        )
        if not ok:
            return False, task_message
        message = "Визит добавлен, в расписании тюнинга создана задача для " + data["host_employee"] + "."
        extra = task_message.replace("Задача добавлена в расписание.", "").strip()
        if extra:
            message += " " + extra
    repository.add_event(
        db, order_kind, order_id, data["kind"], data["text"], data["event_date"], data["event_time"],
        data["duration_minutes"], data["host_employee"], task_id, author_admin_id, now,
    )
    db.commit()
    return True, message


def add_new_request(db, order_kind, order_id, text, created_at):
    """The client's message from the feedback form. The caller commits."""
    return repository.add_event(
        db, order_kind, order_id, "new_request", text, created_at[:10], created_at[11:16], None,
        "", None, None, created_at,
    )


def _event_task_ids(db, event):
    task_ids = [event["schedule_task_id"]] if event["schedule_task_id"] else []
    return task_ids + repository.list_event_task_ids(db, event["id"])


def delete_event(db, event_id, delete_task):
    """Removes an event; the host's task of a visit and the tasks made from
    the event go with it unless already paid. Returns (ok, message)."""
    event = repository.get_event(db, event_id)
    if event is None:
        return False, "Событие уже удалено."
    message = "Событие удалено."
    for task_id in _event_task_ids(db, event):
        ok, task_message = delete_task(task_id)
        if not ok and "уже удалена" not in task_message:
            message += " Задача в расписании осталась: " + task_message
    repository.delete_event_tasks(db, event_id)
    repository.delete_event(db, event_id)
    db.commit()
    return True, message


def forget_order_events(db, order_kind, order_id, delete_task):
    """The order is deleted: so are its events and their unpaid schedule tasks.
    The caller commits."""
    for event in repository.list_order_events(db, order_kind, order_id):
        for task_id in _event_task_ids(db, event):
            delete_task(task_id)
        repository.delete_event_tasks(db, event["id"])
        repository.delete_event(db, event["id"])


def parse_event_task(form):
    """The "Создать задачу" form under an event -> (error, data). data:
    executor, title, comment, work_date, start_time, hours. The reminder
    fields are checked by the injected task maker."""
    executor = str(form.get("task_executor", "")).strip()
    if not executor:
        return "Выберите, кому поручить задачу.", None
    title = str(form.get("task_title", "")).strip()
    if not title:
        return "Укажите, что нужно сделать.", None
    work_date = _parse_date(form.get("task_date"))
    if work_date is None:
        return "Укажите дату задачи.", None
    start_time = _parse_time(form.get("task_time"))
    if start_time is None:
        return "Укажите время задачи.", None
    try:
        minutes = int(str(form.get("task_minutes") or DEFAULT_TASK_MINUTES).strip())
    except ValueError:
        return "Продолжительность задачи указана неверно.", None
    if not MIN_VISIT_MINUTES <= minutes <= MAX_VISIT_MINUTES:
        return "Продолжительность задачи — от 5 минут до 12 часов.", None
    return None, {
        "executor": executor, "title": title, "comment": str(form.get("task_comment", "")).strip(),
        "work_date": work_date, "start_time": start_time, "hours": minutes / 60.0,
    }


def add_event_task(db, event_id, form, context, make_event_task, now=None):
    """Creates a tuning-schedule task (and its Telegram reminder) from an
    event. `make_event_task(db, executor, title, comment, day, start, hours,
    tuning_order_id, form) -> (ok, message, task_id)` is app.py's: it makes
    the unpaid schedule task and the reminder from the form's remind_*
    fields. Returns (ok, message)."""
    event = repository.get_event(db, event_id)
    if event is None:
        return False, "Событие не найдено."
    error, data = parse_event_task(form)
    if error:
        return False, error
    label = EVENT_LABELS.get(event["kind"], event["kind"])
    event_day = dt.date.fromisoformat(event["event_date"]).strftime("%d.%m.%Y")
    source = f"Событие «{label}» от {event_day}"
    if event["text"]:
        source += ": " + event["text"]
    parts = [part for part in (data["comment"], context.get("label", ""), source) if part]
    comment = ". ".join(parts)
    ok, message, task_id = make_event_task(
        db, data["executor"], data["title"], comment, data["work_date"], data["start_time"],
        data["hours"], context.get("tuning_order_id"), form,
    )
    if not ok:
        db.rollback()
        return False, message
    repository.add_event_task(db, event_id, task_id, now or dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    db.commit()
    return True, message


def _task_views(db, order_kind, order_id):
    """event_id -> its tasks (made with "Создать задачу"), each with its
    reminders."""
    rows = [dict(row) for row in repository.list_order_event_tasks(db, order_kind, order_id)]
    reminders = {}
    for reminder in repository.list_task_reminders(db, [row["task_id"] for row in rows]):
        reminders.setdefault(reminder["schedule_task_id"], []).append(dict(reminder))
    by_event = {}
    for row in rows:
        row["reminders"] = reminders.get(row["task_id"], [])
        by_event.setdefault(row["event_id"], []).append(row)
    return by_event


def panel_context(db, order_kind, order_id, hosts, recipients=None, tasks_enabled=False):
    """Everything the «События» panel of an order page needs. `hosts`: names
    of the people a visit (or a task made from an event) can be handed to;
    `recipients`: the Telegram reminder picker; `tasks_enabled`: whether the
    "Создать задачу" form is offered under the events."""
    now = dt.datetime.now()
    today = now.date().isoformat()
    stamp = (today, now.strftime("%H:%M"))
    tasks_by_event = _task_views(db, order_kind, order_id) if tasks_enabled else {}
    events = []
    for row in repository.list_events(db, order_kind, order_id):
        event = dict(row)
        event["kind_label"] = EVENT_LABELS.get(event["kind"], event["kind"])
        event["end_time"] = ""
        if event["event_time"] and event["duration_minutes"]:
            end = int(event["event_time"][:2]) * 60 + int(event["event_time"][3:]) + event["duration_minutes"]
            event["end_time"] = "%02d:%02d" % (end // 60 % 24, end % 60)
        event["upcoming"] = event["kind"] == "visit" and (event["event_date"], event["event_time"]) >= stamp
        event["tasks"] = tasks_by_event.get(event["id"], [])
        event["task_title"] = TASK_TITLE_SUGGESTIONS.get(event["kind"], "")
        events.append(event)
    return {
        "order_kind": order_kind, "order_id": order_id, "events": events, "hosts": hosts,
        "types": MANUAL_TYPES, "durations": VISIT_DURATION_CHOICES,
        "default_duration": DEFAULT_VISIT_MINUTES, "today": today,
        "tasks_enabled": tasks_enabled, "recipients": recipients or [],
        "task_durations": TASK_DURATION_CHOICES, "default_task_minutes": DEFAULT_TASK_MINUTES,
        "reminder_lead_minutes": DEFAULT_REMINDER_LEAD_MINUTES,
        "default_task_time": "%02d:00" % min(now.hour + 1, 21),
        "notice": session.pop("order_events_notice", None),
        "error": session.pop("order_events_error", None),
    }
