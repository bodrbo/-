"""Order events: the log of a tuning / dealer order — a new request (arrives
by itself from the site's feedback form), a dialog with the client and a
visit. A visit with a host also becomes a task of the host in the tuning
schedule (the task is created through the injected `make_visit_task`)."""

import datetime as dt

from flask import session

from . import repository
from .constants import (
    DEFAULT_VISIT_MINUTES, EVENT_LABELS, MANUAL_TYPES, MAX_VISIT_MINUTES, MIN_VISIT_MINUTES,
    TEXT_MAX_LENGTH, VISIT_DURATION_CHOICES,
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


def delete_event(db, event_id, delete_task):
    """Removes an event; the host's task of a visit goes with it unless it is
    already paid. Returns (ok, message)."""
    event = repository.get_event(db, event_id)
    if event is None:
        return False, "Событие уже удалено."
    message = "Событие удалено."
    if event["schedule_task_id"]:
        ok, task_message = delete_task(event["schedule_task_id"])
        if not ok:
            message += " Задача в расписании осталась: " + task_message
    repository.delete_event(db, event_id)
    db.commit()
    return True, message


def forget_order_events(db, order_kind, order_id, delete_task):
    """The order is deleted: so are its events and their unpaid schedule tasks.
    The caller commits."""
    for event in repository.list_order_events(db, order_kind, order_id):
        if event["schedule_task_id"]:
            delete_task(event["schedule_task_id"])
        repository.delete_event(db, event["id"])


def panel_context(db, order_kind, order_id, hosts):
    """Everything the «События» panel of an order page needs. `hosts`: names
    of the people a visit can be handed to."""
    now = dt.datetime.now()
    today = now.date().isoformat()
    stamp = (today, now.strftime("%H:%M"))
    events = []
    for row in repository.list_events(db, order_kind, order_id):
        event = dict(row)
        event["kind_label"] = EVENT_LABELS.get(event["kind"], event["kind"])
        event["end_time"] = ""
        if event["event_time"] and event["duration_minutes"]:
            end = int(event["event_time"][:2]) * 60 + int(event["event_time"][3:]) + event["duration_minutes"]
            event["end_time"] = "%02d:%02d" % (end // 60 % 24, end % 60)
        event["upcoming"] = event["kind"] == "visit" and (event["event_date"], event["event_time"]) >= stamp
        events.append(event)
    return {
        "order_kind": order_kind, "order_id": order_id, "events": events, "hosts": hosts,
        "types": MANUAL_TYPES, "durations": VISIT_DURATION_CHOICES,
        "default_duration": DEFAULT_VISIT_MINUTES, "today": today,
        "notice": session.pop("order_events_notice", None),
        "error": session.pop("order_events_error", None),
    }
