"""Validation and atomic replay of operations created without a connection."""

import datetime as dt
import os
import re
import sqlite3

from modules.fleet.constants import BOATS, CHECKLIST_TYPE_LABELS

from . import repository


MAX_DESCRIPTION_LENGTH = 1000
MAX_QUESTION_LENGTH = 2000
MAX_COMMENT_LENGTH = 3000
MAX_CHECKLIST_ANSWERS = 100
MAX_EXTRA_DEFECTS = 30
OPERATION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9-]{16,80}$")
ATTACHMENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9-]{16,80}$")


class OfflineValidationError(ValueError):
    """Client payload is malformed and should not be retried unchanged."""


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _clean_timestamp(value, fallback):
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M")


def _validate_operation(operation):
    if not isinstance(operation, dict):
        raise OfflineValidationError("Некорректный формат операции.")
    operation_id = str(operation.get("id") or "").strip()
    operation_type = str(operation.get("type") or "").strip()
    if not OPERATION_ID_PATTERN.fullmatch(operation_id):
        raise OfflineValidationError("Некорректный идентификатор операции.")
    if operation_type not in {"defect", "checklist"}:
        raise OfflineValidationError("Неизвестный тип офлайн-операции.")
    payload = operation.get("payload")
    if not isinstance(payload, dict):
        raise OfflineValidationError("В операции отсутствуют данные.")
    return operation_id, operation_type, payload


def _validate_boat(payload):
    boat = str(payload.get("boat") or "").strip()
    if boat not in {item["name"] for item in BOATS}:
        raise OfflineValidationError("Не удалось определить судно.")
    return boat


def _description(value, empty_message="Опишите неисправность."):
    result = str(value or "").strip()
    if not result:
        raise OfflineValidationError(empty_message)
    if len(result) > MAX_DESCRIPTION_LENGTH:
        raise OfflineValidationError(
            f"Описание должно быть не длиннее {MAX_DESCRIPTION_LENGTH} символов."
        )
    return result


def _attachment_references(answers):
    references = set()
    for answer in answers:
        photo_ids = answer.get("photo_ids") or []
        if not isinstance(photo_ids, list):
            raise OfflineValidationError("Некорректный список фотографий.")
        for attachment_id in photo_ids:
            attachment_id = str(attachment_id or "").strip()
            if not ATTACHMENT_ID_PATTERN.fullmatch(attachment_id):
                raise OfflineValidationError("Некорректный идентификатор фотографии.")
            references.add(attachment_id)
    return references


def _save_attachments(files, required_ids, photos_dir, operation_id, allowed_extensions):
    uploads = {}
    for uploaded in files:
        original = os.path.basename(uploaded.filename or "")
        attachment_id, extension = os.path.splitext(original)
        extension = extension.lower()
        if attachment_id not in required_ids or attachment_id in uploads:
            continue
        if extension not in allowed_extensions or not (uploaded.mimetype or "").startswith("image/"):
            raise OfflineValidationError("Одна из фотографий имеет неподдерживаемый формат.")
        os.makedirs(photos_dir, exist_ok=True)
        filename = f"offline-{operation_id}-{attachment_id}{extension}"
        path = os.path.join(photos_dir, filename)
        uploaded.save(path)
        uploads[attachment_id] = {"filename": filename, "path": path}

    missing = required_ids - set(uploads)
    if missing:
        for item in uploads.values():
            try:
                os.remove(item["path"])
            except OSError:
                pass
        raise OfflineValidationError("Не удалось получить все фотографии чек-листа.")
    return uploads


def _sync_defect(db, payload, employee_name, timestamp):
    boat = _validate_boat(payload)
    description = _description(payload.get("description"))
    reported_at = _clean_timestamp(payload.get("reported_at"), timestamp)
    cursor = db.execute(
        "INSERT INTO boat_defects "
        "(boat, checklist_id, answer_id, description, employee_name, status, "
        "reported_at, updated_at) VALUES (?, NULL, NULL, ?, ?, 'new', ?, ?)",
        (boat, description, employee_name, reported_at, timestamp),
    )
    return cursor.lastrowid, [
        {
            "kind": "manual_defect",
            "boat": boat,
            "description": description,
            "employee_name": employee_name,
            "record_id": cursor.lastrowid,
            "photo_paths": [],
        }
    ]


