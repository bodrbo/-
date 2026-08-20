"""Administrator interface for excursion-payment reconciliation and refunds."""

import datetime as dt

from flask import Blueprint, redirect, render_template, request, session, url_for

from . import services


def create_refunds_blueprint(
    get_db,
    admin_login_required,
    yclients_records_fetcher,
    yclients_configured,
    yookassa_request,
    yookassa_configured,
    receipt_vat_code,
    receipt_payment_mode,
):
    blueprint = Blueprint("refunds", __name__)

    def period_from(values):
        today = dt.date.today()
        default_start = today - dt.timedelta(days=14)
        default_end = today + dt.timedelta(days=45)
        try:
            start = dt.date.fromisoformat(values.get("start_date", ""))
        except (TypeError, ValueError):
            start = default_start
        try:
            end = dt.date.fromisoformat(values.get("end_date", ""))
        except (TypeError, ValueError):
            end = default_end
        if start > end:
            start, end = end, start
        if (end - start).days > 366:
            end = start + dt.timedelta(days=366)
        return start.isoformat(), end.isoformat()

    def redirect_index(message, success, start_date=None, end_date=None):
        session["refunds_notice"] = {
            "message": message,
            "type": "success" if success else "error",
        }
        if not start_date or not end_date:
            saved_period = session.get("refunds_period") or ()
            if len(saved_period) == 2:
                start_date, end_date = saved_period
        params = {}
        if start_date and end_date:
            params.update(start_date=start_date, end_date=end_date)
        return redirect(url_for("refunds.index", **params))

    @blueprint.route("/trips/refunds")
    @admin_login_required
    def index():
        start_date, end_date = period_from(request.args)
        session["refunds_period"] = (start_date, end_date)
        search = " ".join(request.args.get("q", "").strip().split())[:100]
        status_filter = request.args.get("status", "all")
        if status_filter not in ("all", "linked", "unlinked", "refunded"):
            status_filter = "all"
        data = services.dashboard(get_db(), start_date, end_date, search, status_filter)
        return render_template(
            "refunds/index.html",
            **data,
            start_date=start_date,
            end_date=end_date,
            search=search,
            status_filter=status_filter,
            notice=session.pop("refunds_notice", None),
            yclients_configured=yclients_configured(),
            yookassa_configured=yookassa_configured(),
            active_page="trips",
        )

    @blueprint.route("/trips/refunds/sync", methods=["POST"])
    @admin_login_required
    def sync():
        start_date, end_date = period_from(request.form)
        if not yclients_configured() or not yookassa_configured():
            return redirect_index(
                "Для сверки должны быть настроены одновременно YCLIENTS и ЮKassa.",
                False,
                start_date,
                end_date,
            )
        try:
            raw_records = yclients_records_fetcher(start_date, end_date)
            records_saved = services.sync_yclients_records(get_db(), raw_records)
            payments_since = (
                dt.date.fromisoformat(start_date) - dt.timedelta(days=180)
            ).isoformat()
            stats = services.sync_yookassa_payments(
                get_db(), yookassa_request, payments_since
            )
        except Exception as error:
            return redirect_index(
                f"Не удалось завершить сверку: {error}", False, start_date, end_date
            )
        return redirect_index(
            f"Сверка завершена: записей YCLIENTS — {records_saved}, платежей ЮKassa — "
            f"{stats['saved']}, связаны автоматически — {stats['auto_linked']}.",
            True,
            start_date,
            end_date,
        )

    @blueprint.route(
        "/trips/refunds/records/<int:record_id>/payment", methods=["POST"]
    )
    @admin_login_required
    def attach_payment(record_id):
        try:
            success, message = services.link_remote_payment(
                get_db(),
                record_id,
                request.form.get("yookassa_payment_id", ""),
                yookassa_request,
                session.get("admin_name") or "Администратор",
            )
        except Exception as error:
            success, message = False, f"Не удалось получить платёж из ЮKassa: {error}"
        return redirect_index(message, success)

    @blueprint.route(
        "/trips/refunds/payments/<int:payment_id>/link", methods=["POST"]
    )
    @admin_login_required
    def link_payment(payment_id):
        try:
            record_id = int(request.form.get("record_id", ""))
        except ValueError:
            return redirect_index("Выберите запись YCLIENTS.", False)
        success, message = services.link_stored_payment(
            get_db(),
            payment_id,
            record_id,
            session.get("admin_name") or "Администратор",
        )
        return redirect_index(message, success)

    @blueprint.route(
        "/trips/refunds/payments/<int:payment_id>/unlink", methods=["POST"]
    )
    @admin_login_required
    def unlink_payment(payment_id):
        success, message = services.unlink_payment(get_db(), payment_id)
        return redirect_index(message, success)

    @blueprint.route(
        "/trips/refunds/payments/<int:payment_id>/refund", methods=["POST"]
    )
    @admin_login_required
    def create_refund(payment_id):
        try:
            success, message = services.create_refund(
                get_db(),
                payment_id,
                request.form.get("amount", ""),
                request.form.get("mode", "partial"),
                request.form.get("reason", ""),
                request.form.get("email", ""),
                request.form.get("operation_key", ""),
                request.form.get("confirmed") == "1",
                session.get("admin_name") or "Администратор",
                yookassa_request,
                receipt_vat_code,
                receipt_payment_mode,
            )
        except Exception as error:
            success, message = False, f"Возврат не выполнен: {error}"
        return redirect_index(message, success)

    @blueprint.route(
        "/trips/refunds/operations/<int:refund_id>/retry", methods=["POST"]
    )
    @admin_login_required
    def retry_refund(refund_id):
        success, message = services.retry_unknown_refund(
            get_db(), refund_id, yookassa_request
        )
        return redirect_index(message, success)

    return blueprint
