"""Validation and writes of the dealer center."""

import datetime as dt

from . import repository, shipping
from .constants import (
    DEFAULT_GOODS_UNIT, DEFAULT_STATUS, DISCOUNT_TYPES, FINAL_STATUS, GOODS_KIND, GOODS_RELEASE_STATUSES,
    GOODS_UNITS, ITEM_KINDS, ORDER_TYPE_EQUIPMENT, ORDER_TYPE_GOODS, PAYMENT_TYPES, SHIPPED_STATUS,
    STATUS_LABELS, STATUSES_BY_TYPE,
)


class ShipmentError(ValueError):
    """The stock cannot cover the order lines being saved."""


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


def _parse_equipment_items(form, known_profiles, errors):
    """The boat and motor lines of the form; the problems go to `errors`."""
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
            "kind": kind, "profile_id": profile_id, "product_id": None, "unit": DEFAULT_GOODS_UNIT,
            "name": name, "serial_number": serial, "quantity": int(quantity), "price": round(price, 2),
        })
    return items


def _parse_goods_items(form, known_products, errors):
    """The goods lines of the form. known_products: {product_id: unit} of the
    warehouse catalog; a line with an unknown (or no) product is a good typed
    by hand. The quantity may be fractional (square or running metres), but
    pieces are whole."""
    product_ids = form.getlist("item_product_id[]")
    names = form.getlist("item_name[]")
    units = form.getlist("item_unit[]")
    quantities = form.getlist("item_quantity[]")
    prices = form.getlist("item_price[]")
    items = []
    for index, raw_name in enumerate(names):
        name = " ".join(raw_name.split())
        if not name:
            continue
        label = f"Товар «{name}»"
        product_raw = product_ids[index] if index < len(product_ids) else ""
        product_id = int(product_raw) if product_raw.isdigit() and int(product_raw) in known_products else None
        if product_id is not None:
            unit = known_products[product_id]
        else:
            unit = units[index] if index < len(units) else ""
        if unit not in GOODS_UNITS:
            unit = DEFAULT_GOODS_UNIT
        quantity = _number(quantities[index] if index < len(quantities) else "1")
        if quantity is None or quantity <= 0 or quantity > 99999:
            errors.append(f"{label}: количество — число больше нуля.")
            continue
        if unit == "piece" and quantity != int(quantity):
            errors.append(f"{label}: штуки — целое число.")
            continue
        quantity = int(quantity) if quantity == int(quantity) else round(quantity, 3)
        price = _number(prices[index] if index < len(prices) else "")
        if price is None or price < 0:
            errors.append(f"{label}: укажите цену — число, не меньше нуля.")
            continue
        if len(name) > 200:
            errors.append(f"{label}: слишком длинное название.")
            continue
        items.append({
            "kind": GOODS_KIND, "profile_id": None, "product_id": product_id, "unit": unit, "name": name,
            "serial_number": "", "quantity": quantity, "price": round(price, 2),
        })
    return items


def parse_order_form(form, known_profiles, order_type=ORDER_TYPE_EQUIPMENT, known_products=None):
    """Reads the order form. known_profiles: {profile_id: equipment_type} of
    the catalog of boats and motors, known_products: {product_id: unit} of
    the warehouse catalog (goods orders). Returns (error, data); the lines
    without a name are skipped (the form always sends a spare empty row)."""
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

    if order_type == ORDER_TYPE_GOODS:
        items = _parse_goods_items(form, known_products or {}, errors)
        if not items and not errors:
            errors.append("Добавьте в заказ хотя бы один товар.")
    else:
        items = _parse_equipment_items(form, known_profiles, errors)
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
        "order_type": order_type,
    }


