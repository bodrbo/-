"""Persistence helpers for the excursion services catalog."""


def list_services(db):
    services = [
        dict(row)
        for row in db.execute(
            "SELECT id, name, service_type, tripster_id, "
            "duration_hours AS hours, price, "
            "created_at, updated_at FROM excursion_services "
            "ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
    ]
    by_id = {service["id"]: service for service in services}
    for service in services:
        service["boat_prices"] = {}
    for row in db.execute(
        "SELECT service_id, boat, hourly_price "
        "FROM excursion_service_boat_prices ORDER BY service_id, boat"
    ).fetchall():
        service = by_id.get(row["service_id"])
        if service is not None:
            service["boat_prices"][row["boat"]] = row["hourly_price"]
    return services


def get_service(db, service_id):
    return next(
        (service for service in list_services(db) if service["id"] == service_id),
        None,
    )


def get_service_by_name(db, name):
    # SQLite's built-in NOCASE collation only folds Latin characters. The
    # catalog is Russian, so compare in Python to catch «Средний»/«средний».
    identity = str(name or "").casefold()
    return next(
        (service for service in list_services(db)
         if service["name"].casefold() == identity),
        None,
    )


def get_service_by_tripster_id(db, tripster_id):
    if tripster_id is None:
        return None
    return next(
        (
            service for service in list_services(db)
            if service["tripster_id"] == tripster_id
        ),
        None,
    )


def create_service(db, data, timestamp):
    try:
        cursor = db.execute(
            "INSERT INTO excursion_services "
            "(name, service_type, tripster_id, duration_hours, price, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                data["name"], data["service_type"], data["tripster_id"],
                data["hours"], data["price"], timestamp, timestamp,
            ),
        )
        for boat, hourly_price in data["boat_prices"].items():
            db.execute(
                "INSERT INTO excursion_service_boat_prices "
                "(service_id, boat, hourly_price, updated_at) VALUES (?, ?, ?, ?)",
                (cursor.lastrowid, boat, hourly_price, timestamp),
            )
        db.commit()
        return cursor.lastrowid
    except Exception:
        db.rollback()
        raise


def list_addon_products(db):
    return [
        dict(row)
        for row in db.execute(
            "SELECT excursion_addon_products.*, excursion_services.name AS auto_add_service_name "
            "FROM excursion_addon_products "
            "LEFT JOIN excursion_services "
            "ON excursion_services.id = excursion_addon_products.auto_add_service_id "
            "ORDER BY excursion_addon_products.name COLLATE NOCASE"
        ).fetchall()
    ]


def get_addon_product(db, product_id):
    return next(
        (p for p in list_addon_products(db) if p["id"] == product_id), None
    )


def get_addon_product_by_name(db, name):
    identity = str(name or "").casefold()
    return next(
        (p for p in list_addon_products(db) if p["name"].casefold() == identity),
        None,
    )


def create_addon_product(db, data, timestamp):
    cursor = db.execute(
        "INSERT INTO excursion_addon_products "
        "(name, cost_price, sale_price, auto_add_service_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            data["name"], data["cost_price"], data["sale_price"],
            data["auto_add_service_id"], timestamp, timestamp,
        ),
    )
    db.commit()
    return cursor.lastrowid


def update_addon_product(db, product_id, data, timestamp):
    cursor = db.execute(
        "UPDATE excursion_addon_products SET name = ?, cost_price = ?, "
        "sale_price = ?, auto_add_service_id = ?, updated_at = ? WHERE id = ?",
        (
            data["name"], data["cost_price"], data["sale_price"],
            data["auto_add_service_id"], timestamp, product_id,
        ),
    )
    db.commit()
    return cursor.rowcount > 0


def update_service(db, service_id, data, timestamp):
    try:
        cursor = db.execute(
            "UPDATE excursion_services SET name = ?, service_type = ?, "
            "tripster_id = ?, duration_hours = ?, price = ?, updated_at = ? "
            "WHERE id = ?",
            (
                data["name"], data["service_type"], data["tripster_id"],
                data["hours"], data["price"], timestamp, service_id,
            ),
        )
        db.execute(
            "DELETE FROM excursion_service_boat_prices WHERE service_id = ?",
            (service_id,),
        )
        for boat, hourly_price in data["boat_prices"].items():
            db.execute(
                "INSERT INTO excursion_service_boat_prices "
                "(service_id, boat, hourly_price, updated_at) VALUES (?, ?, ?, ?)",
                (service_id, boat, hourly_price, timestamp),
            )
        db.commit()
        return cursor.rowcount > 0
    except Exception:
        db.rollback()
        raise
