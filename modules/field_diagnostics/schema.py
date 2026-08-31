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

