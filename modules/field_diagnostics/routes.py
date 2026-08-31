"""Admin web workflow for field boat diagnostics."""

import datetime as dt
import os

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from .constants import ANSWER_STATUSES, FIELD_DIAGNOSTIC_QUESTIONS, INSPECTION_TYPES
from .pdf import build_diagnostic_pdf


MODEL_LIMIT = 180
OWNER_NAME_LIMIT = 160
PHONE_LIMIT = 50
COMMENT_LIMIT = 4000


def create_blueprint(
    get_db,
    admin_login_required,
    normalize_boat_model,
    boat_model_choices,
):
    blueprint = Blueprint("field_diagnostics", __name__)

    def get_sheet(db, sheet_id):
        return db.execute(
            "SELECT * FROM field_diagnostic_sheets WHERE id = ?", (sheet_id,)
        ).fetchone()

    def render_index(errors=None, form_values=None, status_code=200):
        db = get_db()
        sheets = db.execute(
            "SELECT * FROM field_diagnostic_sheets ORDER BY id DESC"
        ).fetchall()
        response = render_template(
            "field_diagnostics.html",
            active_page="tuning",
            sub_page="diagnostics",
            diag_page="field",
            sheets=sheets,
            inspection_types=INSPECTION_TYPES,
            boat_model_choices=boat_model_choices(db),
            errors=errors or [],
            form_values=form_values or {},
        )
        return response, status_code

    @blueprint.get("/tuning/diagnostics/field")
    @admin_login_required
    def index():
        return render_index()[0]

    @blueprint.post("/tuning/diagnostics/field/add")
    @admin_login_required
    def add_sheet():
        values = {
            "boat_model": " ".join(request.form.get("boat_model", "").split()),
            "owner_name": " ".join(request.form.get("owner_name", "").split()),
            "owner_phone": request.form.get("owner_phone", "").strip(),
            "inspection_type": request.form.get("inspection_type", "").strip(),
        }
        errors = []
        if not values["boat_model"]:
            errors.append("Укажите модель лодки.")
        elif len(values["boat_model"]) > MODEL_LIMIT:
            errors.append("Название модели должно быть короче 180 символов.")
        if not values["owner_name"]:
            errors.append("Укажите имя судовладельца.")
        elif len(values["owner_name"]) > OWNER_NAME_LIMIT:
            errors.append("Имя судовладельца должно быть короче 160 символов.")
        if not values["owner_phone"]:
            errors.append("Укажите телефон судовладельца.")
        elif len(values["owner_phone"]) > PHONE_LIMIT:
            errors.append("Телефон должен быть короче 50 символов.")
        if values["inspection_type"] not in INSPECTION_TYPES:
            errors.append("Выберите тип осмотра.")
        if errors:
            return render_index(errors, values, 400)

        db = get_db()
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        model_key = normalize_boat_model(values["boat_model"])
        profile = db.execute(
            "SELECT id, model_name FROM tuning_boat_profiles WHERE model_key = ?",
            (model_key,),
        ).fetchone()
        if profile is None:
            cursor = db.execute(
                "INSERT INTO tuning_boat_profiles "
                "(model_key, model_name, specifications, created_at, updated_at) "
                "VALUES (?, ?, '', ?, ?)",
                (model_key, values["boat_model"], now, now),
            )
            profile_id = cursor.lastrowid
            canonical_model = values["boat_model"]
        else:
            profile_id = profile["id"]
            canonical_model = profile["model_name"]

        cursor = db.execute(
            "INSERT INTO field_diagnostic_sheets "
            "(boat_profile_id, boat_model, owner_name, owner_phone, inspection_type, "
            "status, created_by_name, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'in_progress', ?, ?)",
            (
                profile_id,
                canonical_model,
                values["owner_name"],
                values["owner_phone"],
                values["inspection_type"],
                session.get("admin_name", ""),
                now,
            ),
        )
        db.commit()
        return redirect(url_for("field_diagnostics.sheet", sheet_id=cursor.lastrowid))

    @blueprint.get("/tuning/diagnostics/field/<int:sheet_id>")
    @admin_login_required
    def sheet(sheet_id):
        db = get_db()
        diagnostic_sheet = get_sheet(db, sheet_id)
        if diagnostic_sheet is None:
            return redirect(url_for("field_diagnostics.index"))

        questions = FIELD_DIAGNOSTIC_QUESTIONS.get(
            diagnostic_sheet["inspection_type"], ()
        )
        answers = db.execute(
            "SELECT * FROM field_diagnostic_answers "
            "WHERE sheet_id = ? ORDER BY question_index",
            (sheet_id,),
        ).fetchall()
        current_index = len(answers)
        done = current_index >= len(questions) and bool(questions)
        if done and diagnostic_sheet["status"] != "completed":
            completed_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            db.execute(
                "UPDATE field_diagnostic_sheets "
                "SET status = 'completed', completed_at = ? WHERE id = ?",
                (completed_at, sheet_id),
            )
            db.commit()
            diagnostic_sheet = get_sheet(db, sheet_id)

        problems = [answer for answer in answers if answer["status"] == "problem"]
        ok_answers = [answer for answer in answers if answer["status"] == "ok"]
        return render_template(
            "field_diagnostic_sheet.html",
            active_page="tuning",
            sub_page="diagnostics",
            diag_page="field",
            sheet=diagnostic_sheet,
            inspection_label=INSPECTION_TYPES.get(
                diagnostic_sheet["inspection_type"], diagnostic_sheet["inspection_type"]
            ),
            done=done,
            questions=questions,
            question=questions[current_index] if not done and questions else None,
            question_index=current_index,
            total=len(questions),
            answered_count=len(answers),
            problems=problems,
            ok_answers=ok_answers,
        )

    @blueprint.post("/tuning/diagnostics/field/<int:sheet_id>/answer")
    @admin_login_required
    def answer(sheet_id):
        db = get_db()
        diagnostic_sheet = get_sheet(db, sheet_id)
        if diagnostic_sheet is None:
            return redirect(url_for("field_diagnostics.index"))
        if diagnostic_sheet["status"] == "completed":
            return redirect(url_for("field_diagnostics.sheet", sheet_id=sheet_id))

        questions = FIELD_DIAGNOSTIC_QUESTIONS.get(
            diagnostic_sheet["inspection_type"], ()
        )
        raw_index = request.form.get("question_index", "").strip()
        status = request.form.get("status", "").strip()
        comment = request.form.get("comment", "").strip()
        answered_count = db.execute(
            "SELECT COUNT(*) FROM field_diagnostic_answers WHERE sheet_id = ?",
            (sheet_id,),
        ).fetchone()[0]

        if not raw_index.isdigit() or int(raw_index) != answered_count:
            return redirect(url_for("field_diagnostics.sheet", sheet_id=sheet_id))
        question_index = int(raw_index)
        if question_index >= len(questions) or status not in ANSWER_STATUSES:
            return redirect(url_for("field_diagnostics.sheet", sheet_id=sheet_id))
        if status == "problem" and not comment:
            return render_template(
                "field_diagnostic_sheet.html",
                active_page="tuning",
                sub_page="diagnostics",
                diag_page="field",
                sheet=diagnostic_sheet,
                inspection_label=INSPECTION_TYPES[diagnostic_sheet["inspection_type"]],
                done=False,
                questions=questions,
                question=questions[question_index],
                question_index=question_index,
                total=len(questions),
                answered_count=answered_count,
                problems=[],
                ok_answers=[],
                answer_error="Опишите обнаруженную неисправность.",
            ), 400
        if len(comment) > COMMENT_LIMIT:
            return render_template(
                "field_diagnostic_sheet.html",
                active_page="tuning",
                sub_page="diagnostics",
                diag_page="field",
                sheet=diagnostic_sheet,
                inspection_label=INSPECTION_TYPES[diagnostic_sheet["inspection_type"]],
                done=False,
                questions=questions,
                question=questions[question_index],
                question_index=question_index,
                total=len(questions),
                answered_count=answered_count,
                problems=[],
                ok_answers=[],
                answer_error="Описание должно быть короче 4000 символов.",
            ), 400

        question = questions[question_index]
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT OR IGNORE INTO field_diagnostic_answers "
            "(sheet_id, question_index, section_name, question_title, question_text, "
            "status, comment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sheet_id,
                question_index,
                question["section"],
                question["title"],
                question["text"],
                status,
                comment or None,
                now,
            ),
        )
        db.commit()
        return redirect(url_for("field_diagnostics.sheet", sheet_id=sheet_id))

    @blueprint.get("/tuning/diagnostics/field/<int:sheet_id>/diagnostic-sheet.pdf")
    @admin_login_required
    def pdf(sheet_id):
        db = get_db()
        diagnostic_sheet = get_sheet(db, sheet_id)
        if diagnostic_sheet is None:
            return redirect(url_for("field_diagnostics.index"))
        if diagnostic_sheet["status"] != "completed":
            return redirect(url_for("field_diagnostics.sheet", sheet_id=sheet_id))
        answers = db.execute(
            "SELECT * FROM field_diagnostic_answers "
            "WHERE sheet_id = ? ORDER BY question_index",
            (sheet_id,),
        ).fetchall()
        try:
            pdf_bytes = build_diagnostic_pdf(
                diagnostic_sheet,
                answers,
                INSPECTION_TYPES.get(
                    diagnostic_sheet["inspection_type"],
                    diagnostic_sheet["inspection_type"],
                ),
                os.path.join(current_app.static_folder, "fonts"),
            )
        except (ImportError, OSError):
            return (
                "Формирование PDF временно недоступно. Проверьте установку "
                "ReportLab и файлов шрифтов, затем перезапустите приложение.",
                503,
            )
        response = current_app.response_class(pdf_bytes, mimetype="application/pdf")
        response.headers["Content-Disposition"] = (
            'inline; filename="Diagnostic-sheet-%d.pdf"' % sheet_id
        )
        return response

    return blueprint

