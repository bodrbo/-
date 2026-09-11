"""Administrator routes for the internal trip schedule."""

import datetime as dt

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from modules.excursion_services import repository as service_repository

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
):
    blueprint = Blueprint("schedule", __name__)

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
        can_view_clients = team_view and can_view_team_clients()
        context = services.day_view(
            db,
            day,
            selected_employee,
            boats,
            boat_colors,
            avatar_url,
            include_unassigned_tripster=not team_view,
        )
        return render_template(
            "schedule/index.html",
            **context,
            active_page="schedule",
            day=day,
            day_label=services.day_label(day),
            previous_day=(day - dt.timedelta(days=1)).isoformat(),
            next_day=(day + dt.timedelta(days=1)).isoformat(),
            today=dt.date.today().isoformat(),
            boats=boats,
            trip_services=service_repository.list_services(db),
            addon_products=service_repository.list_addon_products(db),
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
            tripster_configured=tripster_configured(),
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
            db, request.form, boats, service_repository.list_services(db),
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
            db, request.form, boats, service_repository.list_services(db),
            item_id=item_id,
        )
        if success:
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
        success, message = services.delete_item(get_db(), item_id)
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

    @blueprint.route("/schedule/tripster/sync", methods=["POST"])
    @manage_required
    def sync_tripster():
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

    @blueprint.route("/internal/cron/sync-tripster")
    def cron_sync_tripster():
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

    return blueprint
