"""SQLite schema for the cached trip-weather forecast."""


def init_schema(conn):
    # Cached hourly forecast for the marina, keyed by the local calendar
    # hour it describes — one row per hour, upserted on every sync so a
    # trip card reads the cache instead of calling the API on page load.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_forecast_hours (
            local_hour TEXT PRIMARY KEY,
            dt_unix INTEGER NOT NULL,
            temp REAL,
            wind_speed REAL,
            wind_gust REAL,
            wind_deg REAL,
            precip_mm REAL NOT NULL DEFAULT 0,
            weather_id INTEGER,
            weather_main TEXT,
            fetched_at TEXT NOT NULL
        )
        """
    )
    # One row per (trip, event) — the same idempotent-delivery shape as
    # task_notification_deliveries, so a cron re-run never double-sends the
    # captain alert for a trip it already flagged.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_alert_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_item_id INTEGER NOT NULL,
            notification_event TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            delivery_status TEXT,
            UNIQUE(schedule_item_id, notification_event)
        )
        """
    )