def save_order(db, order_id, data, client_id, actor=None, after_writeoff=None):
    """Inserts or updates the order and replaces its lines. Returns the id.
    A shipped goods order keeps its write-off in step with the lines: the old
    one is returned and the new lines are written off the same warehouse; if
    that cannot be covered nothing is saved and ShipmentError is raised."""
    stamp = now_text()
    shipped_from = None
    if order_id is not None:
        existing = repository.get_order(db, order_id)
        if existing is not None and shipping.writeoffs(db, order_id):
            shipped_from = existing["ship_warehouse_id"]
    if order_id is None:
        order_id = db.execute(
            "INSERT INTO dealer_orders (client_id, client_name, phone, status, order_date, deadline_date, "
            "discount_type, discount_value, subtotal, total, comment, order_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (client_id, data["client_name"], data["phone"], DEFAULT_STATUS, data["order_date"],
             data["deadline_date"], data["discount_type"], data["discount_value"], data["subtotal"],
             data["total"], data["comment"], data["order_type"], stamp, stamp),
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
            "INSERT INTO dealer_order_items (order_id, kind, profile_id, product_id, unit, name, serial_number, "
            "quantity, price, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, item["kind"], item["profile_id"], item["product_id"], item["unit"], item["name"],
             item["serial_number"], item["quantity"], item["price"], position),
        )
    written = []
    if shipped_from is not None:
        shipping.release(db, order_id)
        lines = shipping.stock_lines(db, order_id)
        place = shipping.warehouse(db, shipped_from)
        missing = shipping.shortages(db, lines, shipped_from) if place is not None else []
        if place is None or missing:
            db.rollback()
            raise ShipmentError(
                (shipping.shortage_text(place["name"], missing) if place is not None
                 else "Склад отгрузки удалён.")
                + " Заказ уже отгружен — списание пересчитывается по новым позициям; измените позиции или "
                "верните заказ на предыдущий статус."
            )
        db.execute("UPDATE dealer_orders SET ship_warehouse_id = ? WHERE id = ?", (shipped_from, order_id))
        written = shipping.write_off(db, {"id": order_id}, lines, shipped_from, actor, stamp)
    db.commit()
    if after_writeoff:
        for product_id in written:
            after_writeoff(db, product_id)
    return order_id


def set_status(db, order_id, status, warehouse_id=None, actor=None, after_writeoff=None):
    """(ok, message). completed_at is stamped on the way to the final status
    ("Передан клиенту" / "Завершен") and cleared when the order goes back.

    Goods orders move stock: "Отгружен" writes the catalog goods off the
    warehouse given (all of them or nothing — a warehouse that lacks
    something refuses), a status before it (or "Отменён") returns them, and
    "Завершен" needs the shipment first."""
    order = repository.get_order(db, order_id)
    if order is None:
        return False, "Заказ не найден."
    order_type = order["order_type"] or ORDER_TYPE_EQUIPMENT
    if status not in {item["value"] for item in STATUSES_BY_TYPE.get(order_type, ())}:
        return False, "Неизвестный статус."
    stamp = now_text()
    note = ""
    written = []
    if order_type == ORDER_TYPE_GOODS:
        has_writeoff = bool(shipping.writeoffs(db, order_id))
        lines = shipping.stock_lines(db, order_id)
        if status == SHIPPED_STATUS and not has_writeoff and lines:
            place = shipping.warehouse(db, warehouse_id)
            if place is None:
                return False, "Укажите склад отгрузки — с него спишется товар."
            missing = shipping.shortages(db, lines, place["id"])
            if missing:
                return False, shipping.shortage_text(place["name"], missing)
            written = shipping.write_off(db, order, lines, place["id"], actor, stamp)
            db.execute("UPDATE dealer_orders SET ship_warehouse_id = ? WHERE id = ?", (place["id"], order_id))
            note = f" Списано со склада «{place['name']}»: {len(lines)} поз."
        elif status == FINAL_STATUS[ORDER_TYPE_GOODS] and not has_writeoff and lines:
            return False, "Сначала отгрузите заказ: поставьте статус «Отгружен» и укажите склад."
        elif status in GOODS_RELEASE_STATUSES and has_writeoff:
            shipping.release(db, order_id)
            note = " Товар возвращён на склад."
    completed = order["completed_at"]
    if status == FINAL_STATUS[order_type]:
        completed = completed or stamp
    else:
        completed = None
    db.execute(
        "UPDATE dealer_orders SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
        (status, completed, stamp, order_id),
    )
    db.commit()
    if after_writeoff:
        for product_id in written:
            after_writeoff(db, product_id)
    return True, f"Статус: «{STATUS_LABELS[status]}».{note}"


