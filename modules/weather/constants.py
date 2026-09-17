"""Thresholds and lookup tables for the trip weather forecast."""

# Any forecast gust (or sustained wind, where gust isn't reported) above
# this is a bad-weather trigger for the captain alert.
WIND_GUST_ALERT_MS = 10.0

# OpenWeather condition ids 200-299 are all thunderstorm variants.
THUNDERSTORM_ID_MIN = 200
THUNDERSTORM_ID_MAX = 300

# How far ahead of "now" a trip is still worth alerting captains about —
# matches the API's own 48h hourly forecast horizon, so nothing beyond this
# window has forecast data to evaluate anyway.
ALERT_LOOKAHEAD_HOURS = 48

# How long a synced hour is kept in the cache after it's passed — a small
# buffer, not zero, so a trip that started slightly before "now" still has
# its departure hour on hand if the page is refreshed mid-trip.
CACHE_RETENTION_HOURS = 6

COMPASS_POINTS = (
    "С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ",
)

# Bundled alongside the other Telegram photo assets — same fire-and-forget
# static-asset convention, see app.py's supply-delivered / software-request
# "done" photo notices.
BAD_WEATHER_PHOTO_FILENAME = "weather-bad.jpg"
