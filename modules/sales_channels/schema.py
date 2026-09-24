"""SQLite schema for persistent, user-created sales channels."""


BUILTIN_CHANNELS = (
    ("direct", "Напрямую", 10),
    ("tripster", "Трипстер", 20),
    ("sputnik", "Спутник", 30),
    ("bodrbo_tuning", "Сайт bodrbo-tuning.ru", 40),
    ("bodrbo_fort", "Сайт bodrbo-fort.ru", 50),
    ("aggregator", "Через агрегатора/агента", 60),
    ("mixed", "Смешанно / другое", 70),
)


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'custom',
            sort_order INTEGER NOT NULL DEFAULT 100,
            created_at TEXT NOT NULL
        )
        """
    )
    for code, name, sort_order in BUILTIN_CHANNELS:
        conn.execute(
            "INSERT OR IGNORE INTO sales_channels "
            "(code, name, kind, sort_order, created_at) "
            "VALUES (?, ?, 'builtin', ?, '2026-09-24 00:00')",
            (code, name, sort_order),
        )
        conn.execute(
            "UPDATE sales_channels SET name = ?, sort_order = ? "
            "WHERE code = ? AND kind = 'builtin'",
            (name, sort_order, code),
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_channels_order "
        "ON sales_channels(sort_order, name)"
    )
