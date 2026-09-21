"""Persistent vessel directory and compatibility views for the fleet domain."""

import datetime as dt

from .constants import (
    BOATS,
    DEFAULT_BOATS,
    DEFAULT_FUEL_CONFIG,
    DEFAULT_SCHEDULE_BOAT_COLORS,
    FUEL_CONFIG,
    SCHEDULE_BOAT_COLORS,
)


def _timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def init_schema(conn, refresh_runtime=True):
    """Create the editable fleet directory and seed the former static fleet."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_vessels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            investor TEXT NOT NULL DEFAULT '',
            commission_direct REAL NOT NULL DEFAULT 30,
            commission_aggregator REAL NOT NULL DEFAULT 30,
            fuel_cost REAL NOT NULL DEFAULT 768,
            mooring_cost REAL NOT NULL DEFAULT 1333,
            tank_capacity_liters REAL NOT NULL DEFAULT 0,
            group_trip_liters REAL NOT NULL DEFAULT 0,
            schedule_color TEXT NOT NULL DEFAULT '#607d8b',
            length_m REAL,
            width_m REAL,
            specifications TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(fleet_vessels)")
    }
    # A Passenger restart can be interrupted after CREATE TABLE but before a
    # deploy finishes.  Keep this migration additive so opening /fleet heals a
    # partially-created table instead of returning an opaque 500 response.
    migrations = {
        "investor": "TEXT NOT NULL DEFAULT ''",
        "commission_direct": "REAL NOT NULL DEFAULT 30",
        "commission_aggregator": "REAL NOT NULL DEFAULT 30",
        "fuel_cost": "REAL NOT NULL DEFAULT 768",
        "mooring_cost": "REAL NOT NULL DEFAULT 1333",
        "tank_capacity_liters": "REAL NOT NULL DEFAULT 0",
        "group_trip_liters": "REAL NOT NULL DEFAULT 0",
        "schedule_color": "TEXT NOT NULL DEFAULT '#607d8b'",
        "length_m": "REAL",
        "width_m": "REAL",
        "specifications": "TEXT NOT NULL DEFAULT ''",
        "sort_order": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
        "deleted_at": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            conn.execute(
                f'ALTER TABLE fleet_vessels ADD COLUMN "{column}" {definition}'
            )
    timestamp = _timestamp()
    for position, boat in enumerate(DEFAULT_BOATS):
        fuel = DEFAULT_FUEL_CONFIG.get(boat["name"], {})
        conn.execute(
            "INSERT OR IGNORE INTO fleet_vessels "
            "(name, investor, commission_direct, commission_aggregator, "
            "fuel_cost, mooring_cost, tank_capacity_liters, group_trip_liters, "
            "schedule_color, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                boat["name"],
                boat["investor"],
                boat["commission_direct"],
                boat["commission_aggregator"],
                boat["fuel"],
                boat["mooring"],
                fuel.get("capacity_liters", 0),
                fuel.get("group_trip_liters", 0),
                DEFAULT_SCHEDULE_BOAT_COLORS.get(boat["name"], "#607d8b"),
                position,
                timestamp,
                timestamp,
            ),
        )
    if refresh_runtime:
        refresh_runtime_fleet(conn)


def refresh_runtime_fleet(conn):
    """Refresh imported legacy constants without replacing their objects."""
    rows = conn.execute(
        "SELECT name, investor, commission_direct, commission_aggregator, "
        "fuel_cost, mooring_cost, tank_capacity_liters, group_trip_liters, "
        "schedule_color FROM fleet_vessels WHERE deleted_at IS NULL "
        "ORDER BY sort_order, id"
    ).fetchall()

    BOATS[:] = [
        {
            "name": row[0],
            "investor": row[1],
            "commission_direct": row[2],
            "commission_aggregator": row[3],
            "fuel": row[4],
            "mooring": row[5],
        }
        for row in rows
    ]

    FUEL_CONFIG.clear()
    FUEL_CONFIG.update(
        {
            row[0]: {
                "capacity_liters": float(row[6] or 0),
                "group_trip_liters": float(row[7] or 0),
            }
            for row in rows
        }
    )

    SCHEDULE_BOAT_COLORS.clear()
    for row in rows:
        color = row[8] or "#607d8b"
        SCHEDULE_BOAT_COLORS[row[0]] = color
