"""HTTP client for OpenWeather One Call API 4.0 (the "One Call by Call" product)."""

import requests as _requests

API_BASE = "https://api.openweathermap.org/data/4.0/onecall/timeline/1h"

# The 1h-step timeline returns up to 20 records per page; two pages comfortably
# cover the full 48h forecast horizon with one page to spare if OWM ever
# shortens the first page.
MAX_PAGES = 3


def fetch_hourly_forecast(api_key, lat, lon, requester=None, timeout=15):
    """Return the raw hourly forecast records for the next ~48h, newest-page
    pagination followed automatically. Raises requests.RequestException (or
    HTTPError via raise_for_status) on failure — callers decide how to
    degrade, this stays a thin, honest HTTP wrapper. The one exception is a
    401: OWM returns that for a brand-new key that hasn't activated yet
    *and* for a key without the separate "One Call by Call" subscription
    this specific product requires, so it's translated into a message that
    actually points at the fix instead of a bare status code."""
    requester = requester or _requests.get
    url = API_BASE
    params = {"lat": lat, "lon": lon, "units": "metric", "appid": api_key}
    hours = []
    for _ in range(MAX_PAGES):
        response = requester(url, params=params, timeout=timeout)
        try:
            response.raise_for_status()
        except _requests.HTTPError as error:
            if response.status_code == 401:
                raise RuntimeError(
                    "OpenWeather отклонил ключ (401). Возможные причины: ключ ещё "
                    "не активировался (это может занять до пары часов после "
                    "создания), либо на аккаунте не оформлена отдельная подписка "
                    "«One Call by Call» — обычный бесплатный ключ OpenWeather её "
                    "не включает."
                ) from error
            raise
        payload = response.json()
        hours.extend(payload.get("data") or [])
        next_url = payload.get("next")
        if not next_url:
            break
        url, params = next_url, None
    return hours
