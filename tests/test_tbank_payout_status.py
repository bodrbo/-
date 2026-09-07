import unittest
from unittest.mock import patch

from support import application_module


class TbankPayoutStatusTests(unittest.TestCase):
    EMPLOYEE = "Тест Статус Т-Банк"
    PERIOD = "2026-08-31"

    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM payments WHERE employee = ?", (self.EMPLOYEE,))
            db.execute(
                "DELETE FROM tbank_payout_registries WHERE employee = ?",
                (self.EMPLOYEE,),
            )
            db.execute("DELETE FROM entries WHERE employee = ?", (self.EMPLOYEE,))
            db.execute(
                "INSERT INTO entries "
                "(employee, work_type, work_date, quantity, rate, amount, created_at) "
                "VALUES (?, 'Тестовая выплата', '2026-09-01', 1, 5000, 5000, "
                "'2026-09-01 10:00')",
                (self.EMPLOYEE,),
            )
            self.payout_id = db.execute(
                "INSERT INTO tbank_payout_registries "
                "(employee, period_key, amount, recipient_name, recipient_inn, "
                "payment_registry_id, status, created_at) "
                "VALUES (?, ?, 5000, ?, '123456789012', '98765', 'created', "
                "'2026-09-07 10:00')",
                (self.EMPLOYEE, self.PERIOD, self.EMPLOYEE),
            ).lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM payments WHERE employee = ?", (self.EMPLOYEE,))
            db.execute(
                "DELETE FROM tbank_payout_registries WHERE employee = ?",
                (self.EMPLOYEE,),
            )
            db.execute("DELETE FROM entries WHERE employee = ?", (self.EMPLOYEE,))
            db.commit()

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def configured_patches(self):
        return (
            patch.object(application_module, "TBANK_API_TOKEN_PAYMENT", "payment-token"),
            patch.object(application_module, "TBANK_ACCOUNT_NUMBER", "40702810000000000000"),
        )

    def test_executed_registry_marks_the_same_payroll_card_paid(self):
        self.login()
        token_patch, account_patch = self.configured_patches()
        with token_patch, account_patch, patch.object(
            application_module,
            "_tbank_request",
            return_value={"paymentRegistryId": 98765, "status": "EXECUTED"},
        ) as request_mock:
            response = self.client.post(
                f"/pay/tbank/{self.payout_id}/status",
                data={"employee_filter": "all"},
            )

        self.assertEqual(response.status_code, 302)
        request_mock.assert_called_once_with(
            "/v1/self-employed/payment-registry/98765",
            {},
            token="payment-token",
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            payout = db.execute(
                "SELECT bank_status, status_checked_at, status_error "
                "FROM tbank_payout_registries WHERE id = ?",
                (self.payout_id,),
            ).fetchone()
            payment_count = db.execute(
                "SELECT COUNT(*) FROM payments WHERE employee = ? AND period_key = ?",
                (self.EMPLOYEE, self.PERIOD),
            ).fetchone()[0]
        self.assertEqual(payout["bank_status"], "EXECUTED")
        self.assertIsNotNone(payout["status_checked_at"])
        self.assertIsNone(payout["status_error"])
        self.assertEqual(payment_count, 1)

        token_patch, account_patch = self.configured_patches()
        with token_patch, account_patch, patch.object(
            application_module, "_tbank_request"
        ) as cooldown_request:
            repeated = self.client.post(f"/pay/tbank/{self.payout_id}/status")
        self.assertEqual(repeated.status_code, 302)
        cooldown_request.assert_not_called()
        with application_module.app.app_context():
            payment_count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM payments WHERE employee = ? AND period_key = ?",
                (self.EMPLOYEE, self.PERIOD),
            ).fetchone()[0]
        self.assertEqual(payment_count, 1)

    def test_pending_registry_does_not_mark_payroll_paid(self):
        self.login()
        token_patch, account_patch = self.configured_patches()
        with token_patch, account_patch, patch.object(
            application_module,
            "_tbank_request",
            return_value={"paymentRegistry": {"status": "ACCEPTED"}},
        ):
            response = self.client.post(f"/pay/tbank/{self.payout_id}/status")

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            status = db.execute(
                "SELECT bank_status FROM tbank_payout_registries WHERE id = ?",
                (self.payout_id,),
            ).fetchone()[0]
            payment_count = db.execute(
                "SELECT COUNT(*) FROM payments WHERE employee = ?",
                (self.EMPLOYEE,),
            ).fetchone()[0]
        self.assertEqual(status, "ACCEPTED")
        self.assertEqual(payment_count, 0)

    def test_api_error_is_shown_without_marking_payroll_paid(self):
        self.login()
        token_patch, account_patch = self.configured_patches()
        with token_patch, account_patch, patch.object(
            application_module,
            "_tbank_request",
            side_effect=RuntimeError("временная ошибка банка"),
        ):
            response = self.client.post(f"/pay/tbank/{self.payout_id}/status")

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            payout = db.execute(
                "SELECT bank_status, status_error FROM tbank_payout_registries "
                "WHERE id = ?",
                (self.payout_id,),
            ).fetchone()
            payment_count = db.execute(
                "SELECT COUNT(*) FROM payments WHERE employee = ?",
                (self.EMPLOYEE,),
            ).fetchone()[0]
        self.assertIsNone(payout["bank_status"])
        self.assertIn("временная ошибка банка", payout["status_error"])
        self.assertEqual(payment_count, 0)

    def test_payroll_page_shows_manual_status_check(self):
        self.login()
        token_patch, account_patch = self.configured_patches()
        with token_patch, account_patch:
            html = self.client.get(
                f"/admin?week={self.PERIOD}"
            ).get_data(as_text=True)

        self.assertIn("Проверить оплату", html)
        self.assertIn(
            f'/pay/tbank/{self.payout_id}/status', html
        )

        with application_module.app.app_context():
            columns = {
                row[1] for row in application_module.get_db().execute(
                    "PRAGMA table_info(tbank_payout_registries)"
                ).fetchall()
            }
        self.assertTrue(
            {"bank_status", "status_checked_at", "status_error"}.issubset(columns)
        )


if __name__ == "__main__":
    unittest.main()
