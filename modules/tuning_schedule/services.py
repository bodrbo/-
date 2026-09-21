"""Validation and view models for the tuning-center work schedule."""

import datetime as dt
import math

from modules.schedule.services import current_timestamp, day_label, parse_day  # noqa: F401 (re-exported)

from . import repository
from .constants import DEFAULT_DAY_END_HOUR, DEFAULT_DAY_START_HOUR, MIN_CARD_MINUTES

TASK_TITLE_MAX_LENGTH = 200
EMPLOYEE_NAME_MAX_LENGTH = 120
COMMENT_MAX_LENGTH = 2000
MAX_DAY_HOURS = 16
PX_PER_MINUTE = 1.25


def _normalise_text(value, limit):
    return " ".join(str(value or "").strip().split())[:limit]


def _parse_hhmm(raw_time):
    try:
        parsed = dt.datetime.strptime(str(raw_time or "").strip(), "%H:%M")
    except ValueError:
        return None
    return parsed.hour, parsed.minute


def _clean_task_days(day_rows, errors):
    """day_rows: iterable of (raw_date, raw_start_time, raw_hours) triples
    — one row for a single-day task ("Дата выполнения"), or one row per
    day of a "Период выполнения", each day carrying its own start time.
    Returns a de-duplicated, validated list of (date_iso, "HH:MM", hours)
    tuples."""
    clean = []
    seen_dates = set()
    for raw_date, raw_start_time, raw_hours in day_rows:
        try:
            work_date = dt.date.fromisoformat(str(raw_date or "").strip())
        except ValueError:
            errors.append("Некорректная дата в периоде выполнения.")
            continue
        parsed_time = _parse_hhmm(raw_start_time)
        if parsed_time is None:
            errors.append(f"Укажите время старта на {work_date.isoformat()}.")
            continue
        start_time = f"{parsed_time[0]:02d}:{parsed_time[1]:02d}"
        try:
            hours = float(str(raw_hours or "").strip().replace(",", "."))
        except ValueError:
            hours = 0
        if hours <= 0:
            errors.append(f"Укажите часы на {work_date.isoformat()}.")
            continue
        if hours > MAX_DAY_HOURS:
            errors.append(
                f"Слишком много часов на {work_date.isoformat()} "
                f"— не больше {MAX_DAY_HOURS} за один день."
            )
            continue
        if work_date.isoformat() in seen_dates:
            continue
        seen_dates.add(work_date.isoformat())
        clean.append((work_date.isoformat(), start_time, hours))
    if not clean and not errors:
        errors.append("Укажите хотя бы один день выполнения.")
    return clean


def add_day_crew_member(db, day, employee_id):
    eligible = {
        employee["id"]: employee
        for employee in repository.list_tuning_crew_employees(db)
    }
    if employee_id not in eligible:
        return False, "Сотрудник не найден или не является тюнингмэном."
    added = repository.add_day_crew_member(
        db, day.isoformat(), employee_id, current_timestamp()
    )
    if not added:
        return True, f"{eligible[employee_id]['name']} уже добавлен в расписание."
    return True, f"{eligible[employee_id]['name']} добавлен в расписание."


def remove_day_crew_member(db, day, employee_id):
    eligible = {
        employee["id"]: employee
        for employee in repository.list_tuning_crew_employees(db)
    }
    assigned_ids = repository.list_day_task_employee_ids(db, day.isoformat())
    employee_name = eligible.get(employee_id, {}).get("name", "Сотрудник")
    if employee_id in assigned_ids:
        return False, (
            f"Нельзя убрать {employee_name}: на эту дату уже назначена задача. "
            "Сначала перенесите или удалите задачу."
        )
    removed = repository.remove_day_crew_member(db, day.isoformat(), employee_id)
    if not removed:
        return False, "Сотрудник уже отсутствует в расписании на эту дату."
    return True, f"{employee_name} убран из расписания."


