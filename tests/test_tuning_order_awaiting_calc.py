import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class TuningOrderAwaitingCalcTests(unittest.TestCase):
    """An order with a work that has no price yet carries one more badge,
    "Ждёт расчёта", next to its status — whatever that status is."""

    CLIENT_NAME = "tuning-awaiting-calc-test"

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
                "SELECT id FROM tuning_orders WHERE client_name LIKE ?", (self.CLIENT_NAME + "%",)).fetchall()]
            if ids:
                application_module._delete_tuning_order_records(db, ids)
            db.commit()

    def create(self, suffix, works):
        """works: (name, cost, multiplier) — blank cost/multiplier = no price yet."""
        data = MultiDict([
            ("client_name", f"{self.CLIENT_NAME}{suffix}"), ("equipment_type", "boat"),
            ("boat_model", "Salute"), ("boat_registration_number", ""), ("motor_model", ""),
            ("motor_serial_number", ""), ("phone", ""), ("order_date", "2026-09-10"),
            ("sale_channel", "direct"), ("discount_type", "percent"), ("discount_value", "0"),
        ])
        for name, cost, mult in works:
            data.add("work_name[]", name); data.add("cost_price[]", str(cost))
            data.add("multiplier[]", str(mult)); data.add("item_id[]", "")
        self.client.post("/tuning/add", data=data)
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT id FROM tuning_orders WHERE client_name = ?", (f"{self.CLIENT_NAME}{suffix}",)
            ).fetchone()["id"]

    def set_status(self, order_id, status):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_orders SET status = ? WHERE id = ?", (status, order_id))
            db.commit()

    def count(self, order_id):
        with application_module.app.app_context():
            return application_module.tuning_order_pending_price_count(application_module.get_db(), order_id)

    def listing(self, query=""):
        return self.client.get("/tuning" + query).get_data(as_text=True)

    def test_the_count_is_works_without_a_price_that_are_not_removed(self):
        order_id = self.create("A", [("Рассчитанная", 100, 5), ("Без цены", "", ""), ("Ещё без цены", "", "")])
        self.assertEqual(self.count(order_id), 2)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_order_items SET status = 'removed' WHERE order_id = ? AND work_name = 'Без цены'",
                       (order_id,))
            db.commit()
        self.assertEqual(self.count(order_id), 1)

    def test_an_order_with_everything_priced_has_no_badge(self):
        order_id = self.create("B", [("Рассчитанная", 100, 5)])
        self.assertEqual(self.count(order_id), 0)
        self.assertNotIn("status-awaiting-calc", self.client.get(f"/tuning/edit/{order_id}").get_data(as_text=True))
        self.assertNotIn("status-awaiting-calc", self.client.get(f"/tuning/{order_id}/board").get_data(as_text=True))

    def test_the_badge_shows_in_the_list_the_card_and_the_board_whatever_the_status(self):
        for status in ("new", "in_progress", "done", "handed_over"):
            order_id = self.create(f"C{status}", [("Без цены", "", "")])
            self.set_status(order_id, status)
            page = self.listing(f"?q={self.CLIENT_NAME}C{status}")
            self.assertIn("Ждёт расчёта", page, status)
            self.assertIn("status-awaiting-calc", page, status)
            card = self.client.get(f"/tuning/edit/{order_id}").get_data(as_text=True)
            self.assertIn("Ждёт расчёта · 1", card, status)
            board = self.client.get(f"/tuning/{order_id}/board").get_data(as_text=True)
            self.assertIn("Ждёт расчёта · 1", board, status)

    def test_pricing_the_work_removes_the_badge(self):
        order_id = self.create("D", [("Без цены", "", "")])
        self.assertEqual(self.count(order_id), 1)
        with application_module.app.app_context():
            item_id = application_module.get_db().execute(
                "SELECT id FROM tuning_order_items WHERE order_id = ?", (order_id,)).fetchone()["id"]
        data = MultiDict([
            ("client_name", f"{self.CLIENT_NAME}D"), ("equipment_type", "boat"), ("boat_model", "Salute"),
            ("boat_registration_number", ""), ("motor_model", ""), ("motor_serial_number", ""), ("phone", ""),
            ("order_date", "2026-09-10"), ("sale_channel", "direct"), ("discount_type", "percent"),
            ("discount_value", "0"), ("work_name[]", "Без цены"), ("cost_price[]", "100"),
            ("multiplier[]", "3"), ("item_id[]", str(item_id)),
        ])
        self.client.post(f"/tuning/edit/{order_id}", data=data)
        self.assertEqual(self.count(order_id), 0)

    def test_the_list_can_show_only_orders_waiting_for_a_calculation(self):
        waiting = self.create("E", [("Без цены", "", "")])
        ready = self.create("F", [("Рассчитанная", 100, 5)])
        page = self.listing(f"?q={self.CLIENT_NAME}&awaiting=1")
        self.assertIn(f"/tuning/edit/{waiting}", page)
        self.assertNotIn(f"/tuning/edit/{ready}", page)
        self.assertIn('name="awaiting"', page)
        everything = self.listing(f"?q={self.CLIENT_NAME}")
        self.assertIn(f"/tuning/edit/{ready}", everything)


if __name__ == "__main__":
    unittest.main()
