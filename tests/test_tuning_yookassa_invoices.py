import unittest
from unittest import mock

from support import application_module


class TuningYookassaInvoiceTests(unittest.TestCase):
    MARK = "tuning-invoice-test"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self._cleanup(db)
            self.client_id = db.execute(
                "INSERT INTO clients (client_name, token, created_at) VALUES ('Инв Клиент', ?, "
                "'2026-09-01 10:00')", (f"{self.MARK}-token",),
            ).lastrowid
            self.order_id = db.execute(
                "INSERT INTO tuning_orders (client_id, client_name, equipment_type, boat_model, "
                "sale_channel, phone, subtotal, total, status, order_date, created_at, updated_at, "
                "source, source_ref) VALUES (?, 'Инв Клиент', 'boat', 'Тест', 'direct', "
                "'+7 900 000-00-00', 10000, 10000, 'in_progress', '2026-09-10', "
                "'2026-09-10 10:00', '2026-09-10 10:00', 'manual', ?)",
                (self.client_id, self.MARK),
            ).lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref = ?", (self.MARK,)
        ).fetchall()]
        for order_id in ids:
            db.execute("DELETE FROM tuning_yookassa_payments WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_payments WHERE order_id = ?", (order_id,))
            application_module._delete_tuning_order_records(db, [order_id])
        db.execute("DELETE FROM clients WHERE token = ?", (f"{self.MARK}-token",))
        db.commit()

    def invoice(self, status="pending", details=None):
        return {
            "id": "inv-t1", "status": status,
            "delivery_method": {"type": "self", "url": "https://yookassa.ru/my/i/t1/a"},
            "expires_at": "2026-10-10T10:00:00.000Z", "payment_details": details,
        }

    def create_link(self, api):
        with mock.patch.object(application_module, "yookassa_configured", return_value=True), \
                mock.patch.object(application_module, "_yookassa_request", api):
            return self.client.post(
                f"/tuning/{self.order_id}/yookassa/create", data={"amount": "4000"}
            )

    def row(self):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT * FROM tuning_yookassa_payments WHERE order_id = ?", (self.order_id,)
            ).fetchone()

    def test_link_is_a_long_lived_invoice(self):
        api = mock.Mock(return_value=self.invoice())
        response = self.create_link(api)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(api.call_args.args[:2], ("POST", "/invoices"))
        body = api.call_args.kwargs["json_body"]
        self.assertEqual(body["delivery_method_data"], {"type": "self"})
        self.assertEqual(body["payment_data"]["amount"]["value"], "4000.00")
        self.assertEqual(body["payment_data"]["metadata"]["tuning_order_id"], str(self.order_id))
        self.assertEqual(body["cart"][0]["price"]["value"], "4000.00")
        self.assertTrue(body["expires_at"].endswith("Z"))
        row = self.row()
        self.assertEqual(row["yookassa_invoice_id"], "inv-t1")
        self.assertEqual(row["yookassa_payment_id"], "invoice:inv-t1")
        self.assertEqual(row["confirmation_url"], "https://yookassa.ru/my/i/t1/a")
        self.assertEqual(row["expires_at"], "2026-10-10T10:00:00.000Z")
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("https://yookassa.ru/my/i/t1/a", page)
        self.assertIn("до 10.10", page)

    def test_goods_receipt_lines_use_the_unit_price(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO tuning_order_products (order_id, product_id, product_name, quantity, "
                "unit_price, cost_price, unit, created_at) VALUES (?, 1, 'Фитинг', 3, 250, 100, "
                "'piece', '2026-09-10 10:00')", (self.order_id,),
            )
            db.execute(
                "INSERT INTO tuning_order_products (order_id, product_id, product_name, quantity, "
                "unit_price, cost_price, unit, created_at) VALUES (?, 2, 'Канат', 2.5, 120, 50, "
                "'linear_m', '2026-09-10 10:00')", (self.order_id,),
            )
            db.commit()
        api = mock.Mock(return_value=self.invoice())
        with mock.patch.object(application_module, "yookassa_configured", return_value=True), \
                mock.patch.object(application_module, "_yookassa_request", api):
            self.client.post(f"/tuning/{self.order_id}/yookassa/create-goods")
        body = api.call_args.kwargs["json_body"]
        items = body["payment_data"]["receipt"]["items"]
        self.assertEqual(
            [(i["quantity"], i["amount"]["value"]) for i in items], [(3, "250.00"), (2.5, "120.00")]
        )
        self.assertEqual(body["payment_data"]["amount"]["value"], "1050.00")  # 3*250 + 2.5*120
        cart_total = sum(float(c["price"]["value"]) * c["quantity"] for c in body["cart"])
        self.assertAlmostEqual(cart_total, 1050.0)
        with application_module.app.app_context():
            application_module.get_db().execute(
                "DELETE FROM tuning_order_products WHERE order_id = ?", (self.order_id,)
            )
            application_module.get_db().commit()

    def test_falls_back_to_a_plain_payment_and_warns(self):
        def api(method, path, **kwargs):
            if path == "/invoices":
                raise RuntimeError("invoices disabled")
            return {"id": "pay-plain", "status": "pending",
                    "confirmation": {"confirmation_url": "https://yookassa.ru/checkout/plain"}}

        self.create_link(api)
        row = self.row()
        self.assertIsNone(row["yookassa_invoice_id"])
        self.assertEqual(row["yookassa_payment_id"], "pay-plain")
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("короткоживущая", page)

    def test_paid_invoice_is_recorded_once_via_check(self):
        self.create_link(mock.Mock(return_value=self.invoice()))
        paid = self.invoice("succeeded", {"id": "pay-inv", "status": "succeeded"})
        row = self.row()
        with mock.patch.object(application_module, "_yookassa_request", mock.Mock(return_value=paid)):
            for _ in range(2):
                self.client.post(
                    f"/tuning/{self.order_id}/yookassa/{row['id']}/check"
                )
        row = self.row()
        with application_module.app.app_context():
            payments = application_module.get_db().execute(
                "SELECT amount FROM tuning_payments WHERE order_id = ?", (self.order_id,)
            ).fetchall()
        self.assertEqual((row["status"], row["yookassa_payment_id"]), ("succeeded", "pay-inv"))
        self.assertEqual([p["amount"] for p in payments], [4000.0])

    def test_webhook_payment_is_matched_to_its_invoice_link(self):
        self.create_link(mock.Mock(return_value=self.invoice()))

        def api(method, path, **kwargs):
            if path == "/payments/pay-hook":
                return {"id": "pay-hook", "status": "succeeded",
                        "amount": {"value": "4000.00", "currency": "RUB"},
                        "invoice_details": {"id": "inv-t1"},
                        "metadata": {"tuning_order_id": str(self.order_id)}}
            return self.invoice("succeeded", {"id": "pay-hook", "status": "succeeded"})

        with mock.patch.object(application_module, "_yookassa_request", api):
            response = self.client.post(
                "/yookassa/webhook", json={"event": "payment.succeeded", "object": {"id": "pay-hook"}}
            )
        self.assertEqual(response.status_code, 200)
        row = self.row()
        self.assertEqual((row["status"], row["yookassa_payment_id"]), ("succeeded", "pay-hook"))

    def test_webhook_falls_back_to_order_and_amount(self):
        self.create_link(mock.Mock(return_value=self.invoice()))

        def api(method, path, **kwargs):
            if path == "/payments/pay-nolink":
                return {"id": "pay-nolink", "status": "succeeded",
                        "amount": {"value": "4000.00", "currency": "RUB"},
                        "metadata": {"tuning_order_id": str(self.order_id)}}
            return self.invoice("succeeded", {"id": "pay-nolink", "status": "succeeded"})

        with mock.patch.object(application_module, "_yookassa_request", api):
            self.client.post(
                "/yookassa/webhook", json={"event": "payment.succeeded", "object": {"id": "pay-nolink"}}
            )
        self.assertEqual(self.row()["status"], "succeeded")

    def test_expired_invoices_are_polled_into_canceled_and_not_offered_to_the_client(self):
        self.create_link(mock.Mock(return_value=self.invoice()))
        open_page = self.client.get(f"/client/{self.MARK}-token").get_data(as_text=True)
        self.assertIn("https://yookassa.ru/my/i/t1/a", open_page)
        with application_module.app.app_context():
            db = application_module.get_db()
            with mock.patch.object(
                application_module, "_yookassa_request",
                mock.Mock(return_value=self.invoice("canceled")),
            ):
                polled = application_module._sync_open_tuning_invoices(db)
            self.assertEqual(polled, 1)
            self.assertEqual(application_module._sync_open_tuning_invoices(db), 0)
        self.assertEqual(self.row()["status"], "canceled")
        page = self.client.get(f"/client/{self.MARK}-token").get_data(as_text=True)
        self.assertNotIn("https://yookassa.ru/my/i/t1/a", page)

    def test_deleting_an_invoice_link_warns(self):
        self.create_link(mock.Mock(return_value=self.invoice()))
        row = self.row()
        self.client.post(f"/tuning/{self.order_id}/yookassa/{row['id']}/delete")
        self.assertIsNone(self.row())
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("не отправляйте", page)


if __name__ == "__main__":
    unittest.main()
