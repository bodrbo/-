"""SQL access for the fleet domain.

Keeping SQL here lets route handlers describe HTTP behaviour while the
service layer owns validations and domain transitions.
"""

from .schema import rename_vessel_references


def list_vessels(db, include_archived=False):
    where = "" if include_archived else "WHERE deleted_at IS NULL"
    return db.execute(
        f"SELECT * FROM fleet_vessels {where} ORDER BY sort_order, id"
    ).fetchall()


def get_vessel(db, vessel_id):
    return db.execute(
        "SELECT * FROM fleet_vessels WHERE id = ?", (vessel_id,)
    ).fetchone()


def get_vessel_by_name(db, name):
    return db.execute(
        "SELECT * FROM fleet_vessels WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()


def create_vessel(db, data, timestamp):
    archived = get_vessel_by_name(db, data["name"])
    if archived is not None and archived["deleted_at"]:
        db.execute(
            "UPDATE fleet_vessels SET name = ?, tank_capacity_liters = ?, "
            "schedule_color = ?, capacity = ?, length_m = ?, width_m = ?, specifications = ?, "
            "sort_order = (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM fleet_vessels), "
            "updated_at = ?, deleted_at = NULL WHERE id = ?",
            (
                data["name"], data["tank_capacity_liters"], data["schedule_color"],
                data["capacity"], data["length_m"], data["width_m"], data["specifications"],
                timestamp, archived["id"],
            ),
        )
        vessel_id = archived["id"]
    else:
        cursor = db.execute(
            "INSERT INTO fleet_vessels "
            "(name, tank_capacity_liters, schedule_color, capacity, length_m, width_m, "
            "specifications, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, "
            "(SELECT COALESCE(MAX(sort_order), -1) + 1 FROM fleet_vessels), ?, ?)",
            (
                data["name"], data["tank_capacity_liters"], data["schedule_color"],
                data["capacity"], data["length_m"], data["width_m"], data["specifications"],
                timestamp, timestamp,
            ),
        )
        vessel_id = cursor.lastrowid
    db.execute(
        "INSERT OR IGNORE INTO boat_fuel_state (boat, updated_at) VALUES (?, ?)",
        (data["name"], timestamp),
    )
    db.commit()
    return vessel_id


def update_vessel(db, vessel_id, data, timestamp):
    vessel = get_vessel(db, vessel_id)
    if vessel is None or vessel["deleted_at"]:
        return False
    with db:
        if vessel["name"] != data["name"]:
            rename_vessel_references(db, vessel["name"], data["name"])
        db.execute(
            "UPDATE fleet_vessels SET name = ?, tank_capacity_liters = ?, "
            "schedule_color = ?, capacity = ?, length_m = ?, width_m = ?, specifications = ?, "
            "updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (
                data["name"], data["tank_capacity_liters"], data["schedule_color"],
                data["capacity"], data["length_m"], data["width_m"], data["specifications"],
                timestamp, vessel_id,
            ),
        )
    return True


def archive_vessel(db, vessel_id, timestamp):
    cursor = db.execute(
        "UPDATE fleet_vessels SET deleted_at = ?, updated_at = ? "
        "WHERE id = ? AND deleted_at IS NULL",
        (timestamp, timestamp, vessel_id),
    )
    db.commit()
    return cursor.rowcount == 1


def list_boat_profiles(db):
    return db.execute(
        "SELECT * FROM fleet_boat_profiles ORDER BY boat"
    ).fetchall()


def get_boat_profile(db, boat):
    return db.execute(
        "SELECT * FROM fleet_boat_profiles WHERE boat = ?", (boat,)
    ).fetchone()


def save_boat_photo(db, boat, filename, updated_at):
    existing = get_boat_profile(db, boat)
    if existing is None:
        db.execute(
            "INSERT INTO fleet_boat_profiles (boat, photo_filename, updated_at) "
            "VALUES (?, ?, ?)",
            (boat, filename, updated_at),
        )
    else:
        db.execute(
            "UPDATE fleet_boat_profiles SET photo_filename = ?, updated_at = ? "
            "WHERE boat = ?",
            (filename, updated_at, boat),
        )
    db.commit()