def add_payment(db, order_id, raw_amount, payment_type, raw_date, note, punch_receipt=False,
                is_correction=False, receipts=None):
    """(ok, message). With `punch_receipt` the payment goes to the cash desk
    as a fiscal receipt (receipts.fiscalize); `is_correction` makes it a
    correction receipt for money taken earlier, dated `raw_date`. Without a
    receipt the payment is only recorded and the date may be an old one.

    receipts: .configured() -> bool and .fiscalize(db, order, payment_id,
    amount, payment_type, correction_date) — injected from app.py (None = no
    cash desk at all)."""
    order = repository.get_order(db, order_id)
    if order is None:
        return False, "Заказ не найден."
    amount = _number(raw_amount)
    if amount is None or amount <= 0:
        return False, "Сумма платежа должна быть больше нуля."
    if payment_type not in PAYMENT_TYPES:
        return False, "Выберите способ оплаты."
    day = _date(raw_date)
    if day is False:
        return False, "Некорректная дата платежа."
    today = dt.date.today().isoformat()
    if day and day > today:
        return False, "Дата платежа не может быть в будущем."
    punch_receipt = bool(punch_receipt)
    is_correction = bool(is_correction) and punch_receipt      # a correction exists only with a receipt
    if is_correction and not day:
        return False, "Для чека коррекции укажите дату платежа — не позже сегодняшней."
    stamp = now_text()
    # The money of a correction receipt was taken on its date, an ordinary
    # receipt is dated now; a payment without a receipt may be an old one.
    if is_correction or (not punch_receipt and day and day != today):
        paid_at = f"{day} 12:00"
    else:
        paid_at = stamp
    payment_id = db.execute(
        "INSERT INTO dealer_payments (order_id, amount, paid_at, payment_type, note, created_at, "
        "is_correction, no_receipt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, round(amount, 2), paid_at, payment_type, (note or "").strip()[:300], stamp,
         1 if is_correction else 0, 0 if punch_receipt else 1),
    ).lastrowid
    db.commit()
    if not punch_receipt:
        return True, "Платёж записан без чека."
    if receipts is None or not receipts.configured():
        return True, "Платёж записан, но касса (МодульКасса) не подключена — чек не пробит."
    receipts.fiscalize(db, order, payment_id, round(amount, 2), payment_type, day if is_correction else None)
    sent = repository.latest_receipt(db, payment_id)
    if sent is not None and (sent["status"] or "").lower() == "failed":
        return True, "Платёж записан, но чек на кассу не ушёл — его можно повторить в списке платежей."
    return True, "Платёж записан, чек коррекции отправлен на кассу." if is_correction \
        else "Платёж записан, чек отправлен на кассу."


def check_receipt(db, order_id, payment_id, receipts):
    """Asks the cash desk how the receipt of a payment is doing. (ok, message)."""
    payment = repository.get_payment(db, order_id, payment_id)
    receipt = repository.latest_receipt(db, payment_id) if payment is not None else None
    if receipt is None:
        return False, "По этому платежу чека нет."
    if receipts is None or not receipts.configured():
        return False, "Касса (МодульКасса) не подключена."
    receipts.check(db, receipt)
    db.commit()
    state = repository.latest_receipt(db, payment_id)["status"]
    return True, f"Статус чека обновлён: {state}."


