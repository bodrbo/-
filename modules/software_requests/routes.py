"""Staff submission widget and administrator request journal."""

import datetime as dt

from flask import Blueprint, jsonify, make_response, redirect, render_template, request, session, url_for

from . import repository
from .constants import DESCRIPTION_MAX_LENGTH, PAGE_PATH_MAX_LENGTH, REQUEST_STATUSES


def create_blueprint(get_db, admin_login_required, active_team_account):
    blueprint = Blueprint("software_requests", __name__)

    def current_staff():
        db = get_db()
        admin_id = session.get("admin_id")
        if admin_id:
            row = db.execute(
                "SELECT id, admin_name FROM admin_accounts WHERE id = ?",
                (admin_id,),
            ).fetchone()
            if row is not None:
                return {
                    "author_type": "admin",
                    "author_admin_id": row["id"],
                    "author_employee_id": None,
                    "author_name": row["admin_name"],
                }

        if session.get("team_id"):
            row = active_team_account(db)
            if row is not None:
                return {
                    "author_type": "employee",
                    "author_admin_id": None,
                    "author_employee_id": row["employee_id"],
                    "author_name": row["name"],
                }
        return None

    @blueprint.route("/software-requests/widget")
    def widget():
        if current_staff() is None:
            return "", 204
        response = make_response(render_template("_software_request_widget.html"))
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @blueprint.route("/software-requests", methods=["POST"])
    def submit():
        staff = current_staff()
        if staff is None:
            return jsonify({"ok": False, "error": "Требуется вход в систему."}), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Ожидались данные в формате JSON."}), 400

        description = str(payload.get("description") or "").strip()
        if not description:
            return jsonify({"ok": False, "error": "Опишите ошибку или желаемую доработку."}), 400
        if len(description) > DESCRIPTION_MAX_LENGTH:
            return jsonify({"ok": False, "error": f"Описание не должно превышать {DESCRIPTION_MAX_LENGTH} символов."}), 400

        page_path = str(payload.get("page_path") or "").strip()
        if (
            len(page_path) > PAGE_PATH_MAX_LENGTH
            or (page_path and (not page_path.startswith("/") or page_path.startswith("//")))
        ):
            page_path = ""

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        request_id = repository.create_request(
            get_db(),
            **staff,
            description=description,
            page_path=page_path,
            timestamp=now,
        )
        return jsonify({"ok": True, "request_id": request_id}), 201

    @blueprint.route("/settings/software-requests")
    @admin_login_required
    def index():
        selected_status = request.args.get("status", "")
        if selected_status not in REQUEST_STATUSES:
            selected_status = ""
        db = get_db()
        counts = repository.status_counts(db)
        return render_template(
            "settings_software_requests.html",
            requests=repository.list_requests(db, selected_status or None),
            statuses=REQUEST_STATUSES,
            status_counts={key: counts.get(key, 0) for key in REQUEST_STATUSES},
            total_count=sum(counts.values()),
            selected_status=selected_status,
            active_page="settings",
            manager_view=False,
        )

    @blueprint.route("/settings/software-requests/<int:request_id>/status", methods=["POST"])
    @admin_login_required
    def set_status(request_id):
        status = request.form.get("status", "")
        if status in REQUEST_STATUSES:
            repository.update_status(
                get_db(), request_id, status,
                dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            )
        return redirect(url_for("software_requests.index"))

    return blueprint
