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
    if "source_ref" not in item_columns:
        conn.execute("ALTER TABLE schedule_items ADD COLUMN source_ref TEXT")
    if "source_updated_at" not in item_columns:
        conn.execute("ALTER TABLE schedule_items ADD COLUMN source_updated_at TEXT")
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
            price REAL NOT NULL DEFAULT 0,
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
    if "price" not in participant_columns:
        conn.execute(
            "ALTER TABLE schedule_participants "
            "ADD COLUMN price REAL NOT NULL DEFAULT 0"
        )
        # Preserve the total of historical trips while moving their single
        # planned amount down to the client level. A client's share follows
        # the number of guests attached to that client.
        conn.execute(
            "UPDATE schedule_participants SET price = COALESCE(("
            "SELECT schedule_items.revenue * schedule_participants.guests_count "
            "/ NULLIF((SELECT SUM(other.guests_count) "
            "FROM schedule_participants AS other "
            "WHERE other.schedule_item_id = schedule_participants.schedule_item_id), 0) "
            "FROM schedule_items "
            "WHERE schedule_items.id = schedule_participants.schedule_item_id"
            "), 0)"
        )
    if "source" not in participant_columns:
        conn.execute(
            "ALTER TABLE schedule_participants "
            "ADD COLUMN source TEXT NOT NULL DEFAULT 'internal'"
        )
    if "source_ref" not in participant_columns:
        conn.execute(
            "ALTER TABLE schedule_participants ADD COLUMN source_ref TEXT"
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
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_schedule_items_source_ref "
        "ON schedule_items(source, source_ref) WHERE source_ref IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_schedule_participants_source_ref "
        "ON schedule_participants(source, source_ref) "
        "WHERE source_ref IS NOT NULL"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tripster_orders (
            order_id INTEGER PRIMARY KEY,
            experience_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            event_start TEXT,
            is_grouping_enabled INTEGER NOT NULL DEFAULT 0,
            persons_count INTEGER NOT NULL DEFAULT 0,
            traveler_id INTEGER,
            traveler_name TEXT NOT NULL DEFAULT '',
            traveler_phone TEXT NOT NULL DEFAULT '',
            traveler_email TEXT NOT NULL DEFAULT '',
            price_rub REAL NOT NULL DEFAULT 0,
            order_url TEXT NOT NULL DEFAULT '',
            raw_payload TEXT NOT NULL,
            schedule_item_id INTEGER,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tripster_orders_schedule_item "
        "ON tripster_orders(schedule_item_id, status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tripster_travelers (
            traveler_id INTEGER PRIMARY KEY,
            client_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tripster_sync_state (
            sync_key TEXT PRIMARY KEY,
            last_success_at TEXT NOT NULL
        )
        """
    )
