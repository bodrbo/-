"""Validation and view models for the internal trip schedule."""

import datetime as dt
import json
import math
import secrets
import sqlite3

from modules.clients.constants import CLIENT_CONTACT_METHODS
from modules.clients import yclients as yclients_clients
from modules.excursion_services import repository as service_repository
from modules.fleet import repository as fleet_repository
from modules.sales_channels import repository as sales_channel_repository

from . import repository
from .constants import (
    CREW_ROLES,
    DEFAULT_DAY_END_HOUR,
    DEFAULT_DAY_START_HOUR,
    ITEM_KINDS,
    MAX_ITEM_HOURS,
    MIN_ITEM_MINUTES,
    TIME_STEP_MINUTES,
)


MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)
MANUAL_PAYMENT_METHODS = {
    "cash": "наличными",
    "cashless": "безналично",
}


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def event_capacity_for_crew(vessel_capacity, crew_count, participants_count=0):
    """The hard rule requested in place of a free-typed «Вместимость»: an
    event card's guest seats are the boat's own passenger capacity (set
    once in Флот) minus however many crew this specific card actually has
    — e.g. 12 seats total - 2 crew (гид + капитан) = 10 guest seats.
    Never below the guests already booked (mirrors tripster_services' own
    bump-up rule) or below 1. Returns None when the boat has no configured
    capacity yet, so callers can fall back to the old manual entry."""
    if not vessel_capacity:
        return None
    return max(int(vessel_capacity) - int(crew_count or 0), int(participants_count or 0), 1)


def recompute_event_capacities_for_boat(db, boat, vessel_capacity, timestamp=None):
    """Re-derive every live event card's seat count on `boat` from Fleet's
    own passenger capacity, whenever that capacity is saved — so existing
    cards (made by hand, by the YCLIENTS backfill, by Tripster) stay in
    sync without a separate migration. A boat with no capacity configured
    is left untouched; its cards keep using the manual entry until Флот
    is filled in for it."""
    if not vessel_capacity:
        return 0
    timestamp = timestamp or current_timestamp()
    updated = 0
    for row in repository.list_active_event_items_for_boat(db, boat):
        new_capacity = event_capacity_for_crew(
            vessel_capacity, row["crew_count"], row["participants_count"]
        )
        if new_capacity != row["capacity"]:
            repository.update_item_capacity(db, row["id"], new_capacity, timestamp)
            updated += 1
    db.commit()
    return updated


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


def _parse_money(raw_value, errors, label="Стоимость"):
    raw_value = str(raw_value or "").strip().replace(" ", "").replace(",", ".")
    if not raw_value:
        return 0.0
    try:
        value = float(raw_value)
    except ValueError:
        errors.append(f"{label} должна быть числом.")
        return 0.0
    if value < 0 or value > 10_000_000:
        errors.append(f"{label} должна быть от 0 до 10 000 000 ₽.")
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


def _validate_booking_client(db, form, errors):
    name = _normalise_text(form.get("customer_name"), 180)
    phone = _normalise_text(form.get("customer_phone"), 40)
    raw_client_id = str(form.get("customer_client_id") or "").strip()
    contact_method = str(
        form.get("customer_preferred_contact_method") or ""
    ).strip()
    valid_contact_methods = {item["value"] for item in CLIENT_CONTACT_METHODS}
    if contact_method and contact_method not in valid_contact_methods:
        errors.append("Выберите корректный канал связи.")
        contact_method = ""
    raw_sales_channel = str(
        form.get("customer_sales_partner_id") or ""
    ).strip()
    sales_channel = ""
    if raw_sales_channel:
        sales_channel = sales_channel_repository.normalise_value(
            db, raw_sales_channel
        )
        if not sales_channel:
            errors.append("Выберите корректный канал продаж.")
    sales_partner_id = sales_channel_repository.partner_id(sales_channel)
    if not name:
        errors.append("Для записи укажите имя клиента.")
    phone_identity = _normalise_phone_identity(phone)
    if not name:
        return name, phone, None

    clients = repository.list_all_clients(db)
    client = None
    if raw_client_id:
        try:
            selected_id = int(raw_client_id)
        except ValueError:
            selected_id = None
        client = next(
            (candidate for candidate in clients if candidate["id"] == selected_id),
            None,
        )
        if client is None:
            errors.append("Выбранный клиент больше недоступен.")
            return name, phone, None
        stored_identity = _normalise_phone_identity(client["phone"])
        if stored_identity and phone_identity and stored_identity != phone_identity:
            errors.append("Телефон не совпадает с выбранным клиентом.")
            return name, phone, None
    else:
        matches = []
        if len(phone_identity) >= 7:
            matches = [
                candidate for candidate in clients
                if _normalise_phone_identity(candidate["phone"]) == phone_identity
            ]
        if len(matches) > 1:
            errors.append("В базе найдено несколько клиентов с этим телефоном.")
            return name, phone, None
        client = matches[0] if matches else None

    if client is not None:
        name = client["client_name"]
        phone = client["phone"]
    participant = {
        "client_id": client["id"] if client is not None else None,
        "client_name": name,
        "client_phone": phone,
        "guests_count": 1,
        "client_token": secrets.token_urlsafe(16) if client is None else None,
        "sales_partner_id": sales_partner_id,
        "sales_channel": sales_channel,
        "preferred_contact_method": contact_method,
    }
    return name, phone, participant


