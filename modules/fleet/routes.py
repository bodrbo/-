"""HTTP routes for the fleet module."""

import datetime as dt
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
    BOAT_PHOTO_EXTENSIONS,
    BOAT_PHOTO_MAX_BYTES,
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
            boats=services.fleet_boat_cards(db, fuel_services.fuel_summary),
            fleet_notice=session.pop("fleet_notice", None),
            default_vessel_color=services.DEFAULT_VESSEL_COLOR,
            active_page="fleet",
        )

    @blueprint.route("/fleet/vessels", methods=["POST"])
    @admin_login_required
    def create_vessel():
        success, message, boat_index = services.create_vessel(get_db(), request.form)
        session["fleet_notice"] = {
            "type": "success" if success else "error",
            "message": message,
        }
        if success and boat_index is not None:
            return redirect(url_for("fleet.boat_detail", boat_index=boat_index))
        return redirect(url_for("fleet.index") + "#fleet-create-vessel")

    @blueprint.route("/fleet/<int:boat_index>")
    @admin_login_required
    def boat_detail(boat_index):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))

        db = get_db()
        vessel = services.vessel_for_boat(db, boat)
        profile = repository.get_boat_profile(db, boat)
        current_defects = services.current_defects_for_boat(db, boat)

        def _valid_iso_date(raw):
            raw = (raw or "").strip()
            if not raw:
                return None
            try:
                dt.datetime.strptime(raw, "%Y-%m-%d")
            except ValueError:
                return None
            return raw

        def _valid_page(raw):
            try:
                return max(1, int(raw))
            except (TypeError, ValueError):
                return 1

        checklist_from = _valid_iso_date(request.args.get("checklist_from"))
        checklist_to = _valid_iso_date(request.args.get("checklist_to"))
        checklist_page = _valid_page(request.args.get("checklist_page", "1"))
        checklists = services.fleet_boat_checklists(
            db, boat,
            date_from=checklist_from, date_to=checklist_to, page=checklist_page,
        )

        archive_from = _valid_iso_date(request.args.get("archive_from"))
        archive_to = _valid_iso_date(request.args.get("archive_to"))
        archive_page = _valid_page(request.args.get("archive_page", "1"))
        archived_defects = services.archived_defects_for_boat(
            db, boat,
            date_from=archive_from, date_to=archive_to, page=archive_page,
        )

        return render_template(
            "fleet_boat.html",
            boat=boat,
            boat_index=boat_index,
            vessel=vessel,
            boats=BOATS,
            boat_photo_url=services.boat_photo_url(profile),
            boat_photo_notice=session.pop("boat_photo_notice", None),
            fleet_notice=session.pop("fleet_notice", None),
            checklists=checklists["items"],
            checklists_total=checklists["total"],
            checklists_page=checklists["page"],
            checklists_total_pages=checklists["total_pages"],
            checklist_pagination_items=services.fleet_pagination_items(
                checklists["page"], checklists["total_pages"]
            ),
            checklist_filter_from=checklist_from or "",
            checklist_filter_to=checklist_to or "",
            checklists_open=bool(
                checklist_from or checklist_to or request.args.get("checklist_page")
            ),
            documents=repository.list_documents(db, boat),
            current_defects=current_defects,
            archived_defects=archived_defects["items"],
            archived_defects_total=archived_defects["total"],
            archived_defects_page=archived_defects["page"],
            archived_defects_total_pages=archived_defects["total_pages"],
            archive_pagination_items=services.fleet_pagination_items(
                archived_defects["page"], archived_defects["total_pages"]
            ),
            archive_filter_from=archive_from or "",
            archive_filter_to=archive_to or "",
            archive_open=bool(
                archive_from or archive_to or request.args.get("archive_page")
            ),
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

    @blueprint.route("/fleet/vessels/<int:vessel_id>/update", methods=["POST"])
    @admin_login_required
    def update_vessel(vessel_id):
        success, message, boat_index = services.update_vessel(
            get_db(), vessel_id, request.form
        )
        session["fleet_notice"] = {
            "type": "success" if success else "error",
            "message": message,
        }
        if boat_index is not None:
            return redirect(
                url_for("fleet.boat_detail", boat_index=boat_index)
                + "#fleet-vessel-settings"
            )
        return redirect(url_for("fleet.index"))

    @blueprint.route("/fleet/vessels/<int:vessel_id>/delete", methods=["POST"])
    @admin_login_required
    def delete_vessel(vessel_id):
        success, message = services.archive_vessel(get_db(), vessel_id)
        session["fleet_notice"] = {
            "type": "success" if success else "error",
            "message": message,
        }
        return redirect(url_for("fleet.index"))

    @blueprint.route("/fleet/<int:boat_index>/photo", methods=["POST"])
    @admin_login_required
    def upload_boat_photo(boat_index):
        boat = services.boat_by_index(boat_index)
        if boat is None:
            return redirect(url_for("fleet.index"))

        if request.content_length and request.content_length > BOAT_PHOTO_MAX_BYTES:
            session["boat_photo_notice"] = {
                "type": "error",
                "message": "Фотография должна быть меньше 8 МБ.",
            }
            return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

        photo = request.files.get("photo")
        if photo is None or not photo.filename:
            session["boat_photo_notice"] = {
                "type": "error",
                "message": "Выберите фотографию катера.",
            }
            return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

        extension = os.path.splitext(photo.filename)[1].lower()
        if extension not in BOAT_PHOTO_EXTENSIONS:
            session["boat_photo_notice"] = {
                "type": "error",
                "message": "Фотография должна быть в формате JPG, PNG или WebP.",
            }
            return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

        db = get_db()
        previous_profile = repository.get_boat_profile(db, boat)
        previous_filename = (
            previous_profile["photo_filename"] if previous_profile else None
        )
        photos_dir = os.path.join(current_app.static_folder, "fleet_boats")
        os.makedirs(photos_dir, exist_ok=True)
        filename = f"{boat_index}-{secrets.token_hex(8)}{extension}"
        photo.save(os.path.join(photos_dir, filename))
        repository.save_boat_photo(
            db, boat, filename, services.current_timestamp()
        )
        if previous_filename and previous_filename != filename:
            try:
                os.remove(os.path.join(photos_dir, previous_filename))
            except OSError:
                pass

        session["boat_photo_notice"] = {
            "type": "success",
            "message": "Фотография катера обновлена.",
        }
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

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
        operation = request.form.get("fuel_operation", "tank")
        occurred_at = fuel_services.resolve_operation_timestamp(
            request.form.get("occurred_at", ""),
            request.form.get("occurred_at_auto") == "1",
        )
        if operation == "reserve_to_boat":
            success, message = fuel_services.transfer_reserve_between_boats(
                get_db(),
                boat,
                request.form.get("destination_boat", ""),
                request.form.get("liters", ""),
                occurred_at,
                "admin",
                session.get("admin_name") or "Администратор",
            )
        else:
            success, message = fuel_services.record_refill(
                get_db(),
                boat,
                request.form.get("liters", ""),
                occurred_at,
                request.form.get("fill_to_full") == "1",
                "admin",
                session.get("admin_name") or "Администратор",
                operation,
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
            defect_notice=session.pop("defect_notice", None),
            **services.defect_detail_context(db, defect, "admin", boat_index),
        )

    @blueprint.route(
        "/fleet/<int:boat_index>/defects/<int:defect_id>/transfer",
        methods=["POST"],
    )
    @admin_login_required
    def transfer_defect(boat_index, defect_id):
        source_boat = services.boat_by_index(boat_index)
        if source_boat is None:
            return redirect(url_for("fleet.index"))
        try:
            destination_index = int(request.form.get("destination_boat_index", ""))
        except (TypeError, ValueError):
            destination_index = -1
        destination_boat = services.boat_by_index(destination_index)
        success, message = services.transfer_defect(
            get_db(),
            defect_id,
            source_boat,
            destination_boat,
            session.get("admin_name") or "Администратор",
        )
        session["defect_notice"] = {
            "type": "success" if success else "error",
            "message": message,
        }
        target_index = destination_index if success else boat_index
        return redirect(
            url_for(
                "fleet.defect_detail",
                boat_index=target_index,
                defect_id=defect_id,
            )
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
                request.form.get("comment", ""),
            )
            if assignment_id is not None and task_assigned_notifier is not None:
                task_assigned_notifier(db, "defect", assignment_id)
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

    return blueprint
