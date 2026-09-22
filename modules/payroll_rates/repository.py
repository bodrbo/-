"""SQL access for payroll pay rates."""


def list_excursion_role_rates(db):
    return db.execute(
        "SELECT role, rate, updated_at FROM excursion_role_rates ORDER BY role"
    ).fetchall()


def get_excursion_role_rate(db, role):
    row = db.execute(
        "SELECT rate FROM excursion_role_rates WHERE role = ?", (role,)
    ).fetchone()
    return row["rate"] if row else None


def set_excursion_role_rate(db, role, rate, timestamp):
    db.execute(
        "UPDATE excursion_role_rates SET rate = ?, updated_at = ? WHERE role = ?",
        (rate, timestamp, role),
    )
    db.commit()
