"""Business operations shared by the fleet HTTP interfaces."""

import datetime as dt

from flask import url_for

from . import repository
from .constants import (
    BOATS,
    CHECKLIST_QUESTIONS,
    DEFECT_ASSIGNABLE_POSITIONS,
    DEFECT_PLAN_STATUSES,
    DEFECT_STATUSES,
    TASK_ASSIGNMENT_COMMENT_MAX_LENGTH,
)


DEFECT_DESCRIPTION_MAX_LENGTH = 1000


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def boat_by_index(boat_index):
    """Resolve the ASCII index used in public fleet URLs to a vessel name."""
    if 0 <= boat_index < len(BOATS):
        return BOATS[boat_index]["name"]
    return None


def boat_photo_url(profile):
    if profile is None or not profile["photo_filename"]:
        return None
    return url_for(
        "static", filename=f"fleet_boats/{profile['photo_filename']}"
    )


def fleet_boat_cards(db, fuel_summary):
    """Build the fleet index view models without mutating reference data."""
    profiles = {
        row["boat"]: row for row in repository.list_boat_profiles(db)
    }
    cards = []
    for index, boat in enumerate(BOATS):
        name = boat["name"]
        cards.append(
            {
                **boat,
                "index": index,
                "photo_url": boat_photo_url(profiles.get(name)),
                "fuel": fuel_summary(db, name, 0),
            }
        )
    return cards


def checklist_questions_for(checklist_type, boat):
    section = CHECKLIST_QUESTIONS.get(checklist_type) or {}
    return list(section.get("common", [])) + list(section.get("by_boat", {}).get(boat, []))


def get_checklist_answer_photos(db, answer_id):
    rows = repository.list_checklist_answer_photos(db, answer_id)
    return [
        {
            "id": row["id"],
            "url": url_for("static", filename=f"checklist_photos/{row['filename']}"),
            "comment": None,
        }
        for row in rows
    ]