def day_view(db, day):
    crew = repository.list_tuning_crew_employees(db)
    day_crew_ids = set(repository.list_day_crew_ids(db, day.isoformat()))
    assigned_today_ids = repository.list_day_task_employee_ids(db, day.isoformat())
    for employee in crew:
        employee["position_label"] = " · ".join(employee["positions"])
        employee["has_day_task"] = employee["id"] in assigned_today_ids
    day_crew = [employee for employee in crew if employee["id"] in day_crew_ids]
    available_crew = [employee for employee in crew if employee["id"] not in day_crew_ids]

    tasks_by_employee_name = {}
    for row in repository.list_day_tasks(db, day.isoformat()):
        task = dict(row)
        task["is_linked"] = task["assignment_id"] is not None
        if task["is_linked"]:
            task["equipment_label"] = (
                (task["motor_model"] or "Мотор") if task["equipment_type"] == "motor"
                else (task["boat_model"] or "Лодка")
            )
        tasks_by_employee_name.setdefault(task["employee_name"], []).append(task)

    for employee in day_crew:
        employee["tasks"] = tasks_by_employee_name.pop(employee["name"], [])

    # Tasks whose assignee isn't (or is no longer) on today's roster — e.g.
    # the roster entry for that day was removed after the task was placed.
    # Surfaced separately rather than silently dropped.
    leftover_tasks = [
        task for tasks in tasks_by_employee_name.values() for task in tasks
    ]

    return {
        "crew": crew,
        "day_crew": day_crew,
        "available_crew": available_crew,
        "leftover_tasks": leftover_tasks,
    }