def _checklist_date_filter(boat, date_from, date_to):
    """started_at is "YYYY-MM-DD HH:MM" text; a bare "YYYY-MM-DD" bound
    compares correctly against it lexicographically (it's a prefix of any
    timestamp on that day), so no date parsing is needed for >= date_from.
    date_to gets " 23:59" appended so the whole end day is included, not
    just its midnight."""
    query = " WHERE boat = ?"
    params = [boat]
    if date_from:
        query += " AND started_at >= ?"
        params.append(date_from)
    if date_to:
        query += " AND started_at <= ?"
        params.append(date_to + " 23:59")
    return query, params


def count_checklists(db, boat, date_from=None, date_to=None):
    where, params = _checklist_date_filter(boat, date_from, date_to)
    return db.execute(
        f"SELECT COUNT(*) FROM boat_checklists{where}", params
    ).fetchone()[0]


def list_checklists(db, boat, date_from=None, date_to=None, page=1, per_page=20):
    where, params = _checklist_date_filter(boat, date_from, date_to)
    params = params + [per_page, (page - 1) * per_page]
    return db.execute(
        f"SELECT * FROM boat_checklists{where} "
        "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()


def list_checklist_answers(db, checklist_id):
    return db.execute(
        "SELECT * FROM boat_checklist_answers WHERE checklist_id = ? ORDER BY question_index",
        (checklist_id,),
    ).fetchall()


def list_checklist_answer_photos(db, answer_id):
    return db.execute(
        "SELECT id, filename FROM checklist_answer_photos WHERE answer_id = ? ORDER BY id",
        (answer_id,),
    ).fetchall()


def list_documents(db, boat):
    return db.execute(
        "SELECT * FROM boat_documents WHERE boat = ? ORDER BY uploaded_at DESC, id DESC",
        (boat,),
    ).fetchall()


def get_document(db, boat, document_id):
    return db.execute(
        "SELECT * FROM boat_documents WHERE id = ? AND boat = ?",
        (document_id, boat),
    ).fetchone()


def add_document(db, boat, title, filename, original_filename, uploaded_at):
    db.execute(
        "INSERT INTO boat_documents (boat, title, filename, original_filename, uploaded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (boat, title, filename, original_filename, uploaded_at),
    )
    db.commit()


def delete_document(db, document_id):
    db.execute("DELETE FROM boat_documents WHERE id = ?", (document_id,))
    db.commit()


def list_current_defects(db, boat):
    return db.execute(
        "SELECT * FROM boat_defects WHERE boat = ? AND status != 'resolved' "
        "ORDER BY reported_at DESC, id DESC",
        (boat,),
    ).fetchall()


def _archived_defect_date_filter(boat, date_from, date_to):
    """Filters on reported_at ("Обнаружено", the only date column shown in
    the archive table) — see _checklist_date_filter above for why a bare
    "YYYY-MM-DD" bound compares correctly against it as text."""
    query = " WHERE boat = ? AND status = 'resolved'"
    params = [boat]
    if date_from:
        query += " AND reported_at >= ?"
        params.append(date_from)
    if date_to:
        query += " AND reported_at <= ?"
        params.append(date_to + " 23:59")
    return query, params


def count_archived_defects(db, boat, date_from=None, date_to=None):
    where, params = _archived_defect_date_filter(boat, date_from, date_to)
    return db.execute(
        f"SELECT COUNT(*) FROM boat_defects{where}", params
    ).fetchone()[0]


def list_archived_defects(db, boat, date_from=None, date_to=None, page=1, per_page=20):
    where, params = _archived_defect_date_filter(boat, date_from, date_to)
    params = params + [per_page, (page - 1) * per_page]
    return db.execute(
        f"SELECT * FROM boat_defects{where} "
        # Sort stays on updated_at (most recently resolved first), unlike
        # the reported_at filter above — archive order was never tied to
        # discovery date, only the new date filter is.
        "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()


def get_defect(db, defect_id, boat=None):
    if boat is None:
        return db.execute(
            "SELECT * FROM boat_defects WHERE id = ?", (defect_id,)
        ).fetchone()
    return db.execute(
        "SELECT * FROM boat_defects WHERE id = ? AND boat = ?", (defect_id, boat)
    ).fetchone()


def list_defect_transfers(db, defect_id):
    return db.execute(
        "SELECT * FROM boat_defect_transfers WHERE defect_id = ? "
        "ORDER BY transferred_at DESC, id DESC",
        (defect_id,),
    ).fetchall()


def transfer_defect(
    db,
    defect_id,
    source_boat,
    destination_boat,
    transferred_by,
    transferred_at,
):
    with db:
        cursor = db.execute(
            "UPDATE boat_defects SET boat = ?, updated_at = ? "
            "WHERE id = ? AND boat = ?",
            (destination_boat, transferred_at, defect_id, source_boat),
        )
        if cursor.rowcount != 1:
            return False
        db.execute(
            "INSERT INTO boat_defect_transfers "
            "(defect_id, source_boat, destination_boat, transferred_by, transferred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                defect_id,
                source_boat,
                destination_boat,
                transferred_by,
                transferred_at,
            ),
        )
    return True


def add_defect(db, boat, description, employee_name, reported_at):
    cursor = db.execute(
        "INSERT INTO boat_defects "
        "(boat, checklist_id, answer_id, description, employee_name, status, "
        "reported_at, updated_at) "
        "VALUES (?, NULL, NULL, ?, ?, 'new', ?, ?)",
        (boat, description, employee_name, reported_at, reported_at),
    )
    db.commit()
    return cursor.lastrowid


def get_latest_assignment(db, defect_id):
    return db.execute(
        "SELECT * FROM defect_assignments WHERE defect_id = ? ORDER BY id DESC LIMIT 1",
        (defect_id,),
    ).fetchone()


def list_plan_items(db, defect_id):
    return db.execute(
        "SELECT * FROM defect_work_plan_items WHERE defect_id = ? ORDER BY id",
        (defect_id,),
    ).fetchall()


def save_case_notes(db, defect_id, anamnesis, diagnosis, updated_at):
    db.execute(
        "UPDATE boat_defects SET anamnesis = ?, diagnosis = ?, updated_at = ? WHERE id = ?",
        (anamnesis, diagnosis, updated_at, defect_id),
    )
    db.commit()


def add_plan_item(db, defect_id, description, timestamp):
    db.execute(
        "INSERT INTO defect_work_plan_items "
        "(defect_id, description, status, created_at, updated_at) "
        "VALUES (?, ?, 'pending', ?, ?)",
        (defect_id, description, timestamp, timestamp),
    )
    db.commit()


def set_plan_item_status(db, defect_id, item_id, status, updated_at):
    db.execute(
        "UPDATE defect_work_plan_items SET status = ?, updated_at = ? "
        "WHERE id = ? AND defect_id = ?",
        (status, updated_at, item_id, defect_id),
    )
    db.commit()


def set_defect_status(db, boat, defect_id, status, updated_at):
    db.execute(
        "UPDATE boat_defects SET status = ?, updated_at = ? WHERE id = ? AND boat = ?",
        (status, updated_at, defect_id, boat),
    )
    db.commit()


def delete_defect(db, defect_id, boat):
    """Delete one scoped defect and records that cannot exist without it.

    Payroll entries referenced by completed assignments deliberately remain:
    they are accounting history and are not owned by the defect aggregate.
    """
    defect = get_defect(db, defect_id, boat)
    if defect is None:
        return False

    with db:
        db.execute(
            "DELETE FROM boat_defect_transfers WHERE defect_id = ?", (defect_id,)
        )
        db.execute(
            "DELETE FROM defect_work_plan_items WHERE defect_id = ?", (defect_id,)
        )
        db.execute("DELETE FROM defect_assignments WHERE defect_id = ?", (defect_id,))
        db.execute(
            "DELETE FROM boat_defects WHERE id = ? AND boat = ?", (defect_id, boat)
        )
    return True


def list_employees_with_positions(db, positions):
    placeholders = ",".join("?" * len(positions))
    rows = db.execute(
        f"SELECT DISTINCT employees.name FROM employees "
        f"JOIN employee_positions ON employee_positions.employee_id = employees.id "
        f"WHERE employee_positions.position IN ({placeholders}) "
        f"AND employees.deleted_at IS NULL ORDER BY employees.name",
        positions,
    ).fetchall()
    return [row["name"] for row in rows]


def add_assignment(
    db, defect_id, employee_name, rate, norm_hours, comment, assigned_at
):
    cur = db.execute(
        "INSERT INTO defect_assignments "
        "(defect_id, employee_name, rate, norm_hours, comment, assignment_status, assigned_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (defect_id, employee_name, rate, norm_hours, comment, assigned_at),
    )
    db.commit()
    return cur.lastrowid
