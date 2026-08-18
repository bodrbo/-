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
)


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def boat_by_index(boat_index):
    """Resolve the ASCII index used in public fleet URLs to a vessel name."""
    if 0 <= boat_index < len(BOATS):
        return BOATS[boat_index]["name"]
    return None


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
        "plan_items": plan_items,
        "completed_count": completed_count,
        "defect_statuses": DEFECT_STATUSES,
        "plan_statuses": DEFECT_PLAN_STATUSES,
        "viewer_role": viewer_role,
        "boat_index": boat_index,
        "active_page": "fleet" if viewer_role == "admin" else None,
    }


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


def create_defect_assignment(db, defect_id, employee_name, rate_raw, hours_raw):
    valid_employees = assignable_employees(db)
    try:
        rate = float((rate_raw or "").strip().replace(",", "."))
        hours = float((hours_raw or "").strip().replace(",", "."))
    except ValueError:
        return False

    if employee_name not in valid_employees or rate <= 0 or hours <= 0:
        return False

    repository.add_assignment(db, defect_id, employee_name, rate, hours, current_timestamp())
    return True