def _validated_answers(payload):
    checklist_type = str(payload.get("checklist_type") or "").strip()
    if checklist_type not in CHECKLIST_TYPE_LABELS:
        raise OfflineValidationError("Неизвестный тип чек-листа.")
    answers = payload.get("answers")
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        raise OfflineValidationError("В пакете отсутствует шаблон чек-листа.")
    if not isinstance(answers, list) or not answers:
        raise OfflineValidationError("В чек-листе нет ответов.")
    if len(questions) > MAX_CHECKLIST_ANSWERS or len(answers) > MAX_CHECKLIST_ANSWERS:
        raise OfflineValidationError("В чек-листе слишком много ответов.")
    if len(answers) != len(questions):
        raise OfflineValidationError("Чек-лист заполнен не полностью.")

    validated = []
    indices = set()
    for answer in answers:
        if not isinstance(answer, dict):
            raise OfflineValidationError("Некорректный ответ чек-листа.")
        index = answer.get("question_index")
        if not isinstance(index, int) or index < 0 or index >= MAX_CHECKLIST_ANSWERS:
            raise OfflineValidationError("Некорректный номер вопроса.")
        if index in indices:
            raise OfflineValidationError("Один из вопросов заполнен дважды.")
        indices.add(index)
        status = str(answer.get("status") or "").strip()
        if status not in {"ok", "problem"}:
            raise OfflineValidationError("Некорректный статус ответа.")
        question_text = str(answer.get("question_text") or "").strip()
        question_title = str(answer.get("question_title") or "").strip()
        if not question_text or len(question_text) > MAX_QUESTION_LENGTH:
            raise OfflineValidationError("Некорректный текст вопроса.")
        snapshot = questions[index] if index < len(questions) else None
        if not isinstance(snapshot, dict) or str(snapshot.get("text") or "").strip() != question_text:
            raise OfflineValidationError("Вопросы чек-листа повреждены или изменены.")
        comment = str(answer.get("comment") or "").strip()
        if len(comment) > MAX_COMMENT_LENGTH:
            raise OfflineValidationError("Комментарий к вопросу слишком длинный.")
        validated.append(
            {
                "question_index": index,
                "question_text": question_text,
                "question_title": question_title,
                "status": status,
                "comment": comment,
                "photo_ids": answer.get("photo_ids") or [],
            }
        )
    if indices != set(range(len(validated))):
        raise OfflineValidationError("Чек-лист заполнен не полностью.")
    validated.sort(key=lambda item: item["question_index"])
    return checklist_type, validated


