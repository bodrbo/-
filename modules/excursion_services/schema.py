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
            service_type TEXT NOT NULL DEFAULT 'group',
            tripster_id INTEGER,
            duration_hours REAL NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(excursion_services)")
    }
    service_type_is_new = "service_type" not in columns
    if service_type_is_new:
        conn.execute(
            "ALTER TABLE excursion_services "
            "ADD COLUMN service_type TEXT NOT NULL DEFAULT 'group'"
        )
        conn.execute(
            "UPDATE excursion_services SET service_type = 'individual' "
            "WHERE lower(name) LIKE '%аренд%' "
            "OR lower(name) LIKE '%индивидуаль%'"
        )
    if "tripster_id" not in columns:
        conn.execute(
            "ALTER TABLE excursion_services ADD COLUMN tripster_id INTEGER"
        )
    if catalog_is_new:
        timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.executemany(
            "INSERT INTO excursion_services "
            "(name, duration_hours, service_type, price, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            [
                (name, duration_hours, service_type, timestamp, timestamp)
                for name, duration_hours, service_type in DEFAULT_SERVICES
            ],
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS excursion_service_boat_prices (
            service_id INTEGER NOT NULL,
            boat TEXT NOT NULL,
            hourly_price REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (service_id, boat)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_excursion_services_name "
        "ON excursion_services(name COLLATE NOCASE)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_excursion_services_type "
        "ON excursion_services(service_type, name COLLATE NOCASE)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_excursion_services_tripster_id "
        "ON excursion_services(tripster_id)"
    )
