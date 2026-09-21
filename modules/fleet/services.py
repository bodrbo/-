"""Business operations shared by the fleet HTTP interfaces."""

import datetime as dt
import re
import sqlite3

from flask import url_for

from . import repository
from .constants import (
    CHECKLIST_QUESTIONS,
    DEFECT_ASSIGNABLE_POSITIONS,
    DEFECT_PLAN_STATUSES,
    DEFECT_STATUSES,
    TASK_ASSIGNMENT_COMMENT_MAX_LENGTH,
)
from .schema import boats_for_db, refresh_runtime_fleet


DEFECT_DESCRIPTION_MAX_LENGTH = 1000
FLEET_ARCHIVE_PAGE_SIZE = 20
VESSEL_NAME_MAX_LENGTH = 120
VESSEL_SPECIFICATIONS_MAX_LENGTH = 5000
VESSEL_DIMENSION_MAX_METERS = 1000
VESSEL_TANK_MAX_LITERS = 10000
DEFAULT_VESSEL_COLOR = "#607d8b"


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def boat_by_index(db, boat_index):
    """Resolve the ASCII index used in public fleet URLs to a vessel name."""
    boats = boats_for_db(db)
    if 0 <= boat_index < len(boats):
        return boats[boat_index]["name"]
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
    for index, vessel in enumerate(repository.list_vessels(db)):
        name = vessel["name"]
        cards.append(
            {
                **dict(vessel),
                "index": index,
                "photo_url": boat_photo_url(profiles.get(name)),
                "fuel": fuel_summary(db, name, 0),
            }
        )
    return cards


def vessel_for_boat(db, boat):
    return repository.get_vessel_by_name(db, boat)


def _parse_positive_number(raw_value, label, errors, maximum, required=False):
    raw_value = str(raw_value or "").strip().replace(" ", "").replace(",", ".")
    if not raw_value:
        if required:
            errors.append(f"Заполните поле «{label}».")
        return None
    try:
        value = float(raw_value)
    except ValueError:
        errors.append(f"Поле «{label}» должно быть числом.")
        return None
    if value <= 0 or value > maximum:
        errors.append(f"Поле «{label}» должно быть больше нуля и не больше {maximum:g}.")
        return None
    return value


def parse_vessel_form(form):
    errors = []
    name = " ".join(str(form.get("name") or "").strip().split())
    if not name:
        errors.append("Укажите название катера.")
    elif len(name) > VESSEL_NAME_MAX_LENGTH:
        errors.append(f"Название должно быть не длиннее {VESSEL_NAME_MAX_LENGTH} символов.")

    tank_capacity = _parse_positive_number(
        form.get("tank_capacity_liters"), "Объём бака", errors,
        VESSEL_TANK_MAX_LITERS, required=True,
    )
    length_m = _parse_positive_number(
        form.get("length_m"), "Длина", errors, VESSEL_DIMENSION_MAX_METERS,
    )
    width_m = _parse_positive_number(
        form.get("width_m"), "Ширина", errors, VESSEL_DIMENSION_MAX_METERS,
    )
    schedule_color = str(form.get("schedule_color") or "").strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", schedule_color):
        errors.append("Выберите корректный цвет расписания.")
        schedule_color = DEFAULT_VESSEL_COLOR
    specifications = str(form.get("specifications") or "").strip()
    if len(specifications) > VESSEL_SPECIFICATIONS_MAX_LENGTH:
        errors.append(
            f"Характеристики должны быть не длиннее "
            f"{VESSEL_SPECIFICATIONS_MAX_LENGTH} символов."
        )
    return {
        "name": name,
        "tank_capacity_liters": tank_capacity or 0,
        "schedule_color": schedule_color,
        "length_m": length_m,
        "width_m": width_m,
        "specifications": specifications,
    }, errors


def create_vessel(db, form, refresh_runtime=False):
    data, errors = parse_vessel_form(form)
    existing = repository.get_vessel_by_name(db, data["name"]) if data["name"] else None
    if existing is not None and not existing["deleted_at"]:
        errors.append("Катер с таким названием уже есть во флоте.")
    if errors:
        return False, " ".join(errors), None
    try:
        vessel_id = repository.create_vessel(db, data, current_timestamp())
    except sqlite3.IntegrityError:
        return False, "Катер с таким названием уже есть во флоте.", None
    if refresh_runtime:
        refresh_runtime_fleet(db)
    boats = boats_for_db(db)
    index = next(
        (position for position, boat in enumerate(boats) if boat["name"] == data["name"]),
        None,
    )
    return True, f"Катер «{data['name']}» добавлен во флот.", index


