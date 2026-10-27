"""Administrator routes of the Дилерский центр."""

import datetime as dt

from flask import Blueprint, redirect, render_template, request, session, url_for

from . import repository, services
from .constants import DEALER_STATUSES, ITEM_KINDS, PAYMENT_TYPES, STATUS_LABELS


def create_dealer_blueprint(get_db, access_required, client_choices, resolve_client):
    """client_choices(db) -> clients for the picker; resolve_client(db,
    client_id, client_name, phone) -> client id (finds or creates the client
    in the shared client base) — both injected from app.py."""
    blueprint = Blueprint("dealer", __name__)

    def catalog_maps(db):
        positions = repository.catalog_positions(db)
        return positions, {row["id"]: row["equipment_type"] for row in positions}

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
        db = get_db()
        query = request.args.get("q", "").strip()[:120]
        status = request.args.get("status", "").strip()
        if status not in STATUS_LABELS:
            status = ""
        rows = db.execute(
            "SELECT o.*, COALESCE((SELECT SUM(amount) FROM dealer_payments WHERE order_id = o.id), 0) AS paid, "
            "COALESCE((SELECT GROUP_CONCAT(name || CASE WHEN quantity > 1 THEN ' ×' || quantity ELSE '' END, '; ') "
            " FROM dealer_order_items WHERE order_id = o.id), '') AS items_text "
            "FROM dealer_orders o " + ("WHERE o.status = ? " if status else "")
            + "ORDER BY o.order_date DESC, o.id DESC",
            (status,) if status else (),
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
            "dealer/orders.html", active_page="dealer", sub_page="orders",
            orders=shown, statuses=DEALER_STATUSES, selected_status=status, search_query=query,
            total_sum=sum(o["total"] for o in counted),
            paid_sum=sum(o["paid"] for o in counted),
            remaining_sum=sum(o["remaining"] for o in counted),
            counted=len(counted), notice=session.pop("dealer_notice", None),
        )

    # ---- one order

    def render_form(db, order, items, form_state=None, error=None):
        positions, _ = catalog_maps(db)
        payments = paid = None
        if order is not None:
            payments = repository.list_payments(db, order["id"])
            paid = repository.paid_amount(db, order["id"])
        return render_template(
            "dealer/form.html", active_page="dealer", sub_page="orders",
            order=order, items=items, form_state=form_state, error=error,
            catalog=[
                {"id": p["id"], "kind": p["equipment_type"], "name": p["model_name"],
                 "brand": p["brand"] or "", "price": p["price"]}
                for p in positions
            ],
            item_kinds=ITEM_KINDS, client_choices=client_choices(db),
            statuses=DEALER_STATUSES, status_label=STATUS_LABELS.get(order["status"]) if order else None,
            payments=payments, paid=paid,
            remaining=max(0.0, round((order["total"] - paid), 2)) if order is not None else None,
            payment_types=PAYMENT_TYPES, today=dt.date.today().isoformat(),
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
                "serial_number": at("item_serial[]"), "quantity": at("item_quantity[]", "1"),
                "price": at("item_price[]"),
            })
        return {key: form.get(key, "") for key in (
            "client_name", "client_id", "phone", "order_date", "deadline_date",
            "discount_type", "discount_value", "comment",
        )}, items

    def save_from_request(db, order_id):
        _, known = catalog_maps(db)
        error, data = services.parse_order_form(request.form, known)
        if error:
            return error, None
        client_id = resolve_client(db, data["client_id"], data["client_name"], data["phone"])
        return None, services.save_order(db, order_id, data, client_id)

    @blueprint.route("/dealer/orders/new", methods=["GET", "POST"])
    @access_required
    def new_order():
        db = get_db()
        if request.method == "POST":
            error, order_id = save_from_request(db, None)
            if error is None:
                notice("success", "Заказ создан.")
                return redirect(url_for("dealer.order", order_id=order_id))
            state, items = state_from_form(request.form)
            return render_form(db, None, items, form_state=state, error=error), 400
        return render_form(db, None, [])

    @blueprint.route("/dealer/orders/<int:order_id>", methods=["GET", "POST"])
    @access_required
    def order(order_id):
        db = get_db()
        row = repository.get_order(db, order_id)
        if row is None:
            return redirect(url_for("dealer.orders"))
        if request.method == "POST":
            error, _ = save_from_request(db, order_id)
            if error is None:
                notice("success", "Заказ сохранён.")
                return redirect(url_for("dealer.order", order_id=order_id))
            state, items = state_from_form(request.form)
            return render_form(db, dict(row), items, form_state=state, error=error), 400
        return render_form(db, dict(row), [dict(i) for i in repository.list_items(db, order_id)])

    @blueprint.route("/dealer/orders/<int:order_id>/status", methods=["POST"])
    @access_required
    def update_status(order_id):
        ok, message = services.set_status(get_db(), order_id, request.form.get("status", ""))
        notice("success" if ok else "error", message)
        target = request.form.get("next", "").strip()
        if target.startswith("/") and not target.startswith("//") and "\\" not in target and len(target) <= 600:
            return redirect(target)
        return redirect(url_for("dealer.order", order_id=order_id))

    @blueprint.route("/dealer/orders/<int:order_id>/payments", methods=["POST"])
    @access_required
    def add_payment(order_id):
        ok, message = services.add_payment(
            get_db(), order_id, request.form.get("amount"), request.form.get("payment_type", ""),
            request.form.get("paid_date"), request.form.get("note"),
        )
        notice("success" if ok else "error", message)
        return redirect(url_for("dealer.order", order_id=order_id) + "#payments")

    @blueprint.route("/dealer/orders/<int:order_id>/payments/<int:payment_id>/delete", methods=["POST"])
    @access_required
    def delete_payment(order_id, payment_id):
        services.delete_payment(get_db(), order_id, payment_id)
        notice("success", "Платёж удалён.")
        return redirect(url_for("dealer.order", order_id=order_id) + "#payments")

    @blueprint.route("/dealer/orders/<int:order_id>/delete", methods=["POST"])
    @access_required
    def delete_order(order_id):
        ok, message = services.delete_order(get_db(), order_id)
        notice("success" if ok else "error", message)
        if ok:
            return redirect(url_for("dealer.orders"))
        return redirect(url_for("dealer.order", order_id=order_id))

    # ---- price list

    @blueprint.route("/dealer/prices", methods=["GET", "POST"])
    @access_required
    def prices():
        db = get_db()
        positions, known = catalog_maps(db)
        if request.method == "POST":
            error, saved = services.save_prices(
                db, list(zip(request.form.getlist("profile_id[]"), request.form.getlist("price[]"))), known
            )
            notice("error" if error else "success", error or "Прайс-лист сохранён.")
            return redirect(url_for("dealer.prices"))
        return render_template(
            "dealer/prices.html", active_page="dealer", sub_page="prices",
            positions=positions, item_kinds=ITEM_KINDS, notice=session.pop("dealer_notice", None),
        )

    return blueprint
