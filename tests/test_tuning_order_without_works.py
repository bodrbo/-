import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class TuningOrderWithoutWorksTests(unittest.TestCase):
    """An administrator can create and save an order without a single work;
    partner estimate requests still need at least one."""

    CLIENT_NAME = "tuning-no-works-test"

    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        self._cleanup()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            ids = [r["id"] for r in db.execute(
                "SELECT id FROM tuning_orders WHERE client_name LIKE ?", (self.CLIENT_NAME + "%",)).fetchall()]
            if ids:
                application_module._delete_tuning_order_records(db, ids)
            db.commit()

    def form(self, suffix, works=(), item_ids=()):
        data = MultiDict([
            ("client_name", f"{self.CLIENT_NAME}{suffix}"), ("equipment_type", "boat"),
            ("boat_model", "Salute"), ("boat_registration_number", ""), ("motor_model", ""),
            ("motor_serial_number", ""), ("phone", ""), ("order_date", "2026-11-17"),
            ("sale_channel", "direct"), ("discount_type", "percent"), ("discount_value", "0"),
        ])
        for index, (name, cost, mult) in enumerate(works):
            data.add("work_name[]", name); data.add("cost_price[]", str(cost))
            data.add("multiplier[]", str(mult))
            data.add("item_id[]", str(item_ids[index]) if index < len(item_ids) else "")
        if not works:  # the form's empty row
            for field in ("work_name[]", "cost_price[]", "multiplier[]", "item_id[]"):
                data.add(field, "")
        return data

    def order(self, suffix):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT * FROM tuning_orders WHERE client_name = ?", (f"{self.CLIENT_NAME}{suffix}",)
            ).fetchone()

    def items(self, order_id):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT * FROM tuning_order_items WHERE order_id = ?", (order_id,)).fetchall()

    def test_an_order_can_be_created_without_works(self):
        response = self.client.post("/tuning/add", data=self.form("A"))
        self.assertEqual(response.status_code, 302)
        order = self.order("A")
        self.assertIsNotNone(order)
        self.assertEqual(self.items(order["id"]), [])
        self.assertEqual(order["total"], 0)

    def test_all_works_can_be_removed_from_an_existing_order(self):
        self.client.post("/tuning/add", data=self.form("B", works=[("Шумоизоляция", 100, 2)]))
        order = self.order("B")
        (item,) = self.items(order["id"])
        response = self.client.post(f"/tuning/edit/{order['id']}", data=self.form("B"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.items(order["id"]), [])
        self.assertEqual(self.order("B")["total"], 0)
        page = self.client.get(f"/tuning/edit/{order['id']}").get_data(as_text=True)
        self.assertNotIn("Добавьте хотя бы одну работу", page)

    def test_other_fields_save_on_an_order_without_works(self):
        self.client.post("/tuning/add", data=self.form("C"))
        order = self.order("C")
        data = self.form("C")
        data["boat_model"] = "Касатка 520"
        self.client.post(f"/tuning/edit/{order['id']}", data=data)
        self.assertEqual(self.order("C")["boat_model"], "Касатка 520")

    def test_partner_estimate_request_still_needs_a_work(self):
        errors, data = application_module._process_partner_estimate_request(
            MultiDict([("equipment_type", "boat"), ("boat_model", "Salute"), ("work_name[]", "")]),
            {"id": 1, "client_name": f"{self.CLIENT_NAME}P", "phone": ""},
        )
        self.assertIsNone(data)
        self.assertIn("Добавьте хотя бы одну работу.", errors)


if __name__ == "__main__":
    unittest.main()
