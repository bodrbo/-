"""SQLite schema and data backfill for client business segments."""

import secrets


def _phone_identity(phone):
    digits = "".join(character for character in str(phone or "") if character.isdigit())
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def _backfill_booking_participants(conn):
    """Link individual bookings created before named participants existed."""
    clients_by_phone = {}
    for client in conn.execute(
        "SELECT id, phone FROM clients ORDER BY id"
    ).fetchall():
        identity = _phone_identity(client[1])
        if identity:
            clients_by_phone.setdefault(identity, []).append(client[0])

    bookings = conn.execute(
        "SELECT si.id, si.customer_name, si.customer_phone, si.created_at "
        "FROM schedule_items si WHERE si.kind = 'booking' "
        "AND si.deleted_at IS NULL AND TRIM(si.customer_name) != '' "
        "AND TRIM(si.customer_phone) != '' "
        "AND NOT EXISTS (SELECT 1 FROM schedule_participants sp "
        " WHERE sp.schedule_item_id = si.id)"
    ).fetchall()
    for booking in bookings:
        identity = _phone_identity(booking[2])
        if len(identity) < 7:
            continue
        matches = clients_by_phone.get(identity, [])
        if len(matches) > 1:
            continue
        if matches:
            client_id = matches[0]
        else:
            cursor = conn.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES (?, '', ?, ?, ?)",
                (booking[1], booking[2], secrets.token_urlsafe(16), booking[3]),
            )
            client_id = cursor.lastrowid
            clients_by_phone[identity] = [client_id]
        conn.execute(
            "INSERT INTO schedule_participants "
            "(schedule_item_id, client_id, client_name, client_phone, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (booking[0], client_id, booking[1], booking[2], booking[3]),
        )


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS client_segments (
            client_id INTEGER NOT NULL,
            segment TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (client_id, segment)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_segments_segment "
        "ON client_segments(segment, client_id)"
    )

    _backfill_booking_participants(conn)

    # Classify existing production data from its actual relations. This also
    # handles clients created by the schedule feature before segments existed.
    conn.execute(
        "INSERT OR IGNORE INTO client_segments (client_id, segment, created_at) "
        "SELECT DISTINCT client_id, 'excursion', created_at "
        "FROM schedule_participants WHERE client_id IS NOT NULL"
    )
    conn.execute(
        "INSERT OR IGNORE INTO client_segments (client_id, segment, created_at) "
        "SELECT DISTINCT client_id, 'tuning', created_at "
        "FROM tuning_orders WHERE client_id IS NOT NULL"
    )
    conn.execute(
        "INSERT OR IGNORE INTO client_segments (client_id, segment, created_at) "
        "SELECT DISTINCT owner_client_id, 'tuning', started_at "
        "FROM field_diagnostic_sheets WHERE owner_client_id IS NOT NULL"
    )
    # Unlinked legacy contacts predate the excursion schedule and therefore
    # belong to the historical tuning directory.
    conn.execute(
        "INSERT OR IGNORE INTO client_segments (client_id, segment, created_at) "
        "SELECT clients.id, 'tuning', clients.created_at FROM clients "
        "WHERE NOT EXISTS ("
        " SELECT 1 FROM client_segments "
        " WHERE client_segments.client_id = clients.id"
        ")"
    )
