"""SQLite schema for the internal operational schedule."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            boat TEXT NOT NULL,
            service_name TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            capacity INTEGER,
            participants_count INTEGER NOT NULL DEFAULT 0,
            customer_name TEXT NOT NULL DEFAULT '',
            customer_phone TEXT NOT NULL DEFAULT '',
            revenue REAL NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'scheduled',
            source TEXT NOT NULL DEFAULT 'internal',
            accounting_trip_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    item_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(schedule_items)")
    }
    if "service_id" not in item_columns:
        conn.execute("ALTER TABLE schedule_items ADD COLUMN service_id INTEGER")
    conn.execute(
        "UPDATE schedule_items SET service_id = ("
        "SELECT excursion_services.id FROM excursion_services "
        "WHERE excursion_services.name = schedule_items.service_name COLLATE NOCASE"
        ") WHERE service_id IS NULL"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_item_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL,
            employee_name TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(schedule_item_id, employee_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_item_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            client_name TEXT NOT NULL,
            client_phone TEXT NOT NULL,
            guests_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(schedule_item_id, client_id)
        )
        """
    )
    participant_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(schedule_participants)")
    }
    if "guests_count" not in participant_columns:
        conn.execute(
            "ALTER TABLE schedule_participants "
            "ADD COLUMN guests_count INTEGER NOT NULL DEFAULT 1"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_day_crew (
            work_date TEXT NOT NULL,
            employee_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (work_date, employee_id)
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schedule_day_crew "
        "(work_date, employee_id, created_at) "
        "SELECT substr(schedule_items.starts_at, 1, 10), "
        "schedule_assignments.employee_id, schedule_items.created_at "
        "FROM schedule_items JOIN schedule_assignments "
        "ON schedule_assignments.schedule_item_id = schedule_items.id "
        "WHERE schedule_items.deleted_at IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_items_day "
        "ON schedule_items(starts_at, deleted_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_assignments_employee "
        "ON schedule_assignments(employee_id, schedule_item_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_participants_item "
        "ON schedule_participants(schedule_item_id, client_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_participants_client "
        "ON schedule_participants(client_id, schedule_item_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_day_crew_employee "
        "ON schedule_day_crew(employee_id, work_date)"
    )
