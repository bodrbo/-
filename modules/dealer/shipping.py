"""Shipping of a goods order: what the order takes off a warehouse.

A write-off row (supply_writeoffs.dealer_order_id) is made per product of the
order when it is shipped and removed — the stock goes back — when the order
returns to an earlier status, is cancelled, is deleted, or its lines change.
"""

from .constants import GOODS_KIND, GOODS_WRITEOFF_REASON


def stock_lines(db, order_id):
    """The goods of the order that come from the warehouse catalog, one per
    product: [{product_id, name, quantity}]. Hand-typed lines have nothing to
    write off."""
    rows = db.execute(
        "SELECT i.product_id, p.name, SUM(i.quantity) AS quantity "
        "FROM dealer_order_items i JOIN supply_products p ON p.id = i.product_id "
        "WHERE i.order_id = ? AND i.kind = ? AND i.product_id IS NOT NULL "
        "GROUP BY i.product_id ORDER BY MIN(i.position), MIN(i.id)",
        (order_id, GOODS_KIND),
    ).fetchall()
    return [dict(row) for row in rows]


def writeoffs(db, order_id):
    return db.execute("SELECT * FROM supply_writeoffs WHERE dealer_order_id = ?", (order_id,)).fetchall()


def warehouse(db, warehouse_id):
    if warehouse_id is None:
        return None
    return db.execute("SELECT id, name FROM supply_warehouses WHERE id = ?", (warehouse_id,)).fetchone()


def _have(db, product_id, warehouse_id):
    row = db.execute(
        "SELECT quantity FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
        (product_id, warehouse_id),
    ).fetchone()
    return row["quantity"] if row is not None else 0


def shortages(db, lines, warehouse_id):
    """What the warehouse cannot cover: [{name, need, have}]."""
    result = []
    for line in lines:
        have = _have(db, line["product_id"], warehouse_id)
        if have < line["quantity"] - 1e-9:
            result.append({"name": line["name"], "need": line["quantity"], "have": have})
    return result


def shortage_text(warehouse_name, missing):
    parts = ", ".join(f"«{m['name']}» — нужно {m['need']:g}, есть {m['have']:g}" for m in missing)
    return f"На складе «{warehouse_name}» не хватает: {parts}."


def warehouse_options(db, order_id):
    """Every warehouse with what it lacks for the order (an empty list =
    the whole order can be shipped from it)."""
    lines = stock_lines(db, order_id)
    return [
        {"id": row["id"], "name": row["name"], "missing": shortages(db, lines, row["id"])}
        for row in db.execute("SELECT id, name FROM supply_warehouses ORDER BY name COLLATE NOCASE, id").fetchall()
    ]


def write_off(db, order, lines, warehouse_id, actor, stamp):
    """Takes `lines` off the warehouse (the caller has checked the stock).
    Returns the product ids written off."""
    for line in lines:
        product = db.execute(
            "SELECT cost_price FROM supply_products WHERE id = ?", (line["product_id"],)
        ).fetchone()
        cost_price = product["cost_price"] if product is not None else 0
        db.execute(
            "INSERT INTO supply_writeoffs (product_id, warehouse_id, quantity, reason, note, created_at, "
            "cost_price, amount, employee_name, dealer_order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (line["product_id"], warehouse_id, line["quantity"], GOODS_WRITEOFF_REASON,
             f"Заказ дилерского центра №{order['id']}", stamp, cost_price,
             line["quantity"] * cost_price, actor, order["id"]),
        )
        db.execute(
            "UPDATE supply_stock SET quantity = quantity - ? WHERE product_id = ? AND warehouse_id = ?",
            (line["quantity"], line["product_id"], warehouse_id),
        )
    return [line["product_id"] for line in lines]


def release(db, order_id):
    """Returns what the order took off the warehouse; the write-offs go."""
    for row in writeoffs(db, order_id):
        db.execute(
            "UPDATE supply_stock SET quantity = quantity + ? WHERE product_id = ? AND warehouse_id = ?",
            (row["quantity"], row["product_id"], row["warehouse_id"]),
        )
    db.execute("DELETE FROM supply_writeoffs WHERE dealer_order_id = ?", (order_id,))
    db.execute("UPDATE dealer_orders SET ship_warehouse_id = NULL WHERE id = ?", (order_id,))
