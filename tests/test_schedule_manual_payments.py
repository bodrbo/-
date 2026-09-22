import unittest

from modules.schedule import repository, services
from support import application_module


class ScheduleManualPaymentTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            cursor = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
                "VALUES ('Ручная оплата', '', '+79998880019', "
                "'schedule-manual-payment-client', '2026-09-01 09:00')"
            )
            self.client_id = cursor.lastrowid
            item_cursor = db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_name, starts_at, ends_at, capacity, "
                "participants_count, revenue, created_at, updated_at) "
                "VALUES ('event', 'Ларус', 'Средний тур', '2026-09-10 10:00', "
                "'2026-09-10 12:00', 10, 1, 9000, "
                "'2026-09-01 09:00', '2026-09-01 09:00')"
            )
            self.item_id = item_cursor.lastrowid
            participant_cursor = db.execute(
                "INSERT INTO schedule_participants "
                "(schedule_item_id, client_id, client_name, client_phone, guests_count, "
                "price, prepayment, payment_due, created_at, source) "
                "VALUES (?, ?, 'Ручная оплата', '+79998880019', 2, 9000, 0, 9000, "
                "'2026-09-01 09:00', 'internal')",
                (self.item_id, self.client_id),
            )
            self.participant_id = participant_cursor.lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM schedule_manual_payments WHERE schedule_item_id = ?",
                (self.item_id,),
            )
            db.execute(
                "DELETE FROM schedule_participants WHERE id = ?",
                (self.participant_id,),
            )
            db.execute("DELETE FROM schedule_items WHERE id = ?", (self.item_id,))
            db.execute("DELETE FROM clients WHERE id = ?", (self.client_id,))
            db.commit()

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def payment_url(self):
        return (
            f"/schedule/items/{self.item_id}/participants/"
            f"{self.participant_id}/manual-payments"
        )

    def test_service_records_both_methods_and_returns_ledger_total(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            cash_ok, cash_message, _cash_id = services.create_manual_payment(
                db, self.item_id, self.participant_id, "2 500,50", "cash"
            )
            cashless_ok, cashless_message, _cashless_id = services.create_manual_payment(
                db, self.item_id, self.participant_id, "1000", "cashless"
            )
            participant = repository.list_item_participants_with_addons(
                db, self.item_id
            )[0]

            self.assertTrue(cash_ok, cash_message)
            self.assertTrue(cashless_ok, cashless_message)
            self.assertEqual(participant["paid_manual"], 3500.50)
            self.assertEqual(
                {payment["payment_method"] for payment in participant["manual_payments"]},
                {"cash", "cashless"},
            )

    def test_service_rejects_invalid_or_excessive_payment(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE schedule_participants SET paid_online = 8500 WHERE id = ?",
                (self.participant_id,),
            )
            db.commit()

            too_large = services.create_manual_payment(
                db, self.item_id, self.participant_id, "501", "cash"
            )
            invalid_method = services.create_manual_payment(
                db, self.item_id, self.participant_id, "100", "crypto"
            )
            exact = services.create_manual_payment(
                db, self.item_id, self.participant_id, "500", "cashless"
            )

            self.assertFalse(too_large[0])
            self.assertIn("500.00", too_large[1])
            self.assertFalse(invalid_method[0])
            self.assertIn("способ оплаты", invalid_method[1])
            self.assertTrue(exact[0], exact[1])

    def test_routes_create_and_delete_manual_payment(self):
        self.login()
        create_response = self.client.post(
            self.payment_url(),
            json={"amount": "3200", "payment_method": "cash"},
        )
        create_body = create_response.get_json()
        payment = create_body["participants"][0]["manual_payments"][0]

        self.assertEqual(create_response.status_code, 200)
        self.assertTrue(create_body["ok"])
        self.assertEqual(create_body["participants"][0]["paid_manual"], 3200)
        self.assertEqual(payment["payment_method"], "cash")

        delete_response = self.client.post(
            f"{self.payment_url()}/{payment['id']}/delete"
        )
        delete_body = delete_response.get_json()
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_body["participants"][0]["paid_manual"], 0)
        self.assertEqual(delete_body["participants"][0]["manual_payments"], [])

    def test_route_requires_manager_access(self):
        response = self.client.post(
            self.payment_url(),
            json={"amount": "100", "payment_method": "cash"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