def _validate_participants(db, form, capacity, errors):
    raw_client_ids = form.getlist("participant_client_id[]")
    raw_names = form.getlist("participant_name[]")
    raw_phones = form.getlist("participant_phone[]")
    raw_guests = form.getlist("participant_guests[]")
    raw_prices = form.getlist("participant_price[]")
    raw_source_refs = form.getlist("participant_source_ref[]")
    raw_prepayments = form.getlist("participant_prepayment[]")
    raw_payment_dues = form.getlist("participant_payment_due[]")
    raw_sales_partner_ids = form.getlist("participant_sales_partner_id[]")
    raw_contact_methods = form.getlist("participant_preferred_contact_method[]")
    row_count = max(
        len(raw_client_ids), len(raw_names), len(raw_phones), len(raw_guests),
        len(raw_prices), len(raw_source_refs), len(raw_prepayments),
        len(raw_payment_dues), len(raw_sales_partner_ids),
        len(raw_contact_methods),
    )
    valid_contact_methods = {item["value"] for item in CLIENT_CONTACT_METHODS}
    # Search every identity by phone so a tuning client taking an excursion
    # is reused, while only excursion clients appear in the picker itself.
    clients = repository.list_all_clients(db)
    clients_by_id = {client["id"]: client for client in clients}
    clients_by_phone = {}
    for client in clients:
        identity = _normalise_phone_identity(client["phone"])
        if identity:
            clients_by_phone.setdefault(identity, []).append(client)

    participants = []
    seen_phones = set()
    seen_client_ids = set()
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
        price = (
            _parse_money(
                raw_prices[index] if index < len(raw_prices) else "",
                errors,
                f"{row_label}: стоимость",
            )
            if raw_prices else None
        )
        try:
            guests_count = int(
                str(raw_guests[index] if index < len(raw_guests) else "1").strip()
            )
        except (TypeError, ValueError):
            guests_count = 0
        if not 1 <= guests_count <= 100:
            errors.append(
                f"{row_label}: укажите количество гостей от 1 до 100."
            )
            guests_count = 1
        if not name:
            errors.append(f"{row_label}: укажите имя.")
        phone_identity = _normalise_phone_identity(phone)
        if not name:
            continue
        if phone_identity and phone_identity in seen_phones:
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
            stored_identity = _normalise_phone_identity(client["phone"])
            if stored_identity and phone_identity and stored_identity != phone_identity:
                errors.append(
                    f"{row_label}: телефон не совпадает с выбранным клиентом."
                )
                continue
        else:
            matches = clients_by_phone.get(phone_identity, []) if phone_identity else []
            if len(matches) > 1:
                errors.append(
                    f"{row_label}: в базе найдено несколько клиентов с этим телефоном."
                )
                continue
            client = matches[0] if matches else None

        if client is not None:
            if client["id"] in seen_client_ids:
                errors.append(f"{row_label}: этот клиент уже добавлен в рейс.")
                continue
            name = client["client_name"]
            phone = client["phone"]
        source_ref = (
            _normalise_text(raw_source_refs[index], 500)
            if index < len(raw_source_refs)
            and str(raw_source_refs[index]).startswith("orders:")
            else None
        )
        is_tripster = source_ref is not None
        raw_sales_channel = (
            raw_sales_partner_ids[index] if index < len(raw_sales_partner_ids) else ""
        ).strip()
        sales_channel = ""
        if raw_sales_channel:
            sales_channel = sales_channel_repository.normalise_value(
                db, raw_sales_channel
            )
            if not sales_channel:
                errors.append(f"{row_label}: неизвестный канал продаж.")
        sales_partner_id = sales_channel_repository.partner_id(sales_channel)
        contact_method = (
            raw_contact_methods[index] if index < len(raw_contact_methods) else ""
        ).strip()
        if contact_method and contact_method not in valid_contact_methods:
            errors.append(f"{row_label}: неизвестный канал связи.")
            contact_method = ""
        participants.append({
            "client_id": client["id"] if client is not None else None,
            "client_name": name,
            "client_phone": phone,
            "guests_count": guests_count,
            "price": price,
            "prepayment": _parse_money(
                raw_prepayments[index] if index < len(raw_prepayments) else "0",
                errors,
                f"{row_label}: предоплата Tripster",
            ) if is_tripster else 0.0,
            "payment_due": _parse_money(
                raw_payment_dues[index] if index < len(raw_payment_dues) else "0",
                errors,
                f"{row_label}: сумма к доплате",
            ) if is_tripster else None,
            "client_token": secrets.token_urlsafe(16) if client is None else None,
            "source": "tripster" if is_tripster else "internal",
            "source_ref": source_ref,
            "sales_partner_id": sales_partner_id,
            "sales_channel": sales_channel,
            "preferred_contact_method": contact_method,
        })
        if client is not None:
            seen_client_ids.add(client["id"])
        if phone_identity:
            seen_phones.add(phone_identity)

    total_guests = sum(participant["guests_count"] for participant in participants)
    if capacity and total_guests > capacity:
        errors.append("Гостей в рейсе больше, чем доступных мест.")
    return participants


def validate_item_form(db, form, boats, services, exclude_id=None):
    errors = []
    kind = str(form.get("kind") or "booking").strip()
    if kind not in ITEM_KINDS:
        errors.append("Выберите тип рейса.")

    services_by_id = {service["id"]: service for service in services}
    services_by_name = {
        service["name"].casefold(): service for service in services
    }
    try:
        service_id = int(str(form.get("service_id") or "").strip())
    except ValueError:
        service_id = None
    selected_service = services_by_id.get(service_id)
    if selected_service is None:
        legacy_service_name = _normalise_text(form.get("service_name"), 180)
        selected_service = services_by_name.get(legacy_service_name.casefold())
    if selected_service is None:
        errors.append("Выберите вид рейса.")
        service_id = None
        service_name = ""
    else:
        service_id = selected_service["id"]
        service_name = selected_service["name"]

    boats_by_name = {boat_item["name"]: boat_item for boat_item in boats}
    boat_names = set(boats_by_name)
    requires_boat = (
        selected_service is None
        or (selected_service.get("activity_type") or "boat") == "boat"
    )
    boat = _normalise_text(form.get("boat"), 120) if requires_boat else ""
    if requires_boat and boat not in boat_names:
        errors.append("Выберите катер.")

    day_raw = str(form.get("trip_date") or "").strip()
    start_time = str(form.get("start_time") or "").strip()
    end_time = str(form.get("end_time") or "").strip()
    starts_at = _parse_datetime(day_raw, start_time, "Начало", errors)
    ends_at = _parse_datetime(day_raw, end_time, "Окончание", errors)
    if starts_at and ends_at:
        duration = ends_at - starts_at
        if duration.total_seconds() < MIN_ITEM_MINUTES * 60:
            errors.append(
                f"Рейс должен длиться не меньше {MIN_ITEM_MINUTES} минут."
            )
        if duration.total_seconds() > MAX_ITEM_HOURS * 3600:
            errors.append("Рейс не может длиться больше 12 часов.")

    customer_name = _normalise_text(form.get("customer_name"), 180)
    customer_phone = _normalise_text(form.get("customer_phone"), 40)
    note = str(form.get("note") or "").strip()[:2000]
    legacy_revenue = _parse_money(form.get("revenue"), errors)

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

    capacity = None
    participants = []
    participants_count = 0
    booking_guests_count = None
    if kind == "booking":
        raw_booking_guests = str(form.get("guests_count") or "").strip()
        if raw_booking_guests:
            try:
                booking_guests_count = int(raw_booking_guests)
            except ValueError:
                errors.append("Количество гостей должно быть целым числом.")
            else:
                if not 1 <= booking_guests_count <= 1000:
                    errors.append("Количество гостей должно быть от 1 до 1000.")
        customer_name, customer_phone, participant = _validate_booking_client(
            db, form, errors
        )
        if participant is not None:
            participant["guests_count"] = booking_guests_count or 1
            raw_customer_price = form.get("customer_price")
            participant["price"] = (
                legacy_revenue
                if raw_customer_price is None
                else _parse_money(
                    raw_customer_price, errors, "Стоимость для клиента"
                )
            )
            source_ref = _normalise_text(form.get("customer_source_ref"), 500)
            if source_ref.startswith("orders:"):
                participant["source"] = "tripster"
                participant["source_ref"] = source_ref
                participant["prepayment"] = _parse_money(
                    form.get("customer_prepayment"), errors,
                    "Предоплата Tripster",
                )
                participant["payment_due"] = _parse_money(
                    form.get("customer_payment_due"), errors,
                    "Сумма к доплате",
                )
            else:
                participant["source"] = "internal"
                participant["source_ref"] = None
                participant["prepayment"] = 0.0
                participant["payment_due"] = participant["price"]
            participants = [participant]
    elif kind == "event":
        vessel_capacity = boats_by_name.get(boat, {}).get("capacity")
        capacity = event_capacity_for_crew(vessel_capacity, len(assignments))
        if capacity is None:
            try:
                capacity = int(str(form.get("capacity") or "10").strip())
            except ValueError:
                capacity = 0
            if not 1 <= capacity <= 100:
                errors.append("Вместимость события должна быть от 1 до 100 человек.")
        participants = _validate_participants(db, form, capacity, errors)
        if participants and all(
            participant["price"] is None for participant in participants
        ):
            total_guests = sum(
                participant["guests_count"] for participant in participants
            )
            allocated = 0.0
            for index, participant in enumerate(participants):
                if index == len(participants) - 1:
                    participant["price"] = round(legacy_revenue - allocated, 2)
                else:
                    share = round(
                        legacy_revenue * participant["guests_count"] / total_guests,
                        2,
                    )
                    participant["price"] = share
                    allocated += share
        else:
            for participant in participants:
                if participant["price"] is None:
                    participant["price"] = 0.0
        for participant in participants:
            if participant["source"] != "tripster":
                participant["prepayment"] = 0.0
                participant["payment_due"] = participant["price"]
        participants_count = sum(
            participant["guests_count"] for participant in participants
        )
        customer_name = ""
        customer_phone = ""

    revenue = round(sum(
        participant["price"] for participant in participants
    ), 2)

    if starts_at and ends_at and starts_at < ends_at:
        starts_value = starts_at.strftime("%Y-%m-%d %H:%M")
        ends_value = ends_at.strftime("%Y-%m-%d %H:%M")
        employee_conflicts = repository.find_employee_conflicts(
            db, employee_ids, starts_value, ends_value, exclude_id
        )
        if employee_conflicts:
            names = sorted({row["employee_name"] for row in employee_conflicts})
            conflict = employee_conflicts[0]
            errors.append(
                "Уже заняты в это время: " + ", ".join(names) + ". "
                f"Пересекается с «{conflict['service_name']}» "
                f"{conflict['starts_at'][11:16]}–{conflict['ends_at'][11:16]}."
            )
        boat_conflicts = repository.find_boat_conflicts(
            db, boat, starts_value, ends_value, exclude_id
        ) if boat in boat_names else []
        if boat_conflicts:
            conflict = boat_conflicts[0]
            errors.append(
                f"Катер «{boat}» уже занят в это время: «{conflict['service_name']}» "
                f"{conflict['starts_at'][11:16]}–{conflict['ends_at'][11:16]} "
                f"(карточка №{conflict['id']})."
            )

    data = {
        "kind": kind,
        "boat": boat,
        "service_id": service_id,
        "service_name": service_name,
        "starts_at": starts_at.strftime("%Y-%m-%d %H:%M") if starts_at else "",
        "ends_at": ends_at.strftime("%Y-%m-%d %H:%M") if ends_at else "",
        "capacity": capacity,
        "participants_count": participants_count,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "guests_count": booking_guests_count,
        "revenue": revenue,
        "note": note,
    }
    return errors, data, assignments, participants


