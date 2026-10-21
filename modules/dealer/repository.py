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
