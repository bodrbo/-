import unittest
from unittest import mock

from modules.schedule import repository, services
from support import application_module


def fake_phone_normalizer(phone):
    digits = "".join(c for c in phone if c.isdigit())
    return digits


class ScheduleYookassaPaymentTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM client_segments WHERE client_id IN "
                "(SELECT id FROM clients WHERE phone = '+79998880009')"
            )
            db.execute("DELETE FROM clients WHERE phone = '+79998880009'")
            cursor = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
                "VALUES ('Тестовый клиент', '', '+79998880009', "
                "'schedule-yookassa-test-client', '2026-09-01 09:00')"
            )
            self.client_id = cursor.lastrowid
            item_cursor = db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_name, starts_at, ends_at, capacity, "
                "participants_count, revenue, created_at, updated_at) "
                "VALUES ('event', 'Ларус', 'Средний тур', '2026-09-10 10:00', "
                "'2026-09-10 12:00', 10, 1, 9000, '2026-09-01 09:00', '2026-09-01 09:00')"
            )
            self.item_id = item_cursor.lastrowid
            participant_cursor = db.execute(
                "INSERT INTO schedule_participants "
                "(schedule_item_id, client_id, client_name, client_phone, guests_count, "
                "price, prepayment, payment_due, created_at, source) "
                "VALUES (?, ?, 'Тестовый клиент', '+79998880009', 2, 9000, 0, 9000, "
                "'2026-09-01 09:00', 'internal')",
                (self.item_id, self.client_id),
            )
            self.participant_id = participant_cursor.lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM schedule_yookassa_payments WHERE schedule_item_id = ?",
                (self.item_id,),
            )
            db.execute("DELETE FROM schedule_participants WHERE id = ?", (self.participant_id,))
            db.execute("DELETE FROM schedule_items WHERE id = ?", (self.item_id,))
            db.execute(
                "DELETE FROM client_segments WHERE client_id = ?", (self.client_id,)
            )
            db.execute("DELETE FROM clients WHERE id = ?", (self.client_id,))
            db.commit()

    def remote_payment(self, payment_id="pay-schedule-1", status="pending"):
        return {
            "id": payment_id,
            "status": status,
            "amount": {"value": "4500.00", "currency": "RUB"},
            "confirmation": {"confirmation_url": "https://yookassa.ru/checkout/pay-schedule-1"},
        }

    def test_create_participant_payment_inserts_row_with_full_receipt(self):
        posts = []

        def api(method, path, json_body=None, idempotence_key=None, **kwargs):
            posts.append((method, path, json_body, idempotence_key))
            return self.remote_payment()

        with application_module.app.app_context():
            db = application_module.get_db()
            success, message, payment_id = services.create_participant_payment(
                db, self.item_id, self.participant_id, "4500",
                api, 7, fake_phone_normalizer, "https://example.test/",
            )
            row = db.execute(
                "SELECT * FROM schedule_yookassa_payments WHERE id = ?", (payment_id,)
            ).fetchone()

            self.assertTrue(success, message)
            self.assertEqual(row["amount"], 4500.0)
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["confirmation_url"], "https://yookassa.ru/checkout/pay-schedule-1")
            self.assertEqual(len(posts), 1)
            body = posts[0][2]
            self.assertEqual(body["amount"]["value"], "4500.00")
            self.assertEqual(body["receipt"]["customer"]["phone"], "79998880009")
            item = body["receipt"]["items"][0]
            self.assertEqual(item["payment_subject"], "service")
            self.assertEqual(item["payment_mode"], "full_payment")
            self.assertEqual(item["vat_code"], 7)

            participants = repository.list_item_participants_with_addons(db, self.item_id)
            self.assertEqual(len(participants[0]["payments"]), 1)
            self.assertEqual(participants[0]["payments"][0]["id"], payment_id)

    def test_create_participant_payment_without_phone_omits_receipt_customer(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE schedule_participants SET client_phone = '' WHERE id = ?",
                (self.participant_id,),
            )
            db.commit()
            posts = []

            def api(method, path, json_body=None, idempotence_key=None, **kwargs):
                posts.append(json_body)
                return self.remote_payment()

            success, message, _payment_id = services.create_participant_payment(
                db, self.item_id, self.participant_id, "1000",
                api, 7, fake_phone_normalizer, "https://example.test/",
            )
            self.assertTrue(success, message)
            self.assertNotIn("customer", posts[0]["receipt"])

    def test_create_participant_payment_rejects_non_positive_amount(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            success, message, payment_id = services.create_participant_payment(
                db, self.item_id, self.participant_id, "0",
                lambda *a, **k: {}, 7, fake_phone_normalizer, "https://example.test/",
            )
            self.assertFalse(success)
            self.assertIsNone(payment_id)
            self.assertIn("больше нуля", message)

    def test_sync_participant_payment_applies_paid_online_exactly_once(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            success, _message, payment_id = services.create_participant_payment(
                db, self.item_id, self.participant_id, "4500",
                lambda *a, **k: self.remote_payment(), 7, fake_phone_normalizer,
                "https://example.test/",
            )
            self.assertTrue(success)

            def succeeded_api(method, path, **kwargs):
                return self.remote_payment(status="succeeded")

            record = db.execute(
                "SELECT * FROM schedule_yookassa_payments WHERE id = ?", (payment_id,)
            ).fetchone()
            services.sync_participant_payment(db, record, succeeded_api)
            record = db.execute(
                "SELECT * FROM schedule_yookassa_payments WHERE id = ?", (payment_id,)
            ).fetchone()
            services.sync_participant_payment(db, record, succeeded_api)

            participant = db.execute(
                "SELECT paid_online FROM schedule_participants WHERE id = ?",
                (self.participant_id,),
            ).fetchone()
            self.assertEqual(participant["paid_online"], 4500.0)

    def test_delete_participant_payment_refuses_succeeded_allows_pending(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            _success, _message, payment_id = services.create_participant_payment(
                db, self.item_id, self.participant_id, "1500",
                lambda *a, **k: self.remote_payment(payment_id="pay-schedule-2"), 7,
                fake_phone_normalizer, "https://example.test/",
            )
            record = db.execute(
                "SELECT * FROM schedule_yookassa_payments WHERE id = ?", (payment_id,)
            ).fetchone()

            ok, message = services.delete_participant_payment(
                db, record, lambda *a, **k: {}
            )
            self.assertTrue(ok, message)
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM schedule_yookassa_payments WHERE id = ?", (payment_id,)
                ).fetchone()
            )

            _success, _message, payment_id = services.create_participant_payment(
                db, self.item_id, self.participant_id, "1500",
                lambda *a, **k: self.remote_payment(
                    payment_id="pay-schedule-3", status="succeeded"
                ),
                7, fake_phone_normalizer, "https://example.test/",
            )
            record = db.execute(
                "SELECT * FROM schedule_yookassa_payments WHERE id = ?", (payment_id,)
            ).fetchone()
            services.sync_participant_payment(
                db, record, lambda *a, **k: self.remote_payment(
                    payment_id="pay-schedule-3", status="succeeded"
                )
            )
            record = db.execute(
                "SELECT * FROM schedule_yookassa_payments WHERE id = ?", (payment_id,)
            ).fetchone()
            ok, message = services.delete_participant_payment(
                db, record, lambda *a, **k: {}
            )
            self.assertFalse(ok)
            self.assertIn("успешной оплатой", message)

    def test_webhook_applies_schedule_payment(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            _success, _message, payment_id = services.create_participant_payment(
                db, self.item_id, self.participant_id, "2000",
                lambda *a, **k: self.remote_payment(payment_id="pay-schedule-webhook"),
                7, fake_phone_normalizer, "https://example.test/",
            )
            self.assertIsNotNone(payment_id)

        fake_remote = self.remote_payment(payment_id="pay-schedule-webhook", status="succeeded")
        with mock.patch.object(
            application_module, "_yookassa_request", return_value=fake_remote
        ):
            response = self.client.post(
                "/yookassa/webhook",
                json={"event": "payment.succeeded", "object": {"id": "pay-schedule-webhook"}},
            )
            self.assertEqual(response.status_code, 200)

        with application_module.app.app_context():
            db = application_module.get_db()
            participant = db.execute(
                "SELECT paid_online FROM schedule_participants WHERE id = ?",
                (self.participant_id,),
            ).fetchone()
            self.assertEqual(participant["paid_online"], 2000.0)


if __name__ == "__main__":
    unittest.main()
