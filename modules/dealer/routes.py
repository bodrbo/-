"""Administrator routes of the Дилерский центр."""

import datetime as dt

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from . import repository, services, shipping
from .constants import (
    GOODS_UNITS, ITEM_KINDS, ORDER_TYPE_EQUIPMENT, ORDER_TYPE_GOODS, PAYMENT_TYPES,
    STATUS_LABELS, STATUSES_BY_TYPE,
)


def create_dealer_blueprint(get_db, access_required, client_choices, resolve_client,
                            events_panel=None, forget_events=None, create_profile=None,
                            create_tuning_order=None, tuning_order_url=None, low_stock_check=None,
                            receipts=None):
    """client_choices(db) -> clients for the picker; resolve_client(db,
    client_id, client_name, phone) -> client id (finds or creates the client
    in the shared client base) — both injected from app.py. events_panel(db,
    order_id) -> the «События» panel data of an order, forget_events(db,
    order_id) drops them with a deleted order (both optional).
    create_profile(db, kind, name, brand) -> (error, profile_id, created)
    finds or creates a model of the common catalog. create_tuning_order(db,
    order, items, admin_id) -> id of a tuning-center order made for the
    equipment of the order (None when there is none), tuning_order_url(id)
    -> its page. low_stock_check(db, product_id) is called after a product
    was written off the warehouse by a shipment (raises the supply request when
    the stock runs low; optional). receipts: the cash desk (ModulKassa) —
    .configured(), .fiscalize(db, order, payment_id, amount, payment_type,
    correction_date), .check(db, receipt_row) and .pdf_response(db, order_id,
    payment_id) -> a Flask response or (text, status); None = no receipts."""
    blueprint = Blueprint("dealer", __name__)

    def catalog_maps(db):
        positions = repository.catalog_positions(db)
        return positions, {row["id"]: row["equipment_type"] for row in positions}

    def goods_units(db):
        """{product_id: unit} of the warehouse catalog."""
        return {
            row["id"]: row["cost_unit"] if row["cost_unit"] in GOODS_UNITS else "piece"
            for row in repository.catalog_goods(db)
        }

    def list_url(order_type):
        return url_for("dealer.goods" if order_type == ORDER_TYPE_GOODS else "dealer.orders")

    def notice(kind, text):
        session["dealer_notice"] = {"type": kind, "message": text}

    @blueprint.route("/dealer")
    @access_required
    def index():
        return redirect(url_for("dealer.orders"))

    # ---- orders list

    @blueprint.route("/dealer/orders")
    @access_required
    def orders():
        return render_list(get_db(), ORDER_TYPE_EQUIPMENT)

    @blueprint.route("/dealer/goods")
    @access_required
    def goods():
        return render_list(get_db(), ORDER_TYPE_GOODS)

    def render_list(db, order_type):
        query = request.args.get("q", "").strip()[:120]
        status = request.args.get("status", "").strip()
        if status not in {item["value"] for item in STATUSES_BY_TYPE[order_type]}:
            status = ""
        rows = db.execute(
            "SELECT o.*, COALESCE((SELECT SUM(amount) FROM dealer_payments WHERE order_id = o.id), 0) AS paid, "
            "COALESCE((SELECT GROUP_CONCAT(name || CASE WHEN quantity > 1 THEN ' ×' || quantity ELSE '' END, '; ') "
            " FROM dealer_order_items WHERE order_id = o.id), '') AS items_text "
            "FROM dealer_orders o WHERE o.order_type = ? " + ("AND o.status = ? " if status else "")
            + "ORDER BY o.order_date DESC, o.id DESC",
            (order_type, status) if status else (order_type,),
        ).fetchall()
        words = query.casefold().split()
        shown = []
        for row in rows:
            order = dict(row)
            if words:
                haystack = " ".join(
                    str(order.get(field) or "") for field in ("id", "client_name", "phone", "items_text")
                ).casefold()
                if not all(word in haystack for word in words):
                    continue
            order["remaining"] = max(0.0, round(order["total"] - order["paid"], 2))
            order["status_label"] = STATUS_LABELS.get(order["status"], order["status"])
            shown.append(order)
        # A cancelled order stays in the list but is left out of the totals.
        counted = [o for o in shown if o["status"] != "cancelled"]
        return render_template(
            "dealer/orders.html", active_page="dealer",
            sub_page="goods" if order_type == ORDER_TYPE_GOODS else "orders", order_type=order_type,
            orders=shown, statuses=STATUSES_BY_TYPE[order_type], selected_status=status, search_query=query,
            total_sum=sum(o["total"] for o in counted),
            paid_sum=sum(o["paid"] for o in counted),
            remaining_sum=sum(o["remaining"] for o in counted),
            counted=len(counted), notice=session.pop("dealer_notice", None),
        )

    # ---- one order

    def render_form(db, order, items, form_state=None, error=None, order_type=ORDER_TYPE_EQUIPMENT):
        if order is not None:
            order_type = order.get("order_type") or ORDER_TYPE_EQUIPMENT
        positions, _ = catalog_maps(db)
        payments = paid = None
        if order is not None:
            payments = repository.list_payments_with_receipts(db, order["id"])
            paid = repository.paid_amount(db, order["id"])
        return render_template(
            "dealer/form.html", active_page="dealer",
            sub_page="goods" if order_type == ORDER_TYPE_GOODS else "orders",
            order_type=order_type, is_goods=order_type == ORDER_TYPE_GOODS, list_url=list_url(order_type),
            goods_catalog=[
                {"id": g["id"], "name": g["name"], "sku": g["sku"] or "", "price": g["sale_price"],
                 "unit": g["cost_unit"] if g["cost_unit"] in GOODS_UNITS else "piece"}
                for g in repository.catalog_goods(db)
            ] if order_type == ORDER_TYPE_GOODS else [],
            goods_units=GOODS_UNITS,
            order=order, items=items, form_state=form_state, error=error,
            catalog=[
                {"id": p["id"], "kind": p["equipment_type"], "name": p["model_name"],
                 "brand": p["brand"] or "", "price": p["price"]}
                for p in positions
            ],
            item_kinds=ITEM_KINDS, client_choices=client_choices(db),
            statuses=STATUSES_BY_TYPE[order_type], status_label=STATUS_LABELS.get(order["status"]) if order else None,
            payments=payments, paid=paid,
            remaining=max(0.0, round((order["total"] - paid), 2)) if order is not None else None,
            payment_types=PAYMENT_TYPES, today=dt.date.today().isoformat(),
            receipts_configured=bool(receipts and receipts.configured()),
            events_panel=events_panel(db, order["id"]) if order is not None and events_panel else None,
            tuning_orders=[
                {**dict(row), "url": tuning_order_url(row["id"])}
                for row in repository.list_tuning_orders(db, order["id"])
            ] if order is not None and tuning_order_url else [],
            can_make_tuning_order=order is not None and order_type == ORDER_TYPE_EQUIPMENT
            and bool(create_tuning_order) and any(
                item["kind"] in ITEM_KINDS for item in repository.list_items(db, order["id"])
            ),
            notice=session.pop("dealer_notice", None),
        )

    def state_from_form(form):
        items = []
        names = form.getlist("item_name[]")
        for index, name in enumerate(names):
            def at(key, default=""):
                values = form.getlist(key)
                return values[index] if index < len(values) else default
            items.append({
                "kind": at("item_kind[]", "boat"), "profile_id": at("item_profile_id[]"), "name": name,
                "product_id": at("item_product_id[]"), "unit": at("item_unit[]", "piece"),
                "serial_number": at("item_serial[]"), "quantity": at("item_quantity[]", "1"),
                "price": at("item_price[]"),
            })
        return {key: form.get(key, "") for key in (
            "client_name", "client_id", "phone", "order_date", "deadline_date",
            "discount_type", "discount_value", "comment",
        )}, items

    def save_from_request(db, order_id, order_type):
        _, known = catalog_maps(db)
        error, data = services.parse_order_form(
            request.form, known, order_type, goods_units(db) if order_type == ORDER_TYPE_GOODS else None
        )
        if error:
            return error, None
        client_id = resolve_client(db, data["client_id"], data["client_name"], data["phone"])
        try:
            return None, services.save_order(
                db, order_id, data, client_id, actor=session.get("admin_name"), after_writeoff=low_stock_check
            )
        except services.ShipmentError as problem:
            return str(problem), None

    def create_order(order_type):
        db = get_db()
        if request.method == "POST":
            error, order_id = save_from_request(db, None, order_type)
            if error is None:
                notice("success", "Заказ создан.")
                return redirect(url_for("dealer.order", order_id=order_id))
            state, items = state_from_form(request.form)
            return render_form(db, None, items, form_state=state, error=error, order_type=order_type), 400
        return render_form(db, None, [], order_type=order_type)

    @blueprint.route("/dealer/orders/new", methods=["GET", "POST"])
    @access_required
    def new_order():
        return create_order(ORDER_TYPE_EQUIPMENT)

    @blueprint.route("/dealer/goods/new", methods=["GET", "POST"])
    @access_required
    def new_goods_order():
        return create_order(ORDER_TYPE_GOODS)

    @blueprint.route("/dealer/orders/<int:order_id>", methods=["GET", "POST"])
    @access_required
    def order(order_id):
        db = get_db()
        row = repository.get_order(db, order_id)
        if row is None:
            return redirect(url_for("dealer.orders"))
        if request.method == "POST":
            error, _ = save_from_request(db, order_id, row["order_type"])
            if error is None:
                notice("success", "Заказ сохранён.")
                return redirect(url_for("dealer.order", order_id=order_id))
            state, items = state_from_form(request.form)
            return render_form(db, dict(row), items, form_state=state, error=error), 400
        return render_form(db, dict(row), [dict(i) for i in repository.list_items(db, order_id)])

    @blueprint.route("/dealer/orders/<int:order_id>/status", methods=["POST"])
    @access_required
    def update_status(order_id):
        ok, message = services.set_status(
            get_db(), order_id, request.form.get("status", ""),
            warehouse_id=request.form.get("warehouse_id", type=int),
            actor=session.get("admin_name"), after_writeoff=low_stock_check,
        )
        notice("success" if ok else "error", message)
        target = request.form.get("next", "").strip()
        if target.startswith("/") and not target.startswith("//") and "\\" not in target and len(target) <= 600:
            return redirect(target)
        return redirect(url_for("dealer.order", order_id=order_id))

    @blueprint.route("/dealer/orders/<int:order_id>/shipping")
    @access_required
    def shipping_options(order_id):
        """What the «Отгружен» dialog needs: the warehouses with what each lacks
        for the order, or the warehouse the order already went out from."""
        db = get_db()
        order = repository.get_order(db, order_id)
        if order is None or order["order_type"] != ORDER_TYPE_GOODS:
            return jsonify({"error": "Заказ не найден."}), 404
        lines = shipping.stock_lines(db, order_id)
        done = shipping.writeoffs(db, order_id)
        place = shipping.warehouse(db, order["ship_warehouse_id"]) if done else None
        return jsonify({
            "order_id": order_id,
            "lines": [{"name": line["name"], "quantity": line["quantity"]} for line in lines],
            "shipped_from": place["name"] if place is not None else ("—" if done else None),
            "warehouses": shipping.warehouse_options(db, order_id) if lines and not done else [],
        })

    @blueprint.route("/dealer/orders/<int:order_id>/payments", methods=["POST"])
    @access_required
    def add_payment(order_id):
        # "Пробить чек" ticked on the form. A form without the checkbox at all
        # (an old page still open in a tab) only records the payment, as before.
        punch = bool(request.form.get("fiscalize")) and "fiscalize_shown" in request.form
        ok, message = services.add_payment(
            get_db(), order_id, request.form.get("amount"), request.form.get("payment_type", ""),
            request.form.get("paid_date"), request.form.get("note"),
            punch_receipt=punch, is_correction=bool(request.form.get("is_correction")), receipts=receipts,
        )
        notice("success" if ok else "error", message)
        return redirect(url_for("dealer.order", order_id=order_id) + "#payments")

    @blueprint.route("/dealer/orders/<int:order_id>/payments/<int:payment_id>/delete", methods=["POST"])
    @access_required
    def delete_payment(order_id, payment_id):
        services.delete_payment(get_db(), order_id, payment_id)
        notice("success", "Платёж удалён.")
        return redirect(url_for("dealer.order", order_id=order_id) + "#payments")

    @blueprint.route("/dealer/orders/<int:order_id>/payments/<int:payment_id>/receipt/check", methods=["POST"])
    @access_required
    def check_receipt(order_id, payment_id):
        ok, message = services.check_receipt(get_db(), order_id, payment_id, receipts)
        notice("success" if ok else "error", message)
        return redirect(url_for("dealer.order", order_id=order_id) + "#payments")

    @blueprint.route("/dealer/orders/<int:order_id>/payments/<int:payment_id>/receipt/retry", methods=["POST"])
    @access_required
    def retry_receipt(order_id, payment_id):
        ok, message = services.retry_receipt(get_db(), order_id, payment_id, receipts)
        notice("success" if ok else "error", message)
        return redirect(url_for("dealer.order", order_id=order_id) + "#payments")

    @blueprint.route("/dealer/orders/<int:order_id>/payments/<int:payment_id>/receipt.pdf")
    @access_required
    def receipt_pdf(order_id, payment_id):
        if receipts is None:
            return "Чеки не подключены.", 404
        return receipts.pdf_response(get_db(), order_id, payment_id)

    @blueprint.route("/dealer/orders/<int:order_id>/delete", methods=["POST"])
    @access_required
    def delete_order(order_id):
        db = get_db()
        existing = repository.get_order(db, order_id)
        order_type = existing["order_type"] if existing is not None else ORDER_TYPE_EQUIPMENT
        ok, message = services.delete_order(db, order_id)
        notice("success" if ok else "error", message)
        if ok:
            if forget_events:
                forget_events(db, order_id)
                db.commit()
            return redirect(list_url(order_type))
        return redirect(url_for("dealer.order", order_id=order_id))

    @blueprint.route("/dealer/orders/<int:order_id>/tuning", methods=["POST"])
    @access_required
    def make_tuning_order(order_id):
        db = get_db()
        row = repository.get_order(db, order_id)
        if row is None or create_tuning_order is None or row["order_type"] != ORDER_TYPE_EQUIPMENT:
            return redirect(url_for("dealer.orders"))
        tuning_order_id = create_tuning_order(
            db, dict(row), [dict(i) for i in repository.list_items(db, order_id)], session.get("admin_id")
        )
        if tuning_order_id is None:
            notice("error", "В заказе нет лодки или мотора — заказ на тюнинг не создан.")
            return redirect(url_for("dealer.order", order_id=order_id))
        return redirect(tuning_order_url(tuning_order_id))

    # ---- price list

    @blueprint.route("/dealer/prices", methods=["GET", "POST"])
    @access_required
    def prices():
        db = get_db()
        catalog, known = catalog_maps(db)
        if request.method == "POST":
            error, saved = services.save_prices(
                db, list(zip(request.form.getlist("profile_id[]"), request.form.getlist("price[]"))), known
            )
            notice("error" if error else "success", error or "Прайс-лист сохранён.")
            return redirect(url_for("dealer.prices"))
        positions = repository.price_list_positions(db)
        listed = {row["id"] for row in positions}
        return render_template(
            "dealer/prices.html", active_page="dealer", sub_page="prices",
            positions=positions, item_kinds=ITEM_KINDS, notice=session.pop("dealer_notice", None),
            addable=[row for row in catalog if row["id"] not in listed],
        )

    @blueprint.route("/dealer/prices/add", methods=["POST"])
    @access_required
    def add_price():
        db = get_db()
        _, known = catalog_maps(db)
        error, message = services.add_to_price_list(db, request.form, known, create_profile)
        notice("error" if error else "success", error or message)
        return redirect(url_for("dealer.prices"))

    @blueprint.route("/dealer/prices/<int:profile_id>/remove", methods=["POST"])
    @access_required
    def remove_price(profile_id):
        ok, message = services.remove_from_price_list(get_db(), profile_id)
        notice("success" if ok else "error", message)
        return redirect(url_for("dealer.prices"))

    return blueprint
