"""SQL access for the tuning-center work schedule."""


def list_tuning_crew_employees(db):
    """Staff eligible for the day roster — mirrors
    modules.schedule.repository.list_crew_employees, filtered to the
    tuning-center position instead of captain/guide, plus administrators
    (who may work in either schedule). Position names kept in sync with
    app.py's TUNING_ASSIGNABLE_POSITIONS ("Тюнингмэн") and
    modules/employees/constants.py by convention, the same way the
    excursion roster hardcodes its own position list rather than taking it
    as a parameter."""
    rows = db.execute(
        "SELECT employees.id, employees.name, employee_positions.position "
        "FROM employees JOIN employee_positions "
        "ON employee_positions.employee_id = employees.id "
        "WHERE employees.deleted_at IS NULL "
        "AND employee_positions.position IN ('Тюнингмэн', 'Администратор') "
        "ORDER BY employees.name"
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
            "SELECT employee_id FROM tuning_schedule_day_crew "
            "WHERE work_date = ? ORDER BY created_at, employee_id",
            (day,),
        ).fetchall()
    ]


def list_day_task_employee_ids(db, day):
    """Which employees already have a task placed on this day — used the
    same way modules.schedule's list_day_assignment_employee_ids is used,
    to block removing someone from the day roster while they still have
    work scheduled that day."""
    return {
        row["employee_id"]
        for row in db.execute(
            "SELECT DISTINCT employees.id AS employee_id "
            "FROM tuning_schedule_task_days d "
            "JOIN tuning_schedule_tasks t ON t.id = d.task_id "
            "JOIN employees ON employees.name = t.employee_name "
            "WHERE d.work_date = ? AND employees.deleted_at IS NULL "
            "UNION "
            "SELECT DISTINCT employees.id "
            "FROM tuning_schedule_task_days d "
            "JOIN tuning_schedule_task_members m ON m.task_id = d.task_id "
            "JOIN employees ON employees.name = m.employee_name "
            "WHERE d.work_date = ? AND employees.deleted_at IS NULL",
            (day, day),
        ).fetchall()
    }


def add_day_crew_member(db, day, employee_id, timestamp, shift_start=None, shift_end=None):
    cursor = db.execute(
        "INSERT OR IGNORE INTO tuning_schedule_day_crew "
        "(work_date, employee_id, created_at, shift_start, shift_end) VALUES (?, ?, ?, ?, ?)",
        (day, employee_id, timestamp, shift_start, shift_end),
    )
    db.commit()
    return cursor.rowcount > 0


def set_shift_hours(db, day, employee_id, shift_start, shift_end):
    cursor = db.execute(
        "UPDATE tuning_schedule_day_crew SET shift_start = ?, shift_end = ? "
        "WHERE work_date = ? AND employee_id = ?",
        (shift_start, shift_end, day, employee_id),
    )
    db.commit()
    return cursor.rowcount > 0


def list_day_crew_shifts(db, day):
    """{employee_id: (shift_start, shift_end)} for everyone on the roster of
    `day` (either may be None on rows from before shifts had hours)."""
    return {
        row["employee_id"]: (row["shift_start"], row["shift_end"])
        for row in db.execute(
            "SELECT employee_id, shift_start, shift_end FROM tuning_schedule_day_crew "
            "WHERE work_date = ?", (day,),
        ).fetchall()
    }


def get_day_crew_shift_by_name(db, day, employee_name):
    """The roster row (with shift hours) of an employee on `day`, looked up
    by name, or None when they are not on that day's roster."""
    return db.execute(
        "SELECT c.employee_id, c.shift_start, c.shift_end FROM tuning_schedule_day_crew c "
        "JOIN employees e ON e.id = c.employee_id "
        "WHERE c.work_date = ? AND e.name = ? AND e.deleted_at IS NULL",
        (day, employee_name),
    ).fetchone()


def get_employee_id_by_name(db, employee_name):
    row = db.execute(
        "SELECT id FROM employees WHERE name = ? AND deleted_at IS NULL", (employee_name,)
    ).fetchone()
    return row["id"] if row else None


def get_assignment_order_id(db, assignment_id):
    """The order a linked task belongs to (for "also changed on the board")."""
    row = db.execute(
        "SELECT ti.order_id FROM tuning_item_assignments tia "
        "JOIN tuning_order_items ti ON ti.id = tia.item_id WHERE tia.id = ?",
        (assignment_id,),
    ).fetchone()
    return row["order_id"] if row else None


