import re
import unittest
from unittest.mock import Mock, call, patch

from support import application_module
from integrations.tripster import API_URL, TripsterAPIError, fetch_orders
from modules.schedule import tripster_services


class TripsterClientTests(unittest.TestCase):
    @patch("integrations.tripster.requests.get")
    def test_fetches_all_pages_with_token_and_delta(self, get):
        first = Mock(
            ok=True,
            status_code=200,
            json=Mock(return_value={
                "results": [{"id": 1}],
                "next": API_URL + "?page=2",
            }),
        )
        second = Mock(
            ok=True,
            status_code=200,
            json=Mock(return_value={"results": [{"id": 2}], "next": None}),
        )
        get.side_effect = [first, second]

        result = fetch_orders("secret-token", updated_after="2026-09-01 10:00")

        self.assertEqual(result, [{"id": 1}, {"id": 2}])
        self.assertEqual(
            get.call_args_list,
            [
                call(
                    API_URL,
                    headers={
                        "Authorization": "Token secret-token",
                        "Accept": "application/json",
                    },
                    params={"updated_after": "2026-09-01 10:00"},
                    timeout=20,
                ),
                call(
                    API_URL + "?page=2",
                    headers={
                        "Authorization": "Token secret-token",
                        "Accept": "application/json",
                    },
                    params=None,
                    timeout=20,
                ),
            ],
        )

    @patch("integrations.tripster.requests.get")
    def test_rejects_invalid_token_response(self, get):
        get.return_value = Mock(ok=False, status_code=401)
        with self.assertRaisesRegex(TripsterAPIError, "отклонил API-токен"):
            fetch_orders("bad-token")

    @patch("integrations.tripster.requests.get")
    def test_never_forwards_token_to_foreign_pagination_host(self, get):
        get.return_value = Mock(
            ok=True,
            status_code=200,
            json=Mock(return_value={
                "results": [],
                "next": "https://example.org/collect-token",
            }),
        )
        with self.assertRaisesRegex(TripsterAPIError, "небезопасную ссылку"):
            fetch_orders("secret-token")
        self.assertEqual(get.call_count, 1)


