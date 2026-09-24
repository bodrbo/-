"""SQL access for the internal trip schedule."""

import hashlib
import secrets

from modules.clients.constants import (
    CLIENT_RELATIONSHIP_CLIENT,
    CLIENT_RELATIONSHIP_PARTNER,
    EXCURSION_SEGMENT,
)
from modules.clients.services import ensure_segment


def list_excursion_partners(db):
    """Clients in «Клиенты и Партнёры» -> «Партнёры экскурсий» — the choices
    for a participant's «Канал продаж» in the trip form."""
    return [
        dict(row)
        for row in db.execute(
            "SELECT clients.id, clients.client_name FROM clients "
            "JOIN client_segments ON client_segments.client_id = clients.id "
            "AND client_segments.segment = ? "
            "AND client_segments.relationship_type = ? "
            "ORDER BY clients.client_name COLLATE NOCASE",
            (EXCURSION_SEGMENT, CLIENT_RELATIONSHIP_PARTNER),
        ).fetchall()
    ]


def list_crew_employees(db):
    rows = db.execute(
        "SELECT employees.id, employees.name, employee_positions.position "
        "FROM employees JOIN employee_positions "
        "ON employee_positions.employee_id = employees.id "
        "WHERE employees.deleted_at IS NULL "
        "AND employee_positions.position IN ('Капитан', 'Гид', 'Гид-капитан') "
        "ORDER BY employees.name, employee_positions.position"
    ).fetchall()
    employees = {}
    for row in rows:
        employee = employees.setdefault(
            row["id"],
            {"id": row["id"], "name": row["name"], "positions": []},
        )
        employee["positions"].append(row["position"])
    return list(employees.values())


def get_employee_id_by_name(db, name):
    """Used by services.create_item_from_trip to resolve a historical
    trip's entries.employee (a plain name string) back to a real
    employee_id. Not filtered to active positions or deleted_at — a
    historical card should still reflect who actually worked the trip,
    even if they've since left; an active match wins on a name collision."""
    row = db.execute(
        "SELECT id FROM employees WHERE name = ? "
        "ORDER BY deleted_at IS NULL DESC LIMIT 1",
        (name,),
    ).fetchone()
    return row["id"] if row else None


def list_day_crew_ids(db, day):
    return [
        row["employee_id"]
        for row in db.execute(
            "SELECT employee_id FROM schedule_day_crew "
            "WHERE work_date = ? ORDER BY created_at, employee_id",
            (day,),
        ).fetchall()
    ]


def list_day_assignment_employee_ids(db, day):
    return {
        row["employee_id"]
        for row in db.execute(
            "SELECT DISTINCT schedule_assignments.employee_id "
            "FROM schedule_assignments JOIN schedule_items "
            "ON schedule_items.id = schedule_assignments.schedule_item_id "
            "WHERE schedule_items.deleted_at IS NULL "
            "AND substr(schedule_items.starts_at, 1, 10) = ?",
            (day,),
        ).fetchall()
    }


def add_day_crew_member(db, day, employee_id, timestamp):
    cursor = db.execute(
        "INSERT OR IGNORE INTO schedule_day_crew "
        "(work_date, employee_id, created_at) VALUES (?, ?, ?)",
        (day, employee_id, timestamp),
    )
    db.commit()
    return cursor.rowcount > 0


def remove_day_crew_member(db, day, employee_id):
    cursor = db.execute(
        "DELETE FROM schedule_day_crew WHERE work_date = ? AND employee_id = ?",
        (day, employee_id),
    )
    db.commit()
    return cursor.rowcount > 0


def search_clients(db, query, limit=20):
    """Return a small ranked slice of excursion clients for autocomplete."""
    words = [word for word in str(query or "").strip().casefold().split() if word]
    if not words:
        return []
    rows = db.execute(
        "SELECT clients.id, clients.client_name, clients.phone, clients.status, "
        "clients.preferred_contact_method "
        "FROM clients JOIN client_segments "
        "ON client_segments.client_id = clients.id "
        "AND client_segments.segment = ? "
        "AND client_segments.relationship_type = ?",
        (EXCURSION_SEGMENT, CLIENT_RELATIONSHIP_CLIENT),
    ).fetchall()
    ranked = []
    exact_query = " ".join(words)
    for row in rows:
        client = dict(row)
        searchable = f"{client['client_name']} {client['phone']}".casefold()
        if not all(word in searchable for word in words):
            continue
        name = client["client_name"].casefold()
        if name.startswith(exact_query):
            score = 0
        elif exact_query in name:
            score = 1
        else:
            score = 2 + sum(searchable.index(word) for word in words)
        ranked.append((score, name, client["id"], client))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:limit]]


def list_all_clients(db):
    """All identities are used only for safe phone deduplication."""
    return [
        dict(row)
        for row in db.execute(
            "SELECT id, client_name, phone, status, preferred_contact_method "
            "FROM clients "
            "ORDER BY client_name COLLATE NOCASE, phone, id"
        ).fetchall()
    ]


def get_item(db, item_id, include_deleted=False):
    query = "SELECT * FROM schedule_items WHERE id = ?"
    if not include_deleted:
        query += " AND deleted_at IS NULL"
    return db.execute(query, (item_id,)).fetchone()


