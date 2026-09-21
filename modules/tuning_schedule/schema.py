"""SQLite schema for the tuning-center work schedule — day-based roster and
task placement, alongside (not instead of) the excursion schedule's
schema.py. Deliberately separate tables from schedule_day_crew /
tuning_item_assignments — see modules/tuning_schedule/repository.py and
services.py for why."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_schedule_day_crew (
            work_date TEXT NOT NULL,
            employee_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (work_date, employee_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_schedule_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id INTEGER,
            employee_name TEXT NOT NULL,
            title TEXT NOT NULL,
            rate REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            comment TEXT NOT NULL DEFAULT '',
            entry_id INTEGER,
            created_at TEXT NOT NULL,
            responded_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_schedule_task_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            planned_hours REAL NOT NULL,
            UNIQUE(task_id, work_date)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tuning_schedule_task_days_date "
        "ON tuning_schedule_task_days(work_date, task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tuning_schedule_task_days_task "
        "ON tuning_schedule_task_days(task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tuning_schedule_tasks_assignment "
        "ON tuning_schedule_tasks(assignment_id)"
    )
