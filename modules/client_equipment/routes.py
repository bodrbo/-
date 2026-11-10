"""Add / edit / delete routes of the client's equipment (staff only)."""

from flask import Blueprint, redirect, request, session

from . import repository, services


def create_client_equipment_blueprint(get_db, access_required, client_exists, back_url):
    """client_exists(db, client_id) -> bool; back_url(client_id) -> the
    client's cabinet — both injected from app.py."""
    blueprint = Blueprint("client_equipment", __name__)

    def back(client_id):
        return redirect(back_url(client_id) + "#equipment")

    @blueprint.route("/admin/clients/<int:client_id>/equipment/add", methods=["POST"])
    @access_required
    def add(client_id):
        db = get_db()
        if not client_exists(db, client_id):
            return redirect("/")
        ok, message = services.add_item(db, client_id, request.form)
        session["client_equipment_notice" if ok else "client_equipment_error"] = message
        return back(client_id)

    @blueprint.route("/admin/clients/<int:client_id>/equipment/<int:item_id>/update", methods=["POST"])
    @access_required
    def update(client_id, item_id):
        db = get_db()
        item = repository.get(db, item_id)
        if item is None or item["client_id"] != client_id:
            return back(client_id)
        ok, message = services.update_item(db, item_id, request.form)
        session["client_equipment_notice" if ok else "client_equipment_error"] = message
        return back(client_id)

    @blueprint.route("/admin/clients/<int:client_id>/equipment/<int:item_id>/delete", methods=["POST"])
    @access_required
    def delete(client_id, item_id):
        db = get_db()
        item = repository.get(db, item_id)
        if item is not None and item["client_id"] == client_id:
            _ok, message = services.delete_item(db, item_id)
            session["client_equipment_notice"] = message
        return back(client_id)

    return blueprint
