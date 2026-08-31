"""Admin web workflow for field boat diagnostics."""

import datetime as dt
import json
import os

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from .constants import (
    ANSWER_STATUSES,
    DIAGNOSTIC_BLOCKS,
    FIELD_DIAGNOSTIC_QUESTIONS,
    INSPECTION_TYPES,
)
from .pdf import build_diagnostic_pdf


MODEL_LIMIT = 180
OWNER_NAME_LIMIT = 160
PHONE_LIMIT = 50
COMMENT_LIMIT = 4000
OTHER_DEFECT_LIMIT = 2000
OTHER_DEFECT_COUNT_LIMIT = 20


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

    def questions_for_sheet(db, diagnostic_sheet):
        """Use a frozen question set so edits never corrupt an active visit."""
        frozen = diagnostic_sheet["question_set_json"]
        if frozen:
            try:
                questions = json.loads(frozen)
            except (TypeError, ValueError):
                questions = None
            if isinstance(questions, list) and questions:
                return tuple(questions)

        current_questions = list(
            FIELD_DIAGNOSTIC_QUESTIONS.get(
                diagnostic_sheet["inspection_type"], ()
            )
        )
        existing_answers = db.execute(
            "SELECT * FROM field_diagnostic_answers "
            "WHERE sheet_id = ? ORDER BY question_index",
            (diagnostic_sheet["id"],),
        ).fetchall()
        if existing_answers:
            # Migration path for a sheet started before question snapshots:
            # retain every answered item as the prefix, then append only new
            # unanswered questions from the current structure.
            answered_keys = {
                (answer["question_title"], answer["question_text"])
                for answer in existing_answers
            }
            current_sections = {
                (question["title"], question["text"]): question["section"]
                for question in current_questions
            }
            questions = [
                {
                    "section": current_sections.get(
                        (answer["question_title"], answer["question_text"]),
                        "Прочее",
                    ),
                    "title": answer["question_title"],
                    "text": answer["question_text"],
                }
                for answer in existing_answers
            ]
            for answer, question in zip(existing_answers, questions):
                if answer["section_name"] != question["section"]:
                    db.execute(
                        "UPDATE field_diagnostic_answers SET section_name = ? "
                        "WHERE id = ?",
                        (question["section"], answer["id"]),
                    )
            questions.extend(
                question for question in current_questions
                if (question["title"], question["text"]) not in answered_keys
            )
        else:
            questions = current_questions
        if questions:
            db.execute(
                "UPDATE field_diagnostic_sheets SET question_set_json = ? WHERE id = ?",
                (json.dumps(questions, ensure_ascii=False), diagnostic_sheet["id"]),
            )
            db.commit()
        return tuple(questions)

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

    def sheet_context(db, diagnostic_sheet, answer_error=None,
                      other_error=None, other_values=None):
        questions = questions_for_sheet(db, diagnostic_sheet)
        answers = db.execute(
            "SELECT * FROM field_diagnostic_answers "
            "WHERE sheet_id = ? ORDER BY question_index",
            (diagnostic_sheet["id"],),
        ).fetchall()
        extra_defects = db.execute(
            "SELECT * FROM field_diagnostic_extra_defects "
            "WHERE sheet_id = ? ORDER BY id",
            (diagnostic_sheet["id"],),
        ).fetchall()
        current_index = len(answers)
        fixed_done = current_index >= len(questions) and bool(questions)
        done = diagnostic_sheet["status"] == "completed"
        other_step = fixed_done and not done
        current_question = (
            questions[current_index]
            if not fixed_done and current_index < len(questions)
            else None
        )

        block_results = []
        block_states = []
        for block_name in DIAGNOSTIC_BLOCKS:
            block_answers = [
                answer for answer in answers
                if answer["section_name"] == block_name
            ]
            block_questions = [
                question for question in questions
                if question["section"] == block_name
            ]
            if block_name == "Прочее":
                state = (
                    "done" if done
                    else "current" if other_step
                    else "pending"
                )
            else:
                state = (
                    "current"
                    if current_question and current_question["section"] == block_name
                    else "done"
                    if block_questions and len(block_answers) >= len(block_questions)
                    else "pending"
                )
            block_states.append({"name": block_name, "state": state})
            block_results.append({
                "name": block_name,
                "answers": block_answers,
                "extra_defects": extra_defects if block_name == "Прочее" else (),
            })

        problems = [answer for answer in answers if answer["status"] == "problem"]
        ok_answers = [answer for answer in answers if answer["status"] == "ok"]
        return {
            "active_page": "tuning",
            "sub_page": "diagnostics",
            "diag_page": "field",
            "sheet": diagnostic_sheet,
            "inspection_label": INSPECTION_TYPES.get(
                diagnostic_sheet["inspection_type"],
                diagnostic_sheet["inspection_type"],
            ),
            "done": done,
            "other_step": other_step,
            "questions": questions,
            "question": current_question,
            "question_index": current_index,
            "total": len(questions),
            "answered_count": len(answers),
            "problems": problems,
            "ok_answers": ok_answers,
            "extra_defects": extra_defects,
            "block_states": block_states,
            "block_results": block_results,
            "answer_error": answer_error,
            "other_error": other_error,
            "other_values": other_values or [""],
        }

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

        question_set_json = json.dumps(
            FIELD_DIAGNOSTIC_QUESTIONS[values["inspection_type"]],
            ensure_ascii=False,
        )
        cursor = db.execute(
            "INSERT INTO field_diagnostic_sheets "
            "(boat_profile_id, boat_model, owner_name, owner_phone, inspection_type, "
            "status, created_by_name, started_at, question_set_json) "
            "VALUES (?, ?, ?, ?, ?, 'in_progress', ?, ?, ?)",
            (
                profile_id,
                canonical_model,
                values["owner_name"],
                values["owner_phone"],
                values["inspection_type"],
                session.get("admin_name", ""),
                now,
                question_set_json,
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

        return render_template(
            "field_diagnostic_sheet.html",
            **sheet_context(db, diagnostic_sheet),
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

        questions = questions_for_sheet(db, diagnostic_sheet)
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
                **sheet_context(
                    db,
                    diagnostic_sheet,
                    answer_error="Опишите обнаруженную неисправность.",
                ),
            ), 400
        if len(comment) > COMMENT_LIMIT:
            return render_template(
                "field_diagnostic_sheet.html",
                **sheet_context(
                    db,
                    diagnostic_sheet,
                    answer_error="Описание должно быть короче 4000 символов.",
                ),
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

    @blueprint.post("/tuning/diagnostics/field/<int:sheet_id>/other")
    @admin_login_required
    def save_other(sheet_id):
        db = get_db()
        diagnostic_sheet = get_sheet(db, sheet_id)
        if diagnostic_sheet is None:
            return redirect(url_for("field_diagnostics.index"))
        if diagnostic_sheet["status"] == "completed":
            return redirect(url_for("field_diagnostics.sheet", sheet_id=sheet_id))

        questions = questions_for_sheet(db, diagnostic_sheet)
        answered_count = db.execute(
            "SELECT COUNT(*) FROM field_diagnostic_answers WHERE sheet_id = ?",
            (sheet_id,),
        ).fetchone()[0]
        if answered_count < len(questions):
            return redirect(url_for("field_diagnostics.sheet", sheet_id=sheet_id))

        raw_values = request.form.getlist("other_defect[]")
        values = [value.strip() for value in raw_values if value.strip()]
        error = None
        if len(values) > OTHER_DEFECT_COUNT_LIMIT:
            error = "За один осмотр можно добавить не более 20 прочих неисправностей."
        elif any(len(value) > OTHER_DEFECT_LIMIT for value in values):
            error = "Описание каждой неисправности должно быть короче 2000 символов."
        if error:
            return render_template(
                "field_diagnostic_sheet.html",
                **sheet_context(
                    db,
                    diagnostic_sheet,
                    other_error=error,
                    other_values=raw_values or [""],
                ),
            ), 400

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        for description in values:
            db.execute(
                "INSERT INTO field_diagnostic_extra_defects "
                "(sheet_id, description, created_at) VALUES (?, ?, ?)",
                (sheet_id, description, now),
            )
        db.execute(
            "UPDATE field_diagnostic_sheets SET status = 'completed', "
            "other_completed_at = ?, completed_at = ? WHERE id = ?",
            (now, now, sheet_id),
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
        extra_defects = db.execute(
            "SELECT * FROM field_diagnostic_extra_defects "
            "WHERE sheet_id = ? ORDER BY id",
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
                extra_defects=extra_defects,
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
