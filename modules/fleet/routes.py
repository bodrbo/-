"""HTTP routes for the fleet module."""

import os
import secrets

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from . import repository, services
from .constants import (
    BOAT_DOCUMENT_EXTENSIONS,
    BOATS,
    CHECKLIST_TYPE_LABELS,
    DEFECT_STATUSES,
)


def create_fleet_blueprint(get_db, admin_login_required):
    """Build the fleet Blueprint with the application's DB and auth adapters."""
    blueprint = Blueprint("fleet", __name__)

    @blueprint.route("/fleet")
    @admin_login_required
    def index():
        return render_template("fleet_index.html", boats=BOATS, active_page="fleet")

    @blueprint.route("/fleet/<int:boat_index>")
    @admin_login_required
    def boat_detail(boat_index):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))

        db = get_db()
        defects = services.defects_for_boat(db, boat)
        current_defects, archived_defects = services.split_defects(defects)
        return render_template(
            "fleet_boat.html",
            boat=boat,
            boat_index=boat_index,
            checklists=services.fleet_boat_checklists(db, boat),
            documents=repository.list_documents(db, boat),
            current_defects=current_defects,
            archived_defects=archived_defects,
            defect_statuses=DEFECT_STATUSES,
            open_defects_count=len(current_defects),
            assignable_employees=services.assignable_employees(db),
            checklist_type_labels=CHECKLIST_TYPE_LABELS,
            active_page="fleet",
        )

    @blueprint.route(
        "/fleet/<int:boat_index>/defects/<int:defect_id>", methods=["GET", "POST"]
    )
    @admin_login_required
    def defect_detail(boat_index, defect_id):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))

        db = get_db()
        defect = repository.get_defect(db, defect_id, boat)
        if defect is None:
            return redirect(url_for("fleet.boat_detail", boat_index=boat_index))
        if request.method == "POST":
            services.save_defect_case_notes(db, defect_id, request.form)
            return redirect(
                url_for("fleet.defect_detail", boat_index=boat_index, defect_id=defect_id)
            )
        return render_template(
            "defect_detail.html",
            **services.defect_detail_context(db, defect, "admin", boat_index),
        )

    @blueprint.route(
        "/fleet/<int:boat_index>/defects/<int:defect_id>/plan", methods=["POST"]
    )
    @admin_login_required
    def add_defect_plan_item(boat_index, defect_id):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))
        db = get_db()
        if repository.get_defect(db, defect_id, boat) is not None:
            services.add_defect_plan_item(db, defect_id, request.form)
        return redirect(
            url_for("fleet.defect_detail", boat_index=boat_index, defect_id=defect_id)
        )

    @blueprint.route(
        "/fleet/<int:boat_index>/defects/<int:defect_id>/plan/<int:item_id>/status",
        methods=["POST"],
    )
    @admin_login_required
    def set_defect_plan_item_status(boat_index, defect_id, item_id):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))
        db = get_db()
        if repository.get_defect(db, defect_id, boat) is not None:
            services.set_defect_plan_item_status(
                db, defect_id, item_id, request.form.get("status", "")
            )
        return redirect(
            url_for("fleet.defect_detail", boat_index=boat_index, defect_id=defect_id)
        )

    @blueprint.route("/fleet/<int:boat_index>/documents", methods=["POST"])
    @admin_login_required
    def upload_document(boat_index):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))

        title = request.form.get("title", "").strip()
        uploaded_file = request.files.get("document")
        if title and uploaded_file and uploaded_file.filename:
            extension = os.path.splitext(uploaded_file.filename)[1].lower()
            if extension in BOAT_DOCUMENT_EXTENSIONS:
                documents_dir = os.path.join(current_app.static_folder, "boat_documents")
                os.makedirs(documents_dir, exist_ok=True)
                filename = f"{secrets.token_hex(8)}{extension}"
                uploaded_file.save(os.path.join(documents_dir, filename))
                repository.add_document(
                    get_db(),
                    boat,
                    title,
                    filename,
                    uploaded_file.filename,
                    services.current_timestamp(),
                )
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

    @blueprint.route("/fleet/<int:boat_index>/documents/<int:document_id>")
    @admin_login_required
    def download_document(boat_index, document_id):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))
        document = repository.get_document(get_db(), boat, document_id)
        if document is None:
            return redirect(url_for("fleet.boat_detail", boat_index=boat_index))
        documents_dir = os.path.join(current_app.static_folder, "boat_documents")
        return send_from_directory(
            documents_dir,
            document["filename"],
            download_name=document["original_filename"],
        )

    @blueprint.route(
        "/fleet/<int:boat_index>/documents/<int:document_id>/delete", methods=["POST"]
    )
    @admin_login_required
    def delete_document(boat_index, document_id):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))
        db = get_db()
        document = repository.get_document(db, boat, document_id)
        if document is not None:
            try:
                os.remove(
                    os.path.join(
                        current_app.static_folder, "boat_documents", document["filename"]
                    )
                )
            except OSError:
                pass
            repository.delete_document(db, document_id)
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

    @blueprint.route(
        "/fleet/<int:boat_index>/defects/<int:defect_id>/status", methods=["POST"]
    )
    @admin_login_required
    def set_defect_status(boat_index, defect_id):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))
        services.change_defect_status(
            get_db(), boat, defect_id, request.form.get("status", "").strip()
        )
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

    @blueprint.route(
        "/fleet/<int:boat_index>/defects/<int:defect_id>/assign", methods=["POST"]
    )
    @admin_login_required
    def assign_defect(boat_index, defect_id):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))
        db = get_db()
        if repository.get_defect(db, defect_id, boat) is not None:
            services.create_defect_assignment(
                db,
                defect_id,
                request.form.get("employee_name", "").strip(),
                request.form.get("rate", ""),
                request.form.get("norm_hours", ""),
            )
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

    return blueprint
