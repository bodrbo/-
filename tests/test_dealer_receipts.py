import json
import unittest
from unittest import mock

from support import application_module

CLIENT = "Покупатель Чеков Дилера"
TOKEN = "dealer-receipts-token"
OTHER_TOKEN = "dealer-receipts-other-token"
QR = "t=20260907T143000&s=5000.00&fn=9999078900008998&i=0000000571&fp=3125146288&n=1"
FISCAL = {
    "shiftNumber": 8, "checkNumber": 17, "kktNumber": "199036005916", "fnNumber": "9999078900008998",
    "fnDocNumber": 571, "fnDocMark": 3125146288, "date": "2026-09-07T14:30:00+03:00", "sum": 5000,
    "checkType": "SALE", "qr": QR, "ecrRegistrationNumber": "0000000000028799",
}


class _Response:
    def __init__(self, payload=None, ok=True, status_code=200, text=""):
        self._payload = payload if payload is not None else {"status": "QUEUED"}
        self.ok, self.status_code, self.text = ok, status_code, text

    def json(self):
        return self._payload


class DealerReceiptTests(unittest.TestCase):
    """Дилерский центр: платежи с кассовыми чеками (МодульКасса) — как в тюнинг-заказах."""

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self.cleanup(db)
            self.client_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES (?, '', '', ?, 'neutral', '2026-09-01 10:00')", (CLIENT, TOKEN)).lastrowid
            db.execute("INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                       "VALUES (?, 'tuning', 'client', '2026-09-01 10:00')", (self.client_id,))
            self.order_id = db.execute(
                "INSERT INTO dealer_orders (client_id, client_name, phone, status, order_date, subtotal, total, "
                "created_at, updated_at) VALUES (?, ?, '+79990001122', 'approved', '2026-09-10', 20000, 20000, "
                "'2026-09-10 10:00', '2026-09-10 10:00')", (self.client_id, CLIENT)).lastrowid
            db.execute("INSERT INTO dealer_order_items (order_id, kind, name, quantity, price, position) "
                       "VALUES (?, 'boat', 'Лодка Чековая', 1, 20000, 0)", (self.order_id,))
            db.commit()
        self.credentials = [
            mock.patch.object(application_module, "MODULKASSA_USERNAME", "user"),
            mock.patch.object(application_module, "MODULKASSA_PASSWORD", "password"),
        ]
        for patch in self.credentials:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        for row in db.execute("SELECT id FROM dealer_orders WHERE client_name = ?", (CLIENT,)).fetchall():
            for pay in db.execute("SELECT id FROM dealer_payments WHERE order_id = ?", (row["id"],)).fetchall():
                db.execute("DELETE FROM dealer_modulkassa_receipts WHERE payment_id = ?", (pay["id"],))
            db.execute("DELETE FROM dealer_payments WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_order_items WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_orders WHERE id = ?", (row["id"],))
        for row in db.execute("SELECT id FROM clients WHERE client_name = ?", (CLIENT,)).fetchall():
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    # ---- helpers

    def pay(self, response=None, **fields):
        data = {"amount": "5000", "payment_type": "CARD", "note": ""}
        data.update(fields)
        post = mock.patch.object(application_module.requests, "post", return_value=response or _Response())
        with post as sent:
            reply = self.client.post(f"/dealer/orders/{self.order_id}/payments", data=data)
        self.assertEqual(reply.status_code, 302)
        return sent

    def punch(self, **fields):
        return self.pay(fiscalize_shown="1", fiscalize="1", **fields)

    def payments(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM dealer_payments WHERE order_id = ? ORDER BY id", (self.order_id,)).fetchall()]

    def receipts(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT r.* FROM dealer_modulkassa_receipts r JOIN dealer_payments p ON p.id = r.payment_id "
                "WHERE p.order_id = ? ORDER BY r.id", (self.order_id,)).fetchall()]

    def notice(self):
        with self.client.session_transaction() as session:
            return session.get("dealer_notice") or {}

    def make_printed(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE dealer_modulkassa_receipts SET status = 'printed', fiscal_info_json = ?",
                       (json.dumps(FISCAL),))
            db.commit()

    def order_page(self):
        return self.client.get(f"/dealer/orders/{self.order_id}").get_data(as_text=True)

    # ---- punching a receipt

    def test_a_payment_with_the_receipt_checkbox_goes_to_the_cash_desk(self):
        sent = self.punch()
        self.assertEqual(sent.call_count, 1)
        self.assertTrue(sent.call_args.args[0].endswith("/v2/doc"))
        body = sent.call_args.kwargs["json"]
        payment = self.payments()[0]
        self.assertEqual(body["docType"], "SALE")
        self.assertEqual(body["docNum"], f"dealer-{self.order_id}-payment-{payment['id']}")
        self.assertEqual(body["inventPositions"][0]["name"], f"Оплата по заказу дилерского центра №{self.order_id}")
        self.assertEqual(body["inventPositions"][0]["price"], 5000.0)
        self.assertEqual(body["moneyPositions"], [{"paymentType": "CARD", "sum": 5000.0}])
        self.assertEqual(body["email"], "+79990001122")
        self.assertEqual((payment["no_receipt"], payment["is_correction"]), (0, 0))
        (receipt,) = self.receipts()
        self.assertEqual((receipt["payment_id"], receipt["status"], receipt["doc_id"]),
                         (payment["id"], "queued", body["id"]))
        self.assertIn("чек отправлен", self.notice()["message"])
        page = self.order_page()
        self.assertIn("В очереди", page)
        self.assertIn("Проверить чек", page)

    def test_a_correction_receipt_is_dated_and_sent_as_a_correction(self):
        sent = self.punch(is_correction="1", paid_date="2026-08-15")
        body = sent.call_args.kwargs["json"]
        self.assertEqual(body["docType"], "SALE_CORRECTION")
        self.assertEqual(body["correctionInfo"]["reason"], "INDEPENDENT")
        self.assertTrue(body["correctionInfo"]["documentDate"].startswith("2026-08-15T"))
        payment = self.payments()[0]
        self.assertEqual((payment["paid_at"], payment["is_correction"]), ("2026-08-15 12:00", 1))
        self.assertIn("Чек коррекции", self.order_page())

    def test_a_correction_needs_a_date_not_in_the_future(self):
        for date, text in (("", "укажите дату платежа"), ("2999-01-01", "в будущем")):
            sent = self.punch(is_correction="1", paid_date=date)
            self.assertEqual(sent.call_count, 0)
            self.assertEqual(self.payments(), [])
            self.assertEqual(self.notice()["type"], "error")
            self.assertIn(text, self.notice()["message"])

    def test_an_ordinary_receipt_is_dated_today_whatever_the_date_field_says(self):
        self.punch(paid_date="2026-01-01")
        self.assertNotEqual(self.payments()[0]["paid_at"][:10], "2026-01-01")

    def test_without_the_receipt_checkbox_the_payment_is_only_recorded(self):
        sent = self.pay(fiscalize_shown="1", paid_date="2026-09-20")
        self.assertEqual(sent.call_count, 0)
        payment = self.payments()[0]
        self.assertEqual((payment["no_receipt"], payment["paid_at"]), (1, "2026-09-20 12:00"))
        self.assertEqual(self.receipts(), [])
        self.assertIn("без чека", self.order_page())

    def test_a_correction_cannot_be_ticked_without_a_receipt(self):
        sent = self.pay(fiscalize_shown="1", is_correction="1", paid_date="2026-09-20")
        self.assertEqual(sent.call_count, 0)
        self.assertEqual(self.payments()[0]["is_correction"], 0)

    def test_an_old_form_without_the_checkbox_keeps_recording_only(self):
        sent = self.pay(paid_date="2026-09-21")
        self.assertEqual(sent.call_count, 0)
        self.assertEqual(self.payments()[0]["no_receipt"], 1)

    def test_the_form_offers_the_receipt_controls(self):
        page = self.order_page()
        for text in ('name="fiscalize"', 'name="fiscalize_shown"', 'name="is_correction"', "Пробить чек",
                     "Чек коррекции", 'id="paymentDateField"'):
            self.assertIn(text, page)

    # ---- no cash desk, failures, retries

    def test_without_the_cash_desk_the_payment_is_recorded_and_the_receipt_can_be_sent_later(self):
        with mock.patch.object(application_module, "MODULKASSA_USERNAME", ""):
            sent = self.punch()
            self.assertEqual(sent.call_count, 0)
            self.assertIn("не подключена", self.notice()["message"])
            self.assertIn("не подключена", self.order_page())
        payment = self.payments()[0]
        self.assertEqual(self.receipts(), [])
        page = self.order_page()
        self.assertIn("не отправлен", page)
        self.assertIn(f"/payments/{payment['id']}/receipt/retry", page)
        with mock.patch.object(application_module.requests, "post", return_value=_Response()) as sent:
            self.client.post(f"/dealer/orders/{self.order_id}/payments/{payment['id']}/receipt/retry")
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(len(self.receipts()), 1)

    def test_a_failed_receipt_can_be_repeated_but_a_queued_one_cannot(self):
        failing = application_module.requests.RequestException("нет связи")
        with mock.patch.object(application_module.requests, "post", side_effect=failing):
            self.client.post(f"/dealer/orders/{self.order_id}/payments", data={
                "amount": "5000", "payment_type": "CARD", "fiscalize_shown": "1", "fiscalize": "1"})
        (receipt,) = self.receipts()
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("не ушёл", self.notice()["message"])
        payment = self.payments()[0]
        page = self.order_page()
        self.assertIn("Повторить чек", page)
        self.assertIn("Ошибка", page)
        retry = f"/dealer/orders/{self.order_id}/payments/{payment['id']}/receipt/retry"
        with mock.patch.object(application_module.requests, "post", return_value=_Response()) as sent:
            self.client.post(retry)
            self.assertEqual(sent.call_count, 1)
            self.assertEqual([r["status"] for r in self.receipts()], ["failed", "queued"])
            self.client.post(retry)                           # already queued: not punched twice
            self.assertEqual(sent.call_count, 1)
        self.assertEqual(self.notice()["type"], "error")

    def test_a_correction_payment_is_retried_as_a_correction(self):
        failing = application_module.requests.RequestException("нет связи")
        with mock.patch.object(application_module.requests, "post", side_effect=failing):
            self.client.post(f"/dealer/orders/{self.order_id}/payments", data={
                "amount": "5000", "payment_type": "CASH", "fiscalize_shown": "1", "fiscalize": "1",
                "is_correction": "1", "paid_date": "2026-08-15"})
        payment = self.payments()[0]
        with mock.patch.object(application_module.requests, "post", return_value=_Response()) as sent:
            self.client.post(f"/dealer/orders/{self.order_id}/payments/{payment['id']}/receipt/retry")
        body = sent.call_args.kwargs["json"]
        self.assertEqual(body["docType"], "SALE_CORRECTION")
        self.assertTrue(body["correctionInfo"]["documentDate"].startswith("2026-08-15T"))

    def test_a_payment_without_a_receipt_is_not_retried(self):
        self.pay(fiscalize_shown="1")
        payment = self.payments()[0]
        with mock.patch.object(application_module.requests, "post") as sent:
            self.client.post(f"/dealer/orders/{self.order_id}/payments/{payment['id']}/receipt/retry")
        self.assertEqual(sent.call_count, 0)
        self.assertEqual(self.notice()["type"], "error")

    # ---- checking and the PDF

    def test_checking_updates_the_status_and_opens_the_pdf(self):
        self.punch()
        payment = self.payments()[0]
        answer = _Response({"status": "PRINTED", "fiscalInfo": FISCAL})
        with mock.patch.object(application_module.requests, "get", return_value=answer) as asked:
            self.client.post(f"/dealer/orders/{self.order_id}/payments/{payment['id']}/receipt/check")
        self.assertIn(f"/v1/doc/{self.receipts()[0]['doc_id']}/status", asked.call_args.args[0])
        self.assertEqual(self.receipts()[0]["status"], "printed")
        page = self.order_page()
        self.assertIn("Пробит", page)
        self.assertIn("PDF чека", page)
        self.assertNotIn("Проверить чек", page)
        pdf = self.client.get(f"/dealer/orders/{self.order_id}/payments/{payment['id']}/receipt.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.get_data().startswith(b"%PDF"))

    def test_no_pdf_before_the_fiscal_data_arrives(self):
        self.punch()
        payment = self.payments()[0]
        response = self.client.get(f"/dealer/orders/{self.order_id}/payments/{payment['id']}/receipt.pdf")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("PDF чека", self.order_page())

    def test_the_receipt_pages_need_an_admin_login(self):
        self.punch()
        payment = self.payments()[0]
        anonymous = application_module.app.test_client()
        base = f"/dealer/orders/{self.order_id}/payments/{payment['id']}/receipt"
        self.assertEqual(anonymous.get(base + ".pdf").status_code, 302)
        self.assertEqual(anonymous.post(base + "/check").status_code, 302)
        self.assertEqual(anonymous.post(base + "/retry").status_code, 302)

    def test_the_cron_job_polls_dealer_receipts_too(self):
        self.punch()
        answer = _Response({"status": "PRINTED", "fiscalInfo": FISCAL})
        with mock.patch.object(application_module, "CRON_SECRET", "dealer-cron"), \
                mock.patch.object(application_module, "yookassa_configured", return_value=False), \
                mock.patch.object(application_module.requests, "get", return_value=answer):
            response = self.client.get("/internal/cron/check-modulkassa-receipts?token=dealer-cron")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.receipts()[0]["status"], "printed")

    def test_deleting_a_payment_removes_its_receipts(self):
        self.punch()
        payment = self.payments()[0]
        self.client.post(f"/dealer/orders/{self.order_id}/payments/{payment['id']}/delete")
        self.assertEqual(self.payments(), [])
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM dealer_modulkassa_receipts WHERE payment_id = ?", (payment["id"],)).fetchone()[0]
        self.assertEqual(count, 0)

    # ---- the client

    def test_the_client_opens_his_receipt_from_the_cabinet(self):
        self.punch()
        payment = self.payments()[0]
        url = f"/client/{TOKEN}/dealer/{self.order_id}/payments/{payment['id']}/receipt.pdf"
        self.assertNotIn("Кассовый чек от", self.client.get(f"/client/{TOKEN}").get_data(as_text=True))
        self.assertEqual(self.client.get(url).status_code, 404)             # not fiscalized yet
        self.make_printed()
        cabinet = self.client.get(f"/client/{TOKEN}").get_data(as_text=True)
        self.assertIn("Кассовый чек от", cabinet)
        self.assertIn(url, cabinet)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_data().startswith(b"%PDF"))

    def test_another_client_cannot_open_the_receipt(self):
        self.punch()
        self.make_printed()
        payment = self.payments()[0]
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                       "VALUES (?, '', '', ?, 'neutral', '2026-09-01 10:00')", (CLIENT + " Другой", OTHER_TOKEN))
            db.commit()
        self.addCleanup(self.remove_other)
        url = f"/client/{OTHER_TOKEN}/dealer/{self.order_id}/payments/{payment['id']}/receipt.pdf"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(f"/client/no-such-token/dealer/{self.order_id}/payments/1/receipt.pdf").status_code, 302)

    def remove_other(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM clients WHERE token = ?", (OTHER_TOKEN,))
            db.commit()

    # ---- old databases

    def test_payments_recorded_before_receipts_existed_count_as_recorded_without_a_receipt(self):
        import sqlite3
        from modules.dealer.schema import init_schema
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE dealer_payments (id INTEGER PRIMARY KEY, order_id INTEGER, amount REAL, "
                     "paid_at TEXT, payment_type TEXT, note TEXT, created_at TEXT)")
        conn.execute("INSERT INTO dealer_payments (order_id, amount, paid_at, created_at) VALUES (1, 5, 'x', 'x')")
        init_schema(conn)
        init_schema(conn)
        self.assertEqual(conn.execute("SELECT no_receipt, is_correction FROM dealer_payments").fetchone(), (1, 0))
        conn.execute("INSERT INTO dealer_payments (order_id, amount, paid_at, created_at) VALUES (1, 5, 'x', 'x')")
        self.assertEqual([r[0] for r in conn.execute("SELECT no_receipt FROM dealer_payments ORDER BY id")], [1, 0])


if __name__ == "__main__":
    unittest.main()