def save_item(db, form, boats, services, item_id=None, keep_participants=False):
    if item_id is not None and repository.get_item(db, item_id) is None:
        return False, "Рейс не найден.", None
    errors, data, assignments, participants = validate_item_form(
        db, form, boats, services, exclude_id=item_id
    )
    if errors:
        return False, " ".join(errors), data
    saved_id = repository.save_item(
        db, item_id, data, assignments, participants, current_timestamp(),
        keep_participants=keep_participants,
    )
    action = "обновлён" if item_id is not None else "создан"
    return True, f"Рейс {action}.", saved_id


def _default_role_for_employee(employee):
    positions = set(employee.get("positions") or ())
    if "Гид-капитан" in positions or {"Гид", "Капитан"} <= positions:
        return "guide_captain"
    if "Капитан" in positions:
        return "captain"
    if "Гид" in positions:
        return "guide"
    return "guide_captain"


def move_item(
    db, item_id, start_time, source_employee_id, target_employee_id,
    update_linked_trip_time=None,
):
    """Move a trip on its current day and replace only the dragged
    assignment. update_linked_trip_time(db, trip_id, trip_date, trip_time),
    injected from app.py, keeps the trip this item was auto-closed into
    (if any) in sync — only date/time, since a move never changes the
    item's duration, so hours/pay in entries need no recomputation."""
    item = repository.get_item(db, item_id)
    if item is None:
        return False, "Рейс не найден.", None
    try:
        source_employee_id = int(source_employee_id)
        target_employee_id = int(target_employee_id)
    except (TypeError, ValueError):
        return False, "Не удалось определить сотрудника для переноса.", None

    eligible = {
        employee["id"]: employee
        for employee in repository.list_crew_employees(db)
    }
    target_employee = eligible.get(target_employee_id)
    if target_employee is None:
        return False, "Выбранный сотрудник недоступен для расписания.", None
    work_day = item["starts_at"][:10]
    if target_employee_id not in repository.list_day_crew_ids(db, work_day):
        return False, "Сначала добавьте сотрудника в состав на этот день.", None

    try:
        parsed_time = dt.datetime.strptime(str(start_time or ""), "%H:%M").time()
        old_start = dt.datetime.strptime(item["starts_at"], "%Y-%m-%d %H:%M")
        old_end = dt.datetime.strptime(item["ends_at"], "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return False, "Не удалось определить новое время рейса.", None
    if parsed_time.minute % TIME_STEP_MINUTES:
        return False, (
            f"Перетаскивание доступно с шагом {TIME_STEP_MINUTES} минут."
        ), None
    duration = old_end - old_start
    new_start = dt.datetime.combine(old_start.date(), parsed_time)
    new_end = new_start + duration
    if duration.total_seconds() < MIN_ITEM_MINUTES * 60:
        return False, "Некорректная продолжительность рейса.", None
    if new_end.date() != new_start.date():
        return False, "Рейс нельзя перетащить за границы выбранного дня.", None

    assignments = [dict(row) for row in repository.list_assignments(db, item_id)]
    assignments_by_employee = {
        assignment["employee_id"]: assignment for assignment in assignments
    }
    if source_employee_id == 0:
        if assignments:
            return False, "Исходный сотрудник рейса изменился. Обновите страницу.", None
        source_role = _default_role_for_employee(target_employee)
    else:
        source_assignment = assignments_by_employee.get(source_employee_id)
        if source_assignment is None:
            return False, "Назначение рейса изменилось. Обновите страницу.", None
        source_role = source_assignment["role"]
    if (
        target_employee_id != source_employee_id
        and target_employee_id in assignments_by_employee
    ):
        return False, "Этот сотрудник уже назначен на рейс.", None

    resulting_employee_ids = [
        target_employee_id
        if assignment["employee_id"] == source_employee_id
        else assignment["employee_id"]
        for assignment in assignments
    ]
    if source_employee_id == 0:
        resulting_employee_ids.append(target_employee_id)
    starts_value = new_start.strftime("%Y-%m-%d %H:%M")
    ends_value = new_end.strftime("%Y-%m-%d %H:%M")
    employee_conflicts = repository.find_employee_conflicts(
        db, resulting_employee_ids, starts_value, ends_value, item_id
    )
    if employee_conflicts:
        names = sorted({row["employee_name"] for row in employee_conflicts})
        return False, "Уже заняты в это время: " + ", ".join(names) + ".", None
    if item["boat"] and repository.find_boat_conflicts(
        db, item["boat"], starts_value, ends_value, item_id
    ):
        return False, f"Катер «{item['boat']}» уже занят в это время.", None

    if (
        starts_value == item["starts_at"]
        and source_employee_id == target_employee_id
    ):
        return True, "Положение рейса не изменилось.", {
            "starts_at": starts_value,
            "ends_at": ends_value,
            "assignments": assignments,
        }
    moved = repository.move_item(
        db, item_id, starts_value, ends_value, source_employee_id,
        target_employee, source_role, current_timestamp(),
    )
    if not moved:
        return False, "Рейс изменился во время переноса. Обновите страницу.", None
    if item["payroll_closed_at"]:
        repository.move_item_payroll(db, item_id, new_start.strftime("%Y-%m-%d"))
    if item["accounting_trip_id"] is not None and update_linked_trip_time is not None:
        update_linked_trip_time(
            db, item["accounting_trip_id"], new_start.strftime("%Y-%m-%d"),
            new_start.strftime("%H:%M"),
        )
    return True, "Рейс перенесён.", {
        "starts_at": starts_value,
        "ends_at": ends_value,
        "assignments": [
            {
                "employee_id": row["employee_id"],
                "employee_name": row["employee_name"],
                "role": row["role"],
            }
            for row in repository.list_assignments(db, item_id)
        ],
    }


def delete_item(db, item_id, delete_linked_trip=None):
    """delete_linked_trip(db, trip_id), injected from app.py, cascades the
    deletion to the trip this item was auto-closed into (payroll entries,
    trip_labor, trip_expenses — see app.py::_delete_trip_data) instead of
    refusing to delete an already-closed item outright."""
    item = repository.get_item(db, item_id)
    if item is None:
        return False, "Рейс не найден."
    had_linked_trip = item["accounting_trip_id"] is not None
    if item["payroll_closed_at"]:
        # A boat-less item already paid out through payroll: take its pay back.
        repository.delete_item_payroll(db, item_id)
        had_linked_trip = True
    if item["accounting_trip_id"] is not None:
        if delete_linked_trip is None:
            return False, (
                "Рейс уже связан с финансовым учётом. Сначала отвяжите его в разделе рейсов."
            )
        delete_linked_trip(db, item["accounting_trip_id"])
    deleted = repository.soft_delete_item(db, item_id, current_timestamp())
    if not deleted:
        return False, "Рейс не найден."
    message = (
        "Рейс удалён из расписания вместе со связанными записями "
        "(зарплата, доля инвестора)." if had_linked_trip
        else "Рейс удалён из расписания."
    )
    return True, message


_ROLE_SUFFIXES = (
    (" гид/капитан", "guide_captain"),
    (" гид-капитан", "guide_captain"),
)


def _split_role_from_work_type(work_type):
    """A historical entries.work_type is a WORK_TYPES name, optionally
    suffixed to mark the combined guide+captain rate (see
    modules.payroll_rates — the suffix convention predates that module and
    still lives in old entries rows). Returns (base_service_name, role)."""
    text = str(work_type or "").strip()
    lowered = text.lower()
    for suffix, role in _ROLE_SUFFIXES:
        if lowered.endswith(suffix):
            return text[: -len(suffix)].strip(), role
    return text, "captain"


def create_item_from_trip(db, trip):
    """Reconstruct a schedule_items card (+ assignments) for a trip that
    was never entered through the schedule module — the historical-
    backfill counterpart to auto_close_schedule_items, which links a
    schedule card forward into a trip; this links a trip backward into a
    schedule card. Used by scripts/backfill_yclients_trips.py so old
    YCLIENTS trips show up on the calendar, not just in /trips.

    Deliberately conservative: skips (does not guess) whenever the
    source data is ambiguous — no crew rows, a work_type that doesn't
    match any excursion_services entry, an employee name that doesn't
    match anyone in the employees table, or the boat/time slot is
    already occupied by a card from some other source (Tripster sync,
    a manual entry) — rather than creating a card with missing/wrong
    data or a silent boat-double-booking. Returns (success, message,
    item_id)."""
    labor_rows = repository.get_trip_labor(db, trip["id"])
    if not labor_rows:
        return False, "Нет данных о сотрудниках рейса — карточка не создана.", None

    base_service_name, _role = _split_role_from_work_type(labor_rows[0]["work_type"])
    service = service_repository.get_service_by_name(db, base_service_name)
    if service is None:
        return False, (
            f"Вид рейса «{base_service_name}» не сопоставлен ни с одной "
            "услугой — карточка не создана."
        ), None

    assignments = []
    hours = 0.0
    for row in labor_rows:
        employee_id = repository.get_employee_id_by_name(db, row["employee"])
        if employee_id is None:
            return False, (
                f"Сотрудник «{row['employee']}» не найден в справочнике "
                "сотрудников — карточка не создана."
            ), None
        _base, role = _split_role_from_work_type(row["work_type"])
        assignments.append({
            "employee_id": employee_id,
            "employee_name": row["employee"],
            "role": role,
        })
        hours = max(hours, float(row["quantity"] or 0))

    if hours <= 0:
        return False, "Не удалось определить длительность рейса — карточка не создана.", None
    if not trip["trip_date"]:
        return False, "У рейса не указана дата — карточка не создана.", None

    starts = dt.datetime.strptime(
        f"{trip['trip_date']} {trip['trip_time'] or '00:00'}", "%Y-%m-%d %H:%M"
    )
    ends = starts + dt.timedelta(hours=hours)
    starts_value = starts.strftime("%Y-%m-%d %H:%M")
    ends_value = ends.strftime("%Y-%m-%d %H:%M")

    # A card for this same physical trip can already exist from a source
    # this function never looks at — e.g. Tripster's own sync (source=
    # 'tripster'), whose card gets its boat/crew filled in by hand and so
    # never carries an accounting_trip_id for list_trips_without_schedule_card
    # to notice. Reuse the exact conflict check validate_item_form runs on
    # every manual save instead of blindly creating a second card that then
    # makes editing either one fail with a boat/crew conflict.
    boat_conflicts = repository.find_boat_conflicts(db, trip["boat"], starts_value, ends_value)
    if boat_conflicts:
        conflict = boat_conflicts[0]
        return False, (
            f"Катер «{trip['boat']}» на это время уже занят карточкой "
            f"«{conflict['service_name']}» №{conflict['id']} "
            f"({conflict['starts_at'][11:16]}–{conflict['ends_at'][11:16]}) — "
            "похоже, этот рейс уже есть в расписании из другого источника. "
            "Карточка не создана."
        ), None

    kind = "booking" if "аренда" in base_service_name.lower() else "event"

    capacity = None
    if kind == "event":
        vessel = fleet_repository.get_vessel_by_name(db, trip["boat"])
        vessel_capacity = vessel["capacity"] if vessel else None
        capacity = event_capacity_for_crew(vessel_capacity, len(assignments))

    data = {
        "kind": kind,
        "boat": trip["boat"],
        "service_id": service["id"],
        "service_name": service["name"],
        "starts_at": starts_value,
        "ends_at": ends_value,
        "capacity": capacity,
        "participants_count": 0,
        "customer_name": "",
        "customer_phone": "",
        "revenue": trip["revenue"],
        "note": "Восстановлено из YCLIENTS при переходе на внутреннее расписание.",
    }
    timestamp = current_timestamp()
    item_id = repository.save_item(db, None, data, assignments, [], timestamp)
    repository.set_accounting_trip_id(db, item_id, trip["id"], timestamp)
    db.commit()
    return True, "Карточка создана.", item_id


def attach_participant_from_record(db, item_id, record, timestamp):
    """Populate one schedule_participants row on a reconstructed card
    straight from the raw YCLIENTS record's own embedded `client` — the
    backfill script's counterpart to create_item_from_trip, so a card
    (especially a future one) doesn't sit with no customer until someone
    fills it in by hand. Deliberately keeps `trips` untouched: this reads
    the same already-fetched records the trip import used, and writes
    only to the schedule module's own tables.

    Reuses modules.clients.yclients.import_clients for client upsert (the
    exact dedup-by-yclients_id-then-phone logic the client directory sync
    already relies on) and repository.add_external_participant for the
    insert, keyed by the YCLIENTS record id via the same source/source_ref
    idempotency the public booking API uses — so re-running the backfill
    never double-adds an attendee. Skips (does not guess) a record with no
    usable client identity. Returns (success, message)."""
    remote_client = record.get("client") or {}
    try:
        remote_client_id = int(remote_client.get("id"))
    except (TypeError, ValueError):
        remote_client_id = None
    if remote_client_id is None:
        return False, "В записи YCLIENTS нет данных о клиенте."

    record_id = record.get("id")
    if record_id in (None, ""):
        return False, "У записи YCLIENTS нет идентификатора."
    source_ref = f"record:{record_id}"

    already = db.execute(
        "SELECT 1 FROM schedule_participants WHERE source = 'yclients' "
        "AND source_ref = ?",
        (source_ref,),
    ).fetchone()
    if already is not None:
        return True, "Клиент уже добавлен."

    yclients_clients.import_clients(db, [remote_client], timestamp)
    client = db.execute(
        "SELECT id, client_name, phone FROM clients WHERE yclients_client_id = ?",
        (remote_client_id,),
    ).fetchone()
    if client is None:
        return False, "Не удалось сопоставить клиента YCLIENTS с локальной записью."

    guests_count = int(record.get("clients_count") or 1)
    price = sum(float(s.get("cost") or 0) for s in (record.get("services") or []))

    try:
        repository.add_external_participant(
            db, item_id, client["id"], client["client_name"], client["phone"],
            guests_count, price, timestamp,
            source="yclients", source_ref=source_ref,
        )
    except sqlite3.IntegrityError:
        db.rollback()
        return True, "Клиент уже добавлен."
    repository.fill_blank_customer_contact(
        db, item_id, client["client_name"], client["phone"], timestamp
    )
    db.commit()
    return True, "Клиент добавлен."


AUTO_CLOSE_GRACE_MINUTES = 20


def auto_close_schedule_items(db, create_trip, get_role_rate, apply_minimum_shift=None, now=None):
    """Turn every schedule item whose trip is over into a real trips row —
    the internal-schedule replacement for the YCLIENTS import pipeline.
    Called on a timer (see routes.cron_close_schedule_items), no admin
    confirmation step.

    `create_trip(db, payload, needs_review=False)` is injected from app.py
    (wraps _payload_to_form/_process_trip_form/_insert_trip, the exact path
    used by the trip editor and the YCLIENTS import)
    — returns (errors, trip_id). `get_role_rate(db, role)` is injected from
    modules.payroll_rates (Зарплаты -> Ставки -> Ставки экскурсий) — one
    ₽/hour rate per crew role, not per trip type. `apply_minimum_shift(db,
    start_date, end_date)`, also injected, tops up any crew member's day to
    the guaranteed minimum shift rate once the items in that range are
    closed (see app.py::_schedule_payroll_days, which replaces the YCLIENTS
    staff-schedule call this used to depend on).

    Idempotent: an item is only ever picked up once
    (accounting_trip_id IS NULL), and gets accounting_trip_id set in the
    same pass it's inserted, so a crashed/retried run never double-books
    it. An item that fails validation (e.g. no crew assigned) is left
    alone — it stays a candidate for the next run rather than being
    silently dropped."""
    now = now or dt.datetime.now()
    cutoff = (now - dt.timedelta(minutes=AUTO_CLOSE_GRACE_MINUTES)).strftime("%Y-%m-%d %H:%M")
    timestamp = current_timestamp()

    stats = {"closed": 0, "needs_review": 0, "skipped": 0, "skipped_details": []}
    closed_dates = set()

    for item in repository.list_items_ready_to_close(db, cutoff):
        assignments = repository.list_assignments(db, item["id"])
        if not assignments:
            stats["skipped"] += 1
            stats["skipped_details"].append(
                f"№{item['id']} {item['starts_at']} «{item['service_name']}»: нет назначенного экипажа"
            )
            continue

        starts = dt.datetime.strptime(item["starts_at"], "%Y-%m-%d %H:%M")
        ends = dt.datetime.strptime(item["ends_at"], "%Y-%m-%d %H:%M")
        hours = max((ends - starts).total_seconds() / 3600, 0)

        needs_review = False
        labor_items = []
        for assignment in assignments:
            role = assignment["role"]
            rate = get_role_rate(db, role)
            if not rate and role == "guide":
                # No "guide" rate configured yet at Зарплаты -> Ставки ->
                # Ставки экскурсий — falling back to the captain rate keeps
                # this crew member paid rather than silently zeroed, at the
                # cost of possibly being wrong; needs_review makes that
                # visible on /trips.
                rate = get_role_rate(db, "captain")
                needs_review = True
            if not rate:
                rate = 0
                needs_review = True
            labor_items.append({
                "employee": assignment["employee_name"],
                "work_type": item["service_name"],
                "quantity": round(hours, 2),
                "rate": rate,
            })

        if not item["boat"]:
            # City excursion: no boat -> no trips row (and no investor
            # split), but the crew must still be paid.
            repository.close_item_payroll_only(
                db, item["id"], item["starts_at"][:10], labor_items, timestamp
            )
            db.commit()
            stats["closed"] += 1
            if needs_review:
                stats["needs_review"] += 1
            closed_dates.add(item["starts_at"][:10])
            continue

        payload = {
            "boat": item["boat"],
            "trip_date": item["starts_at"][:10],
            "trip_time": item["starts_at"][11:16],
            "revenue": item["revenue"],
            "sale_channel": (
                "aggregator" if repository.item_has_sales_partner(db, item["id"]) else "direct"
            ),
            "commission_pct": 0,
            "fuel_cost": 0,
            "mooring_cost": 0,
            "labor_items": labor_items,
        }

        errors, trip_id = create_trip(db, payload, needs_review=needs_review)
        if errors:
            stats["skipped"] += 1
            stats["skipped_details"].append(
                f"№{item['id']} {item['starts_at']} «{item['service_name']}», "
                f"катер «{item['boat']}»: {'; '.join(errors)}"
            )
            continue

        repository.set_accounting_trip_id(db, item["id"], trip_id, timestamp)
        db.commit()
        stats["closed"] += 1
        if needs_review:
            stats["needs_review"] += 1
        closed_dates.add(item["starts_at"][:10])

    if closed_dates and apply_minimum_shift:
        apply_minimum_shift(db, min(closed_dates), max(closed_dates))

    return stats


def add_participant_addon(db, item_id, client_id, product_id, raw_quantity):
    if repository.get_item(db, item_id) is None:
        return False, "Рейс не найден.", None
    if not repository.is_participant_of_item(db, item_id, client_id):
        return False, "Этот клиент не участвует в рейсе.", None
    product = service_repository.get_addon_product(db, product_id)
    if product is None:
        return False, "Товар не найден.", None
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError):
        quantity = 0
    if quantity <= 0:
        return False, "Количество должно быть больше нуля.", None
    repository.add_participant_addon(
        db, item_id, client_id, product_id, quantity, current_timestamp()
    )
    return True, f"«{product['name']}» добавлен.", product


