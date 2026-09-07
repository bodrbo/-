import datetime as dt
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

    def test_automatic_check_marks_executed_registry_paid(self):
        token_patch, account_patch = self.configured_patches()
        with application_module.app.app_context(), token_patch, account_patch, patch.object(
            application_module,
            "_tbank_request",
            return_value={"status": "EXECUTED"},
        ):
            db = application_module.get_db()
            stats = application_module._sync_tbank_payout_statuses(
                db,
                now=dt.datetime(2026, 9, 7, 10, 10),
                sleep_func=lambda _seconds: None,
            )
            payout = db.execute(
                "SELECT bank_status, status_checked_at FROM tbank_payout_registries "
                "WHERE id = ?",
                (self.payout_id,),
            ).fetchone()
            payment_count = db.execute(
                "SELECT COUNT(*) FROM payments WHERE employee = ? AND period_key = ?",
                (self.EMPLOYEE, self.PERIOD),
            ).fetchone()[0]

        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["executed"], 1)
        self.assertEqual(payout["bank_status"], "EXECUTED")
        self.assertEqual(payout["status_checked_at"], "2026-09-07 10:10:00")
        self.assertEqual(payment_count, 1)

    def test_automatic_check_respects_delay_and_skips_terminal_status(self):
        token_patch, account_patch = self.configured_patches()
        with application_module.app.app_context(), token_patch, account_patch, patch.object(
            application_module, "_tbank_request"
        ) as request_mock:
            db = application_module.get_db()
            too_early = application_module._sync_tbank_payout_statuses(
                db,
                now=dt.datetime(2026, 9, 7, 10, 9, 59),
                sleep_func=lambda _seconds: None,
            )
            db.execute(
                "UPDATE tbank_payout_registries SET bank_status = 'REJECTED' "
                "WHERE id = ?",
                (self.payout_id,),
            )
            db.commit()
            terminal = application_module._sync_tbank_payout_statuses(
                db,
                now=dt.datetime(2026, 9, 7, 11, 0),
                sleep_func=lambda _seconds: None,
            )

        self.assertEqual(too_early["checked"], 0)
        self.assertEqual(terminal["checked"], 0)
        request_mock.assert_not_called()

    def test_one_automatic_api_error_does_not_block_other_registries(self):
        token_patch, account_patch = self.configured_patches()
        with application_module.app.app_context(), token_patch, account_patch:
            db = application_module.get_db()
            second_id = db.execute(
                "INSERT INTO tbank_payout_registries "
                "(employee, period_key, amount, recipient_name, recipient_inn, "
                "payment_registry_id, status, created_at) "
                "VALUES (?, ?, 5000, ?, '123456789012', '98766', 'created', "
                "'2026-09-07 10:00')",
                (self.EMPLOYEE, self.PERIOD, self.EMPLOYEE),
            ).lastrowid
            db.commit()
            sleeps = []
            with patch.object(
                application_module,
                "_tbank_fetch_payout_registry_status",
                side_effect=[RuntimeError("банк временно недоступен"), "ACCEPTED"],
            ):
                stats = application_module._sync_tbank_payout_statuses(
                    db,
                    now=dt.datetime(2026, 9, 7, 10, 20),
                    sleep_func=sleeps.append,
                )
            first = db.execute(
                "SELECT status_error FROM tbank_payout_registries WHERE id = ?",
                (self.payout_id,),
            ).fetchone()
            second = db.execute(
                "SELECT bank_status FROM tbank_payout_registries WHERE id = ?",
                (second_id,),
            ).fetchone()

        self.assertEqual(stats["checked"], 2)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["pending"], 1)
        self.assertIn("временно недоступен", first["status_error"])
        self.assertEqual(second["bank_status"], "ACCEPTED")
        self.assertEqual(sleeps, [1])

    def test_standalone_cron_route_is_protected_and_reports_results(self):
        denied = self.client.get(
            "/internal/cron/check-tbank-payouts?token=wrong"
        )
        self.assertEqual(denied.status_code, 403)

        with patch.object(application_module, "CRON_SECRET", "cron-secret"), patch.object(
            application_module,
            "_sync_tbank_payout_statuses",
            return_value={
                "configured": True,
                "checked": 2,
                "executed": 1,
                "pending": 1,
                "failed": 0,
                "errors": 0,
            },
        ) as sync_mock:
            response = self.client.get(
                "/internal/cron/check-tbank-payouts?token=cron-secret"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("2 checked, 1 paid", response.get_data(as_text=True))
        sync_mock.assert_called_once()

    def test_existing_hourly_cron_also_checks_tbank_payouts(self):
        payout_stats = {
            "configured": True,
            "checked": 1,
            "executed": 1,
            "pending": 0,
            "failed": 0,
            "errors": 0,
        }
        with patch.object(application_module, "CRON_SECRET", "cron-secret"), patch.object(
            application_module,
            "send_due_task_reminders",
            return_value={"sent_3h": 0, "sent_6h": 0},
        ), patch.object(
            application_module,
            "_sync_tbank_payout_statuses",
            return_value=payout_stats,
        ) as sync_mock, patch.object(
            application_module, "yclients_configured", return_value=False
        ):
            response = self.client.get(
                "/internal/cron/sync-fuel?token=cron-secret"
            )

        # YCLIENTS remains a separately reported dependency, but its absence
        # must not prevent the bank observer from completing first.
        self.assertEqual(response.status_code, 503)
        self.assertIn(
            "tbank payouts: 1 checked, 1 paid, 0 API errors",
            response.get_data(as_text=True),
        )
        sync_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
