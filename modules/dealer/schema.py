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
            order_type TEXT NOT NULL DEFAULT 'equipment',
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
            position INTEGER NOT NULL DEFAULT 0,
            product_id INTEGER,
            unit TEXT NOT NULL DEFAULT 'piece'
        )
        """
    )
    # Databases created before the "Морские товары" subsection: orders there
    # were all boats and motors; a goods line names a warehouse product
    # (product_id, NULL = typed by hand) and its unit.
    order_cols = {row[1] for row in conn.execute("PRAGMA table_info(dealer_orders)").fetchall()}
    if "order_type" not in order_cols:
        conn.execute("ALTER TABLE dealer_orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'equipment'")
    item_cols = {row[1] for row in conn.execute("PRAGMA table_info(dealer_order_items)").fetchall()}
    if "product_id" not in item_cols:
        conn.execute("ALTER TABLE dealer_order_items ADD COLUMN product_id INTEGER")
    if "unit" not in item_cols:
        conn.execute("ALTER TABLE dealer_order_items ADD COLUMN unit TEXT NOT NULL DEFAULT 'piece'")
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
    # Positions the price list shows on top of those that stand in orders:
    # the ones added to it by hand (the price itself is in dealer_prices).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dealer_price_list (
            profile_id INTEGER PRIMARY KEY,
            added_at TEXT NOT NULL
        )
        """
    )
