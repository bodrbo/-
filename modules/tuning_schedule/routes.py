"""Administrator routes for the tuning-center work schedule — a day-based
roster + task calendar, alongside (not instead of) the excursion schedule's
routes.py. See modules/tuning_schedule/services.py for why task creation
funnels through app.py's own tuning_item_assignments helpers rather than
reimplementing them."""

import datetime as dt

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from . import repository, services


def create_tuning_schedule_blueprint(
    get_db,
    access_required,
    manage_required,
    avatar_url,
    create_order_assignment,
    update_order_assignment_status,
    pay_free_task,
    assignment_status_choices,
):
    blueprint = Blueprint("tuning_schedule", __name__)
    assignment_status_values = {choice["value"] for choice in assignment_status_choices}

    def redirect_to_day(day):
        return redirect(url_for("tuning_schedule.index", date=day))

    def set_notice(message, success):
        session["schedule_notice"] = {
            "message": message,
            "type": "success" if success else "error",
        }

    @blueprint.route("/schedule/tuning")
    @access_required
    def index():
        db = get_db()
        day = services.parse_day(request.args.get("date"))
        context = services.day_view(db, day)
        for employee in context["crew"]:
            employee["avatar_url"] = avatar_url(employee["name"])
        return render_template(
            "schedule/tuning_index.html",
            **context,
            active_page="schedule",
            sub_page="tuning",
            day=day,
            day_label=services.day_label(day),
            previous_day=(day - dt.timedelta(days=1)).isoformat(),
            next_day=(day + dt.timedelta(days=1)).isoformat(),
            today=dt.date.today().isoformat(),
            linkable_orders=repository.list_linkable_orders(db),
            assignment_statuses=assignment_status_choices,
            notice=session.pop("schedule_notice", None),
        )

    @blueprint.route("/schedule/tuning/crew", methods=["POST"])
    @manage_required
    def add_crew_member():
        day = services.parse_day(request.form.get("work_date"))
        try:
            employee_id = int(request.form.get("employee_id", ""))
        except (TypeError, ValueError):
            employee_id = 0
        success, message = services.add_day_crew_member(get_db(), day, employee_id)
        set_notice(message, success)
        return redirect_to_day(day.isoformat())

    @blueprint.route("/schedule/tuning/crew/<int:employee_id>/remove", methods=["POST"])
    @manage_required
    def remove_crew_member(employee_id):
        day = services.parse_day(request.form.get("work_date"))
        success, message = services.remove_day_crew_member(get_db(), day, employee_id)
        set_notice(message, success)
        return redirect_to_day(day.isoformat())

    @blueprint.route("/schedule/tuning/orders/<int:order_id>/items")
    @manage_required
    def order_items(order_id):
        return jsonify({
            "items": [
                dict(row) for row in repository.list_order_work_items(get_db(), order_id)
            ]
        })

    def _collect_day_hours(form):
        """A single-day task submits work_date/planned_hours; a
        multi-day one submits parallel work_date[]/planned_hours[] rows,
        one per day the admin filled in."""
        dates = form.getlist("work_date[]") or (
            [form.get("work_date")] if form.get("work_date") else []
        )
        hours = form.getlist("planned_hours[]") or (
            [form.get("planned_hours")] if form.get("planned_hours") else []
        )
        return list(zip(dates, hours))

    @blueprint.route("/schedule/tuning/tasks", methods=["POST"])
    @manage_required
    def create_task():
        db = get_db()
        day = services.parse_day(request.form.get("return_date")).isoformat()
        employee_name = request.form.get("employee_name", "")
        rate = request.form.get("rate", "")
        comment = request.form.get("comment", "")
        day_hours = _collect_day_hours(request.form)
        order_item_id = request.form.get("order_item_id", "").strip()
        order_id = request.form.get("order_id", "").strip()

        if order_item_id and order_id:
            try:
                item = repository.get_order_item(db, int(order_id), int(order_item_id))
            except ValueError:
                item = None
            if item is None:
                set_notice("Работа не найдена в заказе.", False)
                return redirect_to_day(day)
            success, message, _task_id = services.create_linked_task(
                db, item, employee_name, rate, comment, day_hours,
                create_order_assignment,
            )
        else:
            title = request.form.get("title", "")
            success, message, _task_id = services.create_free_task(
                db, employee_name, title, rate, comment, day_hours,
            )
        set_notice(message, success)
        return redirect_to_day(day)

    @blueprint.route("/schedule/tuning/tasks/<int:task_id>/days", methods=["POST"])
    @manage_required
    def add_task_day(task_id):
        day = services.parse_day(request.form.get("return_date")).isoformat()
        success, message = services.add_task_day(
            get_db(), task_id,
            request.form.get("work_date", ""), request.form.get("planned_hours", ""),
        )
        set_notice(message, success)
        return redirect_to_day(day)

    @blueprint.route(
        "/schedule/tuning/tasks/<int:task_id>/days/<int:day_id>/remove", methods=["POST"]
    )
    @manage_required
    def remove_task_day(task_id, day_id):
        day = services.parse_day(request.form.get("return_date")).isoformat()
        success, message = services.remove_task_day(get_db(), task_id, day_id)
        set_notice(message, success)
        return redirect_to_day(day)

    @blueprint.route("/schedule/tuning/tasks/<int:task_id>/status", methods=["POST"])
    @manage_required
    def set_task_status(task_id):
        day = services.parse_day(request.form.get("return_date")).isoformat()
        status = request.form.get("status", "").strip()
        if status not in assignment_status_values:
            set_notice("Некорректный статус.", False)
            return redirect_to_day(day)
        success, message = services.set_task_status(
            get_db(), task_id, status, pay_free_task, update_order_assignment_status,
        )
        set_notice(message, success)
        return redirect_to_day(day)

    return blueprint
