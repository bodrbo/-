"""Validation and writes of the dealer center."""

import datetime as dt

from . import repository
from .constants import DEFAULT_STATUS, DISCOUNT_TYPES, ITEM_KINDS, PAYMENT_TYPES, STATUS_LABELS


def now_text():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _number(raw):
    text = str(raw or "").strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value and abs(value) < 1e12 else None


def _date(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return False


def order_totals(items, discount_type, discount_value):
    """(subtotal, total): the sum of the lines less the order-wide discount."""
    subtotal = round(sum(item["quantity"] * item["price"] for item in items), 2)
    if discount_type == "percent":
        total = subtotal * (1 - discount_value / 100)
    else:
        total = subtotal - discount_value
    return subtotal, round(max(total, 0), 2)


def parse_order_form(form, known_profiles):
    """Reads the order form. known_profiles: {profile_id: equipment_type} of
    the catalog. Returns (error, data); the lines without a name are skipped
    (the form always sends a spare empty row)."""
    errors = []
    client_name = " ".join(form.get("client_name", "").split())
    if not client_name:
        errors.append("Укажите клиента.")
    elif len(client_name) > 160:
        errors.append("Имя клиента — не более 160 символов.")
    phone = form.get("phone", "").strip()
    if len(phone) > 40:
        errors.append("Телефон — не более 40 символов.")
    client_id = form.get("client_id", type=int)

    order_date = _date(form.get("order_date")) or None
    if _date(form.get("order_date")) is False:
        errors.append("Некорректная дата заказа.")
    deadline = _date(form.get("deadline_date"))
    if deadline is False:
        errors.append("Некорректная дата передачи клиенту.")
        deadline = None

    discount_type = form.get("discount_type", "percent")
    if discount_type not in DISCOUNT_TYPES:
        discount_type = "percent"
    discount_value = _number(form.get("discount_value"))
    if form.get("discount_value", "").strip() and discount_value is None:
        errors.append("Скидка должна быть числом.")
    discount_value = discount_value or 0.0
    if discount_value < 0:
        errors.append("Скидка не может быть отрицательной.")
    if discount_type == "percent" and discount_value > 100:
        errors.append("Скидка в процентах — от 0 до 100.")

    kinds = form.getlist("item_kind[]")
    profile_ids = form.getlist("item_profile_id[]")
    names = form.getlist("item_name[]")
    serials = form.getlist("item_serial[]")
    quantities = form.getlist("item_quantity[]")
    prices = form.getlist("item_price[]")
    items = []
    for index, raw_name in enumerate(names):
        name = " ".join(raw_name.split())
        if not name:
            continue
        label = f"Позиция «{name}»"
        kind = kinds[index] if index < len(kinds) else ""
        if kind not in ITEM_KINDS:
            errors.append(f"{label}: выберите — лодка или мотор.")
            continue
        profile_raw = profile_ids[index] if index < len(profile_ids) else ""
        profile_id = int(profile_raw) if profile_raw.isdigit() else None
        if profile_id is not None and known_profiles.get(profile_id) != kind:
            profile_id = None  # not a catalog card of that kind -> a free-text position
        quantity = _number(quantities[index] if index < len(quantities) else "1")
        if quantity is None or quantity < 1 or quantity != int(quantity) or quantity > 999:
            errors.append(f"{label}: количество — целое число от 1.")
            continue
        price = _number(prices[index] if index < len(prices) else "")
        if price is None or price < 0:
            errors.append(f"{label}: укажите цену — число, не меньше нуля.")
            continue
        serial = (serials[index] if index < len(serials) else "").strip()
        if len(name) > 200 or len(serial) > 120:
            errors.append(f"{label}: слишком длинное название или серийный номер.")
            continue
        items.append({
            "kind": kind, "profile_id": profile_id, "name": name, "serial_number": serial,
            "quantity": int(quantity), "price": round(price, 2),
        })
    if not items and not errors:
        errors.append("Добавьте в заказ хотя бы одну лодку или мотор.")

    subtotal, total = order_totals(items, discount_type, discount_value)
    if discount_type == "amount" and discount_value > subtotal and not errors:
        errors.append("Скидка не может быть больше суммы заказа.")
    comment = form.get("comment", "").strip()
    if len(comment) > 2000:
        errors.append("Комментарий — не более 2000 символов.")
    if errors:
        return " ".join(errors), None
    return None, {
        "client_id": client_id, "client_name": client_name, "phone": phone,
        "order_date": order_date or dt.date.today().isoformat(), "deadline_date": deadline,
        "discount_type": discount_type, "discount_value": discount_value,
        "comment": comment, "items": items, "subtotal": subtotal, "total": total,
    }


def save_order(db, order_id, data, client_id):
    """Inserts or updates the order and replaces its lines. Returns the id."""
    stamp = now_text()
    if order_id is None:
        order_id = db.execute(
            "INSERT INTO dealer_orders (client_id, client_name, phone, status, order_date, deadline_date, "
            "discount_type, discount_value, subtotal, total, comment, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (client_id, data["client_name"], data["phone"], DEFAULT_STATUS, data["order_date"],
             data["deadline_date"], data["discount_type"], data["discount_value"], data["subtotal"],
             data["total"], data["comment"], stamp, stamp),
        ).lastrowid
    else:
        db.execute(
            "UPDATE dealer_orders SET client_id = ?, client_name = ?, phone = ?, order_date = ?, "
            "deadline_date = ?, discount_type = ?, discount_value = ?, subtotal = ?, total = ?, "
            "comment = ?, updated_at = ? WHERE id = ?",
            (client_id, data["client_name"], data["phone"], data["order_date"], data["deadline_date"],
             data["discount_type"], data["discount_value"], data["subtotal"], data["total"],
             data["comment"], stamp, order_id),
        )
        db.execute("DELETE FROM dealer_order_items WHERE order_id = ?", (order_id,))
    for position, item in enumerate(data["items"]):
        db.execute(
            "INSERT INTO dealer_order_items (order_id, kind, profile_id, name, serial_number, quantity, "
            "price, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, item["kind"], item["profile_id"], item["name"], item["serial_number"],
             item["quantity"], item["price"], position),
        )
    db.commit()
    return order_id


