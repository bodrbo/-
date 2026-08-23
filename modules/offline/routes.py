"""Captain offline workspace, bootstrap snapshot and replay endpoint."""

import hashlib
import json
import os

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from modules.fleet.constants import (
    BOATS,
    CHECKLIST_QUESTIONS,
    CHECKLIST_TYPE_LABELS,
)

from . import repository, services


def _template_version():
    encoded = json.dumps(
        CHECKLIST_QUESTIONS, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def create_offline_blueprint(
    get_db,
    team_login_required,
    employee_has_position,
    checklist_questions_for,
    notify_events,
    allowed_photo_extensions,
):
    blueprint = Blueprint("offline", __name__)

    def captain_context():
        employee_name = session.get("team_employee_name") or ""
        allowed = employee_has_position(get_db(), employee_name, "Капитан")
        return employee_name, allowed

    @blueprint.route("/team/offline")
    @team_login_required
    def workspace():
        employee_name, allowed = captain_context()
        if not allowed:
            return redirect(url_for("team_dashboard"))
        return render_template(
            "team_offline.html",
            employee_name=employee_name,
            boats=BOATS,
        )

    @blueprint.route("/api/offline/bootstrap")
    @team_login_required
    def bootstrap():
        employee_name, allowed = captain_context()
        if not allowed:
            return jsonify(ok=False, error="Офлайн-режим доступен только капитанам."), 403

        documents_by_boat = {boat["name"]: [] for boat in BOATS}
        for row in repository.list_documents(get_db()):
            document = dict(row)
            document["url"] = url_for(
                "team_download_boat_document", doc_id=row["id"]
            )
            document["extension"] = os.path.splitext(row["filename"])[1].lower()
            documents_by_boat.setdefault(row["boat"], []).append(document)

        boats = []
        for index, boat in enumerate(BOATS):
            boat_name = boat["name"]
            checklists = {}
            for checklist_type, label in CHECKLIST_TYPE_LABELS.items():
                checklists[checklist_type] = {
                    "label": label,
                    "questions": checklist_questions_for(checklist_type, boat_name),
                }
            boats.append(
                {
                    "index": index,
                    "name": boat_name,
                    "documents": documents_by_boat.get(boat_name, []),
                    "checklists": checklists,
                }
            )
        return jsonify(
            ok=True,
            schema_version=1,
            template_version=_template_version(),
            generated_at=services.current_timestamp(),
            employee={"name": employee_name},
            boats=boats,
        )

    @blueprint.route("/api/offline/sync", methods=["POST"])
    @team_login_required
    def sync():
        employee_name, allowed = captain_context()
        if not allowed:
            return jsonify(ok=False, error="Офлайн-режим доступен только капитанам."), 403
        try:
            operation = json.loads(request.form.get("operation") or "")
        except (TypeError, ValueError):
            return jsonify(ok=False, retryable=False, error="Некорректный пакет синхронизации."), 400

        try:
            result = services.sync_operation(
                get_db(),
                operation,
                request.files.getlist("attachments"),
                employee_name,
                os.path.join(current_app.static_folder, "checklist_photos"),
                allowed_photo_extensions,
            )
        except services.OfflineValidationError as error:
            return jsonify(ok=False, retryable=False, error=str(error)), 400

        if result["created"]:
            try:
                notify_events(result["events"])
            except Exception:
                # The operation itself is already safely stored. A transient
                # notification failure must not make the device replay it.
                current_app.logger.exception("Offline sync notification failed")
        return jsonify(
            ok=True,
            created=result["created"],
            operation_id=result["operation_id"],
            record_id=result["record_id"],
        )

    return blueprint
