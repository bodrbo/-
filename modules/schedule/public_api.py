"""Server-to-server API used by the public excursion website.

The browser never receives the integration secret.  bodrbo-fort.ru talks to
this API through its small same-origin PHP proxy, which keeps the credential
outside ``public_html``.
"""

import datetime as dt
import secrets
import sqlite3

from flask import Blueprint, jsonify, request

from modules.excursion_services import repository as service_repository

from . import repository


SOURCE = "fort_site"
MAX_AVAILABILITY_DAYS = 31
MAX_GUESTS_PER_BOOKING = 12


def _json_error(status, error, message):
    return jsonify(ok=False, error=error, message=message), status


def _secret_value(provider):
    value = provider() if callable(provider) else provider
    return str(value or "").strip()


def _token_is_valid(authorization, configured_secret):
    scheme, separator, supplied = (authorization or "").partition(" ")
    token = supplied.strip() if separator and scheme.lower() == "bearer" else ""
    return secrets.compare_digest(
        token.encode("utf-8"), configured_secret.encode("utf-8")
    )


def _authorise(secret_provider):
    configured_secret = _secret_value(secret_provider)
    if not configured_secret:
        return _json_error(
            503,
            "integration_not_configured",
            "Онлайн-запись пока не настроена.",
        )
    if not _token_is_valid(
        request.headers.get("Authorization", ""), configured_secret
    ):
        response, status = _json_error(
            401, "unauthorized", "Неверный токен авторизации."
        )
        response.headers["WWW-Authenticate"] = "Bearer"
        return response, status
    return None


def _normalise_phone_identity(phone):
    digits = "".join(character for character in phone if character.isdigit())
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def _parse_availability_window(raw_start, raw_days):
    today = dt.date.today()
    try:
        start = dt.date.fromisoformat(str(raw_start or today.isoformat()))
    except ValueError:
        return None, None, "Дата начала должна быть в формате ГГГГ-ММ-ДД."
    try:
        days = int(str(raw_days or "21"))
    except ValueError:
        return None, None, "Количество дней должно быть целым числом."
    if not 1 <= days <= MAX_AVAILABILITY_DAYS:
        return None, None, f"Можно запросить от 1 до {MAX_AVAILABILITY_DAYS} дней."
    if start < today:
        start = today
    return start, days, None


