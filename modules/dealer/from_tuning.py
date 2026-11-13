"""Turning a tuning-center order into a dealer-center order.

Its goods become an order of «Морские товары», or its boat and motors an order
of «Лодки и моторы». The client, dates, discount, payments (with their
cash-desk receipts), the events and the notes move along; the tuning order is
deleted. A dealer order has no works: those of the tuning order are dropped, so
the caller asks first (`works_count`) and passes `drop_works` to `convert`.
Nothing here commits — the caller does, or rolls everything back.
"""

from . import services
from .constants import (
    DEFAULT_GOODS_UNIT, GOODS_KIND, GOODS_UNITS, ORDER_TYPE_EQUIPMENT, ORDER_TYPE_GOODS,
)

TARGETS = {
    ORDER_TYPE_GOODS: "Морские товары",
    ORDER_TYPE_EQUIPMENT: "Лодки и моторы",
}
_DONE = ("done", "handed_over")
# tuning status -> status of an order of boats and motors
_EQUIPMENT_STATUS = {
    "new_request": "new_request", "awaiting_estimate": "new_request", "estimate": "estimate",
    "approved": "approved", "in_progress": "approved", "qc": "approved", "done": "ready",
    "handed_over": "handed_over", "cancelled": "cancelled",
}
COMMENT_LIMIT = 1900


def get_order(db, order_id):
    return db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()


def _count(db, sql, *params):
    return db.execute(sql, params).fetchone()[0]


def goods_lines(db, order_id):
    """The goods of the tuning order as dealer lines (a good outside the
    warehouse catalog — product_id 0 — is a hand-typed line)."""
    lines = []
    for row in db.execute(
        "SELECT p.*, sp.id AS catalog_id FROM tuning_order_products p "
        "LEFT JOIN supply_products sp ON sp.id = p.product_id "
        "WHERE p.order_id = ? ORDER BY p.position, p.id", (order_id,)
    ).fetchall():
        lines.append({
            "kind": GOODS_KIND, "profile_id": None, "product_id": row["catalog_id"],
            "unit": row["unit"] if row["unit"] in GOODS_UNITS else DEFAULT_GOODS_UNIT,
            "name": row["product_name"], "serial_number": "", "quantity": row["quantity"],
            "price": row["unit_price"],
        })
    return lines


def equipment_lines(db, order, profile_id_for):
    """The boat and the motors of the tuning order as dealer lines, priced from
    the dealer price list (0 when the model has no price yet)."""
    wanted = []
    if order["equipment_type"] == "boat":
        if (order["boat_model"] or "").strip():
            wanted.append(("boat", order["boat_model"].strip(), order["boat_registration_number"] or ""))
        for motor in db.execute(
            "SELECT motor_model, motor_serial_number FROM tuning_order_motors WHERE order_id = ? "
            "ORDER BY position, id", (order["id"],)
        ).fetchall():
            if (motor["motor_model"] or "").strip():
                wanted.append(("motor", motor["motor_model"].strip(), motor["motor_serial_number"] or ""))
    elif (order["motor_model"] or "").strip():
        wanted.append(("motor", order["motor_model"].strip(), order["motor_serial_number"] or ""))
    lines = []
    for kind, name, serial in wanted:
        profile_id = profile_id_for(db, kind, name)
        price = None
        if profile_id is not None:
            row = db.execute("SELECT price FROM dealer_prices WHERE profile_id = ?", (profile_id,)).fetchone()
            price = row["price"] if row else None
        lines.append({
            "kind": kind, "profile_id": profile_id, "product_id": None, "unit": DEFAULT_GOODS_UNIT,
            "name": name, "serial_number": serial, "quantity": 1, "price": price or 0.0,
        })
    return lines


def works_count(db, order):
    """How many works the order has (removed ones do not count): they are lost on a move."""
    return _count(db, "SELECT COUNT(*) FROM tuning_order_items WHERE order_id = ? AND status != 'removed'",
                  order["id"])


