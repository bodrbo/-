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
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
        """
    )
    columns = [row[1] for row in conn.execute("PRAGMA table_info(client_equipment)").fetchall()]
    if "status" not in columns:
        conn.execute("ALTER TABLE client_equipment ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_equipment_client ON client_equipment (client_id)"
    )