def list_tripster_merge_candidates(db, day, source_item_id):
    """Active trips that can receive the guests from one Tripster card.

    A previously merged source stub is deliberately excluded: it is only a
    routing alias for future imports and no longer represents a real trip.
    """
    rows = db.execute(
        "SELECT schedule_items.*, "
        "GROUP_CONCAT(schedule_assignments.employee_name, ', ') AS crew_names "
        "FROM schedule_items "
        "LEFT JOIN schedule_assignments "
        "ON schedule_assignments.schedule_item_id = schedule_items.id "
        "WHERE schedule_items.deleted_at IS NULL "
        "AND schedule_items.id != ? "
        "AND schedule_items.merged_into_item_id IS NULL "
        "AND NOT (schedule_items.source = 'tripster' "
        "AND schedule_items.tripster_resolved = 0 "
        "AND NOT EXISTS (SELECT 1 FROM schedule_assignments AS assigned "
        "WHERE assigned.schedule_item_id = schedule_items.id)) "
        "AND substr(schedule_items.starts_at, 1, 10) = ? "
        "GROUP BY schedule_items.id "
        "ORDER BY schedule_items.starts_at, schedule_items.id",
        (source_item_id, day),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "service_name": row["service_name"],
            "boat": row["boat"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "participants_count": row["participants_count"],
            "crew_names": row["crew_names"] or "Экипаж не назначен",
        }
        for row in rows
    ]


def list_assignments(db, item_id):
    return db.execute(
        "SELECT * FROM schedule_assignments WHERE schedule_item_id = ? "
        "ORDER BY id",
        (item_id,),
    ).fetchall()


def list_items_ready_to_close(db, cutoff):
    """Schedule items whose end time is at or before `cutoff` (the caller
    applies the grace buffer), not cancelled (deleted_at IS NULL), and not
    already turned into a trip — the auto-close job's candidate queue. See
    services.auto_close_schedule_items."""
    return db.execute(
        "SELECT * FROM schedule_items "
        "WHERE deleted_at IS NULL AND accounting_trip_id IS NULL AND ends_at <= ? "
        "ORDER BY ends_at, id",
        (cutoff,),
    ).fetchall()


def set_accounting_trip_id(db, item_id, trip_id, timestamp):
    db.execute(
        "UPDATE schedule_items SET accounting_trip_id = ?, updated_at = ? WHERE id = ?",
        (trip_id, timestamp, item_id),
    )


def list_active_event_items_for_boat(db, boat):
    """Event-kind schedule_items on `boat` that are still live (not
    soft-deleted), each with its current crew size — the target set for
    services.recompute_event_capacities_for_boat, run whenever Fleet's own
    passenger capacity for this boat is saved."""
    return db.execute(
        "SELECT schedule_items.id, schedule_items.capacity, "
        "schedule_items.participants_count, "
        "(SELECT COUNT(*) FROM schedule_assignments "
        "WHERE schedule_assignments.schedule_item_id = schedule_items.id) AS crew_count "
        "FROM schedule_items "
        "WHERE schedule_items.kind = 'event' AND schedule_items.deleted_at IS NULL "
        "AND schedule_items.boat = ?",
        (boat,),
    ).fetchall()


def update_item_capacity(db, item_id, capacity, timestamp):
    db.execute(
        "UPDATE schedule_items SET capacity = ?, updated_at = ? WHERE id = ?",
        (capacity, timestamp, item_id),
    )


def item_has_sales_partner(db, item_id):
    return db.execute(
        "SELECT 1 FROM schedule_participants "
        "WHERE schedule_item_id = ? AND sales_partner_id IS NOT NULL LIMIT 1",
        (item_id,),
    ).fetchone() is not None


def list_trips_without_schedule_card(db, start_date, end_date):
    """YCLIENTS-sourced trips rows in this date range that don't yet
    have a schedule_items card pointing at them — the backfill target for
    services.create_item_from_trip. `trips`/`entries`/`trip_labor` are
    owned by app.py, not this module, but modules.schedule already reads
    across module boundaries elsewhere (excursion_services, clients) —
    same convention."""
    return db.execute(
        "SELECT * FROM trips WHERE source = 'yclients' "
        "AND trip_date BETWEEN ? AND ? "
        "AND id NOT IN ("
        "SELECT accounting_trip_id FROM schedule_items "
        "WHERE accounting_trip_id IS NOT NULL"
        ") ORDER BY trip_date, trip_time",
        (start_date, end_date),
    ).fetchall()


def list_items_needing_participants(db, start_date, end_date):
    """schedule_items linked to a YCLIENTS-sourced trip, in this date
    range, that still have zero schedule_participants rows — the target
    for services.attach_participant_from_record. Re-scans the whole range
    on every run (not just cards made this run) so a card created before
    this feature existed, or one a previous run's raw records didn't
    cover, still gets topped up."""
    return db.execute(
        "SELECT schedule_items.* FROM schedule_items "
        "JOIN trips ON trips.id = schedule_items.accounting_trip_id "
        "WHERE trips.source = 'yclients' "
        "AND schedule_items.deleted_at IS NULL "
        "AND schedule_items.starts_at BETWEEN ? AND ? "
        "AND NOT EXISTS ("
        "SELECT 1 FROM schedule_participants "
        "WHERE schedule_participants.schedule_item_id = schedule_items.id"
        ") ORDER BY schedule_items.starts_at",
        (f"{start_date} 00:00", f"{end_date} 23:59"),
    ).fetchall()


def fill_blank_customer_contact(db, item_id, name, phone, timestamp):
    """Backfill the card's flat customer_name/customer_phone display
    fields the first time a real participant is attached — only when
    still blank, so a manually-edited value is never clobbered."""
    db.execute(
        "UPDATE schedule_items SET "
        "customer_name = CASE WHEN TRIM(COALESCE(customer_name, '')) = '' "
        "THEN ? ELSE customer_name END, "
        "customer_phone = CASE WHEN TRIM(COALESCE(customer_phone, '')) = '' "
        "THEN ? ELSE customer_phone END, "
        "updated_at = ? WHERE id = ?",
        (name, phone, timestamp, item_id),
    )


