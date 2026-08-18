"""Small Telegram Bot API adapter used to discover personal chats."""

import datetime as dt

import requests


class TelegramAPIError(RuntimeError):
    """A recoverable Telegram API/configuration error for the admin UI."""


def fetch_recent_contacts(bot_token):
    """Return the latest private-chat update for each person who messaged the bot.

    The call deliberately does not advance Telegram's update offset: syncing the
    admin directory must not consume messages that another integration may need.
    """
    if not bot_token:
        raise TelegramAPIError("Telegram-бот не настроен: отсутствует TELEGRAM_BOT_TOKEN.")

    try:
        response = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getUpdates",
            timeout=10,
        )
    except requests.RequestException as error:
        raise TelegramAPIError(f"Не удалось связаться с Telegram: {error}") from error

    if not response.ok:
        raise TelegramAPIError(
            f"Telegram вернул ошибку {response.status_code}: {response.text[:300]}"
        )

    try:
        payload = response.json()
    except ValueError as error:
        raise TelegramAPIError("Telegram вернул некорректный ответ.") from error
    if not payload.get("ok", True):
        raise TelegramAPIError(payload.get("description") or "Telegram не выполнил запрос.")

    contacts = {}
    for update in payload.get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is None or chat.get("type") not in (None, "private"):
            continue

        chat_id = str(chat["id"])
        message_date = message.get("date")
        last_message_at = None
        if message_date:
            last_message_at = dt.datetime.fromtimestamp(
                message_date, tz=dt.timezone.utc
            ).strftime("%Y-%m-%d %H:%M")

        contacts[chat_id] = {
            "chat_id": chat_id,
            "username": chat.get("username") or "",
            "display_name": " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part
            ),
            "last_text": message.get("text") or "",
            "last_message_at": last_message_at,
            "update_id": update.get("update_id") or 0,
        }

    return sorted(contacts.values(), key=lambda item: item["update_id"], reverse=True)