def is_administrator(db, employee_name):
    """Administrators are salaried: their tasks carry no rate."""
    return db.execute(
        "SELECT 1 FROM employee_positions JOIN employees ON employees.id = employee_positions.employee_id "
        "WHERE employees.name = ? AND employees.deleted_at IS NULL "
        "AND employee_positions.position = 'Администратор'",
        (employee_name,),
    ).fetchone() is not None


def list_task_reminders(db, assignment_id, schedule_task_id):
    """Telegram reminders set on a task: against its order task when it has
    one (they then show on the order board too), else against the free
    schedule task itself."""
    return db.execute(
        "SELECT r.*, COALESCE(e.name, a.admin_name) AS remind_recipient_name "
        "FROM tuning_task_reminders r "
        "LEFT JOIN employees e ON e.id = r.remind_employee_id "
        "LEFT JOIN admin_accounts a ON a.id = r.remind_admin_id "
        "WHERE (? IS NOT NULL AND r.assignment_id = ?) OR r.schedule_task_id = ? "
        "ORDER BY r.remind_at, r.id",
        (assignment_id, assignment_id, schedule_task_id),
    ).fetchall()


def get_task_by_assignment(db, assignment_id):
    return db.execute(
        "SELECT * FROM tuning_schedule_tasks WHERE assignment_id = ?", (assignment_id,)
    ).fetchone()


def set_task_day_hours(db, day_id, planned_hours):
    db.execute(
        "UPDATE tuning_schedule_task_days SET planned_hours = ? WHERE id = ?",
        (planned_hours, day_id),
    )
    db.commit()


def delete_task(db, task_id):
    db.execute("DELETE FROM tuning_schedule_task_members WHERE task_id = ?", (task_id,))
    db.execute("DELETE FROM tuning_schedule_task_days WHERE task_id = ?", (task_id,))
    db.execute("DELETE FROM tuning_schedule_tasks WHERE id = ?", (task_id,))
    db.commit()


def delete_task_days(db, task_id):
    db.execute("DELETE FROM tuning_schedule_task_days WHERE task_id = ?", (task_id,))


def remove_day_crew_member(db, day, employee_id):
    cursor = db.execute(
        "DELETE FROM tuning_schedule_day_crew WHERE work_date = ? AND employee_id = ?",
        (day, employee_id),
    )
    db.commit()
    return cursor.rowcount > 0


_DAY_TASK_SELECT = (
    "SELECT t.id AS task_id, t.assignment_id, d.id AS day_id, "
    "d.work_date, d.start_time, d.planned_hours, "
    "COALESCE(tia.employee_name, t.employee_name) AS employee_name, "
    "COALESCE(ti.work_name, t.title) AS title, "
    "COALESCE(tia.rate, t.rate) AS rate, "
    "COALESCE(tia.assignment_status, t.status) AS status, "
    "COALESCE(tia.comment, t.comment) AS comment, "
    "o.id AS order_id, o.equipment_type, o.boat_model, o.motor_model "
    "FROM tuning_schedule_task_days d "
    "JOIN tuning_schedule_tasks t ON t.id = d.task_id "
    "LEFT JOIN tuning_item_assignments tia ON tia.id = t.assignment_id "
    "LEFT JOIN tuning_order_items ti ON ti.id = tia.item_id "
    "LEFT JOIN tuning_orders o ON o.id = COALESCE(ti.order_id, t.order_id) "
)