def get_trip_labor(db, trip_id):
    """Per-employee work_type/quantity(hours) for a trip, the source data
    for reconstructing schedule_assignments. Falls back to the legacy
    single trips.entry_id the same way app.py's own trip-edit screen does,
    for a trip old enough to predate multi-employee trip_labor rows."""
    rows = db.execute(
        "SELECT entries.employee, entries.work_type, entries.quantity "
        "FROM trip_labor JOIN entries ON entries.id = trip_labor.entry_id "
        "WHERE trip_labor.trip_id = ?",
        (trip_id,),
    ).fetchall()
    if rows:
        return rows
    trip = db.execute(
        "SELECT entry_id FROM trips WHERE id = ?", (trip_id,)
    ).fetchone()
    if trip is None or not trip["entry_id"]:
        return []
    row = db.execute(
        "SELECT employee, work_type, quantity FROM entries WHERE id = ?",
        (trip["entry_id"],),
    ).fetchone()
    return [row] if row else []


def list_day_items(db, day):
    items = db.execute(
        "SELECT * FROM schedule_items "
        "WHERE deleted_at IS NULL AND substr(starts_at, 1, 10) = ? "
        "ORDER BY starts_at, id",
        (day,),
    ).fetchall()
    if not items:
        return []
    item_ids = [item["id"] for item in items]
    placeholders = ",".join("?" for _item_id in item_ids)
    assignments = db.execute(
        "SELECT * FROM schedule_assignments "
        f"WHERE schedule_item_id IN ({placeholders}) "
        "ORDER BY schedule_item_id, id",
        tuple(item_ids),
    ).fetchall()
    by_item = {}
    for assignment in assignments:
        by_item.setdefault(assignment["schedule_item_id"], []).append(
            dict(assignment)
        )
    participants = db.execute(
        "SELECT schedule_participants.*, sales_partner.client_name AS sales_partner_name, "
        "own_client.preferred_contact_method AS preferred_contact_method "
        "FROM schedule_participants "
        "LEFT JOIN clients AS sales_partner "
        "ON sales_partner.id = schedule_participants.sales_partner_id "
        "LEFT JOIN clients AS own_client "
        "ON own_client.id = schedule_participants.client_id "
        f"WHERE schedule_participants.schedule_item_id IN ({placeholders}) "
        "ORDER BY schedule_participants.schedule_item_id, schedule_participants.id",
        tuple(item_ids),
    ).fetchall()
    addons_by_item_and_client = {}
    for row in db.execute(
        "SELECT schedule_participant_addons.*, "
        "excursion_addon_products.name AS product_name, "
        "excursion_addon_products.sale_price AS unit_price "
        "FROM schedule_participant_addons "
        "JOIN excursion_addon_products "
        "ON excursion_addon_products.id = schedule_participant_addons.product_id "
        f"WHERE schedule_participant_addons.schedule_item_id IN ({placeholders}) "
        "ORDER BY schedule_participant_addons.id",
        tuple(item_ids),
    ).fetchall():
        key = (row["schedule_item_id"], row["client_id"])
        addons_by_item_and_client.setdefault(key, []).append(dict(row))
    payments_by_participant = {}
    for row in db.execute(
        "SELECT * FROM schedule_yookassa_payments "
        f"WHERE schedule_item_id IN ({placeholders}) ORDER BY id DESC",
        tuple(item_ids),
    ).fetchall():
        payments_by_participant.setdefault(row["participant_id"], []).append(dict(row))
    manual_payments_by_participant = {}
    for row in db.execute(
        "SELECT * FROM schedule_manual_payments "
        f"WHERE schedule_item_id IN ({placeholders}) ORDER BY id DESC",
        tuple(item_ids),
    ).fetchall():
        manual_payments_by_participant.setdefault(
            row["participant_id"], []
        ).append(dict(row))
    attach_receipt_summaries(db, manual_payments_by_participant, payments_by_participant)
    participants_by_item = {}
    for participant in participants:
        participant = dict(participant)
        participant["addons"] = addons_by_item_and_client.get(
            (participant["schedule_item_id"], participant["client_id"]), []
        )
        participant["payments"] = payments_by_participant.get(participant["id"], [])
        participant["manual_payments"] = manual_payments_by_participant.get(
            participant["id"], []
        )
        participant["paid_manual"] = round(sum(
            payment["amount"] for payment in participant["manual_payments"]
        ), 2)
        participants_by_item.setdefault(
            participant["schedule_item_id"], []
        ).append(participant)
    result = []
    for item in items:
        row = dict(item)
        row["assignments"] = by_item.get(item["id"], [])
        row["participants"] = participants_by_item.get(item["id"], [])
        result.append(row)
    return result


def find_employee_conflicts(db, employee_ids, starts_at, ends_at, exclude_id=None):
    if not employee_ids:
        return []
    placeholders = ",".join("?" for _employee_id in employee_ids)
    params = [*employee_ids, ends_at, starts_at]
    query = (
        "SELECT DISTINCT schedule_items.id, schedule_items.service_name, "
        "schedule_items.starts_at, schedule_items.ends_at, "
        "schedule_assignments.employee_name FROM schedule_items "
        "JOIN schedule_assignments ON schedule_assignments.schedule_item_id = schedule_items.id "
        "WHERE schedule_items.deleted_at IS NULL "
        f"AND schedule_assignments.employee_id IN ({placeholders}) "
        "AND schedule_items.starts_at < ? AND schedule_items.ends_at > ?"
    )
    if exclude_id is not None:
        query += " AND schedule_items.id != ?"
        params.append(exclude_id)
    return db.execute(query, tuple(params)).fetchall()


