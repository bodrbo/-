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


def _quoted_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def rename_vessel_references(conn, old_name, new_name):
    """Move references between vessel names across all application tables."""
    reference_columns = {"boat", "boat_name", "source_boat", "destination_boat"}
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' AND name != 'fleet_vessels'"
    ).fetchall()
    for table_row in tables:
        table = table_row[0]
        columns = conn.execute(
            f"PRAGMA table_info({_quoted_identifier(table)})"
        ).fetchall()
        for column in (row[1] for row in columns if row[1] in reference_columns):
            conn.execute(
                f"UPDATE {_quoted_identifier(table)} "
                f"SET {_quoted_identifier(column)} = ? "
                f"WHERE {_quoted_identifier(column)} = ?",
                (new_name, old_name),
            )


def init_schema(conn, refresh_runtime=True):
    """Create the editable fleet directory and seed the former static fleet."""
    table_is_new = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'fleet_vessels'"
    ).fetchone() is None
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
    has_any_rows = conn.execute(
        "SELECT 1 FROM fleet_vessels LIMIT 1"
    ).fetchone() is not None
    # Defaults are bootstrap data, not reference data.  Re-applying them on
    # every request resurrected a vessel's old name after it was renamed.
    if table_is_new or not has_any_rows:
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
    _remove_resurrected_defaults(conn)
    # This initializer also runs from the fleet blueprint on an existing
    # tenant connection.  Persist one-time repairs immediately; otherwise a
    # read-only GET would roll them back when Flask closes the connection.
    conn.commit()
    if refresh_runtime:
        refresh_runtime_fleet(conn)


def _remove_resurrected_defaults(conn):
    """Remove default-name rows accidentally re-seeded after an earlier rename.

    A renamed bootstrap vessel keeps its original sort_order and lower id.
    The buggy request-time seeder created a second, newer row with the old
    default name in that same slot.  This signature lets us repair existing
    demo databases without guessing from names alone.  These rows are bootstrap
    artefacts rather than user records, so deleting them also releases the
    unique name if the original vessel is ever renamed back to its default.
    """
    for position, boat in enumerate(DEFAULT_BOATS):
        duplicate = conn.execute(
            "SELECT id, sort_order FROM fleet_vessels WHERE name = ? COLLATE NOCASE "
            "AND deleted_at IS NULL",
            (boat["name"],),
        ).fetchone()
        if duplicate is None or duplicate[1] != position:
            continue
        older_slot = conn.execute(
            "SELECT id, name FROM fleet_vessels "
            "WHERE sort_order = ? AND id < ? ORDER BY id LIMIT 1",
            (position, duplicate[0]),
        ).fetchone()
        if older_slot is not None:
            rename_vessel_references(conn, boat["name"], older_slot[1])
            conn.execute(
                "DELETE FROM fleet_vessels WHERE id = ?",
                (duplicate[0],),
            )


def boats_for_db(conn):
    """Return the active fleet owned by this exact database connection."""
    rows = conn.execute(
        "SELECT name, investor, commission_direct, commission_aggregator, "
        "fuel_cost, mooring_cost FROM fleet_vessels "
        "WHERE deleted_at IS NULL ORDER BY sort_order, id"
    ).fetchall()
    return [
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


def fuel_config_for_db(conn):
    rows = conn.execute(
        "SELECT name, tank_capacity_liters, group_trip_liters "
        "FROM fleet_vessels WHERE deleted_at IS NULL ORDER BY sort_order, id"
    ).fetchall()
    return {
        row[0]: {
            "capacity_liters": float(row[1] or 0),
            "group_trip_liters": float(row[2] or 0),
        }
        for row in rows
    }


def schedule_colors_for_db(conn):
    return {
        row[0]: row[1] or "#607d8b"
        for row in conn.execute(
            "SELECT name, schedule_color FROM fleet_vessels "
            "WHERE deleted_at IS NULL ORDER BY sort_order, id"
        ).fetchall()
    }


def refresh_runtime_fleet(conn):
    """Refresh imported legacy constants without replacing their objects."""
    BOATS[:] = boats_for_db(conn)
    FUEL_CONFIG.clear()
    FUEL_CONFIG.update(fuel_config_for_db(conn))
    SCHEDULE_BOAT_COLORS.clear()
    SCHEDULE_BOAT_COLORS.update(schedule_colors_for_db(conn))