def problem(db, order, target, profile_id_for):
    """Why the order cannot be moved to `target` (a sentence), or None. Works
    are no obstacle — see `works_count`."""
    label = f"Заказ №{order['id']}"
    if _count(db, "SELECT COUNT(*) FROM partner_commissions WHERE order_id = ?", order["id"]):
        return f"{label}: по заказу начислена комиссия партнёра — перенести его нельзя."
    if _count(db, "SELECT COUNT(*) FROM tuning_yookassa_payments WHERE order_id = ?", order["id"]):
        return f"{label}: есть оплата через ЮKassa — перенести заказ нельзя."
    if _count(db, "SELECT COUNT(*) FROM hull_diagnostic_sheets WHERE tuning_order_id = ?", order["id"]):
        return f"{label}: к заказу есть лист диагностики корпуса — перенести заказ нельзя."
    project = db.execute("SELECT id FROM projects WHERE tuning_order_id = ?", (order["id"],)).fetchone()
    if project is not None and (
        _count(db, "SELECT COUNT(*) FROM bank_transactions WHERE project_id = ?", project["id"])
        or _count(db, "SELECT COUNT(*) FROM transaction_splits WHERE project_id = ?", project["id"])
        or _count(db, "SELECT COUNT(*) FROM entries WHERE project_id = ?", project["id"])
        or _count(db, "SELECT COUNT(*) FROM supply_writeoffs WHERE project_id = ? "
                      "AND tuning_order_product_id IS NULL", project["id"])
    ):
        return f"{label}: к проекту заказа привязаны банковские операции, записи работ или списания — перенести заказ нельзя."
    goods = _count(db, "SELECT COUNT(*) FROM tuning_order_products WHERE order_id = ?", order["id"])
    if target == ORDER_TYPE_GOODS:
        if not goods:
            return f"{label}: в заказе нет товаров — переносить в «Морские товары» нечего."
    elif target == ORDER_TYPE_EQUIPMENT:
        if order["equipment_type"] == "trailer":
            return (f"{label}: прицеп нельзя перенести в «Лодки и моторы» — там только лодки и моторы. "
                    "Товары заказа можно перенести в «Морские товары».")
        if goods:
            return (f"{label}: в заказе есть товары ({goods}) — в заказ на лодки и моторы они не переносятся, "
                    "перенесите заказ в «Морские товары».")
        if not equipment_lines(db, order, profile_id_for):
            return f"{label}: в заказе не указаны лодка или мотор — переносить в «Лодки и моторы» нечего."
    else:
        return "Выберите, куда перенести заказ."
    return None


def _status(order, target, carried_writeoffs, paid):
    status = order["status"]
    if target == ORDER_TYPE_EQUIPMENT:
        return _EQUIPMENT_STATUS.get(status, "new_request")
    if status == "cancelled":
        return "cancelled"
    if status in _DONE:
        return "completed"
    if carried_writeoffs:
        return "shipped"
    if order["total"] > 0 and paid >= order["total"] - 0.005:
        return "paid"
    return "new_request"


def _comment(db, order, target):
    parts = [f"Перенесён из тюнинг-заказа №{order['id']}."]
    if target == ORDER_TYPE_GOODS:
        equipment = []
        if (order["boat_model"] or "").strip():
            kind = "прицеп" if order["equipment_type"] == "trailer" else "лодка"
            equipment.append(f"{kind} {order['boat_model'].strip()}")
        for motor in db.execute(
            "SELECT motor_model FROM tuning_order_motors WHERE order_id = ? ORDER BY position, id", (order["id"],)
        ).fetchall():
            if (motor["motor_model"] or "").strip():
                equipment.append(f"мотор {motor['motor_model'].strip()}")
        if not equipment and (order["motor_model"] or "").strip():
            equipment.append(f"мотор {order['motor_model'].strip()}")
        if equipment:
            parts.append("Техника в заказе: " + ", ".join(equipment) + ".")
    for note in db.execute(
        "SELECT text FROM tuning_order_notes WHERE order_id = ? ORDER BY created_at, id", (order["id"],)
    ).fetchall():
        if (note["text"] or "").strip():
            parts.append(note["text"].strip())
    text = "\n".join(parts)
    return text if len(text) <= COMMENT_LIMIT else text[:COMMENT_LIMIT - 1].rstrip() + "…"


