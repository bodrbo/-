"""Validation and view models for the internal trip schedule."""

import datetime as dt
import math

from . import repository
from .constants import (
    CREW_ROLES,
    DEFAULT_DAY_END_HOUR,
    DEFAULT_DAY_START_HOUR,
    ITEM_KINDS,
    MAX_ITEM_HOURS,
    MIN_ITEM_MINUTES,
)


MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def parse_day(raw_day, fallback=None):
    fallback = fallback or dt.date.today()
    try:
        return dt.date.fromisoformat(str(raw_day or ""))
    except ValueError:
        return fallback


def day_label(day):
    return f"{day.day} {MONTHS_GENITIVE[day.month - 1]}, {WEEKDAYS[day.weekday()]}"


def _normalise_text(value, limit):
    return " ".join(str(value or "").strip().split())[:limit]


def _parse_money(raw_value, errors):
    raw_value = str(raw_value or "").strip().replace(" ", "").replace(",", ".")
    if not raw_value:
        return 0.0
    try:
        value = float(raw_value)
    except ValueError:
        errors.append("Стоимость должна быть числом.")
        return 0.0
    if value < 0 or value > 10_000_000:
        errors.append("Стоимость должна быть от 0 до 10 000 000 ₽.")
    return value


def _parse_datetime(day_raw, time_raw, field_label, errors):
    try:
        return dt.datetime.strptime(
            f"{day_raw} {time_raw}", "%Y-%m-%d %H:%M"
        )
    except ValueError:
        errors.append(f"Проверьте поле «{field_label}».")
        return None


def validate_item_form(db, form, boats, services, exclude_id=None):
    errors = []
    kind = str(form.get("kind") or "booking").strip()
    if kind not in ITEM_KINDS:
        errors.append("Выберите тип рейса.")

    boat_names = {boat["name"] for boat in boats}
    boat = _normalise_text(form.get("boat"), 120)
    if boat not in boat_names:
        errors.append("Выберите катер.")

    service_names = {service["name"] for service in services}
    service_name = _normalise_text(form.get("service_name"), 180)
    if service_name not in service_names:
        errors.append("Выберите вид рейса.")

    day_raw = str(form.get("trip_date") or "").strip()
    start_time = str(form.get("start_time") or "").strip()
    end_time = str(form.get("end_time") or "").strip()
    starts_at = _parse_datetime(day_raw, start_time, "Начало", errors)
    ends_at = _parse_datetime(day_raw, end_time, "Окончание", errors)
    if starts_at and ends_at:
        duration = ends_at - starts_at
        if duration.total_seconds() < MIN_ITEM_MINUTES * 60:
            errors.append("Рейс должен длиться не меньше 30 минут.")
        if duration.total_seconds() > MAX_ITEM_HOURS * 3600:
            errors.append("Рейс не может длиться больше 12 часов.")

    customer_name = _normalise_text(form.get("customer_name"), 180)
    customer_phone = _normalise_text(form.get("customer_phone"), 40)
    note = str(form.get("note") or "").strip()[:2000]
    revenue = _parse_money(form.get("revenue"), errors)

    capacity = None
    participants_count = 0
    if kind == "booking":
        if not customer_name:
            errors.append("Для записи укажите имя клиента.")
    elif kind == "event":
        try:
            capacity = int(str(form.get("capacity") or "10").strip())
        except ValueError:
            capacity = 0
        try:
            participants_count = int(
                str(form.get("participants_count") or "0").strip()
            )
        except ValueError:
            participants_count = -1
        if not 1 <= capacity <= 100:
            errors.append("Вместимость события должна быть от 1 до 100 человек.")
        if not 0 <= participants_count <= max(capacity, 0):
            errors.append("Число участников не может превышать вместимость.")
        customer_name = ""
        customer_phone = ""

    raw_employee_ids = form.getlist("employee_id[]")
    raw_roles = form.getlist("role[]")
    employee_ids = []
    roles_by_employee = {}
    for index, raw_employee_id in enumerate(raw_employee_ids):
        try:
            employee_id = int(raw_employee_id)
        except (TypeError, ValueError):
            continue
        if employee_id in roles_by_employee:
            continue
        role = raw_roles[index] if index < len(raw_roles) else "guide_captain"
        if role not in CREW_ROLES:
            role = "guide_captain"
        employee_ids.append(employee_id)
        roles_by_employee[employee_id] = role
    if not employee_ids:
        errors.append("Назначьте хотя бы одного сотрудника.")

    eligible = {
        employee["id"]: employee
        for employee in repository.list_crew_employees(db)
    }
    missing = [employee_id for employee_id in employee_ids if employee_id not in eligible]
    if missing:
        errors.append("Один из выбранных сотрудников больше не доступен для рейсов.")

    assignments = [
        {
            "employee_id": employee_id,
            "employee_name": eligible[employee_id]["name"],
            "role": roles_by_employee[employee_id],
        }
        for employee_id in employee_ids
        if employee_id in eligible
    ]

    if starts_at and ends_at and starts_at < ends_at:
        starts_value = starts_at.strftime("%Y-%m-%d %H:%M")
        ends_value = ends_at.strftime("%Y-%m-%d %H:%M")
        employee_conflicts = repository.find_employee_conflicts(
            db, employee_ids, starts_value, ends_value, exclude_id
        )
        if employee_conflicts:
            names = sorted({row["employee_name"] for row in employee_conflicts})
            errors.append(
                "Уже заняты в это время: " + ", ".join(names) + "."
            )
        boat_conflicts = repository.find_boat_conflicts(
            db, boat, starts_value, ends_value, exclude_id
        ) if boat in boat_names else []
        if boat_conflicts:
            errors.append(f"Катер «{boat}» уже занят в это время.")

    data = {
        "kind": kind,
        "boat": boat,
        "service_name": service_name,
        "starts_at": starts_at.strftime("%Y-%m-%d %H:%M") if starts_at else "",
        "ends_at": ends_at.strftime("%Y-%m-%d %H:%M") if ends_at else "",
        "capacity": capacity,
        "participants_count": participants_count,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "revenue": revenue,
        "note": note,
    }
    return errors, data, assignments


