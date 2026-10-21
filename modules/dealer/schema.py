"""SQLite schema of the dealer center."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dealer_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER,
            client_name TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new_request',
            order_date TEXT NOT NULL,
            deadline_date TEXT,
            discount_type TEXT NOT NULL DEFAULT 'percent',
            discount_value REAL NOT NULL DEFAULT 0,
            subtotal REAL NOT NULL DEFAULT 0,
            total REAL NOT NULL DEFAULT 0,
            comment TEXT NOT NULL DEFAULT '',
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dealer_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            profile_id INTEGER,
            name TEXT NOT NULL,
            serial_number TEXT NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 1,
            price REAL NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dealer_order_items_order ON dealer_order_items (order_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dealer_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            paid_at TEXT NOT NULL,
            payment_type TEXT,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dealer_payments_order ON dealer_payments (order_id)"
    )
    # Selling price of a catalog position (the catalog itself has none).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dealer_prices (
            profile_id INTEGER PRIMARY KEY,
            price REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
