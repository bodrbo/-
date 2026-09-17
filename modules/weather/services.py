"""Bad-weather evaluation, trip-card display data, and the captain alert."""

import datetime as dt
import html

from modules.notifications import EVENT_SCHEDULE_BAD_WEATHER

from . import client, repository
from .constants import (
    ALERT_LOOKAHEAD_HOURS,
    CACHE_RETENTION_HOURS,
    COMPASS_POINTS,
    THUNDERSTORM_ID_MAX,
    THUNDERSTORM_ID_MIN,
    WIND_GUST_ALERT_MS,
)


def _floor_hour(value):
    return value.replace(minute=0, second=0, microsecond=0)


def sync_forecast(api_key, lat, lon, db, requester=None, now=None):
    """Fetch the latest hourly forecast and refresh the local cache. Lets
    requests.RequestException propagate — the caller (a cron route) decides
    how to report a fetch failure; a stale cache is still usable meanwhile."""
    now = now or dt.datetime.now()
    hours = client.fetch_hourly_forecast(api_key, lat, lon, requester=requester)
    fetched_at = now.strftime("%Y-%m-%d %H:%M")
    repository.upsert_forecast_hours(db, hours, fetched_at)
    cutoff = (now - dt.timedelta(hours=CACHE_RETENTION_HOURS)).strftime("%Y-%m-%d %H:00")
    repository.prune_stale_hours(db, cutoff)
    return len(hours)


def _compass(deg):
    if deg is None:
        return None
    index = int((deg / 45.0) + 0.5) % 8
    return COMPASS_POINTS[index]


def evaluate_trip_weather(db, starts_at, ends_at):
    """None if the trip is outside the cached forecast horizon (nothing to
    show/check yet); otherwise a verdict covering every cached hour the
    trip spans, not just its departure hour — a squall in hour two of a
    three-hour trip still deserves a warning."""
    start_dt = dt.datetime.strptime(starts_at, "%Y-%m-%d %H:%M")
    end_dt = dt.datetime.strptime(ends_at, "%Y-%m-%d %H:%M")
    window_start = _floor_hour(start_dt).strftime("%Y-%m-%d %H:%M")
    window_end = _floor_hour(end_dt).strftime("%Y-%m-%d %H:%M")
    rows = repository.forecast_for_window(db, window_start, window_end)
    if not rows:
        return None

    departure = rows[0]
    worst_gust = 0.0
    worst_gust_dir = None
    max_precip_mm = 0.0
    is_storm = False
    for row in rows:
        gust = row["wind_gust"] if row["wind_gust"] is not None else row["wind_speed"]
        gust = gust or 0.0
        if gust > worst_gust:
            worst_gust = gust
            worst_gust_dir = row["wind_deg"]
        max_precip_mm = max(max_precip_mm, row["precip_mm"] or 0.0)
        weather_id = row["weather_id"]
        if weather_id is not None and THUNDERSTORM_ID_MIN <= weather_id < THUNDERSTORM_ID_MAX:
            is_storm = True

    reasons = []
    if worst_gust > WIND_GUST_ALERT_MS:
        direction = _compass(worst_gust_dir)
        wind_reason = f"ветер до {worst_gust:.0f} м/с"
        if direction:
            wind_reason += f", направление {direction}"
        reasons.append(wind_reason)
    if max_precip_mm > 0:
        reasons.append("осадки")
    if is_storm:
        reasons.append("гроза")

    return {
        "is_bad": bool(reasons),
        "reasons": reasons,
        "departure": departure,
        "worst_gust": worst_gust,
        "worst_gust_dir": worst_gust_dir,
        "max_precip_mm": max_precip_mm,
    }


def _display_payload(verdict):
    # Temperature and direction come from the departure hour (no meaningful
    # "worst" for either); wind and precipitation instead reflect the worst
    # point in the trip's span, so a card flagged bad always shows a number
    # that explains why — not a calm departure-hour reading next to a red
    # border with the actual cause an hour later.
    row = verdict["departure"]
    return {
        "temp": row["temp"],
        "wind_speed": row["wind_speed"],
        "wind_gust": verdict["worst_gust"],
        "wind_dir": _compass(row["wind_deg"]),
        "precip_mm": verdict["max_precip_mm"],
        "weather_main": row["weather_main"],
        "is_bad": verdict["is_bad"],
        "reasons": verdict["reasons"],
    }


def attach_forecast(db, items):
    """Mutates each item in place, adding item['weather'] (a display dict)
    or None when the trip falls outside the cached 48h horizon."""
    for item in items:
        verdict = evaluate_trip_weather(db, item["starts_at"], item["ends_at"])
        item["weather"] = _display_payload(verdict) if verdict else None


def _alert_text(trip, verdict):
    start_dt = dt.datetime.strptime(trip["starts_at"], "%Y-%m-%d %H:%M")
    end_dt = dt.datetime.strptime(trip["ends_at"], "%Y-%m-%d %H:%M")
    interval = f"{start_dt:%d.%m %H:%M}–{end_dt:%H:%M}"
    reasons_text = ", ".join(verdict["reasons"])
    service_name = html.escape(trip["service_name"] or "—")
    boat = html.escape(trip["boat"] or "—")
    return (
        f"⛈ <b>Неблагоприятный прогноз на рейс</b>\n"
        f"Рейс: <b>{service_name}</b>\n"
        f"Когда: {interval}\n"
        f"Судно: {boat}\n"
        f"Прогноз: {html.escape(reasons_text)}\n\n"
        f"Оцените обстановку перед выходом."
    )


def send_weather_alerts(
    db, employee_sender, employee_photo_sender=None, photo_path=None, now=None
):
    """Idempotent per trip: one delivery row per schedule_item_id blocks a
    re-run (or a later cron tick after the forecast worsens further) from
    re-sending the same warning to the same captains.

    Sent as a single photo-with-caption message (not text then a separate
    photo) when both employee_photo_sender and an existing photo_path are
    given — same one-message convention as the other Telegram photo
    notices in this app; falls back to plain text otherwise."""
    now = now or dt.datetime.now()
    attempted_at = now.strftime("%Y-%m-%d %H:%M")
    trips = repository.list_upcoming_captain_trips(db, now, ALERT_LOOKAHEAD_HOURS)
    send_as_photo = employee_photo_sender is not None and photo_path is not None
    sent = 0
    for trip in trips:
        if repository.alert_already_sent(db, trip["id"], EVENT_SCHEDULE_BAD_WEATHER):
            continue
        verdict = evaluate_trip_weather(db, trip["starts_at"], trip["ends_at"])
        if verdict is None or not verdict["is_bad"]:
            continue
        text = _alert_text(trip, verdict)
        statuses = []
        for captain in trip["captains"]:
            if send_as_photo:
                status = employee_photo_sender(
                    db, captain["employee_name"], photo_path, caption=text
                )
            else:
                status = employee_sender(db, captain["employee_name"], text)
            statuses.append(status)
        repository.record_alert(
            db, trip["id"], EVENT_SCHEDULE_BAD_WEATHER, attempted_at, statuses
        )
        sent += 1
    return {"alerts_sent": sent}
