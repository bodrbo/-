"""SQL access for payroll pay rates."""

from .constants import BOAT_RATE_ROLES


def list_excursion_role_rates(db):
    return db.execute(
        "SELECT role, rate, updated_at FROM excursion_role_rates ORDER BY role"
    ).fetchall()


def get_excursion_role_rate(db, role, boat=None):
    """The role's hourly rate; for a role with boat rates and a boat that has
    its own (non-zero) rate — that one. A boat without one falls back to the
    role's general rate."""
    if boat and role in BOAT_RATE_ROLES:
        boat_row = db.execute(
            "SELECT rate FROM excursion_boat_rates WHERE role = ? AND boat = ?", (role, boat)
        ).fetchone()
        if boat_row and boat_row["rate"]:
            return boat_row["rate"]
    row = db.execute(
        "SELECT rate FROM excursion_role_rates WHERE role = ?", (role,)
    ).fetchone()
    return row["rate"] if row else None


def list_boat_rates(db, role):
    """{boat: rate} of the boats that have a rate row for the role."""
    return {
        row["boat"]: row["rate"]
        for row in db.execute(
            "SELECT boat, rate FROM excursion_boat_rates WHERE role = ?", (role,)
        ).fetchall()
    }


def set_boat_rates(db, role, rates, timestamp):
    """rates: {boat: rate}; 0 means "use the role's general rate"."""
    for boat, rate in rates.items():
        cursor = db.execute(
            "UPDATE excursion_boat_rates SET rate = ?, updated_at = ? WHERE role = ? AND boat = ?",
            (rate, timestamp, role, boat),
        )
        if cursor.rowcount == 0:
            db.execute(
                "INSERT INTO excursion_boat_rates (role, boat, rate, updated_at) VALUES (?, ?, ?, ?)",
                (role, boat, rate, timestamp),
            )
    db.commit()


def boat_rates_applied(db, role):
    return db.execute(
        "SELECT 1 FROM excursion_boat_rates_applied WHERE role = ?", (role,)
    ).fetchone() is not None


def mark_boat_rates_applied(db, role, timestamp):
    db.execute(
        "INSERT INTO excursion_boat_rates_applied (role, applied_at) VALUES (?, ?)",
        (role, timestamp),
    )
    db.commit()


def set_excursion_role_rate(db, role, rate, timestamp):
    db.execute(
        "UPDATE excursion_role_rates SET rate = ?, updated_at = ? WHERE role = ?",
        (rate, timestamp, role),
    )
    db.commit()
