"""Persistence helpers for the excursion services catalog."""


def list_services(db):
    return [
        dict(row)
        for row in db.execute(
            "SELECT id, name, duration_hours AS hours, price, "
            "created_at, updated_at FROM excursion_services "
            "ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
    ]


def get_service(db, service_id):
    row = db.execute(
        "SELECT id, name, duration_hours AS hours, price, "
        "created_at, updated_at FROM excursion_services WHERE id = ?",
        (service_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def get_service_by_name(db, name):
    # SQLite's built-in NOCASE collation only folds Latin characters. The
    # catalog is Russian, so compare in Python to catch «Средний»/«средний».
    identity = str(name or "").casefold()
    return next(
        (service for service in list_services(db)
         if service["name"].casefold() == identity),
        None,
    )


def create_service(db, data, timestamp):
    try:
        cursor = db.execute(
            "INSERT INTO excursion_services "
            "(name, duration_hours, price, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                data["name"], data["hours"], data["price"],
                timestamp, timestamp,
            ),
        )
        db.commit()
        return cursor.lastrowid
    except Exception:
        db.rollback()
        raise


def update_service(db, service_id, data, timestamp):
    try:
        cursor = db.execute(
            "UPDATE excursion_services SET name = ?, duration_hours = ?, "
            "price = ?, updated_at = ? WHERE id = ?",
            (
                data["name"], data["hours"], data["price"],
                timestamp, service_id,
            ),
        )
        db.commit()
        return cursor.rowcount > 0
    except Exception:
        db.rollback()
        raise