def list_day_tasks(db, day):
    """One row per task that has a placement on `day`, with that day's own
    planned_hours, plus (for a task linked to an order — assignment_id NOT
    NULL) the live status/rate/order context via tuning_item_assignments —
    those stay the single source of truth for a linked task rather than
    being copied onto tuning_schedule_tasks, so nothing here can drift out
    of sync with the order board. A free task (assignment_id IS NULL) uses
    its own employee_name/title/rate/status columns instead.

    A task with more than one participant yields one row per participant
    (the same task_id / day_id, that person's name and rate, is_member = 1
    for everyone after the first) — so a person's column, their free time and
    their roster all see the task."""
    rows = [dict(row, is_member=0) for row in db.execute(
        _DAY_TASK_SELECT + "WHERE d.work_date = ? ORDER BY employee_name, t.id",
        (day,),
    ).fetchall()]
    if not rows:
        return rows
    task_ids = sorted({row["task_id"] for row in rows})
    placeholders = ",".join("?" for _ in task_ids)
    members = {}
    for member in db.execute(
        "SELECT task_id, employee_name, rate, hours FROM tuning_schedule_task_members "
        f"WHERE task_id IN ({placeholders}) ORDER BY id",
        task_ids,
    ).fetchall():
        members.setdefault(member["task_id"], []).append(member)
    totals = {
        row["task_id"]: row["total"] for row in db.execute(
            "SELECT task_id, SUM(planned_hours) AS total FROM tuning_schedule_task_days "
            f"WHERE task_id IN ({placeholders}) GROUP BY task_id",
            task_ids,
        ).fetchall()
    }
    result = []
    for row in rows:
        result.append(row)
        total = totals.get(row["task_id"]) or 0
        for member in members.get(row["task_id"], []):
            # a person with their own norm-hours gets their share of this day
            hours = row["planned_hours"]
            if member["hours"] is not None and total > 0:
                hours = round(row["planned_hours"] * member["hours"] / total, 2)
            result.append(dict(
                row, employee_name=member["employee_name"], rate=member["rate"],
                planned_hours=hours, member_hours=member["hours"],
                is_member=1, primary_employee=row["employee_name"],
            ))
    result.sort(key=lambda row: (row["employee_name"], row["task_id"]))
    return result


