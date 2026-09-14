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

    def test_handed_over_removes_boat_from_map(self):
        self.set_status(self.order_id, "in_progress")
        self.assertTrue(self.is_on_map(self.order_id))
        response = self.set_status(self.order_id, "handed_over")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.is_on_map(self.order_id))
        with application_module.app.app_context():
            db = application_module.get_db()
            order = db.execute(
                "SELECT status FROM tuning_orders WHERE id = ?", (self.order_id,)
            ).fetchone()
        self.assertEqual(order["status"], "handed_over")

    def test_handed_over_without_ever_being_placed_is_a_no_op(self):
        response = self.set_status(self.order_id, "handed_over")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.is_on_map(self.order_id))

    def test_toggling_status_back_and_forth_stays_consistent(self):
        self.set_status(self.order_id, "in_progress")
        self.assertTrue(self.is_on_map(self.order_id))
        self.set_status(self.order_id, "handed_over")
        self.assertFalse(self.is_on_map(self.order_id))
        self.set_status(self.order_id, "in_progress")
        self.assertTrue(self.is_on_map(self.order_id))

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