def retry_receipt(db, order_id, payment_id, receipts):
    """Sends the receipt of a payment again — only when the last attempt
    failed or none was made, so a receipt is never punched twice.
    (ok, message)."""
    order = repository.get_order(db, order_id)
    payment = repository.get_payment(db, order_id, payment_id)
    if order is None or payment is None:
        return False, "Платёж не найден."
    if payment["no_receipt"] or payment["payment_type"] not in PAYMENT_TYPES:
        return False, "Этот платёж записан без чека."
    last = repository.latest_receipt(db, payment_id)
    if last is not None and (last["status"] or "").lower() != "failed":
        return False, "Чек по этому платежу уже отправлен — повторять не нужно."
    if receipts is None or not receipts.configured():
        return False, "Касса (МодульКасса) не подключена."
    receipts.fiscalize(
        db, order, payment_id, payment["amount"], payment["payment_type"],
        payment["paid_at"][:10] if payment["is_correction"] else None,
    )
    return True, "Чек отправлен на кассу повторно."


def delete_payment(db, order_id, payment_id):
    repository.delete_payment(db, order_id, payment_id)
    db.commit()


def delete_order(db, order_id):
    """(ok, message). An order with payments is cancelled, not deleted."""
    if repository.get_order(db, order_id) is None:
        return False, "Заказ не найден."
    if repository.paid_amount(db, order_id):
        return False, "По заказу есть платежи — удалить его нельзя, переведите в статус «Отменён»."
    shipping.release(db, order_id)       # an order shipped and never paid gives its goods back
    db.execute("DELETE FROM dealer_order_items WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM dealer_orders WHERE id = ?", (order_id,))
    db.commit()
    return True, "Заказ удалён."


def add_to_price_list(db, form, known_profiles, create_profile):
    """A model with its price joins the price list: picked from the common
    catalog (mode "catalog", profile_id) or created (mode "new": kind, name,
    brand). `known_profiles`: {profile id: equipment type} of the catalog;
    create_profile(db, kind, name, brand) -> (error, profile_id, created)
    finds or creates the catalog entry (injected from app.py).
    Returns (error, message)."""
    price = _number(form.get("price"))
    if price is None or price < 0:
        return "Укажите цену — число, не меньше нуля.", None
    price = round(price, 2)
    mode = str(form.get("mode", "")).strip()
    message = "Модель добавлена в прайс-лист."
    if mode == "catalog":
        raw_id = str(form.get("profile_id", "")).strip()
        if not raw_id.isdigit() or int(raw_id) not in known_profiles:
            return "Выберите модель из каталога.", None
        profile_id = int(raw_id)
    elif mode == "new":
        kind = str(form.get("kind", "")).strip()
        if kind not in ITEM_KINDS:
            return "Выберите, что это: лодка или мотор.", None
        name = " ".join(str(form.get("model_name", "")).split())
        if not name:
            return "Укажите название модели.", None
        error, profile_id, created = create_profile(
            db, kind, name, " ".join(str(form.get("brand", "")).split())
        )
        if error:
            return error, None
        message = (
            "Новая модель создана в общем каталоге и добавлена в прайс-лист." if created
            else "Такая модель уже есть в общем каталоге — добавлена из него."
        )
    else:
        return "Выберите: модель из каталога или новая.", None
    if repository.in_price_list(db, profile_id):
        message = "Модель уже была в прайс-листе — цена обновлена."
    stamp = now_text()
    repository.add_to_price_list(db, profile_id, stamp)
    repository.set_price(db, profile_id, price, stamp)
    db.commit()
    return None, message


def remove_from_price_list(db, profile_id):
    """A model added by hand leaves the list; one that stands in an order
    cannot. Returns (ok, message)."""
    position = next((row for row in repository.price_list_positions(db) if row["id"] == profile_id), None)
    if position is None:
        return False, "Этой модели нет в прайс-листе."
    if position["in_orders"]:
        return False, "Модель есть в заказах дилерского центра — пока они не отменены, из прайс-листа её не убрать."
    repository.remove_from_price_list(db, profile_id)
    db.commit()
    return True, f"«{position['model_name']}» убрана из прайс-листа."


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