def list_task_members(db, task_id):
    return db.execute(
        "SELECT * FROM tuning_schedule_task_members WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()


def replace_task_members(db, task_id, members):
    """Sets the further participants of a task to [(employee_name, rate,
    hours)] — hours None = the task's own; someone who stays keeps their
    payout record."""
    keep = {member[0] for member in members}
    for row in list_task_members(db, task_id):
        if row["employee_name"] not in keep:
            db.execute("DELETE FROM tuning_schedule_task_members WHERE id = ?", (row["id"],))
    for name, rate, hours in members:
        cursor = db.execute(
            "UPDATE tuning_schedule_task_members SET rate = ?, hours = ? "
            "WHERE task_id = ? AND employee_name = ?",
            (rate, hours, task_id, name),
        )
        if cursor.rowcount == 0:
            db.execute(
                "INSERT INTO tuning_schedule_task_members (task_id, employee_name, rate, hours) "
                "VALUES (?, ?, ?, ?)",
                (task_id, name, rate, hours),
            )
    db.commit()


def rename_task_member(db, task_id, old_name, new_name):
    db.execute(
        "UPDATE tuning_schedule_task_members SET employee_name = ? WHERE task_id = ? AND employee_name = ?",
        (new_name, task_id, old_name),
    )
    db.commit()


def set_member_entry(db, member_id, entry_id):
    db.execute(
        "UPDATE tuning_schedule_task_members SET entry_id = ? WHERE id = ?", (entry_id, member_id)
    )
    db.commit()


def delete_task_members(db, task_id):
    db.execute("DELETE FROM tuning_schedule_task_members WHERE task_id = ?", (task_id,))


def get_day_task(db, task_id, day_id):
    """The list_day_tasks row of one placement (task_id + its day row id)."""
    return db.execute(
        _DAY_TASK_SELECT + "WHERE d.id = ? AND t.id = ?", (day_id, task_id)
    ).fetchone()


def set_task_day_start(db, day_id, start_time):
    db.execute(
        "UPDATE tuning_schedule_task_days SET start_time = ? WHERE id = ?",
        (start_time, day_id),
    )
    db.commit()


def update_free_task(db, task_id, title, rate, comment, order_id):
    """Title, rate, comment and project of a task that isn't tied to a work
    item (assignment_id IS NULL)."""
    db.execute(
        "UPDATE tuning_schedule_tasks SET title = ?, rate = ?, comment = ?, order_id = ? "
        "WHERE id = ? AND assignment_id IS NULL",
        (title, rate, comment, order_id, task_id),
    )
    db.commit()


def set_linked_task_comment(db, task_id, assignment_id, comment):
    """The comment of an order task lives on tuning_item_assignments (the
    board edits it there); the schedule row keeps a copy."""
    db.execute("UPDATE tuning_item_assignments SET comment = ? WHERE id = ?", (comment, assignment_id))
    db.execute("UPDATE tuning_schedule_tasks SET comment = ? WHERE id = ?", (comment, task_id))
    db.commit()


def set_assignment_due(db, assignment_id, due_from, due_to):
    db.execute(
        "UPDATE tuning_item_assignments SET due_from = ?, due_to = ? WHERE id = ?",
        (due_from, due_to, assignment_id),
    )
    db.commit()


def get_assignment(db, assignment_id):
    return db.execute(
        "SELECT * FROM tuning_item_assignments WHERE id = ?", (assignment_id,)
    ).fetchone()


def delete_task_reminders(db, task_id):
    db.execute("DELETE FROM tuning_task_reminders WHERE schedule_task_id = ?", (task_id,))


def list_other_day_tasks(db, day, task_id):
    """Placements on `day` that belong to other tasks than task_id."""
    return [row for row in list_day_tasks(db, day) if row["task_id"] != task_id]


def set_free_task_employee(db, task_id, employee_name):
    db.execute(
        "UPDATE tuning_schedule_tasks SET employee_name = ? WHERE id = ? AND assignment_id IS NULL",
        (employee_name, task_id),
    )
    db.commit()


def list_task_days(db, task_id):
    return db.execute(
        "SELECT id, work_date, start_time, planned_hours FROM tuning_schedule_task_days "
        "WHERE task_id = ? ORDER BY work_date, start_time",
        (task_id,),
    ).fetchall()


def get_task(db, task_id):
    return db.execute(
        "SELECT * FROM tuning_schedule_tasks WHERE id = ?", (task_id,)
    ).fetchone()


def create_task(db, assignment_id, employee_name, title, rate, comment, created_at, order_id=None):
    cur = db.execute(
        "INSERT INTO tuning_schedule_tasks "
        "(assignment_id, employee_name, title, rate, comment, created_at, order_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (assignment_id, employee_name, title, rate, comment, created_at, order_id),
    )
    db.commit()
    return cur.lastrowid


def add_task_day(db, task_id, work_date, start_time, planned_hours):
    """Places a task on a day, replacing that day's placement if the task
    already has one. Update-then-insert rather than an upsert clause: the
    production SQLite is too old for upserts (it fails with a syntax error
    "near ON")."""
    cursor = db.execute(
        "UPDATE tuning_schedule_task_days SET start_time = ?, planned_hours = ? "
        "WHERE task_id = ? AND work_date = ?",
        (start_time, planned_hours, task_id, work_date),
    )
    if cursor.rowcount == 0:
        db.execute(
            "INSERT INTO tuning_schedule_task_days (task_id, work_date, start_time, planned_hours) "
            "VALUES (?, ?, ?, ?)",
            (task_id, work_date, start_time, planned_hours),
        )
    db.commit()


def remove_task_day(db, task_id, day_id):
    db.execute(
        "DELETE FROM tuning_schedule_task_days WHERE id = ? AND task_id = ?",
        (day_id, task_id),
    )
    db.commit()


def set_task_status(db, task_id, status, responded_at):
    db.execute(
        "UPDATE tuning_schedule_tasks SET status = ?, responded_at = ? WHERE id = ?",
        (status, responded_at, task_id),
    )
    db.commit()


def set_task_entry(db, task_id, entry_id):
    db.execute(
        "UPDATE tuning_schedule_tasks SET entry_id = ? WHERE id = ?",
        (entry_id, task_id),
    )
    db.commit()


def list_linkable_orders(db):
    """Orders an admin could plausibly still assign work on, for the
    schedule's "привязать к заказу" picker — same statuses the tuning
    order board itself treats as active (not cancelled, not already
    handed over)."""
    return db.execute(
        "SELECT id, client_name, equipment_type, boat_model, motor_model "
        "FROM tuning_orders "
        "WHERE status NOT IN ('cancelled', 'handed_over') "
        "ORDER BY id DESC"
    ).fetchall()


def get_linkable_order(db, order_id):
    return db.execute(
        "SELECT id FROM tuning_orders WHERE id = ? AND status NOT IN ('cancelled', 'handed_over')",
        (order_id,),
    ).fetchone()


def get_order_item(db, order_id, item_id):
    return db.execute(
        "SELECT * FROM tuning_order_items WHERE id = ? AND order_id = ?",
        (item_id, order_id),
    ).fetchone()


def list_order_work_items(db, order_id):
    return db.execute(
        "SELECT id, work_name FROM tuning_order_items "
        "WHERE order_id = ? AND status != 'removed' ORDER BY id",
        (order_id,),
    ).fetchall()
