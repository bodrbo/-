import unittest
from unittest.mock import patch

from integrations.telegram import TelegramAPIError, fetch_recent_contacts


class FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class TelegramIntegrationTests(unittest.TestCase):
    def test_missing_token_is_reported_as_recoverable_error(self):
        with self.assertRaises(TelegramAPIError):
            fetch_recent_contacts("")

    @patch("integrations.telegram.requests.get")
    def test_recent_private_contacts_are_parsed_and_deduplicated(self, get):
        get.return_value = FakeResponse(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "date": 1_700_000_000,
                            "text": "/start",
                            "chat": {
                                "id": 42,
                                "type": "private",
                                "username": "captain",
                                "first_name": "Иван",
                            },
                        },
                    },
                    {
                        "update_id": 2,
                        "message": {
                            "date": 1_700_000_100,
                            "text": "Готово",
                            "chat": {
                                "id": 42,
                                "type": "private",
                                "username": "captain_new",
                                "first_name": "Иван",
                                "last_name": "Морской",
                            },
                        },
                    },
                    {
                        "update_id": 3,
                        "message": {
                            "text": "Сообщение группы",
                            "chat": {"id": -100, "type": "group", "title": "Команда"},
                        },
                    },
                ],
            }
        )

        contacts = fetch_recent_contacts("test-token")

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["chat_id"], "42")
        self.assertEqual(contacts[0]["username"], "captain_new")
        self.assertEqual(contacts[0]["display_name"], "Иван Морской")
        self.assertEqual(contacts[0]["last_text"], "Готово")
        get.assert_called_once_with(
            "https://api.telegram.org/bottest-token/getUpdates", timeout=10
        )


if __name__ == "__main__":
    unittest.main()
