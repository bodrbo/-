"""SQL access for the internal trip schedule."""


def list_crew_employees(db):
    rows = db.execute(
        "SELECT employees.id, employees.name, employee_positions.position "
        "FROM employees JOIN employee_positions "
        "ON employee_positions.employee_id = employees.id "
        "WHERE employees.deleted_at IS NULL "
        "AND employee_positions.position IN ('Капитан', 'Гид', 'Гид-капитан') "
        "ORDER BY employees.name, employee_positions.position"
    ).fetchall()
    employees = {}
    for row in rows:
        employee = employees.setdefault(
            row["id"],
            {"id": row["id"], "name": row["name"], "positions": []},
        )
        employee["positions"].append(row["position"])
    return list(employees.values())


def get_item(db, item_id, include_deleted=False):
    query = "SELECT * FROM schedule_items WHERE id = ?"
    if not include_deleted:
        query += " AND deleted_at IS NULL"
    return db.execute(query, (item_id,)).fetchone()


def list_assignments(db, item_id):
    return db.execute(
        "SELECT * FROM schedule_assignments WHERE schedule_item_id = ? "
        "ORDER BY id",
        (item_id,),
    ).fetchall()


def list_day_items(db, day):
    items = db.execute(
        "SELECT * FROM schedule_items "
        "WHERE deleted_at IS NULL AND substr(starts_at, 1, 10) = ? "
        "ORDER BY starts_at, id",
        (day,),
    ).fetchall()
    if not items:
        return []
    item_ids = [item["id"] for item in items]
    placeholders = ",".join("?" for _item_id in item_ids)
    assignments = db.execute(
        "SELECT * FROM schedule_assignments "
        f"WHERE schedule_item_id IN ({placeholders}) "
        "ORDER BY schedule_item_id, id",
        tuple(item_ids),
    ).fetchall()
    by_item = {}
    for assignment in assignments:
        by_item.setdefault(assignment["schedule_item_id"], []).append(
            dict(assignment)
        )
    result = []
    for item in items:
        row = dict(item)
        row["assignments"] = by_item.get(item["id"], [])
        result.append(row)
    return result


def find_employee_conflicts(db, employee_ids, starts_at, ends_at, exclude_id=None):
    if not employee_ids:
        return []
    placeholders = ",".join("?" for _employee_id in employee_ids)
    params = [*employee_ids, ends_at, starts_at]
    query = (
        "SELECT DISTINCT schedule_items.id, schedule_items.service_name, "
        "schedule_items.starts_at, schedule_items.ends_at, "
        "schedule_assignments.employee_name FROM schedule_items "
        "JOIN schedule_assignments ON schedule_assignments.schedule_item_id = schedule_items.id "
        "WHERE schedule_items.deleted_at IS NULL "
        f"AND schedule_assignments.employee_id IN ({placeholders}) "
        "AND schedule_items.starts_at < ? AND schedule_items.ends_at > ?"
    )
    if exclude_id is not None:
        query += " AND schedule_items.id != ?"
        params.append(exclude_id)
    return db.execute(query, tuple(params)).fetchall()


def find_boat_conflicts(db, boat, starts_at, ends_at, exclude_id=None):
    params = [boat, ends_at, starts_at]
    query = (
        "SELECT id, service_name, starts_at, ends_at FROM schedule_items "
        "WHERE deleted_at IS NULL AND boat = ? "
        "AND starts_at < ? AND ends_at > ?"
    )
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    return db.execute(query, tuple(params)).fetchall()


def save_item(db, item_id, data, assignments, timestamp):
    try:
        if item_id is None:
            cursor = db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_name, starts_at, ends_at, capacity, "
                "participants_count, customer_name, customer_phone, revenue, "
                "note, status, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', "
                "'internal', ?, ?)",
                (
                    data["kind"], data["boat"], data["service_name"],
                    data["starts_at"], data["ends_at"], data["capacity"],
                    data["participants_count"], data["customer_name"],
                    data["customer_phone"], data["revenue"], data["note"],
                    timestamp, timestamp,
                ),
            )
            item_id = cursor.lastrowid
        else:
            db.execute(
                "UPDATE schedule_items SET kind = ?, boat = ?, service_name = ?, "
                "starts_at = ?, ends_at = ?, capacity = ?, participants_count = ?, "
                "customer_name = ?, customer_phone = ?, revenue = ?, note = ?, "
                "updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (
                    data["kind"], data["boat"], data["service_name"],
                    data["starts_at"], data["ends_at"], data["capacity"],
                    data["participants_count"], data["customer_name"],
                    data["customer_phone"], data["revenue"], data["note"],
                    timestamp, item_id,
                ),
            )
            db.execute(
                "DELETE FROM schedule_assignments WHERE schedule_item_id = ?",
                (item_id,),
            )
        for assignment in assignments:
            db.execute(
                "INSERT INTO schedule_assignments "
                "(schedule_item_id, employee_id, employee_name, role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    item_id, assignment["employee_id"],
                    assignment["employee_name"], assignment["role"], timestamp,
                ),
            )
        db.commit()
        return item_id
    except Exception:
        db.rollback()
        raise


def soft_delete_item(db, item_id, timestamp):
    cursor = db.execute(
        "UPDATE schedule_items SET deleted_at = ?, updated_at = ? "
        "WHERE id = ? AND deleted_at IS NULL",
        (timestamp, timestamp, item_id),
    )
    db.commit()
    return cursor.rowcount > 0