def _public_events(db, start, days):
    start_at = max(
        dt.datetime.combine(start, dt.time.min),
        dt.datetime.now().replace(second=0, microsecond=0),
    )
    end_at = dt.datetime.combine(start + dt.timedelta(days=days), dt.time.min)
    rows = db.execute(
        "SELECT schedule_items.id, schedule_items.service_id, "
        "schedule_items.service_name, schedule_items.starts_at, "
        "schedule_items.ends_at, schedule_items.capacity, "
        "excursion_services.price AS catalog_price, "
        "COALESCE((SELECT SUM(schedule_participants.guests_count) "
        "FROM schedule_participants WHERE schedule_participants.schedule_item_id = "
        "schedule_items.id), 0) AS actual_participants "
        "FROM schedule_items LEFT JOIN excursion_services "
        "ON excursion_services.id = schedule_items.service_id "
        "WHERE schedule_items.deleted_at IS NULL "
        "AND schedule_items.kind = 'event' "
        "AND schedule_items.status = 'scheduled' "
        "AND schedule_items.capacity IS NOT NULL "
        "AND schedule_items.starts_at >= ? AND schedule_items.starts_at < ? "
        "ORDER BY schedule_items.starts_at, schedule_items.id",
        (
            start_at.strftime("%Y-%m-%d %H:%M"),
            end_at.strftime("%Y-%m-%d %H:%M"),
        ),
    ).fetchall()
    result = []
    for row in rows:
        capacity = int(row["capacity"] or 0)
        occupied = int(row["actual_participants"] or 0)
        available = max(0, capacity - occupied)
        if available <= 0:
            continue
        price = row["catalog_price"]
        if price is None:
            service = service_repository.get_service_by_name(
                db, row["service_name"]
            )
            price = service["price"] if service is not None else 0
        starts_at = dt.datetime.strptime(row["starts_at"], "%Y-%m-%d %H:%M")
        ends_at = dt.datetime.strptime(row["ends_at"], "%Y-%m-%d %H:%M")
        result.append({
            "id": row["id"],
            "service_name": row["service_name"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "duration_minutes": max(
                0, int((ends_at - starts_at).total_seconds() // 60)
            ),
            "price_per_guest": round(float(price or 0), 2),
            "available_seats": available,
            "capacity": capacity,
        })
    return result


def _validate_booking_payload(payload):
    if not isinstance(payload, dict):
        return None, "Тело запроса должно быть JSON-объектом."
    request_id = payload.get("request_id")
    name = payload.get("name")
    phone = payload.get("phone")
    if not all(isinstance(value, str) for value in (request_id, name, phone)):
        return None, "Идентификатор, имя и телефон должны быть строками."
    request_id = request_id.strip()
    name = " ".join(name.strip().split())
    phone = phone.strip()
    if not request_id or len(request_id) > 128:
        return None, "Некорректный идентификатор записи."
    if not name or len(name) > 180:
        return None, "Укажите имя не длиннее 180 символов."
    if len(phone) > 40 or len(_normalise_phone_identity(phone)) != 11:
        return None, "Укажите российский номер телефона."
    try:
        item_id = int(payload.get("schedule_item_id"))
        guests_count = int(payload.get("guests_count"))
    except (TypeError, ValueError):
        return None, "Выберите рейс и количество гостей."
    if item_id <= 0:
        return None, "Выберите рейс."
    if not 1 <= guests_count <= MAX_GUESTS_PER_BOOKING:
        return None, f"За одну запись можно добавить от 1 до {MAX_GUESTS_PER_BOOKING} гостей."
    if payload.get("consent") is not True:
        return None, "Подтвердите согласие на обработку персональных данных."
    return {
        "request_id": request_id,
        "item_id": item_id,
        "name": name,
        "phone": phone,
        "phone_identity": _normalise_phone_identity(phone),
        "guests_count": guests_count,
    }, None


def _event_for_booking(db, item_id):
    return db.execute(
        "SELECT schedule_items.*, excursion_services.price AS catalog_price, "
        "COALESCE((SELECT SUM(schedule_participants.guests_count) "
        "FROM schedule_participants WHERE schedule_participants.schedule_item_id = "
        "schedule_items.id), 0) AS actual_participants "
        "FROM schedule_items LEFT JOIN excursion_services "
        "ON excursion_services.id = schedule_items.service_id "
        "WHERE schedule_items.id = ? AND schedule_items.deleted_at IS NULL",
        (item_id,),
    ).fetchone()


def _create_booking(db, data):
    source_ref = f"{SOURCE}:{data['request_id']}"
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute("BEGIN IMMEDIATE")
    existing = db.execute(
        "SELECT id, schedule_item_id, guests_count, price FROM schedule_participants "
        "WHERE source = ? AND source_ref = ?",
        (SOURCE, source_ref),
    ).fetchone()
    if existing is not None:
        db.commit()
        return {
            "duplicate": True,
            "booking_id": existing["id"],
            "schedule_item_id": existing["schedule_item_id"],
            "guests_count": existing["guests_count"],
            "total": round(float(existing["price"] or 0), 2),
        }, None, 200

    event = _event_for_booking(db, data["item_id"])
    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    if (
        event is None
        or event["kind"] != "event"
        or event["status"] != "scheduled"
        or event["starts_at"] <= now_iso
    ):
        db.rollback()
        return None, ("slot_unavailable", "Этот рейс больше недоступен."), 409
    available = max(
        0, int(event["capacity"] or 0) - int(event["actual_participants"] or 0)
    )
    if data["guests_count"] > available:
        db.rollback()
        return None, (
            "not_enough_seats",
            f"На выбранном рейсе осталось мест: {available}.",
        ), 409

    clients = repository.list_all_clients(db)
    client = next(
        (
            candidate for candidate in clients
            if _normalise_phone_identity(candidate["phone"]) == data["phone_identity"]
        ),
        None,
    )
    name = client["client_name"] if client is not None else data["name"]
    phone = client["phone"] if client is not None else data["phone"]
    price_per_guest = float(event["catalog_price"] or 0)
    if not price_per_guest and event["service_id"] is not None:
        service = service_repository.get_service(db, event["service_id"])
        price_per_guest = float(service["price"] or 0) if service else 0
    total = round(price_per_guest * data["guests_count"], 2)
    try:
        participant_id = repository.add_external_participant(
            db=db,
            item_id=event["id"],
            client_id=client["id"] if client is not None else None,
            name=name,
            phone=phone,
            guests_count=data["guests_count"],
            price=total,
            timestamp=timestamp,
            source=SOURCE,
            source_ref=source_ref,
            sales_channel="bodrbo_fort",
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return None, (
            "already_booked",
            "Этот телефон уже записан на выбранный рейс. Для изменения записи позвоните нам.",
        ), 409
    return {
        "duplicate": False,
        "booking_id": participant_id,
        "schedule_item_id": event["id"],
        "service_name": event["service_name"],
        "starts_at": event["starts_at"],
        "guests_count": data["guests_count"],
        "total": total,
    }, None, 201


def create_public_booking_blueprint(get_db, secret_provider):
    blueprint = Blueprint("public_excursion_booking", __name__)

    @blueprint.after_request
    def disable_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.route("/api/integrations/excursion-booking/availability")
    def availability():
        auth_error = _authorise(secret_provider)
        if auth_error is not None:
            return auth_error
        start, days, validation_error = _parse_availability_window(
            request.args.get("from"), request.args.get("days")
        )
        if validation_error:
            return _json_error(400, "invalid_request", validation_error)
        return jsonify(
            ok=True,
            from_date=start.isoformat(),
            days=days,
            items=_public_events(get_db(), start, days),
        )

    @blueprint.route(
        "/api/integrations/excursion-booking/bookings", methods=["POST"]
    )
    def create_booking():
        auth_error = _authorise(secret_provider)
        if auth_error is not None:
            return auth_error
        payload = request.get_json(silent=True) if request.is_json else None
        data, validation_error = _validate_booking_payload(payload)
        if validation_error:
            return _json_error(400, "invalid_payload", validation_error)
        try:
            result, business_error, status = _create_booking(get_db(), data)
        except sqlite3.Error:
            get_db().rollback()
            return _json_error(
                503,
                "service_unavailable",
                "Не удалось сохранить запись. Попробуйте ещё раз позже.",
            )
        if business_error is not None:
            error, message = business_error
            return _json_error(status, error, message)
        return jsonify(ok=True, **result), status

    return blueprint