class TripsterScheduleImportTests(unittest.TestCase):
    PHONES = ("+79995550101", "+79995550102")

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        self.previous_token = application_module.TRIPSTER_API_TOKEN
        application_module.TRIPSTER_API_TOKEN = "test-tripster-token"
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM tripster_sync_state")
            db.execute("DELETE FROM tripster_orders")
            db.execute("DELETE FROM tripster_travelers")
            db.execute("DELETE FROM schedule_day_crew")
            db.execute("DELETE FROM schedule_participants")
            db.execute("DELETE FROM schedule_assignments")
            db.execute("DELETE FROM schedule_items")
            placeholders = ",".join("?" for _phone in self.PHONES)
            db.execute(
                "DELETE FROM client_segments WHERE client_id IN "
                f"(SELECT id FROM clients WHERE phone IN ({placeholders}))",
                self.PHONES,
            )
            db.execute(
                f"DELETE FROM clients WHERE phone IN ({placeholders})",
                self.PHONES,
            )
            db.commit()

    def tearDown(self):
        application_module.TRIPSTER_API_TOKEN = self.previous_token

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def order(self, order_id=7001, **overrides):
        result = {
            "id": order_id,
            "status": "paid",
            "order_date": "2026-09-01T10:00:00+03:00",
            "experience_id": 321,
            "event": {
                "aware_start_dt": "2026-09-10T13:00:00+03:00",
                "date": "2026-09-10",
                "time": "13:00",
                "is_grouping_enabled": False,
            },
            "persons_count": 2,
            "traveler": {
                "id": order_id + 1000,
                "name": "Турист Tripster",
                "email": "tourist@example.com",
                "phone": self.PHONES[0],
            },
            "price": {
                "value": 12000,
                "currency": "RUB",
                "currency_rate": 1,
            },
            "url": f"https://experience.tripster.ru/experience/order/{order_id}/",
        }
        result.update(overrides)
        return result

    def sync(self, orders, follow_redirects=False):
        self.login()
        with patch.object(
            application_module, "fetch_tripster_orders", return_value=orders
        ) as fetcher:
            response = self.client.post(
                "/schedule/tripster/sync",
                data={"return_date": "2026-09-10", "return_employee": "all"},
                follow_redirects=follow_redirects,
            )
        return response, fetcher

    def test_utc_event_time_is_converted_to_fixed_moscow_time(self):
        order = self.order(event={
            "aware_start_dt": "2026-09-10T10:00:00Z",
            "date": "2026-09-10",
            "time": "10:00",
            "is_grouping_enabled": False,
        })

        normalised = tripster_services._normalise_order(order)

        self.assertEqual(normalised["event_start"], "2026-09-10 13:00")

    def test_paid_order_creates_unassigned_schedule_item_idempotently(self):
        order = self.order()
        first, first_fetcher = self.sync([order], follow_redirects=True)
        second, second_fetcher = self.sync([order])

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(first_fetcher.call_args.kwargs["updated_after"], None)
        self.assertRegex(
            second_fetcher.call_args.kwargs["updated_after"],
            re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$"),
        )
        html = first.get_data(as_text=True)
        self.assertIn("Не назначено", html)
        self.assertIn("Tripster · экскурсия #321", html)

        with application_module.app.app_context():
            db = application_module.get_db()
            items = db.execute("SELECT * FROM schedule_items").fetchall()
            participants = db.execute("SELECT * FROM schedule_participants").fetchall()
            segment = db.execute(
                "SELECT segment FROM client_segments WHERE client_id = ?",
                (participants[0]["client_id"],),
            ).fetchone()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "tripster")
        self.assertEqual(items[0]["source_ref"], "order:7001")
        self.assertEqual(items[0]["boat"], "Не назначен")
        self.assertEqual(items[0]["starts_at"], "2026-09-10 13:00")
        self.assertEqual(items[0]["ends_at"], "2026-09-10 14:00")
        self.assertEqual(items[0]["participants_count"], 2)
        self.assertEqual(items[0]["revenue"], 12000)
        self.assertEqual(len(participants), 1)
        self.assertEqual(participants[0]["source"], "tripster")
        self.assertEqual(segment["segment"], "excursion")

    def test_group_orders_at_same_time_merge_into_one_event(self):
        grouping_event = {
            "aware_start_dt": "2026-09-10T15:00:00+03:00",
            "date": "2026-09-10",
            "time": "15:00",
            "is_grouping_enabled": True,
        }
        first = self.order(
            7101,
            event=grouping_event,
            persons_count=3,
            traveler={
                "id": 8101,
                "name": "Первый турист",
                "email": "",
                "phone": self.PHONES[0],
            },
            price={"value": 9000, "currency": "RUB", "currency_rate": 1},
        )
        second = self.order(
            7102,
            event=grouping_event,
            persons_count=2,
            traveler={
                "id": 8102,
                "name": "Второй турист",
                "email": "",
                "phone": self.PHONES[1],
            },
            price={"value": 6000, "currency": "RUB", "currency_rate": 1},
        )

        self.sync([first, second])

        with application_module.app.app_context():
            db = application_module.get_db()
            items = db.execute("SELECT * FROM schedule_items").fetchall()
            participants = db.execute(
                "SELECT * FROM schedule_participants ORDER BY client_name"
            ).fetchall()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "event")
        self.assertEqual(items[0]["participants_count"], 5)
        self.assertEqual(items[0]["capacity"], 10)
        self.assertEqual(items[0]["revenue"], 15000)
        self.assertEqual(len(participants), 2)
        self.assertEqual(
            sorted(row["guests_count"] for row in participants), [2, 3]
        )

        cancelled_first = dict(first)
        cancelled_first["status"] = "cancelled"
        self.sync([cancelled_first])
        with application_module.app.app_context():
            db = application_module.get_db()
            remaining_item = db.execute("SELECT * FROM schedule_items").fetchone()
            remaining = db.execute(
                "SELECT * FROM schedule_participants"
            ).fetchall()
        self.assertIsNone(remaining_item["deleted_at"])
        self.assertEqual(remaining_item["participants_count"], 2)
        self.assertEqual(remaining_item["revenue"], 6000)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["client_name"], "Второй турист")

    def test_cancelled_order_removes_imported_trip(self):
        paid = self.order()
        self.sync([paid])
        cancelled = self.order(status="cancelled")

        self.sync([cancelled])

        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT deleted_at FROM schedule_items WHERE source_ref = 'order:7001'"
            ).fetchone()
        self.assertIsNotNone(row["deleted_at"])

    def test_manual_sync_requires_configured_token(self):
        self.login()
        application_module.TRIPSTER_API_TOKEN = ""
        with patch.object(application_module, "fetch_tripster_orders") as fetcher:
            response = self.client.post(
                "/schedule/tripster/sync",
                data={"return_date": "2026-09-10"},
                follow_redirects=True,
            )
        self.assertIn("Токен Tripster не настроен", response.get_data(as_text=True))
        fetcher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