def remove_participant_addon(db, item_id, addon_id):
    if not repository.remove_participant_addon(db, addon_id, item_id):
        return False, "Товар не найден."
    return True, "Товар удалён."


def create_participant_payment(
    db, item_id, participant_id, raw_amount, yookassa_request, vat_code,
    phone_normalizer, return_url,
):
    """Create a ЮKassa payment link covering some amount owed by one
    participant (full remaining balance, a percentage of it, or a manual
    figure — all resolved client-side into one final `raw_amount`)."""
    item = repository.get_item(db, item_id)
    if item is None:
        return False, "Рейс не найден.", None
    participant = repository.get_participant(db, participant_id, item_id)
    if participant is None:
        return False, "Клиент не найден в этом рейсе.", None
    errors = []
    amount = _parse_money(raw_amount, errors, "Сумма")
    if not errors and amount <= 0:
        errors.append("Сумма должна быть больше нуля.")
    if errors:
        return False, " ".join(errors), None

    description = f"{item['service_name']} — {participant['client_name']}"[:128]
    receipt = {
        "items": [
            {
                "description": description,
                "quantity": 1,
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "vat_code": vat_code,
                "measure": "piece",
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }
        ],
    }
    phone = phone_normalizer(participant["client_phone"]) if participant["client_phone"] else ""
    if phone:
        receipt["customer"] = {"phone": phone}
    body = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "description": description,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "metadata": {
            "schedule_item_id": str(item_id),
            "schedule_participant_id": str(participant_id),
        },
        "receipt": receipt,
    }
    try:
        remote = yookassa_request(
            "POST", "/payments", json_body=body, idempotence_key=secrets.token_hex(16)
        )
    except Exception as error:
        return False, f"Не удалось создать ссылку на оплату: {error}", None
    timestamp = current_timestamp()
    payment_id = repository.create_yookassa_payment_row(
        db, item_id, participant_id, remote["id"], amount,
        remote.get("status", "pending"), remote["confirmation"]["confirmation_url"],
        timestamp,
    )
    return True, "Ссылка на оплату создана.", payment_id


