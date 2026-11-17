"""Add / delete routes of the order events (shared by tuning and dealer orders)."""

from flask import Blueprint, redirect, request, session

from . import repository, services
from .constants import ORDER_KINDS


def create_order_events_blueprint(get_db, access_required, order_context, order_url, make_visit_task, delete_task,
                                  make_event_task=None):
    """order_context(db, order_kind, order_id) -> {"client", "label",
    "tuning_order_id"} or None when there is no such order; order_url(
    order_kind, order_id) -> the order's page; make_visit_task(db, host,
    title, comment, day, start, hours, tuning_order_id) -> (ok, message,
    task_id) and delete_task(db, task_id) -> (ok, message) — the tuning
    schedule, injected from app.py. make_event_task (see
    services.add_event_task) enables "Создать задачу" under the events of a
    tuning order."""
    blueprint = Blueprint("order_events", __name__)

    def back(order_kind, order_id):
        return redirect(order_url(order_kind, order_id) + "#events")

    @blueprint.route("/events/<order_kind>/<int:order_id>/add", methods=["POST"])
    @access_required
    def add(order_kind, order_id):
        if order_kind not in ORDER_KINDS:
            return redirect("/")
        db = get_db()
        context = order_context(db, order_kind, order_id)
        if context is None:
            return redirect("/")
        ok, message = services.add_manual_event(
            db, order_kind, order_id, request.form, session.get("admin_id"), context, make_visit_task,
        )
        session["order_events_notice" if ok else "order_events_error"] = message
        return back(order_kind, order_id)

    @blueprint.route("/events/<int:event_id>/delete", methods=["POST"])
    @access_required
    def delete(event_id):
        db = get_db()
        event = repository.get_event(db, event_id)
        if event is None:
            return redirect("/")
        _ok, message = services.delete_event(db, event_id, lambda task_id: delete_task(db, task_id))
        session["order_events_notice"] = message
        return back(event["order_kind"], event["order_id"])

    @blueprint.route("/events/<int:event_id>/task", methods=["POST"])
    @access_required
    def add_task(event_id):
        db = get_db()
        event = repository.get_event(db, event_id)
        if event is None:
            return redirect("/")
        context = order_context(db, event["order_kind"], event["order_id"])
        if make_event_task is None or context is None or event["order_kind"] != "tuning":
            return back(event["order_kind"], event["order_id"])
        ok, message = services.add_event_task(db, event_id, request.form, context, make_event_task)
        session["order_events_notice" if ok else "order_events_error"] = message
        return redirect(order_url(event["order_kind"], event["order_id"]) + f"#event-{event_id}")

    return blueprint
