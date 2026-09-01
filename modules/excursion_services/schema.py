"""SQLite schema and bootstrap migration for excursion products."""

import datetime as dt

from .constants import DEFAULT_SERVICES


def init_schema(conn):
    catalog_is_new = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'excursion_services'"
    ).fetchone() is None
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS excursion_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            duration_hours REAL NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    if catalog_is_new:
        timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.executemany(
            "INSERT INTO excursion_services "
            "(name, duration_hours, price, created_at, updated_at) "
            "VALUES (?, ?, 0, ?, ?)",
            [
                (name, duration_hours, timestamp, timestamp)
                for name, duration_hours in DEFAULT_SERVICES
            ],
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_excursion_services_name "
        "ON excursion_services(name COLLATE NOCASE)"
    )
