"""SQLite schema for the internal operational schedule."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            boat TEXT NOT NULL,
            service_name TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            capacity INTEGER,
            participants_count INTEGER NOT NULL DEFAULT 0,
            customer_name TEXT NOT NULL DEFAULT '',
            customer_phone TEXT NOT NULL DEFAULT '',
            revenue REAL NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'scheduled',
            source TEXT NOT NULL DEFAULT 'internal',
            accounting_trip_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_item_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL,
            employee_name TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(schedule_item_id, employee_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_items_day "
        "ON schedule_items(starts_at, deleted_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_assignments_employee "
        "ON schedule_assignments(employee_id, schedule_item_id)"
    )
