"""SQLite schema for field diagnostic sheets."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS field_diagnostic_sheets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boat_profile_id INTEGER,
            boat_model TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            owner_phone TEXT NOT NULL,
            inspection_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'in_progress',
            created_by_name TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS field_diagnostic_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_id INTEGER NOT NULL,
            question_index INTEGER NOT NULL,
            section_name TEXT NOT NULL,
            question_title TEXT NOT NULL,
            question_text TEXT NOT NULL,
            status TEXT NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(sheet_id, question_index)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_field_diagnostic_answers_sheet "
        "ON field_diagnostic_answers (sheet_id, question_index)"
    )
    sheet_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(field_diagnostic_sheets)"
        ).fetchall()
    }
    if "other_completed_at" not in sheet_columns:
        conn.execute(
            "ALTER TABLE field_diagnostic_sheets ADD COLUMN other_completed_at TEXT"
        )
        # Sheets completed before the four-block workflow must remain closed.
        conn.execute(
            "UPDATE field_diagnostic_sheets SET other_completed_at = completed_at "
            "WHERE status = 'completed' AND completed_at IS NOT NULL"
        )
    if "question_set_json" not in sheet_columns:
        conn.execute(
            "ALTER TABLE field_diagnostic_sheets ADD COLUMN question_set_json TEXT"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS field_diagnostic_extra_defects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_field_diagnostic_extra_defects_sheet "
        "ON field_diagnostic_extra_defects (sheet_id, id)"
    )
