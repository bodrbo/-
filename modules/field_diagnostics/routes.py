"""Admin web workflow for field boat diagnostics."""

import datetime as dt
import json
import os
import secrets

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

    def client_choices(db):
        return db.execute(
            "SELECT id, client_name, phone FROM clients "
            "ORDER BY client_name COLLATE NOCASE, phone, id"
        ).fetchall()

    def normalize_phone_identity(phone):
        digits = "".join(character for character in phone if character.isdigit())
        if len(digits) == 11 and digits[0] in ("7", "8"):
            return "7" + digits[1:]
        if len(digits) == 10:
            return "7" + digits
        return digits

    def resolve_owner(db, values):
        """Resolve a cabinet by selected id and phone, or create a new one.

        A display name is never enough to identify a client: duplicate names
        are valid, while the normalized phone number is the stable identity.
        """
        phone_identity = normalize_phone_identity(values["owner_phone"])
        if len(phone_identity) < 7:
            return None, "Укажите корректный номер телефона судовладельца."

        raw_client_id = values["owner_client_id"]
        if raw_client_id:
            if not raw_client_id.isdigit():
                return None, "Выберите судовладельца из списка ещё раз."
            client = db.execute(
                "SELECT id, client_name, boat_model, phone FROM clients WHERE id = ?",
                (int(raw_client_id),),
            ).fetchone()
            if client is None:
                return None, "Выбранный судовладелец больше не существует."
            if normalize_phone_identity(client["phone"]) != phone_identity:
                return (
                    None,
                    "Номер телефона не совпадает с выбранным клиентом. "
                    "Выберите клиента заново или введите нового.",
                )
            values["owner_name"] = client["client_name"]
            values["owner_phone"] = client["phone"]
            values["owner_client_id"] = str(client["id"])
            return client["id"], None

        matches = [
            client
            for client in db.execute(
                "SELECT id, client_name, boat_model, phone FROM clients ORDER BY id"
            ).fetchall()
            if normalize_phone_identity(client["phone"]) == phone_identity
        ]
        if len(matches) > 1:
            return (
                None,
                "В базе найдено несколько клиентов с этим номером. "
                "Выберите нужного судовладельца из списка.",
            )
        if matches:
            client = matches[0]
            values["owner_name"] = client["client_name"]
            values["owner_phone"] = client["phone"]
            values["owner_client_id"] = str(client["id"])
            return client["id"], None

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cursor = db.execute(
            "INSERT INTO clients "
            "(client_name, boat_model, phone, token, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                values["owner_name"],
                values["boat_model"],
                values["owner_phone"],
                secrets.token_urlsafe(16),
                now,
            ),
        )
        values["owner_client_id"] = str(cursor.lastrowid)
        return cursor.lastrowid, None

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
            client_choices=client_choices(db),
            errors=errors or [],
            form_values=form_values or {},
        )
        return response, status_code

    def edit_context(db, diagnostic_sheet, errors=None, form_values=None,
                     extra_form_rows=None):
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
        answers_by_block = []
        for block_name in DIAGNOSTIC_BLOCKS:
            answers_by_block.append({
                "name": block_name,
                "answers": [
                    answer for answer in answers
                    if answer["section_name"] == block_name
                ],
            })
        values = form_values or {
            "boat_model": diagnostic_sheet["boat_model"],
            "owner_name": diagnostic_sheet["owner_name"],
            "owner_phone": diagnostic_sheet["owner_phone"],
            "inspection_type": diagnostic_sheet["inspection_type"],
            "owner_client_id": str(diagnostic_sheet["owner_client_id"] or ""),
        }
        if extra_form_rows is None:
            extra_form_rows = [
                {"id": row["id"], "description": row["description"]}
                for row in extra_defects
            ]
            extra_form_rows.append({"id": "", "description": ""})
        return {
            "active_page": "tuning",
            "sub_page": "diagnostics",
            "diag_page": "field",
            "sheet": diagnostic_sheet,
            "inspection_types": INSPECTION_TYPES,
            "boat_model_choices": boat_model_choices(db),
            "client_choices": client_choices(db),
            "answers": answers,
            "answers_by_block": answers_by_block,
            "extra_defects": extra_defects,
            "extra_form_rows": extra_form_rows,
            "errors": errors or [],
            "form_values": values,
        }

    def ensure_boat_profile(db, model_name, now):
        model_key = normalize_boat_model(model_name)
        profile = db.execute(
            "SELECT id, model_name FROM tuning_boat_profiles WHERE model_key = ?",
            (model_key,),
        ).fetchone()
        if profile is not None:
            return profile["id"], profile["model_name"]
        cursor = db.execute(
            "INSERT INTO tuning_boat_profiles "
            "(model_key, model_name, specifications, created_at, updated_at) "
            "VALUES (?, ?, '', ?, ?)",
            (model_key, model_name, now, now),
        )
        return cursor.lastrowid, model_name

    def validate_sheet_values(form):
        values = {
            "boat_model": " ".join(form.get("boat_model", "").split()),
            "owner_name": " ".join(form.get("owner_name", "").split()),
            "owner_phone": form.get("owner_phone", "").strip(),
            "inspection_type": form.get("inspection_type", "").strip(),
            "owner_client_id": form.get("owner_client_id", "").strip(),
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
        if values["owner_client_id"] and not values["owner_client_id"].isdigit():
            errors.append("Выберите судовладельца из списка ещё раз.")
        return values, errors

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
        values, errors = validate_sheet_values(request.form)
        if errors:
            return render_index(errors, values, 400)

        db = get_db()
        owner_client_id, owner_error = resolve_owner(db, values)
        if owner_error:
            return render_index([owner_error], values, 400)
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        profile_id, canonical_model = ensure_boat_profile(
            db, values["boat_model"], now
        )

        question_set_json = json.dumps(
            FIELD_DIAGNOSTIC_QUESTIONS[values["inspection_type"]],
            ensure_ascii=False,
        )
        cursor = db.execute(
            "INSERT INTO field_diagnostic_sheets "
            "(boat_profile_id, boat_model, owner_client_id, owner_name, owner_phone, "
            "inspection_type, status, created_by_name, started_at, question_set_json) "
            "VALUES (?, ?, ?, ?, ?, ?, 'in_progress', ?, ?, ?)",
            (
                profile_id,
                canonical_model,
                owner_client_id,
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

    @blueprint.route(
        "/tuning/diagnostics/field/<int:sheet_id>/edit",
        methods=["GET", "POST"],
    )
    @admin_login_required
    def edit_sheet(sheet_id):
        db = get_db()
        diagnostic_sheet = get_sheet(db, sheet_id)
        if diagnostic_sheet is None:
            return redirect(url_for("field_diagnostics.index"))
        if request.method == "GET":
            return render_template(
                "field_diagnostic_edit.html",
                **edit_context(db, diagnostic_sheet),
            )

        values, errors = validate_sheet_values(request.form)
        answers = db.execute(
            "SELECT * FROM field_diagnostic_answers "
            "WHERE sheet_id = ? ORDER BY question_index",
            (sheet_id,),
        ).fetchall()
        if (
            answers
            and values["inspection_type"] in INSPECTION_TYPES
            and values["inspection_type"] != diagnostic_sheet["inspection_type"]
        ):
            errors.append(
                "Тип осмотра нельзя изменить после первого ответа. "
                "Создайте новый лист, если был выбран неверный тип."
            )
            values["inspection_type"] = diagnostic_sheet["inspection_type"]

        answer_updates = []
        for answer in answers:
            status = request.form.get(
                "answer_status_%d" % answer["id"], answer["status"]
            ).strip()
            comment = request.form.get(
                "answer_comment_%d" % answer["id"], answer["comment"] or ""
            ).strip()
            if status not in ANSWER_STATUSES:
                errors.append(
                    "Выберите корректный результат для пункта «%s»."
                    % answer["question_title"]
                )
                continue
            if status == "problem" and not comment:
                errors.append(
                    "Опишите неисправность в пункте «%s»."
                    % answer["question_title"]
                )
            if len(comment) > COMMENT_LIMIT:
                errors.append(
                    "Описание в пункте «%s» должно быть короче 4000 символов."
                    % answer["question_title"]
                )
            answer_updates.append(
                (status, comment if status == "problem" else None, answer["id"])
            )

        extra_ids = request.form.getlist("extra_id[]")
        extra_descriptions = request.form.getlist("extra_description[]")
        extra_updates = []
        existing_extra_ids = {
            row["id"]
            for row in db.execute(
                "SELECT id FROM field_diagnostic_extra_defects WHERE sheet_id = ?",
                (sheet_id,),
            ).fetchall()
        }
        seen_extra_ids = set()
        if len(extra_ids) != len(extra_descriptions):
            errors.append("Не удалось сопоставить прочие неисправности. Обновите страницу.")
        else:
            for raw_id, raw_description in zip(extra_ids, extra_descriptions):
                description = raw_description.strip()
                if len(description) > OTHER_DEFECT_LIMIT:
                    errors.append(
                        "Описание каждой прочей неисправности должно быть короче 2000 символов."
                    )
                if raw_id:
                    if not raw_id.isdigit() or int(raw_id) not in existing_extra_ids:
                        errors.append("Одна из прочих неисправностей больше не существует.")
                        continue
                    extra_id = int(raw_id)
                    if extra_id in seen_extra_ids:
                        errors.append("Одна из прочих неисправностей отправлена дважды.")
                        continue
                    seen_extra_ids.add(extra_id)
                    extra_updates.append((extra_id, description))
                elif description:
                    extra_updates.append((None, description))

        resulting_extra_count = (
            len(existing_extra_ids - seen_extra_ids)
            + sum(1 for extra_id, description in extra_updates if extra_id and description)
            + sum(1 for extra_id, description in extra_updates if extra_id is None and description)
        )
        if resulting_extra_count > OTHER_DEFECT_COUNT_LIMIT:
            errors.append("В блоке «Прочее» можно сохранить не более 20 неисправностей.")

        owner_client_id = None
        if not errors:
            owner_client_id, owner_error = resolve_owner(db, values)
            if owner_error:
                errors.append(owner_error)

        if errors:
            extra_form_rows = [
                {"id": raw_id, "description": raw_description}
                for raw_id, raw_description in zip(extra_ids, extra_descriptions)
            ]
            if not extra_form_rows:
                extra_form_rows = [{"id": "", "description": ""}]
            return render_template(
                "field_diagnostic_edit.html",
                **edit_context(
                    db,
                    diagnostic_sheet,
                    errors=errors,
                    form_values=values,
                    extra_form_rows=extra_form_rows,
                ),
            ), 400

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        profile_id, canonical_model = ensure_boat_profile(
            db, values["boat_model"], now
        )
        question_set_json = diagnostic_sheet["question_set_json"]
        if values["inspection_type"] != diagnostic_sheet["inspection_type"]:
            question_set_json = json.dumps(
                FIELD_DIAGNOSTIC_QUESTIONS[values["inspection_type"]],
                ensure_ascii=False,
            )
        db.execute(
            "UPDATE field_diagnostic_sheets SET boat_profile_id = ?, boat_model = ?, "
            "owner_client_id = ?, owner_name = ?, owner_phone = ?, inspection_type = ?, "
            "question_set_json = ? WHERE id = ?",
            (
                profile_id,
                canonical_model,
                owner_client_id,
                values["owner_name"],
                values["owner_phone"],
                values["inspection_type"],
                question_set_json,
                sheet_id,
            ),
        )
        for status, comment, answer_id in answer_updates:
            db.execute(
                "UPDATE field_diagnostic_answers SET status = ?, comment = ? "
                "WHERE id = ? AND sheet_id = ?",
                (status, comment, answer_id, sheet_id),
            )
        for extra_id, description in extra_updates:
            if extra_id is None:
                db.execute(
                    "INSERT INTO field_diagnostic_extra_defects "
                    "(sheet_id, description, created_at) VALUES (?, ?, ?)",
                    (sheet_id, description, now),
                )
            elif description:
                db.execute(
                    "UPDATE field_diagnostic_extra_defects SET description = ? "
                    "WHERE id = ? AND sheet_id = ?",
                    (description, extra_id, sheet_id),
                )
            else:
                db.execute(
                    "DELETE FROM field_diagnostic_extra_defects "
                    "WHERE id = ? AND sheet_id = ?",
                    (extra_id, sheet_id),
                )
        db.commit()
        return redirect(url_for("field_diagnostics.sheet", sheet_id=sheet_id))

    @blueprint.post("/tuning/diagnostics/field/<int:sheet_id>/delete")
    @admin_login_required
    def delete_sheet(sheet_id):
        db = get_db()
        if get_sheet(db, sheet_id) is None:
            return redirect(url_for("field_diagnostics.index"))
        db.execute(
            "DELETE FROM field_diagnostic_extra_defects WHERE sheet_id = ?",
            (sheet_id,),
        )
        db.execute(
            "DELETE FROM field_diagnostic_answers WHERE sheet_id = ?",
            (sheet_id,),
        )
        db.execute("DELETE FROM field_diagnostic_sheets WHERE id = ?", (sheet_id,))
        db.commit()
        return redirect(url_for("field_diagnostics.index"))

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