def sync_participant_payment(db, record, yookassa_request):
    """Refresh a stored payment's status from the API and, the first time
    it turns succeeded, reduce the participant's remaining balance — the
    guard on `applied` keeps a webhook and a manual "Проверить" click (or
    a redelivered webhook) from double-counting the same payment."""
    remote = yookassa_request("GET", f"/payments/{record['yookassa_payment_id']}")
    status = remote.get("status", record["status"])
    timestamp = current_timestamp()
    repository.update_yookassa_payment_status(db, record["id"], status, timestamp)
    if status == "succeeded" and not record["applied"]:
        repository.apply_yookassa_payment(
            db, record["id"], record["participant_id"], record["amount"]
        )
    db.commit()
    if status == "succeeded":
        # The cash desk registers the receipt a moment after the payment, so
        # this often only finds "pending" — the cron / "Проверить чек" button
        # pick it up later.
        refresh_payment_receipt(db, record, yookassa_request)
    return status


def _pick_payment_receipt(items):
    """The income receipt of a payment: a registered one wins over a
    pending one; refunds and cancelled receipts are ignored."""
    candidates = [
        item for item in items
        if item.get("type", "payment") == "payment" and item.get("status") != "canceled"
    ]
    candidates.sort(key=lambda item: item.get("status") == "succeeded", reverse=True)
    return candidates[0] if candidates else None


