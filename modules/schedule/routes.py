"""Administrator routes for the internal trip schedule."""

import datetime as dt

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from . import repository, services
from .constants import CREW_ROLES, ITEM_KINDS


def create_schedule_blueprint(
    get_db,
    admin_login_required,
    boats,
    boat_colors,
    trip_services,
    avatar_url,
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

    @blueprint.route("/schedule")
    @admin_login_required
    def index():
        day = services.parse_day(request.args.get("date"))
        selected_employee = request.args.get("employee", "all")
        context = services.day_view(
            get_db(), day, selected_employee, boats, boat_colors, avatar_url
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
            trip_services=trip_services,
            item_kinds=ITEM_KINDS,
            crew_roles=CREW_ROLES,
            notice=session.pop("schedule_notice", None),
        )

    @blueprint.route("/schedule/clients/search")
    @admin_login_required
    def search_clients():
        query = request.args.get("q", "").strip()
        if len(query) < 2:
            return jsonify({"clients": []})
        return jsonify({
            "clients": repository.search_clients(get_db(), query, limit=20)
        })

    @blueprint.route("/schedule/crew", methods=["POST"])
    @admin_login_required
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
    @admin_login_required
    def remove_crew_member(employee_id):
        day = services.parse_day(request.form.get("work_date"))
        success, message = services.remove_day_crew_member(
            get_db(), day, employee_id
        )
        set_notice(message, success)
        return redirect_to_day(day.isoformat())

    @blueprint.route("/schedule/items", methods=["POST"])
    @admin_login_required
    def create_item():
        day = services.parse_day(request.form.get("trip_date")).isoformat()
        selected_employee = request.form.get("return_employee", "all")
        success, message, _item_id = services.save_item(
            get_db(), request.form, boats, trip_services
        )
        set_notice(message, success)
        return redirect_to_day(day, selected_employee)

    @blueprint.route("/schedule/items/<int:item_id>", methods=["POST"])
    @admin_login_required
    def update_item(item_id):
        day = services.parse_day(request.form.get("trip_date")).isoformat()
        selected_employee = request.form.get("return_employee", "all")
        success, message, _saved_id = services.save_item(
            get_db(), request.form, boats, trip_services, item_id=item_id
        )
        set_notice(message, success)
        return redirect_to_day(day, selected_employee)

    @blueprint.route("/schedule/items/<int:item_id>/delete", methods=["POST"])
    @admin_login_required
    def delete_item(item_id):
        item = repository.get_item(get_db(), item_id)
        day = (
            item["starts_at"][:10]
            if item is not None
            else services.parse_day(request.form.get("return_date")).isoformat()
        )
        success, message = services.delete_item(get_db(), item_id)
        set_notice(message, success)
        return redirect_to_day(day, request.form.get("return_employee", "all"))

    return blueprint