def convert(db, order, target, profile_id_for, delete_records, drop_works):
    """Makes the dealer order of `target` from the tuning order, moves what
    belongs to it and deletes the tuning order together with its works
    (`drop_works(db, order_id)` clears them, schedule cards and photos too).
    Returns the new order's id. The caller has checked `problem`, has the
    admin's consent for losing the works and commits."""
    order_id = order["id"]
    stamp = services.now_text()
    items = goods_lines(db, order_id) if target == ORDER_TYPE_GOODS else equipment_lines(db, order, profile_id_for)
    discount_type = order["discount_type"] if order["discount_type"] in ("percent", "amount") else "percent"
    subtotal = services.order_totals(items, "amount", 0)[0]
    discount_value = float(order["discount_value"] or 0)
    if discount_type == "amount":
        discount_value = min(discount_value, subtotal)
    _, total = services.order_totals(items, discount_type, discount_value)

    payments = db.execute("SELECT * FROM tuning_payments WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()
    paid = sum(p["amount"] for p in payments)
    writeoffs = db.execute(
        "SELECT w.* FROM supply_writeoffs w JOIN tuning_order_products p ON p.id = w.tuning_order_product_id "
        "WHERE p.order_id = ? ORDER BY w.id", (order_id,)
    ).fetchall() if target == ORDER_TYPE_GOODS else []
    status = _status(order, target, bool(writeoffs), paid)
    final = (status == "completed") if target == ORDER_TYPE_GOODS else (status == "handed_over")
    completed_at = (order["completed_at"] or stamp) if final else None

    new_id = db.execute(
        "INSERT INTO dealer_orders (client_id, client_name, phone, status, order_date, deadline_date, "
        "discount_type, discount_value, subtotal, total, comment, order_type, ship_warehouse_id, "
        "completed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order["client_id"], order["client_name"], order["phone"] or "", status, order["order_date"],
         order["deadline_date"] or None, discount_type, discount_value, subtotal, total,
         _comment(db, order, target), target, writeoffs[0]["warehouse_id"] if writeoffs else None,
         completed_at, stamp, stamp),
    ).lastrowid
    services.insert_items(db, new_id, items)

    for payment in payments:
        new_payment = db.execute(
            "INSERT INTO dealer_payments (order_id, amount, paid_at, payment_type, note, created_at, "
            "is_correction, no_receipt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id, payment["amount"], payment["paid_at"], payment["payment_type"],
             f"Из тюнинг-заказа №{order_id}", payment["created_at"], payment["is_correction"],
             payment["no_receipt"]),
        ).lastrowid
        for receipt in db.execute(
            "SELECT * FROM modulkassa_receipts WHERE payment_id = ? ORDER BY id", (payment["id"],)
        ).fetchall():
            db.execute("DELETE FROM modulkassa_receipts WHERE id = ?", (receipt["id"],))
            db.execute(
                "INSERT INTO dealer_modulkassa_receipts (payment_id, doc_id, status, fiscal_info_json, "
                "failure_message, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_payment, receipt["doc_id"], receipt["status"], receipt["fiscal_info_json"],
                 receipt["failure_message"], receipt["created_at"], receipt["updated_at"]),
            )

    for row in writeoffs:        # what was taken off the warehouse stays taken off, now for the new order
        db.execute(
            "UPDATE supply_writeoffs SET dealer_order_id = ?, tuning_order_product_id = NULL, project_id = NULL, "
            "note = ? WHERE id = ?",
            (new_id, f"Заказ дилерского центра №{new_id} (из тюнинг-заказа №{order_id})", row["id"]),
        )
    db.execute("UPDATE order_events SET order_kind = 'dealer', order_id = ? "
               "WHERE order_kind = 'tuning' AND order_id = ?", (new_id, order_id))

    db.execute("DELETE FROM tuning_order_note_reminders WHERE note_id IN "
               "(SELECT id FROM tuning_order_notes WHERE order_id = ?)", (order_id,))
    db.execute("DELETE FROM tuning_order_notes WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM tuning_order_products WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM projects WHERE tuning_order_id = ?", (order_id,))
    drop_works(db, order_id)
    delete_records(db, [order_id])
    return new_id
