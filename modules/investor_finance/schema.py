"""Database schema for investor terms, legacy ledger and payouts."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS investor_asset_profiles (
            boat TEXT PRIMARY KEY,
            investor_name TEXT NOT NULL,
            investment_date TEXT NOT NULL,
            investment_amount REAL NOT NULL,
            investor_share REAL NOT NULL,
            financial_year_days INTEGER NOT NULL DEFAULT 360,
            season_months INTEGER,
            manager_commission_note TEXT,
            history_through_date TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'legacy_xlsx',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS investor_ledger_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boat TEXT NOT NULL,
            investor_name TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            entry_kind TEXT NOT NULL CHECK (entry_kind IN ('income', 'expense')),
            description TEXT NOT NULL,
            amount REAL NOT NULL CHECK (amount >= 0),
            source_type TEXT NOT NULL DEFAULT 'legacy_xlsx',
            source_row INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_investor_ledger_boat_date "
        "ON investor_ledger_entries (boat, entry_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_investor_ledger_investor_date "
        "ON investor_ledger_entries (investor_name, entry_date)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS investor_distributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boat TEXT NOT NULL,
            investor_name TEXT NOT NULL,
            payout_date TEXT NOT NULL,
            amount REAL NOT NULL CHECK (amount >= 0),
            source_type TEXT NOT NULL DEFAULT 'legacy_xlsx',
            source_row INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_investor_distributions_investor_date "
        "ON investor_distributions (investor_name, payout_date)"
    )
