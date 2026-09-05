import datetime as dt
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
            db.execute(
                "DELETE FROM excursion_services "
                "WHERE name = 'Тестовая услуга Tripster'"
            )
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
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM excursion_services "
                "WHERE name = 'Тестовая услуга Tripster'"
            )
            db.commit()

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

    def add_mapping_service(self, tripster_id=321, hours=2.5):
        with application_module.app.app_context():
            db = application_module.get_db()
            cursor = db.execute(
                "INSERT INTO excursion_services "
                "(name, service_type, tripster_id, duration_hours, price, "
                "created_at, updated_at) VALUES (?, 'group', ?, ?, ?, ?, ?)",
                (
                    "Тестовая услуга Tripster", tripster_id, hours, 3000,
                    "2026-09-05 10:00", "2026-09-05 10:00",
                ),
            )
            db.commit()
            return cursor.lastrowid

    def test_utc_event_time_is_converted_to_fixed_moscow_time(self):
        order = self.order(event={
            "aware_start_dt": "2026-09-10T10:00:00Z",
            "date": "2026-09-10",
            "time": "10:00",
            "is_grouping_enabled": False,
        })

        normalised = tripster_services._normalise_order(order)

        self.assertEqual(normalised["event_start"], "2026-09-10 13:00")

    def test_ticket_breakdown_is_used_when_persons_count_is_missing(self):
        order = self.order(
            persons_count=None,
            price={
                "value": 9000,
                "currency": "RUB",
                "currency_rate": 1,
                "per_ticket": [
                    {"title": "Взрослый", "count": 2, "price": 6000},
                    {"title": "Детский", "count": 1, "price": 3000},
                ],
            },
        )

        normalised = tripster_services._normalise_order(order)

        self.assertEqual(normalised["persons_count"], 3)

    def test_tripster_id_maps_order_to_catalog_service_and_duration(self):
        service_id = self.add_mapping_service()

        self.sync([self.order()])

        with application_module.app.app_context():
            item = application_module.get_db().execute(
                "SELECT * FROM schedule_items WHERE source = 'tripster'"
            ).fetchone()
        self.assertEqual(item["service_id"], service_id)
        self.assertEqual(item["service_name"], "Тестовая услуга Tripster")
        self.assertEqual(item["ends_at"], "2026-09-10 15:30")
        self.assertEqual(item["kind"], "event")
        self.assertEqual(
            item["source_ref"], "event:321:2026-09-10 13:00"
        )

    def test_catalog_group_type_merges_orders_without_tripster_group_flag(self):
        service_id = self.add_mapping_service()
        first = self.order(
            7201,
            persons_count=2,
            traveler={
                "id": 8201,
                "name": "Первый турист",
                "email": "",
                "phone": self.PHONES[0],
            },
        )
        second = self.order(
            7202,
            persons_count=3,
            traveler={
                "id": 8202,
                "name": "Второй турист",
                "email": "",
                "phone": self.PHONES[1],
            },
        )

        self.sync([first, second])

        with application_module.app.app_context():
            db = application_module.get_db()
            items = db.execute(
                "SELECT * FROM schedule_items WHERE deleted_at IS NULL"
            ).fetchall()
            participants = db.execute(
                "SELECT * FROM schedule_participants"
            ).fetchall()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["service_id"], service_id)
        self.assertEqual(items[0]["kind"], "event")
        self.assertEqual(items[0]["participants_count"], 5)
        self.assertEqual(len(participants), 2)

    def test_repeat_import_maps_previously_unclassified_order(self):
        self.sync([self.order()])
        with application_module.app.app_context():
            before = application_module.get_db().execute(
                "SELECT service_id FROM schedule_items "
                "WHERE source_ref = 'order:7001'"
            ).fetchone()
        self.assertIsNone(before["service_id"])

        service_id = self.add_mapping_service()
        self.sync([])

        with application_module.app.app_context():
            item = application_module.get_db().execute(
                "SELECT * FROM schedule_items WHERE source_ref = 'order:7001'"
            ).fetchone()
        self.assertEqual(item["service_id"], service_id)
        self.assertEqual(item["service_name"], "Тестовая услуга Tripster")
        self.assertEqual(item["ends_at"], "2026-09-10 15:30")

    def test_repeat_import_preserves_manually_selected_service(self):
        self.sync([self.order()])
        with application_module.app.app_context():
            db = application_module.get_db()
            manual_service = db.execute(
                "SELECT id, name FROM excursion_services "
                "WHERE name = 'Средний тур'"
            ).fetchone()
            db.execute(
                "UPDATE schedule_items SET service_id = ?, service_name = ? "
                "WHERE source_ref = 'order:7001'",
                (manual_service["id"], manual_service["name"]),
            )
            db.commit()

        self.add_mapping_service()
        self.sync([])

        with application_module.app.app_context():
            item = application_module.get_db().execute(
                "SELECT service_id, service_name FROM schedule_items "
                "WHERE source_ref = 'order:7001'"
            ).fetchone()
        self.assertEqual(item["service_id"], manual_service["id"])
        self.assertEqual(item["service_name"], "Средний тур")

    def test_paid_order_creates_unassigned_schedule_item_idempotently(self):
        order = self.order()
        first, first_fetcher = self.sync([order], follow_redirects=True)
        second, second_fetcher = self.sync([order])

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(first_fetcher.call_args.kwargs["updated_after"], None)
        self.assertEqual(second_fetcher.call_args.kwargs["updated_after"], None)
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
            imported_client = db.execute(
                "SELECT acquisition_channel FROM clients WHERE id = ?",
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
        self.assertEqual(imported_client["acquisition_channel"], "tripster")

    def test_manual_full_sync_refreshes_guest_count(self):
        self.sync([self.order(persons_count=2)])
        response, fetcher = self.sync([self.order(persons_count=5)])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(fetcher.call_args.kwargs["updated_after"], None)
        with application_module.app.app_context():
            item = application_module.get_db().execute(
                "SELECT participants_count FROM schedule_items "
                "WHERE source_ref = 'order:7001'"
            ).fetchone()
        self.assertEqual(item["participants_count"], 5)

    def test_full_sync_updates_totals_and_keeps_tripster_payment_breakdown(self):
        grouping_event = {
            "aware_start_dt": "2026-09-05T13:00:00+03:00",
            "date": "2026-09-05",
            "time": "13:00",
            "is_grouping_enabled": True,
        }
        first = self.order(
            7301,
            event=grouping_event,
            persons_count=2,
            traveler={
                "id": 8301,
                "name": "Группа из двух гостей",
                "email": "",
                "phone": self.PHONES[0],
            },
            price={"value": 6000, "currency": "RUB", "currency_rate": 1},
        )
        second = self.order(
            7302,
            event=grouping_event,
            persons_count=3,
            traveler={
                "id": 8302,
                "name": "Группа из трёх гостей",
                "email": "",
                "phone": self.PHONES[1],
            },
            price={"value": 6000, "currency": "RUB", "currency_rate": 1},
        )
        self.sync([first, second])

        first["price"] = {
            "value": 11100,
            "pre_pay": 0,
            "payment_to_guide": 11100,
            "currency": "RUB",
            "currency_rate": 1,
            "per_ticket": [
                {"title": "Стандартный билет", "count": 2, "price": 8300}
            ],
        }
        second["price"] = {
            "value": 11100,
            # The API can expose a non-reconciling commission-adjusted pre_pay.
            # The customer-facing prepayment is total minus payment_to_guide.
            "pre_pay": 2966.5,
            "payment_to_guide": 8300,
            "currency": "RUB",
            "currency_rate": 1,
            "per_ticket": [
                {"title": "Стандартный билет", "count": 3, "price": 8300}
            ],
        }
        self.sync([first, second])

        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute(
                "SELECT participants_count, revenue FROM schedule_items "
                "WHERE source_ref = 'event:321:2026-09-05 13:00'"
            ).fetchone()
            participants = db.execute(
                "SELECT client_name, guests_count, price, prepayment, payment_due "
                "FROM schedule_participants ORDER BY guests_count"
            ).fetchall()
        self.assertEqual(item["participants_count"], 5)
        self.assertEqual(item["revenue"], 22200)
        self.assertEqual(
            [
                (
                    row["client_name"], row["guests_count"], row["price"],
                    row["prepayment"], row["payment_due"],
                )
                for row in participants
            ],
            [
                ("Группа из двух гостей", 2, 11100, 0, 11100),
                ("Группа из трёх гостей", 3, 11100, 2800, 8300),
            ],
        )
        self.login()
        page = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertIn("Общая сумма заказа", page)
        self.assertIn("Предоплата на Tripster", page)
        self.assertIn("Сумма к доплате", page)
        self.assertIn('"prepayment": 2800.0', page)
        self.assertIn('"payment_due": 8300.0', page)

    def test_price_falls_back_to_prepay_and_payment_to_guide(self):
        order = self.order(price={
            "pre_pay": 2000,
            "payment_to_guide": 7000,
            "currency": "RUB",
            "currency_rate": 1,
        })

        normalised = tripster_services._normalise_order(order)

        self.assertEqual(normalised["price_rub"], 9000)
        self.assertEqual(normalised["prepayment_rub"], 2000)
        self.assertEqual(normalised["payment_due_rub"], 7000)

    def test_official_total_has_priority_over_ticket_breakdown(self):
        normalised = tripster_services._normalise_order(self.order(price={
            "value": 11100,
            "pre_pay": 2800,
            "payment_to_guide": 8300,
            "per_ticket": [
                {"title": "Стандартный билет", "count": 3, "price": 8300}
            ],
            "currency": "RUB",
            "currency_rate": 1,
        }))

        self.assertEqual(normalised["price_rub"], 11100)
        self.assertEqual(normalised["prepayment_rub"], 2800)
        self.assertEqual(normalised["payment_due_rub"], 8300)

    def test_prepayment_reconciles_total_with_amount_due(self):
        normalised = tripster_services._normalise_order(self.order(price={
            "value": 11100,
            "pre_pay": 2966.5,
            "payment_to_guide": 8300,
            "currency": "RUB",
            "currency_rate": 1,
        }))

        self.assertEqual(normalised["price_rub"], 11100)
        self.assertEqual(normalised["prepayment_rub"], 2800)
        self.assertEqual(normalised["payment_due_rub"], 8300)

    def test_incremental_sync_keeps_updated_after_cursor_for_cron(self):
        first_fetcher = Mock(return_value=[])
        second_fetcher = Mock(return_value=[])
        with application_module.app.app_context():
            db = application_module.get_db()
            tripster_services.sync_orders(
                db, first_fetcher, now=dt.datetime(2026, 9, 5, 12, 0)
            )
            tripster_services.sync_orders(
                db, second_fetcher, now=dt.datetime(2026, 9, 5, 13, 0)
            )
        first_fetcher.assert_called_once_with(updated_after=None)
        second_fetcher.assert_called_once_with(
            updated_after="2026-09-05 11:50"
        )

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
