"""SQL for the cached forecast and the captain-alert delivery log."""

import datetime as dt


def upsert_forecast_hours(db, hours, fetched_at):
    """Cache the API's raw hourly records, keyed by local calendar hour.

    OWM's dt is Unix UTC; the rest of this app stores every other
    timestamp as naive local server time (see schedule_items.starts_at),
    so fromtimestamp() here keeps this cache directly comparable to those
    columns without a timezone library anywhere in the codebase.

    Plain select-then-insert/update rather than an upsert (``INSERT ...
    ON CONFLICT``) — this hosting has a documented history of that syntax
    misbehaving in production (see modules/settings/repository.py), so the
    codebase avoids it everywhere.
    """
    for record in hours:
        dt_unix = record.get("dt")
        if dt_unix is None:
            continue
        local_hour = dt.datetime.fromtimestamp(dt_unix).strftime("%Y-%m-%d %H:00")
        rain = (record.get("rain") or {}).get("1h") or 0
        snow = (record.get("snow") or {}).get("1h") or 0
        weather = (record.get("weather") or [{}])[0]
        values = (
            dt_unix, record.get("temp"), record.get("wind_speed"),
            record.get("wind_gust"), record.get("wind_deg"), rain + snow,
            weather.get("id"), weather.get("main"), fetched_at,
        )
        existing = db.execute(
            "SELECT 1 FROM weather_forecast_hours WHERE local_hour = ?",
            (local_hour,),
        ).fetchone()
        if existing is not None:
            db.execute(
                "UPDATE weather_forecast_hours SET "
                "dt_unix = ?, temp = ?, wind_speed = ?, wind_gust = ?, wind_deg = ?, "
                "precip_mm = ?, weather_id = ?, weather_main = ?, fetched_at = ? "
                "WHERE local_hour = ?",
                values + (local_hour,),
            )
        else:
            db.execute(
                "INSERT INTO weather_forecast_hours "
                "(dt_unix, temp, wind_speed, wind_gust, wind_deg, "
                "precip_mm, weather_id, weather_main, fetched_at, local_hour) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values + (local_hour,),
            )
    db.commit()


def prune_stale_hours(db, before_local_hour):
    db.execute(
        "DELETE FROM weather_forecast_hours WHERE local_hour < ?",
        (before_local_hour,),
    )
    db.commit()


def forecast_for_window(db, start_local_hour, end_local_hour):
    return db.execute(
        "SELECT * FROM weather_forecast_hours "
        "WHERE local_hour >= ? AND local_hour <= ? ORDER BY local_hour",
        (start_local_hour, end_local_hour),
    ).fetchall()


def list_upcoming_captain_trips(db, now, horizon_hours):
    """Not-deleted trips starting within horizon_hours that have a captain
    or guide-captain assigned — the only rows the alert cron needs to look
    at. Trips with no captain-role crew are skipped: there's no one to
    warn yet, and re-checking them every run would be wasted work."""
    start = now.strftime("%Y-%m-%d %H:%M")
    end = (now + dt.timedelta(hours=horizon_hours)).strftime("%Y-%m-%d %H:%M")
    trips = db.execute(
        "SELECT id, service_name, boat, starts_at, ends_at FROM schedule_items "
        "WHERE deleted_at IS NULL AND starts_at >= ? AND starts_at <= ? "
        "ORDER BY starts_at, id",
        (start, end),
    ).fetchall()
    if not trips:
        return []
    ids = [trip["id"] for trip in trips]
    placeholders = ",".join("?" for _ in ids)
    assignments = db.execute(
        "SELECT schedule_item_id, employee_name FROM schedule_assignments "
        f"WHERE schedule_item_id IN ({placeholders}) "
        "AND role IN ('captain', 'guide_captain') "
        "ORDER BY schedule_item_id, id",
        tuple(ids),
    ).fetchall()
    captains_by_item = {}
    for row in assignments:
        captains_by_item.setdefault(row["schedule_item_id"], []).append(dict(row))
    result = []
    for trip in trips:
        captains = captains_by_item.get(trip["id"])
        if not captains:
            continue
        row = dict(trip)
        row["captains"] = captains
        result.append(row)
    return result


def alert_already_sent(db, schedule_item_id, event):
    return db.execute(
        "SELECT 1 FROM weather_alert_deliveries "
        "WHERE schedule_item_id = ? AND notification_event = ?",
        (schedule_item_id, event),
    ).fetchone() is not None


def record_alert(db, schedule_item_id, event, attempted_at, status):
    db.execute(
        "INSERT OR IGNORE INTO weather_alert_deliveries "
        "(schedule_item_id, notification_event, attempted_at, delivery_status) "
        "VALUES (?, ?, ?, ?)",
        (schedule_item_id, event, attempted_at, str(status)),
    )
    db.commit()
