"""SQL access for the fleet domain.

Keeping SQL here lets route handlers describe HTTP behaviour while the
service layer owns validations and domain transitions.
"""


def list_checklists(db, boat):
    return db.execute(
        "SELECT * FROM boat_checklists WHERE boat = ? ORDER BY started_at DESC, id DESC",
        (boat,),
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


def list_defects(db, boat):
    return db.execute(
        "SELECT * FROM boat_defects WHERE boat = ? ORDER BY reported_at DESC, id DESC",
        (boat,),
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


def add_assignment(db, defect_id, employee_name, rate, norm_hours, assigned_at):
    cur = db.execute(
        "INSERT INTO defect_assignments "
        "(defect_id, employee_name, rate, norm_hours, assignment_status, assigned_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (defect_id, employee_name, rate, norm_hours, assigned_at),
    )
    db.commit()
    return cur.lastrowid
