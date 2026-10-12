import os
import shutil
import subprocess
import tempfile
import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class TuningWorkDiscountTests(unittest.TestCase):
    """A discount can be given on one work, not only on the whole order: it
    comes off that work's own price, the order-wide discount applies to what
    is left, and it shows in the client's cabinet, the board and the acts."""

    CLIENT_NAME = "tuning-work-discount-test"
    TOKEN = "tuning-work-discount-token"

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
        self.addCleanup(self._cleanup_now)

    def _cleanup_now(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE client_name = ?", (self.CLIENT_NAME,)
        ).fetchall()]
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        row = db.execute("SELECT id FROM clients WHERE token = ?", (self.TOKEN,)).fetchone()
        if row:
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    # ---- helpers

    def form(self, works, discount=("percent", "0"), order_id=None):
        """works: (name, cost, multiplier, discount_value, discount_type[, item_id])."""
        data = MultiDict([
            ("client_name", self.CLIENT_NAME), ("client_id", str(self.client_id)),
            ("equipment_type", "boat"), ("boat_model", "Salute"), ("boat_registration_number", ""),
            ("motor_model", ""), ("motor_serial_number", ""), ("phone", ""),
            ("order_date", "2026-09-10"), ("sale_channel", "direct"),
            ("discount_type", discount[0]), ("discount_value", discount[1]),
        ])
        for work in works:
            name, cost, mult, d_value, d_type = work[:5]
            data.add("work_name[]", name)
            data.add("cost_price[]", str(cost))
            data.add("multiplier[]", str(mult))
            data.add("item_discount_value[]", str(d_value))
            data.add("item_discount_type[]", d_type)
            data.add("item_id[]", str(work[5]) if len(work) > 5 else "")
        return data

    def create(self, works, **kwargs):
        return self.client.post("/tuning/add", data=self.form(works, **kwargs))

    def order(self):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT * FROM tuning_orders WHERE client_name = ? ORDER BY id DESC LIMIT 1",
                (self.CLIENT_NAME,)).fetchone()
            return dict(row) if row else None

    def items(self, order_id=None):
        order_id = order_id or self.order()["id"]
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM tuning_order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()]

    # ---- saving and totals

    def test_a_percent_discount_on_one_work_lowers_its_price_and_the_order_total(self):
        response = self.create([
            ("Полировка", 1000, 10, 10, "percent"),   # 10 000 -> 9 000
            ("Пайол", 500, 4, "", "percent"),          # 2 000, no discount
        ])
        self.assertEqual(response.status_code, 302)
        first, second = self.items()
        self.assertEqual((first["list_price"], first["price"], first["discount_type"], first["discount_value"]),
                         (10000, 9000, "percent", 10))
        self.assertIsNone(second["list_price"])
        self.assertEqual(second["price"], 2000)
        order = self.order()
        self.assertEqual((order["subtotal"], order["total"]), (11000, 11000))

    def test_an_amount_discount_on_one_work(self):
        self.create([("Полировка", 1000, 10, 1500, "amount")])
        (item,) = self.items()
        self.assertEqual((item["list_price"], item["price"], item["discount_type"]), (10000, 8500, "amount"))
        self.assertEqual(self.order()["total"], 8500)

    def test_the_order_wide_discount_applies_to_what_is_left(self):
        self.create([("Полировка", 1000, 10, 10, "percent")], discount=("percent", "10"))
        order = self.order()
        self.assertEqual((order["subtotal"], order["total"]), (9000, 8100))  # 10 000 -10% -10%

    def test_a_work_without_a_price_yet_takes_no_discount(self):
        self.create([("После дефектовки", "", "", 10, "percent")])
        (item,) = self.items()
        self.assertEqual((item["price_pending"], item["price"]), (1, 0))
        self.assertIsNone(item["list_price"])

    def test_bad_discounts_are_refused(self):
        for value, kind in ((101, "percent"), (-5, "percent"), (20001, "amount"), ("abc", "percent")):
            self.create([("Полировка", 1000, 10, value, kind)])
            self.assertIsNone(self.order(), (value, kind))

    def test_editing_keeps_changes_and_removes_a_discount(self):
        self.create([("Полировка", 1000, 10, 10, "percent")])
        order_id = self.order()["id"]
        item_id = self.items()[0]["id"]
        self.client.post(f"/tuning/edit/{order_id}", data=self.form(
            [("Полировка", 1000, 10, 25, "percent", item_id)]))
        self.assertEqual((self.items()[0]["price"], self.items()[0]["discount_value"]), (7500, 25))
        self.client.post(f"/tuning/edit/{order_id}", data=self.form(
            [("Полировка", 1000, 10, "", "percent", item_id)]))
        item = self.items()[0]
        self.assertEqual((item["price"], item["list_price"], item["discount_value"]), (10000, None, 0))
        self.assertEqual(self.order()["total"], 10000)

    def test_a_copied_order_keeps_the_work_discount(self):
        self.create([("Полировка", 1000, 10, 10, "percent")], discount=("percent", "5"))
        source = self.order()
        response = self.client.post(f"/tuning/{source['id']}/copy")
        self.assertEqual(response.status_code, 302)
        copy = self.order()
        self.assertNotEqual(copy["id"], source["id"])
        (item,) = self.items(copy["id"])
        self.assertEqual((item["list_price"], item["price"], item["discount_type"], item["discount_value"]),
                         (10000, 9000, "percent", 10))
        self.assertEqual(copy["total"], 8550)  # 9 000 less the order-wide 5%

    # ---- display

    def test_the_form_has_the_discount_fields_and_shows_the_old_price(self):
        self.create([("Полировка", 1000, 10, 10, "percent")])
        page = self.client.get(f"/tuning/edit/{self.order()['id']}").get_data(as_text=True)
        self.assertIn('name="item_discount_value[]"', page)
        self.assertIn('name="item_discount_type[]"', page)
        self.assertIn("Скидка на весь заказ", page)
        row = page.split('class="work-row')[1]
        self.assertIn('value="10"', row.split('name="item_discount_value[]"')[1][:200])
        self.assertIn("10\xa0000", row)   # the price before the discount
        self.assertIn("9\xa0000", row)    # and after it

    def test_the_client_cabinet_shows_price_before_after_and_the_discount(self):
        self.create([("Полировка", 1000, 10, 10, "percent"), ("Пайол", 500, 4, "", "percent")])
        page = self.client.get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertIn('class="work-price-was"', page)
        self.assertIn("10\xa0000", page)
        self.assertIn("9\xa0000", page)
        self.assertIn("Скидка 10%", page)
        self.assertIn("Скидки на работы", page)
        self.assertIn("−1\xa0000", page)
        self.assertEqual(page.count('class="work-discount-badge"'), 1)  # only the discounted work

    def test_an_order_without_work_discounts_looks_as_before_in_the_cabinet(self):
        self.create([("Пайол", 500, 4, "", "percent")])
        page = self.client.get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertNotIn('class="work-price-was"', page)
        self.assertNotIn("Скидки на работы", page)

    def test_the_board_shows_the_discounted_work(self):
        self.create([("Полировка", 1000, 10, 15, "percent")])
        page = self.client.get(f"/tuning/{self.order()['id']}/board").get_data(as_text=True)
        self.assertIn('class="work-price-was"', page)
        self.assertIn("−15%", page)

    # ---- the acts

    def build(self, builder, order_dict, items, goods=()):
        with application_module.app.test_request_context():
            return getattr(application_module, builder)(order_dict, items, goods)

    def text_of(self, pdf):
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not installed")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "act.pdf")
            with open(path, "wb") as handle:
                handle.write(pdf)
            return subprocess.run(
                ["pdftotext", "-layout", path, "-"], capture_output=True, text=True, check=True
            ).stdout

    def order_and_items(self, order_discount=("percent", 0)):
        self.create([("Полировка корпуса", 1000, 10, 10, "percent"), ("Пайол", 500, 4, "", "percent")],
                    discount=(order_discount[0], str(order_discount[1])))
        order = self.order()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_order_items SET status = 'done' WHERE order_id = ?", (order["id"],))
            db.commit()
            rows = db.execute("SELECT * FROM tuning_order_items WHERE order_id = ? ORDER BY id",
                              (order["id"],)).fetchall()
        return order, rows

    def test_the_completed_work_act_shows_the_discount_of_a_work(self):
        order, items = self.order_and_items()
        pdf = self.build("_build_act_pdf", order, items)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        text = self.text_of(pdf)
        self.assertIn("Скидка", text)
        self.assertIn("10000,00", text)    # price before
        self.assertIn("−1000,00", text)    # the discount
        self.assertIn("(10%)", text)
        self.assertIn("9000,00", text)     # price after
        self.assertIn("11000,00", text)    # the sum of works

    def test_the_handover_act_shows_it_too(self):
        order, items = self.order_and_items()
        text = self.text_of(self.build("_build_handover_act_pdf", order, items))
        self.assertIn("−1000,00", text)
        self.assertIn("(10%)", text)

    def test_an_act_without_work_discounts_keeps_its_layout(self):
        self.create([("Пайол", 500, 4, "", "percent")])
        order = self.order()
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT * FROM tuning_order_items WHERE order_id = ?", (order["id"],)).fetchall()
        text = self.text_of(self.build("_build_act_pdf", order, rows))
        self.assertNotIn("Скидка", text)

    def test_the_order_wide_discount_stacks_on_work_discounts_in_both_acts(self):
        order, items = self.order_and_items(order_discount=("percent", 10))
        for builder in ("_build_act_pdf", "_build_handover_act_pdf"):
            text = self.text_of(self.build(builder, order, items))
            self.assertIn("Скидка (10%):", text, builder)
            self.assertIn("1100,00", text, builder)      # 10% of 11 000
            self.assertIn("К оплате:", text, builder)
            self.assertIn("9900,00", text, builder)      # 11 000 - 1 100


if __name__ == "__main__":
    unittest.main()
