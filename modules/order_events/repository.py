"""SQL access for the order events."""


def add_event(db, order_kind, order_id, kind, text, event_date, event_time, duration_minutes,
              host_employee, schedule_task_id, author_admin_id, created_at):
    return db.execute(
        "INSERT INTO order_events (order_kind, order_id, kind, text, event_date, event_time, "
        "duration_minutes, host_employee, schedule_task_id, author_admin_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order_kind, order_id, kind, text, event_date, event_time, duration_minutes,
         host_employee, schedule_task_id, author_admin_id, created_at),
    ).lastrowid


def list_events(db, order_kind, order_id):
    """Newest first by the day of the event (so planned visits stand on top)."""
    return db.execute(
        "SELECT e.*, a.admin_name AS author_name, "
        "EXISTS (SELECT 1 FROM tuning_schedule_tasks t WHERE t.id = e.schedule_task_id) AS task_exists "
        "FROM order_events e LEFT JOIN admin_accounts a ON a.id = e.author_admin_id "
        "WHERE e.order_kind = ? AND e.order_id = ? "
        "ORDER BY e.event_date DESC, e.event_time DESC, e.id DESC",
        (order_kind, order_id),
    ).fetchall()


def get_event(db, event_id):
    return db.execute("SELECT * FROM order_events WHERE id = ?", (event_id,)).fetchone()


def delete_event(db, event_id):
    db.execute("DELETE FROM order_events WHERE id = ?", (event_id,))


def list_order_events(db, order_kind, order_id):
    return db.execute(
        "SELECT * FROM order_events WHERE order_kind = ? AND order_id = ?", (order_kind, order_id)
    ).fetchall()


def add_event_task(db, event_id, schedule_task_id, created_at):
    db.execute(
        "INSERT INTO order_event_tasks (event_id, schedule_task_id, created_at) VALUES (?, ?, ?)",
        (event_id, schedule_task_id, created_at),
    )


def list_event_task_ids(db, event_id):
    return [
        row["schedule_task_id"] for row in db.execute(
            "SELECT schedule_task_id FROM order_event_tasks WHERE event_id = ? ORDER BY id", (event_id,)
        ).fetchall()
    ]


def delete_event_tasks(db, event_id):
    db.execute("DELETE FROM order_event_tasks WHERE event_id = ?", (event_id,))


def list_order_event_tasks(db, order_kind, order_id):
    """Every task made from the events of an order, with its first day in the
    schedule — tasks deleted from the schedule since are left out."""
    return db.execute(
        "SELECT et.event_id, t.id AS task_id, t.title, t.employee_name, t.status, "
        "(SELECT d.work_date FROM tuning_schedule_task_days d WHERE d.task_id = t.id "
        " ORDER BY d.work_date, d.start_time LIMIT 1) AS work_date, "
        "(SELECT d.start_time FROM tuning_schedule_task_days d WHERE d.task_id = t.id "
        " ORDER BY d.work_date, d.start_time LIMIT 1) AS start_time "
        "FROM order_event_tasks et "
        "JOIN order_events e ON e.id = et.event_id "
        "JOIN tuning_schedule_tasks t ON t.id = et.schedule_task_id "
        "WHERE e.order_kind = ? AND e.order_id = ? ORDER BY et.id",
        (order_kind, order_id),
    ).fetchall()


def list_task_reminders(db, task_ids):
    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    return db.execute(
        "SELECT r.schedule_task_id, r.remind_at, r.sent_at, "
        "COALESCE(e.name, a.admin_name) AS recipient_name "
        "FROM tuning_task_reminders r "
        "LEFT JOIN employees e ON e.id = r.remind_employee_id "
        "LEFT JOIN admin_accounts a ON a.id = r.remind_admin_id "
        f"WHERE r.schedule_task_id IN ({placeholders}) ORDER BY r.remind_at, r.id",
        list(task_ids),
    ).fetchall()