def refresh_payment_receipt(db, record, yookassa_request):
    """Reads the fiscal receipt of a paid link from ЮKassa (its receipts API
    lists the receipts issued for a payment, including those the connected
    ModulKassa cash desk registered) and stores the fiscal data — ФН, ФД,
    ФП and the registration time — the PDF is built from. Best-effort:
    returns the stored receipt status ('' when nothing was found or the API
    failed) and never raises."""
    try:
        listing = yookassa_request(
            "GET", "/receipts", params={"payment_id": record["yookassa_payment_id"]}
        )
    except Exception:
        return record["receipt_status"] if "receipt_status" in record.keys() else ""
    receipt = _pick_payment_receipt(listing.get("items") or [])
    timestamp = current_timestamp()
    if receipt is None:
        repository.save_yookassa_receipt(db, record["id"], "", None, timestamp)
        db.commit()
        return ""
    has_fiscal_data = all(
        receipt.get(key)
        for key in ("fiscal_document_number", "fiscal_storage_number", "fiscal_attribute", "registered_at")
    )
    if receipt.get("status") == "succeeded" and has_fiscal_data:
        stored = {
            key: receipt.get(key)
            for key in (
                "id", "type", "registered_at", "fiscal_document_number",
                "fiscal_storage_number", "fiscal_attribute", "fiscal_provider_id",
            )
        }
        repository.save_yookassa_receipt(
            db, record["id"], "succeeded", json.dumps(stored, ensure_ascii=False), timestamp
        )
        status = "succeeded"
    else:
        repository.save_yookassa_receipt(db, record["id"], "pending", None, timestamp)
        status = "pending"
    db.commit()
    return status


def sync_pending_receipts(db, yookassa_request, days=7):
    since = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    found = 0
    rows = repository.list_payments_needing_receipt(db, since)
    for record in rows:
        if refresh_payment_receipt(db, record, yookassa_request) == "succeeded":
            found += 1
    return len(rows), found


def sync_participant_payment_by_remote_id(db, yookassa_payment_id, yookassa_request):
    record = repository.get_yookassa_payment_by_remote_id(db, yookassa_payment_id)
    if record is None:
        return None
    return sync_participant_payment(db, record, yookassa_request)


def delete_participant_payment(db, record, yookassa_request):
    if record["status"] == "succeeded":
        return False, "Нельзя удалить ссылку с успешной оплатой."
    if record["status"] == "waiting_for_capture":
        try:
            yookassa_request(
                "POST", f"/payments/{record['yookassa_payment_id']}/cancel",
                json_body={}, idempotence_key=secrets.token_hex(16),
            )
        except Exception as error:
            return False, f"Не удалось отменить оплату в ЮKassa: {error}"
    repository.delete_yookassa_payment_row(db, record["id"], record["participant_id"])
    return True, "Ссылка на оплату удалена."


