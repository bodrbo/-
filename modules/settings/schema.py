"""SQLite schema for the admin-editable system settings key/value store."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
