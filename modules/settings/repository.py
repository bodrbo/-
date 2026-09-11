"""Persistence for the system_settings key/value store.

Plain select-then-insert/update rather than an upsert (``INSERT ...
ON CONFLICT``) — this hosting has a documented history of that syntax
misbehaving in production, so the codebase avoids it everywhere.
"""


def get_value(db, key, default=""):
    row = db.execute(
        "SELECT value FROM system_settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row is not None else default


def get_all(db):
    return {
        row["key"]: row["value"]
        for row in db.execute("SELECT key, value FROM system_settings").fetchall()
    }


def set_value(db, key, value, timestamp):
    existing = db.execute(
        "SELECT 1 FROM system_settings WHERE key = ?", (key,)
    ).fetchone()
    if existing is not None:
        db.execute(
            "UPDATE system_settings SET value = ?, updated_at = ? WHERE key = ?",
            (value, timestamp, key),
        )
    else:
        db.execute(
            "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, timestamp),
        )