def find_boat_conflicts(db, boat, starts_at, ends_at, exclude_id=None):
    params = [boat, ends_at, starts_at]
    query = (
        "SELECT id, service_name, starts_at, ends_at FROM schedule_items "
        "WHERE deleted_at IS NULL AND boat = ? "
        "AND starts_at < ? AND ends_at > ?"
    )
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    return db.execute(query, tuple(params)).fetchall()


def save_item(db, item_id, data, assignments, participants, timestamp, keep_participants=False):
    """keep_participants is for the trip-info-only edit screen: it edits
    boat/service/date/time/crew for an item whose participants are managed
    separately (add_participant/update_participant/delete_participant), so
    this must leave schedule_participants — and the participants_count/
    revenue rollup on the item itself — exactly as they already are,
    instead of replacing them with an empty submitted list."""
    try:
        # A participant already on this item before this save (survives an
        # edit that re-submits the same client) is never treated as "new"
        # again below, even though the whole schedule_participants table for
        # this item gets deleted and reinserted on every save — that's what
        # keeps a manually removed auto-added product from reappearing the
        # next time someone re-saves the trip for an unrelated reason.
        existing_client_ids = set()
        reusable_booking_participant = None
        if item_id is not None:
            existing_participants = db.execute(
                "SELECT id, client_id FROM schedule_participants "
                "WHERE schedule_item_id = ? ORDER BY id",
                (item_id,),
            ).fetchall()
            existing_client_ids = {row["client_id"] for row in existing_participants}
            existing_item = db.execute(
                "SELECT kind FROM schedule_items WHERE id = ?", (item_id,)
            ).fetchone()
            # An individual booking has exactly one participant. Keep that
            # row stable while its client details or price are edited so
            # manual/ЮKassa payments remain attached to the booking.
            if (
                not keep_participants
                and data["kind"] == "booking"
                and existing_item is not None
                and existing_item["kind"] == "booking"
                and len(existing_participants) == 1
                and len(participants) == 1
            ):
                reusable_booking_participant = existing_participants[0]
        if item_id is None:
            cursor = db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_id, service_name, starts_at, ends_at, capacity, "
                "participants_count, customer_name, customer_phone, guests_count, "
                "revenue, note, status, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', "
                "'internal', ?, ?)",
                (
                    data["kind"], data["boat"], data["service_id"], data["service_name"],
                    data["starts_at"], data["ends_at"], data["capacity"],
                    data["participants_count"], data["customer_name"],
                    data["customer_phone"], data.get("guests_count"),
                    data["revenue"], data["note"],
                    timestamp, timestamp,
                ),
            )
            item_id = cursor.lastrowid
        else:
            participants_count = data["participants_count"]
            revenue = data["revenue"]
            if keep_participants:
                current = db.execute(
                    "SELECT participants_count, revenue FROM schedule_items WHERE id = ?",
                    (item_id,),
                ).fetchone()
                if current is not None:
                    participants_count = current["participants_count"]
                    revenue = current["revenue"]
            db.execute(
                "UPDATE schedule_items SET kind = ?, boat = ?, service_id = ?, service_name = ?, "
                "starts_at = ?, ends_at = ?, capacity = ?, participants_count = ?, "
                "customer_name = ?, customer_phone = ?, guests_count = ?, revenue = ?, note = ?, "
                "updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (
                    data["kind"], data["boat"], data["service_id"], data["service_name"],
                    data["starts_at"], data["ends_at"], data["capacity"],
                    participants_count, data["customer_name"],
                    data["customer_phone"], data.get("guests_count"), revenue, data["note"],
                    timestamp, item_id,
                ),
            )
            db.execute(
                "DELETE FROM schedule_assignments WHERE schedule_item_id = ?",
                (item_id,),
            )
            if not keep_participants:
                if reusable_booking_participant is None:
                    db.execute(
                        "DELETE FROM schedule_participants WHERE schedule_item_id = ?",
                        (item_id,),
                    )
                else:
                    db.execute(
                        "DELETE FROM schedule_participants "
                        "WHERE schedule_item_id = ? AND id != ?",
                        (item_id, reusable_booking_participant["id"]),
                    )
        for assignment in assignments:
            db.execute(
                "INSERT INTO schedule_assignments "
                "(schedule_item_id, employee_id, employee_name, role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    item_id, assignment["employee_id"],
                    assignment["employee_name"], assignment["role"], timestamp,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO schedule_day_crew "
                "(work_date, employee_id, created_at) VALUES (?, ?, ?)",
                (data["starts_at"][:10], assignment["employee_id"], timestamp),
            )
        auto_add_products = db.execute(
            "SELECT id FROM excursion_addon_products WHERE auto_add_service_id = ?",
            (data["service_id"],),
        ).fetchall() if data.get("service_id") is not None and not keep_participants else []
        for participant_index, participant in enumerate(
            [] if keep_participants else participants
        ):
            client_id = participant["client_id"]
            if client_id is None:
                cursor = db.execute(
                    "INSERT INTO clients "
                    "(client_name, boat_model, phone, token, created_at, "
                    "preferred_contact_method) VALUES (?, '', ?, ?, ?, ?)",
                    (
                        participant["client_name"], participant["client_phone"],
                        participant["client_token"], timestamp,
                        participant.get("preferred_contact_method", ""),
                    ),
                )
                client_id = cursor.lastrowid
            ensure_segment(db, client_id, EXCURSION_SEGMENT, timestamp)
            db.execute(
                "UPDATE clients SET preferred_contact_method = ? WHERE id = ?",
                (participant.get("preferred_contact_method", ""), client_id),
            )
            if reusable_booking_participant is not None and participant_index == 0:
                old_client_id = reusable_booking_participant["client_id"]
                db.execute(
                    "UPDATE schedule_participants SET client_id = ?, client_name = ?, "
                    "client_phone = ?, guests_count = ?, price = ?, prepayment = ?, "
                    "payment_due = ?, source = ?, source_ref = ?, sales_partner_id = ? "
                    "WHERE id = ? AND schedule_item_id = ?",
                    (
                        client_id, participant["client_name"],
                        participant["client_phone"], participant["guests_count"],
                        participant["price"], participant.get("prepayment", 0),
                        participant.get("payment_due", participant["price"]),
                        participant.get("source", "internal"),
                        participant.get("source_ref"),
                        participant.get("sales_partner_id"),
                        reusable_booking_participant["id"], item_id,
                    ),
                )
                if old_client_id != client_id:
                    db.execute(
                        "UPDATE schedule_participant_addons SET client_id = ? "
                        "WHERE schedule_item_id = ? AND client_id = ?",
                        (client_id, item_id, old_client_id),
                    )
            else:
                db.execute(
                    "INSERT INTO schedule_participants "
                    "(schedule_item_id, client_id, client_name, client_phone, "
                    "guests_count, price, prepayment, payment_due, created_at, "
                    "source, source_ref, sales_partner_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item_id, client_id, participant["client_name"],
                        participant["client_phone"], participant["guests_count"],
                        participant["price"], participant.get("prepayment", 0),
                        participant.get("payment_due", participant["price"]), timestamp,
                        participant.get("source", "internal"),
                        participant.get("source_ref"),
                        participant.get("sales_partner_id"),
                    ),
                )
            if auto_add_products and client_id not in existing_client_ids:
                for product in auto_add_products:
                    db.execute(
                        "INSERT OR IGNORE INTO schedule_participant_addons "
                        "(schedule_item_id, client_id, product_id, quantity, "
                        "auto_added, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                        (
                            item_id, client_id, product["id"],
                            participant["guests_count"], timestamp,
                        ),
                    )
        db.commit()
        return item_id
    except Exception:
        db.rollback()
        raise


