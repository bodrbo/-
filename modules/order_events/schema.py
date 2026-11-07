"""SQLite schema of the order events."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_kind TEXT NOT NULL,
            order_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            event_date TEXT NOT NULL,
            event_time TEXT NOT NULL DEFAULT '',
            duration_minutes INTEGER,
            host_employee TEXT NOT NULL DEFAULT '',
            schedule_task_id INTEGER,
            author_admin_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events (order_kind, order_id)"
    )
