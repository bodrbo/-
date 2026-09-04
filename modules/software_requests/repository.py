"""Persistence helpers for software improvement requests."""


def create_request(
    db,
    *,
    author_type,
    author_admin_id,
    author_employee_id,
    author_name,
    description,
    page_path,
    timestamp,
):
    cursor = db.execute(
        "INSERT INTO software_requests "
        "(author_type, author_admin_id, author_employee_id, author_name, "
        "description, page_path, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)",
        (
            author_type,
            author_admin_id,
            author_employee_id,
            author_name,
            description,
            page_path or None,
            timestamp,
            timestamp,
        ),
    )
    db.commit()
    return cursor.lastrowid


def list_requests(db, status=None):
    query = "SELECT * FROM software_requests"
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += (
        " ORDER BY CASE status WHEN 'new' THEN 0 WHEN 'in_progress' THEN 1 "
        "ELSE 2 END, created_at DESC, id DESC"
    )
    return db.execute(query, params).fetchall()


def status_counts(db):
    return {
        row["status"]: row["total"]
        for row in db.execute(
            "SELECT status, COUNT(*) AS total FROM software_requests GROUP BY status"
        ).fetchall()
    }


def update_status(db, request_id, status, timestamp):
    cursor = db.execute(
        "UPDATE software_requests SET status = ?, updated_at = ? WHERE id = ?",
        (status, timestamp, request_id),
    )
    db.commit()
    return cursor.rowcount > 0