def move_item(
    db,
    item_id,
    starts_at,
    ends_at,
    source_employee_id,
    target_employee,
    target_role,
    timestamp,
):
    """Move a trip in time and optionally replace one crew assignment."""
    try:
        cursor = db.execute(
            "UPDATE schedule_items SET starts_at = ?, ends_at = ?, updated_at = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (starts_at, ends_at, timestamp, item_id),
        )
        if cursor.rowcount == 0:
            db.rollback()
            return False
        target_employee_id = target_employee["id"]
        if source_employee_id != target_employee_id:
            if source_employee_id == 0:
                db.execute(
                    "INSERT INTO schedule_assignments "
                    "(schedule_item_id, employee_id, employee_name, role, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        item_id, target_employee_id, target_employee["name"],
                        target_role, timestamp,
                    ),
                )
            else:
                assignment = db.execute(
                    "UPDATE schedule_assignments SET employee_id = ?, "
                    "employee_name = ? WHERE schedule_item_id = ? "
                    "AND employee_id = ?",
                    (
                        target_employee_id, target_employee["name"], item_id,
                        source_employee_id,
                    ),
                )
                if assignment.rowcount == 0:
                    db.rollback()
                    return False
            db.execute(
                "INSERT OR IGNORE INTO schedule_day_crew "
                "(work_date, employee_id, created_at) VALUES (?, ?, ?)",
                (starts_at[:10], target_employee_id, timestamp),
            )
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def soft_delete_item(db, item_id, timestamp):
    cursor = db.execute(
        "UPDATE schedule_items SET deleted_at = ?, updated_at = ? "
        "WHERE id = ? AND deleted_at IS NULL",
        (timestamp, timestamp, item_id),
    )
    db.commit()
    return cursor.rowcount > 0


def list_item_participants_with_addons(db, item_id):
    """Participants of one item, each carrying its addon purchases — what
    the schedule modal re-fetches after a manual add/remove so it can
    refresh in place without reloading the whole day."""
    participants = db.execute(
        "SELECT schedule_participants.*, sales_partner.client_name AS sales_partner_name, "
        "own_client.preferred_contact_method AS preferred_contact_method "
        "FROM schedule_participants "
        "LEFT JOIN clients AS sales_partner "
        "ON sales_partner.id = schedule_participants.sales_partner_id "
        "LEFT JOIN clients AS own_client "
        "ON own_client.id = schedule_participants.client_id "
        "WHERE schedule_participants.schedule_item_id = ? "
        "ORDER BY schedule_participants.id",
        (item_id,),
    ).fetchall()
    addons_by_client = {}
    for row in db.execute(
        "SELECT schedule_participant_addons.*, "
        "excursion_addon_products.name AS product_name, "
        "excursion_addon_products.sale_price AS unit_price "
        "FROM schedule_participant_addons "
        "JOIN excursion_addon_products "
        "ON excursion_addon_products.id = schedule_participant_addons.product_id "
        "WHERE schedule_participant_addons.schedule_item_id = ? "
        "ORDER BY schedule_participant_addons.id",
        (item_id,),
    ).fetchall():
        addons_by_client.setdefault(row["client_id"], []).append(dict(row))
    payments_by_participant = {}
    for row in db.execute(
        "SELECT * FROM schedule_yookassa_payments "
        "WHERE schedule_item_id = ? ORDER BY id DESC",
        (item_id,),
    ).fetchall():
        payments_by_participant.setdefault(row["participant_id"], []).append(dict(row))
    manual_payments_by_participant = {}
    for row in db.execute(
        "SELECT * FROM schedule_manual_payments "
        "WHERE schedule_item_id = ? ORDER BY id DESC",
        (item_id,),
    ).fetchall():
        manual_payments_by_participant.setdefault(
            row["participant_id"], []
        ).append(dict(row))
    attach_receipt_summaries(db, manual_payments_by_participant, payments_by_participant)
    result = []
    for participant in participants:
        participant = dict(participant)
        participant["addons"] = addons_by_client.get(participant["client_id"], [])
        participant["payments"] = payments_by_participant.get(participant["id"], [])
        participant["manual_payments"] = manual_payments_by_participant.get(
            participant["id"], []
        )
        participant["paid_manual"] = round(sum(
            payment["amount"] for payment in participant["manual_payments"]
        ), 2)
        result.append(participant)
    return result


