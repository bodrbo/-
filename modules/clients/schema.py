"""SQLite schema and data backfill for client business segments."""

import secrets


def _migrate_clients_table(conn):
    """Make phone optional/non-unique while preserving stable local IDs."""
    columns = {
        row[1]: row for row in conn.execute("PRAGMA table_info(clients)").fetchall()
    }
    table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'clients'"
    ).fetchone()
    table_sql = (table_sql_row[0] if table_sql_row else "").upper()
    phone_is_required = bool(columns.get("phone") and columns["phone"][3])
    phone_is_unique = "PHONE TEXT NOT NULL UNIQUE" in table_sql
    if phone_is_required or phone_is_unique:
        conn.execute("DROP TABLE IF EXISTS clients_phone_optional_migration")
        conn.execute(
            """
            CREATE TABLE clients_phone_optional_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name TEXT NOT NULL,
                boat_model TEXT NOT NULL DEFAULT '',
                phone TEXT DEFAULT '',
                token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'neutral',
                created_at TEXT NOT NULL,
                yclients_client_id INTEGER,
                email TEXT NOT NULL DEFAULT '',
                birth_date TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                yclients_last_change_date TEXT NOT NULL DEFAULT ''
            )
            """
        )
        destination_columns = (
            "id", "client_name", "boat_model", "phone", "token", "status",
            "created_at", "yclients_client_id", "email", "birth_date",
            "comment", "yclients_last_change_date",
        )
        defaults = {
            "boat_model": "''",
            "phone": "''",
            "status": "'neutral'",
            "yclients_client_id": "NULL",
            "email": "''",
            "birth_date": "''",
            "comment": "''",
            "yclients_last_change_date": "''",
        }
        select_expressions = [
            column_name if column_name in columns else defaults[column_name]
            for column_name in destination_columns
        ]
        conn.execute(
            "INSERT INTO clients_phone_optional_migration "
            f"({', '.join(destination_columns)}) "
            f"SELECT {', '.join(select_expressions)} FROM clients"
        )
        conn.execute("DROP TABLE clients")
        conn.execute(
            "ALTER TABLE clients_phone_optional_migration RENAME TO clients"
        )

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(clients)").fetchall()
    }
    additions = (
        ("yclients_client_id", "INTEGER"),
        ("email", "TEXT NOT NULL DEFAULT ''"),
        ("birth_date", "TEXT NOT NULL DEFAULT ''"),
        ("comment", "TEXT NOT NULL DEFAULT ''"),
        ("yclients_last_change_date", "TEXT NOT NULL DEFAULT ''"),
    )
    for column_name, definition in additions:
        if column_name not in columns:
            conn.execute(
                f"ALTER TABLE clients ADD COLUMN {column_name} {definition}"
            )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_yclients_id "
        "ON clients(yclients_client_id) WHERE yclients_client_id IS NOT NULL"
    )


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
    _migrate_clients_table(conn)
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