def save_item(db, form, boats, services, item_id=None):
    if item_id is not None and repository.get_item(db, item_id) is None:
        return False, "Рейс не найден.", None
    errors, data, assignments = validate_item_form(
        db, form, boats, services, exclude_id=item_id
    )
    if errors:
        return False, " ".join(errors), data
    saved_id = repository.save_item(
        db, item_id, data, assignments, current_timestamp()
    )
    action = "обновлён" if item_id is not None else "создан"
    return True, f"Рейс {action}.", saved_id


def delete_item(db, item_id):
    item = repository.get_item(db, item_id)
    if item is None:
        return False, "Рейс не найден."
    if item["accounting_trip_id"] is not None:
        return False, (
            "Рейс уже связан с финансовым учётом. Сначала отвяжите его в разделе рейсов."
        )
    deleted = repository.soft_delete_item(db, item_id, current_timestamp())
    return (True, "Рейс удалён из расписания.") if deleted else (False, "Рейс не найден.")


def day_view(db, day, selected_employee, boats, avatar_url):
    crew = repository.list_crew_employees(db)
    selected_id = None
    if selected_employee not in (None, "", "all"):
        try:
            candidate_id = int(selected_employee)
        except ValueError:
            candidate_id = None
        if any(employee["id"] == candidate_id for employee in crew):
            selected_id = candidate_id

    visible_crew = (
        [employee for employee in crew if employee["id"] == selected_id]
        if selected_id is not None else crew
    )
    for employee in crew:
        employee["avatar_url"] = avatar_url(employee["name"])
        employee["position_label"] = " · ".join(employee["positions"])

    boat_indices = {boat["name"]: index for index, boat in enumerate(boats)}
    raw_items = repository.list_day_items(db, day.isoformat())
    items = []
    earliest = DEFAULT_DAY_START_HOUR * 60
    latest = DEFAULT_DAY_END_HOUR * 60
    for item in raw_items:
        starts = dt.datetime.strptime(item["starts_at"], "%Y-%m-%d %H:%M")
        ends = dt.datetime.strptime(item["ends_at"], "%Y-%m-%d %H:%M")
        start_minutes = starts.hour * 60 + starts.minute
        end_minutes = ends.hour * 60 + ends.minute
        earliest = min(earliest, (start_minutes // 60) * 60)
        latest = max(latest, int(math.ceil(end_minutes / 60.0)) * 60)
        item["start_time"] = starts.strftime("%H:%M")
        item["end_time"] = ends.strftime("%H:%M")
        item["trip_date"] = day.isoformat()
        item["kind_label"] = ITEM_KINDS[item["kind"]]
        item["assignment_ids"] = [
            assignment["employee_id"] for assignment in item["assignments"]
        ]
        item["assignment_roles"] = [
            assignment["role"] for assignment in item["assignments"]
        ]
        item["crew_label"] = ", ".join(
            assignment["employee_name"] for assignment in item["assignments"]
        )
        item["boat_tone"] = boat_indices.get(item["boat"], 0) % 3
        items.append(item)

    earliest = max(0, earliest)
    latest = min(24 * 60, latest)
    total_minutes = max(60, latest - earliest)
    px_per_minute = 1.25
    cards_by_employee = {employee["id"]: [] for employee in visible_crew}
    for item in items:
        for assignment in item["assignments"]:
            employee_id = assignment["employee_id"]
            if employee_id not in cards_by_employee:
                continue
            start_dt = dt.datetime.strptime(item["starts_at"], "%Y-%m-%d %H:%M")
            end_dt = dt.datetime.strptime(item["ends_at"], "%Y-%m-%d %H:%M")
            start_minutes = start_dt.hour * 60 + start_dt.minute
            end_minutes = end_dt.hour * 60 + end_dt.minute
            card = dict(item)
            card["assignment_role"] = CREW_ROLES.get(
                assignment["role"], assignment["role"]
            )
            card["top_px"] = round((start_minutes - earliest) * px_per_minute, 2)
            card["height_px"] = round(
                max(MIN_ITEM_MINUTES, end_minutes - start_minutes) * px_per_minute,
                2,
            )
            cards_by_employee[employee_id].append(card)

    hour_marks = []
    for minute in range(earliest, latest + 1, 60):
        hour_marks.append({
            "label": f"{minute // 60:02d}:00",
            "top_px": round((minute - earliest) * px_per_minute, 2),
        })

    now = dt.datetime.now()
    now_line_px = None
    if day == now.date():
        now_minutes = now.hour * 60 + now.minute
        if earliest <= now_minutes <= latest:
            now_line_px = round((now_minutes - earliest) * px_per_minute, 2)

    return {
        "crew": crew,
        "visible_crew": visible_crew,
        "items": items,
        "cards_by_employee": cards_by_employee,
        "selected_employee": str(selected_id) if selected_id is not None else "all",
        "hour_marks": hour_marks,
        "grid_height": round(total_minutes * px_per_minute, 2),
        "day_start_minutes": earliest,
        "px_per_minute": px_per_minute,
        "now_line_px": now_line_px,
    }
