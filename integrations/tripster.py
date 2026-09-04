"""Small HTTP client for Tripster's guide orders API."""

from urllib.parse import urljoin, urlparse

import requests


API_URL = "https://experience.tripster.ru/api/guides/v1/orders/"


class TripsterAPIError(RuntimeError):
    """Raised when Tripster cannot return a usable orders response."""


def fetch_orders(token, updated_after=None, timeout=20):
    """Fetch every result page, optionally limited by Tripster update time."""
    clean_token = str(token or "").strip()
    if not clean_token:
        raise TripsterAPIError("Токен Tripster не настроен.")

    url = API_URL
    params = {"updated_after": updated_after} if updated_after else None
    headers = {
        "Authorization": f"Token {clean_token}",
        "Accept": "application/json",
    }
    orders = []
    seen_urls = set()
    while url:
        if url in seen_urls:
            raise TripsterAPIError("Tripster вернул циклическую пагинацию.")
        seen_urls.add(url)
        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=timeout
            )
        except requests.RequestException as error:
            raise TripsterAPIError(f"Ошибка соединения с Tripster: {error}") from error
        params = None
        if response.status_code in (401, 403):
            raise TripsterAPIError("Tripster отклонил API-токен.")
        if not response.ok:
            raise TripsterAPIError(
                f"Tripster вернул HTTP {response.status_code}."
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise TripsterAPIError("Tripster вернул ответ не в формате JSON.") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise TripsterAPIError("В ответе Tripster отсутствует список заказов.")
        orders.extend(
            order for order in payload["results"] if isinstance(order, dict)
        )
        next_url = payload.get("next")
        if next_url is not None and not isinstance(next_url, str):
            raise TripsterAPIError("Tripster вернул некорректную ссылку пагинации.")
        if next_url:
            next_url = urljoin(API_URL, next_url)
            parsed_next = urlparse(next_url)
            if (
                parsed_next.scheme != "https"
                or parsed_next.hostname != "experience.tripster.ru"
            ):
                raise TripsterAPIError(
                    "Tripster вернул небезопасную ссылку пагинации."
                )
        url = next_url
    return orders
