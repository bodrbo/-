"""SQL access for the tuning-center work schedule."""


def list_tuning_crew_employees(db):
    """Tuningmen eligible for the day roster — mirrors
    modules.schedule.repository.list_crew_employees, filtered to the
    tuning-center position instead of captain/guide. Position name kept in
    sync with app.py's TUNING_ASSIGNABLE_POSITIONS ("Тюнингмэн") by
    convention, the same way the excursion roster hardcodes its own
    position list rather than taking it as a parameter."""
    rows = db.execute(
        "SELECT employees.id, employees.name, employee_positions.position "
        "FROM employees JOIN employee_positions "
        "ON employee_positions.employee_id = employees.id "
        "WHERE employees.deleted_at IS NULL "
        "AND employee_positions.position = 'Тюнингмэн' "
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
            "WHERE d.work_date = ? AND employees.deleted_at IS NULL",
            (day,),
        ).fetchall()
    }


def add_day_crew_member(db, day, employee_id, timestamp):
    cursor = db.execute(
        "INSERT OR IGNORE INTO tuning_schedule_day_crew "
        "(work_date, employee_id, created_at) VALUES (?, ?, ?)",
        (day, employee_id, timestamp),
    )
    db.commit()
    return cursor.rowcount > 0


def remove_day_crew_member(db, day, employee_id):
    cursor = db.execute(
        "DELETE FROM tuning_schedule_day_crew WHERE work_date = ? AND employee_id = ?",
        (day, employee_id),
    )
    db.commit()
    return cursor.rowcount > 0


def list_day_tasks(db, day):
    """One row per task that has a placement on `day`, with that day's own
    planned_hours, plus (for a task linked to an order — assignment_id NOT
    NULL) the live status/rate/order context via tuning_item_assignments —
    those stay the single source of truth for a linked task rather than
    being copied onto tuning_schedule_tasks, so nothing here can drift out
    of sync with the order board. A free task (assignment_id IS NULL) uses
    its own employee_name/title/rate/status columns instead."""
    return db.execute(
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
        "LEFT JOIN tuning_orders o ON o.id = ti.order_id "
        "WHERE d.work_date = ? "
        "ORDER BY employee_name, t.id",
        (day,),
    ).fetchall()


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


def create_task(db, assignment_id, employee_name, title, rate, comment, created_at):
    cur = db.execute(
        "INSERT INTO tuning_schedule_tasks "
        "(assignment_id, employee_name, title, rate, comment, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (assignment_id, employee_name, title, rate, comment, created_at),
    )
    db.commit()
    return cur.lastrowid


def add_task_day(db, task_id, work_date, start_time, planned_hours):
    db.execute(
        "INSERT INTO tuning_schedule_task_days (task_id, work_date, start_time, planned_hours) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(task_id, work_date) DO UPDATE SET "
        "start_time = excluded.start_time, planned_hours = excluded.planned_hours",
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