def is_participant_of_item(db, item_id, client_id):
    return db.execute(
        "SELECT 1 FROM schedule_participants "
        "WHERE schedule_item_id = ? AND client_id = ?",
        (item_id, client_id),
    ).fetchone() is not None


def add_participant_addon(db, item_id, client_id, product_id, quantity, timestamp):
    """Manual add — increases quantity if this product is already attached
    to this participant rather than erroring on the UNIQUE constraint."""
    existing = db.execute(
        "SELECT id, quantity FROM schedule_participant_addons "
        "WHERE schedule_item_id = ? AND client_id = ? AND product_id = ?",
        (item_id, client_id, product_id),
    ).fetchone()
    if existing is not None:
        db.execute(
            "UPDATE schedule_participant_addons SET quantity = ? WHERE id = ?",
            (existing["quantity"] + quantity, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO schedule_participant_addons "
            "(schedule_item_id, client_id, product_id, quantity, auto_added, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (item_id, client_id, product_id, quantity, timestamp),
        )
    db.commit()


def remove_participant_addon(db, addon_id, item_id):
    cursor = db.execute(
        "DELETE FROM schedule_participant_addons WHERE id = ? AND schedule_item_id = ?",
        (addon_id, item_id),
    )
    db.commit()
    return cursor.rowcount > 0


def create_yookassa_payment_row(
    db, item_id, participant_id, yookassa_payment_id, amount, status,
    confirmation_url, timestamp,
):
    cursor = db.execute(
        "INSERT INTO schedule_yookassa_payments "
        "(schedule_item_id, participant_id, yookassa_payment_id, amount, status, "
        "confirmation_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, participant_id, yookassa_payment_id, amount, status,
         confirmation_url, timestamp, timestamp),
    )
    db.commit()
    return cursor.lastrowid


def get_yookassa_payment(db, payment_id, participant_id):
    return db.execute(
        "SELECT * FROM schedule_yookassa_payments WHERE id = ? AND participant_id = ?",
        (payment_id, participant_id),
    ).fetchone()


def get_yookassa_payment_by_remote_id(db, yookassa_payment_id):
    return db.execute(
        "SELECT * FROM schedule_yookassa_payments WHERE yookassa_payment_id = ?",
        (yookassa_payment_id,),
    ).fetchone()


def update_yookassa_payment_status(db, payment_id, status, timestamp):
    db.execute(
        "UPDATE schedule_yookassa_payments SET status = ?, updated_at = ? WHERE id = ?",
        (status, timestamp, payment_id),
    )


def save_yookassa_receipt(db, payment_id, status, receipt_json, timestamp):
    db.execute(
        "UPDATE schedule_yookassa_payments SET receipt_status = ?, "
        "receipt_json = COALESCE(?, receipt_json), receipt_checked_at = ? WHERE id = ?",
        (status, receipt_json, timestamp, payment_id),
    )


def list_payments_needing_receipt(db, since_timestamp):
    """Succeeded link payments whose fiscal receipt isn't stored yet —
    registration in the cash desk is asynchronous, so they are polled."""
    return db.execute(
        "SELECT * FROM schedule_yookassa_payments "
        "WHERE status = 'succeeded' AND receipt_status != 'succeeded' "
        "AND updated_at >= ? ORDER BY id",
        (since_timestamp,),
    ).fetchall()


def apply_yookassa_payment(db, payment_id, participant_id, amount):
    """First-time-succeeded side effect — reduces the participant's
    remaining balance and flags the payment as applied so a later webhook
    call or manual "Проверить" click can't double-count it."""
    db.execute(
        "UPDATE schedule_participants SET paid_online = paid_online + ? WHERE id = ?",
        (amount, participant_id),
    )
    db.execute(
        "UPDATE schedule_yookassa_payments SET applied = 1 WHERE id = ?",
        (payment_id,),
    )


def delete_yookassa_payment_row(db, payment_id, participant_id):
    cursor = db.execute(
        "DELETE FROM schedule_yookassa_payments WHERE id = ? AND participant_id = ?",
        (payment_id, participant_id),
    )
    db.commit()
    return cursor.rowcount > 0


def create_manual_payment_row(
    db, item_id, participant_id, amount, payment_method, timestamp,
):
    cursor = db.execute(
        "INSERT INTO schedule_manual_payments "
        "(schedule_item_id, participant_id, amount, payment_method, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, participant_id, amount, payment_method, timestamp),
    )
    db.commit()
    return cursor.lastrowid


def get_manual_payment(db, payment_id, participant_id, item_id):
    return db.execute(
        "SELECT * FROM schedule_manual_payments "
        "WHERE id = ? AND participant_id = ? AND schedule_item_id = ?",
        (payment_id, participant_id, item_id),
    ).fetchone()


