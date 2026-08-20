import datetime as dt
import unittest
from unittest import mock

import requests

from modules.refunds import repository, services
from support import application_module


class RefundsModuleIntegrationTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM excursion_refunds")
            db.execute("DELETE FROM excursion_yookassa_payments")
            db.execute("DELETE FROM excursion_refund_records")
            db.commit()

        self.original_timestamp = services.current_timestamp
        services.current_timestamp = lambda: "2026-08-20 12:00"
        self.addCleanup(setattr, services, "current_timestamp", self.original_timestamp)

    def log_in_as_admin(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def raw_record(self, record_id=501, deleted=False, email="client@example.ru"):
        return {
            "id": record_id,
            "activity_id": 7001,
            "visit_id": 8001,
            "datetime": "2026-08-25T18:00:00+03:00",
            "deleted": deleted,
            "online": True,
            "paid_full": 1,
            "prepaid": True,
            "prepaid_confirmed": True,
            "client": {
                "name": "Иван Петров",
                "phone": "+7 900 000-00-00",
                "email": email,
            },
            "services": [
                {
                    "title": "Вечерняя экскурсия",
                    "cost_to_pay": 5000,
                    "amount": 1,
                }
            ],
        }

    def remote_payment(
        self,
        payment_id="pay-excursion-501",
        amount="5000.00",
        refunded="0.00",
        refundable=True,
        metadata=None,
    ):
        return {
            "id": payment_id,
            "status": "succeeded",
            "paid": True,
            "refundable": refundable,
            "amount": {"value": amount, "currency": "RUB"},
            "refunded_amount": {"value": refunded, "currency": "RUB"},
            "created_at": "2026-08-20T08:30:00Z",
            "description": "Оплата экскурсии",
            "metadata": metadata or {},
            "payment_method": {
                "type": "bank_card",
                "card": {"last4": "4242"},
            },
        }

    def seed_linked_payment(self, email="client@example.ru"):
        with application_module.app.app_context():
            db = application_module.get_db()
            services.sync_yclients_records(db, [self.raw_record(email=email)])
            payment, _ = services.sync_remote_payment(db, self.remote_payment())
            repository.link_payment(
                db,
                payment["id"],
                501,
                "manual",
                "Администратор теста",
                "2026-08-20 12:00",
            )
            db.commit()
            return payment["id"]

    def test_sync_preserves_cancelled_booking_and_auto_links_only_by_metadata(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            saved = services.sync_yclients_records(
                db, [self.raw_record(deleted=True)]
            )
            pages = []

            def api(method, path, params=None, **kwargs):
                pages.append((method, path, params))
                return {
                    "items": [
                        self.remote_payment(
                            metadata={"yclients_record_id": "501"}
                        )
                    ]
                }

            stats = services.sync_yookassa_payments(
                db, api, dt.date(2026, 8, 1).isoformat()
            )
            record = repository.get_record(db, 501)
            payment = repository.get_payment_by_remote_id(db, "pay-excursion-501")

            self.assertEqual(saved, 1)
            self.assertEqual(record["is_deleted"], 1)
            self.assertEqual(record["expected_amount"], 5000)
            self.assertEqual(stats["auto_linked"], 1)
            self.assertEqual(payment["yclients_record_id"], 501)
            self.assertEqual(payment["card_last4"], "4242")
            self.assertEqual(pages[0][2]["limit"], 100)

    def test_full_refund_is_idempotent_and_does_not_send_a_receipt(self):
        payment_id = self.seed_linked_payment()
        posts = []

        def api(method, path, json_body=None, idempotence_key=None, **kwargs):
            if method == "GET":
                return self.remote_payment()
            posts.append((json_body, idempotence_key))
            return {
                "id": "refund-full-501",
                "status": "succeeded",
                "amount": {"value": "5000.00", "currency": "RUB"},
                "payment_id": "pay-excursion-501",
                "created_at": "2026-08-20T09:05:00Z",
                "metadata": json_body["metadata"],
                "receipt_registration": "pending",
            }

        with application_module.app.app_context():
            db = application_module.get_db()
            success, message = services.create_refund(
                db,
                payment_id,
                "",
                "full",
                "Отмена рейса из-за погоды",
                "",
                "a" * 32,
                True,
                "Администратор теста",
                api,
                7,
                "full_prepayment",
            )
            duplicate_success, _ = services.create_refund(
                db,
                payment_id,
                "",
                "full",
                "Отмена рейса из-за погоды",
                "",
                "a" * 32,
                True,
                "Администратор теста",
                api,
                7,
                "full_prepayment",
            )
            refund = repository.list_refunds(db, payment_id)[0]
            payment = services.dashboard(
                db, "2026-08-01", "2026-09-01"
            )["records"][0]["payments"][0]

            self.assertTrue(success, message)
            self.assertFalse(duplicate_success)
            self.assertEqual(len(posts), 1)
            self.assertNotIn("receipt", posts[0][0])
            self.assertEqual(posts[0][1], "a" * 32)
            self.assertEqual(refund["status"], "succeeded")
            self.assertEqual(refund["yookassa_refund_id"], "refund-full-501")
            self.assertEqual(payment["available_amount"], 0)

    def test_partial_refund_sends_receipt_and_requires_client_email(self):
        payment_id = self.seed_linked_payment(email="")
        captured = []

        def api(method, path, json_body=None, **kwargs):
            if method == "GET":
                return self.remote_payment()
            captured.append(json_body)
            return {
                "id": "refund-partial-501",
                "status": "succeeded",
                "amount": {"value": "1500.00", "currency": "RUB"},
                "payment_id": "pay-excursion-501",
                "metadata": json_body["metadata"],
            }

        with application_module.app.app_context():
            db = application_module.get_db()
            missing_email, missing_message = services.create_refund(
                db,
                payment_id,
                "1500",
                "partial",
                "Частичная отмена мест",
                "",
                "b" * 32,
                True,
                "Администратор теста",
                api,
                7,
                "full_prepayment",
            )
            success, message = services.create_refund(
                db,
                payment_id,
                "1500",
                "partial",
                "Частичная отмена мест",
                "refund@example.ru",
                "c" * 32,
                True,
                "Администратор теста",
                api,
                7,
                "full_prepayment",
            )

            self.assertFalse(missing_email)
            self.assertIn("email", missing_message)
            self.assertTrue(success, message)
            receipt = captured[0]["receipt"]
            self.assertEqual(receipt["customer"]["email"], "refund@example.ru")
            self.assertEqual(receipt["items"][0]["amount"]["value"], "1500.00")
            self.assertEqual(receipt["items"][0]["vat_code"], 7)
            self.assertEqual(
                receipt["items"][0]["payment_mode"], "full_prepayment"
            )

    def test_unknown_network_result_retries_with_the_same_idempotence_key(self):
        payment_id = self.seed_linked_payment()
        post_keys = []

        def uncertain_api(method, path, idempotence_key=None, **kwargs):
            if method == "GET":
                return self.remote_payment()
            post_keys.append(idempotence_key)
            raise requests.Timeout("timeout")

        with application_module.app.app_context():
            db = application_module.get_db()
            success, _ = services.create_refund(
                db,
                payment_id,
                "1000",
                "partial",
                "Возврат одного места",
                "client@example.ru",
                "d" * 32,
                True,
                "Администратор теста",
                uncertain_api,
                7,
                "full_prepayment",
            )
            refund = repository.list_refunds(db, payment_id)[0]

            def retry_api(method, path, idempotence_key=None, json_body=None, **kwargs):
                if method == "GET":
                    return self.remote_payment()
                post_keys.append(idempotence_key)
                return {
                    "id": "refund-recovered-501",
                    "status": "succeeded",
                    "amount": {"value": "1000.00", "currency": "RUB"},
                    "payment_id": "pay-excursion-501",
                    "metadata": json_body["metadata"],
                }

            retried, _ = services.retry_unknown_refund(db, refund["id"], retry_api)
            updated = repository.get_refund(db, refund["id"])

            self.assertFalse(success)
            self.assertTrue(retried)
            self.assertEqual(post_keys, ["d" * 32, "d" * 32])
            self.assertEqual(updated["status"], "succeeded")

    def test_returning_the_remainder_after_an_earlier_partial_refund_uses_a_receipt(self):
        payment_id = self.seed_linked_payment()
        captured = []

        def api(method, path, json_body=None, **kwargs):
            if method == "GET":
                return self.remote_payment(refunded="1000.00")
            captured.append(json_body)
            return {
                "id": "refund-final-part-501",
                "status": "succeeded",
                "amount": {"value": "4000.00", "currency": "RUB"},
                "payment_id": "pay-excursion-501",
                "metadata": json_body["metadata"],
            }

        with application_module.app.app_context():
            db = application_module.get_db()
            success, message = services.create_refund(
                db,
                payment_id,
                "",
                "full",
                "Возврат оставшейся суммы",
                "client@example.ru",
                "e" * 32,
                True,
                "Администратор теста",
                api,
                7,
                "full_prepayment",
            )
            payment = services.dashboard(
                db, "2026-08-01", "2026-09-01"
            )["records"][0]["payments"][0]
            refund = repository.list_refunds(db, payment_id)[0]

            self.assertTrue(success, message)
            self.assertEqual(refund["refund_kind"], "partial")
            self.assertEqual(refund["refunded_before"], 1000)
            self.assertIn("receipt", captured[0])
            self.assertEqual(payment["available_amount"], 0)

    def test_partial_refund_rejects_a_remainder_below_one_ruble(self):
        payment_id = self.seed_linked_payment()
        posts = []

        def api(method, path, json_body=None, **kwargs):
            if method == "GET":
                return self.remote_payment()
            posts.append(json_body)
            return {}

        with application_module.app.app_context():
            success, message = services.create_refund(
                application_module.get_db(),
                payment_id,
                "4999.50",
                "partial",
                "Частичная отмена рейса",
                "client@example.ru",
                "f" * 32,
                True,
                "Администратор теста",
                api,
                7,
                "full_prepayment",
            )

            self.assertFalse(success)
            self.assertIn("остаться не меньше 1 ₽", message)
            self.assertEqual(posts, [])

    def test_yookassa_http_500_is_treated_as_an_unknown_outcome(self):
        response = mock.Mock(ok=False, status_code=500, text="internal error")
        with mock.patch.object(
            application_module.requests, "request", return_value=response
        ):
            with self.assertRaises(requests.HTTPError):
                application_module._yookassa_request("POST", "/refunds")

    def test_refunds_page_requires_admin_and_renders_linked_payment(self):
        self.seed_linked_payment()
        response = self.client.get("/trips/refunds")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        self.log_in_as_admin()
        response = self.client.get(
            "/trips/refunds?start_date=2026-08-01&end_date=2026-09-01"
        )
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Возвраты по экскурсионным рейсам", page)
        self.assertIn("Иван Петров", page)
        self.assertIn("pay-excursion-501", page)


if __name__ == "__main__":
    unittest.main()
