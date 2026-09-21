"""Validation and view models for the tuning-center work schedule."""

import datetime as dt

from modules.schedule.services import current_timestamp, day_label, parse_day  # noqa: F401 (re-exported)

from . import repository

TASK_TITLE_MAX_LENGTH = 200
EMPLOYEE_NAME_MAX_LENGTH = 120
COMMENT_MAX_LENGTH = 2000


def _normalise_text(value, limit):
    return " ".join(str(value or "").strip().split())[:limit]


def _clean_day_hours(day_hours, errors):
    """day_hours: iterable of (raw_date, raw_hours) pairs — one "Дата
    выполнения" pair for a single-day task, or one pair per day of a
    "Период выполнения". Returns a de-duplicated, validated list of
    (date_iso, hours) tuples."""
    clean = []
    seen_dates = set()
    for raw_date, raw_hours in day_hours:
        try:
            work_date = dt.date.fromisoformat(str(raw_date or "").strip())
        except ValueError:
            errors.append("Некорректная дата в периоде выполнения.")
            continue
        try:
            hours = float(str(raw_hours or "").strip().replace(",", "."))
        except ValueError:
            hours = 0
        if hours <= 0:
            errors.append(f"Укажите часы на {work_date.isoformat()}.")
            continue
        if work_date.isoformat() in seen_dates:
            continue
        seen_dates.add(work_date.isoformat())
        clean.append((work_date.isoformat(), hours))
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


def create_free_task(db, employee_name, title, rate, comment, day_hours):
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
    clean_days = _clean_day_hours(day_hours, errors)
    if errors:
        return False, " ".join(errors), None
    task_id = repository.create_task(
        db, None, employee_name, title, rate, comment, current_timestamp()
    )
    for work_date, hours in clean_days:
        repository.add_task_day(db, task_id, work_date, hours)
    return True, "Задача добавлена в расписание.", task_id


def create_linked_task(db, item, employee_name, rate, comment, day_hours, create_order_assignment):
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
    clean_days = _clean_day_hours(day_hours, errors)
    if errors:
        return False, " ".join(errors), None
    total_hours = sum(hours for _, hours in clean_days)
    assignment_id = create_order_assignment(db, item, employee_name, rate, total_hours, comment)
    task_id = repository.create_task(
        db, assignment_id, employee_name, item["work_name"], rate, comment, current_timestamp()
    )
    for work_date, hours in clean_days:
        repository.add_task_day(db, task_id, work_date, hours)
    return True, "Задача добавлена в расписание.", task_id


def add_task_day(db, task_id, raw_date, raw_hours):
    errors = []
    clean_days = _clean_day_hours([(raw_date, raw_hours)], errors)
    if errors:
        return False, " ".join(errors)
    work_date, hours = clean_days[0]
    repository.add_task_day(db, task_id, work_date, hours)
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
