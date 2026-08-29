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
    session,
    url_for,
)

from . import fuel_services, repository, services
from .constants import (
    BOAT_DOCUMENT_EXTENSIONS,
    BOATS,
    CHECKLIST_TYPE_LABELS,
    DEFECT_STATUSES,
)


def create_fleet_blueprint(
    get_db, admin_login_required, task_assigned_notifier=None
):
    """Build the fleet Blueprint with the application's DB and auth adapters."""
    blueprint = Blueprint("fleet", __name__)

    @blueprint.route("/fleet")
    @admin_login_required
    def index():
        db = get_db()
        return render_template(
            "fleet_index.html",
            boats=BOATS,
            fuel_by_boat={
                boat["name"]: fuel_services.fuel_summary(db, boat["name"], 0)
                for boat in BOATS
            },
            active_page="fleet",
        )

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
            fuel=fuel_services.fuel_summary(db, boat),
            fuel_notice=session.pop("fuel_notice", None),
            defect_notice=session.pop("defect_notice", None),
            defects_open=request.args.get("defects") == "open",
            viewer_role="admin",
            active_page="fleet",
        )

    @blueprint.route("/fleet/<int:boat_index>/defects", methods=["POST"])
    @admin_login_required
    def create_defect(boat_index):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))

        success, message, _ = services.create_manual_defect(
            get_db(),
            boat,
            request.form.get("description", ""),
            session.get("admin_name") or "Администратор",
        )
        session["defect_notice"] = {
            "type": "success" if success else "error",
            "message": message,
        }
        return redirect(
            url_for("fleet.boat_detail", boat_index=boat_index, defects="open")
            + "#current-defects"
        )

    @blueprint.route("/fleet/<int:boat_index>/fuel/refill", methods=["POST"])
    @admin_login_required
    def add_fuel_refill(boat_index):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))
        success, message = fuel_services.record_refill(
            get_db(),
            boat,
            request.form.get("liters", ""),
            request.form.get("occurred_at", ""),
            request.form.get("fill_to_full") == "1",
            "admin",
            session.get("admin_name") or "Администратор",
            request.form.get("fuel_operation", "tank"),
        )
        session["fuel_notice"] = {
            "type": "success" if success else "error",
            "message": message,
        }
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

    @blueprint.route(
        "/fleet/<int:boat_index>/fuel/trips/<int:event_id>/consumption",
        methods=["POST"],
    )
    @admin_login_required
    def set_manual_fuel_consumption(boat_index, event_id):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))
        success, message = fuel_services.record_individual_consumption(
            get_db(),
            boat,
            event_id,
            request.form.get("liters", ""),
            "admin",
            session.get("admin_name") or "Администратор",
        )
        session["fuel_notice"] = {
            "type": "success" if success else "error",
            "message": message,
        }
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

    @blueprint.route(
        "/fleet/<int:boat_index>/fuel/transactions/<int:transaction_id>/delete",
        methods=["POST"],
    )
    @admin_login_required
    def delete_fuel_transaction(boat_index, transaction_id):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))
        success, message = fuel_services.delete_transaction(
            get_db(),
            boat,
            transaction_id,
            session.get("admin_name") or "Администратор",
        )
        session["fuel_notice"] = {
            "type": "success" if success else "error",
            "message": message,
        }
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

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
        "/fleet/<int:boat_index>/defects/<int:defect_id>/delete", methods=["POST"]
    )
    @admin_login_required
    def delete_defect(boat_index, defect_id):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))
        services.delete_defect(get_db(), boat, defect_id)
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
            assignment_id = services.create_defect_assignment(
                db,
                defect_id,
                request.form.get("employee_name", "").strip(),
                request.form.get("rate", ""),
                request.form.get("norm_hours", ""),
            )
            if assignment_id is not None and task_assigned_notifier is not None:
                task_assigned_notifier(db, "defect", assignment_id)
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

    return blueprint