def attach_receipt_summaries(db, manual_by_participant, online_by_participant):
    """Adds a small `receipt` dict to every manual payment (ModulKassa status
    and whether the fiscal data for the PDF has arrived) and marks succeeded
    online payments as having a payment confirmation to download."""
    manual_ids = [
        payment["id"] for payments in manual_by_participant.values() for payment in payments
    ]
    latest = {}
    if manual_ids:
        placeholders = ",".join("?" for _id in manual_ids)
        for row in db.execute(
            "SELECT * FROM schedule_modulkassa_receipts "
            f"WHERE payment_id IN ({placeholders}) ORDER BY id",
            tuple(manual_ids),
        ).fetchall():
            latest[row["payment_id"]] = row  # newest wins (ORDER BY id)
    for payments in manual_by_participant.values():
        for payment in payments:
            row = latest.get(payment["id"])
            if row is None:
                payment["receipt"] = None
                continue
            payment["receipt"] = {
                "status": (row["status"] or "").lower(),
                "failure": bool(row["failure_message"]),
                "pdf_ready": bool(row["fiscal_info_json"]) and (row["status"] or "").lower()
                in RECEIPT_SUCCESS_STATUSES,
                "version": receipt_version(row["fiscal_info_json"] or row["doc_id"]),
            }
    for payments in online_by_participant.values():
        for payment in payments:
            payment["receipt"] = {
                "status": payment.get("receipt_status") or "",
                "failure": False,
                "pdf_ready": bool(payment.get("receipt_json"))
                and payment.get("receipt_status") == "succeeded",
                "version": receipt_version(payment.get("receipt_json") or ""),
            }
            # Keep the raw fiscal payload out of the JSON sent to the browser.
            payment.pop("receipt_json", None)


RECEIPT_SUCCESS_STATUSES = ("printed", "wait_for_callback", "completed")


def receipt_version(payload):
    """Short fingerprint of a receipt's fiscal data. Added to the PDF URL so
    a phone's PDF viewer (which caches a document by its URL, whatever the
    Cache-Control header says) can never show an older rendering."""
    return hashlib.sha1(str(payload or "").encode("utf-8")).hexdigest()[:8]


def attach_and_get_version(db, kind, payment):
    """receipt_version() of the payment's current receipt data."""
    if kind == "manual":
        row = get_latest_receipt(db, payment["id"], only_successful=True)
        return receipt_version(row["fiscal_info_json"] if row else "")
    return receipt_version(payment["receipt_json"])


def get_manual_payment_context(db, payment_id):
    """One manual payment with the trip and client it belongs to — the
    facts a receipt needs."""
    return db.execute(
        "SELECT pay.*, item.service_name, item.starts_at, item.ends_at, "
        "participant.client_name, participant.client_phone, participant.client_id, "
        "clients.token AS client_token "
        "FROM schedule_manual_payments pay "
        "JOIN schedule_items item ON item.id = pay.schedule_item_id "
        "JOIN schedule_participants participant ON participant.id = pay.participant_id "
        "LEFT JOIN clients ON clients.id = participant.client_id "
        "WHERE pay.id = ?",
        (payment_id,),
    ).fetchone()


def get_online_payment_context(db, payment_id):
    return db.execute(
        "SELECT pay.*, item.service_name, item.starts_at, item.ends_at, "
        "participant.client_name, participant.client_phone, participant.client_id, "
        "clients.token AS client_token "
        "FROM schedule_yookassa_payments pay "
        "JOIN schedule_items item ON item.id = pay.schedule_item_id "
        "JOIN schedule_participants participant ON participant.id = pay.participant_id "
        "LEFT JOIN clients ON clients.id = participant.client_id "
        "WHERE pay.id = ?",
        (payment_id,),
    ).fetchone()


def get_latest_receipt(db, payment_id, only_successful=False):
    query = "SELECT * FROM schedule_modulkassa_receipts WHERE payment_id = ?"
    if only_successful:
        query += (
            " AND LOWER(status) IN ('printed', 'wait_for_callback', 'completed')"
            " AND fiscal_info_json IS NOT NULL"
        )
    return db.execute(query + " ORDER BY id DESC LIMIT 1", (payment_id,)).fetchone()


def delete_manual_payment(db, payment_id, participant_id, item_id):
    db.execute(
        "DELETE FROM schedule_modulkassa_receipts WHERE payment_id IN "
        "(SELECT id FROM schedule_manual_payments "
        "WHERE id = ? AND participant_id = ? AND schedule_item_id = ?)",
        (payment_id, participant_id, item_id),
    )
    cursor = db.execute(
        "DELETE FROM schedule_manual_payments "
        "WHERE id = ? AND participant_id = ? AND schedule_item_id = ?",
        (payment_id, participant_id, item_id),
    )
    db.commit()
    return cursor.rowcount > 0


def _recompute_item_totals(db, item_id, timestamp):
    """participants_count/revenue on schedule_items are a cached rollup of
    the participant rows — every direct add/edit/remove of a participant
    (outside the whole-trip save_item path, which computes and writes
    these itself) has to refresh them or the day view's totals go stale."""
    row = db.execute(
        "SELECT COALESCE(SUM(guests_count), 0) AS guests, "
        "COALESCE(SUM(price), 0) AS revenue "
        "FROM schedule_participants WHERE schedule_item_id = ?",
        (item_id,),
    ).fetchone()
    item = db.execute(
        "SELECT kind FROM schedule_items WHERE id = ?", (item_id,)
    ).fetchone()
    booking_participant = None
    if item is not None and item["kind"] == "booking":
        booking_participant = db.execute(
            "SELECT client_name, client_phone, guests_count "
            "FROM schedule_participants WHERE schedule_item_id = ? "
            "ORDER BY id LIMIT 1",
            (item_id,),
        ).fetchone()
    booking_name = (
        booking_participant["client_name"] if booking_participant else None
    )
    db.execute(
        "UPDATE schedule_items SET participants_count = ?, revenue = ?, "
        "customer_name = CASE WHEN ? IS NULL THEN customer_name ELSE ? END, "
        "customer_phone = CASE WHEN ? IS NULL THEN customer_phone ELSE ? END, "
        "guests_count = CASE WHEN ? IS NULL THEN guests_count ELSE ? END, "
        "updated_at = ? "
        "WHERE id = ?",
        (
            row["guests"], row["revenue"],
            booking_name,
            booking_name,
            booking_name,
            booking_participant["client_phone"] if booking_participant else None,
            booking_name,
            booking_participant["guests_count"] if booking_participant else None,
            timestamp, item_id,
        ),
    )


