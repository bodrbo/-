"""SQLite schema for admin-editable payroll pay rates."""

import datetime as dt

from .constants import DEFAULT_EXCURSION_ROLE_RATES, EXCURSION_ROLES


def init_schema(conn):
    rates_is_new = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'excursion_role_rates'"
    ).fetchone() is None
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS excursion_role_rates (
            role TEXT PRIMARY KEY,
            rate REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    if rates_is_new:
        timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.executemany(
            "INSERT INTO excursion_role_rates (role, rate, updated_at) VALUES (?, ?, ?)",
            [
                (role, DEFAULT_EXCURSION_ROLE_RATES.get(role, 0), timestamp)
                for role in EXCURSION_ROLES
            ],
        )
