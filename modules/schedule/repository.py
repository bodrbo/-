"""SQL access for the internal trip schedule."""

from modules.clients.constants import EXCURSION_SEGMENT
from modules.clients.services import ensure_segment


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


def list_day_crew_ids(db, day):
    return [
        row["employee_id"]
        for row in db.execute(
            "SELECT employee_id FROM schedule_day_crew "
            "WHERE work_date = ? ORDER BY created_at, employee_id",
            (day,),
        ).fetchall()
    ]


def list_day_assignment_employee_ids(db, day):
    return {
        row["employee_id"]
        for row in db.execute(
            "SELECT DISTINCT schedule_assignments.employee_id "
            "FROM schedule_assignments JOIN schedule_items "
            "ON schedule_items.id = schedule_assignments.schedule_item_id "
            "WHERE schedule_items.deleted_at IS NULL "
            "AND substr(schedule_items.starts_at, 1, 10) = ?",
            (day,),
        ).fetchall()
    }


def add_day_crew_member(db, day, employee_id, timestamp):
    cursor = db.execute(
        "INSERT OR IGNORE INTO schedule_day_crew "
        "(work_date, employee_id, created_at) VALUES (?, ?, ?)",
        (day, employee_id, timestamp),
    )
    db.commit()
    return cursor.rowcount > 0


def remove_day_crew_member(db, day, employee_id):
    cursor = db.execute(
        "DELETE FROM schedule_day_crew WHERE work_date = ? AND employee_id = ?",
        (day, employee_id),
    )
    db.commit()
    return cursor.rowcount > 0


def search_clients(db, query, limit=20):
    """Return a small ranked slice of excursion clients for autocomplete."""
    words = [word for word in str(query or "").strip().casefold().split() if word]
    if not words:
        return []
    rows = db.execute(
        "SELECT clients.id, clients.client_name, clients.phone, clients.status "
        "FROM clients JOIN client_segments "
        "ON client_segments.client_id = clients.id "
        "AND client_segments.segment = ?",
        (EXCURSION_SEGMENT,),
    ).fetchall()
    ranked = []
    exact_query = " ".join(words)
    for row in rows:
        client = dict(row)
        searchable = f"{client['client_name']} {client['phone']}".casefold()
        if not all(word in searchable for word in words):
            continue
        name = client["client_name"].casefold()
        if name.startswith(exact_query):
            score = 0
        elif exact_query in name:
            score = 1
        else:
            score = 2 + sum(searchable.index(word) for word in words)
        ranked.append((score, name, client["id"], client))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:limit]]


def list_all_clients(db):
    """All identities are used only for safe phone deduplication."""
    return [
        dict(row)
        for row in db.execute(
            "SELECT id, client_name, phone, status FROM clients "
            "ORDER BY client_name COLLATE NOCASE, phone, id"
        ).fetchall()
    ]


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
    participants = db.execute(
        "SELECT * FROM schedule_participants "
        f"WHERE schedule_item_id IN ({placeholders}) "
        "ORDER BY schedule_item_id, id",
        tuple(item_ids),
    ).fetchall()
    participants_by_item = {}
    for participant in participants:
        participants_by_item.setdefault(
            participant["schedule_item_id"], []
        ).append(dict(participant))
    result = []
    for item in items:
        row = dict(item)
        row["assignments"] = by_item.get(item["id"], [])
        row["participants"] = participants_by_item.get(item["id"], [])
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


def save_item(db, item_id, data, assignments, participants, timestamp):
    try:
        if item_id is None:
            cursor = db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_id, service_name, starts_at, ends_at, capacity, "
                "participants_count, customer_name, customer_phone, revenue, "
                "note, status, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', "
                "'internal', ?, ?)",
                (
                    data["kind"], data["boat"], data["service_id"], data["service_name"],
                    data["starts_at"], data["ends_at"], data["capacity"],
                    data["participants_count"], data["customer_name"],
                    data["customer_phone"], data["revenue"], data["note"],
                    timestamp, timestamp,
                ),
            )
            item_id = cursor.lastrowid
        else:
            db.execute(
                "UPDATE schedule_items SET kind = ?, boat = ?, service_id = ?, service_name = ?, "
                "starts_at = ?, ends_at = ?, capacity = ?, participants_count = ?, "
                "customer_name = ?, customer_phone = ?, revenue = ?, note = ?, "
                "updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (
                    data["kind"], data["boat"], data["service_id"], data["service_name"],
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
            db.execute(
                "DELETE FROM schedule_participants WHERE schedule_item_id = ?",
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
            db.execute(
                "INSERT OR IGNORE INTO schedule_day_crew "
                "(work_date, employee_id, created_at) VALUES (?, ?, ?)",
                (data["starts_at"][:10], assignment["employee_id"], timestamp),
            )
        for participant in participants:
            client_id = participant["client_id"]
            if client_id is None:
                cursor = db.execute(
                    "INSERT INTO clients "
                    "(client_name, boat_model, phone, token, created_at) "
                    "VALUES (?, '', ?, ?, ?)",
                    (
                        participant["client_name"], participant["client_phone"],
                        participant["client_token"], timestamp,
                    ),
                )
                client_id = cursor.lastrowid
            ensure_segment(db, client_id, EXCURSION_SEGMENT, timestamp)
            db.execute(
                "INSERT INTO schedule_participants "
                "(schedule_item_id, client_id, client_name, client_phone, "
                "guests_count, price, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id, client_id, participant["client_name"],
                    participant["client_phone"], participant["guests_count"],
                    participant["price"], timestamp,
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
