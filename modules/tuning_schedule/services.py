"""Validation and view models for the tuning-center work schedule."""

import datetime as dt
import math

from modules.schedule.services import current_timestamp, day_label, parse_day  # noqa: F401 (re-exported)

from . import repository
from .constants import (
    DEFAULT_DAY_END_HOUR, DEFAULT_DAY_START_HOUR, DEFAULT_SHIFT_END, DEFAULT_SHIFT_START,
    MIN_CARD_MINUTES,
)

ADMIN_POSITION = "Администратор"
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


def _minutes(label):
    parsed = _parse_hhmm(label)
    return parsed[0] * 60 + parsed[1] if parsed else None


def _label(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def clean_shift_hours(raw_start, raw_end, errors):
    """Working hours of a shift as ("HH:MM", "HH:MM"). Both blank means the
    default workday; otherwise both must be valid and end after start."""
    start_raw, end_raw = str(raw_start or "").strip(), str(raw_end or "").strip()
    if not start_raw and not end_raw:
        return DEFAULT_SHIFT_START, DEFAULT_SHIFT_END
    start, end = _minutes(start_raw), _minutes(end_raw)
    if start is None or end is None:
        errors.append("Укажите рабочие часы смены (начало и конец, формат ЧЧ:ММ).")
        return None
    if end <= start:
        errors.append("Конец смены должен быть позже её начала.")
        return None
    return _label(start), _label(end)


def shift_bounds(row):
    """(start, end) labels of a roster row, falling back to the default
    workday for rows saved before shifts had hours."""
    return (row["shift_start"] or DEFAULT_SHIFT_START, row["shift_end"] or DEFAULT_SHIFT_END)


def add_day_crew_member(db, day, employee_id, raw_start=None, raw_end=None):
    eligible = {
        employee["id"]: employee
        for employee in repository.list_tuning_crew_employees(db)
    }
    if employee_id not in eligible:
        return False, "Сотрудник не найден или не является тюнингмэном."
    errors = []
    hours = clean_shift_hours(raw_start, raw_end, errors)
    if errors:
        return False, " ".join(errors)
    added = repository.add_day_crew_member(
        db, day.isoformat(), employee_id, current_timestamp(), hours[0], hours[1]
    )
    if not added:
        return True, f"{eligible[employee_id]['name']} уже добавлен в расписание."
    return True, (
        f"{eligible[employee_id]['name']} добавлен в расписание: смена {hours[0]}–{hours[1]}."
    )


def set_shift_hours(db, day, employee_id, raw_start, raw_end):
    errors = []
    hours = clean_shift_hours(raw_start, raw_end, errors)
    if errors:
        return False, " ".join(errors)
    if not repository.set_shift_hours(db, day.isoformat(), employee_id, hours[0], hours[1]):
        return False, "Сотрудник не стоит в расписании на эту дату."
    return True, f"Часы смены обновлены: {hours[0]}–{hours[1]}."


def employees_off_shift(db, day_iso, employee_names):
    """The names among `employee_names` who are not on the roster of day_iso."""
    return [
        name for name in employee_names
        if repository.get_day_crew_shift_by_name(db, day_iso, name) is None
    ]


def _next_free_start(db, day_iso, employee_name, shift_start, exclude_task_id=None):
    """Minute of the day a new task of this employee should start at: the
    start of their shift, or the end of their last task that day if later."""
    start_minutes = _minutes(shift_start)
    for row in repository.list_day_tasks(db, day_iso):
        if row["employee_name"] != employee_name or row["task_id"] == exclude_task_id:
            continue
        begins = _minutes(row["start_time"]) or 0
        start_minutes = max(start_minutes, begins + round(row["planned_hours"] * 60))
    return start_minutes


def place_assignment_task(db, assignment_id, employee_name, title, rate, comment, day_iso, hours):
    """Puts an order task (a tuning_item_assignments row) on the employee's
    day as a card: at the start of their shift, or right after the last task
    they already have that day. Returns None when the employee isn't on that
    day's roster, otherwise {"placed": bool, ...} — not placed (with a
    reason) when the hours don't fit a single day."""
    crew = repository.get_day_crew_shift_by_name(db, day_iso, employee_name)
    if crew is None:
        return None
    if hours <= 0 or hours > MAX_DAY_HOURS:
        return {"placed": False, "reason": "hours"}
    shift_start, shift_end = shift_bounds(crew)
    start_minutes = _next_free_start(db, day_iso, employee_name, shift_start)
    end_minutes = start_minutes + round(hours * 60)
    if end_minutes > 24 * 60:
        return {"placed": False, "reason": "midnight"}
    existing = repository.get_task_by_assignment(db, assignment_id)  # e.g. left empty by a failed run
    task_id = existing["id"] if existing else repository.create_task(
        db, assignment_id, employee_name, title, rate, comment, current_timestamp()
    )
    try:
        repository.add_task_day(db, task_id, day_iso, _label(start_minutes), hours)
    except Exception:
        if existing is None:
            repository.delete_task(db, task_id)  # never leave a card-less task behind
        raise
    return {
        "placed": True, "task_id": task_id, "start": _label(start_minutes),
        "end": _label(end_minutes), "past_shift_end": end_minutes > _minutes(shift_end),
        "shift_end": shift_end,
    }


def move_assignment_task(db, assignment_id, employee_name, day_iso):
    """Moves the card of a one-day order task to another day (after the
    employee's other tasks there). Returns None when the assignment has no
    card; {"moved": False, "reason": ...} when it can't move (a multi-day
    card, the same day, or the employee isn't on that day's roster)."""
    task = repository.get_task_by_assignment(db, assignment_id)
    if task is None:
        return None
    days = repository.list_task_days(db, task["id"])
    if not days:
        return None  # a task with no day is no card — the caller places it afresh
    if len(days) != 1:
        return {"moved": False, "reason": "multi_day"}
    if days[0]["work_date"] == day_iso:
        return {"moved": False, "reason": "same_day"}
    crew = repository.get_day_crew_shift_by_name(db, day_iso, employee_name)
    if crew is None:
        return {"moved": False, "reason": "off_shift"}
    shift_start, _shift_end = shift_bounds(crew)
    start_minutes = _next_free_start(db, day_iso, employee_name, shift_start)
    hours = days[0]["planned_hours"]
    if start_minutes + round(hours * 60) > 24 * 60:
        return {"moved": False, "reason": "midnight"}
    repository.delete_task_days(db, task["id"])
    repository.add_task_day(db, task["id"], day_iso, _label(start_minutes), hours)
    return {"moved": True, "start": _label(start_minutes)}


MAX_PERIOD_DAYS = 92


def place_assignment_period(
    db, assignment_id, employee_name, title, rate, comment, from_iso, to_iso, hours, replace=False
):
    """Spreads an order task given for a period over the days of that period
    the employee is on the shift: the norm-hours are divided by the number
    of those working days and each day's share goes into the free time of
    that day (after the tasks already there, from the start of the shift).

    Returns None when the employee works none of the days; otherwise
    {"placed": bool, ...}: not placed (with a reason) when the share doesn't
    fit a day; placed gives the days ([{date, start, end, hours,
    past_shift_end}]), the per-day hours and the dates skipped for not
    fitting before midnight. With `replace` the card's previous days are
    dropped first (the dates of the task were changed)."""
    try:
        first = dt.date.fromisoformat(from_iso)
        last = dt.date.fromisoformat(to_iso)
    except (TypeError, ValueError):
        return None
    if last < first or (last - first).days >= MAX_PERIOD_DAYS:
        return {"placed": False, "reason": "period"}
    days = [first + dt.timedelta(days=offset) for offset in range((last - first).days + 1)]
    workdays = []
    for day in days:
        crew = repository.get_day_crew_shift_by_name(db, day.isoformat(), employee_name)
        if crew is not None:
            workdays.append((day.isoformat(), crew))
    if not workdays:
        return None
    if hours <= 0:
        return {"placed": False, "reason": "hours"}
    per_day = round(hours / len(workdays), 2)
    if per_day <= 0 or per_day > MAX_DAY_HOURS:
        return {"placed": False, "reason": "hours"}
    existing = repository.get_task_by_assignment(db, assignment_id)
    task_id = existing["id"] if existing else repository.create_task(
        db, assignment_id, employee_name, title, rate, comment, current_timestamp()
    )
    if replace and existing:
        repository.delete_task_days(db, task_id)
    placed, skipped = [], []
    for index, (day_iso, crew) in enumerate(workdays):
        day_hours = per_day if index < len(workdays) - 1 else round(hours - per_day * (len(workdays) - 1), 2)
        if day_hours <= 0:
            continue
        shift_start, shift_end = shift_bounds(crew)
        start = _next_free_start(db, day_iso, employee_name, shift_start, exclude_task_id=task_id)
        end = start + round(day_hours * 60)
        if end > 24 * 60:
            skipped.append(day_iso)
            continue
        repository.add_task_day(db, task_id, day_iso, _label(start), day_hours)
        placed.append({
            "date": day_iso, "start": _label(start), "end": _label(end), "hours": day_hours,
            "past_shift_end": end > _minutes(shift_end),
        })
    if not placed:
        if existing is None:
            repository.delete_task(db, task_id)  # never leave a card-less task behind
        return {"placed": False, "reason": "midnight"}
    return {
        "placed": True, "task_id": task_id, "days": placed, "per_day": per_day,
        "workdays": len(workdays), "skipped": skipped,
    }


def place_pending_period_tasks(db, day_iso):
    """Order tasks given for a period that cover `day_iso` and have no card
    yet (the person wasn't on the shift when it was assigned, or it was
    assigned before periods were placed automatically) get their days now.
    Returns how many tasks were placed."""
    rows = db.execute(
        "SELECT tia.id, tia.employee_name, tia.rate, tia.norm_hours, tia.comment, "
        "tia.due_from, tia.due_to, ti.work_name "
        "FROM tuning_item_assignments tia JOIN tuning_order_items ti ON ti.id = tia.item_id "
        "WHERE tia.due_to IS NOT NULL AND tia.due_from <= ? AND tia.due_to >= ? "
        "AND tia.partner_id IS NULL AND tia.assignment_status NOT IN ('done', 'rejected') "
        "AND ti.status != 'removed' "
        "AND NOT EXISTS (SELECT 1 FROM tuning_schedule_tasks t "
        "                JOIN tuning_schedule_task_days d ON d.task_id = t.id "
        "                WHERE t.assignment_id = tia.id) "
        "ORDER BY tia.id",
        (day_iso, day_iso),
    ).fetchall()
    count = 0
    for row in rows:
        result = place_assignment_period(
            db, row["id"], row["employee_name"], row["work_name"], row["rate"], row["comment"],
            row["due_from"], row["due_to"], row["norm_hours"],
        )
        if result and result.get("placed"):
            count += 1
    return count


def sync_assignment_hours(db, assignment_id, hours):
    """Keeps the card's length equal to the task's norm-hours: a one-day card
    takes them whole, a multi-day one is scaled day by day in proportion."""
    task = repository.get_task_by_assignment(db, assignment_id)
    if task is None or not (0 < hours <= MAX_DAY_HOURS * MAX_PERIOD_DAYS):
        return
    days = repository.list_task_days(db, task["id"])
    if len(days) == 1 and hours <= MAX_DAY_HOURS:
        repository.set_task_day_hours(db, days[0]["id"], hours)
    elif len(days) > 1:
        total = sum(day["planned_hours"] for day in days)
        if total <= 0:
            return
        shares = [round(day["planned_hours"] * hours / total, 2) for day in days]
        shares[-1] = round(hours - sum(shares[:-1]), 2)
        if all(0 < share <= MAX_DAY_HOURS for share in shares):
            for day, share in zip(days, shares):
                repository.set_task_day_hours(db, day["id"], share)


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
    shifts = repository.list_day_crew_shifts(db, day.isoformat())
    for employee in crew:
        employee["position_label"] = " · ".join(employee["positions"])
        employee["has_day_task"] = employee["id"] in assigned_today_ids
        if employee["id"] in shifts:
            start, end = shifts[employee["id"]]
            employee["shift_start"] = start or DEFAULT_SHIFT_START
            employee["shift_end"] = end or DEFAULT_SHIFT_END
            employee["shift_label"] = f"{employee['shift_start']}–{employee['shift_end']}"
    for employee in crew:
        employee["is_admin"] = ADMIN_POSITION in employee["positions"]
    day_crew = [employee for employee in crew if employee["id"] in day_crew_ids]
    available_crew = [employee for employee in crew if employee["id"] not in day_crew_ids]
    # administrators can be given a task even when they are not on the day's
    # shift — they are put on it automatically (see _ensure_on_roster)
    available_admins = [employee for employee in available_crew if employee["is_admin"]]

    tasks_by_employee_name = {}
    for row in repository.list_day_tasks(db, day.isoformat()):
        task = dict(row)
        task["is_linked"] = task["assignment_id"] is not None
        if task["order_id"] is not None:
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
        "available_admins": available_admins,
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

    participants = {}
    for task in raw_tasks:
        participants.setdefault(task["task_id"], []).append(task["employee_name"])
    cards_by_employee = {employee["name"]: [] for employee in day_crew}
    for task in raw_tasks:
        if task["employee_name"] not in cards_by_employee:
            continue
        task["is_linked"] = task["assignment_id"] is not None
        # an administrator's task can carry Telegram reminders, with or
        # without a project
        # (reminders belong to the task, so only its first participant's card
        # carries the bell)
        task["can_remind"] = (
            not task.get("is_member")
            and repository.is_administrator(db, task["employee_name"])
        )
        task["reminders"] = (
            repository.list_task_reminders(db, task["assignment_id"], task["task_id"])
            if task["can_remind"] else []
        )
        task["with_names"] = [
            name for name in participants.get(task["task_id"], []) if name != task["employee_name"]
        ]
        if task["order_id"] is not None:
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
        task["editor"] = editor_payload(db, task)
        cards_by_employee[task["employee_name"]].append(task)

    hour_marks = []
    for minute in range(earliest, latest + 1, 60):
        hour_marks.append({
            "label": f"{minute // 60:02d}:00",
            "top_px": round((minute - earliest) * PX_PER_MINUTE, 2),
        })

    # what each person has on this day: the norm-hours of their cards and
    # what those hours would earn (rate x hours; an administrator's tasks
    # carry no rate). Declined tasks are not counted.
    day_totals = {}
    for name, cards in cards_by_employee.items():
        counted = [card for card in cards if card["status"] != "rejected"]
        day_totals[name] = {
            "hours": round(sum(card["planned_hours"] for card in counted), 2),
            "earnings": round(sum(card["planned_hours"] * (card["rate"] or 0) for card in counted), 2),
            "tasks": len(counted),
        }
    return {
        "day_totals": day_totals,
        "cards_by_employee": cards_by_employee,
        "hour_marks": hour_marks,
        "grid_height": round(total_minutes * PX_PER_MINUTE, 2),
        # for the drag-and-drop script: where the grid starts and how big a minute is
        "grid_start_minutes": earliest,
        "grid_minutes": total_minutes,
        "px_per_minute": PX_PER_MINUTE,
    }


def _clean_rate(db, employee_name, raw_rate, errors):
    """The hourly rate of a task. Administrators are salaried: no rate is
    asked for (whatever was submitted is ignored) and their tasks carry 0."""
    if repository.is_administrator(db, employee_name):
        return 0.0
    try:
        rate = float(str(raw_rate or "").strip().replace(",", "."))
    except ValueError:
        rate = 0
    if rate <= 0:
        errors.append("Ставка должна быть больше нуля.")
    return rate


def _ensure_on_roster(db, employee_name, clean_days):
    """An administrator isn't required to be on the shift schedule; when a
    task is put on a day they are not on, they are added to that day's
    roster (default hours, widened to cover the task). Anyone else is left
    alone. Returns the dates the person was added for."""
    if not repository.is_administrator(db, employee_name):
        return []
    employee_id = repository.get_employee_id_by_name(db, employee_name)
    if employee_id is None:
        return []
    added = []
    for work_date, start_time, hours in clean_days:
        if employee_id in repository.list_day_crew_ids(db, work_date):
            continue
        start = _minutes(start_time)
        shift_start = min(_minutes(DEFAULT_SHIFT_START), start)
        shift_end = max(_minutes(DEFAULT_SHIFT_END), min(start + round(hours * 60), 24 * 60 - 1))
        repository.add_day_crew_member(
            db, work_date, employee_id, current_timestamp(), _label(shift_start), _label(shift_end)
        )
        added.append(work_date)
    return added


def _roster_note(employee_name, added_days):
    if not added_days:
        return ""
    days = ", ".join(
        dt.date.fromisoformat(day).strftime("%d.%m") for day in added_days
    )
    return f" {employee_name} добавлен в смену на {days}."


def person_days(clean_days, person_hours):
    """The days of a task as one participant has them: with their own
    norm-hours (None = the task's own) spread over the days in proportion to
    the task's hours of each day; the last day takes the rounding remainder."""
    if person_hours is None:
        return list(clean_days)
    total = sum(hours for _date, _start, hours in clean_days)
    if total <= 0:
        return list(clean_days)
    shares = [round(hours * person_hours / total, 2) for _date, _start, hours in clean_days]
    shares[-1] = round(person_hours - sum(shares[:-1]), 2)
    return [(date, start, share) for (date, start, _hours), share in zip(clean_days, shares)]


def scaled_day_rows(day_rows, person_hours):
    """Raw (date, start, hours) form rows of a task, as one participant with
    their own norm-hours has them (unchanged when that isn't set/parsable)."""
    if person_hours is None:
        return list(day_rows)
    try:
        hours = [float(str(row[2]).strip().replace(",", ".")) for row in day_rows]
    except (ValueError, IndexError):
        return list(day_rows)
    total = sum(hours)
    if total <= 0:
        return list(day_rows)
    shares = [round(value * person_hours / total, 2) for value in hours]
    shares[-1] = round(person_hours - sum(shares[:-1]), 2)
    return [(row[0], row[1], str(share)) for row, share in zip(day_rows, shares)]


def clean_extra_participants(db, raw_extras, primary_name, errors, clean_days=None):
    """Further people on one task: raw_extras = [(employee_name, raw_rate,
    raw_hours)] as entered (blank rows skipped; raw_hours blank = the task's
    own hours). Returns [(name, rate, hours-or-None)]; problems go to
    `errors`. Each person may appear once, never the first participant. With
    `clean_days` a person's share of every day is checked to fit a day."""
    eligible = {employee["name"] for employee in repository.list_tuning_crew_employees(db)}
    seen = {primary_name}
    result = []
    for raw in raw_extras:
        raw_name, raw_rate = raw[0], raw[1]
        raw_hours = raw[2] if len(raw) > 2 else ""
        name = _normalise_text(raw_name, EMPLOYEE_NAME_MAX_LENGTH)
        if not name:
            continue
        if name not in eligible:
            errors.append("Выберите сотрудника из списка тюнингмэнов.")
            continue
        if name in seen:
            errors.append(f"{name} указан в задаче дважды.")
            continue
        hours = None
        if str(raw_hours or "").strip():
            try:
                hours = float(str(raw_hours).strip().replace(",", "."))
            except ValueError:
                hours = 0
            if hours <= 0:
                errors.append(f"{name}: часы должны быть числом больше нуля.")
                continue
            if clean_days and any(
                not (0 < share <= MAX_DAY_HOURS) for _d, _s, share in person_days(clean_days, hours)
            ):
                errors.append(f"{name}: столько часов не помещается в сутки по дням задачи.")
                continue
        result.append((name, _clean_rate(db, name, raw_rate, errors), hours))
        seen.add(name)
    return result


def collect_extra_participants(form):
    """The 'ещё сотрудники' rows of a task form -> [(name, raw_rate, raw_hours)]."""
    names = form.getlist("extra_employee_name[]")
    rates = form.getlist("extra_rate[]")
    hours = form.getlist("extra_hours[]")
    return [
        (name, rates[i] if i < len(rates) else "", hours[i] if i < len(hours) else "")
        for i, name in enumerate(names) if str(name or "").strip()
    ]


def _pay_unpaid_members(db, task, pay_free_task):
    """Pays every further participant of a finished free task their own
    rate x their hours (the task's own when they have none), once (their
    payout is remembered per person)."""
    task_hours = sum(row["planned_hours"] for row in repository.list_task_days(db, task["id"]))
    for member in repository.list_task_members(db, task["id"]):
        if member["entry_id"]:
            continue
        total_hours = member["hours"] if member["hours"] is not None else task_hours
        entry_id = pay_free_task(db, {
            "id": task["id"], "employee_name": member["employee_name"], "rate": member["rate"],
            "title": task["title"], "order_id": task["order_id"], "entry_id": None,
        }, total_hours)
        if entry_id:
            repository.set_member_entry(db, member["id"], entry_id)


def create_free_task(db, employee_name, title, rate, comment, day_rows, order_id=None, extras=()):
    """A task that is not tied to a work item. With `order_id` it still
    belongs to that project (tuning order) and is shown with it. `extras`
    are raw (name, rate) rows of further participants of the same task."""
    errors = []
    employee_name = _normalise_text(employee_name, EMPLOYEE_NAME_MAX_LENGTH)
    title = _normalise_text(title, TASK_TITLE_MAX_LENGTH)
    comment = _normalise_text(comment, COMMENT_MAX_LENGTH)
    eligible = {employee["name"] for employee in repository.list_tuning_crew_employees(db)}
    if not employee_name or employee_name not in eligible:
        errors.append("Выберите сотрудника из списка тюнингмэнов.")
    if not title:
        errors.append("Укажите название задачи.")
    rate = _clean_rate(db, employee_name, rate, errors)
    if order_id is not None and repository.get_linkable_order(db, order_id) is None:
        errors.append("Проект не найден или уже закрыт.")
    clean_days = _clean_task_days(day_rows, errors)
    members = clean_extra_participants(db, extras, employee_name, errors, clean_days)
    if errors:
        return False, " ".join(errors), None
    added_days = _ensure_on_roster(db, employee_name, clean_days)
    task_id = repository.create_task(
        db, None, employee_name, title, rate, comment, current_timestamp(), order_id
    )
    for work_date, start_time, hours in clean_days:
        repository.add_task_day(db, task_id, work_date, start_time, hours)
    if members:
        repository.replace_task_members(db, task_id, members)
        for name, _rate, member_hours in members:
            added_days += _ensure_on_roster(db, name, person_days(clean_days, member_hours))
    return True, "Задача добавлена в расписание." + _roster_note(employee_name, added_days), task_id


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
    rate = _clean_rate(db, employee_name, rate, errors)
    clean_days = _clean_task_days(day_rows, errors)
    if errors:
        return False, " ".join(errors), None
    added_days = _ensure_on_roster(db, employee_name, clean_days)
    total_hours = sum(hours for _, _, hours in clean_days)
    work_dates = sorted(work_date for work_date, _, _ in clean_days)
    due = {"due_from": work_dates[0], "due_to": work_dates[-1] if work_dates[-1] != work_dates[0] else None} \
        if work_dates else {}
    assignment_id = create_order_assignment(
        db, item, employee_name, rate, total_hours, comment, **due
    )
    task_id = repository.create_task(
        db, assignment_id, employee_name, item["work_name"], rate, comment, current_timestamp()
    )
    for work_date, start_time, hours in clean_days:
        repository.add_task_day(db, task_id, work_date, start_time, hours)
    return True, "Задача добавлена в расписание." + _roster_note(employee_name, added_days), task_id


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
        order_id = repository.get_assignment_order_id(db, task["assignment_id"])
        if order_id is not None:
            return True, f"Статус обновлён — так же и на доске задач заказа №{order_id}."
        return True, "Статус обновлён."
    repository.set_task_status(db, task_id, status, current_timestamp())
    if status == "done":
        if not task["entry_id"]:
            total_hours = sum(
                row["planned_hours"] for row in repository.list_task_days(db, task_id)
            )
            entry_id = pay_free_task(db, task, total_hours)
            if entry_id:
                repository.set_task_entry(db, task_id, entry_id)
        _pay_unpaid_members(db, repository.get_task(db, task_id), pay_free_task)
    return True, "Статус обновлён."


def move_task_card(db, task_id, day_id, raw_start, raw_target_employee_id, reassign_order_assignment,
                   raw_source_employee=None):
    """Drag-and-drop of a calendar card: a new start time on the same day,
    optionally in another employee's column. Returns (ok, message, card)
    where card = {start, end, employee_name} on success.

    The card must fit before midnight and not overlap another card of any
    participant that day; the target must be on that day's roster.
    Changing the employee moves the whole task, so it is refused for a
    multi-day task and for a finished one; for an order task it goes through
    `reassign_order_assignment(db, assignment_id, employee_name)` (the same
    hand-over the order board uses: payout/materials follow, the new person
    is notified), for a free task the card's own employee is changed.
    A task with several people shows a card in each person's column:
    `raw_source_employee` is the column the card was dragged from, so
    dragging a further participant's card to another column replaces just
    that person."""
    row = repository.get_day_task(db, task_id, day_id)
    if row is None:
        return False, "Задача не найдена. Обновите страницу.", None
    start_minutes = _minutes(str(raw_start or "").strip())
    if start_minutes is None:
        return False, "Некорректное время начала.", None
    day_iso = row["work_date"]
    # each participant has their own hours of this day (the task's, or their own norm)
    own_hours = {
        other["employee_name"]: other["planned_hours"]
        for other in repository.list_day_tasks(db, day_iso) if other["task_id"] == task_id
    }
    for person, person_hours in own_hours.items():
        if start_minutes + round(person_hours * 60) > 24 * 60:
            return False, "Задача не помещается до конца суток.", None
    end_minutes = start_minutes + round(row["planned_hours"] * 60)
    primary_name = row["employee_name"]
    members = {m["employee_name"] for m in repository.list_task_members(db, task_id)}
    source = str(raw_source_employee or "").strip()
    moving_member = source if source and source != primary_name and source in members else None
    current_name = moving_member or primary_name
    target_name = current_name
    if raw_target_employee_id not in (None, ""):
        try:
            target_id = int(raw_target_employee_id)
        except (TypeError, ValueError):
            return False, "Некорректный сотрудник.", None
        if target_id not in repository.list_day_crew_ids(db, day_iso):
            return False, "Этот сотрудник не стоит в смене на эту дату.", None
        names = {e["id"]: e["name"] for e in repository.list_tuning_crew_employees(db)}
        if target_id not in names:
            return False, "Сотрудник не найден или не подходит для расписания тюнинга.", None
        target_name = names[target_id]
        if target_name != current_name and (target_name == primary_name or target_name in members):
            return False, f"{target_name} уже участвует в этой задаче.", None

    participants = {primary_name} | members
    participants.discard(current_name)
    participants.add(target_name)
    hours_of = dict(own_hours)
    hours_of[target_name] = own_hours.get(current_name, row["planned_hours"])
    for other in repository.list_day_tasks(db, day_iso):
        if other["employee_name"] not in participants or other["task_id"] == task_id:
            continue
        other_start = _minutes(other["start_time"]) or 0
        other_end = other_start + round(other["planned_hours"] * 60)
        person_end = start_minutes + round(hours_of.get(other["employee_name"], row["planned_hours"]) * 60)
        if start_minutes < other_end and other_start < person_end:
            return False, f"{other['employee_name']}: это время занято задачей «{other['title']}».", None

    message = "Задача перенесена."
    if target_name != current_name:
        if len(repository.list_task_days(db, task_id)) > 1:
            return False, (
                "Задача идёт несколько дней — сменить сотрудника можно на доске задач заказа."
            ), None
        if moving_member:
            repository.rename_task_member(db, task_id, current_name, target_name)
            message = f"В задаче {current_name} заменён на {target_name}."
        elif row["assignment_id"] is not None:
            if row["status"] == "done":
                return False, "Выполненную задачу нельзя передать другому сотруднику.", None
            ok, text = reassign_order_assignment(db, row["assignment_id"], target_name)
            if not ok:
                return False, text, None
            message = text or message
        else:
            repository.set_free_task_employee(db, task_id, target_name)
            message = f"Задача передана: {current_name} → {target_name}."
    repository.set_task_day_start(db, day_id, _label(start_minutes))
    return True, message, {
        "start": _label(start_minutes), "end": _label(end_minutes), "employee_name": target_name,
    }


def editor_payload(db, card):
    """Everything the schedule's task editor shows for one calendar card
    (a list_day_tasks row): the task's own fields and all its days."""
    task = repository.get_task(db, card["task_id"])
    linked = task["assignment_id"] is not None
    assignment = repository.get_assignment(db, task["assignment_id"]) if linked else None
    members = repository.list_task_members(db, task["id"])
    paid_entry = (assignment["entry_id"] if linked else task["entry_id"]) or any(
        member["entry_id"] for member in members
    )
    # the card may belong to a further participant; the editor always shows
    # the task's first participant and lists the others
    primary_name = assignment["employee_name"] if linked else task["employee_name"]
    primary_rate = assignment["rate"] if linked else task["rate"]
    coworkers = []
    if linked:
        coworkers = [
            row["employee_name"] for row in db.execute(
                "SELECT employee_name FROM tuning_item_assignments "
                "WHERE item_id = ? AND id != ? AND assignment_status != 'rejected' ORDER BY id",
                (assignment["item_id"], assignment["id"]),
            ).fetchall()
        ]
    return {
        "task_id": task["id"],
        "is_linked": linked,
        "title": card["title"],
        "employee_name": primary_name,
        "order_id": card["order_id"],
        "rate": primary_rate,
        "status": card["status"],
        "comment": card["comment"] or "",
        "is_admin": repository.is_administrator(db, primary_name),
        "members": [
            {"name": member["employee_name"], "rate": member["rate"], "hours": member["hours"],
             "is_admin": repository.is_administrator(db, member["employee_name"])}
            for member in members
        ],
        "coworkers": coworkers,
        "paid": bool(paid_entry),
        "days": [
            {"date": day["work_date"], "start": day["start_time"], "hours": day["planned_hours"]}
            for day in repository.list_task_days(db, task["id"])
        ],
    }


def update_task(db, task_id, form, hooks):
    """Saves everything the task editor can change: employee, title and
    project (a free task; an order task keeps its work), rate, comment,
    days (date / start / hours — added, changed or removed) and status.

    `hooks` carries app.py's own helpers so an order task goes through the
    exact code the order board uses: reassign(db, assignment_id, name),
    terms(db, assignment_id, rate, hours), status(db, task_id, status) and
    the set of valid status values. All validation happens before the first
    write. Returns (ok, message)."""
    task = repository.get_task(db, task_id)
    if task is None:
        return False, "Задача не найдена."
    linked = task["assignment_id"] is not None
    assignment = repository.get_assignment(db, task["assignment_id"]) if linked else None
    current_name = assignment["employee_name"] if linked else task["employee_name"]
    current_status = assignment["assignment_status"] if linked else task["status"]
    paid = bool(assignment["entry_id"] if linked else task["entry_id"])

    errors = []
    employee_name = _normalise_text(form.get("employee_name"), EMPLOYEE_NAME_MAX_LENGTH)
    eligible = {employee["name"] for employee in repository.list_tuning_crew_employees(db)}
    if employee_name not in eligible:
        errors.append("Выберите сотрудника из списка тюнингмэнов.")
    comment = _normalise_text(form.get("comment"), COMMENT_MAX_LENGTH)
    status = str(form.get("status", "") or current_status).strip()
    if status not in hooks["statuses"]:
        errors.append("Некорректный статус.")

    title = task["title"]
    order_id = task["order_id"]
    if not linked:
        title = _normalise_text(form.get("title"), TASK_TITLE_MAX_LENGTH)
        if not title:
            errors.append("Укажите название задачи.")
        raw_order = str(form.get("order_id", "") or "").strip()
        order_id = None
        if raw_order:
            try:
                order_id = int(raw_order)
            except ValueError:
                errors.append("Проект не найден.")
            else:
                if order_id != task["order_id"] and repository.get_linkable_order(db, order_id) is None:
                    errors.append("Проект не найден или уже закрыт.")
    rate = _clean_rate(db, employee_name, form.get("rate"), errors)
    day_rows = [
        row for row in zip(
            form.getlist("work_date[]"), form.getlist("start_time[]"), form.getlist("planned_hours[]")
        ) if any(str(value or "").strip() for value in row)
    ]
    clean_days = _clean_task_days(day_rows, errors)
    if not errors and len(clean_days) < len(day_rows):
        errors.append("Одна и та же дата указана дважды.")
    extras = clean_extra_participants(
        db, collect_extra_participants(form), employee_name, errors, clean_days
    )
    if linked and not errors:
        taken = {
            row["employee_name"] for row in db.execute(
                "SELECT employee_name FROM tuning_item_assignments WHERE item_id = ? AND id != ?",
                (assignment["item_id"], assignment["id"]),
            ).fetchall()
        }
        for name, _extra_rate, _extra_hours in extras:
            if name in taken:
                errors.append(f"{name} уже назначен на эту работу.")

    renamed = employee_name != current_name
    if renamed and not errors:
        if paid and not linked:
            errors.append("Задача уже оплачена — сменить сотрудника нельзя.")
        elif current_status == "done" and linked:
            errors.append("Выполненную задачу нельзя передать другому сотруднику.")
    if not errors:
        # every participant's own hours of each day (a person with their own
        # norm-hours has a shorter or longer card than the task's)
        shares = {employee_name: {d: h for d, _s, h in clean_days}}
        for name, _extra_rate, extra_hours in extras:
            shares[name] = {d: h for d, _s, h in person_days(clean_days, extra_hours)}
        for work_date, start_time, _hours in clean_days:
            start = _minutes(start_time)
            # every participant must be on the shift and free at that time
            for person, per_day in shares.items():
                end = start + round(per_day[work_date] * 60)
                if end > 24 * 60:
                    errors.append(f"{person}: {work_date} задача не помещается до конца суток.")
                    continue
                if not repository.is_administrator(db, person) \
                        and repository.get_day_crew_shift_by_name(db, work_date, person) is None:
                    errors.append(f"{person} не стоит в смене на {work_date}.")
                    continue
                for other in repository.list_other_day_tasks(db, work_date, task_id):
                    if other["employee_name"] != person:
                        continue
                    other_start = _minutes(other["start_time"]) or 0
                    if start < other_start + round(other["planned_hours"] * 60) and other_start < end:
                        errors.append(f"{person}: {work_date} это время занято задачей «{other['title']}».")
                        break
    if errors:
        return False, " ".join(errors)

    notes = []
    if renamed:
        if linked:
            ok, text = hooks["reassign"](db, task["assignment_id"], employee_name)
            if not ok:
                return False, text
            if text:
                notes.append(text)
        else:
            repository.set_free_task_employee(db, task_id, employee_name)
    added_days = _ensure_on_roster(db, employee_name, clean_days)
    repository.delete_task_days(db, task_id)
    for work_date, start_time, hours in clean_days:
        repository.add_task_day(db, task_id, work_date, start_time, hours)
    total_hours = sum(hours for _date, _start, hours in clean_days)
    if linked:
        dates = sorted(work_date for work_date, _start, _hours in clean_days)
        repository.set_assignment_due(
            db, task["assignment_id"], dates[0], dates[-1] if dates[-1] != dates[0] else None
        )
        if comment != (assignment["comment"] or ""):
            repository.set_linked_task_comment(db, task_id, task["assignment_id"], comment)
        if abs(rate - assignment["rate"]) > 1e-9 or abs(total_hours - assignment["norm_hours"]) > 1e-9:
            ok, text = hooks["terms"](db, task["assignment_id"], rate, total_hours)
            if not ok:
                notes.append(text)
            elif paid:
                notes.append(text)
        item = repository.get_order_item(
            db, repository.get_assignment_order_id(db, task["assignment_id"]), assignment["item_id"]
        )
        for name, extra_rate, extra_hours in extras:
            # one more person on the same work: their own order task over the same days
            ok, text, _new_id = create_linked_task(
                db, item, name, extra_rate, comment, person_days(clean_days, extra_hours),
                hooks["create_assignment"],
            )
            notes.append(f"{name} добавлен в задачу." if ok else text)
            if not ok:
                break
    else:
        repository.update_free_task(db, task_id, title, rate, comment, order_id)
        repository.replace_task_members(db, task_id, extras)
        for name, _extra_rate, extra_hours in extras:
            added_days += _ensure_on_roster(db, name, person_days(clean_days, extra_hours))
    if status != current_status:
        set_task_status(db, task_id, status, hooks["pay_free_task"], hooks["update_status"])
    elif not linked and current_status == "done":
        _pay_unpaid_members(db, repository.get_task(db, task_id), hooks["pay_free_task"])
    return True, ("Задача сохранена. " + " ".join(notes)).strip() + _roster_note(employee_name, added_days)


def delete_task(db, task_id, revoke_order_assignment):
    """Removes a task from the schedule. An order task is revoked from its
    employee exactly as on the order board (its payout is deleted and they
    are told; refused when materials were written off against it); a free
    task goes away with its days and reminders, unless it was already paid."""
    task = repository.get_task(db, task_id)
    if task is None:
        return False, "Задача уже удалена."
    if task["assignment_id"] is not None:
        return revoke_order_assignment(db, task["assignment_id"])
    if task["entry_id"] or any(m["entry_id"] for m in repository.list_task_members(db, task_id)):
        return False, (
            "Задача уже оплачена — сначала удалите выплату на странице «Зарплаты», затем саму задачу."
        )
    repository.delete_task_reminders(db, task_id)
    repository.delete_task(db, task_id)
    return True, f"Задача «{task['title']}» удалена."
