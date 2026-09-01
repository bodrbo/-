"""Shared operations for client directory membership."""

from .constants import CLIENT_SEGMENTS


def ensure_segment(db, client_id, segment, created_at):
    if segment not in CLIENT_SEGMENTS:
        raise ValueError("Unknown client segment")
    db.execute(
        "INSERT OR IGNORE INTO client_segments (client_id, segment, created_at) "
        "VALUES (?, ?, ?)",
        (client_id, segment, created_at),
    )