def _participant_amount_due(participant):
    addons_total = sum(
        float(addon["quantity"] or 0) * float(addon["unit_price"] or 0)
        for addon in participant.get("addons", [])
    )
    due = (
        float(participant.get("payment_due") or 0)
        + addons_total
        - float(participant.get("paid_online") or 0)
        - float(participant.get("paid_manual") or 0)
    )
    return max(0.0, round(due, 2))


def create_manual_payment(
    db, item_id, participant_id, raw_amount, payment_method,
):
    """Record money received outside ЮKassa and keep an auditable ledger."""
    if repository.get_item(db, item_id) is None:
        return False, "Рейс не найден.", None
    if repository.get_participant(db, participant_id, item_id) is None:
        return False, "Клиент не найден в этом рейсе.", None
    method = str(payment_method or "").strip()
    if method not in MANUAL_PAYMENT_METHODS:
        return False, "Выберите способ оплаты: наличные или безналичные.", None
    errors = []
    amount = _parse_money(raw_amount, errors, "Сумма")
    if not errors and amount <= 0:
        errors.append("Сумма должна быть больше нуля.")
    participant = next(
        (
            row for row in repository.list_item_participants_with_addons(db, item_id)
            if row["id"] == participant_id
        ),
        None,
    )
    if participant is None:
        return False, "Клиент не найден в этом рейсе.", None
    amount_due = _participant_amount_due(participant)
    if not errors and amount_due <= 0:
        errors.append("У клиента нет суммы к доплате.")
    if not errors and amount > amount_due:
        errors.append(
            f"Сумма платежа не может превышать остаток {amount_due:.2f} ₽."
        )
    if errors:
        return False, " ".join(errors), None
    payment_id = repository.create_manual_payment_row(
        db, item_id, participant_id, amount, method, current_timestamp()
    )
    return (
        True,
        f"Оплата {amount:.2f} ₽ записана {MANUAL_PAYMENT_METHODS[method]}.",
        payment_id,
    )


def remove_manual_payment(db, item_id, participant_id, payment_id):
    if repository.get_manual_payment(
        db, payment_id, participant_id, item_id
    ) is None:
        return False, "Ручной платёж не найден."
    repository.delete_manual_payment(db, payment_id, participant_id, item_id)
    return True, "Ручной платёж удалён."


def _resolve_client_for_participant(db, raw_client_id, phone, errors):
    """Same matching rules as the whole-trip form's participant rows
    (_validate_participants) — reuse by id when the picker matched one,
    else by an unambiguous phone — just kept separate since this is a
    single client, not an array of rows."""
    identity = _normalise_phone_identity(phone)
    if raw_client_id:
        try:
            candidate_id = int(raw_client_id)
        except (TypeError, ValueError):
            candidate_id = None
        client = next(
            (c for c in repository.list_all_clients(db) if c["id"] == candidate_id),
            None,
        )
        if client is None:
            errors.append("Выбранный клиент больше недоступен.")
        return client
    if identity:
        matches = [
            c for c in repository.list_all_clients(db)
            if _normalise_phone_identity(c["phone"]) == identity
        ]
        if len(matches) > 1:
            errors.append("В базе найдено несколько клиентов с этим телефоном.")
            return None
        return matches[0] if matches else None
    return None


def _validate_sales_partner_id(db, form, errors):
    raw_value = str(form.get("sales_partner_id") or "").strip()
    if not raw_value:
        return None, ""
    sales_channel = sales_channel_repository.normalise_value(db, raw_value)
    if not sales_channel:
        errors.append("Неизвестный канал продаж.")
        return None, ""
    return sales_channel_repository.partner_id(sales_channel), sales_channel


def add_participant_quick(db, item_id, form):
    """Quick-add form on an already-saved event — just name/phone/guests;
    price is computed from the trip's own service rate, same as the
    whole-trip form does automatically when nothing's been typed in."""
    item = repository.get_item(db, item_id)
    if item is None:
        return False, "Рейс не найден.", None
    errors = []
    name = _normalise_text(form.get("client_name"), 180)
    phone = _normalise_text(form.get("client_phone"), 40)
    raw_client_id = str(form.get("client_id") or "").strip()
    if not name:
        errors.append("Укажите имя клиента.")
    try:
        guests_count = int(str(form.get("guests_count") or "1").strip())
    except (TypeError, ValueError):
        guests_count = 0
    if not 1 <= guests_count <= 100:
        errors.append("Количество гостей должно быть от 1 до 100.")
    client = _resolve_client_for_participant(db, raw_client_id, phone, errors)
    if errors:
        return False, " ".join(errors), None
    if client is not None:
        name = client["client_name"]
        phone = client["phone"]
    price = 0.0
    if item["service_id"] is not None:
        service = service_repository.get_service(db, item["service_id"])
        if service is not None:
            price = round((service["price"] or 0) * guests_count, 2)
    participant_id = repository.add_participant(
        db, item_id, client["id"] if client is not None else None,
        name, phone, guests_count, price, current_timestamp(),
    )
    return True, f"«{name}» добавлен в рейс.", participant_id


def edit_participant(db, item_id, participant_id, form):
    errors = []
    name = _normalise_text(form.get("client_name"), 180)
    phone = _normalise_text(form.get("client_phone"), 40)
    if not name:
        errors.append("Укажите имя клиента.")
    try:
        guests_count = int(str(form.get("guests_count") or "1").strip())
    except (TypeError, ValueError):
        guests_count = 0
    if not 1 <= guests_count <= 100:
        errors.append("Количество гостей должно быть от 1 до 100.")
    price = _parse_money(form.get("price"), errors, "Стоимость")
    sales_partner_id, sales_channel = _validate_sales_partner_id(db, form, errors)
    contact_method = str(form.get("preferred_contact_method") or "").strip()
    allowed_contact_methods = {item["value"] for item in CLIENT_CONTACT_METHODS}
    if contact_method and contact_method not in allowed_contact_methods:
        errors.append("Некорректный канал связи.")
    if errors:
        return False, " ".join(errors)
    updated = repository.update_participant(
        db, participant_id, item_id,
        {
            "client_name": name, "client_phone": phone, "guests_count": guests_count,
            "price": price, "sales_partner_id": sales_partner_id,
            "sales_channel": sales_channel,
            "preferred_contact_method": contact_method,
        },
        current_timestamp(),
    )
    if not updated:
        return False, "Клиент не найден в этом рейсе."
    return True, "Изменения сохранены."


def remove_participant(db, item_id, participant_id):
    if not repository.delete_participant(db, participant_id, item_id, current_timestamp()):
        return False, "Клиент не найден в этом рейсе."
    return True, "Клиент удалён из рейса."


def add_day_crew_member(db, day, employee_id):
    eligible = {
        employee["id"]: employee for employee in repository.list_crew_employees(db)
    }
    if employee_id not in eligible:
        return False, "Сотрудник не найден или его должность не относится к экипажу."
    added = repository.add_day_crew_member(
        db, day.isoformat(), employee_id, current_timestamp()
    )
    if not added:
        return True, f"{eligible[employee_id]['name']} уже добавлен в расписание."
    return True, f"{eligible[employee_id]['name']} добавлен в расписание."