def update_vessel(db, vessel_id, form, refresh_runtime=False):
    vessel = repository.get_vessel(db, vessel_id)
    if vessel is None or vessel["deleted_at"]:
        return False, "Катер не найден.", None
    data, errors = parse_vessel_form(form)
    duplicate = repository.get_vessel_by_name(db, data["name"]) if data["name"] else None
    if duplicate is not None and duplicate["id"] != vessel_id:
        errors.append("Катер с таким названием уже существует.")
    if errors:
        return False, " ".join(errors), None
    try:
        repository.update_vessel(db, vessel_id, data, current_timestamp())
    except sqlite3.IntegrityError:
        return False, "Не удалось переименовать катер: такое название уже занято.", None
    if refresh_runtime:
        refresh_runtime_fleet(db)
    boats = boats_for_db(db)
    index = next(
        (position for position, boat in enumerate(boats) if boat["name"] == data["name"]),
        None,
    )
    return True, "Параметры катера сохранены.", index


def archive_vessel(db, vessel_id, refresh_runtime=False):
    vessel = repository.get_vessel(db, vessel_id)
    if vessel is None or vessel["deleted_at"]:
        return False, "Катер уже удалён или не найден."
    if len(repository.list_vessels(db)) <= 1:
        return False, "Нельзя удалить единственный катер из флота."
    repository.archive_vessel(db, vessel_id, current_timestamp())
    if refresh_runtime:
        refresh_runtime_fleet(db)
    return (
        True,
        f"Катер «{vessel['name']}» убран из действующего флота. История сохранена.",
    )


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


def fleet_boat_checklists(db, boat, date_from=None, date_to=None, page=1, per_page=FLEET_ARCHIVE_PAGE_SIZE):
    total = repository.count_checklists(db, boat, date_from, date_to)
    total_pages = max(1, -(-total // per_page))  # ceiling division, no math import needed
    page = max(1, min(page, total_pages))
    checklists = []
    for row in repository.list_checklists(db, boat, date_from, date_to, page, per_page):
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
    return {
        "items": checklists,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "per_page": per_page,
    }


def fleet_pagination_items(current_page, total_pages):
    """Page-number list for the pager, collapsing distant pages to a single
    ellipsis — same shape as app.py's _client_pagination_items, duplicated
    here rather than imported to avoid a circular import (app.py imports
    this package to build its blueprint)."""
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    visible = sorted({
        1,
        total_pages,
        max(1, current_page - 1),
        current_page,
        min(total_pages, current_page + 1),
    })
    items = []
    previous = None
    for page_number in visible:
        if previous is not None and page_number - previous > 1:
            items.append(None)
        items.append(page_number)
        previous = page_number
    return items


def _enrich_defect_with_assignment(db, row):
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
    return defect


def current_defects_for_boat(db, boat):
    return [
        _enrich_defect_with_assignment(db, row)
        for row in repository.list_current_defects(db, boat)
    ]


def archived_defects_for_boat(db, boat, date_from=None, date_to=None, page=1, per_page=FLEET_ARCHIVE_PAGE_SIZE):
    total = repository.count_archived_defects(db, boat, date_from, date_to)
    total_pages = max(1, -(-total // per_page))
    page = max(1, min(page, total_pages))
    items = [
        _enrich_defect_with_assignment(db, row)
        for row in repository.list_archived_defects(db, boat, date_from, date_to, page, per_page)
    ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "per_page": per_page,
    }


def create_manual_defect(db, boat, description, reported_by):
    """Create a current defect reported outside a checklist.

    Both the administrator and captain interfaces use this operation so the
    validation and initial state cannot drift between the two entry points.
    """
    valid_boats = {item["name"] for item in boats_for_db(db)}
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


def assignable_employees(db):
    return repository.list_employees_with_positions(db, DEFECT_ASSIGNABLE_POSITIONS)


def defect_detail_context(db, defect, viewer_role, boat_index=None):
    plan_items = repository.list_plan_items(db, defect["id"])
    completed_count = sum(1 for item in plan_items if item["status"] == "done")
    return {
        "defect": defect,
        "boats": boats_for_db(db),
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
    valid_boats = {item["name"] for item in boats_for_db(db)}
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