def calendar_view(db, day, day_crew):
    """Pixel-positioned calendar cards for `day`, one column per employee
    in `day_crew` (the same list day_view already built — reused so the
    roster and the calendar always agree on who's shown). Mirrors
    modules.schedule.services.day_view's minute-precision layout math
    (day-bounds expansion, px_per_minute scale, hour_marks) but without
    that module's drag/weather/participant machinery — this calendar is
    view-only for now (see PR discussion: no drag-and-drop yet)."""
    day_iso = day.isoformat()
    raw_tasks = [dict(row) for row in repository.list_day_tasks(db, day_iso)]

    earliest = DEFAULT_DAY_START_HOUR * 60
    latest = DEFAULT_DAY_END_HOUR * 60
    for task in raw_tasks:
        parsed_time = _parse_hhmm(task["start_time"]) or (DEFAULT_DAY_START_HOUR, 0)
        start_minutes = parsed_time[0] * 60 + parsed_time[1]
        end_minutes = start_minutes + round(task["planned_hours"] * 60)
        task["_start_minutes"] = start_minutes
        task["_end_minutes"] = end_minutes
        earliest = min(earliest, (start_minutes // 60) * 60)
        latest = max(latest, int(math.ceil(end_minutes / 60.0)) * 60)
    earliest = max(0, earliest)
    latest = min(24 * 60, latest)
    total_minutes = max(60, latest - earliest)

    cards_by_employee = {employee["name"]: [] for employee in day_crew}
    for task in raw_tasks:
        if task["employee_name"] not in cards_by_employee:
            continue
        task["is_linked"] = task["assignment_id"] is not None
        if task["is_linked"]:
            task["equipment_label"] = (
                (task["motor_model"] or "Мотор") if task["equipment_type"] == "motor"
                else (task["boat_model"] or "Лодка")
            )
        start_minutes = task["_start_minutes"]
        end_minutes = task["_end_minutes"]
        task["start_label"] = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
        task["end_label"] = f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
        task["top_px"] = round((start_minutes - earliest) * PX_PER_MINUTE, 2)
        task["height_px"] = round(
            max(MIN_CARD_MINUTES, end_minutes - start_minutes) * PX_PER_MINUTE, 2
        )
        cards_by_employee[task["employee_name"]].append(task)

    hour_marks = []
    for minute in range(earliest, latest + 1, 60):
        hour_marks.append({
            "label": f"{minute // 60:02d}:00",
            "top_px": round((minute - earliest) * PX_PER_MINUTE, 2),
        })

    return {
        "cards_by_employee": cards_by_employee,
        "hour_marks": hour_marks,
        "grid_height": round(total_minutes * PX_PER_MINUTE, 2),
    }


def create_free_task(db, employee_name, title, rate, comment, day_rows):
    errors = []
    employee_name = _normalise_text(employee_name, EMPLOYEE_NAME_MAX_LENGTH)
    title = _normalise_text(title, TASK_TITLE_MAX_LENGTH)
    comment = _normalise_text(comment, COMMENT_MAX_LENGTH)
    eligible = {employee["name"] for employee in repository.list_tuning_crew_employees(db)}
    if not employee_name or employee_name not in eligible:
        errors.append("Выберите сотрудника из списка тюнингмэнов.")
    if not title:
        errors.append("Укажите название задачи.")
    try:
        rate = float(str(rate or "").strip().replace(",", "."))
    except ValueError:
        rate = 0
    if rate <= 0:
        errors.append("Ставка должна быть больше нуля.")
    clean_days = _clean_task_days(day_rows, errors)
    if errors:
        return False, " ".join(errors), None
    task_id = repository.create_task(
        db, None, employee_name, title, rate, comment, current_timestamp()
    )
    for work_date, start_time, hours in clean_days:
        repository.add_task_day(db, task_id, work_date, start_time, hours)
    return True, "Задача добавлена в расписание.", task_id


def create_linked_task(db, item, employee_name, rate, comment, day_rows, create_order_assignment):
    """`create_order_assignment` is app.py's _create_tuning_item_assignment,
    injected so a task added here goes through the exact same
    tuning_item_assignments insert (+ notification + budget-overrun flag)
    as "Поручить задачу" on the order/board — one code path for both entry
    points, not two that could drift apart."""
    errors = []
    employee_name = _normalise_text(employee_name, EMPLOYEE_NAME_MAX_LENGTH)
    comment = _normalise_text(comment, COMMENT_MAX_LENGTH)
    eligible = {employee["name"] for employee in repository.list_tuning_crew_employees(db)}
    if not employee_name or employee_name not in eligible:
        errors.append("Выберите сотрудника из списка тюнингмэнов.")
    try:
        rate = float(str(rate or "").strip().replace(",", "."))
    except ValueError:
        rate = 0
    if rate <= 0:
        errors.append("Ставка должна быть больше нуля.")
    clean_days = _clean_task_days(day_rows, errors)
    if errors:
        return False, " ".join(errors), None
    total_hours = sum(hours for _, _, hours in clean_days)
    assignment_id = create_order_assignment(db, item, employee_name, rate, total_hours, comment)
    task_id = repository.create_task(
        db, assignment_id, employee_name, item["work_name"], rate, comment, current_timestamp()
    )
    for work_date, start_time, hours in clean_days:
        repository.add_task_day(db, task_id, work_date, start_time, hours)
    return True, "Задача добавлена в расписание.", task_id


def add_task_day(db, task_id, raw_date, raw_start_time, raw_hours):
    errors = []
    clean_days = _clean_task_days([(raw_date, raw_start_time, raw_hours)], errors)
    if errors:
        return False, " ".join(errors)
    work_date, start_time, hours = clean_days[0]
    repository.add_task_day(db, task_id, work_date, start_time, hours)
    return True, "День добавлен в расписание задачи."


def remove_task_day(db, task_id, day_id):
    repository.remove_task_day(db, task_id, day_id)
    return True, "День убран из расписания задачи."


def set_task_status(db, task_id, status, pay_free_task, update_order_assignment_status):
    """For a task linked to an order (assignment_id set), status changes go
    through update_order_assignment_status — app.py's
    _set_tuning_item_assignment_status, the same helper the order board
    uses, so tuning_item_assignments.assignment_status (the one source of
    truth for a linked task) and its first-time-done payout stay exactly
    as they already work today. A free task tracks its own status/payout
    here (pay_free_task — app.py's own entries-insert helper, injected the
    same way)."""
    task = repository.get_task(db, task_id)
    if task is None:
        return False, "Задача не найдена."
    if task["assignment_id"] is not None:
        update_order_assignment_status(db, task["assignment_id"], status)
        return True, "Статус обновлён."
    repository.set_task_status(db, task_id, status, current_timestamp())
    if status == "done" and not task["entry_id"]:
        total_hours = sum(
            row["planned_hours"] for row in repository.list_task_days(db, task_id)
        )
        entry_id = pay_free_task(db, task, total_hours)
        if entry_id:
            repository.set_task_entry(db, task_id, entry_id)
    return True, "Статус обновлён."