def set_status(db, order_id, status):
    """(ok, message). completed_at is stamped on the way to "Передан клиенту"
    and cleared when the order goes back."""
    if status not in STATUS_LABELS:
        return False, "Неизвестный статус."
    order = repository.get_order(db, order_id)
    if order is None:
        return False, "Заказ не найден."
    stamp = now_text()
    completed = order["completed_at"]
    if status == "handed_over":
        completed = completed or stamp
    else:
        completed = None
    db.execute(
        "UPDATE dealer_orders SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
        (status, completed, stamp, order_id),
    )
    db.commit()
    return True, f"Статус: «{STATUS_LABELS[status]}»."


def add_payment(db, order_id, raw_amount, payment_type, raw_date, note):
    """(ok, message)."""
    if repository.get_order(db, order_id) is None:
        return False, "Заказ не найден."
    amount = _number(raw_amount)
    if amount is None or amount <= 0:
        return False, "Сумма платежа должна быть больше нуля."
    if payment_type not in PAYMENT_TYPES:
        return False, "Выберите способ оплаты."
    day = _date(raw_date)
    if day is False:
        return False, "Некорректная дата платежа."
    if day and day > dt.date.today().isoformat():
        return False, "Дата платежа не может быть в будущем."
    stamp = now_text()
    paid_at = f"{day} 12:00" if day and day != dt.date.today().isoformat() else stamp
    db.execute(
        "INSERT INTO dealer_payments (order_id, amount, paid_at, payment_type, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, round(amount, 2), paid_at, payment_type, (note or "").strip()[:300], stamp),
    )
    db.commit()
    return True, "Платёж записан."


def delete_payment(db, order_id, payment_id):
    db.execute("DELETE FROM dealer_payments WHERE id = ? AND order_id = ?", (payment_id, order_id))
    db.commit()


def delete_order(db, order_id):
    """(ok, message). An order with payments is cancelled, not deleted."""
    if repository.get_order(db, order_id) is None:
        return False, "Заказ не найден."
    if repository.paid_amount(db, order_id):
        return False, "По заказу есть платежи — удалить его нельзя, переведите в статус «Отменён»."
    db.execute("DELETE FROM dealer_order_items WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM dealer_orders WHERE id = ?", (order_id,))
    db.commit()
    return True, "Заказ удалён."


def save_prices(db, raw_prices, known_profiles):
    """raw_prices: [(profile_id_text, price_text)]; blank clears the price.
    Returns (error, saved_count)."""
    parsed = []
    for raw_id, raw_price in raw_prices:
        if not str(raw_id).isdigit() or int(raw_id) not in known_profiles:
            continue
        text = str(raw_price or "").strip()
        if not text:
            parsed.append((int(raw_id), None))
            continue
        price = _number(text)
        if price is None or price < 0:
            return "Цена должна быть числом, не меньше нуля.", 0
        parsed.append((int(raw_id), round(price, 2)))
    stamp = now_text()
    for profile_id, price in parsed:
        repository.set_price(db, profile_id, price, stamp)
    db.commit()
    return None, len(parsed)
