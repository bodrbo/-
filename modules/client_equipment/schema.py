"""SQLite schema of the client's equipment."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS client_equipment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'boat',
            name TEXT NOT NULL,
            serial_number TEXT NOT NULL DEFAULT '',
            year INTEGER,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_equipment_client ON client_equipment (client_id)"
    )
