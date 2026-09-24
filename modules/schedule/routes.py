"""Administrator routes for the internal trip schedule."""

import datetime as dt

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, session, url_for

from modules.clients.constants import CLIENT_CONTACT_METHODS
from modules.excursion_services import repository as service_repository

from modules.weather import services as weather_services

from . import notifications as schedule_notifications
from . import repository, services, tripster_services
from .constants import CREW_ROLES, ITEM_KINDS


def create_schedule_blueprint(
    get_db,
    access_required,
    manage_required,
    is_manager_view,
    is_team_view,
    can_view_team_clients,
    boats,
    boat_colors,
    avatar_url,
    employee_notifier=None,
    tripster_fetcher=None,
    tripster_configured=lambda: False,
    cron_secret=None,
    yookassa_configured=lambda: False,
    yookassa_request=None,
    receipt_vat_code=lambda: 1,
    phone_normalizer=lambda phone: phone,
    weather_configured=lambda: False,
    weather_sync=None,
    create_trip_from_schedule=None,
    get_role_rate=None,
    apply_minimum_shift=None,
    delete_linked_trip=None,
    update_linked_trip_time=None,
    receipts=None,
):
    blueprint = Blueprint("schedule", __name__)

    def request_boats(db):
        return boats(db) if callable(boats) else boats

    def request_boat_colors(db):
        return boat_colors(db) if callable(boat_colors) else boat_colors

    def redirect_to_day(day, selected_employee="all"):
        return redirect(url_for(
            "schedule.index", date=day, employee=selected_employee
        ))

    def set_notice(message, success):
        session["schedule_notice"] = {
            "message": message,
            "type": "success" if success else "error",
        }

    def notify_item_changes(before, after):
        return schedule_notifications.notify_item_changes(
            get_db(), before, after, employee_notifier
        )

    @blueprint.route("/schedule")
    @access_required
    def index():
        db = get_db()
        day = services.parse_day(request.args.get("date"))
        selected_employee = request.args.get("employee", "all")
        team_view = is_team_view()
        demo_view = bool(session.get("demo_tenant_id"))
        can_view_clients = team_view and can_view_team_clients()
        context = services.day_view(
            db,
            day,
            selected_employee,
            request_boats(db),
            request_boat_colors(db),
            avatar_url,
            include_unassigned_tripster=not team_view,
            include_tripster=not demo_view,
            attach_weather=weather_services.attach_forecast,
        )
        return render_template(
            "schedule/index.html",
            **context,
            active_page="schedule",
            sub_page="excursions",
            day=day,
            day_label=services.day_label(day),
            previous_day=(day - dt.timedelta(days=1)).isoformat(),
            next_day=(day + dt.timedelta(days=1)).isoformat(),
            today=dt.date.today().isoformat(),
            boats=request_boats(db),
            trip_services=service_repository.list_services(db),
            addon_products=service_repository.list_addon_products(db),
            excursion_partners=repository.list_excursion_partners(db),
            client_contact_methods=CLIENT_CONTACT_METHODS,
            item_kinds=ITEM_KINDS,
            crew_roles=CREW_ROLES,
            notice=session.pop("schedule_notice", None),
            manager_view=is_manager_view(),
            team_view=team_view,
            can_manage=not team_view,
            can_view_clients=can_view_clients,
            schedule_items_json=(
                services.readonly_item_details(context["items"])
                if can_view_clients
                else [] if team_view
                else context["items"]
            ),
            tripster_configured=not demo_view and tripster_configured(),
            yookassa_configured=yookassa_configured(),
            receipts_configured=bool(receipts is not None and receipts.configured()),
            weather_configured=weather_configured(),
        )

    @blueprint.route("/schedule/clients/search")
    @manage_required
    def search_clients():
        query = request.args.get("q", "").strip()
        if len(query) < 2:
            return jsonify({"clients": []})
        return jsonify({
            "clients": repository.search_clients(get_db(), query, limit=20)
        })

    @blueprint.route("/schedule/crew", methods=["POST"])
    @manage_required
    def add_crew_member():
        day = services.parse_day(request.form.get("work_date"))
        try:
            employee_id = int(request.form.get("employee_id", ""))
        except (TypeError, ValueError):
            employee_id = 0
        success, message = services.add_day_crew_member(
            get_db(), day, employee_id
        )
        set_notice(message, success)
        return redirect_to_day(day.isoformat())

    @blueprint.route(
        "/schedule/crew/<int:employee_id>/remove", methods=["POST"]
    )
    @manage_required
    def remove_crew_member(employee_id):
        day = services.parse_day(request.form.get("work_date"))
        success, message = services.remove_day_crew_member(
            get_db(), day, employee_id
        )
        set_notice(message, success)
        return redirect_to_day(day.isoformat())

    @blueprint.route("/schedule/items", methods=["POST"])
    @manage_required
    def create_item():
        day = services.parse_day(request.form.get("trip_date")).isoformat()
        selected_employee = request.form.get("return_employee", "all")
        db = get_db()
        success, message, item_id = services.save_item(
            db, request.form, request_boats(db), service_repository.list_services(db),
        )
        if success:
            notify_item_changes(
                None, schedule_notifications.item_snapshot(db, item_id)
            )
        set_notice(message, success)
        return redirect_to_day(day, selected_employee)

    @blueprint.route("/schedule/items/<int:item_id>", methods=["POST"])
    @manage_required
    def update_item(item_id):
        day = services.parse_day(request.form.get("trip_date")).isoformat()
        selected_employee = request.form.get("return_employee", "all")
        db = get_db()
        before = schedule_notifications.item_snapshot(db, item_id)
        success, message, _saved_id = services.save_item(
            db, request.form, request_boats(db), service_repository.list_services(db),
            item_id=item_id,
            keep_participants=request.form.get("keep_participants") == "1",
        )
        if success:
            if request.form.get("resolve_tripster") == "1":
                tripster_services.mark_item_resolved(
                    db, item_id, services.current_timestamp()
                )
            notify_item_changes(
                before, schedule_notifications.item_snapshot(db, item_id)
            )
        set_notice(message, success)
        return redirect_to_day(day, selected_employee)

    @blueprint.route("/schedule/items/<int:item_id>/move", methods=["POST"])
    @manage_required
    def move_item(item_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "Некорректный запрос."}), 400
        db = get_db()
        before = schedule_notifications.item_snapshot(db, item_id)
        success, message, result = services.move_item(
            db,
            item_id,
            payload.get("start_time"),
            payload.get("source_employee_id"),
            payload.get("target_employee_id"),
            update_linked_trip_time=update_linked_trip_time,
        )
        if not success:
            return jsonify({"ok": False, "message": message}), 400
        after = schedule_notifications.item_snapshot(db, item_id)
        notify_item_changes(before, after)
        return jsonify({"ok": True, "message": message, "item": result})

    @blueprint.route("/schedule/items/<int:item_id>/delete", methods=["POST"])
    @manage_required
    def delete_item(item_id):
        before = schedule_notifications.item_snapshot(get_db(), item_id)
        item = repository.get_item(get_db(), item_id)
        day = (
            item["starts_at"][:10]
            if item is not None
            else services.parse_day(request.form.get("return_date")).isoformat()
        )
        success, message = services.delete_item(
            get_db(), item_id, delete_linked_trip=delete_linked_trip,
        )
        if success:
            notify_item_changes(before, None)
        set_notice(message, success)
        return redirect_to_day(day, request.form.get("return_employee", "all"))

    @blueprint.route("/schedule/items/<int:item_id>/addons", methods=["POST"])
    @manage_required
    def add_item_addon(item_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "Некорректный запрос."}), 400
        try:
            client_id = int(payload.get("client_id"))
            product_id = int(payload.get("product_id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "Некорректные данные."}), 400
        success, message, product = services.add_participant_addon(
            get_db(), item_id, client_id, product_id, payload.get("quantity")
        )
        if not success:
            return jsonify({"ok": False, "message": message}), 400
        return jsonify({
            "ok": True, "message": message,
            "participants": repository.list_item_participants_with_addons(get_db(), item_id),
        })

    @blueprint.route(
        "/schedule/items/<int:item_id>/addons/<int:addon_id>/delete",
        methods=["POST"],
    )
    @manage_required
    def remove_item_addon(item_id, addon_id):
        success, message = services.remove_participant_addon(
            get_db(), item_id, addon_id
        )
        if not success:
            return jsonify({"ok": False, "message": message}), 400
        return jsonify({
            "ok": True, "message": message,
            "participants": repository.list_item_participants_with_addons(get_db(), item_id),
        })

    @blueprint.route("/schedule/items/<int:item_id>/participants", methods=["POST"])
    @manage_required
    def add_item_participant(item_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "Некорректный запрос."}), 400
        db = get_db()
        success, message, _participant_id = services.add_participant_quick(
            db, item_id, payload
        )
        if not success:
            return jsonify({"ok": False, "message": message}), 400
        item = repository.get_item(db, item_id)
        return jsonify({
            "ok": True, "message": message,
            "participants": repository.list_item_participants_with_addons(db, item_id),
            "participants_count": item["participants_count"] if item else 0,
            "capacity": item["capacity"] if item else None,
        })

    @blueprint.route(
        "/schedule/items/<int:item_id>/participants/<int:participant_id>",
        methods=["POST"],
    )
    @manage_required
    def update_item_participant(item_id, participant_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "Некорректный запрос."}), 400
        db = get_db()
        success, message = services.edit_participant(
            db, item_id, participant_id, payload
        )
        if not success:
            return jsonify({"ok": False, "message": message}), 400
        item = repository.get_item(db, item_id)
        return jsonify({
            "ok": True, "message": message,
            "participants": repository.list_item_participants_with_addons(db, item_id),
            "participants_count": item["participants_count"] if item else 0,
            "capacity": item["capacity"] if item else None,
        })

    @blueprint.route(
        "/schedule/items/<int:item_id>/participants/<int:participant_id>/delete",
        methods=["POST"],
    )
    @manage_required
    def delete_item_participant(item_id, participant_id):
        db = get_db()
        success, message = services.remove_participant(db, item_id, participant_id)
        if not success:
            return jsonify({"ok": False, "message": message}), 400
        item = repository.get_item(db, item_id)
        return jsonify({
            "ok": True, "message": message,
            "participants": repository.list_item_participants_with_addons(db, item_id),
            "participants_count": item["participants_count"] if item else 0,
            "capacity": item["capacity"] if item else None,
        })

    def _participants_response(db, item_id, message):
        item = repository.get_item(db, item_id)
        return jsonify({
            "ok": True, "message": message,
            "participants": repository.list_item_participants_with_addons(db, item_id),
            "participants_count": item["participants_count"] if item else 0,
            "capacity": item["capacity"] if item else None,
        })

    @blueprint.route(
        "/schedule/items/<int:item_id>/participants/<int:participant_id>"
        "/manual-payments",
        methods=["POST"],
    )
    @manage_required
    def create_participant_manual_payment(item_id, participant_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "Некорректный запрос."}), 400
        db = get_db()
        success, message, payment_id = services.create_manual_payment(
            db,
            item_id,
            participant_id,
            payload.get("amount"),
            payload.get("payment_method"),
        )
        if not success:
            return jsonify({"ok": False, "message": message}), 400
        if receipts is not None and payment_id:
            # Best-effort: a cash-desk outage never blocks recording the payment.
            receipts.fiscalize(db, payment_id)
        return _participants_response(db, item_id, message)

    def _receipt_payment_or_none(db, kind, item_id, participant_id, payment_id):
        if kind == "manual":
            payment = repository.get_manual_payment_context(db, payment_id)
        elif kind == "online":
            payment = repository.get_online_payment_context(db, payment_id)
        else:
            return None
        if (
            payment is None
            or payment["schedule_item_id"] != item_id
            or payment["participant_id"] != participant_id
        ):
            return None
        return payment

    def _pdf_response(pdf_bytes, filename):
        response = current_app.response_class(pdf_bytes, mimetype="application/pdf")
        response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @blueprint.route(
        "/schedule/items/<int:item_id>/participants/<int:participant_id>"
        "/manual-payments/<int:payment_id>/receipt/<action>",
        methods=["POST"],
    )
    @manage_required
    def manual_payment_receipt_action(item_id, participant_id, payment_id, action):
        db = get_db()
        if receipts is None or action not in ("retry", "check"):
            return jsonify({"ok": False, "message": "Действие недоступно."}), 404
        if _receipt_payment_or_none(db, "manual", item_id, participant_id, payment_id) is None:
            return jsonify({"ok": False, "message": "Платёж не найден."}), 404
        if action == "retry":
            receipts.fiscalize(db, payment_id)
            return _participants_response(db, item_id, "Чек отправлен на кассу повторно.")
        receipts.check(db, payment_id)
        return _participants_response(db, item_id, "Статус чека обновлён.")

    @blueprint.route(
        "/schedule/items/<int:item_id>/participants/<int:participant_id>"
        "/receipts/<kind>/<int:payment_id>.pdf"
    )
    @manage_required
    def participant_receipt_pdf(item_id, participant_id, kind, payment_id):
        db = get_db()
        if receipts is None or _receipt_payment_or_none(
            db, kind, item_id, participant_id, payment_id
        ) is None:
            return "Чек не найден.", 404
        pdf_bytes, info = receipts.pdf(db, kind, payment_id)
        if pdf_bytes is None:
            return info, 404
        return _pdf_response(pdf_bytes, info)

    @blueprint.route(
        "/schedule/items/<int:item_id>/participants/<int:participant_id>"
        "/receipts/<kind>/<int:payment_id>/link"
    )
    @manage_required
    def participant_receipt_link(item_id, participant_id, kind, payment_id):
        """Public download link the manager can send to the client — the
        client's own token authorises it, like the cabinet's tuning receipts."""
        db = get_db()
        payment = _receipt_payment_or_none(db, kind, item_id, participant_id, payment_id)
        if receipts is None or payment is None:
            return jsonify({"ok": False, "message": "Чек не найден."}), 404
        if not payment["client_token"]:
            return jsonify({"ok": False, "message": "У клиента нет личной ссылки."}), 400
        return jsonify({"ok": True, "url": url_for(
            "schedule.public_receipt_pdf", token=payment["client_token"],
            kind=kind, payment_id=payment_id, _external=True,
        )})

    @blueprint.route("/client/<token>/schedule-receipts/<kind>/<int:payment_id>.pdf")
    def public_receipt_pdf(token, kind, payment_id):
        db = get_db()
        if receipts is None:
            return "Чек не найден.", 404
        if kind == "manual":
            payment = repository.get_manual_payment_context(db, payment_id)
        elif kind == "online":
            payment = repository.get_online_payment_context(db, payment_id)
        else:
            payment = None
        if payment is None or not payment["client_token"] or payment["client_token"] != token:
            return "Чек не найден.", 404
        pdf_bytes, info = receipts.pdf(db, kind, payment_id)
        if pdf_bytes is None:
            return info, 404
        return _pdf_response(pdf_bytes, info)

    @blueprint.route(
        "/schedule/items/<int:item_id>/participants/<int:participant_id>"
        "/manual-payments/<int:payment_id>/delete",
        methods=["POST"],
    )
    @manage_required
    def delete_participant_manual_payment(
        item_id, participant_id, payment_id,
    ):
        db = get_db()
        success, message = services.remove_manual_payment(
            db, item_id, participant_id, payment_id
        )
        if not success:
            return jsonify({"ok": False, "message": message}), 404
        return _participants_response(db, item_id, message)

    @blueprint.route(
        "/schedule/items/<int:item_id>/participants/<int:participant_id>/yookassa",
        methods=["POST"],
    )
    @manage_required
    def create_participant_payment(item_id, participant_id):
        if not yookassa_configured() or yookassa_request is None:
            return jsonify({"ok": False, "message": "ЮKassa не настроена на сервере."}), 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "Некорректный запрос."}), 400
        db = get_db()
        return_url = url_for("home", _external=True)
        success, message, _payment_id = services.create_participant_payment(
            db, item_id, participant_id, payload.get("amount"),
            yookassa_request, receipt_vat_code(), phone_normalizer, return_url,
        )
        if not success:
            return jsonify({"ok": False, "message": message}), 400
        return _participants_response(db, item_id, message)

    @blueprint.route(
        "/schedule/items/<int:item_id>/participants/<int:participant_id>"
        "/yookassa/<int:payment_id>/check",
        methods=["POST"],
    )
    @manage_required
    def check_participant_payment(item_id, participant_id, payment_id):
        if not yookassa_configured() or yookassa_request is None:
            return jsonify({"ok": False, "message": "ЮKassa не настроена на сервере."}), 400
        db = get_db()
        record = repository.get_yookassa_payment(db, payment_id, participant_id)
        if record is None:
            return jsonify({"ok": False, "message": "Ссылка на оплату не найдена."}), 404
        try:
            services.sync_participant_payment(db, record, yookassa_request)
        except Exception as error:
            return jsonify({"ok": False, "message": f"Не удалось проверить оплату: {error}"}), 400
        return _participants_response(db, item_id, "Статус оплаты обновлён.")

    @blueprint.route(
        "/schedule/items/<int:item_id>/participants/<int:participant_id>"
        "/yookassa/<int:payment_id>/delete",
        methods=["POST"],
    )
    @manage_required
    def delete_participant_payment(item_id, participant_id, payment_id):
        db = get_db()
        record = repository.get_yookassa_payment(db, payment_id, participant_id)
        if record is None:
            return jsonify({"ok": False, "message": "Ссылка на оплату не найдена."}), 404
        if record["status"] == "waiting_for_capture" and not yookassa_configured():
            return jsonify({"ok": False, "message": "ЮKassa не настроена на сервере."}), 400
        success, message = services.delete_participant_payment(
            db, record, yookassa_request
        )
        if not success:
            return jsonify({"ok": False, "message": message}), 400
        return _participants_response(db, item_id, message)

    @blueprint.route("/schedule/tripster/sync", methods=["POST"])
    @manage_required
    def sync_tripster():
        if session.get("demo_tenant_id"):
            # Real Tripster bookings must never land in a demo tenant's
            # own database — see no_real_data_for_demo_tenant in app.py.
            abort(404)
        day = services.parse_day(request.form.get("return_date")).isoformat()
        selected_employee = request.form.get("return_employee", "all")
        if not tripster_configured() or tripster_fetcher is None:
            set_notice("Токен Tripster не настроен на сервере.", False)
            return redirect_to_day(day, selected_employee)
        try:
            stats = tripster_services.sync_orders(
                get_db(), tripster_fetcher, force_full=True,
                employee_notifier=employee_notifier,
            )
        except (RuntimeError, ValueError) as error:
            set_notice(f"Не удалось загрузить Tripster: {error}", False)
        else:
            set_notice(
                "Tripster обновлён: "
                f"новых рейсов — {stats['created']}, "
                f"обновлено — {stats['updated']}, "
                f"сопоставлено с услугами — {stats['matched']}, "
                f"отмен — {stats['cancelled']}, "
                f"ожидают оплаты или даты — {stats['pending']}.",
                True,
            )
        return redirect_to_day(day, selected_employee)

    @blueprint.route("/schedule/tripster/merge-candidates")
    @manage_required
    def tripster_merge_candidates():
        if session.get("demo_tenant_id"):
            abort(404)
        day = services.parse_day(request.args.get("date")).isoformat()
        try:
            source_item_id = int(request.args.get("source_item_id", ""))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "Некорректная карточка Tripster."}), 400
        return jsonify({
            "ok": True,
            "date": day,
            "items": repository.list_tripster_merge_candidates(
                get_db(), day, source_item_id
            ),
        })

    @blueprint.route(
        "/schedule/items/<int:item_id>/tripster/attach", methods=["POST"]
    )
    @manage_required
    def attach_tripster_item(item_id):
        if session.get("demo_tenant_id"):
            abort(404)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "Некорректный запрос."}), 400
        try:
            target_item_id = int(payload.get("target_item_id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "Выберите рейс."}), 400
        success, message, target = tripster_services.attach_item_to_existing_trip(
            get_db(), item_id, target_item_id, services.current_timestamp()
        )
        if not success:
            return jsonify({"ok": False, "message": message}), 400
        return jsonify({
            "ok": True,
            "message": message,
            "target_item_id": target_item_id,
            "redirect_url": url_for(
                "schedule.index", date=target["starts_at"][:10], employee="all"
            ),
        })

    @blueprint.route("/schedule/weather/sync", methods=["POST"])
    @manage_required
    def sync_weather():
        day = services.parse_day(request.form.get("return_date")).isoformat()
        selected_employee = request.form.get("return_employee", "all")
        if not weather_configured() or weather_sync is None:
            set_notice("Ключ OpenWeather не настроен на сервере.", False)
            return redirect_to_day(day, selected_employee)
        try:
            stats = weather_sync(get_db())
        except Exception as error:
            # Wider than the Tripster catch beside it on purpose: this
            # wraps a network call plus a database write, and a raw 500
            # here is worse than a slightly-too-broad catch — the same
            # call is already wrapped this broadly in the hourly cron.
            set_notice(f"Не удалось обновить прогноз погоды: {error}", False)
        else:
            set_notice(
                "Прогноз погоды обновлён: "
                f"часов синхронизировано — {stats['hours_synced']}, "
                f"предупреждений капитанам отправлено — {stats['alerts_sent']}.",
                True,
            )
        return redirect_to_day(day, selected_employee)

    @blueprint.route("/internal/cron/sync-tripster")
    def cron_sync_tripster():
        if session.get("demo_tenant_id"):
            abort(404)
        if not cron_secret or request.args.get("token") != cron_secret:
            return "forbidden", 403
        if not tripster_configured() or tripster_fetcher is None:
            return "tripster not configured", 503
        try:
            stats = tripster_services.sync_orders(
                get_db(), tripster_fetcher,
                employee_notifier=employee_notifier,
            )
        except (RuntimeError, ValueError) as error:
            return f"error: {error}", 502
        return (
            f"ok: {stats['received']} received, {stats['created']} created, "
            f"{stats['updated']} updated, {stats['cancelled']} cancelled, "
            f"{stats['matched']} matched, {stats['pending']} pending, "
            f"{stats['invalid']} invalid",
            200,
        )

    @blueprint.route("/internal/cron/close-schedule-items")
    def cron_close_schedule_items():
        if not cron_secret or request.args.get("token") != cron_secret:
            return "forbidden", 403
        if create_trip_from_schedule is None or get_role_rate is None:
            return "not configured", 503
        stats = services.auto_close_schedule_items(
            get_db(), create_trip_from_schedule, get_role_rate,
            apply_minimum_shift=apply_minimum_shift,
        )
        return (
            f"ok: {stats['closed']} closed, {stats['needs_review']} flagged, "
            f"{stats['skipped']} skipped",
            200,
        )

    return blueprint
