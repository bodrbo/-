"""SQLite schema for staff software improvement requests."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS software_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_type TEXT NOT NULL,
            author_admin_id INTEGER,
            author_employee_id INTEGER,
            author_name TEXT NOT NULL,
            description TEXT NOT NULL,
            page_path TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_software_requests_status_created "
        "ON software_requests(status, created_at DESC, id DESC)"
    )
