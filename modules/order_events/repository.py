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
