import datetime as dt
import unittest
from unittest import mock

from werkzeug.datastructures import MultiDict

from support import application_module


class _FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def json(self):
        return {"status": "QUEUED"}


class TuningCorrectionPaymentTests(unittest.TestCase):
    """A payment can be recorded as "чек коррекции": it is taken on its own
    (past) date and its receipt goes to ModulKassa as SALE_CORRECTION with
    that date, instead of an ordinary SALE dated today."""

    CLIENT_NAME = "tuning-correction-payment-test"
    TOKEN = "tuning-correction-payment-token"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())
            db = application_module.get_db()
            self.client_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES (?, 'Salute', '', ?, 'neutral', '2026-09-01 10:00')", (self.CLIENT_NAME, self.TOKEN),
            ).lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                "VALUES (?, 'tuning', 'client', '2026-09-01 10:00')", (self.client_id,),
            )
            db.commit()
        self.client.post("/tuning/add", data=MultiDict([
            ("client_name", self.CLIENT_NAME), ("client_id", str(self.client_id)),
            ("equipment_type", "boat"), ("boat_model", "Salute"), ("boat_registration_number", ""),
            ("motor_model", ""), ("motor_serial_number", ""), ("phone", "+79990001122"),
            ("order_date", "2026-09-10"), ("sale_channel", "direct"),
            ("discount_type", "percent"), ("discount_value", "0"),
            ("work_name[]", "Полировка"), ("cost_price[]", "1000"), ("multiplier[]", "10"),
            ("item_discount_value[]", ""), ("item_discount_type[]", "percent"), ("item_id[]", ""),
        ]))
        self.order_id = self.order()["id"]
        patches = [
            mock.patch.object(application_module, "MODULKASSA_USERNAME", "user"),
            mock.patch.object(application_module, "MODULKASSA_PASSWORD", "password"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._cleanup_now)

    def _cleanup_now(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE client_name = ?", (self.CLIENT_NAME,)).fetchall()]
        for order_id in ids:
            payment_ids = [r["id"] for r in db.execute(
                "SELECT id FROM tuning_payments WHERE order_id = ?", (order_id,)).fetchall()]
            for payment_id in payment_ids:
                db.execute("DELETE FROM modulkassa_receipts WHERE payment_id = ?", (payment_id,))
            db.execute("DELETE FROM tuning_payments WHERE order_id = ?", (order_id,))
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        row = db.execute("SELECT id FROM clients WHERE token = ?", (self.TOKEN,)).fetchone()
        if row:
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    def order(self):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM tuning_orders WHERE client_name = ? ORDER BY id DESC LIMIT 1",
                (self.CLIENT_NAME,)).fetchone())

    def payments(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM tuning_payments WHERE order_id = ? ORDER BY id", (self.order_id,)).fetchall()]

    def pay(self, **fields):
        data = {"amount": "5000", "payment_type": "CARD"}
        data.update(fields)
        with mock.patch.object(application_module.requests, "post", return_value=_FakeResponse()) as post:
            response = self.client.post(f"/tuning/{self.order_id}/pay", data=data)
        return response, post

    def test_a_correction_payment_is_recorded_on_its_date_and_sent_as_a_correction_receipt(self):
        response, post = self.pay(is_correction="1", payment_date="2026-08-15")
        self.assertEqual(response.status_code, 302)
        (payment,) = self.payments()
        self.assertEqual(payment["paid_at"][:10], "2026-08-15")
        self.assertEqual((payment["is_correction"], payment["payment_type"], payment["amount"]), (1, "CARD", 5000))
        self.assertEqual(post.call_count, 1)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["docType"], "SALE_CORRECTION")
        self.assertEqual(body["correctionInfo"]["reason"], "INDEPENDENT")
        self.assertTrue(body["correctionInfo"]["documentDate"].startswith("2026-08-15T"))
        self.assertEqual(body["moneyPositions"], [{"paymentType": "CARD", "sum": 5000.0}])
        self.assertTrue(post.call_args.args[0].endswith("/v2/doc"))

    def test_an_ordinary_payment_is_still_an_ordinary_sale(self):
        _, post = self.pay()
        (payment,) = self.payments()
        self.assertEqual(payment["is_correction"], 0)
        self.assertEqual(payment["paid_at"][:10], dt.date.today().isoformat())
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["docType"], "SALE")
        self.assertNotIn("correctionInfo", body)

    def test_a_date_field_without_the_checkbox_is_ignored(self):
        _, post = self.pay(payment_date="2026-08-15")
        (payment,) = self.payments()
        self.assertEqual((payment["is_correction"], payment["paid_at"][:10]),
                         (0, dt.date.today().isoformat()))
        self.assertEqual(post.call_args.kwargs["json"]["docType"], "SALE")

    def test_a_correction_needs_a_valid_past_date_or_nothing_is_saved(self):
        tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        for bad in ("", "не дата", "2026-13-45", tomorrow):
            response, post = self.pay(is_correction="1", payment_date=bad)
            self.assertEqual(response.status_code, 302, bad)
            self.assertEqual(self.payments(), [], bad)
            self.assertEqual(post.call_count, 0, bad)
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("Для чека коррекции укажите дату платежа", page)
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertNotIn("Для чека коррекции укажите дату платежа", page)  # shown once

    def test_todays_date_is_accepted(self):
        today = dt.date.today().isoformat()
        self.pay(is_correction="1", payment_date=today)
        (payment,) = self.payments()
        self.assertEqual((payment["is_correction"], payment["paid_at"][:10]), (1, today))

    def test_retrying_a_failed_correction_receipt_stays_a_correction(self):
        self.pay(is_correction="1", payment_date="2026-08-15")
        (payment,) = self.payments()
        with mock.patch.object(application_module.requests, "post", return_value=_FakeResponse()) as post:
            self.client.post(f"/tuning/{self.order_id}/pay/{payment['id']}/receipt/retry")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["docType"], "SALE_CORRECTION")
        self.assertTrue(body["correctionInfo"]["documentDate"].startswith("2026-08-15T"))

    def test_the_form_has_the_checkbox_and_a_hidden_date_field(self):
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn('name="is_correction"', page)
        self.assertIn('id="paymentDateField" hidden', page)
        self.assertIn('name="payment_date"', page)
        self.assertIn(f'max="{dt.date.today().isoformat()}"', page)

    def test_the_payment_table_marks_a_correction(self):
        self.pay(is_correction="1", payment_date="2026-08-15")
        self.pay()
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertEqual(page.count("Чек коррекции</span>"), 1)
        self.assertIn("15/08/2026", page)


if __name__ == "__main__":
    unittest.main()