def fleet_boat_checklists(db, boat):
    checklists = []
    for row in repository.list_checklists(db, boat):
        questions = checklist_questions_for(row["checklist_type"], row["boat"])
        answers = repository.list_checklist_answers(db, row["id"])
        problems = [
            {
                "question_text": answer["question_text"],
                "comment": answer["comment"],
                "photos": get_checklist_answer_photos(db, answer["id"]),
            }
            for answer in answers
            if answer["status"] == "problem"
        ]
        checklists.append(
            {
                "id": row["id"],
                "checklist_type": row["checklist_type"],
                "employee_name": row["employee_name"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "total": len(questions),
                "answered": len(answers),
                "problems": problems,
            }
        )
    return checklists


def defects_for_boat(db, boat):
    defects = []
    for row in repository.list_defects(db, boat):
        defect = dict(row)
        assignment_row = repository.get_latest_assignment(db, defect["id"])
        assignment = dict(assignment_row) if assignment_row else None
        defect["assignment"] = assignment
        defect["can_assign"] = (
            assignment is None
            or assignment["assignment_status"] == "rejected"
            or (
                assignment["assignment_status"] == "accepted"
                and assignment["entry_id"] is not None
            )
        )
        defects.append(defect)
    return defects


def create_manual_defect(db, boat, description, reported_by):
    """Create a current defect reported outside a checklist.

    Both the administrator and captain interfaces use this operation so the
    validation and initial state cannot drift between the two entry points.
    """
    valid_boats = {item["name"] for item in BOATS}
    description = (description or "").strip()
    reported_by = (reported_by or "").strip()

    if boat not in valid_boats:
        return False, "Не удалось определить судно.", None
    if not description:
        return False, "Опишите неисправность.", None
    if len(description) > DEFECT_DESCRIPTION_MAX_LENGTH:
        return (
            False,
            f"Описание должно быть не длиннее {DEFECT_DESCRIPTION_MAX_LENGTH} символов.",
            None,
        )

    defect_id = repository.add_defect(
        db,
        boat,
        description,
        reported_by or "Не указано",
        current_timestamp(),
    )
    return True, "Неисправность добавлена в текущий список.", defect_id


def split_defects(defects):
    current = [defect for defect in defects if defect["status"] != "resolved"]
    archived = sorted(
        (defect for defect in defects if defect["status"] == "resolved"),
        key=lambda defect: (defect["updated_at"], defect["id"]),
        reverse=True,
    )
    return current, archived


def assignable_employees(db):
    return repository.list_employees_with_positions(db, DEFECT_ASSIGNABLE_POSITIONS)


def defect_detail_context(db, defect, viewer_role, boat_index=None):
    plan_items = repository.list_plan_items(db, defect["id"])
    completed_count = sum(1 for item in plan_items if item["status"] == "done")
    return {
        "defect": defect,
        "boats": BOATS,
        "transfer_history": repository.list_defect_transfers(db, defect["id"]),
        "plan_items": plan_items,
        "completed_count": completed_count,
        "defect_statuses": DEFECT_STATUSES,
        "plan_statuses": DEFECT_PLAN_STATUSES,
        "viewer_role": viewer_role,
        "boat_index": boat_index,
        "active_page": "fleet" if viewer_role == "admin" else None,
    }


def transfer_defect(db, defect_id, source_boat, destination_boat, transferred_by):
    valid_boats = {item["name"] for item in BOATS}
    if source_boat not in valid_boats or destination_boat not in valid_boats:
        return False, "Не удалось определить выбранный катер."
    if source_boat == destination_boat:
        return False, "Выберите другой катер для переноса."
    if repository.get_defect(db, defect_id, source_boat) is None:
        return False, "Неисправность не найдена у исходного катера."

    transferred = repository.transfer_defect(
        db,
        defect_id,
        source_boat,
        destination_boat,
        (transferred_by or "Администратор").strip() or "Администратор",
        current_timestamp(),
    )
    if not transferred:
        return False, "Не удалось перенести неисправность. Обновите страницу."
    return True, f"Неисправность перенесена на катер «{destination_boat}»."


def save_defect_case_notes(db, defect_id, form):
    repository.save_case_notes(
        db,
        defect_id,
        form.get("anamnesis", "").strip(),
        form.get("diagnosis", "").strip(),
        current_timestamp(),
    )


def add_defect_plan_item(db, defect_id, form):
    description = form.get("description", "").strip()
    if description:
        repository.add_plan_item(db, defect_id, description, current_timestamp())


def set_defect_plan_item_status(db, defect_id, item_id, status):
    valid_statuses = {item["value"] for item in DEFECT_PLAN_STATUSES}
    if status in valid_statuses:
        repository.set_plan_item_status(db, defect_id, item_id, status, current_timestamp())


def change_defect_status(db, boat, defect_id, status):
    valid_statuses = {item["value"] for item in DEFECT_STATUSES}
    if status not in valid_statuses:
        return False
    repository.set_defect_status(db, boat, defect_id, status, current_timestamp())
    return True


def delete_defect(db, boat, defect_id):
    """Remove a defect aggregate only when it belongs to the selected boat."""
    return repository.delete_defect(db, defect_id, boat)


def create_defect_assignment(
    db, defect_id, employee_name, rate_raw, hours_raw, comment_raw=""
):
    valid_employees = assignable_employees(db)
    try:
        rate = float((rate_raw or "").strip().replace(",", "."))
        hours = float((hours_raw or "").strip().replace(",", "."))
    except ValueError:
        return None

    if employee_name not in valid_employees or rate <= 0 or hours <= 0:
        return None
    comment = (comment_raw or "").strip()
    if len(comment) > TASK_ASSIGNMENT_COMMENT_MAX_LENGTH:
        return None

    return repository.add_assignment(
        db, defect_id, employee_name, rate, hours, comment, current_timestamp()
    )