def _auto_add_addons_for_participant(db, item_id, service_id, client_id, guests_count, timestamp):
    """Same rule save_item applies to a newly-added participant, but for
    the one-participant-at-a-time quick-add path."""
    if service_id is None:
        return
    for product in db.execute(
        "SELECT id FROM excursion_addon_products WHERE auto_add_service_id = ?",
        (service_id,),
    ).fetchall():
        db.execute(
            "INSERT OR IGNORE INTO schedule_participant_addons "
            "(schedule_item_id, client_id, product_id, quantity, auto_added, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (item_id, client_id, product["id"], guests_count, timestamp),
        )


def add_participant(db, item_id, client_id, name, phone, guests_count, price, timestamp):
    """Quick-add: one participant, attached to an already-saved item. Client
    is reused by id when the picker matched one, else created fresh —
    mirrors the client-resolution save_item does for the whole-trip form."""
    resolved_client_id = client_id
    if resolved_client_id is None:
        cursor = db.execute(
            "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
            "VALUES (?, '', ?, ?, ?)",
            (name, phone, secrets.token_urlsafe(16), timestamp),
        )
        resolved_client_id = cursor.lastrowid
    ensure_segment(db, resolved_client_id, EXCURSION_SEGMENT, timestamp)
    cursor = db.execute(
        "INSERT INTO schedule_participants "
        "(schedule_item_id, client_id, client_name, client_phone, guests_count, price, "
        "prepayment, payment_due, created_at, source, source_ref, sales_partner_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 'internal', NULL, NULL)",
        (item_id, resolved_client_id, name, phone, guests_count, price, price, timestamp),
    )
    participant_id = cursor.lastrowid
    item = db.execute(
        "SELECT service_id FROM schedule_items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is not None:
        _auto_add_addons_for_participant(
            db, item_id, item["service_id"], resolved_client_id, guests_count, timestamp
        )
    _recompute_item_totals(db, item_id, timestamp)
    db.commit()
    return participant_id


def add_external_participant(
    db, item_id, client_id, name, phone, guests_count, price, timestamp,
    source, source_ref,
):
    """Insert one idempotent participant from a trusted external API.

    The caller owns the transaction so the last-seat check and this insert
    stay atomic.  This mirrors ``add_participant`` without committing and
    records a stable source reference for safe retries.
    """
    resolved_client_id = client_id
    if resolved_client_id is None:
        cursor = db.execute(
            "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
            "VALUES (?, '', ?, ?, ?)",
            (name, phone, secrets.token_urlsafe(16), timestamp),
        )
        resolved_client_id = cursor.lastrowid
    ensure_segment(db, resolved_client_id, EXCURSION_SEGMENT, timestamp)
    cursor = db.execute(
        "INSERT INTO schedule_participants "
        "(schedule_item_id, client_id, client_name, client_phone, guests_count, price, "
        "prepayment, payment_due, created_at, source, source_ref, sales_partner_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL)",
        (
            item_id, resolved_client_id, name, phone, guests_count, price, price,
            timestamp, source, source_ref,
        ),
    )
    item = db.execute(
        "SELECT service_id FROM schedule_items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is not None:
        _auto_add_addons_for_participant(
            db, item_id, item["service_id"], resolved_client_id, guests_count, timestamp
        )
    _recompute_item_totals(db, item_id, timestamp)
    return cursor.lastrowid


def get_participant(db, participant_id, item_id):
    return db.execute(
        "SELECT * FROM schedule_participants WHERE id = ? AND schedule_item_id = ?",
        (participant_id, item_id),
    ).fetchone()


def update_participant(db, participant_id, item_id, data, timestamp):
    row = db.execute(
        "SELECT * FROM schedule_participants WHERE id = ? AND schedule_item_id = ?",
        (participant_id, item_id),
    ).fetchone()
    if row is None:
        return False
    # A Tripster booking's payment_due tracks a real external payment
    # state — editing the guest/price fields here shouldn't silently
    # overwrite it the way it's reset for a plain internal record.
    payment_due = data["price"] if row["source"] != "tripster" else row["payment_due"]
    db.execute(
        "UPDATE schedule_participants SET client_name = ?, client_phone = ?, "
        "guests_count = ?, price = ?, payment_due = ?, sales_partner_id = ? WHERE id = ?",
        (
            data["client_name"], data["client_phone"], data["guests_count"],
            data["price"], payment_due, data["sales_partner_id"], participant_id,
        ),
    )
    db.execute(
        "UPDATE clients SET preferred_contact_method = ? WHERE id = ?",
        (data["preferred_contact_method"], row["client_id"]),
    )
    _recompute_item_totals(db, item_id, timestamp)
    db.commit()
    return True


def delete_participant(db, participant_id, item_id, timestamp):
    row = db.execute(
        "SELECT client_id FROM schedule_participants WHERE id = ? AND schedule_item_id = ?",
        (participant_id, item_id),
    ).fetchone()
    if row is None:
        return False
    db.execute(
        "DELETE FROM schedule_participant_addons WHERE schedule_item_id = ? AND client_id = ?",
        (item_id, row["client_id"]),
    )
    db.execute("DELETE FROM schedule_participants WHERE id = ?", (participant_id,))
    _recompute_item_totals(db, item_id, timestamp)
    db.commit()
    return True