def _sync_checklist(
    db,
    payload,
    employee_name,
    timestamp,
    files,
    photos_dir,
    operation_id,
    allowed_extensions,
    upload_tracker=None,
):
    boat = _validate_boat(payload)
    checklist_type, answers = _validated_answers(payload)
    extra_raw = payload.get("extra_defects") or []
    if not isinstance(extra_raw, list) or len(extra_raw) > MAX_EXTRA_DEFECTS:
        raise OfflineValidationError("Некорректный список дополнительных неисправностей.")
    extra_defects = [_description(item) for item in extra_raw if str(item or "").strip()]
    required_ids = _attachment_references(answers)
    uploads = _save_attachments(
        files, required_ids, photos_dir, operation_id, allowed_extensions
    )
    if upload_tracker is not None:
        upload_tracker.update(uploads)

    started_at = _clean_timestamp(payload.get("started_at"), timestamp)
    completed_at = _clean_timestamp(payload.get("completed_at"), timestamp)
    cursor = db.execute(
        "INSERT INTO boat_checklists "
        "(employee_name, checklist_type, boat, started_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (employee_name, checklist_type, boat, started_at, completed_at),
    )
    checklist_id = cursor.lastrowid
    events = []
    for answer in answers:
        cursor = db.execute(
            "INSERT INTO boat_checklist_answers "
            "(checklist_id, question_index, question_text, status, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                checklist_id,
                answer["question_index"],
                answer["question_text"],
                answer["status"],
                answer["comment"] or None,
                completed_at,
            ),
        )
        answer_id = cursor.lastrowid
        photo_paths = []
        for attachment_id in answer["photo_ids"]:
            upload = uploads[attachment_id]
            db.execute(
                "INSERT INTO checklist_answer_photos (answer_id, filename, created_at) "
                "VALUES (?, ?, ?)",
                (answer_id, upload["filename"], completed_at),
            )
            photo_paths.append(upload["path"])
        if answer["status"] == "problem":
            label = answer["question_title"] or answer["question_text"]
            description = (
                f"{label} — {answer['comment']}" if answer["comment"] else label
            )
            defect_cursor = db.execute(
                "INSERT INTO boat_defects "
                "(boat, checklist_id, answer_id, description, employee_name, status, "
                "reported_at, updated_at) VALUES (?, ?, ?, ?, ?, 'new', ?, ?)",
                (
                    boat,
                    checklist_id,
                    answer_id,
                    description,
                    employee_name,
                    completed_at,
                    timestamp,
                ),
            )
            events.append(
                {
                    "kind": "checklist_problem",
                    "boat": boat,
                    "checklist_type": checklist_type,
                    "question": label,
                    "comment": answer["comment"],
                    "employee_name": employee_name,
                    "record_id": defect_cursor.lastrowid,
                    "photo_paths": photo_paths,
                }
            )

    for description in extra_defects:
        defect_cursor = db.execute(
            "INSERT INTO boat_defects "
            "(boat, checklist_id, answer_id, description, employee_name, status, "
            "reported_at, updated_at) VALUES (?, ?, NULL, ?, ?, 'new', ?, ?)",
            (boat, checklist_id, description, employee_name, completed_at, timestamp),
        )
        events.append(
            {
                "kind": "extra_defect",
                "boat": boat,
                "checklist_type": checklist_type,
                "description": description,
                "employee_name": employee_name,
                "record_id": defect_cursor.lastrowid,
                "photo_paths": [],
            }
        )
    return checklist_id, events, uploads


def sync_operation(db, operation, files, employee_name, photos_dir, allowed_extensions):
    """Replay one operation atomically and return its server id/events."""
    operation_id, operation_type, payload = _validate_operation(operation)
    existing = repository.get_operation(db, operation_id)
    if existing is not None:
        if existing["employee_name"] != employee_name:
            raise OfflineValidationError("Эта операция принадлежит другому сотруднику.")
        return {
            "created": False,
            "operation_id": operation_id,
            "record_id": existing["server_record_id"],
            "events": [],
        }

    received_at = current_timestamp()
    client_created_at = _clean_timestamp(operation.get("created_at"), received_at)
    uploads = {}
    try:
        with db:
            if operation_type == "defect":
                record_id, events = _sync_defect(
                    db, payload, employee_name, received_at
                )
            else:
                record_id, events, _saved_uploads = _sync_checklist(
                    db,
                    payload,
                    employee_name,
                    received_at,
                    files,
                    photos_dir,
                    operation_id,
                    allowed_extensions,
                    upload_tracker=uploads,
                )
            repository.record_operation(
                db,
                operation_id,
                employee_name,
                operation_type,
                record_id,
                client_created_at,
                received_at,
            )
    except (OfflineValidationError, sqlite3.Error):
        for item in uploads.values():
            try:
                os.remove(item["path"])
            except OSError:
                pass
        raise

    return {
        "created": True,
        "operation_id": operation_id,
        "record_id": record_id,
        "events": events,
    }
