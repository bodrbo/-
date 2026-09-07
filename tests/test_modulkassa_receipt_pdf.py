import json
import unittest
from unittest.mock import patch

from support import application_module


class ModulkassaReceiptPdfTests(unittest.TestCase):
    SOURCE_REF = "receipt-pdf-test-order"
    CLIENT_TOKEN = "receipt-pdf-client-token"
    OTHER_TOKEN = "receipt-pdf-other-token"
    QR_PAYLOAD = (
        "t=20260907T143000&s=12500.00&fn=9999078900008998"
        "&i=0000000571&fp=3125146288&n=1"
    )

    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            self.client_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Анна Тестова', 'Тестовая лодка', '+79990001122', ?, "
                "'2026-09-07 12:00')",
                (self.CLIENT_TOKEN,),
            ).lastrowid
            self.other_client_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Другой Клиент', '', '', ?, '2026-09-07 12:00')",
                (self.OTHER_TOKEN,),
            ).lastrowid
            self.order_id = db.execute(
                "INSERT INTO tuning_orders "
                "(client_id, client_name, equipment_type, boat_model, "
                "boat_registration_number, motor_model, motor_serial_number, "
                "sale_channel, phone, discount_pct, discount_type, discount_value, "
                "subtotal, total, status, source, source_ref, order_date, "
                "created_at, updated_at) VALUES (?, 'Анна Тестова', 'boat', "
                "'Тестовая лодка', '', '', '', 'direct', '+79990001122', 0, "
                "'percent', 0, 12500, 12500, 'done', 'manual', ?, '2026-09-07', "
                "'2026-09-07 12:00', '2026-09-07 12:00')",
                (self.client_id, self.SOURCE_REF),
            ).lastrowid
            self.payment_id = db.execute(
                "INSERT INTO tuning_payments "
                "(order_id, amount, paid_at, created_at, payment_type) "
                "VALUES (?, 12500, '2026-09-07 14:30', "
                "'2026-09-07 14:30', 'CARD')",
                (self.order_id,),
            ).lastrowid
            fiscal_info = {
                "shiftNumber": 8,
                "checkNumber": 17,
                "kktNumber": "199036005916",
                "fnNumber": "9999078900008998",
                "fnDocNumber": 571,
                "fnDocMark": 3125146288,
                "date": "2026-09-07T14:30:00+03:00",
                "sum": 12500,
                "checkType": "SALE",
                "qr": self.QR_PAYLOAD,
                "ecrRegistrationNumber": "0000000000028799",
            }
            db.execute(
                "INSERT INTO modulkassa_receipts "
                "(payment_id, doc_id, status, fiscal_info_json, created_at, updated_at) "
                "VALUES (?, 'receipt-pdf-doc-id', 'completed', ?, "
                "'2026-09-07 14:30:01', '2026-09-07 14:30:10')",
                (self.payment_id, json.dumps(fiscal_info, ensure_ascii=False)),
            )
            db.commit()
        self.addCleanup(self.cleanup_database)

    @classmethod
    def _clear_test_data(cls, db):
        order_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM tuning_orders WHERE source_ref = ?", (cls.SOURCE_REF,)
            ).fetchall()
        ]
        for order_id in order_ids:
            payment_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM tuning_payments WHERE order_id = ?", (order_id,)
                ).fetchall()
            ]
            for payment_id in payment_ids:
                db.execute(
                    "DELETE FROM modulkassa_receipts WHERE payment_id = ?",
                    (payment_id,),
                )
            db.execute("DELETE FROM tuning_payments WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_orders WHERE id = ?", (order_id,))
        db.execute(
            "DELETE FROM clients WHERE token IN (?, ?)",
            (cls.CLIENT_TOKEN, cls.OTHER_TOKEN),
        )
        db.commit()

    @classmethod
    def cleanup_database(cls):
        with application_module.app.app_context():
            cls._clear_test_data(application_module.get_db())

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def test_client_can_open_own_receipt_pdf_with_fiscal_details(self):
        original_qr_builder = application_module._build_fiscal_receipt_qr
        captured_qr_payloads = []

        def capture_qr(payload, size):
            captured_qr_payloads.append(payload)
            return original_qr_builder(payload, size)

        with patch.object(
            application_module,
            "_build_fiscal_receipt_qr",
            side_effect=capture_qr,
        ):
            response = self.client.get(
                f"/client/{self.CLIENT_TOKEN}/orders/{self.order_id}/payments/"
                f"{self.payment_id}/receipt.pdf"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertIn("inline; filename=", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertGreater(len(response.data), 10000)
        self.assertEqual(captured_qr_payloads, [self.QR_PAYLOAD])
        self.assertIn(
            application_module.FNS_RECEIPT_CHECK_URL.encode(), response.data
        )
        with application_module.app.app_context():
            receipt = application_module._modulkassa_receipt_for_payment(
                application_module.get_db(), self.order_id, self.payment_id
            )
        fiscal_info = application_module._modulkassa_fiscal_info(receipt)
        self.assertEqual(fiscal_info["fnNumber"], "9999078900008998")
        self.assertEqual(fiscal_info["fnDocMark"], 3125146288)
        self.assertEqual(fiscal_info["sum"], 12500)
        self.assertEqual(
            application_module._receipt_datetime_from_qr(self.QR_PAYLOAD),
            "07.09.2026 14:30",
        )

    def test_client_cannot_open_another_clients_receipt(self):
        response = self.client.get(
            f"/client/{self.OTHER_TOKEN}/orders/{self.order_id}/payments/"
            f"{self.payment_id}/receipt.pdf"
        )

        self.assertEqual(response.status_code, 404)

    def test_admin_receipt_requires_login_and_then_returns_pdf(self):
        url = (
            f"/tuning/{self.order_id}/pay/{self.payment_id}/receipt.pdf"
        )
        anonymous = self.client.get(url)
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/admin/login", anonymous.headers["Location"])

        self.login()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"%PDF-"))

    def test_receipt_links_are_visible_in_both_cabinets(self):
        client_html = self.client.get(
            f"/client/{self.CLIENT_TOKEN}"
        ).get_data(as_text=True)
        client_url = (
            f"/client/{self.CLIENT_TOKEN}/orders/{self.order_id}/payments/"
            f"{self.payment_id}/receipt.pdf"
        )
        self.assertIn("Кассовые чеки", client_html)
        self.assertIn("Открыть PDF", client_html)
        self.assertIn(client_url, client_html)

        self.login()
        admin_html = self.client.get(
            f"/tuning/edit/{self.order_id}"
        ).get_data(as_text=True)
        self.assertIn("PDF чека", admin_html)
        self.assertIn(
            f"/tuning/{self.order_id}/pay/{self.payment_id}/receipt.pdf",
            admin_html,
        )

    def test_incomplete_receipt_has_no_pdf_link(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE modulkassa_receipts SET status = 'pending', "
                "fiscal_info_json = NULL WHERE payment_id = ?",
                (self.payment_id,),
            )
            db.commit()

        client_html = self.client.get(
            f"/client/{self.CLIENT_TOKEN}"
        ).get_data(as_text=True)
        self.assertNotIn("Открыть PDF", client_html)
        response = self.client.get(
            f"/client/{self.CLIENT_TOKEN}/orders/{self.order_id}/payments/"
            f"{self.payment_id}/receipt.pdf"
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
