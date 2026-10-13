import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class TuningRemovedWorkTotalsTests(unittest.TestCase):
    """A work taken off the order ("Задача снята") stays in the list for the
    record but no longer counts anywhere: not in the totals at the top of the
    order card, not in the sums under the list of works."""

    CLIENT_NAME = "tuning-removed-work-totals-test"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        self._cleanup_now()
        self.addCleanup(self._cleanup_now)

    def _cleanup_now(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            ids = [r["id"] for r in db.execute(
                "SELECT id FROM tuning_orders WHERE client_name = ?", (self.CLIENT_NAME,)).fetchall()]
            if ids:
                application_module._delete_tuning_order_records(db, ids)
            db.commit()

    def form(self, items, removed=(), discount=("percent", "0")):
        data = MultiDict([
            ("client_name", self.CLIENT_NAME), ("equipment_type", "boat"), ("boat_model", "Salute"),
            ("boat_registration_number", ""), ("motor_model", ""), ("motor_serial_number", ""),
            ("phone", ""), ("order_date", "2026-09-10"), ("sale_channel", "direct"),
            ("discount_type", discount[0]), ("discount_value", discount[1]),
        ])
        for index, (name, cost, mult, item_id) in enumerate(items):
            data.add("work_name[]", name)
            data.add("cost_price[]", str(cost))
            data.add("multiplier[]", str(mult))
            data.add("item_id[]", str(item_id))
            data.add("item_removed[]", "1" if index in removed else "0")
        return data

    def order(self):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM tuning_orders WHERE client_name = ?", (self.CLIENT_NAME,)).fetchone())

    def item_ids(self):
        with application_module.app.app_context():
            return [r["id"] for r in application_module.get_db().execute(
                "SELECT id FROM tuning_order_items WHERE order_id = ? ORDER BY id", (self.order()["id"],)
            ).fetchall()]

    def make_order(self, discount=("percent", "0")):
        # 5 000 + 3 000 (+ goods none)
        self.client.post("/tuning/add", data=self.form(
            [("Полировка", 500, 10, ""), ("Пайол", 300, 10, "")], discount=discount))
        return self.order()["id"]

    def remove_second_work(self, order_id):
        item_id = self.item_ids()[1]
        return self.client.post(f"/tuning/{order_id}/item/{item_id}/status", data={"status": "removed"})

    def test_taking_a_work_off_lowers_the_order_total(self):
        order_id = self.make_order()
        self.assertEqual(self.order()["total"], 8000)
        self.remove_second_work(order_id)
        order = self.order()
        self.assertEqual((order["subtotal"], order["total"]), (5000, 5000))

    def test_the_sums_under_the_list_of_works_skip_the_removed_work(self):
        order_id = self.make_order()
        self.remove_second_work(order_id)
        page = self.client.get(f"/tuning/edit/{order_id}").get_data(as_text=True)
        rows = page.split('class="work-row')[1:]
        flags = [("data-removed=\"1\"" in row.split(">")[0] + ">" + row.split(">")[1]) for row in rows[:2]]
        self.assertEqual(flags, [False, True])
        self.assertIn("work-row-removed", rows[1].split(">")[0])
        self.assertIn("Задача снята", rows[1])
        self.assertIn("if (row.dataset.removed === '1') return;", page)  # the live sum skips it
        self.assertIn('name="item_removed[]" value="1"', page)

    def test_saving_the_form_keeps_the_removed_work_out_of_the_total(self):
        order_id = self.make_order(discount=("percent", "10"))
        self.remove_second_work(order_id)
        ids = self.item_ids()
        response = self.client.post(f"/tuning/edit/{order_id}", data=self.form(
            [("Полировка", 500, 10, ids[0]), ("Пайол", 300, 10, ids[1])], removed=(1,),
            discount=("percent", "10")))
        self.assertEqual(response.status_code, 302)
        order = self.order()
        self.assertEqual((order["subtotal"], order["total"]), (5000, 4500))  # 5 000 less 10%
        with application_module.app.app_context():
            status = application_module.get_db().execute(
                "SELECT status FROM tuning_order_items WHERE id = ?", (ids[1],)).fetchone()["status"]
        self.assertEqual(status, "removed")

    def test_an_amount_discount_is_checked_against_what_is_still_paid_for(self):
        order_id = self.make_order()
        self.remove_second_work(order_id)
        ids = self.item_ids()
        # 6 000 off is more than the remaining 5 000, although the removed work would cover it
        self.client.post(f"/tuning/edit/{order_id}", data=self.form(
            [("Полировка", 500, 10, ids[0]), ("Пайол", 300, 10, ids[1])], removed=(1,),
            discount=("amount", "6000")))
        self.assertEqual(self.order()["discount_value"], 0)

    def test_a_restored_work_counts_again(self):
        order_id = self.make_order()
        self.remove_second_work(order_id)
        item_id = self.item_ids()[1]
        self.client.post(f"/tuning/{order_id}/item/{item_id}/status", data={"status": "approved"})
        self.assertEqual(self.order()["total"], 8000)
        page = self.client.get(f"/tuning/edit/{order_id}").get_data(as_text=True)
        self.assertNotIn("work-row-removed", page.split('id="work-container"')[1].split("</form>")[0].split("<template")[0])


if __name__ == "__main__":
    unittest.main()
