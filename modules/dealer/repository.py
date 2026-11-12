"""SQL access for the dealer center."""

import json

from .constants import RECEIPT_SUCCESS_STATUSES


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


def catalog_goods(db):
    """Goods of the warehouse catalog with their selling price, by name."""
    return db.execute(
        "SELECT id, name, sku, sale_price, cost_unit FROM supply_products ORDER BY name COLLATE NOCASE, id"
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


def list_tuning_orders(db, order_id):
    """Tuning-center orders made from this dealer order."""
    return db.execute(
        "SELECT id, status, order_date FROM tuning_orders WHERE dealer_order_id = ? ORDER BY id", (order_id,)
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


def get_payment(db, order_id, payment_id):
    return db.execute(
        "SELECT * FROM dealer_payments WHERE id = ? AND order_id = ?", (payment_id, order_id)
    ).fetchone()


def fiscal_info(raw):
    """The fiscal data ModulKassa answered with (a dict), or None."""
    try:
        info = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def has_fiscal_qr(raw):
    info = fiscal_info(raw)
    return bool(info and info.get("qr"))


def list_payments_with_receipts(db, order_id):
    """The payments with the state of their latest receipt: receipt_status,
    receipt_failure_message and receipt_pdf_available. A retry adds another
    receipt row for the same payment, so only the newest one is joined."""
    rows = db.execute(
        "SELECT p.*, r.status AS receipt_status, r.failure_message AS receipt_failure_message, "
        "r.fiscal_info_json AS receipt_fiscal_info_json "
        "FROM dealer_payments p LEFT JOIN dealer_modulkassa_receipts r ON r.id = ("
        "  SELECT id FROM dealer_modulkassa_receipts r2 WHERE r2.payment_id = p.id ORDER BY r2.id DESC LIMIT 1) "
        "WHERE p.order_id = ? ORDER BY p.paid_at DESC, p.id DESC",
        (order_id,),
    ).fetchall()
    result = []
    for row in rows:
        payment = dict(row)
        payment["receipt_pdf_available"] = bool(
            (payment["receipt_status"] or "").lower() in RECEIPT_SUCCESS_STATUSES
            and has_fiscal_qr(payment["receipt_fiscal_info_json"])
        )
        result.append(payment)
    return result


def latest_receipt(db, payment_id):
    return db.execute(
        "SELECT * FROM dealer_modulkassa_receipts WHERE payment_id = ? ORDER BY id DESC LIMIT 1", (payment_id,)
    ).fetchone()


_SUCCESSFUL = "LOWER(r2.status) IN ('printed', 'wait_for_callback', 'completed') AND r2.fiscal_info_json IS NOT NULL"


def successful_receipt(db, order_id, payment_id):
    """What the PDF of a receipt is built from: the payment, its client and
    the fiscal data of the newest successful receipt (None when there is none)."""
    return db.execute(
        "SELECT p.id AS payment_id, p.order_id, p.amount, p.paid_at, p.payment_type, "
        "o.client_id, o.client_name, r.status AS receipt_status, r.fiscal_info_json "
        "FROM dealer_payments p JOIN dealer_orders o ON o.id = p.order_id "
        "JOIN dealer_modulkassa_receipts r ON r.id = ("
        f"  SELECT id FROM dealer_modulkassa_receipts r2 WHERE r2.payment_id = p.id AND {_SUCCESSFUL} "
        "  ORDER BY r2.id DESC LIMIT 1) "
        "WHERE p.order_id = ? AND p.id = ?",
        (order_id, payment_id),
    ).fetchone()


def client_receipts(db, order_id):
    """The receipts the client may open: only fiscal ones with a QR, no
    internal data."""
    rows = db.execute(
        "SELECT p.id AS payment_id, p.amount, p.paid_at, p.payment_type, r.status AS receipt_status, "
        "r.fiscal_info_json "
        "FROM dealer_payments p JOIN dealer_modulkassa_receipts r ON r.id = ("
        f"  SELECT id FROM dealer_modulkassa_receipts r2 WHERE r2.payment_id = p.id AND {_SUCCESSFUL} "
        "  ORDER BY r2.id DESC LIMIT 1) "
        "WHERE p.order_id = ? ORDER BY p.paid_at DESC, p.id DESC",
        (order_id,),
    ).fetchall()
    return [
        {"payment_id": row["payment_id"], "amount": row["amount"], "paid_at": row["paid_at"]}
        for row in rows if has_fiscal_qr(row["fiscal_info_json"])
    ]


def delete_payment(db, order_id, payment_id):
    db.execute("DELETE FROM dealer_modulkassa_receipts WHERE payment_id IN "
               "(SELECT id FROM dealer_payments WHERE id = ? AND order_id = ?)", (payment_id, order_id))
    db.execute("DELETE FROM dealer_payments WHERE id = ? AND order_id = ?", (payment_id, order_id))


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
