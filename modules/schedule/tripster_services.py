"""Idempotent projection of Tripster orders into the internal schedule."""

import datetime as dt
import json
import secrets

from modules.clients.constants import EXCURSION_SEGMENT
from modules.clients.services import ensure_segment
from modules.excursion_services import repository as service_repository


PAID_STATUS = "paid"
CANCELLED_STATUS = "cancelled"
SOURCE = "tripster"
UNASSIGNED_BOAT = "Не назначен"
# Moscow has observed UTC+3 year-round since 2014.  A fixed offset is enough
# for current Tripster bookings and, unlike zoneinfo, remains importable in
# Beget's Python 3.7 Passenger environment.
MOSCOW_TZ = dt.timezone(dt.timedelta(hours=3))


def _text(value, limit=500):
    return " ".join(str(value or "").strip().split())[:limit]


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalise_phone(phone):
    digits = "".join(
        character for character in str(phone or "") if character.isdigit()
    )
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def _event_start(order):
    event = order.get("event") if isinstance(order.get("event"), dict) else {}
    raw_aware = _text(event.get("aware_start_dt"), 80)
    if raw_aware:
        try:
            parsed = dt.datetime.fromisoformat(raw_aware.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(MOSCOW_TZ).replace(tzinfo=None)
            return parsed.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    raw_date = _text(event.get("date"), 20)
    raw_time = _text(event.get("time"), 20)
    if not raw_date or not raw_time:
        return None
    for value_format in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(
                f"{raw_date} {raw_time}", value_format
            ).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return None


def _price_rub(order):
    price = order.get("price") if isinstance(order.get("price"), dict) else {}
    value = max(0.0, _number(price.get("value")))
    currency = _text(price.get("currency"), 12).upper()
    if currency and currency not in {"RUB", "RUR", "₽"}:
        rate = _number(price.get("currency_rate"))
        if rate > 0:
            value *= rate
    return round(value, 2)


def _persons_count(order):
    """Read the order total, falling back to Tripster ticket breakdown."""
    direct_count = _integer(order.get("persons_count"))
    if direct_count > 0:
        return max(1, min(100, direct_count))
    price = order.get("price") if isinstance(order.get("price"), dict) else {}
    tickets = price.get("per_ticket")
    if not isinstance(tickets, list):
        tickets = []
    ticket_count = sum(
        max(0, _integer(ticket.get("count")))
        for ticket in tickets
        if isinstance(ticket, dict)
    )
    return max(1, min(100, ticket_count or 1))


def _normalise_order(order):
    order_id = _integer(order.get("id"))
    experience_id = _integer(order.get("experience_id"))
    if order_id <= 0 or experience_id <= 0:
        return None
    traveler = (
        order.get("traveler")
        if isinstance(order.get("traveler"), dict)
        else {}
    )
    event = order.get("event") if isinstance(order.get("event"), dict) else {}
    return {
        "order_id": order_id,
        "experience_id": experience_id,
        "status": _text(order.get("status"), 40).lower(),
        "event_start": _event_start(order),
        "is_grouping_enabled": 1 if event.get("is_grouping_enabled") else 0,
        "persons_count": _persons_count(order),
        "traveler_id": _integer(traveler.get("id")) or None,
        "traveler_name": _text(traveler.get("name"), 180),
        "traveler_phone": _text(traveler.get("phone"), 40),
        "traveler_email": _text(traveler.get("email"), 180),
        "price_rub": _price_rub(order),
        "order_url": _text(order.get("url"), 500),
        "raw_payload": json.dumps(order, ensure_ascii=False, sort_keys=True),
    }


def _source_ref(order, is_group_order=None):
    if is_group_order is None:
        is_group_order = bool(order["is_grouping_enabled"])
    if is_group_order:
        return f"event:{order['experience_id']}:{order['event_start']}"
    return f"order:{order['order_id']}"


def _mapped_service(db, experience_id):
    return service_repository.get_service_by_tripster_id(db, experience_id)


def _ensure_schedule_item(db, order, timestamp):
    mapped_service = _mapped_service(db, order["experience_id"])
    is_group_order = bool(
        order["is_grouping_enabled"]
        or (
            mapped_service is not None
            and mapped_service["service_type"] == "group"
        )
    )
    source_ref = _source_ref(order, is_group_order)
    existing = db.execute(
        "SELECT * FROM schedule_items WHERE source = ? AND source_ref = ?",
        (SOURCE, source_ref),
    ).fetchone()
    if existing is not None:
        if mapped_service is not None and existing["service_id"] is None:
            starts_at = dt.datetime.strptime(
                order["event_start"], "%Y-%m-%d %H:%M"
            )
            ends_at = starts_at + dt.timedelta(hours=mapped_service["hours"])
            db.execute(
                "UPDATE schedule_items SET service_id = ?, service_name = ?, "
                "ends_at = ?, deleted_at = NULL, status = 'scheduled', "
                "source_updated_at = ?, updated_at = ? WHERE id = ?",
                (
                    mapped_service["id"], mapped_service["name"],
                    ends_at.strftime("%Y-%m-%d %H:%M"), timestamp, timestamp,
                    existing["id"],
                ),
            )
        else:
            db.execute(
                "UPDATE schedule_items SET deleted_at = NULL, status = 'scheduled', "
                "source_updated_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, existing["id"]),
            )
        return existing["id"], False

    starts_at = dt.datetime.strptime(order["event_start"], "%Y-%m-%d %H:%M")
    duration_hours = mapped_service["hours"] if mapped_service is not None else 1
    ends_at = starts_at + dt.timedelta(hours=duration_hours)
    kind = "event" if is_group_order else "booking"
    service_id = mapped_service["id"] if mapped_service is not None else None
    service_name = (
        mapped_service["name"] if mapped_service is not None
        else f"Tripster · экскурсия #{order['experience_id']}"
    )
    if mapped_service is not None:
        note = (
            "Импортировано из Tripster. Услуга определена по Tripster ID; "
            "назначьте катер и сотрудников."
        )
    else:
        note = (
            "Импортировано из Tripster. API не передаёт длительность, катер и "
            "экипаж — проверьте окончание рейса и назначьте услугу, катер и "
            "сотрудников."
        )
    cursor = db.execute(
        "INSERT INTO schedule_items "
        "(kind, boat, service_id, service_name, starts_at, ends_at, capacity, "
        "participants_count, customer_name, customer_phone, revenue, note, status, "
        "source, source_ref, source_updated_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, '', '', 0, ?, 'scheduled', ?, ?, ?, ?, ?)",
        (
            kind, UNASSIGNED_BOAT, service_id, service_name,
            starts_at.strftime("%Y-%m-%d %H:%M"),
            ends_at.strftime("%Y-%m-%d %H:%M"),
            10 if kind == "event" else None,
            note,
            SOURCE,
            source_ref,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return cursor.lastrowid, True


def _find_client_by_identity(db, order):
    traveler_id = order["traveler_id"]
    if traveler_id is not None:
        linked = db.execute(
            "SELECT client_id FROM tripster_travelers WHERE traveler_id = ?",
            (traveler_id,),
        ).fetchone()
        if linked is not None:
            return linked["client_id"]

    phone_identity = _normalise_phone(order["traveler_phone"])
    if len(phone_identity) >= 7:
        matches = []
        for client in db.execute("SELECT id, phone FROM clients").fetchall():
            if _normalise_phone(client["phone"]) == phone_identity:
                matches.append(client["id"])
        if len(matches) == 1:
            return matches[0]

    if order["traveler_email"]:
        matches = db.execute(
            "SELECT id FROM clients WHERE lower(email) = lower(?)",
            (order["traveler_email"],),
        ).fetchall()
        if len(matches) == 1:
            return matches[0]["id"]
    return None


def _ensure_client(db, order, timestamp):
    client_id = _find_client_by_identity(db, order)
    name = order["traveler_name"] or f"Клиент Tripster #{order['order_id']}"
    if client_id is None:
        cursor = db.execute(
            "INSERT INTO clients "
            "(client_name, boat_model, phone, token, created_at, email) "
            "VALUES (?, '', ?, ?, ?, ?)",
            (
                name,
                order["traveler_phone"],
                secrets.token_urlsafe(16),
                timestamp,
                order["traveler_email"],
            ),
        )
        client_id = cursor.lastrowid
    else:
        db.execute(
            "UPDATE clients SET phone = CASE WHEN TRIM(COALESCE(phone, '')) = '' "
            "THEN ? ELSE phone END, email = CASE WHEN TRIM(COALESCE(email, '')) = '' "
            "THEN ? ELSE email END WHERE id = ?",
            (order["traveler_phone"], order["traveler_email"], client_id),
        )
    ensure_segment(db, client_id, EXCURSION_SEGMENT, timestamp)
    if order["traveler_id"] is not None:
        linked = db.execute(
            "SELECT traveler_id FROM tripster_travelers WHERE traveler_id = ?",
            (order["traveler_id"],),
        ).fetchone()
        if linked is None:
            db.execute(
                "INSERT INTO tripster_travelers "
                "(traveler_id, client_id, updated_at) VALUES (?, ?, ?)",
                (order["traveler_id"], client_id, timestamp),
            )
        else:
            db.execute(
                "UPDATE tripster_travelers SET client_id = ?, updated_at = ? "
                "WHERE traveler_id = ?",
                (client_id, timestamp, order["traveler_id"]),
            )
    return client_id, name


def _rebuild_item(db, item_id, timestamp):
    item = db.execute(
        "SELECT * FROM schedule_items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is None:
        return False
    active_rows = db.execute(
        "SELECT * FROM tripster_orders WHERE schedule_item_id = ? AND status = ? "
        "ORDER BY order_id",
        (item_id, PAID_STATUS),
    ).fetchall()
    db.execute(
        "DELETE FROM schedule_participants WHERE schedule_item_id = ? AND source = ?",
        (item_id, SOURCE),
    )
    if not active_rows:
        db.execute(
            "UPDATE schedule_items SET deleted_at = ?, updated_at = ?, "
            "source_updated_at = ? WHERE id = ?",
            (timestamp, timestamp, timestamp, item_id),
        )
        return True

    grouped = {}
    for row in active_rows:
        order = dict(row)
        client_id, client_name = _ensure_client(db, order, timestamp)
        entry = grouped.setdefault(client_id, {
            "client_id": client_id,
            "client_name": client_name,
            "client_phone": order["traveler_phone"],
            "guests_count": 0,
            "price": 0.0,
            "order_ids": [],
        })
        entry["guests_count"] += order["persons_count"]
        entry["price"] += order["price_rub"]
        entry["order_ids"].append(str(order["order_id"]))

    for participant in grouped.values():
        existing = db.execute(
            "SELECT id FROM schedule_participants "
            "WHERE schedule_item_id = ? AND client_id = ?",
            (item_id, participant["client_id"]),
        ).fetchone()
        if existing is not None:
            db.execute(
                "UPDATE schedule_participants SET client_name = ?, client_phone = ?, "
                "guests_count = ?, price = ? WHERE id = ?",
                (
                    participant["client_name"], participant["client_phone"],
                    participant["guests_count"], round(participant["price"], 2),
                    existing["id"],
                ),
            )
            continue
        db.execute(
            "INSERT INTO schedule_participants "
            "(schedule_item_id, client_id, client_name, client_phone, guests_count, "
            "price, created_at, source, source_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id, participant["client_id"], participant["client_name"],
                participant["client_phone"], participant["guests_count"],
                round(participant["price"], 2), timestamp, SOURCE,
                "orders:" + ",".join(participant["order_ids"]),
            ),
        )

    totals = db.execute(
        "SELECT COALESCE(SUM(guests_count), 0) AS guests, "
        "COALESCE(SUM(price), 0) AS revenue FROM schedule_participants "
        "WHERE schedule_item_id = ?",
        (item_id,),
    ).fetchone()
    first = next(iter(grouped.values()))
    capacity = item["capacity"]
    if item["kind"] == "event":
        capacity = max(capacity or 10, totals["guests"])
    db.execute(
        "UPDATE schedule_items SET participants_count = ?, revenue = ?, capacity = ?, "
        "customer_name = ?, customer_phone = ?, deleted_at = NULL, status = 'scheduled', "
        "source_updated_at = ?, updated_at = ? WHERE id = ?",
        (
            totals["guests"], round(totals["revenue"], 2), capacity,
            first["client_name"] if item["kind"] == "booking" else "",
            first["client_phone"] if item["kind"] == "booking" else "",
            timestamp, timestamp, item_id,
        ),
    )
    return False


def _apply_catalog_mappings(db, timestamp):
    """Attach catalog services to older Tripster items left unclassified."""
    mappings = {
        service["tripster_id"]: service
        for service in service_repository.list_services(db)
        if service["tripster_id"] is not None
    }
    if not mappings:
        return 0
    rows = db.execute(
        "SELECT DISTINCT schedule_items.id, schedule_items.starts_at, "
        "tripster_orders.experience_id FROM schedule_items "
        "JOIN tripster_orders ON tripster_orders.schedule_item_id = schedule_items.id "
        "WHERE schedule_items.source = ? AND schedule_items.deleted_at IS NULL "
        "AND schedule_items.service_id IS NULL AND tripster_orders.status = ?",
        (SOURCE, PAID_STATUS),
    ).fetchall()
    matched = 0
    for row in rows:
        service = mappings.get(row["experience_id"])
        if service is None:
            continue
        try:
            starts_at = dt.datetime.strptime(row["starts_at"], "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            continue
        ends_at = starts_at + dt.timedelta(hours=service["hours"])
        cursor = db.execute(
            "UPDATE schedule_items SET service_id = ?, service_name = ?, "
            "ends_at = ?, source_updated_at = ?, updated_at = ? "
            "WHERE id = ? AND service_id IS NULL",
            (
                service["id"], service["name"],
                ends_at.strftime("%Y-%m-%d %H:%M"), timestamp, timestamp,
                row["id"],
            ),
        )
        matched += cursor.rowcount
    return matched


def sync_orders(db, fetcher, now=None, force_full=False):
    """Fetch order deltas and apply paid/cancelled bookings atomically."""
    now = (now or dt.datetime.now()).replace(second=0, microsecond=0)
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    state = db.execute(
        "SELECT last_success_at FROM tripster_sync_state WHERE sync_key = 'orders'"
    ).fetchone()
    updated_after = None
    if not force_full and state is not None and state["last_success_at"]:
        try:
            cursor = dt.datetime.strptime(state["last_success_at"], "%Y-%m-%d %H:%M")
            updated_after = (cursor - dt.timedelta(minutes=10)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            updated_after = None

    payloads = fetcher(updated_after=updated_after)
    stats = {
        "received": len(payloads),
        "created": 0,
        "updated": 0,
        "cancelled": 0,
        "pending": 0,
        "invalid": 0,
        "matched": 0,
    }
    affected_item_ids = set()
    try:
        for payload in payloads:
            order = _normalise_order(payload)
            if order is None:
                stats["invalid"] += 1
                continue
            existing = db.execute(
                "SELECT schedule_item_id FROM tripster_orders WHERE order_id = ?",
                (order["order_id"],),
            ).fetchone()
            old_item_id = existing["schedule_item_id"] if existing else None
            if old_item_id is not None:
                affected_item_ids.add(old_item_id)

            schedule_item_id = old_item_id
            if order["status"] == PAID_STATUS and order["event_start"]:
                schedule_item_id, created = _ensure_schedule_item(
                    db, order, timestamp
                )
                affected_item_ids.add(schedule_item_id)
                stats["created" if created else "updated"] += 1
            elif order["status"] == CANCELLED_STATUS:
                stats["cancelled"] += 1
            else:
                stats["pending"] += 1

            order_values = (
                order["experience_id"], order["status"], order["event_start"],
                order["is_grouping_enabled"], order["persons_count"],
                order["traveler_id"], order["traveler_name"],
                order["traveler_phone"], order["traveler_email"],
                order["price_rub"], order["order_url"], order["raw_payload"],
                schedule_item_id,
            )
            if existing is None:
                db.execute(
                    "INSERT INTO tripster_orders "
                    "(order_id, experience_id, status, event_start, "
                    "is_grouping_enabled, persons_count, traveler_id, traveler_name, "
                    "traveler_phone, traveler_email, price_rub, order_url, raw_payload, "
                    "schedule_item_id, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (order["order_id"],) + order_values + (timestamp, timestamp),
                )
            else:
                db.execute(
                    "UPDATE tripster_orders SET experience_id = ?, status = ?, "
                    "event_start = ?, is_grouping_enabled = ?, persons_count = ?, "
                    "traveler_id = ?, traveler_name = ?, traveler_phone = ?, "
                    "traveler_email = ?, price_rub = ?, order_url = ?, raw_payload = ?, "
                    "schedule_item_id = ?, last_seen_at = ? WHERE order_id = ?",
                    order_values + (timestamp, order["order_id"]),
                )

        for item_id in affected_item_ids:
            _rebuild_item(db, item_id, timestamp)
        stats["matched"] = _apply_catalog_mappings(db, timestamp)
        if state is None:
            db.execute(
                "INSERT INTO tripster_sync_state (sync_key, last_success_at) "
                "VALUES ('orders', ?)",
                (timestamp,),
            )
        else:
            db.execute(
                "UPDATE tripster_sync_state SET last_success_at = ? "
                "WHERE sync_key = 'orders'",
                (timestamp,),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return stats
