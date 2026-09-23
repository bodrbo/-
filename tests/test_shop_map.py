import unittest

from support import application_module


class ShopMapAutoPlacementTests(unittest.TestCase):
    CLIENT_TOKEN = "shop-map-test-client"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            self.order_id = self._insert_order(db, equipment_type="boat")
            db.commit()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        order_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM tuning_orders WHERE client_name = 'Клиент Карты Цеха'"
            ).fetchall()
        ]
        for order_id in order_ids:
            db.execute("DELETE FROM shop_map_boats WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_orders WHERE id = ?", (order_id,))
        db.commit()

    @staticmethod
    def _insert_order(db, equipment_type="boat", boat_model="Тест-катер"):
        cursor = db.execute(
            "INSERT INTO tuning_orders "
            "(client_name, equipment_type, boat_model, sale_channel, phone, "
            "subtotal, total, status, created_at, updated_at) "
            "VALUES ('Клиент Карты Цеха', ?, ?, 'direct', '+79990003344', "
            "1000, 1000, 'estimate', '2026-09-01 10:00', '2026-09-01 10:00')",
            (equipment_type, boat_model),
        )
        return cursor.lastrowid

    def set_status(self, order_id, status):
        return self.client.post(
            f"/tuning/{order_id}/status", data={"status": status}
        )

    def is_on_map(self, order_id):
        with application_module.app.app_context():
            db = application_module.get_db()
            return db.execute(
                "SELECT 1 FROM shop_map_boats WHERE order_id = ?", (order_id,)
            ).fetchone() is not None

    def test_in_progress_places_boat_on_map(self):
        self.assertFalse(self.is_on_map(self.order_id))
        response = self.set_status(self.order_id, "in_progress")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.is_on_map(self.order_id))

    def boats_on(self, date):
        with application_module.app.app_context():
            db = application_module.get_db()
            return [b["order_id"] for b in application_module._shop_map_boats(db, on_date=date)]

    def set_dates(self, order_id, acceptance=None, deadline=None):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE tuning_orders SET acceptance_date = ?, deadline_date = ? WHERE id = ?",
                (acceptance, deadline, order_id),
            )
            db.commit()

    def test_handed_over_keeps_history_but_boat_leaves_after_completion(self):
        self.set_status(self.order_id, "in_progress")
        self.assertTrue(self.is_on_map(self.order_id))
        response = self.set_status(self.order_id, "handed_over")
        self.assertEqual(response.status_code, 302)
        # The row stays so the timeline keeps its history ...
        self.assertTrue(self.is_on_map(self.order_id))
        # ... the boat stands in the shop until its completion day, not after.
        today = application_module.dt.date.today()
        self.assertIn(self.order_id, self.boats_on(today.isoformat()))
        self.assertNotIn(
            self.order_id, self.boats_on((today + application_module.dt.timedelta(days=1)).isoformat())
        )

    def test_handed_over_without_ever_being_placed_is_a_no_op(self):
        response = self.set_status(self.order_id, "handed_over")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.is_on_map(self.order_id))

    def test_presence_follows_acceptance_date_and_deadline(self):
        self.set_dates(self.order_id, "2030-05-10", "2030-05-20")
        self.set_status(self.order_id, "in_progress")
        self.assertTrue(self.is_on_map(self.order_id))
        self.assertNotIn(self.order_id, self.boats_on("2030-05-09"))
        self.assertIn(self.order_id, self.boats_on("2030-05-10"))
        self.assertIn(self.order_id, self.boats_on("2030-05-20"))
        self.assertNotIn(self.order_id, self.boats_on("2030-05-21"))

    def test_acceptance_date_places_boat_before_work_starts(self):
        self.set_dates(self.order_id, "2030-06-01", "2030-06-05")
        with application_module.app.app_context():
            application_module._auto_place_boat_on_shop_map(
                application_module.get_db(), {"id": self.order_id}
            )
        self.assertTrue(self.is_on_map(self.order_id))
        self.assertIn(self.order_id, self.boats_on("2030-06-03"))

    def test_estimate_without_acceptance_date_is_never_shown(self):
        with application_module.app.app_context():
            application_module._auto_place_boat_on_shop_map(
                application_module.get_db(), {"id": self.order_id}
            )
        self.assertFalse(self.is_on_map(self.order_id))

    def test_timeline_page_and_bad_date(self):
        self.set_dates(self.order_id, "2030-05-10", "2030-05-20")
        self.set_status(self.order_id, "in_progress")
        page = self.client.get("/tuning/shop-map?date=2030-05-10").get_data(as_text=True)
        self.assertIn("Лента времени", page)
        self.assertIn("Приходит", page)
        self.assertIn("Клиент Карты Цеха", page)
        later = self.client.get("/tuning/shop-map?date=2030-05-25").get_data(as_text=True)
        self.assertNotIn("Приходит", later)
        self.assertEqual(self.client.get("/tuning/shop-map?date=junk").status_code, 200)

    def test_boats_only_block_each_other_while_both_are_in_the_shop(self):
        overlap = application_module._shop_map_intervals_overlap
        self.assertFalse(overlap("2030-01-01", "2030-01-10", "2030-01-11", "2030-01-20"))
        self.assertTrue(overlap("2030-01-01", "2030-01-10", "2030-01-10", "2030-01-20"))
        self.assertTrue(overlap("2030-01-01", None, "2030-03-01", "2030-03-05"))
        self.assertFalse(overlap("2030-01-01", "2030-01-10", None, None))

    def test_toggling_status_back_and_forth_stays_consistent(self):
        self.set_status(self.order_id, "in_progress")
        self.assertTrue(self.is_on_map(self.order_id))
        self.set_status(self.order_id, "handed_over")
        self.set_status(self.order_id, "in_progress")
        self.assertTrue(self.is_on_map(self.order_id))
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM shop_map_boats WHERE order_id = ?", (self.order_id,)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_non_boat_equipment_never_gets_placed(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            motor_order_id = self._insert_order(
                db, equipment_type="motor", boat_model=""
            )
            db.commit()
        try:
            self.set_status(motor_order_id, "in_progress")
            self.assertFalse(self.is_on_map(motor_order_id))
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM shop_map_boats WHERE order_id = ?", (motor_order_id,))
                db.execute("DELETE FROM tuning_orders WHERE id = ?", (motor_order_id,))
                db.commit()


if __name__ == "__main__":
    unittest.main()
