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

    def test_moving_a_boat_only_respects_boats_present_on_the_viewed_day(self):
        key = application_module._tuning_equipment_profile_key("boat", "Тест-катер")
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT OR REPLACE INTO tuning_boat_profiles (model_key, model_name, equipment_type, "
                "specifications, length_m, width_m, created_at, updated_at) "
                "VALUES (?, 'Тест-катер', 'boat', '', 5, 2, 'x', 'x')", (key,)
            )
            other_id = self._insert_order(db)
            db.commit()
        try:
            self.set_dates(self.order_id, "2030-05-10", None)      # in the shop from May 10
            self.set_dates(other_id, "2030-05-11", None)           # arrives a day later
            for order_id in (self.order_id, other_id):
                with application_module.app.app_context():
                    application_module._auto_place_boat_on_shop_map(
                        application_module.get_db(), {"id": order_id}
                    )
            with application_module.app.app_context():
                db = application_module.get_db()
                boat_ids = {
                    r["order_id"]: r["id"] for r in db.execute(
                        "SELECT id, order_id FROM shop_map_boats WHERE order_id IN (?, ?)",
                        (self.order_id, other_id),
                    ).fetchall()
                }
                other_row = db.execute(
                    "SELECT x_m, y_m FROM shop_map_boats WHERE order_id = ?", (other_id,)
                ).fetchone()
                target = (other_row["x_m"], other_row["y_m"])
            drag = lambda date: self.client.post(
                f"/tuning/shop-map/boats/{boat_ids[self.order_id]}/drag",
                data={"x_m": target[0], "y_m": target[1], "date": date},
            )
            # the other boat isn't in the shop on May 10 -> its spot is free that day
            self.assertEqual(drag("2030-05-10").status_code, 200)
            # on May 11 both are there -> too close
            self.assertEqual(drag("2030-05-11").status_code, 400)
            # ... and the map for that day flags the clash
            page = self.client.get("/tuning/shop-map?date=2030-05-11").get_data(as_text=True)
            self.assertIn("is-conflict", page)
            calm = self.client.get("/tuning/shop-map?date=2030-05-10").get_data(as_text=True)
            self.assertNotIn("shop-map-boat-hull is-conflict", calm)
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM shop_map_boats WHERE order_id = ?", (other_id,))
                db.execute("DELETE FROM tuning_orders WHERE id = ?", (other_id,))
                db.execute("DELETE FROM tuning_boat_profiles WHERE model_key = ?", (key,))
                db.commit()

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