def remove_day_crew_member(db, day, employee_id):
    eligible = {
        employee["id"]: employee for employee in repository.list_crew_employees(db)
    }
    assigned_ids = repository.list_day_assignment_employee_ids(
        db, day.isoformat()
    )
    employee_name = eligible.get(employee_id, {}).get("name", "Сотрудник")
    if employee_id in assigned_ids:
        return False, (
            f"Нельзя убрать {employee_name}: на эту дату уже назначен рейс. "
            "Сначала переназначьте или удалите рейс."
        )
    removed = repository.remove_day_crew_member(
        db, day.isoformat(), employee_id
    )
    if not removed:
        return False, "Сотрудник уже отсутствует в расписании на эту дату."
    return True, f"{employee_name} убран из расписания."


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
    for key, value in boat_colors.items():
        # Current fleet settings use the natural ``boat -> colour`` shape.
        # Keep accepting the legacy YCLIENTS ``colour -> boat`` dictionary so
        # integrations and old tests remain compatible.
        value_color = _normalise_hex_color(value)
        if value_color:
            boat_name, color = key, value_color
        else:
            boat_name, color = value, _normalise_hex_color(key)
        if color and boat_name not in result:
            result[boat_name] = color
    return result


def readonly_item_details(items):
    """Return the crew manifest without internal notes or price breakdowns.

    Captains need the final amount still due from each guest group at boarding,
    but not the underlying price, prepayment, payment ledger, or trip revenue.
    """
    result = []
    for item in items:
        participants = [
            {
                "client_name": participant["client_name"],
                "client_phone": participant["client_phone"],
                "guests_count": participant["guests_count"],
                "amount_due": _participant_amount_due(participant),
            }
            for participant in item["participants"]
        ]
        if not participants and item["customer_name"]:
            participants.append({
                "client_name": item["customer_name"],
                "client_phone": item["customer_phone"],
                "guests_count": 1,
                "amount_due": None,
            })
        participant_total = sum(
            max(0, int(participant["guests_count"] or 0))
            for participant in participants
        )
        result.append({
            "id": item["id"],
            "kind_label": item["kind_label"],
            "service_name": item["service_name"],
            "boat": item["boat"],
            "boat_label": item["boat_label"],
            "activity_type": item["activity_type"],
            "trip_date": item["trip_date"],
            "start_time": item["start_time"],
            "end_time": item["end_time"],
            "capacity": item["capacity"],
            "guests_count": item["guests_count"],
            "participants_count": participant_total or item["participants_count"],
            "participants": participants,
            "assignments": [
                {
                    "employee_name": assignment["employee_name"],
                    "role_label": CREW_ROLES.get(
                        assignment["role"], assignment["role"]
                    ),
                }
                for assignment in item["assignments"]
            ],
        })
    return result


def day_view(
    db,
    day,
    selected_employee,
    boats,
    boat_colors,
    avatar_url,
    include_unassigned_tripster=True,
    include_tripster=True,
    attach_weather=None,
):
    crew = repository.list_crew_employees(db)
    raw_items = repository.list_day_items(db, day.isoformat())
    if not include_tripster:
        raw_items = [item for item in raw_items if item["source"] != "tripster"]
    if not include_unassigned_tripster:
        raw_items = [
            item for item in raw_items
            if item["source"] != "tripster" or item["assignments"]
        ]
    has_unassigned = any(not item["assignments"] for item in raw_items)
    unassigned_employee = {
        "id": 0,
        "name": "Не назначено",
        "positions": [],
        "position_label": "Заказы без катера и экипажа",
        "avatar_url": None,
        "has_day_assignment": False,
        "is_unassigned": True,
    }
    day_crew_ids = set(repository.list_day_crew_ids(db, day.isoformat()))
    assigned_today_ids = repository.list_day_assignment_employee_ids(
        db, day.isoformat()
    )
    day_crew = [employee for employee in crew if employee["id"] in day_crew_ids]
    available_crew = [employee for employee in crew if employee["id"] not in day_crew_ids]
    selected_id = None
    selected_unassigned = selected_employee == "unassigned" and has_unassigned
    if selected_employee not in (None, "", "all"):
        try:
            candidate_id = int(selected_employee)
        except ValueError:
            candidate_id = None
        if any(employee["id"] == candidate_id for employee in day_crew):
            selected_id = candidate_id

    if selected_unassigned:
        visible_crew = [unassigned_employee]
    elif selected_id is not None:
        visible_crew = [
            employee for employee in day_crew if employee["id"] == selected_id
        ]
    else:
        visible_crew = list(day_crew)
        if has_unassigned:
            visible_crew.append(unassigned_employee)
    for employee in crew:
        employee["avatar_url"] = avatar_url(employee["name"])
        employee["position_label"] = " · ".join(employee["positions"])
        employee["has_day_assignment"] = employee["id"] in assigned_today_ids

    colors_by_boat = _display_colors_by_boat(boat_colors)
    activity_by_service_id = {
        service["id"]: service["activity_type"]
        for service in service_repository.list_services(db)
    }
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
        item["activity_type"] = activity_by_service_id.get(
            item["service_id"], "boat"
        )
        item["boat_label"] = (
            item["boat"]
            or (
                "Не требуется"
                if item["activity_type"] == "city"
                else "Не назначен"
            )
        )
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

    if attach_weather is not None:
        # Must run before the card-copy loop below (`card = dict(item)`),
        # so every per-employee card inherits item["weather"] too.
        attach_weather(db, items)

    earliest = max(0, earliest)
    latest = min(24 * 60, latest)
    total_minutes = max(60, latest - earliest)
    px_per_minute = 1.25
    cards_by_employee = {employee["id"]: [] for employee in visible_crew}
    for item in items:
        item_assignments = item["assignments"] or [{
            "employee_id": 0,
            "role": "unassigned",
        }]
        for assignment in item_assignments:
            employee_id = assignment["employee_id"]
            if employee_id not in cards_by_employee:
                continue
            start_dt = dt.datetime.strptime(item["starts_at"], "%Y-%m-%d %H:%M")
            end_dt = dt.datetime.strptime(item["ends_at"], "%Y-%m-%d %H:%M")
            start_minutes = start_dt.hour * 60 + start_dt.minute
            end_minutes = end_dt.hour * 60 + end_dt.minute
            card = dict(item)
            card["assignment_role"] = (
                "Нужно назначить"
                if assignment["role"] == "unassigned"
                else CREW_ROLES.get(assignment["role"], assignment["role"])
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
        "day_crew": day_crew,
        "available_crew": available_crew,
        "visible_crew": visible_crew,
        "has_unassigned": has_unassigned,
        "items": items,
        "cards_by_employee": cards_by_employee,
        "selected_employee": (
            "unassigned" if selected_unassigned
            else str(selected_id) if selected_id is not None
            else "all"
        ),
        "hour_marks": hour_marks,
        "grid_height": round(total_minutes * px_per_minute, 2),
        "day_start_minutes": earliest,
        "px_per_minute": px_per_minute,
        "time_step_minutes": TIME_STEP_MINUTES,
        "time_step_px": round(TIME_STEP_MINUTES * px_per_minute, 2),
        "now_line_px": now_line_px,
    }
