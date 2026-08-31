"""Validation and view models for the internal trip schedule."""

import datetime as dt
import math
import secrets

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


def _normalise_phone_identity(phone):
    digits = "".join(character for character in phone if character.isdigit())
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def _validate_participants(db, form, capacity, errors):
    raw_client_ids = form.getlist("participant_client_id[]")
    raw_names = form.getlist("participant_name[]")
    raw_phones = form.getlist("participant_phone[]")
    row_count = max(len(raw_client_ids), len(raw_names), len(raw_phones))
    clients = repository.list_clients(db)
    clients_by_id = {client["id"]: client for client in clients}
    clients_by_phone = {}
    for client in clients:
        identity = _normalise_phone_identity(client["phone"])
        if identity:
            clients_by_phone.setdefault(identity, []).append(client)

    participants = []
    seen_phones = set()
    for index in range(row_count):
        raw_client_id = raw_client_ids[index] if index < len(raw_client_ids) else ""
        name = _normalise_text(
            raw_names[index] if index < len(raw_names) else "", 180
        )
        phone = _normalise_text(
            raw_phones[index] if index < len(raw_phones) else "", 40
        )
        if not raw_client_id and not name and not phone:
            continue

        row_label = f"Участник №{index + 1}"
        if not name:
            errors.append(f"{row_label}: укажите имя.")
        phone_identity = _normalise_phone_identity(phone)
        if len(phone_identity) < 7:
            errors.append(f"{row_label}: укажите корректный телефон.")
        if not name or len(phone_identity) < 7:
            continue
        if phone_identity in seen_phones:
            errors.append(f"{row_label}: этот клиент уже добавлен в рейс.")
            continue

        client = None
        if raw_client_id:
            try:
                client = clients_by_id.get(int(raw_client_id))
            except (TypeError, ValueError):
                client = None
            if client is None:
                errors.append(f"{row_label}: выбранный клиент больше недоступен.")
                continue
            if _normalise_phone_identity(client["phone"]) != phone_identity:
                errors.append(
                    f"{row_label}: телефон не совпадает с выбранным клиентом."
                )
                continue
        else:
            matches = clients_by_phone.get(phone_identity, [])
            if len(matches) > 1:
                errors.append(
                    f"{row_label}: в базе найдено несколько клиентов с этим телефоном."
                )
                continue
            client = matches[0] if matches else None

        if client is not None:
            name = client["client_name"]
            phone = client["phone"]
        participants.append({
            "client_id": client["id"] if client is not None else None,
            "client_name": name,
            "client_phone": phone,
            "client_token": secrets.token_urlsafe(16) if client is None else None,
        })
        seen_phones.add(phone_identity)

    if capacity and len(participants) > capacity:
        errors.append("Клиентов в рейсе больше, чем доступных мест.")
    return participants


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
    participants = []
    participants_count = 0
    if kind == "booking":
        if not customer_name:
            errors.append("Для записи укажите имя клиента.")
    elif kind == "event":
        try:
            capacity = int(str(form.get("capacity") or "10").strip())
        except ValueError:
            capacity = 0
        if not 1 <= capacity <= 100:
            errors.append("Вместимость события должна быть от 1 до 100 человек.")
        participants = _validate_participants(db, form, capacity, errors)
        participants_count = len(participants)
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
    return errors, data, assignments, participants


def save_item(db, form, boats, services, item_id=None):
    if item_id is not None and repository.get_item(db, item_id) is None:
        return False, "Рейс не найден.", None
    errors, data, assignments, participants = validate_item_form(
        db, form, boats, services, exclude_id=item_id
    )
    if errors:
        return False, " ".join(errors), data
    saved_id = repository.save_item(
        db, item_id, data, assignments, participants, current_timestamp()
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


def _normalise_hex_color(raw_color):
    value = str(raw_color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(character * 2 for character in value)
    if len(value) != 6 or any(
        character not in "0123456789abcdefABCDEF" for character in value
    ):
        return None
    return f"#{value.lower()}"


def _relative_luminance(color):
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    channels = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _card_ink_for(color):
    background = _relative_luminance(color)
    dark_ink = _relative_luminance("#102a36")
    white_ink = 1.0
    dark_contrast = (background + 0.05) / (dark_ink + 0.05)
    white_contrast = (white_ink + 0.05) / (background + 0.05)
    return "#102a36" if dark_contrast >= white_contrast else "#ffffff"


def _display_colors_by_boat(boat_colors):
    result = {}
    for raw_color, boat_name in boat_colors.items():
        color = _normalise_hex_color(raw_color)
        if color and boat_name not in result:
            result[boat_name] = color
    return result


def day_view(db, day, selected_employee, boats, boat_colors, avatar_url):
    crew = repository.list_crew_employees(db)
    clients = repository.list_clients(db)
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

    colors_by_boat = _display_colors_by_boat(boat_colors)
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
        item["boat_color"] = colors_by_boat.get(item["boat"], "#607d8b")
        item["boat_ink"] = _card_ink_for(item["boat_color"])
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
        "clients": clients,
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
