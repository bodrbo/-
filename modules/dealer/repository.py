"""SQL access for the dealer center."""


def catalog_positions(db):
    """Boats and motors of the catalog with their selling price (None when
    not set), boats first, by name."""
    return db.execute(
        "SELECT p.id, p.model_name, p.equipment_type, p.brand, p.power_hp, p.photo_filename, "
        "dealer_prices.price AS price "
        "FROM tuning_boat_profiles p LEFT JOIN dealer_prices ON dealer_prices.profile_id = p.id "
        "WHERE p.equipment_type IN ('boat', 'motor') "
        "ORDER BY p.equipment_type, p.model_name COLLATE NOCASE, p.id"
    ).fetchall()


# Orders that count as "standing" for the price list: a cancelled order's
# models do not keep a position in it.
_IN_ORDERS = (
    "p.id IN (SELECT i.profile_id FROM dealer_order_items i JOIN dealer_orders o ON o.id = i.order_id "
    "WHERE i.profile_id IS NOT NULL AND o.status != 'cancelled')"
)


def price_list_positions(db):
    """The price list: the models that stand in the dealer orders plus those
    added to the list by hand, boats first, by name. `in_orders` = the model
    stands in an order (so it cannot be taken off the list)."""
    return db.execute(
        "SELECT p.id, p.model_name, p.equipment_type, p.brand, p.power_hp, dealer_prices.price AS price, "
        f"{_IN_ORDERS} AS in_orders "
        "FROM tuning_boat_profiles p LEFT JOIN dealer_prices ON dealer_prices.profile_id = p.id "
        "WHERE p.equipment_type IN ('boat', 'motor') "
        f"AND (p.id IN (SELECT profile_id FROM dealer_price_list) OR {_IN_ORDERS}) "
        "ORDER BY p.equipment_type, p.model_name COLLATE NOCASE, p.id"
    ).fetchall()


def in_price_list(db, profile_id):
    return any(row["id"] == profile_id for row in price_list_positions(db))


def add_to_price_list(db, profile_id, timestamp):
    db.execute(
        "INSERT OR IGNORE INTO dealer_price_list (profile_id, added_at) VALUES (?, ?)", (profile_id, timestamp)
    )


def remove_from_price_list(db, profile_id):
    db.execute("DELETE FROM dealer_price_list WHERE profile_id = ?", (profile_id,))
    db.execute("DELETE FROM dealer_prices WHERE profile_id = ?", (profile_id,))


def get_order(db, order_id):
    return db.execute("SELECT * FROM dealer_orders WHERE id = ?", (order_id,)).fetchone()


def list_items(db, order_id):
    return db.execute(
        "SELECT * FROM dealer_order_items WHERE order_id = ? ORDER BY position, id", (order_id,)
    ).fetchall()


def list_payments(db, order_id):
    return db.execute(
        "SELECT * FROM dealer_payments WHERE order_id = ? ORDER BY paid_at DESC, id DESC", (order_id,)
    ).fetchall()


def paid_amount(db, order_id):
    return db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM dealer_payments WHERE order_id = ?", (order_id,)
    ).fetchone()[0]


def set_price(db, profile_id, price, timestamp):
    if price is None:
        db.execute("DELETE FROM dealer_prices WHERE profile_id = ?", (profile_id,))
        return
    cursor = db.execute(
        "UPDATE dealer_prices SET price = ?, updated_at = ? WHERE profile_id = ?",
        (price, timestamp, profile_id),
    )
    if cursor.rowcount == 0:
        db.execute(
            "INSERT INTO dealer_prices (profile_id, price, updated_at) VALUES (?, ?, ?)",
            (profile_id, price, timestamp),
        )
